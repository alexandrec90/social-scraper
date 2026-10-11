"""Bluesky, live: `searchPosts` on the public AppView, for both domains. No account.

Measured 2026-10-10: `https://api.bsky.app/xrpc/app.bsky.feed.searchPosts?q=%24TSLA&
sort=latest&limit=N` answers signed out (`public.api.bsky.app` answered 403). The answer
is `{"posts": [...], "cursor": ...}`; each post carries `uri`
(`at://did:plc:.../app.bsky.feed.post/<rkey>`), `author.did`, `record` (`createdAt`,
`text`, `langs`, `facets` -- hashtags are `app.bsky.richtext.facet#tag` features --
and `reply.root`/`reply.parent` on a reply), the reply/repost/like/quote/bookmark
counts and `indexedAt`. `since` and `until` (ISO datetimes) bound a search, and each
search here passes the newest `createdAt` it saw last time as `since`.

**No identifier leaves unhashed** (`.claude/rules/data-lake.md`). A post's URI, and the
bsky.app URL built from it, contain its author's DID, so `external_id` is the
`stable_hash` of the URI, a reply's `parent_id` and `raw.conversation_id` are hashes of
its parent's and its thread root's URIs, the author is `user_id=did` (hashed like every
author), and `url` is None. Rows join among themselves; none links back to a profile.

**Stocks**: the watchlist, then the symbols trending everywhere, searched as `$SYM`.
Search ignores the `$` -- `$TSLA` also matched "TSLA sentiment rose", with no cashtag
(2026-10-10) -- and Bluesky is general-topic (`backfill.lake_post_strict`), so a result
is kept only when it writes an explicit, listed `$TICKER` (`has_strict_cashtag`) or
tags the searched symbol as an upper-case hashtag (`#TSLA`), which then counts as a
symbol the platform tagged.

**Sports**: the day's most-mentioned teams and athletes, searched by their quoted name,
alternating with the leagues' own `bluesky_queries` -- `sports/x.py`'s plan. A search's
results are about its entity's (or league's) leagues, which `SportsIngestor.context`
carries, as for an X search.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from sqlalchemy.engine import Engine

from social_scraper import feeds
from social_scraper.config import Settings
from social_scraper.db import repository
from social_scraper.feeds import FeedCrawler, FeedRunStats
from social_scraper.fetch import Browser
from social_scraper.models import BLUESKY, AuthorInfo, Engagement, Post, stable_hash
from social_scraper.pipeline import Ingestor
from social_scraper.sports import registry
from social_scraper.sports.ingest import SportsIngestor
from social_scraper.sports.leagues import League
from social_scraper.sports.matcher import Matcher
from social_scraper.symbols.extract import has_strict_cashtag
from social_scraper.symbols.universe import Universe
from social_scraper.throttle import Throttle
from social_scraper.x import discovery

HOST = "api.bsky.app"
SEARCH_URL = f"https://{HOST}/xrpc/app.bsky.feed.searchPosts"
PAGE_SIZE = 50
CHANNEL = "search"
HASHTAG = "app.bsky.richtext.facet#tag"

SEARCHED_PREFIX = "bluesky:searched:"
SINCE_PREFIX = "bluesky:since:"
SYMBOL_GAP = timedelta(hours=2)
SPORTS_SEARCHED_PREFIX = "sports:bluesky:searched:"
SPORTS_SINCE_PREFIX = "sports:bluesky:since:"
SPORTS_QUERY_INDEX_KEY = "sports:bluesky:query_index"
TRENDING_WINDOW = timedelta(hours=24)
TRENDING_LIMIT = 30


def search_url(query: str, since: str | None = None) -> str:
    params = {"q": query, "sort": "latest", "limit": PAGE_SIZE}
    if since:
        params["since"] = since
    return f"{SEARCH_URL}?{urlencode(params)}"


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def hashtags(record: dict[str, Any]) -> tuple[str, ...]:
    """The hashtags a post's facets carry, as written (`#TSLA` -> `TSLA`)."""
    out = []
    for facet in record.get("facets") or []:
        for feature in (facet or {}).get("features") or []:
            if (feature or {}).get("$type") == HASHTAG and isinstance(feature.get("tag"), str):
                out.append(feature["tag"])
    return tuple(out)


