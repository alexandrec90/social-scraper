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
# because visiting a timeline needs it; the lake sees `author_hash` alone. The stock
# table's on-topic count is the column `timeline_finance_tweets`; `key` gives it the
# domain-neutral name `x.accounts` uses for both tables, with no migration.
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
    Column(
        "timeline_finance_tweets",
        Integer,
        key="timeline_relevant_tweets",
        nullable=False,
        default=0,
    ),
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

# --- the sports domain (`social_scraper/sports/`) ----------------------------------------
# Its own tables rather than a `domain` column on the stock ones: `create_all` adds a
# table but never alters one, and the two domains share no subject to join on. Shapes
# follow the stock tables, so the crawlers and the export treat both alike.

sports_posts = Table(
    "sports_posts",
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
    Column("entities", JSON, nullable=False),
    Column("leagues", JSON, nullable=False),
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
    Column("raw", JSON),
    UniqueConstraint("platform", "external_id", name="uq_sports_posts_platform_external_id"),
    Index("ix_sports_posts_updated_at", "updated_at"),
    Index("ix_sports_posts_created_at", "created_at"),
)

sports_mentions = Table(
    "sports_mentions",
    metadata,
    Column("platform", String(16), primary_key=True),
    Column("external_id", String(64), primary_key=True),
    Column("entity_id", String(32), primary_key=True),
    Column("match_type", String(16), nullable=False),
    Column("channel", String(64), nullable=False),
    Column("created_at", TS, nullable=False),
    Column("updated_at", TS, nullable=False),
    Index("ix_sports_mentions_entity_created", "entity_id", "created_at"),
    Index("ix_sports_mentions_updated_at", "updated_at"),
)

sports_engagement = Table(
    "sports_engagement_snapshots",
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
    Index("ix_sports_engagement_observed_at", "observed_at"),
)

# Teams and athletes a mention can name: Wikidata's, refreshed weekly
# (`sports.wikidata`), plus athletes confirmed from text (`sports.discover`). `entity_id`
# is the Wikidata QID. `subreddit` and `x_handle` stay here -- the lake gets neither.
sports_entities = Table(
    "sports_entities",
    metadata,
    Column("entity_id", String(32), primary_key=True),
    Column("kind", String(16), nullable=False),  # team | athlete
    Column("name", Text, nullable=False),
    Column("aliases", JSON, nullable=False),
    Column("subreddit", String(64)),
    Column("x_handle", String(32)),
    Column("team_id", String(32)),
    Column("popularity", Integer, nullable=False, default=0),
    Column("source", String(16), nullable=False),  # wikidata | discovered
    Column("active", Boolean, nullable=False),
    Column("first_seen_at", TS, nullable=False),
    Column("refreshed_at", TS, nullable=False),
    Column("mention_count", BigInteger, nullable=False, default=0),
    Column("last_mentioned_at", TS),
    Column("updated_at", TS, nullable=False),
)

# Which leagues an entity plays in: a club is in its domestic league and in Europe.
# `source` says who vouches for the membership: a weekly sync retires only its own.
sports_entity_leagues = Table(
    "sports_entity_leagues",
    metadata,
    Column("entity_id", String(32), primary_key=True),
    Column("league", String(32), primary_key=True),
    Column("source", String(16), nullable=False),  # wikidata | discovered
    Column("active", Boolean, nullable=False),
    Column("refreshed_at", TS, nullable=False),
)

# `x_accounts` for the sports domain, plus the reputation evidence the stock table
# lacks: an organisation's account, and how many of its own tweets sell picks.
sports_x_accounts = Table(
    "sports_x_accounts",
    metadata,
    Column("user_id", String(32), primary_key=True),
    Column("handle", String(32), nullable=False),
    Column("status", String(16), nullable=False),
    Column("source", String(16), nullable=False),
    Column("followers", BigInteger),
    Column("verified", Boolean),
    Column("affiliated", Boolean, nullable=False, default=False),
    Column("search_hits", Integer, nullable=False, default=0),
    Column("search_engagement", BigInteger, nullable=False, default=0),
    Column("timeline_tweets", Integer, nullable=False, default=0),
    Column("timeline_relevant_tweets", Integer, nullable=False, default=0),
    Column("tout_tweets", Integer, nullable=False, default=0),
    Column("mentioned_by_tracked", Integer, nullable=False, default=0),
    Column("score", Float, nullable=False, default=0.0),
    Column("first_seen_at", TS, nullable=False),
    Column("last_seen_at", TS, nullable=False),
    Column("last_timeline_at", TS),
    Index("ix_sports_x_accounts_status_score", "status", "score"),
)

