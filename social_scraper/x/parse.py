"""X web-app tweet objects -> `Post`.

Tweets are never read out of the DOM, which is obfuscated and reshuffled constantly.
They are read from the data the X web app loads for itself, which arrives two ways:
GraphQL XHR responses (`SearchTimeline`, `UserOriginalsTimeline`, `UserTweets`,
pagination) and the first page streamed into the HTML (see `browser.py`,
"Server-rendered data").

Two object shapes are in the wild, and both are read:

| field      | classic (`legacy`)                  | 2026 web app                        |
| ---------- | ----------------------------------- | ----------------------------------- |
| text       | `legacy.full_text`                  | `details.full_text`                 |
| created    | `legacy.created_at` (Twitter time)  | `details.created_at_ms`             |
| counts     | `legacy.favorite_count`, ...        | `counts.favorite_count`, ...        |
| cashtags   | `legacy.entities.symbols[].text`    | `details.cashtag_entities[]`        |
| followers  | `user.legacy.followers_count`       | `user.relationship_counts.followers`|
| verified   | `user.is_blue_verified`             | `user.verification.is_blue_verified`|
| handle     | `user.legacy.screen_name`           | `user.core.screen_name`             |

A long tweet's full text is `note_tweet...result.text` in both. The walker does not
hard-code where tweets sit in a response: it takes every object whose `__typename` is
`Tweet` (unwrapping `TweetWithVisibilityResults`). A retweet wrapper is skipped -- its
original is inside it and is yielded on its own, with its real author, which is how
amplified accounts get discovered.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from social_scraper.models import X, AuthorInfo, Engagement, Post

X_BASE = "https://x.com"
TWITTER_TIME = "%a %b %d %H:%M:%S %z %Y"
_RETWEET_KEYS = ("retweeted_status_result", "retweeted_tweet_results")
_QUOTE_KEYS = ("quoted_status_result", "quoted_tweet_results")
# `verification.verified_type` of an organisation's account: the gold (Business) and
# grey (Government) checks, as opposed to a paid personal blue check. The field is in
# the 2026 shape `conftest.x_tweet_2026` mirrors; the affiliate label's path is the
# classic GraphQL one and has not been confirmed in a 2026 payload.
ORG_VERIFIED_TYPES = frozenset({"Business", "Government"})


def _dig(obj: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _first(*values: Any) -> Any:
    return next((v for v in values if v is not None), None)


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def parse_time(value: Any) -> datetime | None:
    """Twitter's `Wed Oct 10 20:19:24 +0000 2018`, or epoch milliseconds."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    if not isinstance(value, str):
        return None
    if value.isdigit():
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
    try:
        return datetime.strptime(value, TWITTER_TIME)
    except ValueError:
        return None


def iter_tweet_results(payload: Any) -> Iterator[dict[str, Any]]:
    """Every tweet object anywhere in `payload`, depth-first, visibility-unwrapped."""
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(reversed(node))
            continue
        if not isinstance(node, dict):
            continue
        typename = node.get("__typename")
        if typename == "TweetWithVisibilityResults" and isinstance(node.get("tweet"), dict):
            node = {**node["tweet"], "__typename": "Tweet"}
            typename = "Tweet"
        if typename == "Tweet" and node.get("rest_id"):
            yield node
        stack.extend(reversed(list(node.values())))


def parse_author(result: dict[str, Any]) -> AuthorInfo:
    user = _dig(result, "core", "user_results", "result") or {}
    if user.get("__typename") == "UserUnavailable":
        user = {}
    legacy, core = user.get("legacy") or {}, user.get("core") or {}
    verified = _first(
        _dig(user, "verification", "is_blue_verified"),
        user.get("is_blue_verified"),
        legacy.get("verified"),
    )
    verified_type = _first(_dig(user, "verification", "verified_type"), legacy.get("verified_type"))
    label = _dig(user, "affiliates_highlighted_label", "label", "description")
    affiliated = verified_type in ORG_VERIFIED_TYPES or bool(label)
    return AuthorInfo(
        name=_first(core.get("screen_name"), legacy.get("screen_name")),
        user_id=user.get("rest_id"),
        followers=_int(
            _first(_dig(user, "relationship_counts", "followers"), legacy.get("followers_count"))
        ),
        verified=bool(verified) if verified is not None else None,
        created_at=parse_time(
            _first(core.get("created_at_ms"), core.get("created_at"), legacy.get("created_at"))
        ),
        affiliated=affiliated,
    )


