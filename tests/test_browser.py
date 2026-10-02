"""The Playwright transport against a fake page.

A real Chrome is not available in CI. What these pin down is the glue: which calls
reach the page, how responses become `PageResult`s, that bodies are read after the
page settles (never inside the event handler), and that the listener is removed. The
real browser was exercised live on 2026-09-28 (Reddit JSON through a warmed-up
profile; X server-streamed tweets from a logged-out profile page).
"""

from pathlib import Path

from playwright.sync_api import Error as PlaywrightError

from social_scraper import browser as browser_mod
from social_scraper.browser import (
    BLUR_JS,
    KEEP_SSR_JS,
    LAUNCH_ARGS,
    OFFSCREEN_ARG,
    SSR_PAYLOAD_SUFFIX,
    SSR_TWEETS_JS,
    PlaywrightBrowser,
    launch_args,
    open_for_login,
)


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
