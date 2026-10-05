"""The sports X crawl (`sports/x.py`) against the same fakes as the stock one."""

from datetime import timedelta

import pytest
from conftest import (
    NOW,
    Clock,
    FakeBrowser,
    x_search_payload,
    x_tweet_2026,
    x_tweet_classic,
    x_user_classic,
)
from sqlalchemy import select

from social_scraper.db import repository, schema
from social_scraper.fetch import PageResult, Payload
from social_scraper.sports import registry
from social_scraper.sports.ingest import SportsIngestor
from social_scraper.sports.leagues import League
from social_scraper.sports.matcher import Matcher, NAME, EntityMention
from social_scraper.sports.wikidata import TEAM, EntityRecord
from social_scraper.sports.x import (
    QUERY_INDEX_KEY,
    SEARCHED_PREFIX,
    SportsXPlan,
    SportsXScraper,
    is_tout_text,
)
from social_scraper.throttle import MemoryThrottleStore, Policy, Throttle
from social_scraper.x import accounts

GQL = "https://x.com/i/api/graphql/abc/SearchTimeline?variables=1"
TIMELINE_GQL = "https://x.com/i/api/graphql/q/UserOriginalsTimeline?variables=1"
NHL = League("nhl", "NHL", "hockey", x_queries=("NHL injury min_faves:20",))
HABS = EntityRecord("Q1", TEAM, "Montreal Canadiens", ("Habs",))
REPORTER = x_user_classic("100", "beatwriter", followers=40_000)
FAN = x_user_classic("200", "fan", followers=800)


def page(*tweets):
    return PageResult(
        200, "https://x.com/search", payloads=[Payload(GQL, 200, x_search_payload(*tweets))]
    )


def timeline(*tweets):
    return PageResult(
        200, "https://x.com/u", payloads=[Payload(TIMELINE_GQL, 200, x_search_payload(*tweets))]
    )


def setup(engine, mentions=0):
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


def scraper(engine, matcher, browser, clock=None, **plan):
    clock = clock or Clock()
    throttle = Throttle(
        Policy("x", 0, 0, 1000, 1000), MemoryThrottleStore(), clock=clock, sleep=lambda s: None
    )
    plan = SportsXPlan(**{"pages": 2, "scrolls": 1, "leagues": (NHL,), **plan})
    ingestor = SportsIngestor(engine, matcher, clock)
    return SportsXScraper(browser, throttle, ingestor, engine, plan, clock)


@pytest.mark.parametrize(
    ("text", "tout"),
    [
        ("DM for picks, 10-2 last week", True),
        ("Free picks every night", True),
        ("Lock of the day: Habs ML", True),
        ("3u on the over", True),
        ("Join our VIP discord.gg/abc", True),
        ("Pronostics gratuits ce soir", True),
        ("Habs -1.5 at +180 looks like value", False),
        ("Suzuki is out tonight, lineup change", False),
        ("Parlay odds are terrible on this one", False),
    ],
)
def test_is_tout_text(text, tout):
    assert is_tout_text(text) is tout


def test_queries_alternate_trending_entities_and_league_queries(engine):
    matcher = setup(engine, mentions=3)
    clock = Clock()
    s = scraper(engine, matcher, FakeBrowser(), clock)
    assert s.queries(2) == [
        ('"Montreal Canadiens" (lang:en OR lang:fr)', True),
        ("NHL injury min_faves:20", False),
    ]
    assert s.contexts['"Montreal Canadiens" (lang:en OR lang:fr)'] == frozenset({"nhl"})
    with engine.connect() as conn:
        assert repository.get_state(conn, QUERY_INDEX_KEY) == "1"
        assert repository.get_state(conn, SEARCHED_PREFIX + "Q1") == NOW.isoformat()
    clock.now = NOW + timedelta(hours=1)
    assert s.queries(2) == [("NHL injury min_faves:20", False)] * 2  # entity searched recently
    assert s.queries(0) == []


def test_search_results_are_stored_as_about_the_league(engine):
    matcher = setup(engine)
    browser = FakeBrowser()
    browser.default = page(x_tweet_classic("1", "Huge news out of practice today", REPORTER))
    stats = scraper(engine, matcher, browser, queries=()).run()
    assert stats.searches == 2
    with engine.connect() as conn:
        (row,) = conn.execute(select(schema.sports_posts)).all()
        assert row.leagues == ["nhl"] and row.entities == []
        assert conn.execute(select(schema.posts)).first() is None
        assert {r.user_id for r in accounts.load(conn, schema.sports_x_accounts)} == {"100"}
        assert accounts.load(conn) == []  # the stock accounts table is untouched


def test_timeline_keeps_on_topic_tweets_and_counts_touts(engine):
    matcher = setup(engine)
    with engine.begin() as conn:
        accounts.add_seeds(conn, ["fan"], NOW, schema.sports_x_accounts)
    feed = timeline(
        x_tweet_classic("1", "Montreal Canadiens win in OT", FAN),
        x_tweet_classic("2", "DM for picks, Montreal Canadiens ML lock", FAN),
        x_tweet_classic("3", "lunch", FAN),
    )
    browser = FakeBrowser({"https://x.com/fan": feed})
    browser.default = page()
    stats = scraper(engine, matcher, browser, pages=2, search_share=0.5).run()
    assert stats.timelines == 1
    with engine.connect() as conn:
        stored = {r.external_id for r in conn.execute(select(schema.sports_posts))}
        (fan,) = accounts.load(conn, schema.sports_x_accounts)
    assert stored == {"1", "2"}
    assert (fan.timeline_tweets, fan.timeline_relevant_tweets, fan.tout_tweets) == (3, 2, 1)


def test_organisation_accounts_are_marked_affiliated(engine):
    matcher = setup(engine)
    browser = FakeBrowser()
    # The 2026 shape's user carries `verification.verified_type: "Business"`.
    browser.default = page(x_tweet_2026("9", "Montreal Canadiens lineup", "300", "TeamAccount"))
    scraper(engine, matcher, browser, pages=1).run()
    with engine.connect() as conn:
        (org,) = accounts.load(conn, schema.sports_x_accounts)
    assert org.affiliated is True
