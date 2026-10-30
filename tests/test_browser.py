"""The Playwright transport against a fake page.

A real Chrome is not available in CI. What these pin down is the glue: which calls
reach the page, how responses become `PageResult`s, that bodies are read after the
page settles (never inside the event handler), and that the listener is removed. The
real browser was exercised live on 2026-09-28 (Reddit JSON through a warmed-up
profile; X server-streamed tweets from a logged-out profile page), and headless with the
renamed user agent on 2026-10-02 (the same two reads, through this class).
"""

from pathlib import Path

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from social_scraper import browser as browser_mod
from social_scraper.browser import (
    BLUR_JS,
    KEEP_SSR_JS,
    LAUNCH_ARGS,
    OFFSCREEN_ARG,
    SSR_PAYLOAD_SUFFIX,
    SSR_TWEETS_JS,
    PlaywrightBrowser,
    headful_user_agent,
    launch_args,
    open_for_login,
)
from social_scraper.feeds import classify
from social_scraper.fetch import LaunchTimedOut
from social_scraper.throttle import SERVER_ERROR

HEADLESS_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "HeadlessChrome/154.0.0.0 Safari/537.36"
)


class FakeLaunched:
    """Stands in for both a probe `Browser` and a persistent context."""

    def __init__(self, calls, ua=HEADLESS_UA):
        self.calls, self.ua, self.pages = calls, ua, []

    def new_page(self):
        launched = self

        class Page:
            def evaluate(self, script):
                assert script == "navigator.userAgent"
                return launched.ua

        return Page()

    def add_init_script(self, script):
        self.calls.append(("init_script", script))

    def close(self):
        self.calls.append(("close",))


class FakePlaywright:
    def __init__(self):
        self.calls = []
        self.chromium = self

    def start(self):
        return self

    def stop(self):
        self.calls.append(("stop",))

    def launch(self, **kwargs):
        self.calls.append(("probe", kwargs))
        return FakeLaunched(self.calls)

    def launch_persistent_context(self, **kwargs):
        self.calls.append(("persistent", kwargs))
        return FakeLaunched(self.calls)


def enter(monkeypatch, tmp_path, headless):
    fake = FakePlaywright()
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: fake)
    with PlaywrightBrowser(tmp_path / "profile", "chrome", headless=headless):
        pass
    return fake.calls


def test_headful_user_agent_renames_only_the_headless_token():
    assert headful_user_agent(HEADLESS_UA) == HEADLESS_UA.replace("HeadlessChrome/", "Chrome/")
    headful = HEADLESS_UA.replace("HeadlessChrome/", "Chrome/")
    assert headful_user_agent(headful) == headful
    assert headful_user_agent("") == ""


def test_headless_launch_probes_the_real_ua_and_renames_it(monkeypatch, tmp_path):
    calls = enter(monkeypatch, tmp_path, headless=True)
    kinds = [c[0] for c in calls]
    # The probe is closed before the profile is opened: one Chrome at a time.
    assert kinds[:3] == ["probe", "close", "persistent"]
    assert calls[0][1] == {"channel": "chrome", "headless": True}
    options = calls[2][1]
    assert options["headless"] is True
    assert options["user_agent"] == headful_user_agent(HEADLESS_UA)
    assert options["user_data_dir"] == str(tmp_path / "profile")
    assert ("init_script", KEEP_SSR_JS) in calls


def test_headed_launch_keeps_chromes_own_ua(monkeypatch, tmp_path):
    calls = enter(monkeypatch, tmp_path, headless=False)
    assert [c[0] for c in calls if c[0] in ("probe", "persistent")] == ["persistent"]
    options = calls[0][1]
    assert options["headless"] is False and "user_agent" not in options


def test_launch_options_for_the_bundled_chromium():
    options = PlaywrightBrowser(Path("p"), channel="").launch_options(None)
    assert options["channel"] is None
    assert "--disable-blink-features=AutomationControlled" in options["args"]
    assert options["ignore_default_args"] == ["--enable-automation"]


