from dataclasses import replace
from datetime import timedelta

import pytest
from conftest import NOW, Clock, FakeBrowser, x_search_payload, x_tweet_classic, x_user_classic
from sqlalchemy import select

from social_scraper.db import repository, schema
from social_scraper.fetch import FetchFailed, PageResult, Payload
from social_scraper.pipeline import Ingestor
from social_scraper.throttle import (
    CHALLENGED,
    ERROR_STREAK,
    LOGIN_REQUIRED,
    OK,
    RATE_LIMITED,
    TIMED_OUT,
    MemoryThrottleStore,
    Policy,
    Throttle,
)
from social_scraper.x import accounts
from social_scraper.x import scraper as scraper_mod
from social_scraper.x.discovery import DROPPED, SEED, TRACKED
from social_scraper.x.scraper import (
    EMPTY,
    QUERY_INDEX_KEY,
    SEARCHED_PREFIX,
    TIMELINE_OPS,
    UNAVAILABLE,
    XPlan,
    XScraper,
    classify,
    graphql_filter,
    search_url,
)

GQL = "https://x.com/i/api/graphql/abc/SearchTimeline?variables=1"
# The profile timeline's op since 2026-10-03; `UserTweets` no longer fires on a profile load.
TIMELINE_GQL = (
    "https://x.com/i/api/graphql/qtvmQffnepvr0oPe4A8MqQ/UserOriginalsTimeline?variables=1"
)
# The apostrophe X prints on a missing profile.
CURLY = chr(0x2019)
ALICE = x_user_classic("100", "alice", followers=20_000)
BOB = x_user_classic("200", "bob", followers=3_000)


def page(*tweets, status=200, url="https://x.com/search"):
    return PageResult(200, url, payloads=[Payload(GQL, status, x_search_payload(*tweets))])


def timeline_page(*tweets, url="https://x.com/alice"):
    """A profile load as X serves it logged in: the Posts tab's own GraphQL op."""
    return PageResult(200, url, payloads=[Payload(TIMELINE_GQL, 200, x_search_payload(*tweets))])


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


def test_timeline_filter_takes_the_profile_posts_op():
    want = graphql_filter(TIMELINE_OPS)
    assert want(TIMELINE_GQL)
    assert not want("https://x.com/i/api/graphql/abc/UserByScreenName?x")
    assert not want(GQL)


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
        (PageResult(200, "https://x.com/s"), EMPTY),
        (
            PageResult(200, "https://x.com/gone", text=f"This account doesn{CURLY}t exist"),
            UNAVAILABLE,
        ),
        (PageResult(200, "https://x.com/gone", text="Account suspended"), UNAVAILABLE),
        (PageResult(200, "https://x.com/shy", text="These posts are protected"), UNAVAILABLE),
        (PageResult(200, "https://x.com/s", payloads=[Payload(GQL, 200, {})]), OK),
    ],
)
def test_classify(result, outcome):
    assert classify(result)[0] == outcome


def tracked(engine, *handles):
    """Tracked accounts, never visited: each cycle's timelines go to them first."""
    with engine.begin() as conn:
        accounts.add_seeds(conn, handles, NOW)
        accounts.apply(conn, {f"seed:{h}": (TRACKED, 1.0) for h in handles})


def rows_by_handle(engine):
    with engine.connect() as conn:
        return {r.handle: r for r in accounts.load(conn)}


def two_timelines(engine, universe, browser, clock=None):
    """A cycle of two profile visits and no search (nothing to search for)."""
    return scraper(engine, universe, browser, clock, pages=3, search_share=0.3, queries=())


def test_one_empty_profile_among_loaded_ones_is_the_account_not_a_failure(engine, universe):
    """2026-10-06: one tracked profile came back empty, the other read fine, and the
    whole scrape failed -- and, the visit unrecorded, it was first in line again."""
    tracked(engine, "alice", "dead")
    browser = FakeBrowser(
        {
            "https://x.com/alice": timeline_page(x_tweet_classic("2", "$NVDA", ALICE, ("NVDA",))),
            "https://x.com/dead": PageResult(200, "https://x.com/dead"),
        }
    )
    clock = Clock()
    stats = two_timelines(engine, universe, browser, clock).run()
    assert stats.errors == []
    assert any(n.startswith("@dead: no timeline data captured") for n in stats.notices)
    dead = rows_by_handle(engine)["dead"]
    assert dead.last_timeline_at is not None and dead.status == TRACKED

    clock.now = NOW + timedelta(days=2)
    stats = two_timelines(engine, universe, browser, clock).run()
    assert stats.errors == []
    assert any(n.startswith("@dead:") and n.endswith("-- dropped") for n in stats.notices)
    assert rows_by_handle(engine)["dead"].status == DROPPED


