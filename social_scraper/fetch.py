"""The contract between scrapers and whatever drives the browser.

Scrapers only ever see `Browser` and `PageResult`, so every crawl decision -- what to
fetch next, when a page is a wall, when to stop -- is tested against a fake, and the
Playwright code (`browser.py`) stays a thin, dumb transport.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class Payload:
    """One JSON response the page made while we watched (X GraphQL)."""

    url: str
    status: int
    data: Any


@dataclass(frozen=True)
class PageResult:
    status: int | None
    final_url: str
    data: Any = None
    payloads: list[Payload] = field(default_factory=list)
    # Visible text excerpt: how a captcha or login wall is recognised.
    text: str = ""
    # The load got no answer within the browser's navigation timeout. Returned rather
    # than raised, so the scraper records it against the throttle and goes on to the next
    # page: raised, it ended the whole platform's crawl and failed the cycle (2026-10-08,
    # one 45 s `Page.goto` timeout while a backfill loaded the machine).
    timed_out: bool = False


class Browser(Protocol):
    def get_json(self, url: str) -> PageResult:
        """Navigate to a JSON URL and parse the document."""
        ...

    def capture(
        self,
        url: str,
        want: Callable[[str], bool],
        scrolls: int,
        between: Callable[[], None],
    ) -> PageResult:
        """Open `url`, scroll `scrolls` times, and return every JSON response whose URL
        satisfies `want`. `between` is called before each scroll (the human pause)."""
        ...


class LaunchTimedOut(Exception):
    """The browser did not start within Playwright's launch timeout.

    Its own type so a crawl can tell "Chrome would not start this cycle" -- a loaded
    machine -- from a Playwright error about a page (`launch.py`).
    """


class FetchFailed(Exception):
    """A page load came back as something other than content."""

    def __init__(self, outcome: str, detail: str) -> None:
        super().__init__(f"{outcome}: {detail}")
        self.outcome = outcome
        self.detail = detail
