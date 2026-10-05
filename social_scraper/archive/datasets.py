"""The datasets this project publishes to the lake, and how a DB row maps in.

Everything lives under `social_scraper/` in the pooled tree, and every catalog name is
prefixed `social_scraper_`, so nothing collides with data-lake's own `social_posts`.
The stock domain's four datasets are what ibkr_trader reads; the sports domain's are
separate datasets, so no sports row ever lands in a stock one.

| dataset                               | grain                        | partition              |
| ------------------------------------- | ---------------------------- | ---------------------- |
| social_scraper_posts                  | one post/comment/tweet       | platform, created date |
| social_scraper_mentions               | one (post, symbol)           | platform, created date |
| social_scraper_engagement             | one post at one observation  | platform, observed date|
| social_scraper_symbols                | one symbol ever mentioned    | single file            |
| social_scraper_sports_posts           | one sports post              | platform, created date |
| social_scraper_sports_mentions        | one (post, entity)           | platform, created date |
| social_scraper_sports_engagement      | one post at one observation  | platform, observed date|
| social_scraper_sports_entities        | one team or athlete          | single file            |
| social_scraper_sports_entity_leagues  | one (entity, league)         | single file            |

`posts` keeps data-lake's `social_posts` column names (`platform`, `channel`,
`external_id`, `created_at`, `author_hash`, `title`, `body`, `score`, `num_comments`,
`symbols`, `fetched_at`), so restoring into that table is a column-for-column copy.
`sentiment` is deliberately absent: ibkr_trader scores rows where it is NULL.
"""

from __future__ import annotations

import json
from typing import Any

import pyarrow as pa

from social_scraper.archive.manifest import DatasetSpec
from social_scraper.models import as_utc

PREFIX = "social_scraper"
TS = pa.timestamp("us", tz="UTC")


def _dated(kind: str, ts_field: str):
    def partition_of(row: dict[str, Any]) -> str:
        return f"{PREFIX}/{kind}/platform={row['platform']}/dt={row[ts_field]:%Y-%m-%d}"

    return partition_of


POSTS = DatasetSpec(
    name=f"{PREFIX}_posts",
    ts_column="created_at",
    key_columns=("platform", "external_id"),
    schema=pa.schema(
        [
            ("platform", pa.string()),
            ("channel", pa.string()),
            ("external_id", pa.string()),
            ("kind", pa.string()),
            ("created_at", TS),
            ("fetched_at", TS),
            ("first_seen_at", TS),
            ("author_hash", pa.string()),
            ("title", pa.string()),
            ("body", pa.string()),
            ("url", pa.string()),
            ("parent_id", pa.string()),
            ("lang", pa.string()),
            ("flair", pa.string()),
            ("symbols", pa.list_(pa.string())),
            ("score", pa.int64()),
            ("num_comments", pa.int64()),
            ("upvote_ratio", pa.float64()),
            ("likes", pa.int64()),
            ("reposts", pa.int64()),
            ("quotes", pa.int64()),
            ("views", pa.int64()),
            ("bookmarks", pa.int64()),
            ("author_followers", pa.int64()),
            ("author_verified", pa.bool_()),
            ("stance", pa.float64()),
            ("stance_bull", pa.int32()),
            ("stance_bear", pa.int32()),
            ("extra", pa.string()),
        ]
    ),
    partition_of=_dated("posts", "created_at"),
)

MENTIONS = DatasetSpec(
    name=f"{PREFIX}_mentions",
    ts_column="created_at",
    key_columns=("platform", "external_id", "symbol"),
    schema=pa.schema(
        [
            ("platform", pa.string()),
            ("external_id", pa.string()),
            ("symbol", pa.string()),
            ("match_type", pa.string()),
            ("in_universe", pa.bool_()),
            ("channel", pa.string()),
            ("created_at", TS),
        ]
    ),
    partition_of=_dated("mentions", "created_at"),
)

ENGAGEMENT = DatasetSpec(
    name=f"{PREFIX}_engagement",
    ts_column="observed_at",
    key_columns=("platform", "external_id", "observed_at"),
    schema=pa.schema(
        [
            ("platform", pa.string()),
            ("external_id", pa.string()),
            ("observed_at", TS),
            ("score", pa.int64()),
            ("num_comments", pa.int64()),
            ("upvote_ratio", pa.float64()),
            ("likes", pa.int64()),
            ("reposts", pa.int64()),
            ("quotes", pa.int64()),
            ("views", pa.int64()),
            ("bookmarks", pa.int64()),
        ]
    ),
    partition_of=_dated("engagement", "observed_at"),
)

SYMBOLS = DatasetSpec(
    name=f"{PREFIX}_symbols",
    ts_column="first_seen_at",
    key_columns=("symbol",),
    schema=pa.schema(
        [
            ("symbol", pa.string()),
            ("in_universe", pa.bool_()),
            ("security_name", pa.string()),
            ("exchange", pa.string()),
            ("is_etf", pa.bool_()),
            ("first_seen_at", TS),
            ("last_seen_at", TS),
            ("mention_count", pa.int64()),
        ]
    ),
    partition_of=lambda _row: f"{PREFIX}/symbols",
)

