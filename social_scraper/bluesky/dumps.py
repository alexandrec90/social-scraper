"""Backfill Bluesky history from Zenodo record 14669616 (posts from 2023 to March 2024).

The record's `user_posts.tar.gz` (checked 2026-10-08) holds one `user_posts/<user>.jsonl`
per account crawled, one post per line:

| field                                 | read as                                        |
| ------------------------------------- | ---------------------------------------------- |
| post_id, user_id                      | the dataset's own integer ids (see below)      |
| date                                  | `YYYYMMDDHHMM`, read as UTC                    |
| text, langs                           | body; the first language (ISO 639-3, `eng`)    |
| like_count, reply_count, repost_count | engagement as of the crawl                     |
| reply_to, thread_root                 | parent and conversation, as dataset ids        |
| repost_from                           | set on a repost, whose text is someone else's  |

**Pseudonymised ids.** The dataset replaced every DID and post URI with its own integer,
so a row cannot link back to bsky.app and its author cannot be joined with any other
source. The author hash is taken over a namespaced id (`zenodo-14669616:<user_id>`), so
it can never equal a hash a future live Bluesky crawl derives from a real DID, and
`url` stays empty.

A repost is skipped, as the X importers skip retweets. Bluesky is a general-topic
platform, so only posts with an explicit, listed `$TICKER` are kept
(`lake_post_strict`); a line holding no `$` at all -- nearly all of them -- is rejected
before it is parsed.
"""

from __future__ import annotations

import json
import logging
import tarfile
import zlib
from collections.abc import Iterable, Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from social_scraper import backfill
from social_scraper.archive.store import ObjectStore
from social_scraper.backfill import (
    ARCHIVE_CHANNEL,
    LOG_EVERY,
    NO_CASHTAG,
    OUT_OF_RANGE,
    UNPARSABLE,
    ArchiveFilter,
    Converted,
    FileStats,
    ImportReport,
    Spool,
    lake_post_strict,
)
from social_scraper.models import BLUESKY, AuthorInfo, Engagement, Post
from social_scraper.symbols.universe import Universe

log = logging.getLogger(__name__)

REPORT_FILE = "bluesky-import.json"
AUTHOR_NAMESPACE = "zenodo-14669616"
REPOST = "repost"  # skip reason
JSONL = ".jsonl"
ARCHIVES = (".tar.gz", ".tgz", ".tar")


def is_input(path: Path) -> bool:
    """Whether a file in an input folder is one this importer reads."""
    return path.name.lower().endswith((*ARCHIVES, JSONL))


def expand_inputs(paths: Iterable[Path]) -> list[Path]:
    """Each path, with a directory replaced by the archives and `.jsonl` files directly in it."""
    out: list[Path] = []
    for path in paths:
        if path.is_dir():
            out.extend(sorted(p for p in path.iterdir() if p.is_file() and is_input(p)))
        else:
            out.append(path)
    return out


def read_lines(path: Path) -> Iterator[bytes]:
    """Raw lines of a `.jsonl` file, or of every `.jsonl` member of a tarball, streamed."""
    if path.name.lower().endswith(JSONL):
        with path.open("rb") as fh:
            yield from fh
        return
    # Streamed ("r|*"): the archive is read once, front to back, and nothing is extracted.
    with tarfile.open(path, "r|*") as tf:
        for member in tf:
            if member.isfile() and member.name.lower().endswith(JSONL):
                member_fh = tf.extractfile(member)
                if member_fh is not None:
                    yield from member_fh


def parse_date(value: Any) -> datetime | None:
    """`202403101740` (int or str) as an aware UTC datetime."""
    try:
        return datetime.strptime(str(value), "%Y%m%d%H%M").replace(tzinfo=UTC)
    except ValueError:
        return None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def bluesky_post(record: dict[str, Any]) -> Post | str:
    """One dataset record as a `Post`, or the reason it is not one."""
    if record.get("repost_from") is not None:
        return REPOST
    post_id, text = _int(record.get("post_id")), record.get("text")
    created = parse_date(record.get("date"))
    if post_id is None or not isinstance(text, str) or not text.strip() or created is None:
        return UNPARSABLE
    user, reply_to, root = record.get("user_id"), record.get("reply_to"), record.get("thread_root")
    langs = record.get("langs")
    lang = langs[0] if isinstance(langs, list) and langs and isinstance(langs[0], str) else None
    likes = _int(record.get("like_count"))
    replies = _int(record.get("reply_count"))
    return Post(
        platform=BLUESKY,
        external_id=str(post_id),
        kind="post" if reply_to is None else "reply",
        channel=ARCHIVE_CHANNEL,
        created_at=created,
        author=AuthorInfo(
            name=None, user_id=None if user is None else f"{AUTHOR_NAMESPACE}:{user}"
        ),
        body=text,
        parent_id=None if reply_to is None else str(reply_to),
        lang=lang,
        # Mirrored like X's: likes into `score`, replies into `comments`.
        engagement=Engagement(
            score=likes,
            comments=replies,
            likes=likes,
            reposts=_int(record.get("repost_count")),
        ),
        raw={} if root is None else {"conversation_id": str(root)},
    )


def convert_line(line: bytes, universe: Universe, flt: ArchiveFilter, source: str) -> Converted:
    """The lake rows for one dataset line, cheapest rejections first. Pure."""
    if not flt.all_posts and b"$" not in line:
        return Converted(NO_CASHTAG)
    try:
        record = json.loads(line)
    except ValueError:
        return Converted(UNPARSABLE)
    if not isinstance(record, dict):
        return Converted(UNPARSABLE)
    post = bluesky_post(record)
    if isinstance(post, str):
        return Converted(post)
    if not flt.in_range(post.created_at):
        return Converted(OUT_OF_RANGE)
    return lake_post_strict(post, universe, source, None, flt.all_posts)


def spool_posts(
    path: Path,
    spool: Spool,
    universe: Universe,
    flt: ArchiveFilter,
    log_every: int = LOG_EVERY * 10,
) -> FileStats:
    """Convert every line of one input into the spool. Errors land in the stats."""
    stats = FileStats(str(path))
    try:
        for line in read_lines(path):
            stats.lines += 1
            if stats.lines % log_every == 0:
                log.info("%s: %d posts read, %d kept", path.name, stats.lines, stats.kept)
            spool.take(convert_line(line, universe, flt, path.name), stats)
    # A truncated download ends in EOFError (gzip) or zlib.error, not OSError.
    except (OSError, EOFError, tarfile.TarError, zlib.error) as exc:
        stats.error = f"{type(exc).__name__}: {exc}"
    spool.flush()
    return stats


def import_files(
    paths: Sequence[Path],
    universe: Universe,
    flt: ArchiveFilter,
    stores: Sequence[ObjectStore] | None,
    spool_dir: Path,
    now: datetime,
) -> ImportReport:
    """Import each input. `stores=None` is a dry run: read and counted, nothing written."""

    def one(path: Path, spool: Spool) -> FileStats:
        return spool_posts(path, spool, universe, flt)

    files = expand_inputs(paths)
    report = backfill.run_import(files, one, stores, spool_dir, now)
    if not files:  # a folder with nothing to read is a wrong path, not an empty success
        report.errors.append(f"no {', '.join(ARCHIVES)} or {JSONL} file in {list(map(str, paths))}")
    return report


def write_report(report: ImportReport, logs_dir: Path) -> Path:
    """Overwrite `logs/bluesky-import.json` -- on failure as on success."""
    return backfill.write_report(report, logs_dir, REPORT_FILE)
