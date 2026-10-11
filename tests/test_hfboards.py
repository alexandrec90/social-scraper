"""HFBoards: XenForo HTML parsing and the sports crawl, against canned pages.

The builders below mirror the markup read from the live forum on 2026-10-10 (a thread
list row, a thread page's post article) with invented usernames and text.
"""

from datetime import UTC, datetime, timedelta

import pytest
from conftest import NOW, Clock, FakeBrowser, IsolatedSettings
from sqlalchemy import select

from social_scraper.db import repository, schema
from social_scraper.fetch import PageResult
from social_scraper.hfboards import parse, scraper
from social_scraper.hfboards.parse import parse_count, parse_forum, parse_thread
from social_scraper.hfboards.scraper import (
    THREAD_PREFIX,
    VISITED_PREFIX,
    HFBoardsPlan,
    HFBoardsScraper,
    channel_of,
    crawl_sports,
    forum_contexts,
    forum_url,
)
from social_scraper.models import HFBOARDS
from social_scraper.sports.ingest import SportsIngestor
from social_scraper.sports.leagues import parse_leagues
from social_scraper.sports.matcher import Entity, Matcher
from social_scraper.throttle import MemoryThrottleStore, Policy, Throttle

HABS_FORUM = "montreal-canadiens.16"
LEAFS_FORUM = "toronto-maple-leafs.17"
BASE = "https://forums.hfboards.com"


# --- canned XenForo markup ---------------------------------------------------------------


def iso(at: datetime) -> str:
    """XenForo's `datetime` attribute: local time, offset without a colon."""
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+0000")


def thread_row(
    thread_id: str,
    slug: str,
    title: str,
    last: datetime,
    replies: str = "12",
    started: datetime | None = None,
) -> str:
    started = started or last - timedelta(days=3)
    return f"""
<div class="structItem structItem--thread is-prefix3 js-inlineModContainer js-threadListItem-{thread_id}" data-author="starter">
  <div class="structItem-cell structItem-cell--main">
    <div class="structItem-title">
      <a href="/forums/{HABS_FORUM}/?prefix_id=3" class="labelLink"><span class="label">HF Habs</span></a>
      <a href="/threads/{slug}.{thread_id}/" data-tp-primary="on">{title}</a>
    </div>
    <div class="structItem-minor"><ul class="structItem-parts">
      <li><a class="username">starter</a></li>
      <li><time class="u-dt" datetime="{iso(started)}" data-timestamp="{int(started.timestamp())}">Oct 7</time></li>
    </ul></div>
  </div>
  <div class="structItem-cell structItem-cell--meta">
    <dl class="pairs pairs--justified"><dt>Replies</dt><dd>{replies}</dd></dl>
    <dl class="pairs pairs--justified structItem-minor"><dt>Views</dt><dd>16K</dd></dl>
  </div>
  <div class="structItem-cell structItem-cell--latest">
    <a href="/threads/{slug}.{thread_id}/latest"><time class="structItem-latestDate u-dt" datetime="{iso(last)}" data-timestamp="{int(last.timestamp())}">35m</time></a>
  </div>
</div>"""


def forum_page(*rows: str) -> str:
    return f"""<!DOCTYPE html><html><head><title>Montreal Canadiens | HFBoards</title>
<script>var x = "<div class='structItem structItem--thread'>";</script></head>
<body><div class="block-container"><div class="structItemContainer">
<div class="structItemContainer-group js-threadList">{"".join(rows)}</div>
</div></div></body></html>"""


