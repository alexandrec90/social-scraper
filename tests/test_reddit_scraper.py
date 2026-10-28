import random
from dataclasses import replace
from datetime import timedelta

import pytest
from conftest import NOW, Clock, FakeBrowser, reddit_comment, reddit_listing, reddit_submission

from social_scraper.db import repository
from social_scraper.fetch import FetchFailed, PageResult
from social_scraper.pipeline import Ingestor
from social_scraper.reddit import scraper as scraper_mod
from social_scraper.reddit.scraper import RedditScraper, RedditRunStats, classify, listing_url
from social_scraper.throttle import (
    CHALLENGED,
    ERROR,
    ERROR_STREAK,
    OK,
    RATE_LIMITED,
    SERVER_ERROR,
    TIMED_OUT,
    Block,
    MemoryThrottleStore,
    Policy,
    Throttle,
)

BASE = "https://www.reddit.com/r/wallstreetbets/"
T0 = NOW.timestamp()


def ok(data):
    return PageResult(200, "https://www.reddit.com/x.json", data=data)


class InOrder(random.Random):
    """Crawls the subreddits in the order given, for a test about which come after."""

    def shuffle(self, x) -> None:
        return None


def scraper(engine, universe, browser, clock=None, **kw):
    clock = clock or Clock()
    store = MemoryThrottleStore()
    throttle = Throttle(
        Policy("reddit", 0, 0, 1000, 1000), store, clock=clock, sleep=lambda s: None
    )
    ingestor = Ingestor(engine, universe, clock)
    options = scraper_mod.RedditOptions(**kw)
    return RedditScraper(browser, throttle, ingestor, engine, options, clock), store


def test_listing_url():
    assert listing_url("stocks", "new") == (
        "https://www.reddit.com/r/stocks/new.json?limit=100&raw_json=1"
    )
    assert listing_url("stocks", "top", "t3_x", "t=day").endswith("&t=day&after=t3_x")


@pytest.mark.parametrize(
    ("result", "outcome"),
    [
        (PageResult(429, "u"), RATE_LIMITED),
        (PageResult(403, "u", text="You've been blocked by network security."), CHALLENGED),
        (PageResult(200, "u", text="Please complete the CAPTCHA"), CHALLENGED),
        (PageResult(502, "u"), SERVER_ERROR),
        (PageResult(None, "u"), SERVER_ERROR),
        (PageResult(404, "u"), OK),
        (PageResult(200, "u", data=None), ERROR),
        (PageResult(200, "u", data={"kind": "Listing"}), OK),
    ],
)
def test_classify(result, outcome):
    assert classify(result)[0] == outcome


def test_classify_html_page_when_json_not_expected():
    assert classify(PageResult(200, "u", data=None), expect_json=False)[0] == OK


def test_crawl_listing_pages_until_seen_and_stores_cursor(engine, universe):
    browser = FakeBrowser(
        {
            BASE + "new.json?limit=100&raw_json=1&after=": ok(reddit_listing([], None)),
            BASE + "new.json?limit=100&raw_json=1": ok(
                reddit_listing(
                    [reddit_submission("a", T0), reddit_submission("b", T0 - 60)], "t3_b"
                )
            ),
        }
    )
    s, store = scraper(engine, universe, browser, max_pages=3)
    stats = s.crawl_listing("wallstreetbets", "new")
    assert stats.stored == 2
    assert len(browser.calls) == 2  # second page was empty -> stop
    with engine.connect() as conn:
        assert float(repository.get_state(conn, "reddit:wallstreetbets:new")) == T0
    assert len(store.requests) == 2


def test_incremental_crawl_stops_at_already_seen(engine, universe):
    page = ok(
        reddit_listing([reddit_submission("a", T0), reddit_submission("old", T0 - 3600)], "t3_old")
    )
    browser = FakeBrowser({BASE + "new.json": page})
    s, _ = scraper(engine, universe, browser, max_pages=5)
    with engine.begin() as conn:
        repository.set_state(conn, "reddit:wallstreetbets:new", str(T0 - 1800), NOW)
    s.crawl_listing("wallstreetbets", "new")
    assert len(browser.calls) == 1


def test_fetch_raises_on_wall_and_records_it(engine, universe):
    browser = FakeBrowser({BASE: PageResult(403, "u", text="blocked by network security")})
    s, store = scraper(engine, universe, browser)
    with pytest.raises(FetchFailed) as exc:
        s.fetch(BASE + "new.json", "listing:new")
    assert exc.value.outcome == CHALLENGED
    assert store.get_block("reddit").blocked_until is not None


