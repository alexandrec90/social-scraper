from datetime import timedelta

from conftest import NOW, make_universe
from sqlalchemy import select

from social_scraper.db import repository, schema
from social_scraper.db.repository import (
    dialect_insert,
    get_state,
    get_watermark,
    make_engine,
    record_engagement,
    record_mentions,
    rows_since,
    set_state,
    set_watermark,
    states_with_prefix,
    table_counts,
    trending_symbols,
    upsert_post,
)
from social_scraper.symbols.extract import BARE, CASHTAG, Mention


def post_row(external_id="p1", **over):
    row = {
        "platform": "reddit",
        "external_id": external_id,
        "kind": "submission",
        "channel": "stocks",
        "created_at": NOW - timedelta(hours=1),
        "author_hash": "h",
        "title": "t",
        "body": "b",
        "symbols": ["NVDA"],
        "score": 1,
        "num_comments": 0,
    }
    return {**row, **over}


def test_make_engine_builds_an_engine():
    assert make_engine("sqlite://").dialect.name == "sqlite"


def test_dialect_insert_matches_backend(db):
    with db.connect() as conn:
        stmt = dialect_insert(conn, schema.crawl_state)
        assert hasattr(stmt, "on_conflict_do_update")


def test_upsert_post_inserts_then_refreshes_but_keeps_first_seen(db):
    with db.begin() as conn:
        upsert_post(conn, post_row(), NOW)
    later = NOW + timedelta(hours=2)
    with db.begin() as conn:
        upsert_post(conn, post_row(score=99, body="edited"), later)
    with db.connect() as conn:
        rows = conn.execute(select(schema.posts)).mappings().all()
    assert len(rows) == 1
    row = rows[0]
    assert row["score"] == 99 and row["body"] == "edited"
    assert repository.as_utc(row["first_seen_at"]) == NOW
    assert repository.as_utc(row["last_seen_at"]) == later
    assert repository.as_utc(row["updated_at"]) == later


def test_record_engagement_skips_unchanged_and_empty(db):
    values = {"score": 5, "num_comments": 1}
    with db.begin() as conn:
        assert record_engagement(conn, "reddit", "p1", NOW, values)
        assert not record_engagement(conn, "reddit", "p1", NOW + timedelta(minutes=5), values)
        assert record_engagement(conn, "reddit", "p1", NOW + timedelta(minutes=10), {"score": 6})
        assert not record_engagement(conn, "reddit", "p2", NOW, {})
    with db.connect() as conn:
        assert table_counts(conn)["engagement_snapshots"] == 2


def test_record_mentions_is_idempotent_and_counts_symbols(db):
    u = make_universe("NVDA")
    row = post_row()
    found = [Mention("NVDA", BARE, True), Mention("NEWCO", CASHTAG, False)]
    with db.begin() as conn:
        assert record_mentions(conn, row, found, u, NOW) == 2
        assert record_mentions(conn, row, found, u, NOW) == 0
        record_mentions(conn, post_row("p2"), found[:1], u, NOW)
    with db.connect() as conn:
        syms = {r.symbol: r for r in conn.execute(select(schema.symbols))}
    assert syms["NVDA"].mention_count == 2
    assert syms["NVDA"].security_name == "NVDA Inc"
    assert syms["NEWCO"].in_universe is False


def test_trending_symbols_counts_recent_validated_only(db):
    u = make_universe("NVDA", "AMD")
    with db.begin() as conn:
        for i in range(3):
            record_mentions(conn, post_row(f"n{i}"), [Mention("NVDA", BARE, True)], u, NOW)
        record_mentions(conn, post_row("a1"), [Mention("AMD", BARE, True)], u, NOW)
        record_mentions(conn, post_row("x1"), [Mention("NEWCO", CASHTAG, False)], u, NOW)
        old = post_row("old", created_at=NOW - timedelta(days=3))
        record_mentions(conn, old, [Mention("AMD", BARE, True)], u, NOW)
    with db.connect() as conn:
        assert trending_symbols(conn, NOW - timedelta(hours=24), 10) == [("NVDA", 3), ("AMD", 1)]
        assert trending_symbols(conn, NOW - timedelta(hours=24), 1) == [("NVDA", 3)]


def test_state_roundtrip_and_prefix(db):
    with db.begin() as conn:
        assert get_state(conn, "k") is None
        set_state(conn, "k", "1", NOW)
        set_state(conn, "k", "2", NOW)
        set_state(conn, "x:searched:NVDA", "a", NOW)
        set_state(conn, "x:searched:AMD", "b", NOW)
    with db.connect() as conn:
        assert get_state(conn, "k") == "2"
        assert states_with_prefix(conn, "x:searched:") == {"NVDA": "a", "AMD": "b"}


def test_rows_since_orders_and_limits(db):
    with db.begin() as conn:
        for i in range(3):
            set_state(conn, f"k{i}", str(i), NOW + timedelta(minutes=i))
    with db.connect() as conn:
        all_rows = rows_since(conn, schema.crawl_state, "updated_at", None, 10)
        assert [r["key"] for r in all_rows] == ["k0", "k1", "k2"]
        after = rows_since(conn, schema.crawl_state, "updated_at", NOW, 1)
        assert [r["key"] for r in after] == ["k1"]


def test_watermark_roundtrip(db):
    with db.begin() as conn:
        assert get_watermark(conn, "d") is None
        set_watermark(conn, "d", NOW, NOW)
        set_watermark(conn, "d", NOW + timedelta(hours=1), NOW)
    with db.connect() as conn:
        assert get_watermark(conn, "d") == NOW + timedelta(hours=1)


def test_table_counts(db):
    with db.begin() as conn:
        upsert_post(conn, post_row(), NOW)
    with db.connect() as conn:
        counts = table_counts(conn)
    assert counts == {"posts": 1, "mentions": 0, "engagement_snapshots": 0, "symbols": 0}


def test_create_all_is_idempotent(db):
    schema.create_all(db)
    schema.create_all(db)
    with db.connect() as conn:
        assert table_counts(conn)["posts"] == 0
