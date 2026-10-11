"""The live Bluesky crawl (`bluesky/live.py`) against canned `searchPosts` answers.

Payloads mirror the shape captured live on 2026-10-10 with invented DIDs and text.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import NOW, Clock, FakeBrowser, IsolatedSettings

from social_scraper.bluesky import live
from social_scraper.db import repository, schema
from social_scraper.fetch import PageResult
from social_scraper.models import BLUESKY, stable_hash
from social_scraper.pipeline import Ingestor
from social_scraper.sports import registry
from social_scraper.sports.leagues import League
from social_scraper.sports.matcher import NAME, EntityMention, Matcher
from social_scraper.sports.wikidata import TEAM, EntityRecord
from social_scraper.throttle import MemoryThrottleStore, Policy, Throttle

DID = "did:plc:testauthor000000000000aa"
OTHER = "did:plc:testparent000000000000bb"
STAMP = "2026-09-28T14:00:00.000Z"


def bsky_post(
    rkey: str, text: str, did: str = DID, created: str = STAMP, tags=(), reply=None, **over
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "$type": "app.bsky.feed.post",
        "createdAt": created,
        "text": text,
        "langs": ["en"],
        "facets": [
            {
                "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": t}],
                "index": {"byteStart": 0, "byteEnd": 1},
            }
            for t in tags
        ]
        + [{"features": [{"$type": "app.bsky.richtext.facet#link", "uri": "https://example.com"}]}],
    }
    if reply:
        record["reply"] = reply
    return {
        "uri": f"at://{did}/app.bsky.feed.post/{rkey}",
        "cid": f"bafy{rkey}",
        "author": {"did": did, "handle": "someone.bsky.social", "displayName": "Someone"},
        "record": record,
        "replyCount": 2,
        "repostCount": 3,
        "likeCount": 7,
        "quoteCount": 1,
        "bookmarkCount": 0,
        "indexedAt": created,
        **over,
    }


def answer(*posts) -> PageResult:
    return PageResult(200, live.SEARCH_URL, data={"posts": list(posts), "cursor": "1"})


def throttle(clock=None):
    return Throttle(
        Policy(BLUESKY, 0, 0, 1000, 1000),
        MemoryThrottleStore(),
        clock=clock or Clock(),
        sleep=lambda s: None,
    )


def settings(**over):
    values = {
        "watchlist": "TSLA",
        "bluesky_searches_per_cycle": 1,
        "bluesky_sports_searches_per_cycle": 2,
        **over,
    }
    return IsolatedSettings(**values)


def stored(engine, table=schema.posts):
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(table.select())]


# --- parsing --------------------------------------------------------------------------


def test_parse_post_hashes_every_identifier():
    reply = {
        "root": {"uri": f"at://{OTHER}/app.bsky.feed.post/root1"},
        "parent": {"uri": f"at://{OTHER}/app.bsky.feed.post/par1"},
    }
    item = bsky_post("r1", "$TSLA puts printing", reply=reply)
    post = live.parse_post(item)
    assert post is not None
    assert post.external_id == stable_hash(item["uri"]) and len(post.external_id) == 64
    assert post.kind == "reply" and post.channel == "search" and post.url is None
    assert post.parent_id == stable_hash(reply["parent"]["uri"])
    assert post.raw == {"conversation_id": stable_hash(reply["root"]["uri"])}
    assert post.author.name is None and post.author.hash == stable_hash(DID)
    assert post.created_at.isoformat() == "2026-09-28T14:00:00+00:00"
    assert post.lang == "en"
    e = post.engagement
    assert (e.score, e.likes, e.comments, e.reposts, e.quotes, e.bookmarks) == (7, 7, 2, 3, 1, 0)
    # No DID anywhere a row is built from.
    fields = (post.external_id, post.parent_id, post.url, post.author.name, str(post.raw))
    assert not any(DID in str(f) or OTHER in str(f) or "did:" in str(f) for f in fields)


def test_parse_post_top_level_and_fallbacks():
    item = bsky_post("p1", "hello", created="bad", indexedAt="2026-09-28T13:00:00Z")
    post = live.parse_post(item)
    assert post is not None and post.kind == "post" and post.parent_id is None and post.raw == {}
    assert post.created_at.hour == 13
    assert live.parse_post({"uri": "at://x", "record": {"text": " ", "createdAt": STAMP}}) is None
    assert live.parse_post({"record": {"text": "x"}}) is None


@pytest.mark.parametrize("payload", [None, [], {"feed": []}, {"posts": None}])
def test_parse_search_rejects_what_is_not_a_search(payload):
    with pytest.raises(ValueError):
        live.parse_search(payload)


def test_hashtags_reads_tag_facets_only():
    item = bsky_post("p", "x", tags=("TSLA", "stocks"))
    assert live.hashtags(item["record"]) == ("TSLA", "stocks")
    assert live.hashtags({}) == ()


def test_search_url_carries_since_only_when_known():
    q = parse_qs(urlsplit(live.search_url("$TSLA")).query)
    assert q == {"q": ["$TSLA"], "sort": ["latest"], "limit": [str(live.PAGE_SIZE)]}
    assert parse_qs(urlsplit(live.search_url("$TSLA", STAMP)).query)["since"] == [STAMP]


# --- stocks ---------------------------------------------------------------------------


def test_crawl_keeps_strict_cashtags_and_symbol_hashtags_only(engine, universe):
    browser = FakeBrowser(
        {
            live.SEARCH_URL: answer(
                bsky_post("a", "$TSLA breaking out"),
                bsky_post("b", "TSLA sentiment rose to 6.0/10"),  # search ignored the `$`
                bsky_post("c", "ended the week with a call build", tags=("TSLA",)),
                bsky_post("d", "lowercase tag", tags=("tsla",)),
            )
        }
    )
    ingestor = Ingestor(engine, universe, Clock())
    stats = live.crawl(settings(), engine, ingestor, browser, throttle())
    assert stats.errors == [] and stats.requests == 1
    rows = {r["body"]: r for r in stored(engine)}
    assert set(rows) == {"$TSLA breaking out", "ended the week with a call build"}
    assert rows["ended the week with a call build"]["symbols"] == ["TSLA"]
    assert all(r["platform"] == BLUESKY and r["url"] is None for r in rows.values())
    assert parse_qs(urlsplit(browser.calls[0]).query)["q"] == ["$TSLA"]


def test_crawl_resumes_from_the_newest_post_seen(engine, universe):
    clock = Clock()
    browser = FakeBrowser(
        {
            live.SEARCH_URL: answer(
                bsky_post("a", "$TSLA one", created="2026-09-28T14:00:00Z"),
                bsky_post("b", "$TSLA two", created="2026-09-28T14:05:00Z"),
            )
        }
    )
    ingestor = Ingestor(engine, universe, clock)
    live.crawl(settings(), engine, ingestor, browser, throttle(clock))
    assert "since" not in parse_qs(urlsplit(browser.calls[0]).query)
    clock.now = NOW + live.SYMBOL_GAP
    live.crawl(settings(), engine, ingestor, browser, throttle(clock))
    assert parse_qs(urlsplit(browser.calls[1]).query)["since"] == ["2026-09-28T14:05:00+00:00"]


def test_a_future_dated_post_never_moves_the_cursor_past_now(engine, universe):
    clock = Clock()
    browser = FakeBrowser(
        {live.SEARCH_URL: answer(bsky_post("f", "$TSLA", created="2027-01-01T00:00:00Z"))}
    )
    live.crawl(settings(), engine, Ingestor(engine, universe, clock), browser, throttle(clock))
    with engine.connect() as conn:
        assert repository.get_state(conn, live.SINCE_PREFIX + "TSLA") == NOW.isoformat()


def test_crawl_paces_each_symbol(engine, universe):
    browser = FakeBrowser({live.SEARCH_URL: answer()})
    ingestor = Ingestor(engine, universe, Clock())
    live.crawl(settings(), engine, ingestor, browser, throttle())
    live.crawl(settings(), engine, ingestor, browser, throttle())
    assert len(browser.calls) == 1  # TSLA searched within its gap: nothing to read


@pytest.mark.parametrize(
    ("result", "where", "stops"),
    [
        (PageResult(429, "u"), "errors", True),
        (PageResult(403, "u", text="403 Forbidden"), "errors", True),
        (PageResult(502, "u"), "errors", False),
        (PageResult(200, "u", data={"error": "x"}), "errors", False),
        (PageResult(None, "u", timed_out=True), "notices", False),
    ],
)
def test_crawl_failures(engine, universe, result, where, stops):
    browser = FakeBrowser({live.SEARCH_URL: result})
    ingestor = Ingestor(engine, universe, Clock())
    s = settings(watchlist="TSLA,NVDA", bluesky_searches_per_cycle=2)
    stats = live.crawl(s, engine, ingestor, browser, throttle())
    assert getattr(stats, where), stats
    assert len(browser.calls) == (1 if stops else 2)


def test_crawl_404_is_nothing_found(engine, universe):
    browser = FakeBrowser({live.SEARCH_URL: PageResult(404, "u")})
    ingestor = Ingestor(engine, universe, Clock())
    stats = live.crawl(settings(), engine, ingestor, browser, throttle())
    assert stats.errors == [] and stored(engine) == []


def test_about_symbol_keeps_a_cashtag_or_the_symbols_hashtag(universe):
    tagged = live.Result(live.parse_post(bsky_post("t", "quiet day")), ("TSLA",))
    kept = live.about_symbol(tagged, "TSLA", universe)
    assert kept is not None and kept.tagged_symbols == ("TSLA",)
    cash = live.Result(live.parse_post(bsky_post("c", "long $NVDA here")), ())
    assert live.about_symbol(cash, "TSLA", universe) is cash.post
    bare = live.Result(live.parse_post(bsky_post("b", "TSLA is up")), ("stocks",))
    assert live.about_symbol(bare, "TSLA", universe) is None


def test_stock_search_reads_one_symbol_and_bluesky_search_moves_its_cursor(engine, universe):
    clock = Clock()
    browser = FakeBrowser({live.SEARCH_URL: answer(bsky_post("a", "$TSLA ripping"))})
    crawler = live.StockSearch(
        browser, throttle(clock), Ingestor(engine, universe, clock), engine, clock
    )
    assert isinstance(crawler, live.BlueskySearch)
    crawler.read_symbol("TSLA")
    assert crawler.stats.ingest.stored == 1 and len(stored(engine)) == 1
    results = crawler.search("$TSLA", "k")
    assert [type(r) for r in results] == [live.Result]
    assert crawler.get_state("k") == "2026-09-28T14:00:00+00:00"


# --- sports ---------------------------------------------------------------------------


def test_entity_query_quotes_the_name():
    assert live.entity_query("Montreal Canadiens") == '"Montreal Canadiens"'


def test_sports_search_reads_one_query_with_its_context(engine):
    matcher = sports_setup(engine)
    clock = Clock()
    browser = FakeBrowser({live.SEARCH_URL: answer(bsky_post("s", "What a save"))})
    ingestor = live.SportsIngestor(engine, matcher, clock)
    crawler = live.SportsSearch(browser, throttle(clock), ingestor, engine, clock)
    crawler.read(live.SportsQuery("NHL injury", "NHL injury", frozenset({"nhl"})))
    assert [r["leagues"] for r in stored(engine, schema.sports_posts)] == [["nhl"]]
    assert ingestor.context == frozenset()  # reset after the query


NHL = League("nhl", "NHL", "hockey", bluesky_queries=("NHL injury", "LNH blessure"))
HABS = EntityRecord("Q1", TEAM, "Montreal Canadiens", ("Habs",))


def sports_setup(engine, mentions=3):
    with engine.begin() as conn:
        registry.replace_league(conn, "nhl", [HABS], NOW)
        for i in range(mentions):
            row = {
                "platform": "reddit",
                "external_id": f"p{i}",
                "channel": "nhl",
                "created_at": NOW,
            }
            registry.record_mentions(conn, row, [EntityMention("Q1", NAME)], NOW)
        entities = registry.load_entities(conn)
    return Matcher(entities, {"nhl": frozenset({"nhl"})})


def test_sports_queries_alternate_entities_and_league_queries(engine):
    matcher = sports_setup(engine)
    out = live.sports_queries(engine, (NHL,), matcher, 3, NOW)
    assert [q.query for q in out] == ['"Montreal Canadiens"', "NHL injury", "LNH blessure"]
    assert out[0].key == "Q1" and out[0].context == frozenset({"nhl"})
    assert out[1].key == "NHL injury" and out[1].context == NHL.context
    with engine.connect() as conn:
        assert repository.get_state(conn, live.SPORTS_QUERY_INDEX_KEY) == "2"
        assert repository.get_state(conn, live.SPORTS_SEARCHED_PREFIX + "Q1") == NOW.isoformat()
    # The entity is paced; the league queries rotate on.
    again = live.sports_queries(engine, (NHL,), matcher, 2, NOW + timedelta(minutes=30))
    assert [q.query for q in again] == ["NHL injury", "LNH blessure"]
    assert live.sports_queries(engine, (NHL,), matcher, 0, NOW) == []


def test_crawl_sports_stores_results_about_the_query_leagues(engine):
    matcher = sports_setup(engine)
    browser = FakeBrowser(
        {
            live.SEARCH_URL: answer(
                bsky_post("s1", "What a save tonight"),
                bsky_post("s2", "The Habs looked sharp"),
            )
        }
    )
    stats = live.crawl_sports(settings(), (NHL,), matcher, engine, browser, throttle())
    assert stats.errors == [] and stats.requests == 2
    rows = stored(engine, schema.sports_posts)
    assert {r["body"] for r in rows} == {"What a save tonight", "The Habs looked sharp"}
    assert all(r["leagues"] == ["nhl"] for r in rows)
    habs = next(r for r in rows if "Habs" in r["body"])
    assert habs["entities"] == ["Q1"]
    queries = [parse_qs(urlsplit(u).query)["q"][0] for u in browser.calls]
    assert queries == ['"Montreal Canadiens"', "NHL injury"]
    with engine.connect() as conn:
        assert repository.get_state(conn, live.SPORTS_SINCE_PREFIX + "Q1") is not None
