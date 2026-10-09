"""Bulk history into the lake: what every archive importer shares.

An importer (`reddit/dumps.py`, `x/tweet_dumps.py`, `stocktwits/dumps.py`,
`bluesky/dumps.py`, `hackernews/dumps.py`) turns its archive's records into
`Post`s and hands each to `lake_post`, which runs the crawler's own `pipeline.build_row`
-- the same ids, author hash, scrubbing, symbol extraction and stance as a scraped post
-- and returns the `social_scraper_posts` and `social_scraper_mentions` rows `export`
would have written for it.

**Straight to the lake, not through the database.** An archive can be hundreds of
millions of records; upserting them row by row into the operational database the
scheduled crawl uses, only to export them again, would take days and bloat the hot
path's tables. A run streams every input into a DuckDB `Spool` on disk, keeps the
newest copy of each post (the same post can be in two archives), then writes each
day's partition once, through `archive.writer` -- merge, verify, catalog -- into every
configured store.

**Never over a live row.** Partitions merge with `keep_existing`: a post the scraper
already exported keeps the scraper's copy, which was observed later. `fetched_at` is
when the archive captured the post, so ibkr_trader's restore, which refreshes a stored
row only from a later `fetched_at`, never lets a backfill overwrite a live observation
either. `extra.backfill` names the file a row came from. Re-running an import is
idempotent.

What a backfill does not carry: engagement snapshots (an archive kept one observation
per post, which stays on the row) and the symbol registry (`social_scraper_symbols`), a
single file the live export rewrites from the database's counts.

**A general-topic archive needs stronger evidence.** A finance subreddit or StockTwits
is about stocks, so an upper-case word that is a listed symbol usually is one. On
Bluesky or Hacker News it usually is not ("AMD" maybe, "NOW" or "PATH" rarely), so
those importers call `lake_post_strict`: a post is kept only when it writes an
explicit, listed `$TICKER` (`extract.has_strict_cashtag`).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa

from social_scraper.archive import datasets
from social_scraper.archive.manifest import DatasetSpec, ManifestError
from social_scraper.archive.store import ObjectStore
from social_scraper.archive.writer import KEEP_EXISTING, VerifyError, write_partition
from social_scraper.export import lake_rows
from social_scraper.models import Post
from social_scraper.pipeline import build_row
from social_scraper.symbols.extract import Mention, extract_mentions, has_strict_cashtag
from social_scraper.symbols.universe import Universe

log = logging.getLogger(__name__)

# The key in a post's `extra` naming the file it came from.
BACKFILL_KEY = "backfill"
# How an archived post reached the lake, beside the crawler's `search` and `timeline`;
# every importer but Reddit's (whose channel is the subreddit) uses it.
ARCHIVE_CHANNEL = "archive"
SPOOL_BATCH = 50_000
READ_BATCH = 100_000
# DuckDB takes 80% of RAM by default; a full backfill's spool got the run killed for low
# memory on a 16 GB desktop (2026-10-05). Capped, it spills to `temp_directory` instead,
# and fewer threads keep fewer per-thread buffers alive.
SPOOL_MEMORY = "1.5GB"
SPOOL_THREADS = 4
LOG_EVERY = 1_000_000

# Why a record was not kept, shared by every importer; each adds its own.
UNPARSABLE = "unparsable"
OUT_OF_RANGE = "out_of_range"
NO_SYMBOL = "no_symbol"
NO_CASHTAG = "no_cashtag"  # `lake_post_strict`: no explicit, listed $TICKER
HAS_CREDENTIAL = "has_credential"  # matched `privacy.contains_secret`: never written

PART = "part"


def _spool_schema(spec: DatasetSpec) -> pa.Schema:
    return spec.schema.append(pa.field(PART, pa.string()))


POSTS_SPOOL = _spool_schema(datasets.POSTS)
MENTIONS_SPOOL = _spool_schema(datasets.MENTIONS)

# One row per natural key, the newest observation of a post first, ordered so that each
# partition's rows arrive together.
POSTS_SORTED = """
SELECT * FROM posts
QUALIFY row_number() OVER (
    PARTITION BY platform, external_id ORDER BY fetched_at DESC NULLS LAST) = 1