def _symbols(result: dict[str, Any]) -> tuple[str, ...]:
    sources = (
        _dig(result, "details", "cashtag_entities"),
        _dig(result, "legacy", "entities", "symbols"),
        _dig(result, "note_tweet", "note_tweet_results", "result", "entity_set", "symbols"),
    )
    out: dict[str, None] = {}
    for items in sources:
        for item in items or []:
            tag = item
            if isinstance(item, dict):
                tag = _first(item.get("text"), item.get("tag"), item.get("symbol"))
            if isinstance(tag, str) and tag:
                out[tag] = None
    return tuple(out)


def _engagement(result: dict[str, Any]) -> Engagement:
    counts, legacy = result.get("counts") or {}, result.get("legacy") or {}

    def count(key: str) -> int | None:
        return _int(_first(counts.get(key), legacy.get(key)))

    likes, replies = count("favorite_count"), count("reply_count")
    return Engagement(
        score=likes,
        comments=replies,
        likes=likes,
        reposts=count("retweet_count"),
        quotes=count("quote_count"),
        views=_int(_dig(result, "views", "count")),
        bookmarks=count("bookmark_count"),
    )


def _kind(result: dict[str, Any], reply_to: str | None) -> str:
    legacy = result.get("legacy") or {}
    if reply_to:
        return "reply"
    if legacy.get("is_quote_status") or any(result.get(k) or legacy.get(k) for k in _QUOTE_KEYS):
        return "quote"
    return "tweet"


def parse_tweet(result: dict[str, Any], channel: str) -> Post | None:
    """One tweet object -> `Post`, or None for retweet wrappers and empty shells."""
    legacy, details = result.get("legacy") or {}, result.get("details") or {}
    if any(legacy.get(k) is not None or result.get(k) is not None for k in _RETWEET_KEYS):
        return None
    created = parse_time(_first(details.get("created_at_ms"), legacy.get("created_at")))
    text = _first(
        _dig(result, "note_tweet", "note_tweet_results", "result", "text"),
        details.get("full_text"),
        legacy.get("full_text"),
    )
    if created is None or not text:
        return None
    reply_to = _first(
        legacy.get("in_reply_to_status_id_str"),
        details.get("in_reply_to_status_id_str"),
        _dig(result, "reply_to_results", "rest_id"),
    )
    rest_id = str(result["rest_id"])
    conversation = _first(legacy.get("conversation_id_str"), details.get("conversation_id_str"))
    return Post(
        platform=X,
        external_id=rest_id,
        kind=_kind(result, reply_to),
        channel=channel,
        created_at=created,
        author=parse_author(result),
        body=text,
        # The handle-free permalink: a URL naming the author would leak the identifier.
        url=f"{X_BASE}/i/status/{rest_id}",
        parent_id=reply_to,
        lang=_first(legacy.get("lang"), details.get("lang")),
        engagement=_engagement(result),
        tagged_symbols=_symbols(result),
        raw={"conversation_id": conversation} if conversation else {},
    )


def parse_payloads(payloads: list[Any], channel: str) -> list[Post]:
    """Every distinct tweet in a page's captured responses, first occurrence wins."""
    seen: dict[str, Post] = {}
    for payload in payloads:
        for result in iter_tweet_results(payload):
            post = parse_tweet(result, channel)
            if post is not None and post.external_id not in seen:
                seen[post.external_id] = post
    return list(seen.values())
