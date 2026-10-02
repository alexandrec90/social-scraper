"""Playwright transport: a persistent, logged-in Chrome profile per platform.

Why a persistent profile: the login survives between runs (you sign in once with
`social-scraper login <platform>`), and a returning browser with history and cookies
looks like a person rather than a fresh bot every half hour.

Why the installed Chrome (`BROWSER_CHANNEL=chrome`): its fingerprint is a real
Chrome's. `--disable-blink-features=AutomationControlled` and dropping
`--enable-automation` remove the two loudest automation tells (`navigator.webdriver`
and the "controlled by automated software" bar).

Server-rendered data. X's 2026 web app streams the first page of a timeline into the
HTML as JavaScript (TanStack Router / seroval: `self.$R`), not as a GraphQL XHR, and
deletes `self.$R` once hydrated. `KEEP_SSR_JS` runs before any page script and keeps a
reference to that object; `SSR_TWEETS_JS` later walks it for `Tweet` objects and
returns plain-JSON copies, which `capture()` hands back as one more payload. The
parser does not care which route a tweet arrived by.

Out of the way (`BROWSER_MINIMIZED`, the default for the scraper). Chrome takes the
keyboard focus when it launches -- measured 2026-10-02: the foreground window became
Chrome even when placed off-screen -- so a cycle would steal whatever you were typing
into, twice. Minimizing the window over CDP straight after launch hands the focus back
to the previous window within ~0.3 s, and launching it off-screen means nothing flashes
up first. The page keeps rendering minimized (`visibilityState` stays "visible": the
Playwright launch disables Chrome's backgrounding), but wheel events are dropped there,
so a minimized window scrolls with PageDown, which landed a full viewport every time.

This module is deliberately logic-free: every decision about what a page *means* lives
in the scrapers, which are tested against a fake `Browser`.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Any

from social_scraper.fetch import PageResult, Payload

log = logging.getLogger(__name__)

NAV_TIMEOUT_MS = 45_000
TEXT_EXCERPT_CHARS = 3_000
LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]
# Where Windows itself parks minimized windows: off every monitor.
OFFSCREEN_ARG = "--window-position=-32000,-32000"
# A focused input (X's search box) would take PageDown instead of the page.
BLUR_JS = "() => document.activeElement && document.activeElement.blur()"
SSR_PAYLOAD_SUFFIX = "#ssr"
SSR_MAX_NODES = 200_000

# The stream runtime deletes `self.$R.tsr` (the refs array), not `self.$R`, so holding
# `$R` is not enough: every object read *through* `$R` is kept -- which catches the refs
# array the first time a stream script does `$R["tsr"]`.
KEEP_SSR_JS = """
(() => {
  const kept = (self.__keptSsr = self.__keptSsr || []);
  const seen = new WeakSet();
  const keep = (v) => {
    if (v && typeof v === 'object' && !seen.has(v)) { seen.add(v); kept.push(v); }
    return v;
  };
  let current;
  Object.defineProperty(self, '$R', {
    configurable: true,
    get() { return current; },
    set(value) {
      current = value && typeof value === 'object'
        ? new Proxy(keep(value), { get: (t, p) => keep(t[p]) })
        : value;
    },
  });
})();
"""

SSR_TWEETS_JS = """
(maxNodes) => {
  const seen = new Set();
  const stack = [...(self.__keptSsr || [])];
  const tweets = [];
  let visited = 0;
  while (stack.length && visited < maxNodes) {
    const node = stack.pop();
    if (!node || typeof node !== 'object' || seen.has(node)) continue;
    seen.add(node);
    visited += 1;
    if (node.__typename === 'Tweet' && node.rest_id) {
      try { tweets.push(JSON.parse(JSON.stringify(node))); } catch (e) { /* cyclic: skip */ }
    }
    for (const key in node) {
      const value = node[key];
      if (value && typeof value === 'object') stack.push(value);
    }
  }
  return tweets;
}
"""


def launch_args(minimized: bool) -> list[str]:
    return [*LAUNCH_ARGS, OFFSCREEN_ARG] if minimized else list(LAUNCH_ARGS)


class PlaywrightBrowser:
    def __init__(
        self,
        profile_dir: Path,
        channel: str = "chrome",
        headless: bool = False,
        minimized: bool = False,
    ) -> None:
        self.profile_dir = profile_dir
        self.channel = channel
        self.headless = headless
        # A headless browser has no window to move.
        self.minimized = minimized and not headless
        self._pw: Any = None
        self._ctx: Any = None
        self._page: Any = None
        self._rng = secrets.SystemRandom()

    def __enter__(self) -> PlaywrightBrowser:
        from playwright.sync_api import sync_playwright

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            channel=self.channel or None,
            headless=self.headless,
            viewport={"width": 1366, "height": 900},
            locale="en-US",
            args=launch_args(self.minimized),
            ignore_default_args=["--enable-automation"],
        )
        self._ctx.add_init_script(KEEP_SSR_JS)
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        if self.minimized:
            self._minimize()
        return self

    def _minimize(self) -> None:
        from playwright.sync_api import Error as PlaywrightError

        try:
            cdp = self._ctx.new_cdp_session(self._page)
            window = cdp.send("Browser.getWindowForTarget")["windowId"]
            cdp.send(
                "Browser.setWindowBounds",
                {"windowId": window, "bounds": {"windowState": "minimized"}},
            )
            cdp.detach()
        except PlaywrightError as exc:
            # Still off-screen, so it works; it just keeps the focus it took at launch.
            log.warning("could not minimize the browser window: %s", exc)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._ctx is not None:
            self._ctx.close()
        if self._pw is not None:
            self._pw.stop()

    def get_json(self, url: str) -> PageResult:
        from playwright.sync_api import Error as PlaywrightError

        resp = self._page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        data = None
        if resp is not None:
            try:
                data = resp.json()
            except (PlaywrightError, ValueError):
                data = None
        return PageResult(
            status=resp.status if resp is not None else None,
            final_url=self._page.url,
            data=data,
            text="" if data is not None else self._visible_text(),
        )

    def capture(
        self,
        url: str,
        want: Callable[[str], bool],
        scrolls: int,
        between: Callable[[], None],
    ) -> PageResult:
        from playwright.sync_api import Error as PlaywrightError

        hits: list[Any] = []

        def on_response(response: Any) -> None:
            # Only collect here: a blocking call inside a sync-API event handler can
            # deadlock the dispatcher. Bodies are read after the page has settled.
            if want(response.url):
                hits.append(response)

        self._page.on("response", on_response)
        try:
            resp = self._page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            self._settle()
            for _ in range(scrolls):
                between()
                self._scroll()
                self._settle()
            payloads = []
            for hit in hits:
                try:
                    payloads.append(Payload(hit.url, hit.status, hit.json()))
                except (PlaywrightError, ValueError):
                    log.debug("unreadable response body: %s", hit.url)
            ssr = self._ssr_tweets()
            if ssr:
                payloads.append(Payload(self._page.url + SSR_PAYLOAD_SUFFIX, 200, ssr))
        finally:
            self._page.remove_listener("response", on_response)
        return PageResult(
            status=resp.status if resp is not None else None,
            final_url=self._page.url,
            payloads=payloads,
            text=self._visible_text(),
        )

    def _scroll(self) -> None:
        if not self.minimized:
            self._page.mouse.wheel(0, self._rng.randint(1200, 2600))
            return
        # About the same distance as the wheel: two or three viewports.
        self._page.evaluate(BLUR_JS)
        for _ in range(self._rng.randint(2, 3)):
            self._page.keyboard.press("PageDown")
            self._page.wait_for_timeout(self._rng.uniform(150, 450))

    def _settle(self) -> None:
        # A jittered wait instead of "networkidle": X keeps a connection open forever.
        self._page.wait_for_timeout(self._rng.uniform(2500, 4500))

    def _ssr_tweets(self) -> list[Any]:
        from playwright.sync_api import Error as PlaywrightError

        try:
            return self._page.evaluate(SSR_TWEETS_JS, SSR_MAX_NODES) or []
        except PlaywrightError:
            return []

    def _visible_text(self) -> str:
        from playwright.sync_api import Error as PlaywrightError

        try:
            return self._page.inner_text("body", timeout=5_000)[:TEXT_EXCERPT_CHARS]
        except PlaywrightError:
            return ""


def open_for_login(profile_dir: Path, channel: str, url: str) -> None:
    """Open a visible browser on `url` and wait until the user closes it.

    The session cookies land in `profile_dir`, which every later run reuses.
    """
    with PlaywrightBrowser(profile_dir, channel=channel, headless=False) as browser:
        browser._page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        browser._ctx.wait_for_event("close", timeout=0)
