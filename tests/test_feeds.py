"""`feeds`: page classification, symbol picking, and the shared crawl's failure rules."""

from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import NOW, Clock, FakeBrowser, make_universe

from social_scraper import feeds
from social_scraper.db import repository
from social_scraper.feeds import HTML, JSON, FeedCrawler, classify, pick_symbols
from social_scraper.fetch import FetchFailed, PageResult
from social_scraper.pipeline import IngestStats
from social_scraper.symbols.extract import BARE, Mention
from social_scraper.throttle import (
    CHALLENGED,
    ERROR,
    OK,
    RATE_LIMITED,
    TIMED_OUT,
    Block,
    MemoryThrottleStore,
    Policy,
    Throttle,
)


@pytest.mark.parametrize(
    ("result", "expect", "outcome"),
    [
        (PageResult(200, "u", data={"a": 1}), JSON, OK),
        (PageResult(200, "u", html="<html>"), HTML, OK),
        (PageResult(None, "u", timed_out=True), JSON, TIMED_OUT),
        (PageResult(429, "u"), JSON, RATE_LIMITED),
        (PageResult(200, "u", text="Just a moment..."), HTML, CHALLENGED),
        (PageResult(403, "u", data={"error": "x"}), JSON, CHALLENGED),
        (PageResult(503, "u", text="Checking your browser"), HTML, CHALLENGED),
        (PageResult(500, "u"), JSON, ERROR),
        (PageResult(None, "u", text="ConnectionResetError"), JSON, ERROR),
        (PageResult(404, "u"), JSON, OK),
        (PageResult(400, "u", data={"error": "bad query"}), JSON, ERROR),
        (PageResult(200, "u", text="not json"), JSON, ERROR),
        (PageResult(200, "u"), HTML, ERROR),
    ],
)
def test_classify(result, expect, outcome):
    assert classify(result, expect)[0] == outcome


def test_no_body_says_what_an_api_answered_instead():
    big = PageResult(200, "u", text="response larger than 33554432 bytes, not read")
    assert feeds.no_body(big, JSON) == f"HTTP 200 but no json body ({big.text})"
    assert classify(big, JSON) == (ERROR, feeds.no_body(big, JSON))
    assert (
        feeds.no_body(PageResult(200, "u", text="page words"), HTML) == "HTTP 200 but no html body"
    )


def test_classify_never_reads_a_wall_into_a_good_answer():
    # A JSON body that happens to say "captcha" is content, not a challenge.
    result = PageResult(200, "u", data={"body": "captcha"}, text="captcha")
    assert classify(result, JSON) == (OK, "")


def test_pick_symbols_watchlist_first_then_trending_and_paced(engine):
    with engine.begin() as conn:
        repository.set_state(conn, "p:read:AAPL", (NOW - timedelta(minutes=10)).isoformat(), NOW)
    picked = pick_symbols(engine, "p:read:", ["AAPL", "NVDA"], 1, NOW, timedelta(hours=1))
    assert picked == ["NVDA"]
    with engine.connect() as conn:
        assert repository.get_state(conn, "p:read:NVDA") == NOW.isoformat()
    # Stamped: within the gap it is not picked again, and nothing else is a candidate.
    assert pick_symbols(engine, "p:read:", ["NVDA"], 3, NOW, timedelta(hours=1)) == []
    assert pick_symbols(engine, "p:read:", ["NVDA"], 0, NOW, timedelta(hours=1)) == []


def test_pick_symbols_adds_the_symbols_trending_everywhere(engine):
    u = make_universe("TSLA")
    row = {"platform": "reddit", "external_id": "p", "channel": "c", "created_at": NOW}
    with engine.begin() as conn:
        repository.record_mentions(conn, row, [Mention("TSLA", BARE, True)], u, NOW)
    assert pick_symbols(engine, "q:", [], 5, NOW, timedelta(hours=1)) == ["TSLA"]


class Recorder:
    def __init__(self) -> None:
        self.batches: list[list] = []

    def ingest(self, posts):
        self.batches.append(list(posts))
        return IngestStats(seen=len(self.batches[-1]), stored=len(self.batches[-1]))