def post_article(post_id: str, author: str, at: datetime, body: str) -> str:
    return f"""
<article class="message message--post js-post js-inlineModContainer" data-author="{author}" data-content="post-{post_id}" id="js-post-{post_id}" itemscope itemtype="https://schema.org/Comment" itemid="{BASE}/posts/{post_id}/">
  <div class="message-inner">
    <div class="message-cell message-cell--user"><h4 class="message-name"><a class="username">{author}</a></h4></div>
    <div class="message-cell message-cell--main">
      <header class="message-attribution"><a href="/threads/x.1/post-{post_id}">
        <time class="u-dt" dir="auto" datetime="{iso(at)}" data-timestamp="{int(at.timestamp())}">Oct 10</time></a></header>
      <div class="message-content js-messageContent"><div class="message-userContent">
        <article class="message-body js-selectToQuote"><div class="bbWrapper">{body}</div></article>
      </div>
      <div class="message-lastEdit">Last edited: <time class="u-dt" data-timestamp="{int((at + timedelta(hours=1)).timestamp())}">later</time></div>
      <aside class="message-signature"><div class="bbWrapper">Go Habs Go forever</div></aside>
      </div>
    </div>
  </div>
</article>"""


def quote(who: str, text: str) -> str:
    return (
        f'<blockquote data-attributes="member: 1" data-quote="{who}" data-source="post: 9" '
        f'class="bbCodeBlock bbCodeBlock--expandable bbCodeBlock--quote js-expandWatch">'
        f'<div class="bbCodeBlock-title">{who} said:</div><div class="bbCodeBlock-content">'
        f'<div class="bbCodeBlock-expandContent">{text}</div>'
        f'<div class="bbCodeBlock-expandLink"><a>Click to expand...</a></div></div></blockquote>'
    )


def thread_page(title: str, *articles: str) -> str:
    return f"""<!DOCTYPE html><html><head><title>{title} | Page 52 | HFBoards</title></head>
<body><div class="p-title"><h1 class="p-title-value"><a class="labelLink"><span class="label">HF Habs</span><span class="label-append">&nbsp;</span></a>{title}</h1></div>
<div class="block block--messages"><div class="block-body">{"".join(articles)}</div></div>
</body></html>"""


def html_ok(html: str, url: str = BASE) -> PageResult:
    return PageResult(200, url, html=html, text="HFBoards")


CLOUDFLARE = """<!DOCTYPE html><html><head><title>Just a moment...</title></head>
<body><div>Verify you are human by completing the action below.</div></body></html>"""


# --- parsing -----------------------------------------------------------------------------


def test_parse_count_reads_abbreviations():
    assert parse_count("342") == 342
    assert parse_count(" 1K ") == 1000
    assert parse_count("1.2K") == 1200
    assert parse_count("1,234") == 1234
    assert parse_count("2M") == 2_000_000
    assert parse_count("-") is None
    assert parse_count(",") is None


def test_parse_forum_reads_rows():
    last = NOW - timedelta(minutes=35)
    html = forum_page(
        thread_row(
            "3037848", "out-of-town-thread-2026-2027-season", "Out of Town Thread", last, "1K"
        ),
        thread_row(
            "3033911", "prospects-thread", "Prospects &amp; Picks", last - timedelta(hours=1)
        ),
    )
    rows = parse_forum(html)
    assert [r.thread_id for r in rows] == ["3037848", "3033911"]
    first = rows[0]
    assert first.path == "/threads/out-of-town-thread-2026-2027-season.3037848/"
    assert first.title == "Out of Town Thread"  # the prefix label is not the title
    assert first.last_post_at == last.replace(microsecond=0)
    assert first.replies == 1000
    assert rows[1].title == "Prospects & Picks"


def test_parse_forum_empty_forum_is_empty_but_a_wall_is_an_error():
    assert parse_forum('<div class="structItemContainer"><div>No threads.</div></div>') == []
    with pytest.raises(ValueError):
        parse_forum(CLOUDFLARE)


def test_parse_forum_skips_a_row_without_a_thread_link_or_time():
    broken = '<div class="structItem structItem--thread js-threadListItem-1"><a href="/forums/x.1/">x</a></div>'
    no_time = '<div class="structItem structItem--thread js-threadListItem-2"><div class="structItem-title"><a href="/threads/a.2/">a</a></div></div>'
    assert parse_forum(forum_page(broken, no_time)) == []