def _ref(reply: dict[str, Any], which: str) -> str | None:
    uri = ((reply or {}).get(which) or {}).get("uri")
    return stable_hash(uri) if isinstance(uri, str) and uri else None


def _engagement(item: dict[str, Any]) -> Engagement:
    likes = _int(item.get("likeCount"))
    return Engagement(
        score=likes,
        likes=likes,
        comments=_int(item.get("replyCount")),
        reposts=_int(item.get("repostCount")),
        quotes=_int(item.get("quoteCount")),
        bookmarks=_int(item.get("bookmarkCount")),
    )


def _author(item: dict[str, Any]) -> AuthorInfo:
    """The DID as the stable id, hashed like every author's; no handle, no name."""
    did = (item.get("author") or {}).get("did")
    return AuthorInfo(name=None, user_id=did if isinstance(did, str) and did else None)


def _lang(record: dict[str, Any]) -> str | None:
    langs = record.get("langs") or []
    return langs[0] if langs and isinstance(langs[0], str) else None


def _readable(item: dict[str, Any]) -> tuple[str, dict[str, Any], str, datetime] | None:
    """`(uri, record, text, created)` of a post with text and a time, else None."""
    uri, record = item.get("uri"), item.get("record")
    if not isinstance(uri, str) or not uri or not isinstance(record, dict):
        return None
    text = record.get("text")
    created = _time(record.get("createdAt")) or _time(item.get("indexedAt"))
    if not isinstance(text, str) or not text.strip() or created is None:
        return None
    return uri, record, text, created


def parse_post(item: dict[str, Any]) -> Post | None:
    """One search result as a `Post`, or None when it is not a readable post."""
    readable = _readable(item)
    if readable is None:
        return None
    uri, record, text, created = readable
    found = record.get("reply")
    reply: dict[str, Any] = found if isinstance(found, dict) else {}
    root = _ref(reply, "root")
    return Post(
        platform=BLUESKY,
        external_id=stable_hash(uri),
        kind="reply" if reply else "post",
        channel=CHANNEL,
        created_at=created,
        author=_author(item),
        body=text,
        url=None,
        parent_id=_ref(reply, "parent"),
        lang=_lang(record),
        engagement=_engagement(item),
        raw={"conversation_id": root} if root else {},
    )


@dataclass(frozen=True)
class Result:
    """One parsed result: the post, and the hashtags it carries."""

    post: Post
    tags: tuple[str, ...]


def parse_search(payload: Any) -> list[Result]:
    """The posts of a `searchPosts` answer. Raises `ValueError` when it is not one."""
    if not isinstance(payload, dict) or not isinstance(payload.get("posts"), list):
        raise ValueError("not a searchPosts answer")
    out = []
    for item in payload["posts"]:
        if not isinstance(item, dict):
            continue
        post = parse_post(item)
        if post is not None:
            out.append(Result(post, hashtags(item.get("record") or {})))
    return out


def about_symbol(result: Result, symbol: str, universe: Universe) -> Post | None:
    """The post when it is about stocks by the strict rule, the searched symbol's
    hashtag added as tagged; None when it is not."""
    if symbol in result.tags:
        return replace(result.post, tagged_symbols=(symbol,))
    if has_strict_cashtag(result.post.text, universe):
        return result.post
    return None


class BlueskySearch(FeedCrawler):
    """Searches, each read newest first from the `since` it reached last time."""

    platform = BLUESKY

    def search(self, query: str, since_key: str) -> list[Result]:
        since = self.get_state(since_key)
        result = self.fetch(search_url(query, since), "search")
        if result.status == 404:
            return []
        found = parse_search(result.data)
        if found:
            # `createdAt` is what the author's client claims, so it can be in the future;
            # a cursor past now would hide every real post until the clock caught up.
            newest = min(max(r.post.created_at for r in found), self.clock())
            previous = _time(since)
            if previous is None or newest > previous:
                self.set_state(since_key, newest.isoformat())
        return found


