"""HFBoards' XenForo 2 HTML -> thread rows and posts. Pure; stdlib `html.parser` only.

The forum serves no JSON, so its pages are read as HTML. The shapes below were read
from the live site on 2026-10-10 (a signed-out Chrome):

- **A forum's thread list**, `/forums/<slug>.<id>/`, ordered by last post: one
  `div.structItem.structItem--thread` per thread, its id in the class
  `js-threadListItem-<id>`. The thread link is the `a` under `.structItem-title` whose
  href holds `/threads/` -- a prefix label beside it links back to the forum. Two
  `<time data-timestamp>` per row: the thread's start, then its last post. The meta
  cell's `dl`s read "Replies 342", "Replies 1K", "Views 16K".
- **A thread page**, `/threads/<slug>.<id>/latest` redirecting to its last page: one
  `article.message--post` per post, with `data-author` (the username) and
  `data-content="post-<id>"`. The post time is the `time.u-dt` in its attribution
  (`datetime="2026-10-10T20:24:12-0400"`, `data-timestamp`); the body is the
  `.bbWrapper` of `.message-body` -- a signature has a `.bbWrapper` of its own. A quote
  of another post is a `blockquote.bbCodeBlock--quote` inside the body, dropped here:
  it repeats text stored with its own post, and names the quoted user.

A page with neither shape is a `ValueError` -- a wall, or a layout change, which the
crawler must report rather than read as "nothing new".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser

VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())
SKIP = frozenset({"script", "style", "noscript", "template"})
BLOCK = frozenset(
    "address article aside blockquote dd div dl dt figure footer h1 h2 h3 h4 h5 h6 "
    "header li ol p pre section table tbody td tr ul".split()
)
_THREAD_CLASS = re.compile(r"\bjs-threadListItem-(\d+)\b")
_THREAD_PATH = re.compile(r"(/threads/(?:[^/?#]*\.)?(\d+)/)")
_POST_ID = re.compile(r"post-(\d+)")
_COUNT = re.compile(r"([\d.,]+)\s*([KkMm]?)")
_SPACES = re.compile(r"[ \t\r\f\v\xa0]+")
_BLANK_LINES = re.compile(r"\n{3,}")


@dataclass
class Element:
    tag: str
    attrs: dict[str, str]
    children: list[Element | str] = field(default_factory=list)

    @property
    def classes(self) -> frozenset[str]:
        return frozenset(self.attrs.get("class", "").split())

    def has_class(self, name: str) -> bool:
        return name in self.classes

    def iter(self):
        """Every element below this one, depth first, in document order."""
        for child in self.children:
            if isinstance(child, Element):
                yield child
                yield from child.iter()

    def find(self, tag: str | None = None, cls: str | None = None) -> Element | None:
        return next(self.find_all(tag, cls), None)

    def find_all(self, tag: str | None = None, cls: str | None = None):
        for el in self.iter():
            if (tag is None or el.tag == tag) and (cls is None or el.has_class(cls)):
                yield el


class _TreeBuilder(HTMLParser):
    """A forgiving DOM: an end tag closes up to its nearest open match, or nothing."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Element("#root", {})
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        el = Element(tag, {k: v or "" for k, v in attrs})
        self.stack[-1].children.append(el)
        if tag not in VOID:
            self.stack.append(el)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.stack[-1].children.append(Element(tag, {k: v or "" for k, v in attrs}))

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def parse_html(html: str) -> Element:
    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()
    return builder.root


def _is_quote(el: Element) -> bool:
    return el.tag == "blockquote" and bool(el.classes & {"bbCodeBlock--quote", "bbCodeQuote"})


def _is_label(el: Element) -> bool:
    return any(c.startswith("label") for c in el.classes)


def text_of(el: Element, drop=lambda _el: False) -> str:
    """Visible text: block elements and `<br>` break lines, runs of spaces collapse."""
    parts: list[str] = []

    def walk(node: Element) -> None:
        for child in node.children:
            if isinstance(child, str):
                parts.append(child)
                continue
            if child.tag in SKIP or drop(child):
                continue
            if child.tag == "br":
                parts.append("\n")
                continue
            block = child.tag in BLOCK
            if block:
                parts.append("\n")
            walk(child)
            if block:
                parts.append("\n")

    walk(el)
    lines = [_SPACES.sub(" ", line).strip() for line in "".join(parts).split("\n")]
    joined = "\n".join(lines)
    return _BLANK_LINES.sub("\n\n", joined).strip()


