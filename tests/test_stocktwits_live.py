"""The live StockTwits crawl (`stocktwits/parse.py`, `stocktwits/scraper.py`).

Payload builders mirror the shapes a signed-out Chrome loaded on 2026-10-10 (stream and
trending JSON), with invented content: no captured message or username is committed.
"""

from datetime import timedelta

import pytest
from conftest import NOW, Clock, FakeBrowser, IsolatedSettings
from sqlalchemy import select

from social_scraper.db import repository, schema
from social_scraper.feeds import FeedRunStats
from social_scraper.fetch import PageResult
from social_scraper.models import STOCKTWITS, stable_hash
from social_scraper.pipeline import Ingestor
from social_scraper.stocktwits import scraper as st
from social_scraper.stocktwits.parse import parse_message, parse_stream, parse_trending
from social_scraper.throttle import MemoryThrottleStore, Policy, Throttle

STREAM = "https://api.stocktwits.com/api/2/streams/symbol/"


def message(msg_id: int, body: str = "$AAPL looking strong", **over):
    data = {
        "id": msg_id,
        "body": body,
        "created_at": "2026-09-28T14:30:00Z",
        "discussion": False,
        "symbols": [{"id": 686, "symbol": "AAPL", "title": "Apple Inc"}],
        "entities": {"sentiment": {"basic": "Bullish"}},
        "user": {
            "id": 1234567,
            "username": "invented_user",
            "followers": 538,
            "official": False,
            "join_date": "2019-01-31",
        },
        "mentioned_users": [],
    }
    data.update(over)
    return data


def stream(*messages, symbol="AAPL"):
    return {
        "symbol": {"id": 686, "symbol": symbol},
        "cursor": {"more": True, "since": 2, "max": 1},
        "messages": list(messages),
        "response": {"status": 200},
    }


def trending(*symbols):
    return {
        "response": {"status": 200},
        "symbols": [{"id": i, "symbol": s} for i, s in enumerate(symbols)],
    }


def ok(data, url="https://api.stocktwits.com/x.json"):
    return PageResult(200, url, data=data)


def settings(**over):
    base = {"watchlist": "AAPL", "stocktwits_symbols_per_cycle": 1}
    return IsolatedSettings(**{**base, **over})


def make(engine, universe, browser, clock=None, **over):
    clock = clock or Clock()
    store = MemoryThrottleStore()
    throttle = Throttle(
        Policy(STOCKTWITS, 0, 0, 1000, 1000), store, clock=clock, sleep=lambda s: None
    )
    ingestor = Ingestor(engine, universe, clock)
    return st.StockTwitsScraper(browser, throttle, ingestor, engine, settings(**over), clock), store


def stored(engine):
    with engine.connect() as conn:
        return {r.external_id: r for r in conn.execute(select(schema.posts))}


# --- parse ------------------------------------------------------------------------------


def test_parse_message_maps_every_field():
    post = parse_message(
        message(
            11,
            body="$AAPL &amp; $NVDA calls",
            conversation={
                "parent_message_id": 9,
                "in_reply_to_message_id": 10,
                "parent": False,
                "replies": 3,
            },
            likes={"total": 4, "user_ids": [1]},
            reshares={"reshared_count": 2, "user_ids": [1]},
        ),
        "AAPL",
    )
    assert post is not None
    assert post.platform == STOCKTWITS and post.external_id == "11"
    assert post.kind == "reply" and post.parent_id == "10"
    assert post.channel == "symbol:AAPL"
    assert post.created_at == NOW.replace(minute=30, hour=14)
    assert post.body == "$AAPL & $NVDA calls"
    assert post.url == "https://stocktwits.com/message/11"
    assert post.author.name is None and post.author.hash == stable_hash("1234567")
    assert post.author.followers == 538 and post.author.verified is False
    assert post.tagged_symbols == ("AAPL",)
    assert post.raw == {"conversation_id": "9", "sentiment": "bullish"}
    e = post.engagement
    assert (e.score, e.likes, e.reposts, e.comments) == (4, 4, 2, 3)


def test_parse_message_without_thread_sentiment_or_counts():
    post = parse_message(
        message(12, entities={"sentiment": None}, likes=None, user=None, symbols=None), "AAPL"
    )
    assert post is not None
    assert post.kind == "message" and post.parent_id is None and post.raw == {}
    assert post.author.user_id is None and post.tagged_symbols == ()
    assert post.engagement.likes is None and post.engagement.comments is None


@pytest.mark.parametrize(
    "bad",
    [
        message(13, reshare_message={"message": {"id": 1}}),
        message(14, body="   "),
        message(15, created_at="not a date"),
        {"body": "no id", "created_at": "2026-09-28T14:30:00Z"},
        "not a dict",
    ],
)
def test_parse_message_skips_reshares_and_broken_messages(bad):
    assert parse_message(bad, "AAPL") is None


def test_parse_stream_keeps_messages_and_rejects_what_is_not_a_stream():
    assert [p.external_id for p in parse_stream(stream(message(1), message(2)), "AAPL")] == [
        "1",
        "2",
    ]
    with pytest.raises(ValueError):
        parse_stream({"errors": [{"message": "nope"}]}, "AAPL")
    with pytest.raises(ValueError):
        parse_stream(None, "AAPL")


def test_parse_trending_drops_crypto():
    assert parse_trending(trending("PLTR", "AERO.X", "BRK.B", "")) == ["PLTR", "BRK.B"]
    with pytest.raises(ValueError):
        parse_trending({"messages": []})


