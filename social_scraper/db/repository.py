"""Reads and writes against the operational tables.

Every function takes an open `Connection` so the caller owns the transaction: one
ingest batch commits or rolls back as a unit.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import Table, and_, create_engine, func, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import Connection, Engine

from social_scraper.db import schema
from social_scraper.models import as_utc
from social_scraper.symbols.extract import Mention
from social_scraper.symbols.universe import Universe

ENGAGEMENT_FIELDS = (
    "score",
    "num_comments",
    "upvote_ratio",
    "likes",
    "reposts",
    "quotes",
    "views",
    "bookmarks",
)
# Fields a re-observation may change: engagement, edits, and what we derived from them.
POST_MUTABLE_FIELDS = (
    *ENGAGEMENT_FIELDS,
    "title",
    "body",
    "flair",
    "symbols",
    "author_followers",
    "author_verified",
    "stance",
    "stance_bull",
    "stance_bear",
    "raw",
)


def make_engine(url: str) -> Engine:
    return create_engine(url, pool_pre_ping=True)


def dialect_insert(conn: Connection, table: Table):
    """The dialect's INSERT, which is what carries `on_conflict_*`."""
    dialect = postgresql if conn.dialect.name == "postgresql" else sqlite
    return dialect.insert(table)


def upsert_post(conn: Connection, row: dict[str, Any], now: datetime) -> None:
    """Insert a post, or refresh its mutable fields; `created_at`/`first_seen_at` stick."""
    values = {**row, "first_seen_at": now, "last_seen_at": now, "updated_at": now}
    stmt = dialect_insert(conn, schema.posts).values(**values)
    update = {f: stmt.excluded[f] for f in POST_MUTABLE_FIELDS if f in row}
    update |= {"last_seen_at": now, "updated_at": now}
    conn.execute(
        stmt.on_conflict_do_update(index_elements=["platform", "external_id"], set_=update)
    )


def record_engagement(
    conn: Connection, platform: str, external_id: str, observed_at: datetime, values: dict
) -> bool:
    """Append a snapshot unless it equals the latest one. Returns whether it wrote."""
    t = schema.engagement
    latest = conn.execute(
        select(*[t.c[f] for f in ENGAGEMENT_FIELDS])
        .where(and_(t.c.platform == platform, t.c.external_id == external_id))
        .order_by(t.c.observed_at.desc())
        .limit(1)
    ).first()
    current = tuple(values.get(f) for f in ENGAGEMENT_FIELDS)
    if latest is not None and tuple(latest) == current:
        return False
    if all(v is None for v in current):
        return False
    stmt = dialect_insert(conn, t).values(
        platform=platform,
        external_id=external_id,
        observed_at=observed_at,
        **{f: values.get(f) for f in ENGAGEMENT_FIELDS},
    )
    conn.execute(stmt.on_conflict_do_nothing())
    return True


def record_mentions(
    conn: Connection,
    post: dict[str, Any],
    found: Iterable[Mention],
    universe: Universe,
    now: datetime,
) -> int:
    """Store each (post, symbol) once and bump the symbol registry for new ones."""
    new = 0
    for m in found:
        stmt = dialect_insert(conn, schema.mentions).values(
            platform=post["platform"],
            external_id=post["external_id"],
            symbol=m.symbol,
            match_type=m.match_type,
            in_universe=m.in_universe,
            channel=post["channel"],
            created_at=post["created_at"],
            updated_at=now,
        )
        # RETURNING, not rowcount: psycopg 3 reports rowcount -1 here (measured), which
        # is truthy, so every re-seen mention counted as new on Postgres.
        inserted = conn.execute(
            stmt.on_conflict_do_nothing().returning(schema.mentions.c.symbol)
        ).first()
        if inserted is not None:
            new += 1
            _bump_symbol(conn, m, universe, now)
    return new


def _bump_symbol(conn: Connection, m: Mention, universe: Universe, now: datetime) -> None:
    sec = universe.get(m.symbol)
    stmt = dialect_insert(conn, schema.symbols).values(
        symbol=m.symbol,
        in_universe=m.in_universe,
        security_name=sec.name if sec else None,
        exchange=sec.exchange if sec else None,
        is_etf=sec.is_etf if sec else None,
        first_seen_at=now,
        last_seen_at=now,
        mention_count=1,
        updated_at=now,
    )
    conn.execute(
        stmt.on_conflict_do_update(
            index_elements=["symbol"],
            set_={
                "mention_count": schema.symbols.c.mention_count + 1,
                "last_seen_at": now,
                "updated_at": now,
                "in_universe": stmt.excluded.in_universe,
            },
        )
    )


def trending_symbols(conn: Connection, since: datetime, limit: int) -> list[tuple[str, int]]:
    """Validated symbols ranked by distinct posts mentioning them since `since`."""
    t = schema.mentions
    count = func.count().label("n")
    rows = conn.execute(
        select(t.c.symbol, count)
        .where(and_(t.c.created_at >= since, t.c.in_universe.is_(True)))
        .group_by(t.c.symbol)
        .order_by(count.desc(), t.c.symbol)
        .limit(limit)
    )
    return [(r.symbol, int(r.n)) for r in rows]


def get_state(conn: Connection, key: str) -> str | None:
    t = schema.crawl_state
    return conn.execute(select(t.c.value).where(t.c.key == key)).scalar_one_or_none()


def set_state(conn: Connection, key: str, value: str, now: datetime) -> None:
    stmt = dialect_insert(conn, schema.crawl_state).values(key=key, value=value, updated_at=now)
    conn.execute(
        stmt.on_conflict_do_update(index_elements=["key"], set_={"value": value, "updated_at": now})
    )


def states_with_prefix(conn: Connection, prefix: str) -> dict[str, str]:
    """`{key-without-prefix: value}` for every state key starting with `prefix`."""
    t = schema.crawl_state
    rows = conn.execute(select(t.c.key, t.c.value).where(t.c.key.startswith(prefix)))
    return {r.key[len(prefix) :]: r.value for r in rows}


def rows_since(
    conn: Connection, table: Table, ts_column: str, since: datetime | None, limit: int
) -> list[dict[str, Any]]:
    """Rows whose `ts_column` is after `since`, oldest first, at most `limit`."""
    col = table.c[ts_column]
    stmt = select(table).order_by(col).limit(limit)
    if since is not None:
        stmt = stmt.where(col > since)
    return [dict(r._mapping) for r in conn.execute(stmt)]


def get_watermark(conn: Connection, dataset: str) -> datetime | None:
    t = schema.export_watermarks
    value = conn.execute(
        select(t.c.exported_through).where(t.c.dataset == dataset)
    ).scalar_one_or_none()
    return as_utc(value) if value is not None else None


def set_watermark(conn: Connection, dataset: str, through: datetime, now: datetime) -> None:
    stmt = dialect_insert(conn, schema.export_watermarks).values(
        dataset=dataset, exported_through=through, updated_at=now
    )
    conn.execute(
        stmt.on_conflict_do_update(
            index_elements=["dataset"], set_={"exported_through": through, "updated_at": now}
        )
    )


def table_counts(conn: Connection) -> dict[str, int]:
    """Row counts per operational table, for `status`."""
    return {
        t.name: int(conn.execute(select(func.count()).select_from(t)).scalar_one())
        for t in (schema.posts, schema.mentions, schema.engagement, schema.symbols)
    }
