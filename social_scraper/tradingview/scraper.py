"""The TradingView crawl: a few symbols' newest ideas a cycle.

One page load is one symbol's ideas page as JSON:
`/symbols/<SYM>/ideas/?component-data-only=1&sort=recent`. Measured 2026-10-10 in a
real Chrome: navigating to it answers `application/json` -- the data the page renders
from -- and a bare ticker (`TSLA`) redirects to its exchange's (`NASDAQ-TSLA`) with the
query kept, so the universe's symbols need no exchange lookup. A share class is written
with a dot there (`BRK.B`), a hyphen in the universe (`BRK-B`). A symbol TradingView does
not know is a 404: nothing to read, not a failure.

Ideas are slow -- a busy symbol gets a few a day -- so each symbol is read at most once
every `MIN_GAP`, one page of 24 per visit, and only ideas newer than the symbol's cursor
(`tradingview:newest:<SYM>`, the newest `created_at` seen) are stored. Which symbols:
the watchlist, then whatever is trending across every platform (`feeds.pick_symbols`).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from urllib.parse import quote

from sqlalchemy.engine import Engine

from social_scraper.config import Settings
from social_scraper.feeds import FeedCrawler, FeedRunStats, pick_symbols
from social_scraper.fetch import Browser
from social_scraper.models import TRADINGVIEW
from social_scraper.pipeline import Ingestor
from social_scraper.throttle import Throttle
from social_scraper.tradingview.parse import parse_ideas

BASE = "https://www.tradingview.com"
READ_PREFIX = "tradingview:read:"
CURSOR_PREFIX = "tradingview:newest:"
MIN_GAP = timedelta(hours=6)


def tv_symbol(symbol: str) -> str:
    """The universe's spelling (`BRK-B`) as TradingView writes it (`BRK.B`)."""
    return symbol.upper().replace("-", ".")


def ideas_url(symbol: str) -> str:
    path = quote(tv_symbol(symbol), safe=".")
    return f"{BASE}/symbols/{path}/ideas/?component-data-only=1&sort=recent"


class TradingViewScraper(FeedCrawler):
    platform = TRADINGVIEW

    def run(self, symbols: list[str]) -> FeedRunStats:
        steps = [(f"ideas {s}", partial(self.read_symbol, s)) for s in symbols]
        return self.run_steps(steps)

    def read_symbol(self, symbol: str) -> None:
        """One symbol's newest ideas page; only ideas past its cursor are stored."""
        result = self.fetch(ideas_url(symbol), "ideas")
        if result.status == 404:
            return
        posts = parse_ideas(result.data)
        key = CURSOR_PREFIX + symbol
        seen = self.get_state(key)
        cursor = datetime.fromisoformat(seen) if seen else None
        fresh = [p for p in posts if cursor is None or p.created_at > cursor]
        if fresh:
            self.ingest(fresh)
        newest = max((p.created_at for p in posts), default=None)
        if newest is not None and (cursor is None or newest > cursor):
            self.set_state(key, newest.isoformat())


def crawl(
    settings: Settings,
    engine: Engine,
    ingestor: Ingestor,
    browser: Browser,
    throttle: Throttle,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> FeedRunStats:
    """One cycle's TradingView crawl for the stock domain."""
    symbols = pick_symbols(
        engine,
        READ_PREFIX,
        settings.watchlist_symbols,
        settings.tradingview_symbols_per_cycle,
        clock(),
        MIN_GAP,
    )
    return TradingViewScraper(browser, throttle, ingestor, engine, clock).run(symbols)