ORDER BY part
"""
MENTIONS_SORTED = """
SELECT * FROM mentions
QUALIFY row_number() OVER (PARTITION BY platform, external_id, symbol ORDER BY match_type) = 1
ORDER BY part
"""
# `comments` counts replies on any platform: a Reddit comment, an X reply.
BY_YEAR = """
WITH p AS (
    SELECT year(created_at) AS y,
           count(DISTINCT external_id) AS posts,
           count(DISTINCT external_id) FILTER (WHERE kind IN ('comment', 'reply')) AS comments
    FROM posts GROUP BY 1
), m AS (
    SELECT year(created_at) AS y, count(DISTINCT symbol) AS symbols
    FROM mentions WHERE in_universe GROUP BY 1
)
SELECT CAST(p.y AS VARCHAR) AS year, p.posts, p.comments, coalesce(m.symbols, 0) AS symbols
FROM p LEFT JOIN m USING (y)
ORDER BY p.y
"""


@dataclass(frozen=True)
class ArchiveFilter:
    """What every importer but Reddit's filters on: a time window, and `--all-posts`."""

    since: datetime | None = None
    until: datetime | None = None  # exclusive
    all_posts: bool = False

    def in_range(self, created: datetime) -> bool:
        if self.since is not None and created < self.since:
            return False
        return self.until is None or created < self.until


@dataclass(frozen=True)
class Converted:
    """One archive record: why it was skipped, or the lake rows it becomes."""

    skip: str | None
    post: dict[str, Any] | None = None
    mentions: tuple[dict[str, Any], ...] = ()


@dataclass
class FileStats:
    path: str
    lines: int = 0
    bad_lines: int = 0
    kept: int = 0
    mentions: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    error: str | None = None

    def skip(self, reason: str, count: int = 1) -> None:
        if count:
            self.skipped[reason] = self.skipped.get(reason, 0) + count


@dataclass
class ImportReport:
    started_at: str
    finished_at: str | None = None
    dry_run: bool = False
    files: list[FileStats] = field(default_factory=list)
    # After de-duplication across files: what the lake gets.
    posts: int = 0
    mentions: int = 0
    by_year: list[dict[str, Any]] = field(default_factory=list)
    partitions: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def lake_post(
    post: Post,
    universe: Universe,
    source: str,
    observed: datetime | None = None,
    extra: dict[str, Any] | None = None,
    all_posts: bool = False,
) -> Converted:
    """The lake rows for one parsed archive post. Pure.

    `observed` is when the archive captured it (its engagement's time; the post's own
    time when unknown). `extra` lands in the row's `extra` beside `backfill: source`.
    Without `all_posts`, a post that names no symbol is skipped before the costlier half.
    """
    if not all_posts and not extract_mentions(post.text, universe, post.tagged_symbols):
        return Converted(NO_SYMBOL)
    row, found = build_row(post, universe)
    row["first_seen_at"] = row["last_seen_at"] = observed or post.created_at
    row["raw"] = {**(row["raw"] or {}), **(extra or {}), BACKFILL_KEY: source}
    kept, dropped = lake_rows(datasets.POSTS, [row])
    if dropped:
        return Converted(HAS_CREDENTIAL)
    lake = {**kept[0], PART: datasets.POSTS.partition_of(kept[0])}
    return Converted(None, lake, mentions_of(row, found))


def lake_post_strict(
    post: Post, universe: Universe, source: str, extra: dict[str, Any] | None, all_posts: bool
) -> Converted:
    """`lake_post` for a general-topic platform: without `all_posts`, a post is kept only
    when it writes an explicit, listed `$TICKER` (`NO_CASHTAG` otherwise)."""
    if not all_posts and not has_strict_cashtag(post.text, universe):
        return Converted(NO_CASHTAG)
    return lake_post(post, universe, source, extra=extra, all_posts=all_posts)


