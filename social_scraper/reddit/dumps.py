"""Backfill Reddit history from Pushshift / Arctic Shift dump files into the lake.

The live crawler only reaches what a listing still shows, so the lake starts the day the
scraper did. A sentiment feature needs years. The community archives publish that
history as newline-delimited JSON, one Reddit API object per line, zstd-compressed:

- Watchful1's per-subreddit torrents on Academic Torrents (`<sub>_submissions.zst`,
  `<sub>_comments.zst`), built from Pushshift;
- Arctic Shift's monthly dumps and its per-subreddit download tool (`.zst` or `.jsonl`).

Each line is the `data` object a listing child carries, so it goes through the crawler's
own `parse_thing` and `pipeline.build_row` -- the same ids, author hash, scrubbing,
symbol extraction and stance as a scraped post -- and the result is what `export` would
have written for it.

**Straight to the lake, not through the database.** r/wallstreetbets alone is hundreds
of millions of comments; upserting them row by row into the operational database the
scheduled crawl uses, only to export them again, would take days and bloat the hot
path's tables. A run streams every input into a DuckDB spool on disk, keeps the newest
copy of each post (the same post can be in a torrent and a monthly dump), then writes
each day's partition once, through `archive.writer` -- merge, verify, catalog -- into
every configured store.

**Only what a sentiment feature can use, by default:** posts in `REDDIT_SUBREDDITS`,
created since 2015 (ibkr_trader's default training start), that mention at least one
symbol -- a cashtag counts even outside today's symbol directory, which is how a ticker
delisted since keeps its history. `all_posts` keeps the rest.

**Never over a live row.** Partitions merge with `keep_existing`: a post the scraper
already exported keeps the scraper's copy, which was observed later. `fetched_at` is
when the archive captured the post (`retrieved_on`), so ibkr_trader's restore, which
refreshes a stored row only from a later `fetched_at`, never lets a backfill overwrite
a live observation either. Re-running an import is idempotent.

What a backfill does not carry, and why:

- **Engagement snapshots.** An archive kept one observation per post -- Arctic Shift's
  from about 36 hours after posting, Pushshift's from whenever it got there -- not the
  series the crawler records. It stays on the post row with `fetched_at` set to that
  observation's time (`observed_at`); `extra.backfill` names the file it came from.
- **The symbol registry** (`social_scraper_symbols`). It is one file the live export
  rewrites from the database's counts, which would overwrite anything written here.
"""

from __future__ import annotations

import io
import json
import logging
import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import zstandard

from social_scraper.archive import datasets
from social_scraper.archive.manifest import DatasetSpec, ManifestError
from social_scraper.archive.store import ObjectStore
from social_scraper.archive.writer import VerifyError, write_partition
from social_scraper.export import lake_rows
from social_scraper.pipeline import build_row
from social_scraper.reddit.parse import parse_thing
from social_scraper.symbols.extract import Mention, extract_mentions
from social_scraper.symbols.universe import Universe

log = logging.getLogger(__name__)

REPORT_FILE = "reddit-import.json"
DEFAULT_SINCE = datetime(2015, 1, 1, tzinfo=UTC)
SUFFIXES = (".zst", ".jsonl", ".ndjson", ".json")
# Pushshift and Arctic Shift compress with `--long=31`; the decoder must be allowed it.
MAX_WINDOW = 2**31
# The key in a post's `extra` naming the dump it came from.
BACKFILL_KEY = "backfill"
SPOOL_BATCH = 50_000
READ_BATCH = 100_000
READ_BUFFER = 1 << 20  # decompressed bytes per read from a `.zst`
# DuckDB takes 80% of RAM by default; a full backfill's spool got the run killed for low
# memory on a 16 GB desktop (2026-10-05). Capped, it spills to `temp_directory` instead,
# and fewer threads keep fewer per-thread buffers alive.
SPOOL_MEMORY = "1.5GB"
SPOOL_THREADS = 4
LOG_EVERY = 1_000_000

