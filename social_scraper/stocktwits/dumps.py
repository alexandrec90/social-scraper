"""Backfill StockTwits history from the NYU dataset, 2008 to 2022.

*Comprehensive records of a financial social media platform* (NYU; the public bucket
`s3://stocktwits-nyu`, layout checked 2026-10-08) is every StockTwits message from May
2008 into 2022, split into CSV tables keyed on `message_id`. Two are read; the other
four are subsets of these:

| folder                 | columns read                                                |
| ---------------------- | ----------------------------------------------------------- |
| `feature_wo_messages/` | message_id, user_id, created_at, sentiment, parent_message_id, in_reply_to_message_id, symbol_list |
| `messages/`            | message_id, message_body                                    |

**Joined in DuckDB, not walked side by side.** The two tables are cut into different
files (248 and 205), and neither is strictly in id order (measured 2026-10-08: a few
hundred ids per file out of place), so a message cannot be paired with its metadata by
reading both in step. The metadata is loaded once into a DuckDB table sorted by id, and
each messages file is joined against the id range it covers -- a narrow read of that
table, through its zonemaps.

Each message becomes a `Post`: kind `reply` when it answers another message, else
`message`; `parent_id` the message answered, `raw.conversation_id` the thread's first
message; the author hashed from the numeric user id, as X authors are; the body
HTML-unescaped (the dataset keeps `&#39;`). The symbols StockTwits attached
(`symbol_list`) are `tagged_symbols`, and the author's own Bullish/Bearish label lands
in `extra.sentiment`. The dataset recorded no engagement.
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from social_scraper import backfill
from social_scraper.archive.store import ObjectStore
from social_scraper.backfill import (
    ARCHIVE_CHANNEL,
    LOG_EVERY,
    OUT_OF_RANGE,
    READ_BATCH,
    UNPARSABLE,
    ArchiveFilter,
    Converted,
    FileStats,
    ImportReport,
    Spool,
    lake_post,
)
from social_scraper.models import STOCKTWITS, AuthorInfo, Post
from social_scraper.symbols.universe import Universe

log = logging.getLogger(__name__)

REPORT_FILE = "stocktwits-import.json"
FEATURES = "feature_wo_messages"
MESSAGES = "messages"
URL = "https://stocktwits.com/message/{}"
NO_METADATA = "no_metadata"  # skip reason: a message with no feature_wo_messages row
SENTIMENTS = frozenset({"bullish", "bearish"})
_SYMBOL_RE = re.compile(r"'([^']+)'")  # symbol_list is a Python list literal: ['TSLA', 'F']

# Every column as text, so one malformed value becomes a NULL rather than failing a file.
# The reply ids are written as floats (`299412911.0`), a pandas NaN artefact.
LOAD_FEATURES = """
CREATE OR REPLACE TABLE features AS
SELECT TRY_CAST(message_id AS BIGINT) AS message_id,
       user_id,
       TRY_CAST(created_at AS TIMESTAMPTZ) AS created_at,
       sentiment,
       TRY_CAST(TRY_CAST(parent_message_id AS DOUBLE) AS BIGINT) AS conversation_id,
       TRY_CAST(TRY_CAST(in_reply_to_message_id AS DOUBLE) AS BIGINT) AS reply_to,
       symbol_list
FROM read_csv(?, header = true, all_varchar = true, delim = ',', quote = '"', escape = '"')
WHERE TRY_CAST(message_id AS BIGINT) IS NOT NULL
ORDER BY message_id
"""
LOAD_MESSAGES = """
CREATE OR REPLACE TEMP TABLE m AS
SELECT TRY_CAST(message_id AS BIGINT) AS id, message_body AS body
FROM read_csv(?, header = true, all_varchar = true, delim = ',', quote = '"', escape = '"')
"""
# The range bound is what makes this a narrow read of the sorted `features` table.
JOIN_MESSAGES = """
SELECT m.id, m.body, f.message_id IS NOT NULL AS has_meta, f.user_id, f.created_at,
       f.sentiment, f.conversation_id, f.reply_to, f.symbol_list
FROM m LEFT JOIN (SELECT * FROM features WHERE message_id BETWEEN ? AND ?) f
    ON f.message_id = m.id
