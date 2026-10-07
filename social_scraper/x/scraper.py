"""The X crawl: a few page loads per cycle, split between discovery and timelines.

X is the platform that bans, so it gets the thinnest budget (see `Settings.x_*`):
one page load is one search or one profile visit, followed by a handful of slow,
jittered scrolls, and a cycle is `X_PAGES_PER_CYCLE` of those. Tweets come from the
data the page loads for itself (GraphQL XHRs and the server-streamed first page) --
never from the DOM.

Search needs a signed-in profile; profile timelines do not (verified 2026-09-28: a
logged-out `x.com/<handle>` streams its latest tweets). So with no login the scraper
still follows `X_SEED_ACCOUNTS` and anything already tracked -- no account at risk.

What gets stored:
- every search result (the query chose them, so they are on-topic);
- from a timeline, only the on-topic tweets (`is_relevant`) -- the rest are counted,
  for the account's relevance ratio, and discarded.

`XCrawler` is the loop; a domain subclass says what to search and what is on-topic.
`XScraper` is the stock one (cashtags); `sports.x.SportsXScraper` the sports one.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from sqlalchemy import Table
from sqlalchemy.engine import Connection, Engine

from social_scraper.db import repository, schema
from social_scraper.fetch import Browser, FetchFailed, PageResult
from social_scraper.models import Post
from social_scraper.pipeline import Ingestor, IngestStats, PostIngestor
from social_scraper.symbols.extract import extract_mentions
from social_scraper.throttle import (
    CHALLENGED,
    LOGIN_REQUIRED,
    OK,
    RATE_LIMITED,
    OutOfTime,
    Throttle,
)
from social_scraper.x import accounts, discovery
from social_scraper.x.parse import X_BASE, parse_payloads

log = logging.getLogger(__name__)

SEARCH_OPS = ("SearchTimeline",)
# A signed-in profile load fetches its Posts tab as `UserOriginalsTimeline` (captured
# 2026-10-03, when `UserTweets` stopped firing and every timeline visit came back empty);
# the older names stay for the Replies tab and pagination.
TIMELINE_OPS = ("UserOriginalsTimeline", "UserTweets", "UserTweetsAndReplies")
# `/i/jf/onboarding/web?...mode=login` is where a logged-out search lands (2026-09-28).
_LOGIN_PATHS = (
    "/login",
    "/i/flow/login",
    "/i/flow/signup",
    "/i/jf/onboarding",
    "redirect_after_login",
)
_LOCKED_PATHS = ("/account/access", "/account/suspended")
_RATE_TEXT = ("rate limit exceeded", "something went wrong. try reloading")
# What X's profile page says in place of a timeline when the account itself is why there
# is none: deleted or renamed, suspended, protected. X prints a curly apostrophe, which
# `classify` folds to a straight one.
_CURLY_APOSTROPHE = chr(0x2019)
_UNAVAILABLE_TEXT = (
    "this account doesn't exist",
    "account suspended",
    "these posts are protected",
    "these tweets are protected",
)
SEARCHED_PREFIX = "x:searched:"
QUERY_INDEX_KEY = "x:generic_query_index"
TRENDING_WINDOW = timedelta(hours=24)

# A page load that captured no timeline (or search) data at all.
EMPTY = "empty"
# A profile X served normally, saying the account is gone or protected.
UNAVAILABLE = "unavailable"
# Empty profile loads in a row, across accounts, at which X's layout -- not the accounts
# -- is the cause: on 2026-10-03 a renamed op emptied every profile. One empty profile
# among loaded ones is that account (2026-10-06: one of two, the other read fine).
# Searches count the same way, on a streak of their own: one `$MSFT` search came back
# empty among some seventy that read (2026-10-07), and failed the scrape.
LAYOUT_STREAK = 2
# Empty visits in a row after which a tracked account is dropped.
MAX_EMPTY_VISITS = 2
EMPTY_STREAK_PREFIX = "x:empty_timeline_streak:"
EMPTY_SEARCH_STREAK_PREFIX = "x:empty_search_streak:"
EMPTY_VISITS_PREFIX = "x:empty_visits:"


def graphql_filter(ops: tuple[str, ...]) -> Callable[[str], bool]:
    return lambda url: "/graphql/" in url and any(f"/{op}" in url for op in ops)


def search_url(query: str, latest: bool) -> str:
    return f"{X_BASE}/search?q={quote(query)}&src=typed_query&f={'live' if latest else 'top'}"


def classify(result: PageResult) -> tuple[str, str]:
    """What an X page load means for the throttle: `(outcome, detail)`."""
    url, text = result.final_url, result.text.lower().replace(_CURLY_APOSTROPHE, "'")
    if any(p in url for p in _LOGIN_PATHS):
        return LOGIN_REQUIRED, "not logged in -- run `uv run social-scraper login x`"
    if any(p in url for p in _LOCKED_PATHS):
        return CHALLENGED, f"account locked or challenged ({url})"
    if any(p.status == 429 for p in result.payloads):
        return RATE_LIMITED, "GraphQL 429"
    if not result.payloads:
        if any(marker in text for marker in _RATE_TEXT):
            return RATE_LIMITED, "rate-limit page"
        if any(marker in text for marker in _UNAVAILABLE_TEXT):
            return UNAVAILABLE, "account unavailable (deleted, suspended or protected)"
        return EMPTY, "no timeline data captured (layout change or empty page)"
    return OK, ""


@dataclass
class XRunStats:
    requests: int = 0
    searches: int = 0
    timelines: int = 0
    new_accounts: int = 0
    ingest: IngestStats = field(default_factory=IngestStats)
    errors: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class XPlan:
    watchlist: tuple[str, ...] = ()
    queries: tuple[str, ...] = ()
    pages: int = 8
    scrolls: int = 3
    search_share: float = 0.4
    max_tracked: int = 150
    min_followers: int = 500


class XCrawler:
    """The X cycle, whatever the domain. Subclasses supply `queries` and `is_relevant`."""

    accounts_table: Table = schema.x_accounts

    def __init__(
        self,
        browser: Browser,
        throttle: Throttle,
        ingestor: PostIngestor,
        engine: Engine,
        plan: XPlan,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.browser = browser
        self.throttle = throttle
        self.ingestor = ingestor
        self.engine = engine
        self.plan = plan
        self.clock = clock
        self.stats = XRunStats()

    def queries(self, n: int) -> list[tuple[str, bool]]:
        """`n` searches as `(query, latest)`; records whatever rotation state it needs."""
        raise NotImplementedError

    def is_relevant(self, post: Post) -> bool:
        """Whether a timeline tweet is on-topic, so worth storing."""
        raise NotImplementedError

    def is_tout(self, post: Post) -> bool:
        """Whether a tweet sells picks. Only the sports domain has touts."""
        return False

    def run(self) -> XRunStats:
        """One cycle. `ThrottleStop` propagates; a wall or rate limit ends the cycle."""
        now = self.clock()
        with self.engine.connect() as conn:
            rows = accounts.load(conn, self.accounts_table)
        tracked = [r for r in rows if r.status in (discovery.TRACKED, discovery.SEED)]
        n_search, n_timeline = discovery.split_budget(
            self.plan.pages, self.plan.search_share, bool(tracked)
        )
        searches = self.queries(n_search)
        visits = discovery.pick_timelines(rows, now, n_timeline)
        log.info("x cycle: %d searches, %d timelines", len(searches), len(visits))
        try:
            self._execute([*searches, *visits])
        finally:
            self.reconcile()
        return self.stats

    def _execute(self, steps: list[tuple[str, bool] | discovery.AccountRow]) -> None:
        """Run the planned page loads in order.

        A login wall on a *search* skips the remaining searches only: profile timelines
        are readable logged out, so seeds and tracked accounts still get visited. Any
        other wall -- or a login wall on a timeline -- ends the cycle, and so does the
        cycle's deadline, as a notice.
        """
        searching = True
        for step in steps:
            is_visit = isinstance(step, discovery.AccountRow)
            if not (is_visit or searching):
                continue
            try:
                if isinstance(step, discovery.AccountRow):
                    self.timeline(step)
                else:
                    self.search(*step)
            except OutOfTime as exc:
                self.stats.notices.append(str(exc))
                return
            except FetchFailed as exc:
                if exc.outcome == LOGIN_REQUIRED and not is_visit:
                    # Running logged out is a supported mode, not a failure: say it once.
                    self.stats.notices.append(f"search skipped -- {exc.detail}")
                    searching = False
                    continue
                self.stats.errors.append(str(exc))
                if exc.outcome in (CHALLENGED, RATE_LIMITED, LOGIN_REQUIRED):
                    return

    def search(self, query: str, latest: bool) -> None:
        try:
            result = self.fetch(search_url(query, latest), SEARCH_OPS, "search")
        except FetchFailed as exc:
            if exc.outcome != EMPTY:
                raise
            self.empty_search(exc)
            return
        posts = parse_payloads([p.data for p in result.payloads], channel="search")
        self.stats.searches += 1
        self.stats.ingest.add(self.ingestor.ingest(posts))
        with self.engine.begin() as conn:
            self._reset_states(conn, EMPTY_SEARCH_STREAK_PREFIX + self.accounts_table.name)
            self.stats.new_accounts += accounts.observe(
                conn, posts, accounts.SEARCH, self.clock(), self.accounts_table
            )

    def timeline(self, account: discovery.AccountRow) -> None:
        try:
            result = self.fetch(f"{X_BASE}/{quote(account.handle)}", TIMELINE_OPS, "timeline")
        except FetchFailed as exc:
            if exc.outcome not in (EMPTY, UNAVAILABLE):
                raise
            self.empty_timeline(account, exc)
            return
        self.read_timeline(account, result)

    def read_timeline(self, account: discovery.AccountRow, result: PageResult) -> None:
        """Store a loaded profile's on-topic tweets and add the visit to its evidence."""
        posts = parse_payloads([p.data for p in result.payloads], channel="timeline")
        handle = account.handle.lower()
        own = [p for p in posts if (p.author.name or "").lower() == handle]
        relevant = [p for p in posts if self.is_relevant(p)]
        relevant_ids = {p.external_id for p in relevant}
        self.stats.timelines += 1
        self.stats.ingest.add(self.ingestor.ingest(relevant))
        table = self.accounts_table
        with self.engine.begin() as conn:
            self._clear_empty(conn, account)
            user_id = account.user_id
            if own and own[0].author.user_id:
                user_id = accounts.resolve_seed(conn, user_id, own[0].author.user_id, table)
            sample = accounts.TimelineSample(
                tweets=len(own),
                relevant=sum(p.external_id in relevant_ids for p in own),
                touts=sum(self.is_tout(p) for p in own),
            )
            accounts.record_visit(conn, user_id, sample, self.clock(), table)
            amplified = [p for p in relevant if (p.author.name or "").lower() != handle]
            self.stats.new_accounts += accounts.observe(
                conn, amplified, accounts.AMPLIFIED, self.clock(), table
            )

    def empty_timeline(self, account: discovery.AccountRow, exc: FetchFailed) -> None:
        """A profile that showed no timeline: the account's problem, unless it keeps happening.

        One dead account must not fail the cycle, nor be picked first again next cycle
        (an unrecorded visit leaves it the most overdue). So the visit is recorded, and a
        tracked account is dropped once X says it is gone or it comes back empty
        `MAX_EMPTY_VISITS` times. `LAYOUT_STREAK` empty profiles in a row, whoever they
        belong to, are X's layout: an error, held against no account.
        """
        now, table = self.clock(), self.accounts_table
        streak_key = EMPTY_STREAK_PREFIX + table.name
        misses_key = f"{EMPTY_VISITS_PREFIX}{table.name}:{account.user_id}"
        with self.engine.begin() as conn:
            if exc.outcome == EMPTY:
                streak = int(repository.get_state(conn, streak_key) or 0) + 1
                repository.set_state(conn, streak_key, str(streak), now)
                if streak >= LAYOUT_STREAK:
                    self.stats.errors.append(
                        f"error: {streak} profile loads in a row captured no timeline "
                        f"(X's layout, not the accounts) <- {X_BASE}/{account.handle}"
                    )
                    return
                misses = int(repository.get_state(conn, misses_key) or 0) + 1
                repository.set_state(conn, misses_key, str(misses), now)
                gone = misses >= MAX_EMPTY_VISITS
            else:
                gone = True
            accounts.record_visit(conn, account.user_id, accounts.TimelineSample(0, 0), now, table)
            note = f"@{account.handle}: {exc.detail}"
            if gone and account.status == discovery.TRACKED:
                score = discovery.score_account(account)
                accounts.apply(conn, {account.user_id: (discovery.DROPPED, score)}, table)
                note += " -- dropped"
            elif gone:
                note += " -- a seed: take it out of the seed list"
        self.stats.notices.append(note)

    def empty_search(self, exc: FetchFailed) -> None:
        """A search that captured nothing: a notice once, X's layout `LAYOUT_STREAK` in a row.

        The streak spans cycles, as the profiles' does, so a renamed search op still
        fails the scrape on its second cycle rather than going quiet.
        """
        key = EMPTY_SEARCH_STREAK_PREFIX + self.accounts_table.name
        with self.engine.begin() as conn:
            streak = int(repository.get_state(conn, key) or 0) + 1
            repository.set_state(conn, key, str(streak), self.clock())
        if streak < LAYOUT_STREAK:
            self.stats.notices.append(f"search: {exc.detail}")
            return
        url = exc.detail.rpartition(" <- ")[2]
        self.stats.errors.append(
            f"{EMPTY}: {streak} searches in a row captured no results "
            f"(X's layout, not the queries) <- {url}"
        )

    def _clear_empty(self, conn: Connection, account: discovery.AccountRow) -> None:
        """A profile read fine: the empty-profile streak and this account's misses end."""
        self._reset_states(
            conn,
            EMPTY_STREAK_PREFIX + self.accounts_table.name,
            f"{EMPTY_VISITS_PREFIX}{self.accounts_table.name}:{account.user_id}",
        )

    def _reset_states(self, conn: Connection, *keys: str) -> None:
        """Zero each counter in `keys` that is set, writing nothing for the rest."""
        for key in keys:
            if repository.get_state(conn, key) not in (None, "0"):
                repository.set_state(conn, key, "0", self.clock())

    def reconcile(self) -> None:
        with self.engine.begin() as conn:
            rows = accounts.load(conn, self.accounts_table)
            accounts.apply(
                conn,
                discovery.reconcile(rows, self.plan.max_tracked, self.plan.min_followers),
                self.accounts_table,
            )

    def fetch(self, url: str, ops: tuple[str, ...], kind: str) -> PageResult:
        """One throttled page load with scrolling. Raises `FetchFailed` on a wall."""
        self.throttle.acquire()
        result = self.browser.capture(
            url, graphql_filter(ops), self.plan.scrolls, self.throttle.dwell
        )
        self.stats.requests += 1
        outcome, detail = classify(result)
        # X answering "this account is gone" is X working: no strike against the platform.
        self.throttle.record(OK if outcome == UNAVAILABLE else outcome, kind, result.status, detail)
        if outcome != OK:
            raise FetchFailed(outcome, f"{detail} <- {url}")
        return result