def test_parse_thread_strips_quotes_and_signatures():
    at = NOW - timedelta(minutes=5)
    body = (
        quote("someoneelse", "the quoted take")
        + "Suzuki is <b>cooking</b><br>tonight<br><br>  Second   para<div>Third</div>"
    )
    page = parse_thread(
        thread_page("Game Day: Habs vs Leafs", post_article("207182893", "fan_one", at, body))
    )
    assert page.title == "Game Day: Habs vs Leafs"
    [post] = page.posts
    assert post.post_id == "207182893"
    assert post.author == "fan_one"
    assert post.created_at == at.replace(microsecond=0)  # the attribution's time, not the edit's
    assert "quoted take" not in post.body and "someoneelse" not in post.body
    assert "Click to expand" not in post.body and "Go Habs Go forever" not in post.body
    assert post.body == "Suzuki is cooking\ntonight\n\nSecond para\nThird"


def test_parse_thread_reads_an_iso_time_with_a_colonless_offset():
    html = thread_page("t", post_article("5", "a", NOW, "hello")).replace(
        f'data-timestamp="{int(NOW.timestamp())}"', ""
    )
    html = html.replace(iso(NOW), "2026-09-28T11:00:00-0400")
    [post] = parse_thread(html).posts
    assert post.created_at == datetime(2026, 9, 28, 15, 0, tzinfo=UTC)


def test_parse_thread_rejects_a_page_that_is_not_a_thread():
    with pytest.raises(ValueError):
        parse_thread(CLOUDFLARE)


def test_text_of_ignores_scripts():
    root = parse.parse_html("<div>a<script>var b = 1;</script> c</div>")
    assert parse.text_of(root) == "a c"


# --- the crawl ---------------------------------------------------------------------------

LEAGUES = parse_leagues(
    f"""
[[league]]
key = "nhl"
name = "NHL"
sport = "hockey"
hfboards_forums = ["{HABS_FORUM}", "{LEAFS_FORUM}"]
"""
)
HABS = Entity("Q1", "team", "Montreal Canadiens", ("Habs",), frozenset({"nhl"}))


def test_the_parsed_shapes_are_the_parser_types():
    root = parse.parse_html("<div class='a b'><p>x</p></div>")
    assert isinstance(root, parse.Element)
    div = root.find("div")
    assert div is not None and div.has_class("b")
    rows = parse_forum(forum_page(thread_row("7", "game-day", "Game day", NOW)))
    assert [type(r) for r in rows] == [parse.ThreadRow]
    page = parse_thread(thread_page("Game day", post_article("9", "fan", NOW, "Go Habs")))
    assert isinstance(page, parse.ThreadPage) and [type(p) for p in page.posts] == [parse.ForumPost]


def test_latest_url_and_to_post():
    row = parse.ThreadRow("7", "/threads/game-day.7/", "Game day", NOW, 12)
    assert scraper.latest_url(row) == f"{BASE}/threads/game-day.7/latest"
    post = scraper.to_post(parse.ForumPost("9", "fan", NOW, "Go Habs"), HABS_FORUM, "7", "Game day")
    assert (post.platform, post.external_id, post.channel) == (HFBOARDS, "9", "montreal-canadiens")
    assert post.url == f"{BASE}/posts/9/" and post.parent_id == "7"
    assert post.raw == {"link_title": "Game day"} and post.author.name == "fan"
    assert scraper.to_post(parse.ForumPost("9", None, NOW, "x"), HABS_FORUM, "7", None).raw == {}


def make_scraper(engine, browser, clock=None, forums_per_cycle=1, threads=2):
    clock = clock or Clock()
    throttle = Throttle(
        Policy(HFBOARDS, 0, 0, 1000, 1000), MemoryThrottleStore(), clock=clock, sleep=lambda s: None
    )
    ingestor = SportsIngestor(engine, Matcher([HABS]), clock, collect_names=False)
    plan = HFBoardsPlan(forum_contexts(LEAGUES), forums_per_cycle, threads)
    return HFBoardsScraper(browser, throttle, ingestor, engine, plan, clock)