class StockSearch(BlueskySearch):
    def __init__(
        self,
        browser: Browser,
        throttle: Throttle,
        ingestor: Ingestor,
        engine: Engine,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        super().__init__(browser, throttle, ingestor, engine, clock)
        self.universe = ingestor.universe

    def read_symbol(self, symbol: str) -> None:
        found = self.search(f"${symbol}", SINCE_PREFIX + symbol)
        kept = [p for r in found if (p := about_symbol(r, symbol, self.universe))]
        self.ingest(kept)


def crawl(
    settings: Settings,
    engine: Engine,
    ingestor: Ingestor,
    browser: Browser,
    throttle: Throttle,
) -> FeedRunStats:
    """The stock domain's Bluesky crawl: `BLUESKY_SEARCHES_PER_CYCLE` cashtag searches.
    Time is the throttle's clock, so a test moves one clock for both."""
    clock = throttle.clock
    crawler = StockSearch(browser, throttle, ingestor, engine, clock)
    symbols = feeds.pick_symbols(
        engine,
        SEARCHED_PREFIX,
        settings.watchlist_symbols,
        settings.bluesky_searches_per_cycle,
        clock(),
        SYMBOL_GAP,
    )
    steps = [(f"${s}", lambda s=s: crawler.read_symbol(s)) for s in symbols]
    return crawler.run_steps(steps)


@dataclass(frozen=True)
class SportsQuery:
    query: str
    key: str  # the `since` cursor's key: an entity id, or the query itself
    context: frozenset[str]


def entity_query(name: str) -> str:
    """A Bluesky search for one entity's chatter: the exact name."""
    return f'"{name}"'


def sports_queries(
    engine: Engine,
    leagues: Sequence[League],
    matcher: Matcher,
    n: int,
    now: datetime,
) -> list[SportsQuery]:
    """`n` searches: trending entities alternating with the leagues' `bluesky_queries`,
    stamped as searched (mirrors `sports.x.SportsXScraper.queries`)."""
    if n <= 0:
        return []
    generic = [(q, lg.context) for lg in leagues for q in lg.bluesky_queries]
    with engine.connect() as conn:
        trending = registry.trending(conn, now - TRENDING_WINDOW, TRENDING_LIMIT)
        last = {
            k: datetime.fromisoformat(v)
            for k, v in repository.states_with_prefix(conn, SPORTS_SEARCHED_PREFIX).items()
        }
        index = int(repository.get_state(conn, SPORTS_QUERY_INDEX_KEY) or 0)
    names = {eid: name for eid, name, _ in trending}
    n_generic = n // 2 if generic else 0
    picked = discovery.pick_symbols(list(names), last, now, n - n_generic)
    n_generic = n - len(picked) if generic else 0
    entity = [
        SportsQuery(entity_query(names[eid]), eid, matcher.leagues_of([eid])) for eid in picked
    ]
    leagues_q = [
        SportsQuery(q, q, ctx)
        for q, ctx in (generic[(index + i) % len(generic)] for i in range(n_generic))
    ]
    out: list[SportsQuery] = []
    for i in range(max(len(entity), len(leagues_q))):
        out.extend(entity[i : i + 1])
        out.extend(leagues_q[i : i + 1])
    with engine.begin() as conn:
        repository.set_state(conn, SPORTS_QUERY_INDEX_KEY, str(index + n_generic), now)
        for eid in picked:
            repository.set_state(conn, SPORTS_SEARCHED_PREFIX + eid, now.isoformat(), now)
    return out


class SportsSearch(BlueskySearch):
    def __init__(
        self,
        browser: Browser,
        throttle: Throttle,
        ingestor: SportsIngestor,
        engine: Engine,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        super().__init__(browser, throttle, ingestor, engine, clock)
        self.sports = ingestor

    def read(self, q: SportsQuery) -> None:
        found = self.search(q.query, SPORTS_SINCE_PREFIX + q.key)
        self.sports.context = q.context
        try:
            self.ingest([r.post for r in found])
        finally:
            self.sports.context = frozenset()


def crawl_sports(
    settings: Settings,
    leagues: tuple[League, ...],
    matcher: Matcher,
    engine: Engine,
    browser: Browser,
    throttle: Throttle,
) -> FeedRunStats:
    """The sports domain's Bluesky crawl: `BLUESKY_SPORTS_SEARCHES_PER_CYCLE` searches."""
    clock = throttle.clock
    ingestor = SportsIngestor(engine, matcher, clock)
    crawler = SportsSearch(browser, throttle, ingestor, engine, clock)
    queries = sports_queries(
        engine, leagues, matcher, settings.bluesky_sports_searches_per_cycle, clock()
    )
    steps = [(q.query, lambda q=q: crawler.read(q)) for q in queries]
    return crawler.run_steps(steps)
