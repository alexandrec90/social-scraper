"""Backfill X history from tweet datasets published as CSV.

X has had no free bulk archive since academic access ended in 2023, and datasets that
ship only tweet ids can no longer be turned back into text. The ones that shipped the
text are the only way to get X history. First supported: the Cashtag Piggybacking
dataset (Cresci et al., CNR; https://zenodo.org/record/2686862, CC BY 4.0, research use
only), about 9M cashtag tweets from May to September 2017:

| file              | columns read                                                        |
| ----------------- | ------------------------------------------------------------------- |
| `tweets.csv[.zip]`| id, text, user_id, in_reply_to_status_id, retweeted_status_id, lang, created_at |
| `users.csv[.zip]` | id, followers_count, verified, bot                                  |

Each tweet becomes a `Post` shaped like the crawler's (`x/parse.py`): the tweet id, a
handle-free permalink, an author hashed from the numeric user id -- so it joins with
live rows of the same account -- and `kind` `reply` or `tweet`. A retweet is skipped,
as the crawler skips retweet wrappers: it repeats someone else's text. The authors'
bot label from `users.csv` lands in `extra.bot` (1 bot, 0 human, absent unlabelled).
The dataset was collected from the live stream, so `fetched_at` is the tweet's time.
"""

from __future__ import annotations

import csv
import io
import logging
import sys
import zipfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

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
from social_scraper.models import X, AuthorInfo, Post
from social_scraper.symbols.universe import Universe
from social_scraper.x.parse import X_BASE, parse_time

log = logging.getLogger(__name__)

REPORT_FILE = "x-import.json"
# How a tweet reached the lake, beside the crawler's `search` and `timeline`.
CHANNEL = "archive"
TWEETS = "tweets.csv"
USERS = "users.csv"
RETWEET = "retweet"  # skip reason
NO_ID = frozenset({"", "0"})  # what the dataset writes for "not a reply/retweet"


@dataclass(frozen=True)
class TweetFilter:
    since: datetime | None = None
    until: datetime | None = None  # exclusive
    all_posts: bool = False

    def in_range(self, created: datetime) -> bool:
        if self.since is not None and created < self.since:
            return False
        return self.until is None or created < self.until


@dataclass(frozen=True)
class Author:
    followers: int | None = None
    verified: bool | None = None
    bot: int | None = None


def find_file(folder: Path, name: str) -> Path | None:
    """`<name>` or `<name>.zip` directly in `folder`, the plain file first."""
    for candidate in (folder / name, folder / f"{name}.zip"):
        if candidate.is_file():
            return candidate
    return None


@contextmanager
def open_csv(path: Path, member: str) -> Iterator[Iterator[dict[str, str]]]:
    """Rows of a CSV, or of the member named `member` inside a zip (macOS junk skipped)."""
    # Tweets carry multi-kilobyte fields; the default 128 KiB limit is for one field.
    csv.field_size_limit(min(sys.maxsize, 2**31 - 1))
    if path.suffix.lower() != ".zip":
        with path.open(encoding="utf-8", errors="replace", newline="") as fh:
            yield csv.DictReader(fh)
        return
    with zipfile.ZipFile(path) as zf:
        names = [n for n in zf.namelist() if Path(n).name == member and "__MACOSX" not in n]
        if not names:
            raise FileNotFoundError(f"{path}: no {member} inside")
        with zf.open(names[0]) as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="")
            yield csv.DictReader(text)


def _int(value: str | None) -> int | None:
    return int(value) if value and value.isdigit() else None


def load_authors(path: Path) -> dict[str, Author]:
    """`users.csv` as `{user_id: Author}` -- about 500k rows, small enough to hold."""
    authors = {}
    with open_csv(path, USERS) as rows:
        for row in rows:
            verified = row.get("verified")
            authors[row.get("id") or ""] = Author(
                followers=_int(row.get("followers_count")),
                verified=None if verified is None else verified == "1",
                bot=_int(row.get("bot")),
            )
    return authors


