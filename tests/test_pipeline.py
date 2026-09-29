from datetime import timedelta

from conftest import NOW, Clock
from sqlalchemy import select

from social_scraper.db import schema
from social_scraper.models import AuthorInfo, Engagement, Post, stable_hash
from social_scraper.pipeline import Ingestor, IngestStats, build_row


def post(external_id="p1", body="Loading $NVDA calls, email me a@b.co", **over):
    fields = {
        "platform": "reddit",
        "external_id": external_id,
        "kind": "submission",
        "channel": "wallstreetbets",
        "created_at": NOW - timedelta(hours=1),
        "author": AuthorInfo(name="someone"),
        "title": "AMD and NVDA",
        "body": body,
        "flair": "YOLO",
        "engagement": Engagement(score=10, comments=3, upvote_ratio=0.9),
        "raw": {"permalink": "/r/x/1", "unlisted_field": "dropme"},
        **over,
    }
    return Post(**fields)


def test_build_row_hashes_scrubs_extracts_and_scores(universe):
    row, found = build_row(post(), universe)
    assert row["author_hash"] == stable_hash("someone")
    assert "a@b.co" not in row["body"]
    assert row["symbols"] == ["AMD", "NVDA"]
    assert {m.symbol for m in found} == {"AMD", "NVDA"}
    assert row["stance"] == 1.0 and row["stance_bull"] == 1
    assert row["raw"] == {"permalink": "/r/x/1"}
    assert row["score"] == 10 and row["upvote_ratio"] == 0.9
    assert row["flair"] == "YOLO"


def test_build_row_truncates_long_fields(universe):
    row, _ = build_row(post(channel="c" * 100, flair="f" * 300, lang="english-long"), universe)
    assert len(row["channel"]) == 64
    assert len(row["flair"]) == 128
    assert len(row["lang"]) == 8


def test_ingest_stores_posts_mentions_and_snapshots(engine, universe):
    stats = Ingestor(engine, universe, Clock()).ingest([post(), post("p2", body="nothing here")])
    assert (stats.seen, stats.stored, stats.new_mentions, stats.snapshots) == (2, 2, 4, 2)
    with engine.connect() as conn:
        assert len(conn.execute(select(schema.posts)).all()) == 2
        assert len(conn.execute(select(schema.mentions)).all()) == 4


def test_ingest_collapses_duplicates_in_a_batch(engine, universe):
    stats = Ingestor(engine, universe, Clock()).ingest([post(), post()])
    assert stats.seen == 2 and stats.stored == 1


def test_reingest_updates_without_duplicating(engine, universe):
    clock = Clock()
    ing = Ingestor(engine, universe, clock)
    ing.ingest([post()])
    clock.now = NOW + timedelta(hours=1)
    stats = ing.ingest([post(engagement=Engagement(score=500))])
    assert stats.new_mentions == 0 and stats.snapshots == 1
    with engine.connect() as conn:
        assert conn.execute(select(schema.posts.c.score)).scalar_one() == 500


def test_ingest_stats_add():
    a = IngestStats(1, 1, 1, 1)
    a.add(IngestStats(2, 2, 2, 2))
    assert a == IngestStats(3, 3, 3, 3)