def crawler(browser, engine, clock, mem=None, deadline=None) -> FeedCrawler:
    policy = Policy("test", 0, 0, 100, 1000, deadline=deadline)
    throttle = Throttle(policy, mem or MemoryThrottleStore(), clock=clock, sleep=lambda s: None)
    c = FeedCrawler(browser, throttle, Recorder(), engine, clock)
    c.platform = "test"
    return c


def loader(c: FeedCrawler, url: str, expect: str = JSON):
    def step() -> None:
        c.fetch(url, "k", expect)

    return step


def test_run_steps_errors_and_goes_on(engine, clock):
    browser = FakeBrowser(
        {"https://a": PageResult(500, "a"), "https://b": PageResult(200, "b", data=[])}
    )
    c = crawler(browser, engine, clock)

    def bad() -> None:
        raise ValueError("no listing")

    stats = c.run_steps([("a", loader(c, "https://a")), ("p", bad), ("b", loader(c, "https://b"))])
    assert browser.calls == ["https://a", "https://b"] and stats.requests == 2
    assert stats.errors[0].startswith("a: error") and "unparseable" in stats.errors[1]


@pytest.mark.parametrize(("status", "where"), [(429, "errors"), (403, "notices")])
def test_run_steps_stops_at_a_rate_limit_or_challenge(engine, clock, status, where):
    browser = FakeBrowser(
        {"https://a": PageResult(status, "a"), "https://b": PageResult(200, "b", data=[])}
    )
    c = crawler(browser, engine, clock)
    stats = c.run_steps([("a", loader(c, "https://a")), ("b", loader(c, "https://b"))])
    assert browser.calls == ["https://a"] and len(getattr(stats, where)) == 1
    assert len(stats.errors) + len(stats.notices) == 1


def test_a_challenge_again_after_the_cooldown_is_an_error(engine, clock):
    browser = FakeBrowser({"https://a": PageResult(403, "a")})
    c = crawler(browser, engine, clock)
    c.throttle.store.set_block(c.platform, Block(None, "challenged: HTTP 403", 1), NOW)
    stats = c.run_steps([("a", loader(c, "https://a"))])
    assert stats.notices == [] and "challenged" in stats.errors[0]


def test_a_timeout_is_a_notice_until_the_platform_keeps_failing(engine, clock):
    browser = FakeBrowser()
    browser.default = PageResult(None, "u", timed_out=True)
    c = crawler(browser, engine, clock)
    stats = c.run_steps([(s, loader(c, f"https://{s}")) for s in "abcd"])
    # Two notices, then the third failure in a row makes the platform failing: an error,
    # and the run stops there.
    assert len(stats.notices) == 2 and len(stats.errors) == 1 and stats.requests == 3


def test_the_deadline_ends_the_run_as_a_notice(engine):
    clock = Clock(NOW)
    browser = FakeBrowser({"https://a": PageResult(200, "a", data=[])})
    c = crawler(browser, engine, clock, deadline=NOW - timedelta(seconds=1))
    stats = c.run_steps([("a", loader(c, "https://a"))])
    assert browser.calls == [] and "out of cycle time" in stats.notices[0]


def test_fetch_raises_with_the_url_and_reads_html(engine, clock):
    browser = FakeBrowser({"https://h": PageResult(200, "h", html="<p>x</p>")})
    c = crawler(browser, engine, clock)
    assert c.fetch("https://h", "k", HTML).html == "<p>x</p>"
    with pytest.raises(FetchFailed, match="<- https://h"):
        c.fetch("https://h", "k", JSON)


def test_ingest_and_state_helpers(engine, clock):
    c = crawler(FakeBrowser(), engine, clock)
    assert c.ingest(["a", "b"]).stored == 2 and c.stats.ingest.stored == 2
    assert c.get_state("k") is None
    c.set_state("k", "v")
    assert c.get_state("k") == "v"


def test_wall_markers_are_lower_case():
    assert all(m == m.lower() for m in feeds.WALL_MARKERS)