def test_a_profile_that_reads_resets_the_accounts_misses(engine, universe):
    tracked(engine, "alice", "flaky")
    flaky = PageResult(200, "https://x.com/flaky")
    alice = timeline_page(x_tweet_classic("2", "$NVDA", ALICE, ("NVDA",)))
    browser = FakeBrowser({"https://x.com/alice": alice, "https://x.com/flaky": flaky})
    clock = Clock()
    two_timelines(engine, universe, browser, clock).run()
    browser.pages["https://x.com/flaky"] = timeline_page(url="https://x.com/flaky")
    clock.now = NOW + timedelta(days=2)
    two_timelines(engine, universe, browser, clock).run()
    browser.pages["https://x.com/flaky"] = flaky
    clock.now = NOW + timedelta(days=4)
    stats = two_timelines(engine, universe, browser, clock).run()
    assert stats.errors == []
    assert rows_by_handle(engine)["flaky"].status == TRACKED  # one miss since it last read


def test_empty_profiles_in_a_row_are_x_layout_and_fail_the_cycle(engine, universe):
    """2026-10-03: a renamed op emptied every profile. That must still fail, and must
    not be held against the accounts."""
    tracked(engine, "carol", "dave")
    browser = FakeBrowser()
    browser.default = PageResult(200, "https://x.com/someone")
    stats = two_timelines(engine, universe, browser).run()
    assert len(stats.notices) == 1
    assert len(stats.errors) == 1 and "X's layout" in stats.errors[0]
    rows = rows_by_handle(engine)
    assert all(r.status == TRACKED for r in rows.values())
    # The second account's visit is not recorded: it is revisited once X is read again.
    assert sum(r.last_timeline_at is None for r in rows.values()) == 1


def test_the_layout_streak_spans_cycles(engine, universe):
    tracked(engine, "erin")
    browser = FakeBrowser()
    browser.default = PageResult(200, "https://x.com/erin")
    clock = Clock()

    def one_visit():
        return scraper(engine, universe, browser, clock, pages=2, search_share=0.3, queries=())

    assert one_visit().run().errors == []
    clock.now = NOW + timedelta(hours=1)
    assert "X's layout" in one_visit().run().errors[0]


def test_an_unavailable_account_is_dropped_at_once_and_is_no_strike(engine, universe):
    tracked(engine, "alice", "gone")
    browser = FakeBrowser(
        {
            "https://x.com/alice": timeline_page(x_tweet_classic("2", "$NVDA", ALICE, ("NVDA",))),
            "https://x.com/gone": PageResult(
                200, "https://x.com/gone", text=f"This account doesn{CURLY}t exist\nTry searching"
            ),
        }
    )
    s = two_timelines(engine, universe, browser)
    stats = s.run()
    assert stats.errors == []
    assert any("account unavailable" in n and n.endswith("-- dropped") for n in stats.notices)
    assert rows_by_handle(engine)["gone"].status == DROPPED
    assert s.throttle.store.get_block("x").consecutive_failures == 0


def test_an_unavailable_seed_is_kept_and_named(engine, universe):
    with engine.begin() as conn:
        accounts.add_seeds(conn, ["gone"], NOW)
    browser = FakeBrowser()
    browser.default = PageResult(200, "https://x.com/gone", text="Account suspended")
    stats = scraper(engine, universe, browser, pages=2, search_share=0.3, queries=()).run()
    assert stats.errors == []
    assert any("take it out of the seed list" in n for n in stats.notices)
    assert rows_by_handle(engine)["gone"].status == SEED


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


