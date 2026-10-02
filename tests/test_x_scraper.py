from datetime import timedelta

import pytest
from conftest import NOW, Clock, FakeBrowser, x_search_payload, x_tweet_classic, x_user_classic
from sqlalchemy import select

from social_scraper.db import repository, schema
from social_scraper.fetch import FetchFailed, PageResult, Payload
from social_scraper.pipeline import Ingestor
from social_scraper.throttle import (
    CHALLENGED,
    ERROR,
    LOGIN_REQUIRED,
    OK,
    RATE_LIMITED,
    MemoryThrottleStore,
    Policy,
    Throttle,
)
from social_scraper.x import accounts
from social_scraper.x import scraper as scraper_mod
from social_scraper.x.discovery import SEED, TRACKED
from social_scraper.x.scraper import (
    QUERY_INDEX_KEY,
    SEARCHED_PREFIX,
    XPlan,
    XScraper,
    classify,
    graphql_filter,
    search_url,
)

GQL = "https://x.com/i/api/graphql/abc/SearchTimeline?variables=1"
ALICE = x_user_classic("100", "alice", followers=20_000)
BOB = x_user_classic("200", "bob", followers=3_000)


def page(*tweets, status=200, url="https://x.com/search"):
    return PageResult(200, url, payloads=[Payload(GQL, status, x_search_payload(*tweets))])


def scraper(engine, universe, browser, clock=None, **plan):
    clock = clock or Clock()
    throttle = Throttle(
        Policy("x", 0, 0, 1000, 1000), MemoryThrottleStore(), clock=clock, sleep=lambda s: None
    )
    defaults = {"queries": ("generic one", "generic two"), "pages": 4, "scrolls": 2}
    return XScraper(
        browser,
        throttle,
        Ingestor(engine, universe, clock),
        engine,
        XPlan(**{**defaults, **plan}),
        clock,
    )


def test_graphql_filter_and_search_url():
    want = graphql_filter(("SearchTimeline",))
    assert want(GQL)
    assert not want("https://x.com/i/api/graphql/abc/UserByScreenName?x")
    assert not want("https://x.com/SearchTimeline")
    assert search_url("$NVDA lang:en", True) == (
        "https://x.com/search?q=%24NVDA%20lang%3Aen&src=typed_query&f=live"
    )
    assert search_url("x", False).endswith("f=top")


@pytest.mark.parametrize(
    ("result", "outcome"),
    [
        (PageResult(200, "https://x.com/i/flow/login?redirect=1"), LOGIN_REQUIRED),
        # Where a logged-out search actually lands (captured 2026-09-28).
        (
            PageResult(
                200,
                "https://x.com/i/jf/onboarding/web?redirect_after_login=%2Fsearch&mode=login",
            ),
            LOGIN_REQUIRED,
        ),
        (PageResult(200, "https://x.com/account/access"), CHALLENGED),
        (PageResult(200, "https://x.com/s", payloads=[Payload(GQL, 429, {})]), RATE_LIMITED),
        (
            PageResult(200, "https://x.com/s", text="Something went wrong. Try reloading."),
            RATE_LIMITED,
        ),
        (PageResult(200, "https://x.com/s"), ERROR),
        (PageResult(200, "https://x.com/s", payloads=[Payload(GQL, 200, {})]), OK),
    ],
)
def test_classify(result, outcome):
    assert classify(result)[0] == outcome


def test_first_cycle_is_all_discovery(engine, universe):
    browser = FakeBrowser()
    browser.default = page(
        x_tweet_classic("1", "long $NVDA", ALICE, ("NVDA",)),
        x_tweet_classic("2", "$NVDA puts", BOB, ("NVDA",)),
    )
    s = scraper(engine, universe, browser, watchlist=("NVDA", "AMD"))
    stats = s.run()
    assert stats.searches == 4 and stats.timelines == 0
    assert browser.calls[0].startswith("https://x.com/search?q=%24NVDA")
    assert sum("generic" in c for c in browser.calls) == 2
    # The same two tweets each search: upserted each time, mentioned and snapshotted once.
    assert (stats.ingest.stored, stats.ingest.new_mentions, stats.ingest.snapshots) == (8, 2, 2)
    with engine.connect() as conn:
        rows = {r.user_id: r for r in accounts.load(conn)}
        assert rows["100"].search_hits == 4
        assert repository.get_state(conn, QUERY_INDEX_KEY) == "2"
        assert SEARCHED_PREFIX + "NVDA" in {r.key for r in conn.execute(select(schema.crawl_state))}


