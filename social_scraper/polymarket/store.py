"""Polymarket markets into the operational tables: classify, upsert, snapshot.

A market is about a domain because of its event's tags: a stock tag (`POLYMARKET_STOCK_TAGS`)
makes it a stock market, a league's `polymarket_tags` a sports one about that league.
A stock market also gets the listed tickers it names -- "Will Goldman Sachs (GS) beat
quarterly earnings?" names `GS`, and Gamma tags such an event `gs` as well (2026-10-10).

A price snapshot is written only when what a market is worth moved: its outcome prices,
last trade, best bid or ask, or the game's score, period or live flag. Volume alone
grows on every trade and would make every observation a row.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Table, and_, func, or_, select
from sqlalchemy.engine import Connection

from social_scraper.config import SPORTS, STOCKS
from social_scraper.db import schema
from social_scraper.db.repository import dialect_insert
from social_scraper.models import POLYMARKET
from social_scraper.polymarket.parse import Market
from social_scraper.sports.leagues import League
from social_scraper.symbols.extract import STOPLIST, extract_mentions, validated_symbols
from social_scraper.symbols.universe import Universe, normalize_symbol

# What a snapshot compares with the latest one before writing.
PRICE_FIELDS = ("prices", "last_trade_price", "best_bid", "best_ask", "score", "period", "live")
MARKET_MUTABLE = (
    "event_slug",
    "event_title",
    "question",
    "slug",
    "domains",
    "tags",
    "leagues",
    "symbols",
    "outcomes",
    "sports_market_type",
    "line",
    "game_id",
    "teams",
    "score",
    "period",
    "live",
    "ended",
    "start_time",
    "end_date",
    "active",
    "closed",
    "winner",
)


@dataclass(frozen=True)
class Scope:
    """What the enabled domains ask of Polymarket: stock tags, and the leagues' tags."""

    stock_tags: frozenset[str] = frozenset()
    leagues: tuple[League, ...] = ()
    universe: Universe | None = None

    def league_keys(self, tags: Iterable[str]) -> list[str]:
        have = set(tags)
        return sorted(lg.key for lg in self.leagues if have & set(lg.polymarket_tags))

    def domains(self, tags: Iterable[str]) -> list[str]:
        have = set(tags)
        out = []
        if have & self.stock_tags:
            out.append(STOCKS)
        if self.league_keys(have):
            out.append(SPORTS)
        return out


def market_symbols(market: Market, universe: Universe | None) -> list[str]:
    """Listed tickers the market names: in its question or title, or as one of its tags."""
    if universe is None:
        return []
    text = "\n".join(t for t in (market.event_title, market.question) if t)
    found = set(validated_symbols(extract_mentions(text, universe)))
    for tag in market.tags:
        symbol = normalize_symbol(tag)
        if 2 <= len(symbol) <= 5 and symbol in universe and symbol not in STOPLIST:
            found.add(symbol)
    return sorted(found)


def fit(table: Table, row: dict[str, Any]) -> dict[str, Any]:
    """`row` with every string clipped to its column's length. Postgres refuses a longer
    one and fails the whole tag's batch -- 2026-10-10, a player-prop market type of 35
    characters -- where SQLite, under the tests, stores it whole."""
    out = dict(row)
    for column in table.columns:
        limit = getattr(column.type, "length", None)
        value = out.get(column.name)
        if limit and isinstance(value, str) and len(value) > limit:
            out[column.name] = value[:limit]
    return out


