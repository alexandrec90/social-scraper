"""The sports X crawl: the stock loop (`x.scraper.XCrawler`) with sports subjects.

- **Searches**: the most-mentioned teams and athletes of the last day, and the leagues'
  generic queries. Each search's results are stored as about that entity's (or that
  query's) leagues, which also lets one-word names in them resolve.
- **On-topic**: a timeline tweet that names a known entity.
- **Reputation**: the shared discovery loop (`x.discovery`) with two sports-only
  signals -- an organisation's account (affiliate label, Business/Government check)
  scores higher, and an account whose own tweets keep selling picks is dropped
  (`is_tout`). The leagues' own accounts, from Wikidata, are seeds: never dropped.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.engine import Engine

from social_scraper.db import repository, schema
from social_scraper.fetch import Browser
from social_scraper.models import Post
from social_scraper.sports import registry
from social_scraper.sports.leagues import League
from social_scraper.sports.ingest import SportsIngestor
from social_scraper.sports.targets import entity_query
from social_scraper.throttle import Throttle
from social_scraper.x import discovery
from social_scraper.x.scraper import XCrawler, XPlan

SEARCHED_PREFIX = "sports:x:searched:"
QUERY_INDEX_KEY = "sports:x:generic_query_index"
TRENDING_WINDOW = timedelta(hours=24)
TRENDING_LIMIT = 30

# Pick sellers' tells, in English and French. Betting talk itself is on-topic -- the
# operator bets -- so "parlay" or "odds" alone is not here; selling is.
_TOUT = re.compile(
    r"""
    \bdm\s+(?:me\s+)?(?:for|4)\s+(?:picks|plays|vip)\b
    | \b(?:free|vip|premium|paid)\s+(?:picks?|plays?|parlays?|tips?)\b
    | \b(?:lock|pick|play)\s+of\s+the\s+(?:day|night|week)\b
    | \bpotd\b | \bmax\s+bet\b | \btail\s+(?:me|this|us)\b
    | \b\d+(?:\.\d+)?\s?(?:u|units?)\b
    | discord\.gg/ | t\.me/ | whop\.com/ | linktr\.ee/
    | \bjoin\s+(?:my|our|the)\s+(?:vip|discord|telegram|group)\b
    | \bpronos?tics?\s+(?:gratuits?|vip)\b
    """,
    re.IGNORECASE | re.VERBOSE,
)


def is_tout_text(text: str) -> bool:
    return bool(_TOUT.search(text))


@dataclass(frozen=True)
class SportsXPlan(XPlan):
    """`XPlan` plus the leagues whose generic queries the crawl rotates through."""

    leagues: tuple[League, ...] = ()


class SportsXScraper(XCrawler):
    accounts_table = schema.sports_x_accounts

    def __init__(
        self,
        browser: Browser,
        throttle: Throttle,
        ingestor: SportsIngestor,
        engine: Engine,
        plan: SportsXPlan,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        super().__init__(browser, throttle, ingestor, engine, plan, clock)
        self.sports = ingestor
        self.generic = [(q, lg.context) for lg in plan.leagues for q in lg.x_queries]
        # query -> the leagues its results are about, filled by `queries`.
        self.contexts: dict[str, frozenset[str]] = {}

    def queries(self, n: int) -> list[tuple[str, bool]]:
        """`n` searches: trending entities (latest) alternating with league queries (top)."""
        if n <= 0:
            return []
        now = self.clock()
        with self.engine.connect() as conn:
            trending = registry.trending(conn, now - TRENDING_WINDOW, TRENDING_LIMIT)
            last = {
                k: datetime.fromisoformat(v)
                for k, v in repository.states_with_prefix(conn, SEARCHED_PREFIX).items()
            }
            index = int(repository.get_state(conn, QUERY_INDEX_KEY) or 0)
        names = {eid: name for eid, name, _ in trending}
        n_generic = n // 2 if self.generic else 0
        picked = discovery.pick_symbols(list(names), last, now, n - n_generic)
        n_generic = n - len(picked) if self.generic else 0
        out: list[tuple[str, bool]] = []
        for eid in picked:
            query = entity_query(names[eid])
            self.contexts[query] = self.sports.matcher.leagues_of([eid])
            out.append((query, True))
        for i in range(n_generic):
            query, context = self.generic[(index + i) % len(self.generic)]
            self.contexts[query] = context
            out.append((query, False))
        with self.engine.begin() as conn:
            repository.set_state(conn, QUERY_INDEX_KEY, str(index + n_generic), now)
            for eid in picked:
                repository.set_state(conn, SEARCHED_PREFIX + eid, now.isoformat(), now)
        return out

    def search(self, query: str, latest: bool) -> None:
        self.sports.context = self.contexts.get(query, frozenset())
        try:
            super().search(query, latest)
        finally:
            self.sports.context = frozenset()

    def is_relevant(self, post: Post) -> bool:
        return self.sports.is_relevant(post)

    def is_tout(self, post: Post) -> bool:
        return is_tout_text(post.text)