def test_a_minimized_headed_launch_starts_off_screen():
    b = PlaywrightBrowser(Path("p"), headless=False, minimized=True)
    assert b.minimized and b.launch_options(None)["args"] == [*LAUNCH_ARGS, OFFSCREEN_ARG]


def test_minimized_is_ignored_headless():
    # No window to move: a headless launch keeps the wheel scroll and the plain args.
    b = PlaywrightBrowser(Path("p"), headless=True, minimized=True)
    assert not b.minimized and b.launch_options("ua")["args"] == LAUNCH_ARGS


class FakeResponse:
    def __init__(self, url, status=200, body=None, bad=False):
        self.url, self.status, self._body, self._bad = url, status, body, bad

    def json(self):
        if self._bad:
            raise ValueError("not json")
        return self._body


class FakeMouse:
    def __init__(self):
        self.wheels = []

    def wheel(self, dx, dy):
        self.wheels.append(dy)


class FakeKeyboard:
    def __init__(self):
        self.keys = []

    def press(self, key):
        self.keys.append(key)


class FakePage:
    def __init__(self, response=None, extra=(), ssr=None, text="page text"):
        self.url = "about:blank"
        self.mouse = FakeMouse()
        self.keyboard = FakeKeyboard()
        self.blurs = 0
        self.handlers = {}
        self.removed = []
        self._response, self._extra, self._ssr, self._text = response, extra, ssr, text
        self.waits = []

    def on(self, event, handler):
        self.handlers[event] = handler

    def remove_listener(self, event, handler):
        self.removed.append(event)

    def goto(self, url, wait_until, timeout):
        self.url = url
        for r in self._extra:
            self.handlers["response"](r)
        return self._response

    def wait_for_timeout(self, ms):
        self.waits.append(ms)

    def evaluate(self, script, arg=None):
        if script == BLUR_JS:
            self.blurs += 1
            return None
        assert script == SSR_TWEETS_JS
        if isinstance(self._ssr, Exception):
            raise self._ssr
        return self._ssr

    def inner_text(self, selector, timeout):
        return self._text


def make(page, minimized=False):
    b = PlaywrightBrowser(Path("unused"), minimized=minimized)
    b._page = page
    return b


def test_get_json_parses_body():
    b = make(FakePage(FakeResponse("u", 200, {"kind": "Listing"})))
    r = b.get_json("https://www.reddit.com/r/x/new.json")
    assert r.status == 200 and r.data == {"kind": "Listing"} and r.text == ""


def test_get_json_keeps_page_text_when_body_is_not_json():
    b = make(FakePage(FakeResponse("u", 403, bad=True), text="blocked by network security"))
    r = b.get_json("u")
    assert r.data is None and "blocked" in r.text


def test_get_json_without_response():
    r = make(FakePage(None)).get_json("u")
    assert r.status is None and r.data is None


def test_capture_filters_scrolls_and_adds_ssr():
    extra = [
        FakeResponse("https://x.com/i/api/graphql/a/SearchTimeline", 200, {"data": 1}),
        FakeResponse("https://x.com/i/api/graphql/a/Other", 200, {"data": 2}),
        FakeResponse("https://x.com/i/api/graphql/a/SearchTimeline", 200, bad=True),
    ]
    page = FakePage(FakeResponse("u", 200), extra=extra, ssr=[{"__typename": "Tweet"}])
    pauses = []
    r = make(page).capture(
        "https://x.com/search?q=a", lambda u: "SearchTimeline" in u, 2, lambda: pauses.append(1)
    )
    assert [p.data for p in r.payloads] == [{"data": 1}, [{"__typename": "Tweet"}]]
    assert r.payloads[-1].url.endswith(SSR_PAYLOAD_SUFFIX)
    assert len(page.mouse.wheels) == 2 and len(pauses) == 2
    assert page.keyboard.keys == []
    assert page.removed == ["response"]
    assert r.text == "page text"


