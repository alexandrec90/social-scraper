from datetime import timedelta

from conftest import NOW

from social_scraper.db.throttle_store import REQUEST_LOG_RETENTION_DAYS, DbThrottleStore
from social_scraper.throttle import Block


def test_counts_and_last_request(db):
    store = DbThrottleStore(db)
    assert store.last_request_at("x") is None
    store.log_request("x", NOW - timedelta(minutes=30), "search", 200, True)
    store.log_request("x", NOW - timedelta(hours=3), "timeline", 200, True)
    store.log_request("reddit", NOW, "listing:new", 200, True)
    assert store.count_since("x", NOW - timedelta(hours=1)) == 1
    assert store.count_since("x", NOW - timedelta(days=1)) == 2
    assert store.last_request_at("x") == NOW - timedelta(minutes=30)


def test_long_kind_is_truncated(db):
    store = DbThrottleStore(db)
    store.log_request("x", NOW, "k" * 100, None, False)
    assert store.count_since("x", NOW - timedelta(seconds=1)) == 1


def test_block_roundtrip(db):
    store = DbThrottleStore(db)
    assert store.get_block("x") == Block(None, None, 0)
    store.set_block("x", Block(NOW + timedelta(hours=1), "captcha", 2), NOW)
    assert store.get_block("x") == Block(NOW + timedelta(hours=1), "captcha", 2)
    store.set_block("x", Block(None, None, 0), NOW)
    assert store.get_block("x") == Block(None, None, 0)


def test_prune_drops_only_old_rows(db):
    store = DbThrottleStore(db)
    store.log_request("x", NOW - timedelta(days=REQUEST_LOG_RETENTION_DAYS + 1), "k", 200, True)
    store.log_request("x", NOW, "k", 200, True)
    assert store.prune(NOW - timedelta(days=REQUEST_LOG_RETENTION_DAYS)) == 1
    assert store.count_since("x", NOW - timedelta(days=365)) == 1
