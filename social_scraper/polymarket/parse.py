"""Gamma API events (`/events`) -> `Market`s. Pure.

The shape, as `gamma-api.polymarket.com` served it on 2026-10-10: `/events` is a JSON
list of events, each with its `tags` (`{id, slug, label}`) and its `markets`. A market's
`outcomes`, `outcomePrices` and `clobTokenIds` are JSON *strings* holding lists
(`'["Yes", "No"]'`, `'["0.0215", "0.9785"]'`), and `volume`/`liquidity` are decimal
strings beside their numeric twins (`volumeNum`, `liquidityNum`). A game's event carries
its own state: `gameId`, `teams` (`{name, abbreviation, league, record, ...}`),
`score` (`"0-0"`), `period` (`"End P2"`, `"VFT"` once final), `live`, `ended`,
`startTime`; its markets add `sportsMarketType` (`moneyline`, `totals`, ...), `line` and
`gameStartTime`. A resolved market is `closed` with its prices at `["1", "0"]` --
measured on a final NHL game, whose `umaResolutionStatus` read `resolved`.

Kept: what the market asks, its outcomes and prices, and the game's state. Not kept:
`submitted_by`, `resolvedBy` and the like -- wallet addresses identify people -- and
the long rules text (`description`).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# A closed market whose outcome price is at least this is the one that resolved Yes.
RESOLVED_PRICE = 0.99
# Team fields kept: what names the team and its form. Logos, provider ids, timestamps go.
TEAM_FIELDS = ("name", "abbreviation", "alias", "league", "record")
# A time ending in an hour-only offset (`23:00:00+00`), which `fromisoformat` refuses.
_SHORT_OFFSET = re.compile(r"(T\d{2}:\d{2}:\d{2}(?:\.\d+)?[+-]\d{2})$")


@dataclass(frozen=True)
class Market:
    market_id: str
    event_id: str
    question: str
    created_at: datetime
    event_slug: str | None = None
    event_title: str | None = None
    slug: str | None = None
    tags: tuple[str, ...] = ()
    outcomes: tuple[str, ...] = ()
    prices: tuple[float | None, ...] = ()
    last_trade_price: float | None = None
    best_bid: float | None = None
    best_ask: float | None = None
    spread: float | None = None
    volume: float | None = None
    volume_24hr: float | None = None
    liquidity: float | None = None
    sports_market_type: str | None = None
    line: float | None = None
    game_id: str | None = None
    teams: tuple[dict[str, Any], ...] = field(default=())
    score: str | None = None
    period: str | None = None
    live: bool | None = None
    ended: bool | None = None
    start_time: datetime | None = None
    end_date: datetime | None = None
    active: bool | None = None
    closed: bool | None = None

    @property
    def winner(self) -> int | None:
        """The index of the outcome a closed market resolved to, when its prices say."""
        if not self.closed:
            return None
        for i, price in enumerate(self.prices):
            if price is not None and price >= RESOLVED_PRICE:
                return i
        return None


def _float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _str(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def _time(value: Any) -> datetime | None:
    """ISO with `Z`, an offset, or the `"2026-10-10 23:00:00+00"` form games use."""
    text = _str(value)
    if text is None:
        return None
    text = _SHORT_OFFSET.sub(r"\1:00", text.replace(" ", "T", 1).replace("Z", "+00:00"))
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _list(value: Any) -> list[Any]:
    """A list, or the JSON-encoded list Gamma sends in its place."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return value if isinstance(value, list) else []


def _teams(value: Any) -> tuple[dict[str, Any], ...]:
    out = []
    for team in _list(value):
        if isinstance(team, dict):
            kept = {k: team[k] for k in TEAM_FIELDS if team.get(k) not in (None, "")}
            if kept:
                out.append(kept)
    return tuple(out)


def parse_market(market: dict[str, Any], event: dict[str, Any]) -> Market | None:
    """One market of `event`, or None when it lacks an id, a question or a creation time."""
    market_id, question = _str(market.get("id")), _str(market.get("question"))
    created = _time(market.get("createdAt")) or _time(event.get("createdAt"))
    if market_id is None or question is None or created is None:
        return None
    tags = tuple(
        sorted(
            {
                t["slug"].lower()
                for t in event.get("tags") or []
                if isinstance(t, dict) and t.get("slug")
            }
        )
    )
    return Market(
        market_id=market_id,
        event_id=_str(event.get("id")) or "",
        question=question,
        created_at=created,
        event_slug=_str(event.get("slug")),
        event_title=_str(event.get("title")),
        slug=_str(market.get("slug")),
        tags=tags,
        outcomes=tuple(str(o) for o in _list(market.get("outcomes"))),
        prices=tuple(_float(p) for p in _list(market.get("outcomePrices"))),
        last_trade_price=_float(market.get("lastTradePrice")),
        best_bid=_float(market.get("bestBid")),
        best_ask=_float(market.get("bestAsk")),
        spread=_float(market.get("spread")),
        volume=_float(market.get("volumeNum", market.get("volume"))),
        volume_24hr=_float(market.get("volume24hr")),
        liquidity=_float(market.get("liquidityNum", market.get("liquidity"))),
        sports_market_type=_str(market.get("sportsMarketType")),
        line=_float(market.get("line")),
        game_id=_str(event.get("gameId")),
        teams=_teams(event.get("teams")),
        score=_str(event.get("score")),
        period=_str(event.get("period")),
        live=_bool(event.get("live")),
        ended=_bool(event.get("ended")),
        start_time=_time(market.get("gameStartTime"))
        or _time(event.get("startTime"))
        or _time(event.get("startDate")),
        end_date=_time(market.get("endDate")) or _time(event.get("endDate")),
        active=_bool(market.get("active")),
        closed=_bool(market.get("closed")),
    )


def parse_events(payload: Any) -> list[Market]:
    """Every market of every event in an `/events` page.

    Raises `ValueError` when the page is not a list of events: an error object or a
    layout change, which the caller must see rather than read as "no markets".
    """
    if not isinstance(payload, list):
        raise ValueError("not a Gamma /events list")
    out: list[Market] = []
    for event in payload:
        if not isinstance(event, dict):
            continue
        for market in event.get("markets") or []:
            if isinstance(market, dict):
                parsed = parse_market(market, event)
                if parsed is not None:
                    out.append(parsed)
    return out