# Capitalised names seen in a league's posts that no entity matched: the athletes
# Wikidata's rosters missed, until a lookup confirms or rejects them. Never exported;
# unconfirmed names are pruned (`sports.discover.CANDIDATE_RETENTION`).
sports_name_candidates = Table(
    "sports_name_candidates",
    metadata,
    Column("name", String(128), primary_key=True),
    Column("league", String(32), primary_key=True),
    Column("sightings", Integer, nullable=False),
    Column("status", String(16), nullable=False),  # pending | confirmed | rejected
    Column("entity_id", String(32)),
    Column("first_seen_at", TS, nullable=False),
    Column("last_seen_at", TS, nullable=False),
    Column("checked_at", TS),
    Index("ix_sports_name_candidates_status", "status", "sightings"),
)


# --- Polymarket (`social_scraper/polymarket/`) --------------------------------------------
# Prediction markets, not posts: what a market asks, and what it was priced at over time.
# Both domains' markets share the tables; `domains` says which one each is for, and the
# stock datasets ibkr_trader reads never see a row of them. No trader or resolver address
# is kept: Polymarket shows them, but they identify people.

polymarket_markets = Table(
    "polymarket_markets",
    metadata,
    Column("market_id", String(32), primary_key=True),
    # Always "polymarket": the lake partitions every dataset by platform.
    Column("platform", String(16), nullable=False),
    Column("event_id", String(32), nullable=False),
    Column("event_slug", Text),
    Column("event_title", Text),
    Column("question", Text, nullable=False),
    Column("slug", Text),
    Column("domains", JSON, nullable=False),  # ["stocks"], ["sports"] or both
    Column("tags", JSON, nullable=False),  # the event's tag slugs
    Column("leagues", JSON, nullable=False),  # league keys, from their `polymarket_tags`
    Column("symbols", JSON, nullable=False),  # listed tickers the market names
    Column("outcomes", JSON, nullable=False),
    # moneyline | totals | ... | baseball_player_earned_runs_allowed (35 characters, 2026-10-10)
    Column("sports_market_type", String(64)),
    Column("line", Float),
    # A game's own state, as the event reports it.
    Column("game_id", String(32)),
    Column("teams", JSON),
    Column("score", String(32)),
    Column("period", String(32)),
    Column("live", Boolean),
    Column("ended", Boolean),
    Column("start_time", TS),
    Column("end_date", TS),
    Column("created_at", TS, nullable=False),
    Column("active", Boolean),
    Column("closed", Boolean),
    # The resolved outcome's index into `outcomes`, once a closed market's prices say so.
    Column("winner", Integer),
    Column("first_seen_at", TS, nullable=False),
    Column("updated_at", TS, nullable=False),
    Index("ix_polymarket_markets_updated_at", "updated_at"),
    Index("ix_polymarket_markets_open", "closed", "end_date"),
)

# One market at one observation, written only when its price or game state moved.
polymarket_prices = Table(
    "polymarket_prices",
    metadata,
    Column("market_id", String(32), primary_key=True),
    Column("observed_at", TS, primary_key=True),
    Column("platform", String(16), nullable=False),
    Column("prices", JSON, nullable=False),  # one per outcome, in `outcomes` order
    Column("last_trade_price", Float),
    Column("best_bid", Float),
    Column("best_ask", Float),
    Column("spread", Float),
    Column("volume", Float),
    Column("volume_24hr", Float),
    Column("liquidity", Float),
    Column("score", String(32)),
    Column("period", String(32)),
    Column("live", Boolean),
    Index("ix_polymarket_prices_observed_at", "observed_at"),
)


def create_all(engine: Engine) -> None:
    """Create every missing table. Idempotent; never alters an existing table."""
    metadata.create_all(engine)