def test_run_warms_up_crawls_and_refreshes(engine, universe):
    listing = ok(reddit_listing([reddit_submission("a", T0), reddit_comment("c", T0)]))
    browser = FakeBrowser({BASE + "new.json": listing, BASE + "comments.json": listing})
    browser.pages[BASE + "top.json"] = listing
    browser.pages[BASE] = PageResult(200, BASE, text="r/wallstreetbets")
    s, _ = scraper(engine, universe, browser)
    stats = s.run(("wallstreetbets",))
    assert isinstance(stats, RedditRunStats)
    assert browser.calls[0] == BASE  # warm-up first
    assert any("top.json" in c for c in browser.calls)
    assert stats.requests == 4 and not stats.errors


def test_run_skips_the_warm_up_for_an_already_warm_browser(engine, universe):
    listing = ok(reddit_listing([reddit_submission("a", T0)]))
    browser = FakeBrowser({BASE + "new.json": listing, BASE + "comments.json": listing})
    browser.pages[BASE + "top.json"] = listing
    s, _ = scraper(engine, universe, browser)
    stats = s.run(("wallstreetbets",), warm=False)
    assert BASE not in browser.calls and stats.requests == 3


def test_refresh_is_not_repeated_within_the_interval(engine, universe):
    listing = ok(reddit_listing([]))
    browser = FakeBrowser({BASE: listing})
    browser.pages = {BASE + "top.json": listing, BASE + "new.json": listing}
    browser.pages[BASE + "comments.json"] = listing
    browser.pages[BASE] = PageResult(200, BASE)
    clock = Clock()
    s, _ = scraper(engine, universe, browser, clock=clock, refresh_hours=6)
    s.run(("wallstreetbets",))
    clock.now = NOW + timedelta(hours=1)
    s.run(("wallstreetbets",))
    assert sum("top.json" in c for c in browser.calls) == 1


def test_the_cycle_deadline_ends_the_run_with_what_it_crawled(engine, universe):
    listing = ok(reddit_listing([reddit_submission("a", T0)]))
    browser = FakeBrowser({BASE + "new.json": listing, BASE + "comments.json": listing})
    s, store = scraper(engine, universe, browser)
    s.throttle = Throttle(
        Policy("reddit", 30, 30, 1000, 1000, deadline=NOW + timedelta(seconds=10)),
        store,
        clock=Clock(),
        sleep=lambda s: None,
    )
    stats = s.run(("wallstreetbets",), warm=False)
    assert stats.requests == 1 and stats.ingest.stored == 1 and not stats.errors
    assert len(stats.notices) == 1 and "out of cycle time" in stats.notices[0]


def test_run_stops_on_wall_without_raising(engine, universe):
    browser = FakeBrowser()
    browser.default = PageResult(429, "u")
    s, _ = scraper(engine, universe, browser)
    stats = s.run(("wallstreetbets", "stocks"))
    assert len(browser.calls) == 1
    # The day's only failed load: the backoff is the response (`Throttle.notice`).
    assert stats.notices and not stats.errors


def test_a_rate_limit_again_within_the_day_is_a_notice(engine, universe):
    browser = FakeBrowser()
    browser.default = PageResult(429, "u")
    s, _ = scraper(engine, universe, browser)
    s.throttle.record(RATE_LIMITED, "listing:new", 429, "HTTP 429")
    s.throttle.record(OK, "listing:new", 200)
    stats = s.run(("wallstreetbets",))
    assert len(browser.calls) == 1 and stats.notices and not stats.errors


def test_a_rate_limit_the_longest_pause_answers_is_an_error(engine, universe):
    browser = FakeBrowser()
    browser.default = PageResult(429, "u")
    s, _ = scraper(engine, universe, browser)
    p = s.throttle.policy
    s.throttle.policy = replace(p, backoff_max_minutes=2 * p.backoff_base_minutes)
    s.throttle.record(RATE_LIMITED, "listing:new", 429, "HTTP 429")
    s.throttle.record(OK, "listing:new", 200)
    stats = s.run(("wallstreetbets",))
    assert len(browser.calls) == 1 and "HTTP 429" in stats.errors[0]


CHALLENGE = PageResult(
    200,
    "https://www.reddit.com/?solution=abc&js_challenge=1&jsc_token=def",
    text="You've been blocked by network security. If you think you've been blocked...",
)


def test_is_js_challenge():
    assert scraper_mod.is_js_challenge(CHALLENGE)
    assert scraper_mod.is_js_challenge(PageResult(200, BASE, text="blocked by network security"))
    assert not scraper_mod.is_js_challenge(PageResult(200, BASE, text="r/wallstreetbets"))
    # A 403 JSON block is a real block, not the interstitial.
    assert not scraper_mod.is_js_challenge(
        PageResult(403, BASE, text="blocked by network security")
    )


