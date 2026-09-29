"""Turn parsed `Post`s into stored rows: scrub, extract, score, upsert.

The one place a raw author name is turned into a hash and a raw body into a scrubbed
one. Symbol extraction reads the *unscrubbed* text (scrubbing only removes handles and
contact data, never tickers), and the stored text is the scrubbed copy.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.engine import Engine

from social_scraper.db import repository
from social_scraper.models import Post
from social_scraper.privacy import scrub_text
from social_scraper.stance import score_stance
from social_scraper.symbols.extract import Mention, extract_mentions, validated_symbols
from social_scraper.symbols.universe import Universe

# Raw payload fields worth keeping; anything else in `Post.raw` is dropped.
RAW_KEEP = frozenset(
    {"permalink", "domain", "is_self", "over_18", "stickied", "link_title", "conversation_id"}
)


@dataclass
class IngestStats:
    seen: int = 0
    stored: int = 0
    new_mentions: int = 0
    snapshots: int = 0

    def add(self, other: IngestStats) -> None:
        self.seen += other.seen
        self.stored += other.stored
        self.new_mentions += other.new_mentions
        self.snapshots += other.snapshots


def build_row(post: Post, universe: Universe) -> tuple[dict[str, Any], list[Mention]]:
    """The `posts` row for `post`, and the mentions found in it. Pure."""
    found = extract_mentions(post.text, universe, post.tagged_symbols)
    stance = score_stance(post.text)
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
        "symbols": validated_symbols(found),
        "author_followers": post.author.followers,
        "author_verified": post.author.verified,
        "stance": stance.score,
        "stance_bull": stance.bull,
        "stance_bear": stance.bear,
        "raw": {k: v for k, v in post.raw.items() if k in RAW_KEEP} or None,
        **post.engagement.as_dict(),
    }
    return row, found


class Ingestor:
    def __init__(
        self,
        engine: Engine,
        universe: Universe,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.engine = engine
        self.universe = universe
        self.clock = clock

    def ingest(self, posts: Iterable[Post]) -> IngestStats:
        """Store a batch in one transaction. Duplicates in the batch collapse."""
        batch = list(posts)
        stats = IngestStats(seen=len(batch))
        now = self.clock()
        unique = {(p.platform, p.external_id): p for p in batch}
        with self.engine.begin() as conn:
            for post in unique.values():
                row, found = build_row(post, self.universe)
                repository.upsert_post(conn, row, now)
                stats.stored += 1
                engagement = post.engagement.as_dict()
                if repository.record_engagement(
                    conn, post.platform, post.external_id, now, engagement
                ):
                    stats.snapshots += 1
                stats.new_mentions += repository.record_mentions(
                    conn, row, found, self.universe, now
                )
        return stats