def test_stream_symbol_is_stocktwits_spelling():
    assert st.stream_symbol("BRK-B") == "BRK.B"
    assert st.stream_symbol("AAPL") == "AAPL"


def test_stream_url_writes_share_classes_with_a_dot():
    assert st.stream_url("BRK-B") == STREAM + "BRK.B.json"
    assert st.stream_url("AAPL", 42) == STREAM + "AAPL.json?since=42"


# --- crawl ------------------------------------------------------------------------------


def browser_with(stream_data, trending_data=None):
    return FakeBrowser(
        {
            STREAM: ok(stream_data),
            st.TRENDING_URL: ok(trending_data if trending_data is not None else trending()),
        }
    )


def test_crawl_reads_trending_and_the_watchlist_stream(engine, universe):
    browser = browser_with(stream(message(5), message(7, body="$AAPL puts")))
    scraper, store = make(engine, universe, browser)
    stats = scraper.run()
    assert isinstance(stats, FeedRunStats) and not stats.errors
    assert browser.calls == [st.TRENDING_URL, STREAM + "AAPL.json"]
    assert stats.requests == 2 and stats.ingest.stored == 2
    rows = stored(engine)
    assert set(rows) == {"5", "7"} and rows["5"].symbols == ["AAPL"]
    assert rows["5"].raw == {"sentiment": "bullish"}
    with engine.connect() as conn:
        assert repository.get_state(conn, st.SINCE_PREFIX + "AAPL") == "7"
    assert len(store.requests) == 2


def test_second_visit_reads_since_the_cursor(engine, universe):
    clock = Clock()
    browser = browser_with(stream(message(5)))
    make(engine, universe, browser, clock)[0].run()
    clock.now = NOW + timedelta(hours=2)
    browser.calls.clear()
    make(engine, universe, browser, clock)[0].run()
    assert browser.calls == [STREAM + "AAPL.json?since=5"]  # trending not yet due


def test_a_symbol_is_not_read_again_within_the_hour(engine, universe):
    clock = Clock()
    browser = browser_with(stream(message(5)))
    make(engine, universe, browser, clock)[0].run()
    clock.now = NOW + timedelta(minutes=30)
    browser.calls.clear()
    stats = make(engine, universe, browser, clock)[0].run()
    assert browser.calls == [] and stats.requests == 0


def test_trending_refresh_feeds_the_next_cycles_picks(engine, universe):
    clock = Clock()
    browser = browser_with(stream(message(5)), trending("NVDA", "BTC.X", "ZZZZ"))
    make(engine, universe, browser, clock, watchlist="")[0].run()
    with engine.connect() as conn:
        assert repository.get_state(conn, st.TRENDING_KEY) == "NVDA"  # crypto, unlisted dropped
    clock.now = NOW + timedelta(hours=4)
    browser.calls.clear()
    make(engine, universe, browser, clock, watchlist="")[0].run()
    assert browser.calls == [st.TRENDING_URL, STREAM + "NVDA.json"]


def test_trending_is_not_reread_before_it_is_due(engine, universe):
    clock = Clock()
    browser = browser_with(stream(message(5)))
    scraper, _ = make(engine, universe, browser, clock)
    scraper.read_trending()
    assert not scraper.trending_due(NOW + timedelta(hours=1))
    assert scraper.trending_due(NOW + timedelta(hours=3))


def test_an_unknown_symbol_is_skipped_quietly(engine, universe):
    browser = FakeBrowser(
        {STREAM: PageResult(404, STREAM + "AAPL.json"), st.TRENDING_URL: ok(trending())}
    )
    scraper, _ = make(engine, universe, browser)
    stats = scraper.run()
    assert not stats.errors and stats.ingest.stored == 0
    with engine.connect() as conn:
        assert repository.get_state(conn, st.SINCE_PREFIX + "AAPL") is None


def test_a_reshare_is_not_stored(engine, universe):
    browser = browser_with(stream(message(5), message(6, reshare_message={"message": {"id": 1}})))
    make(engine, universe, browser)[0].run()
    assert set(stored(engine)) == {"5"}


def test_a_page_that_is_not_a_stream_is_an_error_and_the_run_goes_on(engine, universe):
    browser = browser_with({"unexpected": True})
    stats = make(engine, universe, browser, stocktwits_symbols_per_cycle=2, watchlist="AAPL,NVDA")[
        0
    ].run()
    assert len(stats.errors) == 2 and all("unparseable" in e for e in stats.errors)
    assert len(browser.calls) == 3  # trending, then both symbols


@pytest.mark.parametrize(
    "wall",
    [
        PageResult(429, STREAM),
        PageResult(403, STREAM, text="Forbidden"),
        PageResult(200, STREAM, text="Just a moment... Verify you are human"),
    ],
)
def test_a_wall_or_rate_limit_stops_the_run(engine, universe, wall):
    browser = FakeBrowser({STREAM: wall, st.TRENDING_URL: ok(trending())})
    scraper, store = make(
        engine, universe, browser, stocktwits_symbols_per_cycle=2, watchlist="AAPL,NVDA"
    )
    stats = scraper.run()
    assert len(stats.errors) == 1
    assert len(browser.calls) == 2  # the second symbol never loaded
    assert store.get_block(STOCKTWITS).blocked_until is not None


def test_crawl_is_the_runner_entry_point(engine, universe):
    browser = browser_with(stream(message(5)))
    throttle = Throttle(
        Policy(STOCKTWITS, 0, 0, 1000, 1000), MemoryThrottleStore(), sleep=lambda s: None
    )
    stats = st.crawl(settings(), engine, Ingestor(engine, universe), browser, throttle)
    assert stats.ingest.stored == 1