class ChallengeOnce(FakeBrowser):
    """A fresh profile: the first HTML load is the challenge, the next passes."""

    def __init__(self, challenges: int = 1) -> None:
        super().__init__({BASE: PageResult(200, BASE, text="r/wallstreetbets")})
        self.default = ok(reddit_listing([]))
        self.challenges = challenges

    def capture(self, url, want, scrolls, between):
        if self.challenges:
            self.challenges -= 1
            self.calls.append(url)
            return CHALLENGE
        return super().capture(url, want, scrolls, between)


def test_warm_up_clears_the_js_challenge_on_a_later_load(engine, universe):
    browser = ChallengeOnce()
    s, store = scraper(engine, universe, browser)
    s.warm_up("wallstreetbets")
    assert browser.calls == [BASE, BASE]
    assert store.get_block("reddit").blocked_until is None  # the interstitial is no block


def test_warm_up_gives_up_after_repeated_challenges(engine, universe):
    browser = ChallengeOnce(challenges=scraper_mod.WARM_UP_ATTEMPTS)
    s, store = scraper(engine, universe, browser)
    with pytest.raises(FetchFailed) as exc:
        s.warm_up("wallstreetbets")
    assert exc.value.outcome == CHALLENGED
    assert store.get_block("reddit").blocked_until is not None
    assert len(browser.calls) == scraper_mod.WARM_UP_ATTEMPTS


def test_warm_up_failure_is_reported(engine, universe):
    browser = FakeBrowser({BASE: PageResult(403, "u", text="blocked")})
    s, _ = scraper(engine, universe, browser)
    with pytest.raises(FetchFailed):
        s.warm_up("wallstreetbets")
    assert "warm-up" in s.stats.notices[0] and s.stats.errors == []


def test_warm_up_failure_after_a_held_block_is_an_error(engine, universe):
    browser = FakeBrowser({BASE: PageResult(403, "u", text="blocked")})
    s, store = scraper(engine, universe, browser)
    store.set_block("reddit", Block(None, "challenged: HTTP 403", 1), NOW)
    with pytest.raises(FetchFailed):
        s.warm_up("wallstreetbets")
    assert "warm-up" in s.stats.errors[0] and s.stats.notices == []


def test_a_first_403_mid_run_is_a_notice_that_ends_the_run(engine, universe):
    """2026-10-12: one anonymous 403 after 21 good loads paused Reddit for its cooldown,
    as it should, and also failed the scheduled scrape -- for a block only waiting lifts."""
    listing = ok(reddit_listing([reddit_submission("a", T0)]))
    browser = FakeBrowser(
        {
            BASE + "new.json": listing,
            BASE + "comments.json": listing,
            BASE + "top.json": listing,
        }
    )
    stocks = "https://www.reddit.com/r/stocks/"
    browser.pages[stocks + "new.json"] = PageResult(403, "u", text="Forbidden")
    s, store = scraper(engine, universe, browser)
    stats = s.run(("wallstreetbets", "stocks", "thetagang"), InOrder(), warm=False)
    assert stats.errors == [] and len(stats.notices) == 1
    assert "r/stocks/new: challenged: HTTP 403" in stats.notices[0]
    assert not any("thetagang" in c for c in browser.calls)  # the run stopped there
    assert store.get_block("reddit").blocked_until is not None


def test_a_403_on_the_first_load_after_the_cooldown_is_an_error(engine, universe):
    browser = FakeBrowser({BASE + "new.json": PageResult(403, "u", text="Forbidden")})
    s, store = scraper(engine, universe, browser)
    store.set_block("reddit", Block(None, "challenged: HTTP 403", 1), NOW)
    stats = s.run(("wallstreetbets",), warm=False)
    assert stats.notices == [] and len(stats.errors) == 1 and "403" in stats.errors[0]


def test_error_on_one_listing_continues_with_the_next(engine, universe):
    browser = FakeBrowser(
        {
            BASE + "new.json": PageResult(200, "u", data=None),
            BASE + "comments.json": ok(reddit_listing([reddit_comment("c", T0)])),
            BASE + "top.json": ok(reddit_listing([])),
            BASE: PageResult(200, BASE),
        }
    )
    s, _ = scraper(engine, universe, browser)
    stats = s.run(("wallstreetbets",))
    assert stats.ingest.stored == 1
    assert any("new" in e for e in stats.errors)


