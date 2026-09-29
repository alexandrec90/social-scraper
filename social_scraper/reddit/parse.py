"""Reddit listing JSON (`/r/<sub>/new.json`, `/comments.json`, `/top.json`) -> `Post`.

Ids follow data-lake's PRAW connector so rows from both sources dedupe on
`(platform, external_id)`: a submission is its bare base36 id (`1abcde`). A comment
is its fullname (`t1_xyz`) -- the two id spaces are separate sequences, and the prefix
keeps them from ever colliding. A comment's `parent_id` is the submission id, so a
consumer can attribute "calls are printing" to the thread's ticker.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from social_scraper.models import REDDIT, AuthorInfo, Engagement, Post

REDDIT_BASE = "https://www.reddit.com"
_GONE = frozenset({"[deleted]", "[removed]", ""})


def _text(value: Any) -> str | None:
    if not isinstance(value, str) or value.strip() in _GONE:
        return None
    return value


def _int(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _author(d: dict[str, Any]) -> AuthorInfo:
    # `user_id` deliberately unset: the hash must be sha256(username) to join with
    # data-lake's connector (see `AuthorInfo.hash`).
    return AuthorInfo(name=_text(d.get("author")))


def _submission(d: dict[str, Any]) -> Post | None:
    title, body = _text(d.get("title")), _text(d.get("selftext"))
    if not (title or body):
        return None
    return Post(
        platform=REDDIT,
        external_id=str(d["id"]),
        kind="submission",
        channel=str(d.get("subreddit", "")),
        created_at=datetime.fromtimestamp(float(d["created_utc"]), tz=UTC),
        author=_author(d),
        title=title,
        body=body,
        url=REDDIT_BASE + d["permalink"] if d.get("permalink") else None,
        flair=_text(d.get("link_flair_text")),
        engagement=Engagement(
            score=_int(d.get("score")),
            comments=_int(d.get("num_comments")),
            upvote_ratio=d.get("upvote_ratio"),
        ),
        raw={
            k: d.get(k)
            for k in ("permalink", "domain", "is_self", "over_18", "stickied")
            if d.get(k) is not None
        },
    )


def _comment(d: dict[str, Any]) -> Post | None:
    body = _text(d.get("body"))
    if not body:
        return None
    link_id = str(d.get("link_id", ""))
    return Post(
        platform=REDDIT,
        external_id=str(d.get("name") or f"t1_{d['id']}"),
        kind="comment",
        channel=str(d.get("subreddit", "")),
        created_at=datetime.fromtimestamp(float(d["created_utc"]), tz=UTC),
        author=_author(d),
        body=body,
        url=REDDIT_BASE + d["permalink"] if d.get("permalink") else None,
        parent_id=link_id.removeprefix("t3_") or None,
        engagement=Engagement(score=_int(d.get("score"))),
        raw={k: d.get(k) for k in ("permalink", "link_title", "stickied") if d.get(k) is not None},
    )


def parse_thing(child: dict[str, Any]) -> Post | None:
    """One listing child (`{"kind": "t3"|"t1", "data": {...}}`), or None to skip it."""
    kind, data = child.get("kind"), child.get("data")
    if not isinstance(data, dict) or "created_utc" not in data:
        return None
    if kind == "t3":
        return _submission(data)
    if kind == "t1":
        return _comment(data)
    return None


def parse_listing(
    payload: Any, ignore_authors: frozenset[str] = frozenset()
) -> tuple[list[Post], str | None]:
    """Posts in a listing page, and the `after` cursor for the next page.

    Raises `ValueError` when `payload` is not a listing at all -- that is a wall or a
    layout change, which the caller must see rather than read as "no new posts".
    """
    if not isinstance(payload, dict) or payload.get("kind") != "Listing":
        raise ValueError("not a Reddit listing")
    data = payload.get("data") or {}
    posts = []
    for child in data.get("children") or []:
        post = parse_thing(child)
        if post is None:
            continue
        if post.author.name and post.author.name.lower() in ignore_authors:
            continue
        posts.append(post)
    return posts, data.get("after")
