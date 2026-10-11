"""Shared fixtures: an in-memory database, a small symbol universe, fakes, payloads.

Payload builders mirror the shapes captured live on 2026-09-28 (Reddit listing JSON,
X classic GraphQL and the 2026 web app's server-streamed tweets) with invented
content -- no real usernames or posts are committed.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic_settings import SettingsConfigDict
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from social_scraper.config import Settings
from social_scraper.db import schema
from social_scraper.browser import SSR_PAYLOAD_SUFFIX
from social_scraper.fetch import PageResult
from social_scraper.symbols.universe import Security, Universe

NOW = datetime(2026, 9, 28, 15, 0, tzinfo=UTC)


class IsolatedSettings(Settings):
    """`Settings` that never reads the developer's `.env`.

    One crawl worker: the in-memory `engine` is a single SQLite connection every thread
    would share, which SQLite refuses. A test of the parallel cycle opts in, on
    `file_engine`.
    """

    model_config = SettingsConfigDict(env_file=None, extra="ignore")
    crawl_workers: int = 1


class Clock:
    """A settable clock: `clock()` returns `clock.now`."""

    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


def sqlite_engine() -> Engine:
    """A fresh in-memory SQLite database with every table, shared across connections."""
    eng = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    schema.create_all(eng)
    return eng


@pytest.fixture
def engine() -> Engine:
    return sqlite_engine()


@pytest.fixture
def file_engine(tmp_path) -> Iterator[Engine]:
    """A SQLite file with a connection per thread: what a parallel cycle can share."""
    eng = create_engine(
        f"sqlite:///{tmp_path / 'scraper.sqlite'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    schema.create_all(eng)
    yield eng
    eng.dispose()


# Postgres joins the matrix whenever DATABASE_URL points at one -- the PR gate's
# service container does -- so the dialect-specific upserts are exercised for real
# there, and a laptop without the stack still runs every test on SQLite.
_PG_URL = os.environ.get("DATABASE_URL", "")
BACKENDS = ["sqlite", *(["postgres"] if _PG_URL.startswith("postgresql") else [])]


@pytest.fixture(params=BACKENDS)
def db(request: pytest.FixtureRequest) -> Iterator[Engine]:
    """Every available backend in turn. Postgres runs in a throwaway schema."""
    if request.param == "sqlite":
        yield sqlite_engine()
        return
    from sqlalchemy import text

    name = f"test_{os.getpid()}_{abs(hash(request.node.nodeid)) % 10**8}"
    base = create_engine(_PG_URL)
    with base.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{name}"'))
    eng = base.execution_options(schema_translate_map={None: name})
    schema.create_all(eng)
    try:
        yield eng
    finally:
        with base.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{name}" CASCADE'))
        base.dispose()


LAKE_BUCKET = "data-lake"


@pytest.fixture
def aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake AWS credentials, and none of the developer's profiles or config files."""
    for name in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_SESSION_TOKEN", "AWS_ENDPOINT_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)


@pytest.fixture
def s3_client(aws_env: None) -> Iterator[Any]:
    """An in-process S3 (moto) holding one empty bucket, `LAKE_BUCKET`. No network.

    The client has no custom endpoint on purpose: moto intercepts AWS-shaped URLs only,
    so an R2-style `endpoint_url` would send a real request.
    """
    import boto3
    from moto import mock_aws

    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=LAKE_BUCKET)
        yield client


def bucket_keys(client: Any, bucket: str = LAKE_BUCKET) -> set[str]:
    page = client.list_objects_v2(Bucket=bucket)
    return {item["Key"] for item in page.get("Contents", [])}


def make_universe(*symbols: str) -> Universe:
    return Universe({s: Security(s, f"{s} Inc", "Q", is_etf=s in {"SPY", "QQQ"}) for s in symbols})


@pytest.fixture
def universe() -> Universe:
    return make_universe("AAPL", "NVDA", "TSLA", "AMD", "GME", "SPY", "QQQ", "IT", "ON", "BRK-B")


class FakeBrowser:
    """Serves queued results; records every URL it was asked for."""

    def __init__(self, pages: dict[str, PageResult] | None = None) -> None:
        self.pages = pages or {}
        self.default: PageResult | None = None
        self.calls: list[str] = []

    def _answer(self, url: str) -> PageResult:
        """Exact URL first, then the longest matching prefix, then `default`."""
        self.calls.append(url)
        if url in self.pages:
            return self.pages[url]
        matches = [p for p in self.pages if url.startswith(p)]
        if matches:
            return self.pages[max(matches, key=len)]
        if self.default is not None:
            return self.default
        raise AssertionError(f"unexpected URL {url}")

    def get_json(self, url: str) -> PageResult:
        return self._answer(url)

    def get_html(self, url: str) -> PageResult:
        return self._answer(url)

    def capture(
        self, url: str, want: Callable[[str], bool], scrolls: int, between: Callable[[], None]
    ) -> PageResult:
        """Hands back only the payloads `want` admits, as `PlaywrightBrowser` does.

        A fake that served every queued payload regardless hid X renaming the profile
        timeline op (`UserTweets` -> `UserOriginalsTimeline`, 2026-10-03): the tests
        stayed green while every live timeline visit captured nothing. The
        server-streamed payload is the real browser's own addition, so it passes too.
        """
        for _ in range(scrolls):
            between()
        result = self._answer(url)
        kept = [p for p in result.payloads if want(p.url) or SSR_PAYLOAD_SUFFIX in p.url]
        return replace(result, payloads=kept)

    def __enter__(self) -> FakeBrowser:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


