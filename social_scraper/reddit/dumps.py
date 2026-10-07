"""Backfill Reddit history from Pushshift / Arctic Shift dump files into the lake.

The live crawler only reaches what a listing still shows, so the lake starts the day the
scraper did. A sentiment feature needs years. The community archives publish that
history as newline-delimited JSON, one Reddit API object per line, zstd-compressed:

- Watchful1's per-subreddit torrents on Academic Torrents (`<sub>_submissions.zst`,
  `<sub>_comments.zst`), built from Pushshift;
- Arctic Shift's monthly dumps of all of Reddit (`RC_<month>.zst`, `RS_<month>.zst`)
  and its per-subreddit download tool (`.zst` or `.jsonl`).

Each line is the `data` object a listing child carries, so it goes through the crawler's
own `parse_thing`, then `backfill.lake_post` -- which is where the spool, the lake write
and the never-over-a-live-row rule live, shared with every other archive importer.

**Only what a sentiment feature can use, by default:** posts in `REDDIT_SUBREDDITS`,
created since 2015 (ibkr_trader's default training start), that mention at least one
symbol -- a cashtag counts even outside today's symbol directory, which is how a ticker
delisted since keeps its history. `all_posts` keeps the rest.

`fetched_at` is when the archive captured the post (`observed_at`): Arctic Shift's
second fetch, about 36 hours after posting, else Pushshift's `retrieved_on`.
"""

from __future__ import annotations

import io
import json
import logging
import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import zstandard

from social_scraper import backfill
from social_scraper.archive.store import ObjectStore
from social_scraper.backfill import (
    LOG_EVERY,
    OUT_OF_RANGE,
    UNPARSABLE,
    Converted,
    FileStats,
    ImportReport,
    Spool,
    lake_post,
)
from social_scraper.reddit.parse import parse_thing
from social_scraper.symbols.universe import Universe

log = logging.getLogger(__name__)

REPORT_FILE = "reddit-import.json"
DEFAULT_SINCE = datetime(2015, 1, 1, tzinfo=UTC)
SUFFIXES = (".zst", ".jsonl", ".ndjson", ".json")
# Pushshift and Arctic Shift compress with `--long=31`; the decoder must be allowed it.
MAX_WINDOW = 2**31
READ_BUFFER = 1 << 20  # decompressed bytes per read from a `.zst`

# Why a line was not kept, beyond the reasons every importer shares (`backfill`).
OTHER_SUBREDDIT = "other_subreddit"
IGNORED_AUTHOR = "ignored_author"


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
    return lake_post(post, universe, source, observed_at(obj), all_posts=flt.all_posts)


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


def spool_file(
    path: Path, spool: Spool, universe: Universe, flt: DumpFilter, log_every: int = LOG_EVERY
) -> FileStats:
    """Convert every line of one dump into the spool. Errors land in the stats."""
    stats = FileStats(str(path))
    wanted = line_filter(flt.subreddits)
    try:
        for line in read_lines(path):
            stats.lines += 1
            if stats.lines % log_every == 0:
                log.info("%s: %d lines read, %d kept", path.name, stats.lines, stats.kept)
            if wanted is not None and not wanted.search(line):
                stats.skip(OTHER_SUBREDDIT)
                continue
            try:
                # Undecodable bytes become U+FFFD rather than costing the post.
                obj = json.loads(line.decode("utf-8", errors="replace"))
            except ValueError:
                stats.bad_lines += 1
                continue
            spool.take(convert(obj, universe, flt, path.name), stats)
    except (OSError, zstandard.ZstdError) as exc:
        stats.error = f"{type(exc).__name__}: {exc}"
    spool.flush()  # what was read before an error is still good: merging never removes
    return stats


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

    def one(path: Path, spool: Spool) -> FileStats:
        return spool_file(path, spool, universe, flt)

    return backfill.run_import(paths, one, stores, spool_dir, now)


def write_report(report: ImportReport, logs_dir: Path) -> Path:
    """Overwrite `logs/reddit-import.json` -- on failure as on success."""
    return backfill.write_report(report, logs_dir, REPORT_FILE)
