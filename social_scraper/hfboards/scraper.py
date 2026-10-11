"""The HFBoards crawl (sports domain): a few forums a cycle, the threads that moved.

Which forums: the leagues' `hfboards_forums` (`montreal-canadiens.16`), each read as
about its league -- the posts are ingested with that league's `context`, so a game
thread's "what a save" is stored, and one-word names ("Habs") resolve.

Per cycle, `HFBOARDS_FORUMS_PER_CYCLE` forums, the least recently visited first. For
each, one load of its thread list (newest last post first, `parse.parse_forum`), then up to
`HFBOARDS_THREADS_PER_FORUM` of the threads with posts past their cursor -- a thread
never read counts only when its last post is within `NEW_THREAD_WINDOW`, so a first
visit does not wake every thread on the page. Each thread is one load of
`/threads/<slug>.<id>/latest`, which XenForo redirects to the last page (measured
2026-10-10); the posts on it past the cursor are ingested, and the cursor moves to the
newest. A thread that gained more than a page since the last visit loses the middle:
the latest page is where the conversation is, and the budget is pages.

Signed out, through the same Chrome as Reddit and X: the forum served every page to a
logged-out browser (2026-10-10).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial

from sqlalchemy.engine import Engine

from social_scraper.config import Settings
from social_scraper.feeds import HTML, FeedCrawler, FeedRunStats, classify
from social_scraper.fetch import Browser, FetchFailed, PageResult
from social_scraper.hfboards.parse import ForumPost, ThreadRow, parse_forum, parse_thread
from social_scraper.models import HFBOARDS, AuthorInfo, Post
from social_scraper.sports.ingest import SportsIngestor
from social_scraper.sports.leagues import League
from social_scraper.sports.matcher import Matcher
from social_scraper.throttle import CHALLENGED, OutOfTime, Throttle

log = logging.getLogger(__name__)

BASE = "https://forums.hfboards.com"
VISITED_PREFIX = "hfboards:visited:"
THREAD_PREFIX = "hfboards:thread:"
NEW_THREAD_WINDOW = timedelta(hours=48)
# Cloudflare's interstitial, matched on the document's own title: a real forum page's
# visible text can quote anything, and Cloudflare injects its scripts into real pages too.
CHALLENGE_TITLE = "<title>just a moment...</title>"


def forum_url(forum: str) -> str:
    return f"{BASE}/forums/{forum}/"


def latest_url(thread: ThreadRow) -> str:
    return f"{BASE}{thread.path}latest"


def channel_of(forum: str) -> str:
    """The forum's slug without its node id: `montreal-canadiens.16` -> `montreal-canadiens`."""
    return forum.rsplit(".", 1)[0][:64]


def to_post(post: ForumPost, forum: str, thread_id: str, title: str | None) -> Post:
    return Post(
        platform=HFBOARDS,
        external_id=post.post_id,
        kind="post",
        channel=channel_of(forum),
        created_at=post.created_at,
        author=AuthorInfo(name=post.author),
        body=post.body,
        url=f"{BASE}/posts/{post.post_id}/",
        parent_id=thread_id,
        raw={"link_title": title} if title else {},
    )


def forum_contexts(leagues: tuple[League, ...]) -> dict[str, frozenset[str]]:
    """`{forum: leagues its posts are about}`, in list order; a forum several leagues
    list is about all of them."""
    out: dict[str, frozenset[str]] = {}
    for league in leagues:
        for forum in league.hfboards_forums:
            out[forum] = out.get(forum, frozenset()) | league.context
    return out


@dataclass(frozen=True)
class HFBoardsPlan:
    """What a cycle reads: `{forum: its leagues}` (`forum_contexts`), and how much."""

    forums: dict[str, frozenset[str]]
    forums_per_cycle: int
    threads_per_forum: int