def stored(engine):
    with engine.connect() as conn:
        return conn.execute(select(schema.sports_posts)).mappings().all()


def habs_browser(posts_at: list[datetime], last: datetime | None = None):
    last = last or max(posts_at)
    articles = [
        post_article(str(100 + i), f"fan{i}", at, f"Habs post {i}") for i, at in enumerate(posts_at)
    ]
    return FakeBrowser(
        {
            forum_url(HABS_FORUM): html_ok(
                forum_page(thread_row("77", "game-day", "Game Day", last))
            ),
            forum_url(LEAFS_FORUM): html_ok(forum_page()),
            f"{BASE}/threads/game-day.77/latest": html_ok(thread_page("Game Day", *articles)),
        }
    )


def test_forum_contexts_unions_leagues():
    leagues = parse_leagues(
        """
[[league]]
key = "nhl"
name = "NHL"
sport = "hockey"
hfboards_forums = ["a.1"]

[[league]]
key = "ahl"
name = "AHL"
sport = "hockey"
hfboards_forums = ["a.1", "b.2"]
"""
    )
    assert forum_contexts(leagues) == {"a.1": frozenset({"nhl", "ahl"}), "b.2": frozenset({"ahl"})}
    assert channel_of("montreal-canadiens.16") == "montreal-canadiens"


def test_crawl_stores_new_posts_with_the_forums_league(engine):
    times = [NOW - timedelta(minutes=30), NOW - timedelta(minutes=10)]
    s = make_scraper(engine, habs_browser(times))
    stats = s.run()
    assert stats.errors == [] and stats.requests == 2
    rows = sorted(stored(engine), key=lambda r: r["external_id"])
    assert [r["external_id"] for r in rows] == ["100", "101"]
    row = rows[0]
    assert row["platform"] == HFBOARDS and row["kind"] == "post"
    assert row["channel"] == "montreal-canadiens" and row["parent_id"] == "77"
    assert row["leagues"] == ["nhl"] and row["entities"] == ["Q1"]
    assert row["url"] == f"{BASE}/posts/100/"
    assert row["raw"] == {"link_title": "Game Day"}
    assert row["author_hash"] and row["author_hash"] != "fan0"
    assert s.sports.context == frozenset()  # reset after the forum


def test_second_run_skips_a_thread_with_nothing_new(engine):
    times = [NOW - timedelta(minutes=30)]
    clock = Clock()
    make_scraper(engine, habs_browser(times), clock).run()
    browser = habs_browser(times)
    clock.now = NOW + timedelta(hours=2)
    stats = make_scraper(engine, browser, clock, forums_per_cycle=2).run()
    assert f"{BASE}/threads/game-day.77/latest" not in browser.calls
    assert stats.ingest.stored == 0


def test_only_posts_past_the_cursor_are_ingested(engine):
    clock = Clock()
    first = NOW - timedelta(minutes=30)
    make_scraper(engine, habs_browser([first]), clock).run()
    later = NOW + timedelta(minutes=20)
    clock.now = NOW + timedelta(hours=1)
    stats = make_scraper(engine, habs_browser([first, later]), clock, forums_per_cycle=2).run()
    assert stats.ingest.seen == 1
    with engine.connect() as conn:
        cursor = repository.get_state(conn, THREAD_PREFIX + "77")
    assert datetime.fromisoformat(cursor) == later.replace(microsecond=0)


def test_an_old_thread_never_read_is_left_alone(engine):
    old = NOW - timedelta(days=5)
    browser = habs_browser([old])
    stats = make_scraper(engine, browser).run()
    assert stats.requests == 1 and stored(engine) == []


