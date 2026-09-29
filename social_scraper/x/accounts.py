"""Persistence for the X discovery loop (`x_accounts`)."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.engine import Connection

from social_scraper.db import schema
from social_scraper.db.repository import dialect_insert
from social_scraper.models import Post, as_utc
from social_scraper.x.discovery import CANDIDATE, SEED, AccountRow

SEARCH = "search"
AMPLIFIED = "amplified"
SEED_ID_PREFIX = "seed:"


def engagement_of(post: Post) -> int:
    e = post.engagement
    return (e.likes or 0) + 2 * (e.reposts or 0) + (e.comments or 0) + 2 * (e.quotes or 0)


def observe(conn: Connection, posts: Iterable[Post], via: str, now: datetime) -> int:
    """Count each author's sighting; unknown authors become candidates. Returns new ones."""
    t = schema.x_accounts
    new = 0
    for post in posts:
        a = post.author
        if not (a.user_id and a.name):
            continue
        hits = 1 if via == SEARCH else 0
        amplified = 1 if via == AMPLIFIED else 0
        stmt = dialect_insert(conn, t).values(
            user_id=a.user_id,
            handle=a.name,
            status=CANDIDATE,
            source=via,
            followers=a.followers,
            verified=a.verified,
            search_hits=hits,
            search_engagement=engagement_of(post) if hits else 0,
            timeline_tweets=0,
            timeline_finance_tweets=0,
            mentioned_by_tracked=amplified,
            score=0.0,
            first_seen_at=now,
            last_seen_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["user_id"],
            set_={
                "handle": a.name,
                "followers": a.followers if a.followers is not None else t.c.followers,
                "verified": a.verified if a.verified is not None else t.c.verified,
                "search_hits": t.c.search_hits + hits,
                "search_engagement": t.c.search_engagement + (engagement_of(post) if hits else 0),
                "mentioned_by_tracked": t.c.mentioned_by_tracked + amplified,
                "last_seen_at": now,
            },
        )
        existed = conn.execute(select(t.c.user_id).where(t.c.user_id == a.user_id)).first()
        conn.execute(stmt)
        new += existed is None
    return new


def load(conn: Connection) -> list[AccountRow]:
    t = schema.x_accounts
    return [
        AccountRow(
            user_id=r.user_id,
            handle=r.handle,
            status=r.status,
            followers=r.followers,
            search_hits=r.search_hits,
            search_engagement=r.search_engagement,
            timeline_tweets=r.timeline_tweets,
            timeline_finance_tweets=r.timeline_finance_tweets,
            mentioned_by_tracked=r.mentioned_by_tracked,
            last_timeline_at=as_utc(r.last_timeline_at) if r.last_timeline_at else None,
        )
        for r in conn.execute(select(t))
    ]


def apply(conn: Connection, changes: dict[str, tuple[str, float]]) -> None:
    t = schema.x_accounts
    for user_id, (status, score) in changes.items():
        conn.execute(update(t).where(t.c.user_id == user_id).values(status=status, score=score))


def record_visit(
    conn: Connection, user_id: str, tweets: int, finance_tweets: int, now: datetime
) -> None:
    """Add one timeline visit's sample to the account's finance-ratio evidence."""
    t = schema.x_accounts
    conn.execute(
        update(t)
        .where(t.c.user_id == user_id)
        .values(
            timeline_tweets=t.c.timeline_tweets + tweets,
            timeline_finance_tweets=t.c.timeline_finance_tweets + finance_tweets,
            last_timeline_at=now,
        )
    )


def add_seeds(conn: Connection, handles: Iterable[str], now: datetime) -> int:
    """Register seed handles. The real user id is learned on the first visit."""
    t = schema.x_accounts
    existing: list[str] = list(conn.execute(select(t.c.handle)).scalars())
    known = {h.lower() for h in existing}
    added = 0
    for handle in handles:
        if handle.lower() in known:
            conn.execute(
                update(t).where(func.lower(t.c.handle) == handle.lower()).values(status=SEED)
            )
            continue
        conn.execute(
            t.insert().values(
                user_id=f"{SEED_ID_PREFIX}{handle.lower()}",
                handle=handle,
                status=SEED,
                source=SEED,
                search_hits=0,
                search_engagement=0,
                timeline_tweets=0,
                timeline_finance_tweets=0,
                mentioned_by_tracked=0,
                score=0.0,
                first_seen_at=now,
                last_seen_at=now,
            )
        )
        added += 1
    return added


def resolve_seed(conn: Connection, placeholder: str, user_id: str) -> str:
    """Swap a seed's placeholder id for the real one; merge if the real one exists."""
    t = schema.x_accounts
    if not placeholder.startswith(SEED_ID_PREFIX) or placeholder == user_id:
        return placeholder
    if conn.execute(select(t.c.user_id).where(t.c.user_id == user_id)).first():
        conn.execute(t.delete().where(t.c.user_id == placeholder))
        conn.execute(update(t).where(t.c.user_id == user_id).values(status=SEED))
    else:
        conn.execute(update(t).where(t.c.user_id == placeholder).values(user_id=user_id))
    return user_id