class HFBoardsScraper(FeedCrawler):
    platform = HFBOARDS

    def __init__(
        self,
        browser: Browser,
        throttle: Throttle,
        ingestor: SportsIngestor,
        engine: Engine,
        plan: HFBoardsPlan,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        super().__init__(browser, throttle, ingestor, engine, clock)
        self.sports = ingestor
        self.forums = plan.forums
        self.forums_per_cycle = plan.forums_per_cycle
        self.threads_per_forum = plan.threads_per_forum

    def classify(self, result: PageResult, expect: str) -> tuple[str, str]:
        if CHALLENGE_TITLE in result.html[:20_000].lower():
            return CHALLENGED, f"challenge page at {result.final_url}"
        return classify(result, expect)

    def pick_forums(self) -> list[str]:
        """The `forums_per_cycle` forums visited longest ago; never visited first."""
        floor = datetime.min.replace(tzinfo=UTC)
        visited = {}
        for forum in self.forums:
            stamp = self.get_state(VISITED_PREFIX + forum)
            visited[forum] = datetime.fromisoformat(stamp) if stamp else floor
        order = sorted(self.forums, key=lambda f: visited[f])  # stable: list order on ties
        return order[: max(self.forums_per_cycle, 0)]

    def run(self) -> FeedRunStats:
        """Each picked forum, then its threads, under `FeedCrawler`'s failure rules."""
        try:
            for forum in self.pick_forums():
                due: list[tuple[ThreadRow, datetime | None]] = []
                self.guarded(f"forum {forum}", partial(self.read_forum, forum, due))
                for thread, cursor in due:
                    step = partial(self.read_thread, forum, thread, cursor)
                    self.guarded(f"thread {thread.thread_id}", step)
        except OutOfTime as exc:
            self.stats.notices.append(str(exc))
        except FetchFailed as exc:
            log.warning("hfboards run stopped: %s", exc)
        return self.stats

    def read_forum(self, forum: str, due: list[tuple[ThreadRow, datetime | None]]) -> None:
        """Load the forum's thread list; the threads due a visit, with their cursors,
        onto `due`."""
        self.set_state(VISITED_PREFIX + forum, self.clock().isoformat())
        result = self.fetch(forum_url(forum), "forum", HTML)
        if result.status == 404:
            self.stats.notices.append(f"forum {forum}: not found (renamed or removed)")
            return
        due.extend(self.due_threads(parse_forum(result.html)))

    def due_threads(self, threads: list[ThreadRow]) -> list[tuple[ThreadRow, datetime | None]]:
        now = self.clock()
        due: list[tuple[ThreadRow, datetime | None]] = []
        for thread in sorted(threads, key=lambda t: t.last_post_at, reverse=True):
            if len(due) >= self.threads_per_forum:
                break
            stamp = self.get_state(THREAD_PREFIX + thread.thread_id)
            cursor = datetime.fromisoformat(stamp) if stamp else None
            if cursor is None and now - thread.last_post_at > NEW_THREAD_WINDOW:
                continue
            if cursor is not None and thread.last_post_at <= cursor:
                continue
            due.append((thread, cursor))
        return due

    def read_thread(self, forum: str, thread: ThreadRow, cursor: datetime | None) -> None:
        result = self.fetch(latest_url(thread), "thread", HTML)
        if result.status == 404:
            self.stats.notices.append(f"thread {thread.thread_id}: not found (deleted or moved)")
            return
        page = parse_thread(result.html)
        title = page.title or thread.title
        fresh = [p for p in page.posts if cursor is None or p.created_at > cursor]
        posts = [to_post(p, forum, thread.thread_id, title) for p in fresh if p.body]
        self.sports.context = self.forums.get(forum, frozenset())
        try:
            self.ingest(posts)
        finally:
            self.sports.context = frozenset()
        newest = max(
            [
                thread.last_post_at,
                *(p.created_at for p in page.posts),
                *([cursor] if cursor else []),
            ]
        )
        self.set_state(THREAD_PREFIX + thread.thread_id, newest.isoformat())


def crawl_sports(
    settings: Settings,
    leagues: tuple[League, ...],
    matcher: Matcher,
    engine: Engine,
    browser: Browser,
    throttle: Throttle,
) -> FeedRunStats:
    """The sports domain's HFBoards crawl for one cycle."""
    plan = HFBoardsPlan(
        forum_contexts(leagues),
        settings.hfboards_forums_per_cycle,
        settings.hfboards_threads_per_forum,
    )
    return HFBoardsScraper(browser, throttle, SportsIngestor(engine, matcher), engine, plan).run()