def test_timeline_visit_keeps_on_topic_tweets_and_learns_amplified(engine, universe):
    with engine.begin() as conn:
        accounts.add_seeds(conn, ["alice"], NOW)
    rt_original = x_tweet_classic("9", "$TSLA squeeze incoming", BOB, ("TSLA",))
    timeline = timeline_page(
        x_tweet_classic("1", "coffee time", ALICE),
        x_tweet_classic("2", "$NVDA earnings beat", ALICE, ("NVDA",)),
        x_tweet_classic("3", "RT", ALICE, retweeted_status_result={"result": rt_original}),
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
    assert (rows["100"].timeline_tweets, rows["100"].timeline_relevant_tweets) == (2, 1)
    assert rows["200"].mentioned_by_tracked == 1


def test_login_wall_on_search_still_visits_timelines(engine, universe):
    with engine.begin() as conn:
        accounts.add_seeds(conn, ["alice"], NOW)
    login = PageResult(200, "https://x.com/i/flow/login")
    browser = FakeBrowser(
        {
            "https://x.com/search": login,
            "https://x.com/alice": timeline_page(x_tweet_classic("2", "$NVDA", ALICE, ("NVDA",))),
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
    # The first empty search is a notice; the two after it are a streak, so X's layout.
    assert len(browser.calls) == 3 and len(stats.notices) == 1 and len(stats.errors) == 2


# `discovery.pick_symbols`' default gap: a symbol searched sooner is not searched again.
SYMBOL_GAP = timedelta(hours=6)


def one_search(engine, universe, browser, clock):
    """A cycle of one `$MSFT` search and nothing else."""
    return scraper(engine, universe, browser, clock, pages=1, watchlist=("MSFT",), queries=())


def test_one_empty_search_among_loaded_ones_is_a_notice_not_a_failure(engine, universe):
    """2026-10-07: one `$MSFT` search came back empty among some seventy that read, and
    failed the scheduled scrape."""
    clock = Clock()
    browser = FakeBrowser()
    browser.default = page(x_tweet_classic("1", "$MSFT", ALICE, ("MSFT",)))
    assert one_search(engine, universe, browser, clock).run().errors == []
    browser.default = PageResult(200, "https://x.com/search")
    clock.now = NOW + SYMBOL_GAP
    stats = one_search(engine, universe, browser, clock).run()
    assert stats.errors == []
    assert any(n.startswith("search: no timeline data captured") for n in stats.notices)


def test_empty_searches_in_a_row_are_x_layout_across_cycles(engine, universe):
    clock = Clock()
    browser = FakeBrowser()
    browser.default = PageResult(200, "https://x.com/search")
    assert one_search(engine, universe, browser, clock).run().errors == []
    clock.now = NOW + SYMBOL_GAP
    errors = one_search(engine, universe, browser, clock).run().errors
    assert len(errors) == 1 and errors[0].startswith("empty: 2 searches in a row")
    assert errors[0].endswith(
        "<- https://x.com/search?q=%24MSFT%20lang%3Aen&src=typed_query&f=live"
    )


def test_a_search_that_reads_ends_the_empty_streak(engine, universe):
    clock = Clock()
    empty = PageResult(200, "https://x.com/search")
    browser = FakeBrowser()
    for hour, result in enumerate((empty, page(), empty)):
        browser.default = result
        clock.now = NOW + hour * SYMBOL_GAP
        assert one_search(engine, universe, browser, clock).run().errors == []


def test_the_cycle_deadline_ends_the_crawl_with_a_notice(engine, universe):
    """No page load starts past the deadline, and what came before is still reported."""
    tracked(engine, "alice")
    clock = Clock()
    browser = FakeBrowser({"https://x.com/alice": PageResult(200, "https://x.com/alice")})
    browser.default = page(x_tweet_classic("1", "$MSFT", ALICE, ("MSFT",)))
    s = scraper(engine, universe, browser, clock, pages=2, search_share=0.5, watchlist=("MSFT",))
    gap = Policy("x", 120, 120, 1000, 1000, deadline=NOW + timedelta(seconds=60))
    s.throttle = Throttle(gap, MemoryThrottleStore(), clock=clock, sleep=lambda s: None)
    stats = s.run()
    assert stats.searches == 1 and stats.timelines == 0 and len(browser.calls) == 1
    assert stats.errors == []
    assert any("out of cycle time" in n for n in stats.notices)


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


def test_the_base_crawler_needs_a_domain(engine, universe):
    """`XCrawler` is the loop; a domain says what to search and what is on-topic."""
    base = scraper_mod.XCrawler.__new__(scraper_mod.XCrawler)
    with pytest.raises(NotImplementedError):
        base.queries(1)
    with pytest.raises(NotImplementedError):
        base.is_relevant(None)
    assert base.is_tout(None) is False
    stock = scraper(engine, universe, FakeBrowser())
    assert isinstance(stock, scraper_mod.XCrawler)


TIMED_OUT_PAGE = PageResult(None, "https://x.com/search", timed_out=True)


def test_a_timed_out_load_is_a_timeout_not_an_empty_page():
    """Empty would count against the account (`empty_timeline`), dropping it for X being slow."""
    assert classify(TIMED_OUT_PAGE) == (TIMED_OUT, "page load timed out")


def test_one_timed_out_search_is_a_notice_and_the_cycle_goes_on(engine, universe):
    with engine.begin() as conn:
        accounts.add_seeds(conn, ["alice"], NOW)
    browser = FakeBrowser(
        {
            "https://x.com/search": TIMED_OUT_PAGE,
            "https://x.com/alice": timeline_page(x_tweet_classic("2", "$NVDA", ALICE, ("NVDA",))),
        }
    )
    stats = scraper(engine, universe, browser, pages=2, search_share=0.5, watchlist=("NVDA",)).run()
    assert stats.errors == [] and stats.timelines == 1
    assert any("timed out" in n for n in stats.notices)


def test_timeouts_in_a_row_are_an_error_and_end_the_cycle(engine, universe):
    with engine.begin() as conn:
        accounts.add_seeds(conn, ["alice", "bob"], NOW)
    browser = FakeBrowser()
    browser.default = TIMED_OUT_PAGE
    s = scraper(engine, universe, browser, pages=4, search_share=0.5, watchlist=("NVDA",))
    stats = s.run()
    assert len(browser.calls) == ERROR_STREAK
    assert len(stats.errors) == 1 and "timed out" in stats.errors[0]
    assert len(stats.notices) == ERROR_STREAK - 1


RATE_LIMIT_PAGE = PageResult(200, "https://x.com/bob", text="Something went wrong. Try reloading.")


def rate_limited_after_a_search(engine, universe):
    """A cycle whose search reads and whose every profile is X's rate-limit page."""
    with engine.begin() as conn:
        accounts.add_seeds(conn, ["alice", "bob", "carol"], NOW)
    browser = FakeBrowser({"https://x.com/search": page(x_tweet_classic("1", "$NVDA", ALICE))})
    browser.default = RATE_LIMIT_PAGE
    s = scraper(engine, universe, browser, pages=4, search_share=0.25, watchlist=("NVDA",))
    return s, browser


def test_one_rate_limit_page_after_clean_loads_is_a_notice_that_ends_the_cycle(engine, universe):
    """2026-10-12 20:12 UTC: a search and a profile read, the next profile was X's
    rate-limit page, and the scrape failed although the breaker had paused X."""
    s, browser = rate_limited_after_a_search(engine, universe)
    stats = s.run()
    assert stats.errors == [] and stats.searches == 1
    assert len(stats.notices) == 1 and "rate-limit page" in stats.notices[0]
    assert len(browser.calls) == 2  # the search, then the one profile X refused
    assert s.throttle.store.get_block("x").blocked_until is not None


def test_a_second_rate_limit_within_the_day_doubles_the_pause_as_a_notice(engine, universe):
    """2026-10-23: limits at 15:38 and 17:05 UTC, a clean cycle between; both were paused
    for the base backoff, and the second failed the scrape."""
    s, _ = rate_limited_after_a_search(engine, universe)
    # An earlier cycle's rate limit, and a success since that reset the breaker's streak.
    s.throttle.record(RATE_LIMITED, "timeline", 200, "rate-limit page")
    s.throttle.record(OK, "timeline", 200)
    stats = s.run()
    assert stats.errors == [] and len(stats.notices) == 1
    block, base = s.throttle.store.get_block("x"), s.throttle.policy.backoff_base_minutes
    assert block.blocked_until - s.clock() == timedelta(minutes=2 * base)


def test_a_rate_limit_the_longest_pause_answers_fails_the_cycle(engine, universe):
    s, _ = rate_limited_after_a_search(engine, universe)
    p = s.throttle.policy
    s.throttle.policy = replace(p, backoff_max_minutes=2 * p.backoff_base_minutes)
    s.throttle.record(RATE_LIMITED, "timeline", 200, "rate-limit page")
    s.throttle.record(OK, "timeline", 200)
    stats = s.run()
    assert len(stats.errors) == 1 and "rate-limit page" in stats.errors[0]
    assert stats.notices == []


def test_run_stats_start_empty():
    stats = scraper_mod.XRunStats()
    assert stats.requests == 0 and stats.errors == [] and stats.ingest.stored == 0
