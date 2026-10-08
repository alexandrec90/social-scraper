"""Copy new and changed operational rows into the lake.

Each dataset has a watermark (`export_watermarks`): the change-timestamp through which
it has been exported. A run reads rows past it in batches, drops any post whose text
matches a credential shape (`privacy.contains_secret` -- dropped, never uploaded),
merges each partition into every store (`archive.writer`; the local tree, then the
bucket when `ARCHIVE_BACKEND=s3`), and only then advances the watermark. A failure in
any store leaves the watermark where it was, so the next run retries the same rows --
the merge is idempotent, so a store that already took them takes them again unchanged.

An unwritable lake **degrades, never blocks** (`.claude/rules/data-lake.md`): the error
is returned in the result for the run artifact, and scraping carries on.

A scheduled `scrape` exports to a `Deadline` inside its cycle. Past it, a dataset is
**deferred**: no further partition is started, a wait for a partition's lock gives up,
and the watermark stays put, so the next run exports the same rows. That is a notice,
not an error -- nothing is wrong with the lake. On 2026-10-07 a `backfill export-full`
held the lake's locks while the 20:30 scrape exported, and an export bounded only by
each lock's own ten-minute timeout ran 35 minutes past its crawl, so the 21:00 fire
was skipped.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pyarrow as pa
from sqlalchemy import Table, select
from sqlalchemy.engine import Connection, Engine

from social_scraper.archive import datasets
from social_scraper.archive.lock import TIMEOUT_S, LockTimeout
from social_scraper.archive.manifest import DatasetSpec, ManifestError
from social_scraper.archive.store import ObjectStore, StoreError, stores_for
from social_scraper.archive.writer import VerifyError, WriteOptions, write_partition
from social_scraper.config import Settings
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
    datasets.SPORTS_POSTS.name: (schema.sports_posts, "updated_at"),
    datasets.SPORTS_MENTIONS.name: (schema.sports_mentions, "updated_at"),
    datasets.SPORTS_ENGAGEMENT.name: (schema.sports_engagement, "observed_at"),
    datasets.SPORTS_ENTITIES.name: (schema.sports_entities, "updated_at"),
    datasets.SPORTS_ENTITY_LEAGUES.name: (schema.sports_entity_leagues, "refreshed_at"),
}


@dataclass
class DatasetExport:
    dataset: str
    rows: int = 0
    partitions: int = 0
    dropped_secrets: int = 0
    error: str | None = None
    # Why the rest of this dataset waits for the next run: the export ran out of time.
    deferred: str | None = None


@dataclass
class ExportResult:
    datasets: list[DatasetExport] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(d.error is None for d in self.datasets)

    @property
    def deferred(self) -> list[str]:
        return [d.dataset for d in self.datasets if d.deferred]


Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Deadline:
    """When an export stops (`until`; None for never), read on `clock`."""

    until: datetime | None = None
    clock: Clock = _utcnow

    def passed(self) -> bool:
        return self.until is not None and self.clock() >= self.until

    def lock_wait_s(self) -> float:
        """How long a partition's lock may be waited for: the time left, never more
        than the lock's own timeout, and none once it has passed."""
        if self.until is None:
            return TIMEOUT_S
        return min(TIMEOUT_S, max(0.0, (self.until - self.clock()).total_seconds()))

    def deferral(self, why: str = "") -> str:
        return f"out of export time at {self.until:%H:%M:%S}Z" + (f" ({why})" if why else "")


NO_DEADLINE = Deadline()


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
    check = spec in datasets.TEXT
    for row in rows:
        if check and contains_secret(row.get("title"), row.get("body"), row.get("url")):
            dropped += 1
            continue
        out.append(datasets.to_lake_row(spec, row))
    return out, dropped


def export_dataset(
    engine: Engine,
    stores: Sequence[ObjectStore],
    spec: DatasetSpec,
    now: datetime,
    batch: int = BATCH_ROWS,
    deadline: Deadline = NO_DEADLINE,
) -> DatasetExport:
    """Export `spec`'s rows past its watermark. Past `deadline`, the rest is `deferred`."""
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
        report.deferred = _write_groups(stores, spec, groups, now, deadline)
        if report.deferred:
            return report  # the watermark stays: the next run exports these rows again
        with engine.begin() as conn:
            repository.set_watermark(conn, spec.name, as_utc(rows[-1][ts_column]), now)
        report.rows += len(out)
        report.partitions += len(groups)
        report.dropped_secrets += dropped
        # No "short page means done" shortcut: `fetch_batch` returns a short page when
        # it holds back a split timestamp, and stopping there would strand those rows.
    return report


def _write_groups(
    stores: Sequence[ObjectStore],
    spec: DatasetSpec,
    groups: dict[str, list[dict]],
    now: datetime,
    deadline: Deadline,
) -> str | None:
    """Write each partition to each store. Why the rest was deferred, or None when all of
    it was written."""
    for part, group in groups.items():
        for store in stores:
            if deadline.passed():
                return deadline.deferral()
            wait = deadline.lock_wait_s()
            try:
                write_partition(store, spec, part, group, now, WriteOptions(wait_s=wait))
            except LockTimeout as exc:
                if wait >= TIMEOUT_S:
                    raise  # the lock's own timeout, not the deadline: a real failure
                return deadline.deferral(str(exc))
    return None


def export_all(
    engine: Engine,
    stores: Sequence[ObjectStore],
    now: datetime,
    deadline: Deadline = NO_DEADLINE,
) -> ExportResult:
    """Export every dataset to every store; one failing dataset does not stop the others.

    Past `deadline`, every dataset not yet done is deferred to the next run."""
    result = ExportResult()
    for spec in datasets.ALL:
        try:
            report = export_dataset(engine, stores, spec, now, deadline=deadline)
        # StoreError is an OSError: an unreachable or refusing bucket fails like a full disk.
        except (OSError, VerifyError, ManifestError, pa.ArrowException) as exc:
            log.warning("export of %s failed: %s", spec.name, exc)
            report = DatasetExport(spec.name, error=f"{type(exc).__name__}: {exc}")
        if report.deferred:
            log.warning("export of %s deferred to the next run: %s", spec.name, report.deferred)
        result.datasets.append(report)
    return result


def export_to_lake(
    engine: Engine,
    settings: Settings,
    now: datetime,
    client: Any = None,
    until: datetime | None = None,
) -> ExportResult:
    """`export_all` into the stores `settings` configure (`client`: an S3 client to use),
    stopping at `until` when given.

    A bucket that cannot even be configured fails every dataset rather than raising, so
    a run still writes its report and `scrape` still exits 1.
    """
    try:
        stores = stores_for(settings, client)
    except StoreError as exc:
        log.warning("lake export not configured: %s", exc)
        error = f"{type(exc).__name__}: {exc}"
        return ExportResult([DatasetExport(spec.name, error=error) for spec in datasets.ALL])
    return export_all(engine, stores, now, Deadline(until))
