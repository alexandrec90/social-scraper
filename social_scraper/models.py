"""Platform-neutral records the parsers produce and the pipeline stores.

A `Post` is anything a person wrote: a Reddit submission or comment, a tweet, a reply.
The raw author name travels on the record only as far as the pipeline, which hashes it;
nothing downstream of `pipeline.py` ever sees it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

REDDIT = "reddit"
X = "x"
STOCKTWITS = "stocktwits"
TRADINGVIEW = "tradingview"
HFBOARDS = "hfboards"
BLUESKY = "bluesky"
POLYMARKET = "polymarket"
# Every platform a cycle crawls, in the order it starts them: what `scrape all` means
# (narrowed by `SCRAPE_PLATFORMS`), and what has a request budget and a breaker.
PLATFORMS = (REDDIT, X, STOCKTWITS, TRADINGVIEW, HFBOARDS, BLUESKY, POLYMARKET)
# Read through a public JSON API over plain HTTPS (`apiclient.py`), not a browser.
API_PLATFORMS = (BLUESKY, POLYMARKET)
# What each domain crawls there. Polymarket is in neither: it is markets, not posts,
# and serves both domains from one crawl (`polymarket/`).
STOCK_PLATFORMS = (REDDIT, X, STOCKTWITS, TRADINGVIEW, BLUESKY)
SPORTS_PLATFORMS = (REDDIT, X, HFBOARDS, BLUESKY)
# The platforms `login` opens a window for; the others are read signed out.
LOGIN_PLATFORMS = (REDDIT, X)
# Archive-only: imported from a published dataset, never crawled, so not in PLATFORMS.
HACKERNEWS = "hackernews"


def stable_hash(value: str) -> str:
    """sha256 hex of `value` -- the same pseudonymisation data-lake's connectors use.

    Unsalted on purpose: data-lake's `social_posts.author_hash` is `sha256(username)`,
    and a salted hash here would never join against it.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def as_utc(value: datetime) -> datetime:
    """Treat a naive datetime as UTC (SQLite drops the zone) and convert aware ones."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True)
class Engagement:
    """What the crowd did with a post, as observed at one moment.

    Reddit fills `score`/`comments`/`upvote_ratio`; X fills the rest and mirrors
    likes into `score` and replies into `comments`, so a consumer that only knows the
    data-lake `social_posts` columns still gets a comparable number.
    """

    score: int | None = None
    comments: int | None = None
    upvote_ratio: float | None = None
    likes: int | None = None
    reposts: int | None = None
    quotes: int | None = None
    views: int | None = None
    bookmarks: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "num_comments": self.comments,
            "upvote_ratio": self.upvote_ratio,
            "likes": self.likes,
            "reposts": self.reposts,
            "quotes": self.quotes,
            "views": self.views,
            "bookmarks": self.bookmarks,
        }


@dataclass(frozen=True)
class AuthorInfo:
    """What a platform told us about who wrote a post.

    `name` is the handle/username and `user_id` the platform's stable id; both are
    identifiers and are hashed before storage. `handle` survives only for X accounts
    the discovery loop tracks, because visiting a timeline needs the URL.
    """

    name: str | None
    user_id: str | None = None
    followers: int | None = None
    verified: bool | None = None
    created_at: datetime | None = None
    # The account speaks for an organisation: X's affiliate label (a newsroom's or a
    # team's staff) or a Business/Government verification. X only; never exported.
    affiliated: bool | None = None

    @property
    def hash(self) -> str | None:
        """Hash of the stable id when there is one (X renames keep it), else the name.

        The Reddit parser leaves `user_id` unset, so a Reddit hash is
        `sha256(username)` -- identical to data-lake's PRAW connector.
        """
        basis = self.user_id or self.name
        return stable_hash(basis) if basis else None


@dataclass(frozen=True)
class Post:
    platform: str
    external_id: str
    kind: str
    channel: str
    created_at: datetime
    author: AuthorInfo
    title: str | None = None
    body: str | None = None
    url: str | None = None
    parent_id: str | None = None
    lang: str | None = None
    flair: str | None = None
    engagement: Engagement = field(default_factory=Engagement)
    # Symbols the platform itself tagged (X `entities.symbols`); text extraction adds more.
    tagged_symbols: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        """Title and body as one string -- what symbol and stance extraction read."""
        return "\n".join(part for part in (self.title, self.body) if part)
