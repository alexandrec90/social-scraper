"""TradingView ideas: parser and crawler.

Payload builders mirror the `component-data-only` shape captured live on 2026-10-10,
with invented content.
"""

from datetime import timedelta
from typing import Any

import pytest
from conftest import NOW, Clock, FakeBrowser, IsolatedSettings

from social_scraper.db import repository, schema
from social_scraper.fetch import PageResult
from social_scraper.models import TRADINGVIEW
from social_scraper.pipeline import Ingestor
from social_scraper.throttle import (
    CHALLENGED,
    RATE_LIMITED,
    MemoryThrottleStore,
    Policy,
    Throttle,
)
from social_scraper.tradingview import scraper as tv
from social_scraper.tradingview.parse import ideas_page, parse_idea, parse_ideas

AAPL_URL = tv.ideas_url("AAPL")


def tv_idea(idea_id: int, created: str = "2026-09-28T14:00:00+00:00", **over: Any) -> dict:
    symbol = {
        "name": "NASDAQ:AAPL",
        "full_name": "NASDAQ:AAPL",
        "short_name": "AAPL",
        "exchange": "NASDAQ",
        "type": "stock",
        "interval": "240",
        "direction": 1,
        **over.pop("symbol", {}),
    }
    return {
        "id": idea_id,
        "name": f"Idea {idea_id}: breakout above resistance",
        "description": "Holding support, looking for a push to new highs.",
        "created_at": created,
        "date_timestamp": 1790604000,
        "chart_url": f"https://www.tradingview.com/chart/AAPL/abc{idea_id}-Idea/",
        "is_public": True,
        "is_visible": True,
        "is_video": False,
        "comments_count": 3,
        "views_count": 120,
        "likes_count": 9,
        "symbol": symbol,
        "user": {"id": 4242, "username": "someone"},
        **over,
    }


def tv_payload(*items: dict, next_page: str | None = None) -> dict:
    return {
        "context": {},
        "data": {
            "ideas": {
                "data": {"total": len(items), "items": list(items), "next": next_page},
                "params": {"sort": "recent"},
            }
        },
    }


def ok(data: Any) -> PageResult:
    return PageResult(200, "https://www.tradingview.com/symbols/NASDAQ-AAPL/ideas/", data=data)


def make(engine, universe, browser, clock=None):
    clock = clock or Clock()
    store = MemoryThrottleStore()
    throttle = Throttle(
        Policy(TRADINGVIEW, 0, 0, 1000, 1000), store, clock=clock, sleep=lambda s: None
    )
    ingestor = Ingestor(engine, universe, clock)
    return tv.TradingViewScraper(browser, throttle, ingestor, engine, clock), store, throttle


def stored(engine) -> dict[str, dict]:
    with engine.connect() as conn:
        rows = conn.execute(schema.posts.select()).mappings().all()
    return {r["external_id"]: dict(r) for r in rows}


# --- parser ---------------------------------------------------------------------------


def test_parse_idea_maps_every_field():
    post = parse_idea(tv_idea(7))
    assert post is not None
    assert post.platform == TRADINGVIEW and post.external_id == "7" and post.kind == "idea"
    assert post.channel == "symbol:AAPL"
    assert post.title.startswith("Idea 7") and "support" in post.body
    assert post.url.endswith("abc7-Idea/")
    assert post.created_at.isoformat() == "2026-09-28T14:00:00+00:00"
    assert post.author.name is None and post.author.user_id == "4242"
    e = post.engagement
    assert (e.score, e.likes, e.comments, e.views) == (9, 9, 3, 120)
    assert post.tagged_symbols == ("AAPL",)
    assert post.raw == {"direction": "long", "interval": "240"}


@pytest.mark.parametrize(("direction", "expected"), [(1, "long"), (2, "short"), (0, None)])
def test_direction_mapping(direction, expected):
    post = parse_idea(tv_idea(1, symbol={"direction": direction}))
    assert post.raw.get("direction") == expected


def test_created_at_falls_back_to_the_timestamp():
    post = parse_idea(tv_idea(1, created="not a date"))
    assert post.created_at.timestamp() == 1790604000


def test_non_equity_symbol_is_not_tagged():
    post = parse_idea(tv_idea(1, symbol={"type": "crypto", "short_name": "BTCUSD"}))
    assert post.tagged_symbols == () and post.channel == "symbol:BTCUSD"


@pytest.mark.parametrize("flag", ["is_public", "is_visible"])
def test_private_or_hidden_idea_is_skipped(flag):
    assert parse_idea(tv_idea(1, **{flag: False})) is None
    assert [
        p.external_id for p in parse_ideas(tv_payload(tv_idea(1, **{flag: False}), tv_idea(2)))
    ] == ["2"]


def test_idea_without_text_or_date_is_skipped():
    assert parse_idea(tv_idea(1, name=None, description=None)) is None
    assert parse_idea(tv_idea(1, created=None, date_timestamp=None)) is None


@pytest.mark.parametrize("payload", [None, {}, {"data": {}}, {"data": {"ideas": {"data": {}}}}, []])
def test_non_ideas_payload_raises(payload):
    with pytest.raises(ValueError):
        parse_ideas(payload)


# --- crawler --------------------------------------------------------------------------


