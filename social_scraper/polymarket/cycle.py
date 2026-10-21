"""The Polymarket crawl: open markets by tag, then the overdue ones by id until they resolve.

Read from the Gamma API (`gamma-api.polymarket.com`) with no key or account, through
`apiclient.ApiClient` -- measured 2026-10-10. One cycle:

1. **By tag.** Of the stock tags (`POLYMARKET_STOCK_TAGS`) and the leagues'
   `polymarket_tags`, the `POLYMARKET_TAGS_PER_CYCLE` read longest ago: the open events
   under each, the most traded in the last day first, `POLYMARKET_PAGES_PER_TAG` pages of
   `PAGE_SIZE`. Of their markets, those traded in the last day are upserted and priced.
   An event under two tags (`nhl` and `sports`) is stored once a cycle.

   Why rotate, why small pages, why only traded markets -- the first live cycle
   (2026-10-10): one `nfl` page of 100 events was 33.6 MB and 10,371 markets, 9,472 of
   them with no trade that day (season-stat and player-prop lines), and all tags at 100
   events came to some 45,000 markets a cycle. Read whole every 30 minutes, that is
   gigabytes a day and a million price rows, nearly all of them for markets nobody
   trades.
2. **Overdue.** A market past its end that is not yet closed drops out of the open
   listings, so the result of a game would never be seen. Up to
   `POLYMARKET_RESOLVE_PER_CYCLE` such events are re-read by id (`/events?id=..&id=..`,
   which answers several at once), until they close with their winner.

A **404** is not "nothing here" on Gamma, as it is on the feeds `FeedCrawler` was built
for: an `/events` query that matches nothing answers 200 `[]` (`?id=999999999`, measured
2026-10-21). On 2026-10-21 15:35Z Gamma answered 404 to every load of a cycle for a few
seconds, so a 404 is Gamma not serving, read like a timeout (`classify`): a notice until
`ERROR_STREAK` loads in a row have failed, then an error that ends the crawl.

Which domains run decides which tags are read: stock tags only with `stocks` in
`DOMAINS`, league tags only with `sports`. Markets are not posts, so nothing here goes
through an ingestor: `polymarket.store` writes its own two tables.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Any
from urllib.parse import urlencode

from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from social_scraper.config import SPORTS, STOCKS, Settings
from social_scraper.db import repository
from social_scraper.db.throttle_store import DbThrottleStore
from social_scraper.feeds import FeedCrawler, FeedRunStats, classify
from social_scraper.fetch import Browser, PageResult
from social_scraper.models import POLYMARKET, Post
from social_scraper.pipeline import IngestStats
from social_scraper.polymarket import store
from social_scraper.polymarket.parse import Market, parse_events
from social_scraper.sports.leagues import League
from social_scraper.symbols.universe import Universe
from social_scraper.throttle import TIMED_OUT, Policy, Throttle, ThrottleStop

log = logging.getLogger(__name__)

HOST = "gamma-api.polymarket.com"
EVENTS_URL = f"https://{HOST}/events"
PAGE_SIZE = 50
# Event ids per by-id lookup: a URL of 20 short ids stays far below any length limit.
IDS_PER_LOOKUP = 20
READ_PREFIX = "polymarket:read:"

BrowserFactory = Callable[[str], Any]
PolicyFor = Callable[[Settings, str], Policy]


def tag_url(tag: str, offset: int) -> str:
    query = {
        "tag_slug": tag,
        "active": "true",
        "closed": "false",
        "order": "volume24hr",
        "ascending": "false",
        "limit": PAGE_SIZE,
        "offset": offset,
    }
    return f"{EVENTS_URL}?{urlencode(query)}"


def ids_url(event_ids: list[str]) -> str:
    return f"{EVENTS_URL}?{urlencode([('id', e) for e in event_ids])}"


@dataclass
class PolymarketRunStats(FeedRunStats):
    markets: int = 0
    snapshots: int = 0
    resolved_lookups: int = 0


class _NoPosts:
    """The ingestor slot `FeedCrawler` has, for a crawl that stores no posts."""

    def ingest(self, posts: Iterable[Post]) -> IngestStats:
        return IngestStats()


@dataclass(frozen=True)
class Plan:
    """What a crawl reads (`scope`): how many tags (0 is all), how deep per tag, how
    many overdue events, and the 24-hour volume a market must be past to be stored."""

    scope: store.Scope
    pages_per_tag: int = 1
    resolve_limit: int = 0
    tags_per_cycle: int = 0
    min_volume_24h: float = 0.0


def traded(market: Market, min_volume: float) -> bool:
    """Whether a market changed hands in the last day, by more than `min_volume`."""
    return market.volume_24hr is not None and market.volume_24hr > min_volume


class PolymarketCrawler(FeedCrawler):
    platform = POLYMARKET
    stats: PolymarketRunStats

    def __init__(
        self,
        browser: Browser,
        throttle: Throttle,
        engine: Engine,
        plan: Plan,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        super().__init__(browser, throttle, _NoPosts(), engine, clock)
        self.plan = plan
        self.scope = plan.scope
        self.pages_per_tag = plan.pages_per_tag
        self.resolve_limit = plan.resolve_limit
        self.stats = PolymarketRunStats()
        self.seen_events: set[str] = set()

    def tags(self) -> list[tuple[str, str]]:
        """`(tag, domain)` for every tag the scope reads, each tag once."""
        out: dict[str, str] = {}
        for tag in sorted(self.scope.stock_tags):
            out.setdefault(tag, STOCKS)
        for league in self.scope.leagues:
            for tag in league.polymarket_tags:
                out.setdefault(tag, SPORTS)
        return list(out.items())

    def pick_tags(self) -> list[tuple[str, str]]:
        """`tags_per_cycle` of `tags()`, the ones read longest ago (never read first),
        stamped as read now. All of them when `tags_per_cycle` is 0."""
        every = self.tags()
        n = self.plan.tags_per_cycle
        if n <= 0 or n >= len(every):
            picked = every
        else:
            with self.engine.connect() as conn:
                last = repository.states_with_prefix(conn, READ_PREFIX)
            # ISO stamps sort as times; "" (never read) sorts first; ties keep list order.
            picked = sorted(every, key=lambda td: last.get(td[0], ""))[:n]
        now = self.clock()
        with self.engine.begin() as conn:
            for tag, _ in picked:
                repository.set_state(conn, READ_PREFIX + tag, now.isoformat(), now)
        return picked

    def run(self) -> PolymarketRunStats:
        steps: list[tuple[str, Callable[[], None]]] = [
            (f"tag:{tag}", partial(self.read_tag, tag, domain)) for tag, domain in self.pick_tags()
        ]
        if self.resolve_limit > 0:
            steps.append(("overdue", self.resolve))
        self.run_steps(steps)
        return self.stats

    def classify(self, result: PageResult, expect: str) -> tuple[str, str]:
        if result.status == 404:
            return TIMED_OUT, "HTTP 404 (Gamma not serving: no match is a 200 [])"
        return classify(result, expect)

    def read_tag(self, tag: str, domain: str) -> None:
        for page in range(self.pages_per_tag):
            result = self.fetch(tag_url(tag, page * PAGE_SIZE), f"events:{domain}")
            payload = result.data
            events = payload if isinstance(payload, list) else None
            self.write(payload, domain, traded_only=True)
            if events is None or len(events) < PAGE_SIZE:
                return

    def resolve(self) -> None:
        with self.engine.connect() as conn:
            overdue = store.pending_events(conn, self.clock(), self.resolve_limit)
        overdue = [e for e in overdue if e not in self.seen_events]
        for i in range(0, len(overdue), IDS_PER_LOOKUP):
            result = self.fetch(ids_url(overdue[i : i + IDS_PER_LOOKUP]), "events:overdue")
            self.stats.resolved_lookups += 1
            # The domain a market already has wins (`market_row`); this is the fallback.
            # Not `traded_only`: an overdue market is read for its result, traded or not.
            self.write(result.data, STOCKS if self.scope.stock_tags else SPORTS)

    def write(self, payload: Any, domain: str, traded_only: bool = False) -> None:
        """Store the markets of a page's events not already stored this cycle (with
        `traded_only`, only those traded in the last day). A page that is not an
        events list raises `ValueError` (an error for the step)."""
        parsed = [m for m in parse_events(payload) if m.event_id not in self.seen_events]
        floor = self.plan.min_volume_24h
        markets = [m for m in parsed if traded(m, floor)] if traded_only else parsed
        with self.engine.begin() as conn:
            stored, snapshots = store.store(conn, markets, self.scope, domain, self.clock())
        self.seen_events.update(m.event_id for m in parsed)
        self.stats.markets += stored
        self.stats.snapshots += snapshots


def scope_for(
    settings: Settings, leagues: tuple[League, ...] | None, universe: Universe | None
) -> store.Scope:
    """What the enabled domains read: stock tags with a universe, league tags with leagues."""
    return store.Scope(
        stock_tags=frozenset(settings.polymarket_stock_tag_list)
        if universe is not None
        else frozenset(),
        leagues=leagues or (),
        universe=universe,
    )


def run_platform(
    settings: Settings,
    engine: Engine,
    factory: BrowserFactory,
    policy_for: PolicyFor,
    leagues: tuple[League, ...] | None,
    universe: Universe | None,
) -> dict[str, Any]:
    """One Polymarket crawl for whichever domains run; what goes wrong lands in the dict.

    `leagues` is None when the sports domain is off, `universe` when stocks is."""
    scope = scope_for(settings, leagues, universe)
    if not scope.stock_tags and not any(lg.polymarket_tags for lg in scope.leagues):
        return {
            "skipped": "no Polymarket tags: POLYMARKET_STOCK_TAGS empty or stocks off, "
            "and no league lists `polymarket_tags` (or sports off)"
        }
    throttle = Throttle(policy_for(settings, POLYMARKET), DbThrottleStore(engine))
    try:
        throttle.check()
        with factory(POLYMARKET) as client:
            plan = Plan(
                scope,
                pages_per_tag=settings.polymarket_pages_per_tag,
                resolve_limit=settings.polymarket_resolve_per_cycle,
                tags_per_cycle=settings.polymarket_tags_per_cycle,
                min_volume_24h=settings.polymarket_min_volume_24h,
            )
            stats = PolymarketCrawler(client, throttle, engine, plan).run()
    except ThrottleStop as exc:
        return {"skipped": str(exc)}
    except (SQLAlchemyError, OSError) as exc:
        log.exception("polymarket crawl failed")
        return {"error": f"{type(exc).__name__}: {exc}"}
    out = asdict(stats)
    if stats.errors:
        out["error"] = "; ".join(stats.errors[:5])
    return out