def mentions_of(row: dict[str, Any], found: Iterable[Mention]) -> tuple[dict[str, Any], ...]:
    """The `social_scraper_mentions` rows for a post row, each with its partition."""
    mentions = []
    for m in found:
        mention = datasets.to_lake_row(
            datasets.MENTIONS,
            {
                "platform": row["platform"],
                "external_id": row["external_id"],
                "symbol": m.symbol,
                "match_type": m.match_type,
                "in_universe": m.in_universe,
                "channel": row["channel"],
                "created_at": row["created_at"],
            },
        )
        mentions.append({**mention, PART: datasets.MENTIONS.partition_of(mention)})
    return tuple(mentions)


def connect(database: Path, temp: Path) -> duckdb.DuckDBPyConnection:
    """A DuckDB file for an import's own use, capped like the spool and spilling to `temp`.

    UTC, so timestamps come back to Arrow in the lake's zone and `year()` is a UTC year.
    Each connection gets its own `temp`: two sharing one could clash on spill files.
    """
    config: dict[str, str | bool | int | float | list[str]] = {
        "temp_directory": str(temp),
        "TimeZone": "UTC",
        "memory_limit": SPOOL_MEMORY,
        "threads": SPOOL_THREADS,
    }
    return duckdb.connect(str(database), config=config)


