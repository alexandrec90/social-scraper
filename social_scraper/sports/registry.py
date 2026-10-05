"""Reads and writes for the sports entity tables. Every function takes a `Connection`.

Membership (`sports_entity_leagues`) carries who vouched for it. A league's weekly sync
replaces its `wikidata` memberships and leaves `discovered` ones alone; an entity with
no active membership left is inactive, so the matcher stops naming it. Rows are never
deleted: mention counts and first-seen dates outlive a relegation.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, select, update
from sqlalchemy.engine import Connection

from social_scraper.db import schema
from social_scraper.db.repository import dialect_insert
from social_scraper.sports.matcher import Entity, EntityMention
from social_scraper.sports.wikidata import EntityRecord

WIKIDATA = "wikidata"
DISCOVERED = "discovered"


def upsert_entity(conn: Connection, record: EntityRecord, source: str, now: datetime) -> None:
    """Insert or refresh one entity's names and links; counts and first-seen stick."""
    t = schema.sports_entities
    values = {
        "entity_id": record.entity_id,
        "kind": record.kind,
        "name": record.name,
        "aliases": list(record.aliases),
        "subreddit": record.subreddit,
        "x_handle": record.x_handle,
        "team_id": record.team_id,
        "popularity": record.popularity,
        "source": source,
        "active": True,
        "first_seen_at": now,
        "refreshed_at": now,
        "mention_count": 0,
        "updated_at": now,
    }
    stmt = dialect_insert(conn, t).values(**values)
    refresh = ("kind", "name", "aliases", "subreddit", "x_handle", "team_id", "popularity")
    set_: dict[str, Any] = {f: stmt.excluded[f] for f in refresh}
    set_ |= {"active": True, "refreshed_at": now, "updated_at": now}
    if source == WIKIDATA:
        set_["source"] = WIKIDATA
    conn.execute(stmt.on_conflict_do_update(index_elements=["entity_id"], set_=set_))


def set_membership(
    conn: Connection, entity_id: str, league: str, source: str, now: datetime
) -> None:
    t = schema.sports_entity_leagues
    stmt = dialect_insert(conn, t).values(
        entity_id=entity_id, league=league, source=source, active=True, refreshed_at=now
    )
    set_: dict[str, Any] = {"active": True, "refreshed_at": now}
    if source == WIKIDATA:
        set_["source"] = WIKIDATA
    conn.execute(stmt.on_conflict_do_update(index_elements=["entity_id", "league"], set_=set_))


def replace_league(
    conn: Connection, league: str, records: Iterable[EntityRecord], now: datetime
) -> int:
    """Make `records` the league's Wikidata membership. Returns how many it holds."""
    records = list(records)
    for record in records:
        upsert_entity(conn, record, WIKIDATA, now)
        set_membership(conn, record.entity_id, league, WIKIDATA, now)
    m = schema.sports_entity_leagues
    keep = {r.entity_id for r in records}
    active: list[str] = list(
        conn.execute(
            select(m.c.entity_id).where(
                and_(m.c.league == league, m.c.source == WIKIDATA, m.c.active.is_(True))
            )
        ).scalars()
    )
    gone = [eid for eid in active if eid not in keep]
    if gone:
        conn.execute(
            update(m)
            .where(and_(m.c.league == league, m.c.entity_id.in_(gone)))
            .values(active=False, refreshed_at=now)
        )
    refresh_active(conn, now)
    return len(keep)


def retire_discovered(conn: Connection, unmentioned_since: datetime, now: datetime) -> int:
    """Deactivate discovered memberships of entities nobody has named since the cutoff."""
    m, e = schema.sports_entity_leagues, schema.sports_entities
    quiet = select(e.c.entity_id).where(
        and_(
            func.coalesce(e.c.last_mentioned_at, e.c.first_seen_at) < unmentioned_since,
        )
    )
    result = conn.execute(
        update(m)
        .where(and_(m.c.source == DISCOVERED, m.c.active.is_(True), m.c.entity_id.in_(quiet)))
        .values(active=False, refreshed_at=now)
    )
    refresh_active(conn, now)
    return int(result.rowcount or 0)


def refresh_active(conn: Connection, now: datetime) -> None:
    """`sports_entities.active` := the entity has at least one active membership."""
    m, e = schema.sports_entity_leagues, schema.sports_entities
    members = select(m.c.entity_id).where(m.c.active.is_(True))
    conn.execute(
        update(e)
        .where(and_(e.c.active.is_(True), e.c.entity_id.not_in(members)))
        .values(active=False, updated_at=now)
    )
    conn.execute(
        update(e)
        .where(and_(e.c.active.is_(False), e.c.entity_id.in_(members)))
        .values(active=True, updated_at=now)
    )


