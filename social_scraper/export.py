"""Copy new and changed operational rows into the lake.

Each dataset has a watermark (`export_watermarks`): the change-timestamp through which
it has been exported. A run reads rows past it in batches, drops any post whose text
matches a credential shape (`privacy.contains_secret` -- dropped, never uploaded),
merges each partition (`archive.writer`), and only then advances the watermark. A
failure leaves the watermark where it was, so the next run retries the same rows.

An unwritable lake **degrades, never blocks** (`.claude/rules/data-lake.md`): the error
is returned in the result for the run artifact, and scraping carries on.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
from sqlalchemy import Table, select
from sqlalchemy.engine import Connection, Engine

from social_scraper.archive import datasets
from social_scraper.archive.manifest import DatasetSpec, ManifestError
from social_scraper.archive.writer import VerifyError, write_partition
from social_scraper.db import repository, schema
from social_scraper.models import as_utc
from social_scraper.privacy import contains_secret

log = logging.getLogger(__name__)

BATCH_ROWS = 20_000

# dataset -> (operational table, the column whose growth means "changed since")
SOURCES: dict[str, tuple[Table, str]] = {
    datasets.POSTS.name: (schema.posts, "updated_at"),
    datasets.MENTIONS.name: (schema.mentions, "updated_at"),
    datasets.ENGAGEMENT.name: (schema.engagement, "observed_at"),
    datasets.SYMBOLS.name: (schema.symbols, "updated_at"),
}


@dataclass
class DatasetExport:
    dataset: str
    rows: int = 0
    partitions: int = 0
    dropped_secrets: int = 0
    error: str | None = None


@dataclass
class ExportResult:
    datasets: list[DatasetExport] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(d.error is None for d in self.datasets)


def fetch_batch(
    conn: Connection, table: Table, ts_column: str, since: datetime | None, limit: int
) -> list[dict[str, Any]]:
    """Up to `limit` rows past `since`, never splitting rows that share a timestamp.

    One ingest stamps a whole batch with the same `updated_at`, so a plain LIMIT could
    stop halfway through it -- and the next `> watermark` read would skip the rest.
    When the page is full, rows at the last timestamp are held back for the next run;
    if the whole page is one timestamp, every row at it is returned.
    """
    rows = repository.rows_since(conn, table, ts_column, since, limit)
    if len(rows) < limit:
        return rows
    last = rows[-1][ts_column]
    head = [r for r in rows if r[ts_column] != last]
    if head:
        return head
    col = table.c[ts_column]
    return [dict(r._mapping) for r in conn.execute(select(table).where(col == last))]


def lake_rows(spec: DatasetSpec, rows: list[dict[str, Any]]) -> tuple[list[dict], int]:
    """Rows projected onto `spec`, with secret-bearing posts removed. `(rows, dropped)`."""
    out, dropped = [], 0
    check = spec is datasets.POSTS
    for row in rows:
        if check and contains_secret(row.get("title"), row.get("body"), row.get("url")):
            dropped += 1
            continue
        out.append(datasets.to_lake_row(spec, row))
    return out, dropped


def export_dataset(
    engine: Engine, root: Path, spec: DatasetSpec, now: datetime, batch: int = BATCH_ROWS
) -> DatasetExport:
    table, ts_column = SOURCES[spec.name]
    report = DatasetExport(spec.name)
    while True:
        with engine.connect() as conn:
            rows = fetch_batch(
                conn, table, ts_column, repository.get_watermark(conn, spec.name), batch
            )
        if not rows:
            break
        out, dropped = lake_rows(spec, rows)
        groups: dict[str, list[dict]] = defaultdict(list)
        for row in out:
            groups[spec.partition_of(row)].append(row)
        for part, group in groups.items():
            write_partition(root, spec, part, group, now)
        with engine.begin() as conn:
            repository.set_watermark(conn, spec.name, as_utc(rows[-1][ts_column]), now)
        report.rows += len(out)
        report.partitions += len(groups)
        report.dropped_secrets += dropped
        # No "short page means done" shortcut: `fetch_batch` returns a short page when
        # it holds back a split timestamp, and stopping there would strand those rows.
    return report


def export_all(engine: Engine, root: Path, now: datetime) -> ExportResult:
    """Export every dataset; one failing dataset does not stop the others."""
    result = ExportResult()
    for spec in datasets.ALL:
        try:
            report = export_dataset(engine, root, spec, now)
        except (OSError, VerifyError, ManifestError, pa.ArrowException) as exc:
            log.warning("export of %s failed: %s", spec.name, exc)
            report = DatasetExport(spec.name, error=f"{type(exc).__name__}: {exc}")
        result.datasets.append(report)
    return result
