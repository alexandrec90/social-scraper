"""The live StockTwits crawl: a few symbol streams a cycle, through the browser.

StockTwits closed its API to new developers, but the web app reads the same JSON
endpoints, and a signed-out Chrome loads them as plain JSON documents (measured
2026-10-10: `api.stocktwits.com/api/2/streams/symbol/AAPL.json` and
`/api/2/trending/symbols.json` both answered 200 with no challenge). So each page load
is one endpoint, read as JSON (`Browser.get_json`), under StockTwits' own throttle and
profile -- never the DOM.

Per cycle:

1. which symbols: `STOCKTWITS_SYMBOLS_PER_CYCLE` of the watchlist, then StockTwits' own
   trending list, then whatever is trending across every platform -- each at most once
   an hour (`feeds.pick_symbols`). They are picked from the trending list as last
   stored, so a list refreshed this cycle is used from the next.
2. the trending list, when older than `STOCKTWITS_TRENDING_HOURS`: crypto (`.X`) and
   symbols outside the directory dropped.
3. each symbol's stream, `?since=` the newest message id read from it before (the
   cursor `stocktwits:since:<SYM>`). One load is the 30 newest; on a busy symbol more
   than 30 may arrive between visits, and the older ones are not paged back for.

Every message the stream returns is stored: StockTwits is about stocks, and a stream
names its symbol. The author's Bullish/Bearish label lands in `extra.sentiment`, as in
the NYU archive.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import partial

from sqlalchemy.engine import Engine

from social_scraper.config import Settings
from social_scraper.feeds import FeedCrawler, FeedRunStats, pick_symbols
from social_scraper.fetch import Browser
from social_scraper.models import STOCKTWITS
from social_scraper.pipeline import Ingestor
from social_scraper.stocktwits.parse import parse_stream, parse_trending
from social_scraper.symbols.universe import normalize_symbol
from social_scraper.throttle import Throttle

API = "https://api.stocktwits.com/api/2"
READ_PREFIX = "stocktwits:read:"
SINCE_PREFIX = "stocktwits:since:"
TRENDING_KEY = "stocktwits:trending"
TRENDING_AT_KEY = "stocktwits:trending_at"
MIN_GAP = timedelta(hours=1)


def stream_symbol(symbol: str) -> str:
    """The directory's `BRK-B` as StockTwits writes it, `BRK.B`."""
    return symbol.replace("-", ".")


def stream_url(symbol: str, since: int | None = None) -> str:
    url = f"{API}/streams/symbol/{stream_symbol(symbol)}.json"
    return f"{url}?since={since}" if since is not None else url


TRENDING_URL = f"{API}/trending/symbols.json"


class StockTwitsScraper(FeedCrawler):
    platform = STOCKTWITS

    def __init__(
        self,
        browser: Browser,
        throttle: Throttle,
        ingestor: Ingestor,
        engine: Engine,
        settings: Settings,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        super().__init__(browser, throttle, ingestor, engine, clock)
        self.universe = ingestor.universe
        self.settings = settings

    def run(self) -> FeedRunStats:
        now = self.clock()
        stored = [s for s in (self.get_state(TRENDING_KEY) or "").split(",") if s]
        candidates = [*self.settings.watchlist_symbols, *stored]
        symbols = pick_symbols(
            self.engine,
            READ_PREFIX,
            candidates,
            self.settings.stocktwits_symbols_per_cycle,
            now,
            MIN_GAP,
        )
        steps: list[tuple[str, Callable[[], None]]] = []
        if self.trending_due(now):
            steps.append(("trending", self.read_trending))
        steps += [(f"${s}", partial(self.read_stream, s)) for s in symbols]
        return self.run_steps(steps)

    def trending_due(self, now: datetime) -> bool:
        last = self.get_state(TRENDING_AT_KEY)
        if last is None:
            return True
        hours = self.settings.stocktwits_trending_hours
        return now - datetime.fromisoformat(last) >= timedelta(hours=hours)

    def read_trending(self) -> None:
        result = self.fetch(TRENDING_URL, "trending")
        if result.status == 404:
            return
        symbols = [normalize_symbol(s) for s in parse_trending(result.data)]
        listed = [s for s in dict.fromkeys(symbols) if s in self.universe]
        self.set_state(TRENDING_KEY, ",".join(listed))
        self.set_state(TRENDING_AT_KEY, self.clock().isoformat())

    def read_stream(self, symbol: str) -> None:
        key = SINCE_PREFIX + symbol
        cursor = self.get_state(key)
        since = int(cursor) if cursor else None
        result = self.fetch(stream_url(symbol, since), "stream")
        if result.status == 404:
            return  # not a symbol StockTwits knows
        posts = parse_stream(result.data, symbol)
        self.ingest(posts)
        ids = [int(p.external_id) for p in posts]
        newest = max([since or 0, *ids])
        if newest:
            self.set_state(key, str(newest))


def crawl(
    settings: Settings,
    engine: Engine,
    ingestor: Ingestor,
    browser: Browser,
    throttle: Throttle,
) -> FeedRunStats:
    """One cycle's StockTwits crawl for the stock domain."""
    return StockTwitsScraper(browser, throttle, ingestor, engine, settings).run()
