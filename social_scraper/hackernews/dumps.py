"""Backfill Hacker News from the `open-index/hacker-news` Parquet archive.

The Hugging Face dataset `open-index/hacker-news` (ODC-BY, checked 2026-10-08) is every
Hacker News item since 2006, one Parquet file per month (`data/<year>/<year>-<mm>.parquet`):

| column             | read as                                                     |
| ------------------ | ----------------------------------------------------------- |
| id, parent         | external_id, parent_id (`0` is none)                        |
| type               | kind: 1 story, 2 comment, 3 poll, 5 job; 4 (poll option) skipped |
| by, time           | author (hashed from the username, as Reddit's are), created_at |
| title, text        | title, body (HTML; see below)                               |
| url                | `extra.link`: the page a story points at                    |
| score, descendants | points and comment count -- stories, polls and jobs only    |
| deleted, dead      | skipped: removed by the author or by the moderators         |

Hacker News never publishes a comment's points, and the dataset writes 0 for them, so a
comment's engagement is left empty rather than stored as a real zero. The body is
HTML: a `<p>` becomes a blank line, other tags are dropped, entities are unescaped, and
code blocks (`<pre>`) are removed entirely -- shell snippets are full of `$VARIABLES`,
and nothing in them is about a stock.

Hacker News is a general-topic forum, so only items with an explicit, listed `$TICKER`
are kept (`lake_post_strict`). Items whose title and text hold no `$` at all
are dropped in Arrow, before any is turned into a Python object.
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from social_scraper import backfill
from social_scraper.archive.store import ObjectStore
from social_scraper.backfill import (
    ARCHIVE_CHANNEL,
    NO_CASHTAG,
    OUT_OF_RANGE,
    READ_BATCH,
    UNPARSABLE,
    ArchiveFilter,
    Converted,
    FileStats,
    ImportReport,
    Spool,
    lake_post_strict,
)
from social_scraper.models import HACKERNEWS, AuthorInfo, Engagement, Post
from social_scraper.symbols.universe import Universe

log = logging.getLogger(__name__)

REPORT_FILE = "hn-import.json"
ITEM_URL = "https://news.ycombinator.com/item?id={}"
KINDS: dict[Any, str] = {1: "story", 2: "comment", 3: "poll", 5: "job"}
POLL_OPTION_TYPE = 4
REMOVED = "removed"  # skip reason: deleted or dead
POLL_OPTION = "poll_option"  # skip reason: a poll's answer, not something anyone posted
COLUMNS = [
    "id",
    "type",
    "by",
    "time",
    "title",
    "text",
    "url",
    "parent",
    "score",
    "descendants",
    "deleted",
    "dead",
]
_CODE_RE = re.compile(r"<pre>.*?</pre>", re.DOTALL | re.IGNORECASE)
_PARAGRAPH_RE = re.compile(r"<p>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


def expand_inputs(paths: Iterable[Path]) -> list[Path]:
    """Each path, with a directory replaced by every `.parquet` file under it."""
    out: list[Path] = []
    for path in paths:
        out.extend(sorted(path.rglob("*.parquet")) if path.is_dir() else [path])
    return out


def plain_text(markup: str) -> str:
    """Hacker News's HTML as text, code blocks removed."""
    text = _PARAGRAPH_RE.sub("\n\n", _CODE_RE.sub("", markup))
    return html.unescape(_TAG_RE.sub("", text)).strip()


def item_post(item: dict[str, Any]) -> Post | str:
    """One archive item as a `Post`, or the reason it is not one."""
    if item.get("deleted") or item.get("dead"):
        return REMOVED
    if item.get("type") == POLL_OPTION_TYPE:
        return POLL_OPTION
    kind, item_id, created = KINDS.get(item.get("type")), item.get("id"), item.get("time")
    title = item.get("title") or None
    body = plain_text(item.get("text") or "") or None
    if kind is None or item_id is None or created is None or not (title or body):
        return UNPARSABLE
    parent = item.get("parent")
    engagement = (
        Engagement()
        if kind == "comment"
        else Engagement(score=item.get("score"), comments=item.get("descendants"))
    )
    return Post(
        platform=HACKERNEWS,
        external_id=str(item_id),
        kind=kind,
        channel=ARCHIVE_CHANNEL,
        created_at=created.astimezone(UTC),
        author=AuthorInfo(name=item.get("by") or None),
        title=title,
        body=body,
        url=ITEM_URL.format(item_id),
        parent_id=str(parent) if parent else None,
        engagement=engagement,
    )


def convert_item(
    item: dict[str, Any], universe: Universe, flt: ArchiveFilter, source: str
) -> Converted:
    """The lake rows for one item, cheapest rejections first. Pure."""
    post = item_post(item)
    if isinstance(post, str):
        return Converted(post)
    if not flt.in_range(post.created_at):
        return Converted(OUT_OF_RANGE)
    link = item.get("url") or None
    extra = {"link": link} if link else None
    return lake_post_strict(post, universe, source, extra, flt.all_posts)


def with_a_dollar(batch: pa.RecordBatch) -> pa.RecordBatch:
    """The rows whose title or text holds a `$` -- the only ones that can carry a cashtag."""
    mask = pc.or_kleene(
        pc.match_substring(batch.column("title"), "$"),
        pc.match_substring(batch.column("text"), "$"),
    )
    return batch.filter(mask)


def spool_items(path: Path, spool: Spool, universe: Universe, flt: ArchiveFilter) -> FileStats:
    """Convert every item of one Parquet file into the spool. Errors land in the stats."""
    stats = FileStats(str(path))
    try:
        for batch in pq.ParquetFile(path).iter_batches(batch_size=READ_BATCH, columns=COLUMNS):
            stats.lines += batch.num_rows
            if not flt.all_posts:
                kept = with_a_dollar(batch)
                stats.skip(NO_CASHTAG, batch.num_rows - kept.num_rows)
                batch = kept
            for item in batch.to_pylist():
                spool.take(convert_item(item, universe, flt, path.name), stats)
    except (OSError, pa.ArrowException) as exc:
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
    """Import each Parquet file. `stores=None` is a dry run: read and counted, not written."""

    def one(path: Path, spool: Spool) -> FileStats:
        return spool_items(path, spool, universe, flt)

    files = expand_inputs(paths)
    report = backfill.run_import(files, one, stores, spool_dir, now)
    if not files:  # a folder with nothing to read is a wrong path, not an empty success
        report.errors.append(f"no .parquet file in {list(map(str, paths))}")
    return report


def write_report(report: ImportReport, logs_dir: Path) -> Path:
    """Overwrite `logs/hn-import.json` -- on failure as on success."""
    return backfill.write_report(report, logs_dir, REPORT_FILE)