def test_ideas_page_is_the_ideas_block_or_a_value_error():
    page = ideas_page({"data": {"ideas": {"data": {"items": [], "next": None, "total": 0}}}})
    assert page["items"] == [] and page["total"] == 0
    for payload in (None, {"data": {}}, {"data": {"ideas": {"data": {"items": "x"}}}}):
        with pytest.raises(ValueError):
            ideas_page(payload)


def test_ideas_url_uses_tradingviews_share_class_spelling():
    assert tv.ideas_url("BRK-B") == (
        "https://www.tradingview.com/symbols/BRK.B/ideas/?component-data-only=1&sort=recent"
    )
    assert tv.tv_symbol("aapl") == "AAPL"


def test_read_symbol_stores_ideas_and_sets_the_cursor(engine, universe):
    browser = FakeBrowser(
        {AAPL_URL: ok(tv_payload(tv_idea(1), tv_idea(2, "2026-09-28T15:00:00+00:00")))}
    )
    crawler, store, _ = make(engine, universe, browser)
    stats = crawler.run(["AAPL"])
    assert not stats.errors and stats.requests == 1
    rows = stored(engine)
    assert set(rows) == {"1", "2"}
    assert rows["1"]["symbols"] == ["AAPL"] and rows["1"]["raw"] == {
        "direction": "long",
        "interval": "240",
    }
    with engine.connect() as conn:
        assert repository.get_state(conn, "tradingview:newest:AAPL") == "2026-09-28T15:00:00+00:00"
    assert store.requests[0][2] == "ideas"


def test_second_visit_stores_only_newer_ideas(engine, universe):
    browser = FakeBrowser({AAPL_URL: ok(tv_payload(tv_idea(1)))})
    crawler, _, _ = make(engine, universe, browser)
    crawler.run(["AAPL"])
    browser.pages[AAPL_URL] = ok(
        tv_payload(
            tv_idea(3, "2026-09-28T16:00:00+00:00"),
            tv_idea(1),
            tv_idea(0, "2026-09-27T10:00:00+00:00"),
        )
    )
    crawler2, _, _ = make(engine, universe, browser)
    stats = crawler2.run(["AAPL"])
    assert stats.ingest.seen == 1
    assert set(stored(engine)) == {"1", "3"}


def test_unknown_symbol_404_is_skipped_quietly(engine, universe):
    browser = FakeBrowser({AAPL_URL: PageResult(404, AAPL_URL)})
    crawler, _, _ = make(engine, universe, browser)
    stats = crawler.run(["AAPL"])
    assert not stats.errors and stats.requests == 1 and not stored(engine)


def test_non_ideas_payload_is_an_error_and_the_run_goes_on(engine, universe):
    nvda = tv.ideas_url("NVDA")
    browser = FakeBrowser({AAPL_URL: ok({"seo": {}}), nvda: ok(tv_payload(tv_idea(5)))})
    crawler, _, _ = make(engine, universe, browser)
    stats = crawler.run(["AAPL", "NVDA"])
    assert len(stats.errors) == 1 and "unparseable" in stats.errors[0]
    assert set(stored(engine)) == {"5"}


@pytest.mark.parametrize(
    ("result", "outcome", "where"),
    [
        (PageResult(429, "u"), RATE_LIMITED, "errors"),
        # A first challenge is a notice: the cooldown is the response (`Throttle.notice`).
        (PageResult(403, "u"), CHALLENGED, "notices"),
        (
            PageResult(200, "u", text="Just a moment... Verify you are human"),
            CHALLENGED,
            "notices",
        ),
    ],
)
def test_wall_or_rate_limit_ends_the_run(engine, universe, result, outcome, where):
    nvda = tv.ideas_url("NVDA")
    browser = FakeBrowser({AAPL_URL: result, nvda: ok(tv_payload(tv_idea(5)))})
    crawler, store, _ = make(engine, universe, browser)
    stats = crawler.run(["AAPL", "NVDA"])
    assert browser.calls == [AAPL_URL]
    assert getattr(stats, where) and not stored(engine)
    block = store.get_block(TRADINGVIEW)
    assert block.blocked_until is not None and block.reason.startswith(outcome)


def test_crawl_picks_watchlist_symbols_and_paces_them(engine, universe):
    clock = Clock()
    settings = IsolatedSettings(watchlist="AAPL,NVDA", tradingview_symbols_per_cycle=1)
    browser = FakeBrowser({AAPL_URL: ok(tv_payload(tv_idea(1)))})
    throttle = Throttle(
        Policy(TRADINGVIEW, 0, 0, 1000, 1000),
        MemoryThrottleStore(),
        clock=clock,
        sleep=lambda s: None,
    )
    ingestor = Ingestor(engine, universe, clock)
    stats = tv.crawl(settings, engine, ingestor, browser, throttle, clock)
    assert browser.calls == [AAPL_URL] and stats.ingest.stored == 1
    browser.pages[tv.ideas_url("NVDA")] = ok(tv_payload())
    tv.crawl(settings, engine, ingestor, browser, throttle, clock)
    assert browser.calls[-1] == tv.ideas_url("NVDA")
    clock.now = NOW + tv.MIN_GAP + timedelta(minutes=1)
    tv.crawl(settings, engine, ingestor, browser, throttle, clock)
    assert browser.calls[-1] == AAPL_URL