STOCK = (POSTS, MENTIONS, ENGAGEMENT, SYMBOLS)

# --- the sports domain ------------------------------------------------------------------
# Same grains as the stock datasets, keyed on Wikidata entities instead of symbols.
# Entities ship without their subreddit and X handle: an athlete's handle is a
# person's identifier, and the lake takes none unhashed.

SPORTS_POSTS = DatasetSpec(
    name=f"{PREFIX}_sports_posts",
    ts_column="created_at",
    key_columns=("platform", "external_id"),
    schema=pa.schema(
        [
            ("platform", pa.string()),
            ("channel", pa.string()),
            ("external_id", pa.string()),
            ("kind", pa.string()),
            ("created_at", TS),
            ("fetched_at", TS),
            ("first_seen_at", TS),
            ("author_hash", pa.string()),
            ("title", pa.string()),
            ("body", pa.string()),
            ("url", pa.string()),
            ("parent_id", pa.string()),
            ("lang", pa.string()),
            ("flair", pa.string()),
            ("entities", pa.list_(pa.string())),
            ("leagues", pa.list_(pa.string())),
            ("score", pa.int64()),
            ("num_comments", pa.int64()),
            ("upvote_ratio", pa.float64()),
            ("likes", pa.int64()),
            ("reposts", pa.int64()),
            ("quotes", pa.int64()),
            ("views", pa.int64()),
            ("bookmarks", pa.int64()),
            ("author_followers", pa.int64()),
            ("author_verified", pa.bool_()),
            ("extra", pa.string()),
        ]
    ),
    partition_of=_dated("sports_posts", "created_at"),
)

SPORTS_MENTIONS = DatasetSpec(
    name=f"{PREFIX}_sports_mentions",
    ts_column="created_at",
    key_columns=("platform", "external_id", "entity_id"),
    schema=pa.schema(
        [
            ("platform", pa.string()),
            ("external_id", pa.string()),
            ("entity_id", pa.string()),
            ("match_type", pa.string()),
            ("channel", pa.string()),
            ("created_at", TS),
        ]
    ),
    partition_of=_dated("sports_mentions", "created_at"),
)

SPORTS_ENGAGEMENT = DatasetSpec(
    name=f"{PREFIX}_sports_engagement",
    ts_column="observed_at",
    key_columns=("platform", "external_id", "observed_at"),
    schema=ENGAGEMENT.schema,
    partition_of=_dated("sports_engagement", "observed_at"),
)

SPORTS_ENTITIES = DatasetSpec(
    name=f"{PREFIX}_sports_entities",
    ts_column="first_seen_at",
    key_columns=("entity_id",),
    schema=pa.schema(
        [
            ("entity_id", pa.string()),
            ("kind", pa.string()),
            ("name", pa.string()),
            ("aliases", pa.list_(pa.string())),
            ("team_id", pa.string()),
            ("popularity", pa.int64()),
            ("source", pa.string()),
            ("active", pa.bool_()),
            ("first_seen_at", TS),
            ("last_mentioned_at", TS),
            ("mention_count", pa.int64()),
        ]
    ),
    partition_of=lambda _row: f"{PREFIX}/sports_entities",
)

SPORTS_ENTITY_LEAGUES = DatasetSpec(
    name=f"{PREFIX}_sports_entity_leagues",
    ts_column="refreshed_at",
    key_columns=("entity_id", "league"),
    schema=pa.schema(
        [
            ("entity_id", pa.string()),
            ("league", pa.string()),
            ("source", pa.string()),
            ("active", pa.bool_()),
            ("refreshed_at", TS),
        ]
    ),
    partition_of=lambda _row: f"{PREFIX}/sports_entity_leagues",
)

SPORTS = (SPORTS_POSTS, SPORTS_MENTIONS, SPORTS_ENGAGEMENT, SPORTS_ENTITIES, SPORTS_ENTITY_LEAGUES)
ALL = (*STOCK, *SPORTS)
# Datasets whose rows carry free text, so a credential pasted into a post could ride along.
TEXT = (POSTS, SPORTS_POSTS)


def to_lake_row(spec: DatasetSpec, db_row: dict[str, Any]) -> dict[str, Any]:
    """Project an operational row onto `spec`'s columns, timestamps in UTC."""
    row = dict(db_row)
    if spec in TEXT:
        row["fetched_at"] = row.get("last_seen_at")
        raw = row.get("raw")
        row["extra"] = json.dumps(raw, sort_keys=True) if raw else None
    out = {}
    for field in spec.schema:
        value = row.get(field.name)
        if field.type == TS and value is not None:
            value = as_utc(value)
        out[field.name] = value
    return out