def test_accounts_seen_repeatedly_get_promoted(engine, universe):
    browser = FakeBrowser()
    browser.default = page(x_tweet_classic("1", "$NVDA", ALICE, ("NVDA",)))
    scraper(engine, universe, browser, max_tracked=5, min_followers=500).run()
    with engine.connect() as conn:
        assert {r.user_id: r.status for r in accounts.load(conn)}["100"] == TRACKED


def test_timeline_visit_keeps_finance_tweets_and_learns_amplified(engine, universe):
    with engine.begin() as conn:
        accounts.add_seeds(conn, ["alice"], NOW)
    rt_original = x_tweet_classic("9", "$TSLA squeeze incoming", BOB, ("TSLA",))
    timeline = page(
        x_tweet_classic("1", "coffee time", ALICE),
        x_tweet_classic("2", "$NVDA earnings beat", ALICE, ("NVDA",)),
        x_tweet_classic("3", "RT", ALICE, retweeted_status_result={"result": rt_original}),
        url="https://x.com/alice",
    )
    browser = FakeBrowser({"https://x.com/alice": timeline})
    browser.default = page()
    s = scraper(engine, universe, browser, pages=2, search_share=0.5, queries=())
    stats = s.run()
    assert stats.timelines == 1
    with engine.connect() as conn:
        stored = {r.external_id for r in conn.execute(select(schema.posts))}
        rows = {r.user_id: r for r in accounts.load(conn)}
    assert stored == {"2", "9"}  # coffee is not stored
    assert rows["100"].status == SEED  # placeholder resolved to the real id
    assert (rows["100"].timeline_tweets, rows["100"].timeline_finance_tweets) == (2, 1)
    assert rows["200"].mentioned_by_tracked == 1


def test_login_wall_on_search_still_visits_timelines(engine, universe):
    with engine.begin() as conn:
        accounts.add_seeds(conn, ["alice"], NOW)
    login = PageResult(200, "https://x.com/i/flow/login")
    browser = FakeBrowser(
        {
            "https://x.com/search": login,
            "https://x.com/alice": page(
                x_tweet_classic("2", "$NVDA", ALICE, ("NVDA",)), url="https://x.com/alice"
            ),
        }
    )
    stats = scraper(engine, universe, browser, pages=4, search_share=0.5, watchlist=("NVDA",)).run()
    assert sum(c.startswith("https://x.com/search") for c in browser.calls) == 1
    assert stats.timelines == 1
    # Logged-out is a supported mode: a notice, not an error that fails the cycle.
    assert stats.errors == []
    assert any("not logged in" in n for n in stats.notices)
    # The remedy must run from a bare shell: the system Python has none of the deps.
    assert any("`uv run social-scraper login x`" in n for n in stats.notices)


def test_challenge_ends_the_cycle(engine, universe):
    browser = FakeBrowser()
    browser.default = PageResult(200, "https://x.com/account/access")
    stats = scraper(engine, universe, browser, pages=4).run()
    assert len(browser.calls) == 1
    assert stats.errors


def test_plain_error_moves_on_to_the_next_page(engine, universe):
    browser = FakeBrowser()
    browser.default = PageResult(200, "https://x.com/search")  # no payloads
    stats = scraper(engine, universe, browser, pages=3).run()
    assert len(browser.calls) == 3 and len(stats.errors) == 3


def test_queries_rotate_generic_and_respect_symbol_gap(engine, universe):
    clock = Clock()
    s = scraper(engine, universe, FakeBrowser(), clock, watchlist=("NVDA",))
    first = s.queries(3)
    assert first[0] == ("$NVDA lang:en", True)
    clock.now = NOW + timedelta(hours=1)
    second = s.queries(2)
    assert all(not latest for _, latest in second)  # NVDA searched an hour ago: generic only
    assert s.queries(0) == []


def test_fetch_scrolls_with_dwell(engine, universe):
    browser = FakeBrowser()
    browser.default = page()
    s = scraper(engine, universe, browser, scrolls=3)
    result = s.fetch("https://x.com/search?q=a", ("SearchTimeline",), "search")
    assert result.payloads and s.stats.requests == 1


def test_fetch_raises_on_login_wall(engine, universe):
    browser = FakeBrowser()
    browser.default = PageResult(200, "https://x.com/i/flow/login")
    with pytest.raises(FetchFailed) as exc:
        scraper(engine, universe, browser).fetch("https://x.com/a", ("UserTweets",), "timeline")
    assert exc.value.outcome == LOGIN_REQUIRED


def test_reconcile_persists_statuses(engine, universe):
    with engine.begin() as conn:
        accounts.observe(conn, [], accounts.SEARCH, NOW)
    scraper(engine, universe, FakeBrowser()).reconcile()


def test_run_stats_start_empty():
    stats = scraper_mod.XRunStats()
    assert stats.requests == 0 and stats.errors == [] and stats.ingest.stored == 0