def test_minimized_capture_scrolls_with_page_down():
    # Measured 2026-10-02 on a minimized Chrome: wheel events are dropped after the
    # first (0 px on 5 of 6), PageDown scrolled a full viewport every time.
    page = FakePage(FakeResponse("u", 200))
    pauses = []
    make(page, minimized=True).capture("u", lambda u: True, 2, lambda: pauses.append(1))
    assert page.mouse.wheels == [] and len(pauses) == 2
    assert 4 <= len(page.keyboard.keys) <= 6 and set(page.keyboard.keys) == {"PageDown"}
    # A focused search box would swallow PageDown, so focus is dropped first.
    assert page.blurs == 2


def test_launch_args_put_a_minimized_window_off_screen():
    assert launch_args(minimized=False) == LAUNCH_ARGS
    assert launch_args(minimized=True) == [*LAUNCH_ARGS, OFFSCREEN_ARG]


class FakeCdp:
    def __init__(self, fail=False):
        self.sent, self.detached, self.fail = [], False, fail

    def send(self, method, params=None):
        if self.fail:
            raise PlaywrightError("no Browser domain")
        self.sent.append((method, params))
        return {"windowId": 7} if method == "Browser.getWindowForTarget" else {}

    def detach(self):
        self.detached = True


class FakeCdpCtx:
    def __init__(self, cdp):
        self.cdp = cdp

    def new_cdp_session(self, page):
        return self.cdp


def test_minimize_sets_the_window_state_over_cdp():
    cdp = FakeCdp()
    b = make(FakePage(), minimized=True)
    b._ctx = FakeCdpCtx(cdp)
    b._minimize()
    assert cdp.sent[-1] == (
        "Browser.setWindowBounds",
        {"windowId": 7, "bounds": {"windowState": "minimized"}},
    )
    assert cdp.detached


def test_minimize_failure_leaves_the_window_off_screen():
    b = make(FakePage(), minimized=True)
    b._ctx = FakeCdpCtx(FakeCdp(fail=True))
    b._minimize()  # logged, not raised: an off-screen window still works


def test_capture_survives_ssr_evaluation_failure():
    page = FakePage(FakeResponse("u", 200), ssr=PlaywrightError("page closed"))
    r = make(page).capture("u", lambda u: True, 0, lambda: None)
    assert r.payloads == []


def test_visible_text_failure_is_empty():
    class NoText(FakePage):
        def inner_text(self, selector, timeout):
            raise PlaywrightError("detached")

    r = make(NoText(FakeResponse("u", 500, bad=True))).get_json("u")
    assert r.text == ""


def test_init_script_keeps_what_the_stream_reads():
    # The two scripts are a pair: one keeps `$R` reads, the other walks what was kept.
    assert "__keptSsr" in KEEP_SSR_JS and "__keptSsr" in SSR_TWEETS_JS
    assert "new Proxy" in KEEP_SSR_JS


class SlowPage(FakePage):
    def goto(self, url, wait_until, timeout):
        raise PlaywrightTimeoutError(f"Page.goto: Timeout {timeout}ms exceeded.")


class HtmlPage(FakePage):
    def __init__(self, response, html="<html><body>forum</body></html>", fail=False, **kw):
        super().__init__(response, **kw)
        self._html, self._fail = html, fail

    def content(self):
        if self._fail:
            raise PlaywrightError("Target closed")
        return self._html


def test_get_html_returns_the_document_and_its_text():
    page = HtmlPage(FakeResponse("u", 200), text="forum")
    r = make(page).get_html("https://forums.hfboards.com/forums/x.1/")
    assert r.status == 200 and r.html.startswith("<html>") and r.text == "forum"
    assert r.final_url == "https://forums.hfboards.com/forums/x.1/" and r.data is None


def test_get_html_with_an_unreadable_document_is_empty_not_a_raise():
    r = make(HtmlPage(FakeResponse("u", 503), fail=True)).get_html("u")
    assert r.status == 503 and r.html == ""


def test_a_timed_out_html_load_is_a_result():
    r = make(SlowPage()).get_html("https://forums.hfboards.com/")
    assert r.timed_out and r.status is None


def test_a_timed_out_json_load_is_a_result_not_a_raise():
    """2026-10-08: the raise ended the platform's crawl and failed the scheduled scrape."""
    r = make(SlowPage()).get_json("https://www.reddit.com/r/x/new.json")
    assert r.timed_out and r.status is None and r.final_url.endswith("new.json")