def tweet_post(row: dict[str, str], author: Author | None) -> Post | str:
    """One `tweets.csv` row as a `Post`, or the reason it is not one."""
    if (row.get("retweeted_status_id") or "") not in NO_ID:
        return RETWEET
    tweet_id, text = row.get("id") or "", row.get("text") or ""
    created = parse_time(row.get("created_at"))
    if not tweet_id.isdigit() or not text.strip() or created is None:
        return UNPARSABLE
    reply_to = row.get("in_reply_to_status_id") or ""
    author = author or Author()
    return Post(
        platform=X,
        external_id=tweet_id,
        kind="tweet" if reply_to in NO_ID else "reply",
        channel=CHANNEL,
        created_at=created,
        # The numeric id only: hashed like the crawler's, and no handle ever stored.
        author=AuthorInfo(
            name=None,
            user_id=row.get("user_id") or None,
            followers=author.followers,
            verified=author.verified,
        ),
        body=text,
        url=f"{X_BASE}/i/status/{tweet_id}",
        parent_id=None if reply_to in NO_ID else reply_to,
        lang=row.get("lang") or None,
    )


def convert_tweet(
    row: dict[str, str],
    authors: dict[str, Author],
    universe: Universe,
    flt: TweetFilter,
    source: str,
) -> Converted:
    """The lake rows for one tweet row, cheapest rejections first. Pure."""
    author = authors.get(row.get("user_id") or "")
    post = tweet_post(row, author)
    if isinstance(post, str):
        return Converted(post)
    if not flt.in_range(post.created_at):
        return Converted(OUT_OF_RANGE)
    extra = {"bot": author.bot} if author is not None and author.bot is not None else None
    return lake_post(post, universe, source, extra=extra, all_posts=flt.all_posts)


def spool_tweets(
    path: Path,
    spool: Spool,
    universe: Universe,
    flt: TweetFilter,
    authors: dict[str, Author],
    log_every: int = LOG_EVERY,
) -> FileStats:
    """Convert every row of a `tweets.csv` into the spool. Errors land in the stats."""
    stats = FileStats(str(path))
    try:
        with open_csv(path, TWEETS) as rows:
            for row in rows:
                stats.lines += 1
                if stats.lines % log_every == 0:
                    log.info("%s: %d rows read, %d kept", path.name, stats.lines, stats.kept)
                spool.take(convert_tweet(row, authors, universe, flt, path.name), stats)
    except (OSError, zipfile.BadZipFile, csv.Error) as exc:
        stats.error = f"{type(exc).__name__}: {exc}"
    spool.flush()
    return stats


def import_folder(
    folders: Sequence[Path],
    universe: Universe,
    flt: TweetFilter,
    stores: Sequence[ObjectStore] | None,
    spool_dir: Path,
    now: datetime,
) -> ImportReport:
    """Import each dataset folder (`tweets.csv[.zip]`, optional `users.csv[.zip]`).

    `stores=None` is a dry run: everything is read and counted, nothing is written.
    """
    inputs: list[Path] = []
    authors_by_tweets: dict[Path, dict[str, Author]] = {}
    missing: list[str] = []
    for folder in folders:
        tweets = find_file(folder, TWEETS)
        if tweets is None:
            missing.append(f"{folder}: no {TWEETS} or {TWEETS}.zip")
            continue
        users = find_file(folder, USERS)
        authors_by_tweets[tweets] = load_authors(users) if users is not None else {}
        inputs.append(tweets)

    def one(path: Path, spool: Spool) -> FileStats:
        return spool_tweets(path, spool, universe, flt, authors_by_tweets[path])

    report = backfill.run_import(inputs, one, stores, spool_dir, now)
    report.errors[:0] = missing
    return report


def write_report(report: ImportReport, logs_dir: Path) -> Path:
    """Overwrite `logs/x-import.json` -- on failure as on success."""
    return backfill.write_report(report, logs_dir, REPORT_FILE)