def test_threads_per_forum_caps_newest_first(engine):
    rows = [thread_row(str(i), f"t{i}", f"T{i}", NOW - timedelta(minutes=i)) for i in (1, 2, 3)]
    pages = {forum_url(HABS_FORUM): html_ok(forum_page(*rows))}
    for i in (1, 2, 3):
        pages[f"{BASE}/threads/t{i}.{i}/latest"] = html_ok(
            thread_page(f"T{i}", post_article(f"5{i}", "a", NOW - timedelta(minutes=i), "Habs win"))
        )
    browser = FakeBrowser(pages)
    make_scraper(engine, browser, threads=2).run()
    assert [c for c in browser.calls if "/threads/" in c] == [
        f"{BASE}/threads/t1.1/latest",
        f"{BASE}/threads/t2.2/latest",
    ]


def test_forums_rotate_least_recently_visited_first(engine):
    clock = Clock()
    empty = html_ok(forum_page())
    browser = FakeBrowser({forum_url(HABS_FORUM): empty, forum_url(LEAFS_FORUM): empty})
    make_scraper(engine, browser, clock).run()
    clock.now += timedelta(minutes=30)
    make_scraper(engine, browser, clock).run()
    clock.now += timedelta(minutes=30)
    make_scraper(engine, browser, clock).run()
    assert browser.calls == [forum_url(HABS_FORUM), forum_url(LEAFS_FORUM), forum_url(HABS_FORUM)]
    with engine.connect() as conn:
        assert repository.get_state(conn, VISITED_PREFIX + LEAFS_FORUM)


def test_a_cloudflare_page_is_a_challenge_and_ends_the_run(engine):
    browser = FakeBrowser()
    browser.default = PageResult(200, BASE, html=CLOUDFLARE, text="Just a moment...")
    s = make_scraper(engine, browser, forums_per_cycle=2)
    stats = s.run()
    assert len(browser.calls) == 1  # the second forum is not knocked on
    assert "challenge" in stats.errors[0]
    assert s.throttle.store.get_block(HFBOARDS).blocked_until is not None


def test_a_403_wall_is_a_challenge(engine):
    browser = FakeBrowser()
    browser.default = PageResult(
        403, BASE, html="<html>blocked</html>", text="Verify you are human"
    )
    stats = make_scraper(engine, browser).run()
    assert "challenge" in stats.errors[0]


def test_a_rate_limit_ends_the_run(engine):
    browser = FakeBrowser()
    browser.default = PageResult(429, BASE)
    stats = make_scraper(engine, browser, forums_per_cycle=2).run()
    assert len(browser.calls) == 1 and "HTTP 429" in stats.errors[0]


def test_an_unparseable_forum_is_an_error_and_the_run_goes_on(engine):
    browser = FakeBrowser(
        {
            forum_url(HABS_FORUM): html_ok("<html><body>new layout</body></html>"),
            forum_url(LEAFS_FORUM): html_ok(forum_page()),
        }
    )
    stats = make_scraper(engine, browser, forums_per_cycle=2).run()
    assert len(browser.calls) == 2
    assert stats.errors == [f"forum {HABS_FORUM}: unparseable page (no thread list on the page)"]


def test_a_missing_forum_is_a_notice(engine):
    browser = FakeBrowser({forum_url(HABS_FORUM): PageResult(404, BASE, html="<html>404</html>")})
    stats = make_scraper(engine, browser).run()
    assert stats.errors == [] and "not found" in stats.notices[0]


def test_crawl_sports_reads_its_settings(engine):
    settings = IsolatedSettings(hfboards_forums_per_cycle=1, hfboards_threads_per_forum=1)
    throttle = Throttle(
        Policy(HFBOARDS, 0, 0, 1000, 1000), MemoryThrottleStore(), sleep=lambda s: None
    )
    # `crawl_sports` runs on the real clock, so the canned post is recent against it.
    browser = habs_browser([datetime.now(UTC) - timedelta(minutes=5)])
    stats = crawl_sports(settings, LEAGUES, Matcher([HABS]), engine, browser, throttle)
    assert stats.errors == [] and stats.ingest.stored == 1
    assert browser.calls[0] == forum_url(HABS_FORUM)
    assert scraper.NEW_THREAD_WINDOW == timedelta(hours=48)