def parse_count(text: str) -> int | None:
    """`"342"` -> 342, `"1K"` -> 1000, `"1.2K"` -> 1200, `"1,234"` -> 1234."""
    m = _COUNT.search(text)
    if not m:
        return None
    try:
        number = float(m.group(1).replace(",", ""))
    except ValueError:  # separators only
        return None
    scale = {"k": 1_000, "m": 1_000_000}.get(m.group(2).lower(), 1)
    return round(number * scale)


def _time(el: Element) -> datetime | None:
    """A XenForo `<time>`: its epoch `data-timestamp`, else its ISO `datetime`."""
    stamp = el.attrs.get("data-timestamp", "")
    if stamp.isdigit():
        return datetime.fromtimestamp(int(stamp), tz=UTC)
    value = el.attrs.get("datetime", "")
    try:  # XenForo writes the offset without a colon: `-0400`
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z").astimezone(UTC)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# --- the thread list ---------------------------------------------------------------------


@dataclass(frozen=True)
class ThreadRow:
    thread_id: str
    path: str  # `/threads/<slug>.<id>/`
    title: str
    last_post_at: datetime
    replies: int | None = None


def _thread_row(row: Element) -> ThreadRow | None:
    title_cell = row.find(cls="structItem-title")
    link = None
    for a in (title_cell or row).find_all("a"):
        if "/threads/" in a.attrs.get("href", ""):
            link = a
            break
    if link is None:
        return None
    path_match = _THREAD_PATH.search(link.attrs["href"])
    class_match = _THREAD_CLASS.search(row.attrs.get("class", ""))
    if path_match is None:
        return None
    thread_id = class_match.group(1) if class_match else path_match.group(2)
    times = [t for t in (_time(el) for el in row.find_all("time")) if t is not None]
    if not times:
        return None
    replies = None
    for dl in row.find_all("dl"):
        label = text_of(dl)
        if label.lower().startswith("replies"):
            replies = parse_count(label[len("replies") :])
    return ThreadRow(
        thread_id=thread_id,
        path=path_match.group(1),
        title=text_of(link, _is_label),
        last_post_at=max(times),
        replies=replies,
    )


def parse_forum(html: str) -> list[ThreadRow]:
    """The threads of a forum page. Empty for an empty forum; `ValueError` for a page
    that is not a thread list at all."""
    root = parse_html(html)
    rows = [el for el in root.find_all("div", "structItem--thread") if el.has_class("structItem")]
    if not rows:
        if root.find(cls="structItemContainer") is None and root.find(cls="js-threadList") is None:
            raise ValueError("no thread list on the page")
        return []
    return [r for r in (_thread_row(el) for el in rows) if r is not None]


# --- a thread page -----------------------------------------------------------------------


@dataclass(frozen=True)
class ForumPost:
    post_id: str
    author: str | None
    created_at: datetime
    body: str


@dataclass(frozen=True)
class ThreadPage:
    title: str | None
    posts: list[ForumPost]


def _post(article: Element) -> ForumPost | None:
    m = _POST_ID.search(article.attrs.get("data-content", "")) or _POST_ID.search(
        article.attrs.get("id", "")
    )
    if m is None:
        return None
    attribution = article.find(cls="message-attribution")
    stamp = (attribution or article).find("time", "u-dt") or article.find("time", "u-dt")
    created = _time(stamp) if stamp is not None else None
    if created is None:
        return None
    message_body = article.find(cls="message-body")
    wrapper = message_body.find(cls="bbWrapper") if message_body is not None else None
    body = text_of(wrapper, _is_quote) if wrapper is not None else ""
    return ForumPost(
        post_id=m.group(1),
        author=article.attrs.get("data-author") or None,
        created_at=created,
        body=body,
    )


def parse_thread(html: str) -> ThreadPage:
    """A thread page's title and posts; `ValueError` for a page that is not a thread."""
    root = parse_html(html)
    articles = [el for el in root.find_all("article", "message--post") if el.has_class("message")]
    if not articles and root.find(cls="block--messages") is None:
        raise ValueError("no posts on the page")
    heading = root.find("h1", "p-title-value")
    title = text_of(heading, _is_label) if heading is not None else None
    posts = [p for p in (_post(a) for a in articles) if p is not None]
    return ThreadPage(title=title or None, posts=posts)
