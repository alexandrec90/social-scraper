"""Sports posts -> `sports_*` rows: scrub, match entities, upsert.

The sports twin of `social_scraper.pipeline`, with one difference in what is kept: a
post is stored only when it is about a league -- it names an entity, it comes from a
league's or a team's subreddit (a game thread that says "what a save" is still about
the game), or an X search for that league found it. Unmatched capitalised names in
those posts are counted for discovery (`sports.discover`): the names, never the post.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.engine import Engine

from social_scraper.db import repository, schema
from social_scraper.models import REDDIT, Post
from social_scraper.pipeline import RAW_KEEP, IngestStats
from social_scraper.privacy import scrub_text
from social_scraper.sports import discover, registry
from social_scraper.sports.matcher import EntityMention, Matcher


def build_row(
    post: Post, matcher: Matcher, context: Iterable[str] = ()
) -> tuple[dict[str, Any], list[EntityMention], frozenset[str]]:
    """The `sports_posts` row, its mentions, and the leagues the post is about. Pure."""
    channel = post.channel if post.platform == REDDIT else None
    found = matcher.match(post.text, channel=channel, context=context)
    # A search for a league's chatter chose its results, as a stock cashtag search does.
    leagues = set(matcher.leagues_of(m.entity_id for m in found)) | set(context)
    if channel:
        leagues |= matcher.channel_leagues.get(channel.lower(), frozenset())
    row = {
        "platform": post.platform,
        "external_id": post.external_id,
        "kind": post.kind,
        "channel": post.channel[:64],
        "created_at": post.created_at,
        "author_hash": post.author.hash,
        "title": scrub_text(post.title),
        "body": scrub_text(post.body),
        "url": post.url,
        "parent_id": post.parent_id,
        "lang": (post.lang or None) and post.lang[:8],
        "flair": (post.flair or None) and post.flair[:128],
        "entities": sorted(m.entity_id for m in found),
        "leagues": sorted(leagues),
        "author_followers": post.author.followers,
        "author_verified": post.author.verified,
        "raw": {k: v for k, v in post.raw.items() if k in RAW_KEEP} or None,
        **post.engagement.as_dict(),
    }
    return row, found, frozenset(leagues)


class SportsIngestor:
    def __init__(
        self,
        engine: Engine,
        matcher: Matcher,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        collect_names: bool = True,
    ) -> None:
        self.engine = engine
        self.matcher = matcher
        self.clock = clock
        self.collect_names = collect_names
        # Leagues every post of the next `ingest` is about (an X search for one league).
        self.context: frozenset[str] = frozenset()

    def is_relevant(self, post: Post) -> bool:
        return bool(self.matcher.match(post.text, context=self.context))

    def ingest(self, posts: Iterable[Post]) -> IngestStats:
        """Store the on-topic posts of a batch in one transaction."""
        batch = list(posts)
        stats = IngestStats(seen=len(batch))
        now = self.clock()
        unique = {(p.platform, p.external_id): p for p in batch}
        with self.engine.begin() as conn:
            for post in unique.values():
                row, found, leagues = build_row(post, self.matcher, self.context)
                if not leagues:
                    continue
                repository.upsert_post(conn, row, now, schema.sports_posts)
                stats.stored += 1
                engagement = post.engagement.as_dict()
                if repository.record_engagement(
                    conn, post.platform, post.external_id, now, engagement, schema.sports_engagement
                ):
                    stats.snapshots += 1
                stats.new_mentions += registry.record_mentions(conn, row, found, now)
                if self.collect_names:
                    names = discover.candidate_names(post.text, self.matcher)
                    discover.count_candidates(conn, names, leagues, now)
        return stats