class Spool:
    """Rows on their way to the lake, in a DuckDB file under `directory`.

    On disk rather than in memory: a filtered decade of r/wallstreetbets does not fit,
    and DuckDB sorts out of core. `take` buffers converted records and flushes them in
    batches of `SPOOL_BATCH`; `flush` writes what is left.
    """

    def __init__(self, directory: Path) -> None:
        self.con = connect(directory / "spool.duckdb", directory / "tmp")
        self._posts: list[dict[str, Any]] = []
        self._mentions: list[dict[str, Any]] = []
        # Replaced, not created: a spool file left by an interrupted run starts empty.
        posts, mentions = POSTS_SPOOL.empty_table(), MENTIONS_SPOOL.empty_table()
        self._append("CREATE OR REPLACE TABLE posts AS SELECT * FROM batch", posts)
        self._append("CREATE OR REPLACE TABLE mentions AS SELECT * FROM batch", mentions)

    def _append(self, sql: str, batch: pa.Table) -> None:
        self.con.register("batch", batch)
        try:
            self.con.execute(sql)
        finally:
            self.con.unregister("batch")

    def add(self, posts: list[dict[str, Any]], mentions: list[dict[str, Any]]) -> None:
        if posts:
            table = pa.Table.from_pylist(posts, schema=POSTS_SPOOL)
            self._append("INSERT INTO posts SELECT * FROM batch", table)
        if mentions:
            table = pa.Table.from_pylist(mentions, schema=MENTIONS_SPOOL)
            self._append("INSERT INTO mentions SELECT * FROM batch", table)

    def take(self, result: Converted, stats: FileStats) -> None:
        """Count `result` in `stats`, and buffer its rows if it was kept."""
        if result.post is None:
            stats.skip(result.skip or UNPARSABLE)
            return
        stats.kept += 1
        stats.mentions += len(result.mentions)
        self._posts.append(result.post)
        self._mentions.extend(result.mentions)
        if len(self._posts) >= SPOOL_BATCH:
            self.flush()

    def flush(self) -> None:
        self.add(self._posts, self._mentions)
        self._posts, self._mentions = [], []

    def sorted_posts(self) -> pa.RecordBatchReader:
        return self.con.execute(POSTS_SORTED).to_arrow_reader(READ_BATCH)

    def sorted_mentions(self) -> pa.RecordBatchReader:
        return self.con.execute(MENTIONS_SORTED).to_arrow_reader(READ_BATCH)

    def summary(self) -> tuple[int, int, list[dict[str, Any]]]:
        """`(unique posts, unique mentions, per-year counts)` -- what the lake gets."""
        posts = self.con.execute(
            "SELECT count(*) FROM (SELECT DISTINCT platform, external_id FROM posts)"
        ).fetchone()
        mentions = self.con.execute(
            "SELECT count(*) FROM (SELECT DISTINCT platform, external_id, symbol FROM mentions)"
        ).fetchone()
        cursor = self.con.execute(BY_YEAR)
        names = [d[0] for d in cursor.description]
        by_year = [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
        return (posts[0] if posts else 0), (mentions[0] if mentions else 0), by_year

    def close(self) -> None:
        self.con.close()

    def __enter__(self) -> Spool:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def partition_runs(batches: Iterable[pa.RecordBatch]) -> Iterator[tuple[str, pa.Table]]:
    """`(partition, rows)` from batches already ordered by the `part` column."""
    current: str | None = None
    pieces: list[pa.Table] = []
    for batch in batches:
        table = pa.Table.from_batches([batch])
        parts = table.column(PART).to_pylist()
        start = 0
        for i in range(1, len(parts) + 1):
            if i < len(parts) and parts[i] == parts[start]:
                continue
            if parts[start] != current and pieces:
                yield str(current), pa.concat_tables(pieces)
                pieces = []
            current = parts[start]
            pieces.append(table.slice(start, i - start))
            start = i
    if pieces:
        yield str(current), pa.concat_tables(pieces)


def write_spool(
    spool: Spool, stores: Sequence[ObjectStore], now: datetime, report: ImportReport
) -> None:
    """Write each spooled partition into every store, once. Stops at the first failure."""
    # Each reader is opened only when its turn comes: a second query on the connection
    # silently ends the first one's result set (measured -- the posts came back empty).
    for spec, open_reader in (
        (datasets.POSTS, spool.sorted_posts),
        (datasets.MENTIONS, spool.sorted_mentions),
    ):
        written = 0
        for part, rows in partition_runs(open_reader()):
            for store in stores:
                write_partition(store, spec, part, rows, now, KEEP_EXISTING)
            written += 1
        report.partitions[spec.name] = written


SpoolOne = Callable[[Path, Spool], FileStats]


def run_import(
    paths: Sequence[Path],
    spool_one: SpoolOne,
    stores: Sequence[ObjectStore] | None,
    spool_dir: Path,
    now: datetime,
) -> ImportReport:
    """`spool_one` every input into one spool, then write the lake partitions to `stores`.

    `stores=None` is a dry run: everything is read and counted, nothing is written.
    """
    report = ImportReport(started_at=now.isoformat(), dry_run=stores is None)
    with Spool(spool_dir) as spool:
        for path in paths:
            stats = spool_one(path, spool)
            spool.flush()  # in case `spool_one` left rows buffered; a no-op otherwise
            report.files.append(stats)
            if stats.error:
                report.errors.append(f"{path}: {stats.error}")
            log.info("%s: %d of %d records kept", path.name, stats.kept, stats.lines)
        report.posts, report.mentions, report.by_year = spool.summary()
        if stores is not None:
            try:
                write_spool(spool, stores, now, report)
            # StoreError is an OSError: an unreachable bucket fails like a full disk.
            except (OSError, VerifyError, ManifestError, pa.ArrowException) as exc:
                report.errors.append(f"lake write: {type(exc).__name__}: {exc}")
    report.finished_at = datetime.now(UTC).isoformat()
    return report


def write_report(report: ImportReport, logs_dir: Path, name: str) -> Path:
    """Overwrite `logs/<name>` with the report -- on failure as on success."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    path = logs_dir / name
    body = {**asdict(report), "ok": report.ok}
    path.write_text(json.dumps(body, indent=2, default=str), encoding="utf-8")
    return path