"""


def load_features(con: duckdb.DuckDBPyConnection, files: Sequence[Path]) -> int:
    """Every `feature_wo_messages` row into the table `features`, sorted by id; its size."""
    con.execute(LOAD_FEATURES, [[str(p) for p in files]])
    count = con.execute("SELECT count(*) FROM features").fetchone()
    return count[0] if count else 0


def joined(con: duckdb.DuckDBPyConnection, path: Path) -> Iterator[dict[str, Any]]:
    """Each message of one `messages` file beside its metadata (`has_meta` false: none)."""
    con.execute(LOAD_MESSAGES, [str(path)])
    # fetchall, not fetchone: duckdb 2.0 streams a result, and one left open blocks `con.sql`.
    (bounds,) = con.execute("SELECT min(id), max(id) FROM m").fetchall()
    lo, hi = (bounds[0], bounds[1]) if bounds[0] is not None else (0, -1)
    # A relation, not `con.execute()`: duckdb 2.0 copies a DB-API result to export Arrow.
    reader = con.sql(JOIN_MESSAGES, params=[lo, hi]).to_arrow_reader(READ_BATCH)
    for batch in reader:
        yield from batch.to_pylist()


def message_post(row: dict[str, Any]) -> Post | str:
    """One joined message as a `Post`, or the reason it is not one."""
    message_id, body = row.get("id"), row.get("body") or ""
    if message_id is None or not body.strip():
        return UNPARSABLE
    if not row.get("has_meta"):
        return NO_METADATA
    created = row.get("created_at")
    if created is None:
        return UNPARSABLE
    reply_to, conversation = row.get("reply_to"), row.get("conversation_id")
    return Post(
        platform=STOCKTWITS,
        external_id=str(message_id),
        kind="message" if reply_to is None else "reply",
        channel=ARCHIVE_CHANNEL,
        created_at=created.astimezone(UTC),
        # The numeric id only, hashed: the dataset carries no username.
        author=AuthorInfo(name=None, user_id=row.get("user_id") or None),
        body=html.unescape(body),
        url=URL.format(message_id),
        parent_id=None if reply_to is None else str(reply_to),
        tagged_symbols=tuple(_SYMBOL_RE.findall(row.get("symbol_list") or "")),
        raw={} if conversation is None else {"conversation_id": str(conversation)},
    )


def convert_message(
    row: dict[str, Any], universe: Universe, flt: ArchiveFilter, source: str
) -> Converted:
    """The lake rows for one joined message, cheapest rejections first. Pure."""
    post = message_post(row)
    if isinstance(post, str):
        return Converted(post)
    if not flt.in_range(post.created_at):
        return Converted(OUT_OF_RANGE)
    sentiment = (row.get("sentiment") or "").strip().lower()
    extra = {"sentiment": sentiment} if sentiment in SENTIMENTS else None
    return lake_post(post, universe, source, extra=extra, all_posts=flt.all_posts)


def spool_messages(
    path: Path,
    spool: Spool,
    con: duckdb.DuckDBPyConnection,
    universe: Universe,
    flt: ArchiveFilter,
    log_every: int = LOG_EVERY,
) -> FileStats:
    """Convert every message of one `messages` file into the spool. Errors land in the stats."""
    stats = FileStats(str(path))
    try:
        for row in joined(con, path):
            stats.lines += 1
            if stats.lines % log_every == 0:
                log.info("%s: %d messages read, %d kept", path.name, stats.lines, stats.kept)
            spool.take(convert_message(row, universe, flt, path.name), stats)
    except (OSError, duckdb.Error) as exc:
        stats.error = f"{type(exc).__name__}: {exc}"
    spool.flush()
    return stats


def import_folder(
    folders: Sequence[Path],
    universe: Universe,
    flt: ArchiveFilter,
    stores: Sequence[ObjectStore] | None,
    spool_dir: Path,
    now: datetime,
) -> ImportReport:
    """Import each dataset folder (`messages/*.csv` beside `feature_wo_messages/*.csv`).

    `stores=None` is a dry run: everything is read and counted, nothing is written.
    """
    features: list[Path] = []
    messages: list[Path] = []
    missing: list[str] = []
    for folder in folders:
        found = sorted((folder / FEATURES).glob("*.csv")), sorted((folder / MESSAGES).glob("*.csv"))
        if not all(found):
            missing.append(f"{folder}: no {FEATURES}/*.csv or no {MESSAGES}/*.csv")
            continue
        features += found[0]
        messages += found[1]
    con = backfill.connect(spool_dir / "stocktwits-features.duckdb", spool_dir / "features-tmp")
    try:
        if features:
            log.info("loading %d metadata files into DuckDB", len(features))
            log.info("%d messages' metadata loaded", load_features(con, features))

        def one(path: Path, spool: Spool) -> FileStats:
            return spool_messages(path, spool, con, universe, flt)

        report = backfill.run_import(messages, one, stores, spool_dir, now)
    except (OSError, duckdb.Error) as exc:  # the metadata load; files fail inside run_import
        report = ImportReport(
            started_at=now.isoformat(),
            finished_at=datetime.now(UTC).isoformat(),
            dry_run=stores is None,
            errors=[f"{FEATURES}: {type(exc).__name__}: {exc}"],
        )
    finally:
        con.close()
    report.errors[:0] = missing
    return report


def write_report(report: ImportReport, logs_dir: Path) -> Path:
    """Overwrite `logs/stocktwits-import.json` -- on failure as on success."""
    return backfill.write_report(report, logs_dir, REPORT_FILE)