def test_one_server_error_is_a_notice_and_the_crawl_goes_on(engine, universe):
    browser = FakeBrowser(
        {
            BASE + "new.json": PageResult(502, "u"),
            BASE + "comments.json": ok(reddit_listing([reddit_comment("c", T0)])),
            BASE + "top.json": ok(reddit_listing([])),
        }
    )
    s, _ = scraper(engine, universe, browser)
    stats = s.run(("wallstreetbets",), warm=False)
    assert stats.errors == [] and any("HTTP 502" in n for n in stats.notices)
    assert stats.ingest.stored == 1


def test_server_errors_in_a_row_pause_and_end_the_run_as_notices(engine, universe):
    browser = FakeBrowser()
    browser.default = PageResult(500, "u")
    s, store = scraper(engine, universe, browser)
    stats = s.run(("wallstreetbets", "stocks"), warm=False)
    assert len(browser.calls) == ERROR_STREAK
    assert len(stats.notices) == ERROR_STREAK and stats.errors == []
    assert store.get_block("reddit").blocked_until is not None


def test_unparseable_page_is_reported_not_raised(engine, universe):
    browser = FakeBrowser(
        {BASE + "new.json": ok({"not": "a listing"}), BASE: PageResult(200, BASE)}
    )
    browser.default = ok(reddit_listing([]))
    s, _ = scraper(engine, universe, browser)
    stats = s.run(("wallstreetbets",))
    assert any("unparseable" in e for e in stats.errors)


def test_empty_subreddit_list_does_nothing(engine, universe):
    browser = FakeBrowser()
    s, _ = scraper(engine, universe, browser)
    s.run(())
    assert browser.calls == []


def test_missing_subreddit_404_is_not_an_error(engine, universe):
    gone = PageResult(404, "u")
    browser = FakeBrowser(
        {
            BASE: PageResult(200, BASE),
            BASE + "new.json": gone,
            BASE + "comments.json": gone,
            BASE + "top.json": gone,
        }
    )
    s, _ = scraper(engine, universe, browser)
    stats = s.run(("wallstreetbets",))
    assert stats.errors == []


TIMED_OUT_PAGE = PageResult(None, "u", timed_out=True)


def test_a_timed_out_load_classifies_as_a_timeout_not_a_missing_body():
    assert classify(TIMED_OUT_PAGE) == (TIMED_OUT, "page load timed out")
    assert classify(TIMED_OUT_PAGE, expect_json=False)[0] == TIMED_OUT


def test_one_timed_out_listing_is_a_notice_and_the_crawl_goes_on(engine, universe):
    """2026-10-08: one 45 s `Page.goto` timeout, on a machine a backfill was loading,
    ended the platform's crawl and failed the scheduled scrape."""
    browser = FakeBrowser(
        {
            BASE + "new.json": TIMED_OUT_PAGE,
            BASE + "comments.json": ok(reddit_listing([reddit_comment("c", T0)])),
            BASE + "top.json": ok(reddit_listing([])),
        }
    )
    s, store = scraper(engine, universe, browser)
    stats = s.run(("wallstreetbets",), warm=False)
    assert stats.errors == []
    assert any("new" in n and "timed out" in n for n in stats.notices)
    assert stats.ingest.stored == 1 and stats.requests == 3
    # Recorded against the breaker as a plain error, which the next success clears.
    assert store.get_block("reddit").consecutive_failures == 0


def test_timeouts_in_a_row_pause_and_end_the_run_as_notices(engine, universe):
    browser = FakeBrowser()
    browser.default = TIMED_OUT_PAGE
    s, store = scraper(engine, universe, browser)
    stats = s.run(("wallstreetbets", "stocks"), warm=False)
    assert len(browser.calls) == ERROR_STREAK
    assert stats.errors == [] and len(stats.notices) == ERROR_STREAK
    assert all("timed out" in n for n in stats.notices)
    assert store.get_block("reddit").blocked_until is not None


def test_a_timeout_after_failing_runs_is_an_error_at_once(engine, universe):
    """The streak is the breaker's persisted count, so a platform that keeps timing out
    across cycles -- through the pause the streak set -- is not a notice every cycle."""
    browser = FakeBrowser()
    browser.default = TIMED_OUT_PAGE
    s, store = scraper(engine, universe, browser)
    store.set_block("reddit", Block(None, "error: HTTP 502", ERROR_STREAK), NOW)
    stats = s.run(("wallstreetbets",), warm=False)
    assert len(browser.calls) == 1 and stats.notices == [] and len(stats.errors) == 1


def test_a_timed_out_warm_up_is_a_notice(engine, universe):
    browser = FakeBrowser({BASE: TIMED_OUT_PAGE})
    s, _ = scraper(engine, universe, browser)
    stats = s.run(("wallstreetbets",))
    assert browser.calls == [BASE]
    assert stats.errors == [] and "warm-up" in stats.notices[0]
