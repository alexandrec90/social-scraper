"""`ThrottleStore` over the operational database.

Each call is its own short transaction: a budget counted from the database is what
lets two processes (a scheduled run and a manual one) share one allowance.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, delete, func, select
from sqlalchemy.engine import Engine

from social_scraper.db import schema
from social_scraper.db.repository import dialect_insert
from social_scraper.models import as_utc
from social_scraper.throttle import Block

REQUEST_LOG_RETENTION_DAYS = 14


class DbThrottleStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def count_since(self, platform: str, since: datetime) -> int:
        t = schema.request_log
        with self.engine.connect() as conn:
            return int(
                conn.execute(
                    select(func.count()).where(and_(t.c.platform == platform, t.c.at >= since))
                ).scalar_one()
            )

    def failures_since(self, platform: str, since: datetime) -> int:
        t = schema.request_log
        with self.engine.connect() as conn:
            return int(
                conn.execute(
                    select(func.count()).where(
                        and_(t.c.platform == platform, t.c.at >= since, t.c.ok.is_(False))
                    )
                ).scalar_one()
            )

    def last_request_at(self, platform: str) -> datetime | None:
        t = schema.request_log
        with self.engine.connect() as conn:
            value = conn.execute(select(func.max(t.c.at)).where(t.c.platform == platform)).scalar()
        return as_utc(value) if value is not None else None

    def log_request(
        self, platform: str, at: datetime, kind: str, status: int | None, ok: bool
    ) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                schema.request_log.insert().values(
                    platform=platform, at=at, kind=kind[:32], status=status, ok=ok
                )
            )

    def get_block(self, platform: str) -> Block:
        t = schema.throttle_state
        with self.engine.connect() as conn:
            row = conn.execute(select(t).where(t.c.platform == platform)).first()
        if row is None:
            return Block(None, None, 0)
        until = as_utc(row.blocked_until) if row.blocked_until else None
        return Block(until, row.reason, row.consecutive_failures)

    def set_block(self, platform: str, block: Block, now: datetime) -> None:
        values = {
            "blocked_until": block.blocked_until,
            "reason": block.reason,
            "consecutive_failures": block.consecutive_failures,
            "updated_at": now,
        }
        with self.engine.begin() as conn:
            stmt = dialect_insert(conn, schema.throttle_state).values(platform=platform, **values)
            conn.execute(stmt.on_conflict_do_update(index_elements=["platform"], set_=values))

    def prune(self, before: datetime) -> int:
        """Drop request-log rows older than any budget window can see."""
        with self.engine.begin() as conn:
            return conn.execute(
                delete(schema.request_log).where(schema.request_log.c.at < before)
            ).rowcount
