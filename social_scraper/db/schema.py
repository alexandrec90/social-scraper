"""Operational tables -- the source of truth the scraper reads and writes.

The lake is bronze and downstream: `export.py` copies these rows out, nothing reads
the lake back. Tables are SQLAlchemy Core so the same definitions run on Postgres (the
deployment) and SQLite (the unit tests).
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.engine import Engine

metadata = MetaData()

TS = DateTime(timezone=True)
# SQLite only auto-increments INTEGER PRIMARY KEY, so the big-int id degrades to it there.
BIG_ID = BigInteger().with_variant(Integer, "sqlite")

posts = Table(
    "posts",
    metadata,
    Column("id", BIG_ID, primary_key=True, autoincrement=True),
    Column("platform", String(16), nullable=False),
    Column("external_id", String(64), nullable=False),
    Column("kind", String(16), nullable=False),
    Column("channel", String(64), nullable=False),
    Column("created_at", TS, nullable=False),
    Column("first_seen_at", TS, nullable=False),
    Column("last_seen_at", TS, nullable=False),
    Column("updated_at", TS, nullable=False),
    Column("author_hash", String(64)),
    Column("title", Text),
    Column("body", Text),
    Column("url", Text),
    Column("parent_id", String(64)),
    Column("lang", String(8)),
    Column("flair", String(128)),
    Column("symbols", JSON, nullable=False),
    Column("score", BigInteger),
    Column("num_comments", BigInteger),
    Column("upvote_ratio", Float),
    Column("likes", BigInteger),
    Column("reposts", BigInteger),
    Column("quotes", BigInteger),
    Column("views", BigInteger),
    Column("bookmarks", BigInteger),
    Column("author_followers", BigInteger),
    Column("author_verified", Boolean),
    Column("stance", Float),
    Column("stance_bull", Integer),
    Column("stance_bear", Integer),
    Column("raw", JSON),
    UniqueConstraint("platform", "external_id", name="uq_posts_platform_external_id"),
    Index("ix_posts_updated_at", "updated_at"),
    Index("ix_posts_created_at", "created_at"),
)

mentions = Table(
    "mentions",
    metadata,
    Column("platform", String(16), primary_key=True),
    Column("external_id", String(64), primary_key=True),
    Column("symbol", String(16), primary_key=True),
    Column("match_type", String(16), nullable=False),
    Column("in_universe", Boolean, nullable=False),
    Column("channel", String(64), nullable=False),
    Column("created_at", TS, nullable=False),
    Column("updated_at", TS, nullable=False),
    Index("ix_mentions_symbol_created", "symbol", "created_at"),
    Index("ix_mentions_updated_at", "updated_at"),
)

engagement = Table(
    "engagement_snapshots",
    metadata,
    Column("platform", String(16), primary_key=True),
    Column("external_id", String(64), primary_key=True),
    Column("observed_at", TS, primary_key=True),
    Column("score", BigInteger),
    Column("num_comments", BigInteger),
    Column("upvote_ratio", Float),
    Column("likes", BigInteger),
    Column("reposts", BigInteger),
    Column("quotes", BigInteger),
    Column("views", BigInteger),
    Column("bookmarks", BigInteger),
    Index("ix_engagement_observed_at", "observed_at"),
)

symbols = Table(
    "symbols",
    metadata,
    Column("symbol", String(16), primary_key=True),
    Column("in_universe", Boolean, nullable=False),
    Column("security_name", Text),
    Column("exchange", String(16)),
    Column("is_etf", Boolean),
    Column("first_seen_at", TS, nullable=False),
    Column("last_seen_at", TS, nullable=False),
    Column("mention_count", BigInteger, nullable=False),
    Column("updated_at", TS, nullable=False),
)

# X accounts the discovery loop knows about. `handle` is kept here, and only here,
# because visiting a timeline needs it; the lake sees `author_hash` alone.
x_accounts = Table(
    "x_accounts",
    metadata,
    Column("user_id", String(32), primary_key=True),
    Column("handle", String(32), nullable=False),
    Column("status", String(16), nullable=False),  # candidate | tracked | dropped | seed
    Column("source", String(16), nullable=False),  # search | mention | seed
    Column("followers", BigInteger),
    Column("verified", Boolean),
    Column("search_hits", Integer, nullable=False, default=0),
    Column("search_engagement", BigInteger, nullable=False, default=0),
    Column("timeline_tweets", Integer, nullable=False, default=0),
    Column("timeline_finance_tweets", Integer, nullable=False, default=0),
    Column("mentioned_by_tracked", Integer, nullable=False, default=0),
    Column("score", Float, nullable=False, default=0.0),
    Column("first_seen_at", TS, nullable=False),
    Column("last_seen_at", TS, nullable=False),
    Column("last_timeline_at", TS),
    Index("ix_x_accounts_status_score", "status", "score"),
)

# Free-form cursors: newest item seen per listing, last refresh time, query rotation.
crawl_state = Table(
    "crawl_state",
    metadata,
    Column("key", String(128), primary_key=True),
    Column("value", Text, nullable=False),
    Column("updated_at", TS, nullable=False),
)

# One row per page load / JSON fetch; budgets are counted from it.
request_log = Table(
    "request_log",
    metadata,
    Column("id", BIG_ID, primary_key=True, autoincrement=True),
    Column("platform", String(16), nullable=False),
    Column("at", TS, nullable=False),
    Column("kind", String(32), nullable=False),
    Column("status", Integer),
    Column("ok", Boolean, nullable=False),
    Index("ix_request_log_platform_at", "platform", "at"),
)

# Per-platform circuit breaker: back off after a 429, stop after a challenge.
throttle_state = Table(
    "throttle_state",
    metadata,
    Column("platform", String(16), primary_key=True),
    Column("blocked_until", TS),
    Column("reason", Text),
    Column("consecutive_failures", Integer, nullable=False, default=0),
    Column("updated_at", TS, nullable=False),
)

export_watermarks = Table(
    "export_watermarks",
    metadata,
    Column("dataset", String(64), primary_key=True),
    Column("exported_through", TS, nullable=False),
    Column("updated_at", TS, nullable=False),
)


def create_all(engine: Engine) -> None:
    """Create every missing table. Idempotent; never alters an existing table."""
    metadata.create_all(engine)
