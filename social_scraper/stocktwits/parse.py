"""StockTwits API JSON (`/api/2/streams/symbol/<SYM>.json`, `/trending/symbols.json`) -> `Post`.

Shapes as a signed-out Chrome loaded them on 2026-10-10:

- a **stream** is `{"symbol": {...}, "cursor": {"more", "since", "max"}, "messages":
  [...]}`, the 30 newest messages naming the symbol. `?since=<id>` returns only newer
  ones (two, on a stream read minutes earlier).
- a **message** carries `id`, `body`, `created_at` (ISO, `Z`), `symbols[].symbol`,
  `user` (`id`, `username`, `followers`, `official`, ...), `entities.sentiment` --
  `{"basic": "Bullish"}` or `"Bearish"`, or null when the author set none (seven of 29
  on an AAPL stream) -- and, when it is in a thread, `conversation`
  (`parent_message_id`, `in_reply_to_message_id`, `parent`, `replies`). `likes`
  (`{"total", "user_ids"}`) and `reshares` (`{"reshared_count", "user_ids"}`) appear only
  when non-zero. A reshare carries `reshare_message`; it is skipped, as the X crawler
  skips retweets.
- **trending** is `{"symbols": [{"symbol": "PLTR", ...}, ...]}`, 30 of them, crypto
  among them with a `.X` suffix (`AERO.X`).

The author is hashed on the numeric user id, the username never read, so a live row's
`author_hash` equals the NYU archive's for the same person (`stocktwits/dumps.py`).
"""

from __future__ import annotations

import html
from datetime import UTC, datetime
from typing import Any

from social_scraper.models import STOCKTWITS, AuthorInfo, Engagement, Post

URL = "https://stocktwits.com/message/{}"
SENTIMENTS = frozenset({"bullish", "bearish"})
CRYPTO_SUFFIX = ".X"


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _int(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _created(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _sentiment(message: dict[str, Any]) -> str | None:
    basic = _dict(_dict(message.get("entities")).get("sentiment")).get("basic")
    label = basic.strip().lower() if isinstance(basic, str) else ""
    return label if label in SENTIMENTS else None


def _author(user: dict[str, Any]) -> AuthorInfo:
    """Hashed on the numeric id; the username is never read."""
    user_id, official = _int(user.get("id")), user.get("official")
    return AuthorInfo(
        name=None,
        user_id=str(user_id) if user_id is not None else None,
        followers=_int(user.get("followers")),
        verified=official if isinstance(official, bool) else None,
    )


def _engagement(message: dict[str, Any], conversation: dict[str, Any]) -> Engagement:
    likes = _int(_dict(message.get("likes")).get("total"))
    return Engagement(
        score=likes,
        likes=likes,
        reposts=_int(_dict(message.get("reshares")).get("reshared_count")),
        comments=_int(conversation.get("replies")),
    )


def _raw(message: dict[str, Any], conversation: dict[str, Any]) -> dict[str, Any]:
    """The thread's first message, and the author's own label, when there are any."""
    raw: dict[str, Any] = {}
    thread = _int(conversation.get("parent_message_id"))
    if thread is not None:
        raw["conversation_id"] = str(thread)
    sentiment = _sentiment(message)
    if sentiment:
        raw["sentiment"] = sentiment
    return raw


def _tagged(message: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        s["symbol"]
        for s in message.get("symbols") or []
        if isinstance(s, dict) and isinstance(s.get("symbol"), str)
    )


def _readable(message: Any) -> tuple[int, str, datetime] | None:
    """`(id, body, created)` of a message worth a `Post`: not a reshare, with text."""
    if not isinstance(message, dict) or message.get("reshare_message"):
        return None
    message_id, body = _int(message.get("id")), message.get("body")
    created = _created(message.get("created_at"))
    if message_id is None or created is None or not isinstance(body, str) or not body.strip():
        return None
    return message_id, body, created


def parse_message(message: Any, symbol: str) -> Post | None:
    """One stream message as a `Post`, or None to skip it (a reshare, no text)."""
    readable = _readable(message)
    if readable is None:
        return None
    message_id, body, created = readable
    conversation = _dict(message.get("conversation"))
    reply_to = _int(conversation.get("in_reply_to_message_id"))
    return Post(
        platform=STOCKTWITS,
        external_id=str(message_id),
        kind="message" if reply_to is None else "reply",
        channel=f"symbol:{symbol}",
        created_at=created,
        author=_author(_dict(message.get("user"))),
        body=html.unescape(body),
        url=URL.format(message_id),
        parent_id=None if reply_to is None else str(reply_to),
        engagement=_engagement(message, conversation),
        tagged_symbols=_tagged(message),
        raw=_raw(message, conversation),
    )


def parse_stream(payload: Any, symbol: str) -> list[Post]:
    """The posts of one symbol stream. Raises `ValueError` when `payload` is not a
    stream -- a wall or a layout change, which must not read as "no new messages"."""
    if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
        raise ValueError("not a StockTwits stream")
    posts = (parse_message(m, symbol) for m in payload["messages"])
    return [p for p in posts if p is not None]


def parse_trending(payload: Any) -> list[str]:
    """The trending symbols as StockTwits writes them, crypto (`.X`) dropped. Raises
    `ValueError` when `payload` is not a trending list."""
    if not isinstance(payload, dict) or not isinstance(payload.get("symbols"), list):
        raise ValueError("not a StockTwits trending list")
    out = []
    for item in payload["symbols"]:
        sym = _dict(item).get("symbol")
        if isinstance(sym, str) and sym.strip() and not sym.upper().endswith(CRYPTO_SUFFIX):
            out.append(sym.strip())
    return out