def load_entities(conn: Connection) -> list[Entity]:
    """Every active entity with its active leagues: what the matcher is built from."""
    m, e = schema.sports_entity_leagues, schema.sports_entities
    leagues: dict[str, set[str]] = {}
    for row in conn.execute(select(m.c.entity_id, m.c.league).where(m.c.active.is_(True))):
        leagues.setdefault(row.entity_id, set()).add(row.league)
    return [
        Entity(
            entity_id=r.entity_id,
            kind=r.kind,
            name=r.name,
            aliases=tuple(r.aliases or ()),
            leagues=frozenset(leagues.get(r.entity_id, ())),
        )
        for r in conn.execute(select(e).where(e.c.active.is_(True)).order_by(e.c.entity_id))
        if r.entity_id in leagues
    ]


def team_subreddits(conn: Connection, leagues: Iterable[str]) -> dict[str, tuple[str, str]]:
    """`{subreddit: (entity_id, league)}` for active teams of `leagues` that have one."""
    m, e = schema.sports_entity_leagues, schema.sports_entities
    wanted = list(leagues)
    if not wanted:
        return {}
    rows = conn.execute(
        select(e.c.subreddit, e.c.entity_id, m.c.league)
        .join(m, m.c.entity_id == e.c.entity_id)
        .where(
            and_(
                e.c.kind == "team",
                e.c.subreddit.is_not(None),
                m.c.active.is_(True),
                m.c.league.in_(wanted),
            )
        )
        .order_by(e.c.entity_id, m.c.league)
    )
    out: dict[str, tuple[str, str]] = {}
    for r in rows:
        out.setdefault(r.subreddit, (r.entity_id, r.league))
    return out


def official_handles(conn: Connection, entity_ids: Iterable[str]) -> list[str]:
    e = schema.sports_entities
    ids = list(entity_ids)
    if not ids:
        return []
    rows = conn.execute(select(e.c.x_handle).where(e.c.entity_id.in_(ids)))
    return sorted({r.x_handle for r in rows if r.x_handle})


def record_mentions(
    conn: Connection, post: dict[str, Any], found: Iterable[EntityMention], now: datetime
) -> int:
    """Store each (post, entity) once; bump the entity's count for new ones."""
    t, e = schema.sports_mentions, schema.sports_entities
    new = 0
    for m in found:
        stmt = dialect_insert(conn, t).values(
            platform=post["platform"],
            external_id=post["external_id"],
            entity_id=m.entity_id,
            match_type=m.match_type,
            channel=post["channel"],
            created_at=post["created_at"],
            updated_at=now,
        )
        # RETURNING, not rowcount: psycopg reports -1 on DO NOTHING (see repository).
        if conn.execute(stmt.on_conflict_do_nothing().returning(t.c.entity_id)).first():
            new += 1
            conn.execute(
                update(e)
                .where(e.c.entity_id == m.entity_id)
                .values(
                    mention_count=e.c.mention_count + 1,
                    last_mentioned_at=now,
                    updated_at=now,
                )
            )
    return new


def trending(
    conn: Connection, since: datetime, limit: int, leagues: Iterable[str] | None = None
) -> list[tuple[str, str, int]]:
    """`(entity_id, name, distinct posts)` since `since`, most-named first."""
    t, e = schema.sports_mentions, schema.sports_entities
    count = func.count().label("n")
    stmt = (
        select(t.c.entity_id, e.c.name, count)
        .join(e, e.c.entity_id == t.c.entity_id)
        .where(and_(t.c.created_at >= since, e.c.active.is_(True)))
        .group_by(t.c.entity_id, e.c.name)
        .order_by(count.desc(), t.c.entity_id)
        .limit(limit)
    )
    if leagues is not None:
        m = schema.sports_entity_leagues
        members = select(m.c.entity_id).where(
            and_(m.c.active.is_(True), m.c.league.in_(list(leagues)))
        )
        stmt = stmt.where(t.c.entity_id.in_(members))
    return [(r.entity_id, r.name, int(r.n)) for r in conn.execute(stmt)]


def counts(conn: Connection) -> dict[str, int]:
    """Row counts per sports table, for `status`."""
    tables = (
        schema.sports_posts,
        schema.sports_mentions,
        schema.sports_engagement,
        schema.sports_entities,
    )
    return {
        t.name: int(conn.execute(select(func.count()).select_from(t)).scalar_one()) for t in tables
    }
