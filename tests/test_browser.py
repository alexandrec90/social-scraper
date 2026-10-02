"""The Playwright transport against a fake page.

A real Chrome is not available in CI. What these pin down is the glue: which calls
reach the page, how responses become `PageResult`s, that bodies are read after the
page settles (never inside the event handler), and that the listener is removed. The
real browser was exercised live on 2026-09-28 (Reddit JSON through a warmed-up
profile; X server-streamed tweets from a logged-out profile page), and headless with the
renamed user agent on 2026-10-02 (the same two reads, through this class).
"""

from pathlib import Path

from playwright.sync_api import Error as PlaywrightError

from social_scraper import browser as browser_mod
from social_scraper.browser import (
    KEEP_SSR_JS,
    SSR_PAYLOAD_SUFFIX,
    SSR_TWEETS_JS,
    PlaywrightBrowser,
    headful_user_agent,
    open_for_login,
)

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


class FakePage:
    def __init__(self, response=None, extra=(), ssr=None, text="page text"):
        self.url = "about:blank"
        self.mouse = FakeMouse()
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

    def evaluate(self, script, arg):
        assert script == SSR_TWEETS_JS
        if isinstance(self._ssr, Exception):
            raise self._ssr
        return self._ssr

    def inner_text(self, selector, timeout):
        return self._text


def make(page):
    b = PlaywrightBrowser(Path("unused"))
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
    assert page.removed == ["response"]
    assert r.text == "page text"


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
            self._page = FakePage(FakeResponse("u", 200))
            self._ctx = FakeCtx()
            return self

        def __exit__(self, *exc):
            events.append("closed")

    monkeypatch.setattr(browser_mod, "PlaywrightBrowser", FakeBrowser)
    open_for_login(tmp_path / "profile", "chrome", "https://x.com/i/flow/login")
    assert events == [("close", 0), "closed"]
