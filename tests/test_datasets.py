import json
from datetime import datetime

from conftest import NOW

from social_scraper.archive import datasets
from social_scraper.archive.datasets import ALL, ENGAGEMENT, MENTIONS, POSTS, SYMBOLS, to_lake_row

# The data-lake `social_posts` columns a restore copies one-for-one.
SOCIAL_POSTS_COLUMNS = {
    "platform",
    "channel",
    "external_id",
    "created_at",
    "author_hash",
    "title",
    "body",
    "score",
    "num_comments",
    "symbols",
    "fetched_at",
}


def test_names_are_namespaced_and_unique():
    names = [s.name for s in ALL]
    assert len(set(names)) == len(names)
    assert all(n.startswith("social_scraper_") for n in names)


def test_posts_carries_every_social_posts_column():
    assert SOCIAL_POSTS_COLUMNS <= set(POSTS.schema.names)
    assert "sentiment" not in POSTS.schema.names  # ibkr_trader scores NULL rows itself


def test_ts_and_key_columns_exist_in_each_schema():
    for spec in ALL:
        assert spec.ts_column in spec.schema.names
        assert set(spec.key_columns) <= set(spec.schema.names)


def test_partition_paths():
    row = {"platform": "x", "created_at": NOW, "observed_at": NOW}
    for spec, folder in ((POSTS, "posts"), (MENTIONS, "mentions"), (ENGAGEMENT, "engagement")):
        assert spec.partition_of(row) == "/".join(
            ("social_scraper", folder, "platform=x", "dt=2026-09-28")
        )
    assert SYMBOLS.partition_of({}) == f"{datasets.PREFIX}/symbols"


def test_to_lake_row_maps_posts_and_normalises_time():
    naive = datetime(2026, 9, 28, 12)
    db_row = {
        "platform": "reddit",
        "external_id": "a",
        "created_at": naive,
        "last_seen_at": NOW,
        "raw": {"permalink": "/r/x"},
        "internal_only": "never exported",
    }
    out = to_lake_row(POSTS, db_row)
    assert set(out) == set(POSTS.schema.names)
    assert out["created_at"].tzinfo is not None
    assert out["fetched_at"] == NOW
    assert json.loads(out["extra"]) == {"permalink": "/r/x"}
    assert out["body"] is None


def test_sports_datasets_are_separate_and_shaped_like_the_stock_ones():
    assert set(datasets.SPORTS).isdisjoint(datasets.STOCK)
    assert datasets.ALL == (*datasets.STOCK, *datasets.SPORTS)
    assert datasets.TEXT == (POSTS, datasets.SPORTS_POSTS)
    row = {"platform": "reddit", "created_at": NOW, "observed_at": NOW}
    assert datasets.SPORTS_POSTS.partition_of(row) == (
        "social_scraper/sports_posts/platform=reddit/dt=2026-09-28"
    )
    assert datasets.SPORTS_ENGAGEMENT.partition_of(row).startswith("social_scraper/sports_eng")
    assert datasets.SPORTS_ENTITIES.partition_of({}) == "social_scraper/sports_entities"
    assert {"entities", "leagues"} <= set(datasets.SPORTS_POSTS.schema.names)
    assert "symbols" not in datasets.SPORTS_POSTS.schema.names


def test_sports_entities_never_carry_a_handle_or_subreddit():
    db_row = {"entity_id": "Q1", "name": "Some Athlete", "x_handle": "someone", "subreddit": "s"}
    out = to_lake_row(datasets.SPORTS_ENTITIES, db_row)
    assert "x_handle" not in out and "subreddit" not in out and out["name"] == "Some Athlete"


def test_sports_posts_map_like_posts():
    out = to_lake_row(datasets.SPORTS_POSTS, {"last_seen_at": NOW, "raw": {"permalink": "/r/x"}})
    assert out["fetched_at"] == NOW and json.loads(out["extra"]) == {"permalink": "/r/x"}


def test_to_lake_row_other_datasets_are_projections():
    out = to_lake_row(MENTIONS, {"platform": "x", "symbol": "NVDA", "updated_at": NOW})
    assert set(out) == set(MENTIONS.schema.names) and out["symbol"] == "NVDA"