def market_row(market: Market, scope: Scope, query_domain: str) -> dict[str, Any]:
    """The `polymarket_markets` row. `query_domain` is the domain whose tag found it,
    for an event whose own tags name neither domain's. Pure."""
    domains = scope.domains(market.tags) or [query_domain]
    return fit(
        schema.polymarket_markets,
        {
            "market_id": market.market_id,
            "platform": POLYMARKET,
            "event_id": market.event_id,
            "event_slug": market.event_slug,
            "event_title": market.event_title,
            "question": market.question,
            "slug": market.slug,
            "domains": domains,
            "tags": list(market.tags),
            "leagues": scope.league_keys(market.tags),
            "symbols": market_symbols(market, scope.universe) if STOCKS in domains else [],
            "outcomes": list(market.outcomes),
            "sports_market_type": market.sports_market_type,
            "line": market.line,
            "game_id": market.game_id,
            "teams": [dict(t) for t in market.teams] or None,
            "score": market.score,
            "period": market.period,
            "live": market.live,
            "ended": market.ended,
            "start_time": market.start_time,
            "end_date": market.end_date,
            "created_at": market.created_at,
            "active": market.active,
            "closed": market.closed,
            "winner": market.winner,
        },
    )


def price_row(market: Market) -> dict[str, Any]:
    return fit(
        schema.polymarket_prices,
        {
            "market_id": market.market_id,
            "platform": POLYMARKET,
            "prices": list(market.prices),
            "last_trade_price": market.last_trade_price,
            "best_bid": market.best_bid,
            "best_ask": market.best_ask,
            "spread": market.spread,
            "volume": market.volume,
            "volume_24hr": market.volume_24hr,
            "liquidity": market.liquidity,
            "score": market.score,
            "period": market.period,
            "live": market.live,
        },
    )


def upsert_market(conn: Connection, row: dict[str, Any], now: datetime) -> None:
    """Insert a market or refresh what may change; `created_at`/`first_seen_at` stick."""
    t = schema.polymarket_markets
    stmt = dialect_insert(conn, t).values(**row, first_seen_at=now, updated_at=now)
    update = {f: stmt.excluded[f] for f in MARKET_MUTABLE} | {"updated_at": now}
    conn.execute(stmt.on_conflict_do_update(index_elements=["market_id"], set_=update))


def record_price(conn: Connection, row: dict[str, Any], now: datetime) -> bool:
    """Append a snapshot unless it prices the market as the latest one did."""
    t = schema.polymarket_prices
    latest = conn.execute(
        select(*[t.c[f] for f in PRICE_FIELDS])
        .where(t.c.market_id == row["market_id"])
        .order_by(t.c.observed_at.desc())
        .limit(1)
    ).first()
    current = tuple(row.get(f) for f in PRICE_FIELDS)
    if latest is not None and tuple(latest) == current:
        return False
    if not row["prices"] and row["last_trade_price"] is None:
        return False
    conn.execute(dialect_insert(conn, t).values(**row, observed_at=now).on_conflict_do_nothing())
    return True


def pending_events(conn: Connection, now: datetime, limit: int) -> list[str]:
    """Events with a market past its end and not yet closed, the longest overdue first:
    no longer in an open-markets listing, so only a lookup by id sees them resolve."""
    t = schema.polymarket_markets
    rows = conn.execute(
        select(t.c.event_id)
        .where(and_(or_(t.c.closed.is_(False), t.c.closed.is_(None)), t.c.end_date < now))
        .group_by(t.c.event_id)
        .order_by(func.min(t.c.end_date))
        .limit(limit)
    )
    return [r.event_id for r in rows if r.event_id]


def store(
    conn: Connection, markets: Sequence[Market], scope: Scope, query_domain: str, now: datetime
) -> tuple[int, int]:
    """Upsert each market and snapshot its price. `(markets stored, snapshots written)`."""
    snapshots = 0
    for market in markets:
        upsert_market(conn, market_row(market, scope, query_domain), now)
        snapshots += record_price(conn, price_row(market), now)
    return len(markets), snapshots


def counts(conn: Connection) -> dict[str, int]:
    """Row counts of the Polymarket tables, for `status`."""
    return {
        t.name: int(conn.execute(select(func.count()).select_from(t)).scalar_one())
        for t in (schema.polymarket_markets, schema.polymarket_prices)
    }