# Why a line was not kept; the per-file counts in the report are keyed by these.
OTHER_SUBREDDIT = "other_subreddit"
OUT_OF_RANGE = "out_of_range"
UNPARSABLE = "unparsable"
IGNORED_AUTHOR = "ignored_author"
NO_SYMBOL = "no_symbol"
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
BY_YEAR = """
WITH p AS (
    SELECT year(created_at) AS y,
           count(DISTINCT external_id) AS posts,
           count(DISTINCT external_id) FILTER (WHERE kind = 'comment') AS comments
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
class DumpFilter:
    """Which archive lines become lake rows."""

    subreddits: frozenset[str]  # lower-case; empty keeps every subreddit
    since: datetime | None = DEFAULT_SINCE
    until: datetime | None = None  # exclusive
    ignore_authors: frozenset[str] = frozenset()  # lower-case
    all_posts: bool = False

    def in_range(self, created: datetime) -> bool:
        if self.since is not None and created < self.since:
            return False
        return self.until is None or created < self.until


@dataclass(frozen=True)
class Converted:
    """One archive line: why it was skipped, or the lake rows it becomes."""

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


def _epoch(value: Any) -> datetime | None:
    """A Unix timestamp as Pushshift writes it -- int, float, or a numeric string."""
    if isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def observed_at(obj: dict[str, Any]) -> datetime | None:
    """When the archive captured the object: what its engagement numbers describe.

    Arctic Shift fetches an item seconds after it is posted (`retrieved_on`), fetches it
    again about 36 hours later, and keeps the second fetch's score while leaving
    `retrieved_on` as it was -- a 694-point post "retrieved" 15 s after posting, in a
    live sample (2026-10-04). Its `_meta.retrieved_2nd_on` is then the score's time.
    """
    meta = obj.get("_meta")
    if isinstance(meta, dict):
        second = _epoch(meta.get("retrieved_2nd_on"))
        if second is not None:
            return second
    for key in ("retrieved_on", "retrieved_utc"):
        stamp = _epoch(obj.get(key))
        if stamp is not None:
            return stamp
    return None


def thing_of(obj: dict[str, Any]) -> dict[str, Any] | None:
    """Wrap a dump object as the listing child `parse_thing` reads.

    A dump file holds one kind, but the object says which: only a submission has a
    `title`, and only a comment has a `body`.
    """
    if "title" in obj:
        return {"kind": "t3", "data": obj}
    if "body" in obj:
        return {"kind": "t1", "data": obj}
    return None


def precheck(obj: Any, flt: DumpFilter) -> str | None:
    """Why `obj` cannot be kept, judged from raw fields before any parsing; else None.

    Most lines of a full monthly dump fail here, so this is the hot path's cheap half.
    """
    if not isinstance(obj, dict):
        return UNPARSABLE
    if flt.subreddits and str(obj.get("subreddit") or "").lower() not in flt.subreddits:
        return OTHER_SUBREDDIT
    created = _epoch(obj.get("created_utc"))
    if created is None:
        return UNPARSABLE
    return None if flt.in_range(created) else OUT_OF_RANGE


def convert(obj: Any, universe: Universe, flt: DumpFilter, source: str) -> Converted:
    """The lake rows for one archive object, cheapest rejections first. Pure."""
    reason = precheck(obj, flt)
    if reason is not None:
        return Converted(reason)
    thing = thing_of(obj)
    post = parse_thing(thing) if thing is not None else None
    if post is None:
        return Converted(UNPARSABLE)
    if post.author.name and post.author.name.lower() in flt.ignore_authors:
        return Converted(IGNORED_AUTHOR)
    if not flt.all_posts and not extract_mentions(post.text, universe, post.tagged_symbols):
        return Converted(NO_SYMBOL)
    row, found = build_row(post, universe)
    seen = observed_at(obj) or post.created_at
    row["first_seen_at"] = row["last_seen_at"] = seen
    row["raw"] = {**(row["raw"] or {}), BACKFILL_KEY: source}
    kept, dropped = lake_rows(datasets.POSTS, [row])
    if dropped:
        return Converted(HAS_CREDENTIAL)
    lake_post = {**kept[0], PART: datasets.POSTS.partition_of(kept[0])}
    return Converted(None, lake_post, mentions_of(row, found))


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


def expand_inputs(paths: Iterable[Path]) -> list[Path]:
    """Each path, with a directory replaced by the dump files directly inside it.

    PowerShell hands a native command its `*.zst` unexpanded, so a directory argument
    is how a Windows user imports a folder of downloads.
    """
    out: list[Path] = []
    for path in paths:
        if path.is_dir():
            out.extend(sorted(p for p in path.iterdir() if p.suffix.lower() in SUFFIXES))
        else:
            out.append(path)
    return out


def read_lines(path: Path) -> Iterator[bytes]:
    """Raw lines of a dump file, decompressing `.zst`.

    Bytes, not text: `line_filter` rejects most lines of a full monthly dump before they
    are worth decoding, and decoding ~400 GB a month to throw it away was most of the cost.
    """
    with path.open("rb") as raw:
        if path.suffix.lower() == ".zst":
            reader = zstandard.ZstdDecompressor(max_window_size=MAX_WINDOW).stream_reader(raw)
            yield from io.BufferedReader(reader, buffer_size=READ_BUFFER)
        else:
            yield from raw


def line_filter(subreddits: frozenset[str]) -> re.Pattern[bytes] | None:
    """A pattern every raw line of a wanted subreddit matches; None when all are wanted.

    Arctic Shift's monthly dumps are all of Reddit, so nearly every line is another
    subreddit's. Searching the raw bytes for `"subreddit":"<name>"` skips those without
    decoding or parsing them. It may pass a line it should not (the full check in
    `precheck` still runs), but never drops one it should keep: names are ASCII
    `[A-Za-z0-9_]`, which JSON never escapes, and case and spacing are allowed for.
    """
    if not subreddits:
        return None
    names = b"|".join(re.escape(s.encode("ascii")) for s in sorted(subreddits))
    return re.compile(rb'"subreddit"\s*:\s*"(?:' + names + rb')"', re.IGNORECASE)


class Spool:
    """Rows on their way to the lake, in a DuckDB file under `directory`.

    On disk rather than in memory: a filtered decade of r/wallstreetbets does not fit,
    and DuckDB sorts out of core.
    """

    def __init__(self, directory: Path) -> None:
        # UTC, so timestamps come back to Arrow in the lake's zone and `year()` is a UTC year.
        config: dict[str, str | bool | int | float | list[str]] = {
            "temp_directory": str(directory / "tmp"),
            "TimeZone": "UTC",
            "memory_limit": SPOOL_MEMORY,
            "threads": SPOOL_THREADS,
        }
        self.con = duckdb.connect(str(directory / "spool.duckdb"), config=config)
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


def spool_file(
    path: Path, spool: Spool, universe: Universe, flt: DumpFilter, log_every: int = LOG_EVERY
) -> FileStats:
    """Convert every line of one dump into the spool. Errors land in the stats."""
    stats = FileStats(str(path))
    posts: list[dict[str, Any]] = []
    mentions: list[dict[str, Any]] = []
    wanted = line_filter(flt.subreddits)
    try:
        for line in read_lines(path):
            stats.lines += 1
            if stats.lines % log_every == 0:
                log.info("%s: %d lines read, %d kept", path.name, stats.lines, stats.kept)
            if wanted is not None and not wanted.search(line):
                stats.skipped[OTHER_SUBREDDIT] = stats.skipped.get(OTHER_SUBREDDIT, 0) + 1
                continue
            try:
                # Undecodable bytes become U+FFFD rather than costing the post.
                obj = json.loads(line.decode("utf-8", errors="replace"))
            except ValueError:
                stats.bad_lines += 1
                continue
            result = convert(obj, universe, flt, path.name)
            if result.post is None:
                reason = result.skip or UNPARSABLE
                stats.skipped[reason] = stats.skipped.get(reason, 0) + 1
                continue
            stats.kept += 1
            stats.mentions += len(result.mentions)
            posts.append(result.post)
            mentions.extend(result.mentions)
            if len(posts) >= SPOOL_BATCH:
                spool.add(posts, mentions)
                posts, mentions = [], []
    except (OSError, zstandard.ZstdError) as exc:
        stats.error = f"{type(exc).__name__}: {exc}"
    # What was read before an error is still good: merging it never removes anything.
    spool.add(posts, mentions)
    return stats


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
                write_partition(store, spec, part, rows, now, keep_existing=True)
            written += 1
        report.partitions[spec.name] = written


def import_dumps(
    paths: Sequence[Path],
    universe: Universe,
    flt: DumpFilter,
    stores: Sequence[ObjectStore] | None,
    spool_dir: Path,
    now: datetime,
) -> ImportReport:
    """Spool every dump in `paths`, then write the lake partitions into `stores`.

    `stores=None` is a dry run: everything is read and counted, nothing is written.
    """
    report = ImportReport(started_at=now.isoformat(), dry_run=stores is None)
    with Spool(spool_dir) as spool:
        for path in paths:
            stats = spool_file(path, spool, universe, flt)
            report.files.append(stats)
            if stats.error:
                report.errors.append(f"{path}: {stats.error}")
            log.info("%s: %d of %d lines kept", path.name, stats.kept, stats.lines)
        report.posts, report.mentions, report.by_year = spool.summary()
        if stores is not None:
            try:
                write_spool(spool, stores, now, report)
            # StoreError is an OSError: an unreachable bucket fails like a full disk.
            except (OSError, VerifyError, ManifestError, pa.ArrowException) as exc:
                report.errors.append(f"lake write: {type(exc).__name__}: {exc}")
    report.finished_at = datetime.now(UTC).isoformat()
    return report


def write_report(report: ImportReport, logs_dir: Path) -> Path:
    """Overwrite `logs/reddit-import.json` -- on failure as on success."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    path = logs_dir / REPORT_FILE
    body = {**asdict(report), "ok": report.ok}
    path.write_text(json.dumps(body, indent=2, default=str), encoding="utf-8")
    return path
