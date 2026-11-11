from datetime import timedelta

import pytest
from conftest import NOW, make_universe
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from social_scraper.db import repository, schema
from social_scraper.db.repository import (
    CONNECT_ATTEMPT_TIMEOUT,
    CONNECT_PATIENCE,
    CONNECT_RETRY_DELAY,
    clear_watermarks,
    connect_patiently,
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


class Clock:
    """A monotonic clock that only moves when the code under test sleeps."""

    def __init__(self):
        self.now = 0.0
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def flaky(failures, error=OSError):
    """A connect that raises `error` `failures` times, then returns "conn"."""
    calls = []

    def connect():
        calls.append(1)
        if len(calls) <= failures:
            raise error("db is restarting")
        return "conn"

    return connect, calls


def test_connect_patiently_rides_out_a_short_outage():
    clock = Clock()
    connect, calls = flaky(2)
    assert connect_patiently(connect, OSError, clock.sleep, clock) == "conn"
    assert len(calls) == 3
    assert clock.slept == [CONNECT_RETRY_DELAY, CONNECT_RETRY_DELAY]


def test_connect_patiently_gives_up_once_its_patience_is_spent():
    clock = Clock()
    connect, calls = flaky(10_000)
    with pytest.raises(OSError, match="restarting"):
        connect_patiently(connect, OSError, clock.sleep, clock)
    assert clock.now >= CONNECT_PATIENCE
    assert len(calls) == CONNECT_PATIENCE // CONNECT_RETRY_DELAY + 1


def test_connect_patiently_does_not_retry_an_error_it_was_not_told_to():
    clock = Clock()
    connect, calls = flaky(1, error=ValueError)
    with pytest.raises(ValueError):
        connect_patiently(connect, OSError, clock.sleep, clock)
    assert len(calls) == 1
    assert clock.slept == []


def test_make_engine_retries_a_connect_the_database_refused(monkeypatch):
    """The 2026-10-11 run: a connect inside a db restart fails, the next one serves."""
    slept = []
    engine = make_engine("sqlite://", sleep=slept.append)
    dbapi = engine.dialect.loaded_dbapi
    real = dbapi.connect
    attempts = []

    def connect(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise dbapi.OperationalError("db is restarting")
        return real(*args, **kwargs)

    monkeypatch.setattr(dbapi, "connect", connect)
    with engine.connect() as conn:
        assert conn.execute(select(1)).scalar_one() == 1
    assert len(attempts) == 2
    assert slept == [CONNECT_RETRY_DELAY]


def test_make_engine_bounds_each_postgres_attempt(monkeypatch):
    """Not psycopg's default 130 s: one hung attempt must not eat the whole patience."""
    engine = make_engine("postgresql+psycopg://u:p@127.0.0.1:1/db", sleep=lambda _s: None)
    dbapi = engine.dialect.loaded_dbapi
    seen = []

    def refuse(*args, **kwargs):
        seen.append(kwargs)
        raise dbapi.OperationalError("connection timeout expired")

    monkeypatch.setattr(dbapi, "connect", refuse)
    monkeypatch.setattr(repository, "CONNECT_PATIENCE", 0)
    with pytest.raises(OperationalError):
        engine.connect()
    assert seen[0]["connect_timeout"] == CONNECT_ATTEMPT_TIMEOUT


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


def test_upsert_post_strips_nul_from_every_string_on_insert_and_refresh(db):
    # Postgres text and JSON refuse U+0000 (devkit ledger 00f22a0e); every domain's
    # posts table is written here, so this is where it is removed.
    raw = {"permalink": "/p\x00", "nested": {"k\x00": ["a\x00", 1, None, True]}}
    with db.begin() as conn:
        upsert_post(conn, post_row(title="t\x00", body="\x00b\x00", raw=raw), NOW)
    with db.begin() as conn:
        upsert_post(conn, post_row(body="edited\x00", symbols=["NV\x00DA"]), NOW)
    with db.connect() as conn:
        row = conn.execute(select(schema.posts)).mappings().one()
    assert row["title"] == "t"
    assert row["body"] == "edited"
    assert row["symbols"] == ["NVDA"]
    assert row["raw"] == {"permalink": "/p", "nested": {"k": ["a", 1, None, True]}}


def test_upsert_post_strips_nul_on_the_sports_table_too(db):
    row = post_row(body="b\x00", entities=["Q\x001"], leagues=["nhl"])
    del row["symbols"]
    with db.begin() as conn:
        upsert_post(conn, row, NOW, schema.sports_posts)
    with db.connect() as conn:
        stored = conn.execute(select(schema.sports_posts)).mappings().one()
    assert stored["body"] == "b" and stored["entities"] == ["Q1"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("a\x00b\x00", "ab"),
        ("clean", "clean"),
        ("", ""),
        ({"k\x00": "v\x00"}, {"k": "v"}),
        (["x\x00", ("y\x00",)], ["x", ("y",)]),
        (5, 5),
        (None, None),
        (NOW, NOW),
    ],
)
def test_strip_nul_recurses_and_leaves_non_strings_alone(value, expected):
    assert repository.strip_nul(value) == expected


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


def test_clear_watermarks_forgets_every_dataset(db):
    with db.begin() as conn:
        assert clear_watermarks(conn) == 0
        set_watermark(conn, "d", NOW, NOW)
        set_watermark(conn, "e", NOW, NOW)
    with db.begin() as conn:
        assert clear_watermarks(conn) == 2
    with db.connect() as conn:
        assert get_watermark(conn, "d") is None and get_watermark(conn, "e") is None


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