# --- Reddit ---------------------------------------------------------------------------


def reddit_submission(post_id: str, created: float, **over: Any) -> dict[str, Any]:
    data = {
        "id": post_id,
        "name": f"t3_{post_id}",
        "subreddit": "wallstreetbets",
        "title": f"Post {post_id}",
        "selftext": "",
        "author": "someone",
        "created_utc": created,
        "score": 10,
        "num_comments": 2,
        "upvote_ratio": 0.9,
        "link_flair_text": "Discussion",
        "permalink": f"/r/wallstreetbets/comments/{post_id}/post/",
        "domain": "self.wallstreetbets",
        "is_self": True,
        "over_18": False,
        "stickied": False,
        **over,
    }
    return {"kind": "t3", "data": data}


def reddit_comment(comment_id: str, created: float, **over: Any) -> dict[str, Any]:
    data = {
        "id": comment_id,
        "name": f"t1_{comment_id}",
        "subreddit": "wallstreetbets",
        "body": f"comment {comment_id}",
        "author": "commenter",
        "created_utc": created,
        "score": 3,
        "link_id": "t3_abc123",
        "parent_id": "t3_abc123",
        "link_title": "Daily Discussion",
        "permalink": f"/r/wallstreetbets/comments/abc123/daily/{comment_id}/",
        **over,
    }
    return {"kind": "t1", "data": data}


def reddit_listing(children: list[dict[str, Any]], after: str | None = None) -> dict[str, Any]:
    return {"kind": "Listing", "data": {"after": after, "children": children}}


# --- X --------------------------------------------------------------------------------


def x_user_classic(user_id: str, handle: str, followers: int = 1000) -> dict[str, Any]:
    return {
        "__typename": "User",
        "rest_id": user_id,
        "is_blue_verified": True,
        "core": {"screen_name": handle, "created_at": "Tue Mar 01 10:00:00 +0000 2016"},
        "legacy": {"followers_count": followers},
    }


def x_tweet_classic(
    rest_id: str, text: str, user: dict[str, Any], symbols: tuple[str, ...] = (), **legacy: Any
) -> dict[str, Any]:
    return {
        "__typename": "Tweet",
        "rest_id": rest_id,
        "core": {"user_results": {"result": user}},
        "views": {"count": "1234"},
        "legacy": {
            "full_text": text,
            "created_at": "Mon Sep 28 14:00:00 +0000 2026",
            "favorite_count": 50,
            "retweet_count": 5,
            "reply_count": 7,
            "quote_count": 1,
            "bookmark_count": 2,
            "lang": "en",
            "conversation_id_str": rest_id,
            "entities": {"symbols": [{"text": s} for s in symbols]},
            **legacy,
        },
    }


def x_search_payload(*tweets: dict[str, Any]) -> dict[str, Any]:
    """A `SearchTimeline` response, entries nested the way the real one nests them."""
    entries = [
        {
            "entryId": f"tweet-{t.get('rest_id', 'x')}",
            "content": {
                "entryType": "TimelineTimelineItem",
                "itemContent": {"itemType": "TimelineTweet", "tweet_results": {"result": t}},
            },
        }
        for t in tweets
    ]
    return {
        "data": {
            "search_by_raw_query": {
                "search_timeline": {
                    "timeline": {
                        "instructions": [{"type": "TimelineAddEntries", "entries": entries}]
                    }
                }
            }
        }
    }


def x_tweet_2026(rest_id: str, text: str, user_id: str, handle: str, **over: Any) -> dict[str, Any]:
    """The 2026 web app shape: `details`, `counts`, `relationship_counts`, `verification`."""
    return {
        "__typename": "Tweet",
        "rest_id": rest_id,
        "core": {
            "user_results": {
                "result": {
                    "__typename": "User",
                    "rest_id": user_id,
                    "core": {"name": "Display", "screen_name": handle},
                    "relationship_counts": {"followers": 233266, "following": 10},
                    "verification": {"is_blue_verified": True, "verified_type": "Business"},
                }
            }
        },
        "counts": {
            "bookmark_count": 4,
            "favorite_count": 27,
            "quote_count": 1,
            "reply_count": 7,
            "retweet_count": 4,
        },
        "details": {
            "cashtag_entities": [],
            "created_at_ms": 1790611200000,
            "full_text": text,
        },
        "legacy": {"lang": "en", "possibly_sensitive": False},
        "views": {"count": "36978"},
        **over,
    }
