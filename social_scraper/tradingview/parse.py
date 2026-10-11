"""TradingView's ideas JSON (`/symbols/<SYM>/ideas/?component-data-only=1`) -> `Post`.

Captured live 2026-10-10: the page's component data is `data.ideas.data` with `items`
(24 a page), `next` (`/symbols/NASDAQ-AAPL/ideas/page-2/`) and `total`. Each idea has
`id`, `name` (its title), `description` (a preview of about 500 characters, not the full
text), `created_at` (ISO, `2026-10-10T11:35:52+00:00`) and `date_timestamp`,
`chart_url`, `is_public`/`is_visible`, `likes_count`/`comments_count`/`views_count`,
`symbol` (`short_name`, `type`, `interval`, `direction`) and `user` (`id`, `username`).

`symbol.direction` is the author's call: 1 is Long and 2 is Short -- checked against the
Long/Short label on the ideas' own pages -- and 0 is neither. It lands in
`raw.direction` (`extra.direction` in the lake), beside the chart's `interval`.

The author is hashed from the numeric user id, as X and StockTwits authors are; the
username is never read.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from social_scraper.models import TRADINGVIEW, AuthorInfo, Engagement, Post

DIRECTIONS = {1: "long", 2: "short"}
# Symbol types whose short name is a listed security the universe can check.
EQUITY_TYPES = frozenset({"stock", "fund"})


def _int(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _created(item: dict[str, Any]) -> datetime | None:
    raw = item.get("created_at")
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            parsed = None
        if parsed is not None:
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    stamp = _int(item.get("date_timestamp"))
    return datetime.fromtimestamp(stamp, tz=UTC) if stamp is not None else None


def _raw(symbol: dict[str, Any]) -> dict[str, Any]:
    """The author's call (`long`/`short`, none for neither) and the chart's timeframe."""
    raw: dict[str, Any] = {}
    direction = DIRECTIONS.get(_int(symbol.get("direction")) or 0)
    if direction:
        raw["direction"] = direction
    if symbol.get("interval"):
        raw["interval"] = str(symbol["interval"])
    return raw


def _engagement(item: dict[str, Any]) -> Engagement:
    likes = _int(item.get("likes_count"))
    return Engagement(
        score=likes,
        likes=likes,
        comments=_int(item.get("comments_count")),
        views=_int(item.get("views_count")),
    )


def _author(item: dict[str, Any]) -> AuthorInfo:
    user_id = _dict(item.get("user")).get("id")
    return AuthorInfo(name=None, user_id=str(user_id) if user_id is not None else None)


def _readable(item: dict[str, Any]) -> tuple[Any, datetime, str | None, str | None] | None:
    """`(id, created, title, body)` of a public idea with a time and some text."""
    if item.get("is_public") is False or item.get("is_visible") is False:
        return None
    idea_id, created = item.get("id"), _created(item)
    title, body = _str(item.get("name")), _str(item.get("description"))
    if idea_id is None or created is None or not (title or body):
        return None
    return idea_id, created, title, body


def parse_idea(item: dict[str, Any]) -> Post | None:
    """One idea, or None to skip it (private, hidden, or missing what a post needs)."""
    readable = _readable(item)
    if readable is None:
        return None
    idea_id, created, title, body = readable
    symbol = _dict(item.get("symbol"))
    short = _str(symbol.get("short_name")) or ""
    return Post(
        platform=TRADINGVIEW,
        external_id=str(idea_id),
        kind="idea",
        channel=f"symbol:{short}" if short else "symbol",
        created_at=created,
        author=_author(item),
        title=title,
        body=body,
        url=_str(item.get("chart_url")),
        engagement=_engagement(item),
        tagged_symbols=(short,) if short and symbol.get("type") in EQUITY_TYPES else (),
        raw=_raw(symbol),
    )


def ideas_page(payload: Any) -> dict[str, Any]:
    """The `data.ideas.data` block. Raises `ValueError` when the payload is not one --
    a wall or a layout change, which must not read as "no new ideas"."""
    try:
        page = payload["data"]["ideas"]["data"]
    except (KeyError, TypeError) as exc:
        raise ValueError("not a TradingView ideas payload") from exc
    if not isinstance(page, dict) or not isinstance(page.get("items"), list):
        raise ValueError("not a TradingView ideas payload")
    return page


def parse_ideas(payload: Any) -> list[Post]:
    """Every public idea on one page, newest first as served."""
    posts = []
    for item in ideas_page(payload)["items"]:
        post = parse_idea(item) if isinstance(item, dict) else None
        if post is not None:
            posts.append(post)
    return posts