def test_a_timed_out_capture_is_a_result_and_drops_its_listener():
    page = SlowPage()
    r = make(page).capture("https://x.com/a", lambda u: True, 2, lambda: None)
    assert r.timed_out and r.payloads == [] and page.removed == ["response"]
    assert page.waits == []  # no settling, no scrolling on a page that never loaded


class DroppedPage(FakePage):
    def __init__(self, code="ERR_CONNECTION_CLOSED"):
        super().__init__(FakeResponse("u", 200))
        self.code = code

    def goto(self, url, wait_until, timeout):
        raise PlaywrightError(f"Page.goto: net::{self.code} at {url}\nCall log:\n  - nav")


TV_IDEAS = "https://www.tradingview.com/symbols/SPCX/ideas/?component-data-only=1"


def test_a_dropped_connection_is_a_server_error_not_a_raise():
    """2026-10-30: one `net::ERR_CONNECTION_CLOSED` ended TradingView's crawl and failed
    the scheduled scrape; it is the site's trouble, which the throttle reads as a 5xx."""
    r = make(DroppedPage()).get_json(TV_IDEAS)
    assert r.status is None and not r.timed_out and r.final_url == TV_IDEAS
    assert r.text == "net::ERR_CONNECTION_CLOSED"
    assert classify(r)[0] == SERVER_ERROR


@pytest.mark.parametrize(
    "code", ["ERR_CONNECTION_RESET", "ERR_EMPTY_RESPONSE", "ERR_NAME_NOT_RESOLVED"]
)
def test_every_load_shape_returns_a_dropped_connection(code):
    assert make(DroppedPage(code)).get_html("u").status is None
    page = DroppedPage(code)
    r = make(page).capture("https://x.com/a", lambda u: True, 2, lambda: None)
    assert r.text == f"net::{code}" and page.removed == ["response"] and page.waits == []


@pytest.mark.parametrize("code", ["ERR_ABORTED", "ERR_BLOCKED_BY_CLIENT", "ERR_CONNECTION_X"])
def test_a_network_error_that_is_not_the_site_still_raises(code):
    with pytest.raises(PlaywrightError, match=code):
        make(DroppedPage(code)).get_json("u")


class StalledPlaywright(FakePlaywright):
    def __init__(self, error):
        super().__init__()
        self.error = error

    def launch_persistent_context(self, **kwargs):
        raise self.error


def test_a_launch_timeout_is_its_own_error_and_stops_playwright(monkeypatch, tmp_path):
    """A raise from `__enter__` never reaches `__exit__`: the driver would outlive the run."""
    fake = StalledPlaywright(PlaywrightTimeoutError("Timeout 180000ms exceeded."))
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: fake)
    with pytest.raises(LaunchTimedOut, match="180000ms"):
        with PlaywrightBrowser(tmp_path / "x", "chrome", headless=False):
            raise AssertionError("entered")
    assert fake.calls[-1] == ("stop",)


def test_any_launch_failure_stops_playwright(monkeypatch, tmp_path):
    fake = StalledPlaywright(PlaywrightError("Chrome not found"))
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: fake)
    with pytest.raises(PlaywrightError, match="Chrome not found"):
        with PlaywrightBrowser(tmp_path / "x", "chrome", headless=False):
            raise AssertionError("entered")
    assert fake.calls[-1] == ("stop",)


def test_open_for_login_waits_for_the_window(monkeypatch, tmp_path):
    events = []

    class FakeCtx:
        def wait_for_event(self, name, timeout):
            events.append((name, timeout))

    class FakeBrowser(PlaywrightBrowser):
        def __enter__(self):
            events.append(("minimized", self.minimized))
            self._page = FakePage(FakeResponse("u", 200))
            self._ctx = FakeCtx()
            return self

        def __exit__(self, *exc):
            events.append("closed")

    monkeypatch.setattr(browser_mod, "PlaywrightBrowser", FakeBrowser)
    open_for_login(tmp_path / "profile", "chrome", "https://x.com/i/flow/login")
    # The login window is the one you have to see and type into.
    assert events == [("minimized", False), ("close", 0), "closed"]
