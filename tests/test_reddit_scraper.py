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
    OK,
    RATE_LIMITED,
    MemoryThrottleStore,
    Policy,
    Throttle,
)

BASE = "https://www.reddit.com/r/wallstreetbets/"
T0 = NOW.timestamp()


def ok(data):
    return PageResult(200, "https://www.reddit.com/x.json", data=data)


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
        (PageResult(502, "u"), ERROR),
        (PageResult(None, "u"), ERROR),
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


def test_run_stops_on_wall_without_raising(engine, universe):
    browser = FakeBrowser()
    browser.default = PageResult(429, "u")
    s, _ = scraper(engine, universe, browser)
    stats = s.run(("wallstreetbets", "stocks"))
    assert len(browser.calls) == 1
    assert stats.errors


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
    assert "warm-up" in s.stats.errors[0]


def test_error_on_one_listing_continues_with_the_next(engine, universe):
    browser = FakeBrowser(
        {
            BASE + "new.json": PageResult(502, "u"),
            BASE + "comments.json": ok(reddit_listing([reddit_comment("c", T0)])),
            BASE + "top.json": ok(reddit_listing([])),
            BASE: PageResult(200, BASE),
        }
    )
    s, _ = scraper(engine, universe, browser)
    stats = s.run(("wallstreetbets",))
    assert stats.ingest.stored == 1
    assert any("new" in e for e in stats.errors)


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