class XScraper(XCrawler):
    """The stock domain: cashtag searches, and a tweet is on-topic when it names a symbol."""

    def __init__(
        self,
        browser: Browser,
        throttle: Throttle,
        ingestor: Ingestor,
        engine: Engine,
        plan: XPlan,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        super().__init__(browser, throttle, ingestor, engine, plan, clock)
        self.universe = ingestor.universe

    def queries(self, n: int) -> list[tuple[str, bool]]:
        """`n` searches: alternate symbol cashtags (latest) with generic queries (top)."""
        if n <= 0:
            return []
        now = self.clock()
        with self.engine.connect() as conn:
            trending = [s for s, _ in repository.trending_symbols(conn, now - TRENDING_WINDOW, 30)]
            last = {
                k: datetime.fromisoformat(v)
                for k, v in repository.states_with_prefix(conn, SEARCHED_PREFIX).items()
            }
            index = int(repository.get_state(conn, QUERY_INDEX_KEY) or 0)
        n_generic = n // 2 if self.plan.queries else 0
        symbols = discovery.pick_symbols(
            [*self.plan.watchlist, *trending], last, now, n - n_generic
        )
        n_generic = n - len(symbols) if self.plan.queries else 0
        generic = [
            self.plan.queries[(index + i) % len(self.plan.queries)] for i in range(n_generic)
        ]
        out = [(f"${s} lang:en", True) for s in symbols] + [(q, False) for q in generic]
        with self.engine.begin() as conn:
            repository.set_state(conn, QUERY_INDEX_KEY, str(index + n_generic), now)
            for s in symbols:
                repository.set_state(conn, SEARCHED_PREFIX + s, now.isoformat(), now)
        return out

    def is_relevant(self, post: Post) -> bool:
        return bool(extract_mentions(post.text, self.universe, post.tagged_symbols))
