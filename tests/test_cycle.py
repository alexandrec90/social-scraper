"""The sports domain's part of a cycle (`sports/cycle.py`), with no network: the leagues
here have no Wikidata ids, or a fake client answers for them."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from conftest import (
    NOW,
    FakeBrowser,
    IsolatedSettings,
    reddit_comment,
    reddit_listing,
    reddit_submission,
    x_search_payload,
    x_tweet_classic,
    x_user_classic,
)
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from sqlalchemy import select

from social_scraper.browser import TIMED_OUT
from social_scraper.db import repository, schema
from social_scraper.db.throttle_store import DbThrottleStore
from social_scraper.feeds import FeedRunStats
from social_scraper.fetch import LaunchTimedOut, PageResult, Payload
from social_scraper.models import BLUESKY, HFBOARDS, REDDIT, X
from social_scraper.runner import policy_for
from social_scraper.sports import cycle, registry
from social_scraper.sports.cycle import VISITED_PREFIX, build_matcher, prepare, run_platform
from social_scraper.sports.leagues import LeagueConfigError, load_leagues, subreddit_leagues
from social_scraper.sports.wikidata import TEAM, EntityRecord
from social_scraper.throttle import Block
from social_scraper.x import accounts

LEAGUES = """
[[league]]
key = "nhl"
name = "NHL"
sport = "hockey"
subreddits = ["nhl"]
team_subreddits = true
x_queries = ["NHL injury"]

[[league]]
key = "khl"
name = "KHL"
sport = "hockey"
subreddits = ["KHL"]
"""
HABS = EntityRecord("Q1", TEAM, "Montreal Canadiens", ("Habs",), subreddit="Habs", x_handle="CH")
SKA = EntityRecord("Q2", TEAM, "SKA Saint Petersburg", subreddit="ska")


@pytest.fixture
def settings(tmp_path):
    leagues = tmp_path / "sports-leagues.toml"
    leagues.write_text(LEAGUES, encoding="utf-8")
    return IsolatedSettings(
        data_dir=tmp_path / "data",
        archive_root=tmp_path / "lake",
        logs_dir=tmp_path / "logs",
        domains="sports",
        sports_leagues_file=leagues,
        sports_reddit_subs_per_cycle=2,
        sports_x_pages_per_cycle=2,
        x_search_share=0.5,
        reddit_min_delay_s=0,
        reddit_max_delay_s=0,
        reddit_active_hours="",
        x_active_hours="",
        x_min_delay_s=0,
        x_max_delay_s=0,
    )


def seed_entities(engine):
    with engine.begin() as conn:
        registry.replace_league(conn, "nhl", [HABS], NOW)
        registry.replace_league(conn, "khl", [SKA], NOW)


def reddit_browser(*subs):
    now = datetime.now(UTC).timestamp()
    pages = {}
    for sub in subs:
        base = f"https://www.reddit.com/r/{sub}/"
        listing = reddit_listing(
            [
                reddit_submission(f"{sub}1", now, subreddit=sub, title="Game thread"),
                reddit_comment(f"{sub}2", now, subreddit=sub, body="Habs look sharp"),
            ]
        )
        pages[base] = PageResult(200, base)
        for name in ("new", "comments", "top"):
            pages[base + f"{name}.json"] = PageResult(200, "u", data=listing)
    return FakeBrowser(pages)


def test_build_matcher_maps_league_and_team_subreddits(engine, settings):
    seed_entities(engine)
    matcher, team_subs = build_matcher(engine, load_leagues(settings.sports_leagues_file))
    assert len(matcher) == 2
    assert matcher.channel_leagues == {
        "nhl": frozenset({"nhl"}),
        "khl": frozenset({"khl"}),
        "habs": frozenset({"nhl"}),
        "ska": frozenset({"khl"}),
    }
    # Only leagues with `team_subreddits = true` put their teams' subreddits in rotation.
    assert team_subs == {"Habs": "Q1"}


def test_prepare_reads_the_list_and_skips_fresh_leagues(engine, settings):
    ctx = prepare(settings, engine, NOW)
    assert [lg.key for lg in ctx.leagues] == ["nhl", "khl"]
    assert ctx.sync.synced == {"nhl": 0, "khl": 0} and len(ctx.matcher) == 0
    assert prepare(settings, engine, NOW + timedelta(hours=1)).sync.synced == {}


def test_prepare_raises_on_a_bad_league_list(engine, settings):
    settings.sports_leagues_file.write_text("[[league]]\nkey = 1\n", encoding="utf-8")
    with pytest.raises(LeagueConfigError):
        prepare(settings, engine, NOW)


def test_reddit_crawl_rotates_subreddits_and_stores_sports_posts(engine, settings):
    prepare(settings, engine, datetime.now(UTC))  # the first sync, which finds nothing
    seed_entities(engine)
    ctx = prepare(settings, engine, datetime.now(UTC))
    browser = reddit_browser("nhl", "Habs", "KHL")
    result = run_platform(settings, REDDIT, engine, ctx, lambda p: browser, policy_for)
    # The two league subreddits outrank the quiet team one. Two subreddits x three
    # listings x two posts, each upserted; "Habs" counts in r/nhl, not in r/KHL.
    assert "error" not in result and result["ingest"]["stored"] == 12
    assert result["ingest"]["new_mentions"] == 1
    with engine.connect() as conn:
        visited = repository.states_with_prefix(conn, VISITED_PREFIX)
        posts = {r.external_id: r.leagues for r in conn.execute(select(schema.sports_posts))}
    assert len(visited) == 2  # SPORTS_REDDIT_SUBS_PER_CYCLE
    assert all(leagues for leagues in posts.values())
    # The next cycle visits what this one left out.
    result = run_platform(settings, REDDIT, engine, ctx, lambda p: browser, policy_for)
    with engine.connect() as conn:
        assert len(repository.states_with_prefix(conn, VISITED_PREFIX)) == 3


def test_x_crawl_seeds_league_accounts_and_uses_the_sports_tables(engine, settings):
    ctx = prepare(settings, engine, datetime.now(UTC))
    with engine.begin() as conn:
        repository.set_state(conn, "sports:league_x:nhl", "NHL", NOW)
    user = x_user_classic("7", "NHL", followers=9_000_000)
    feed = x_search_payload(x_tweet_classic("1", "Montreal Canadiens injury update", user))
    gql = "https://x.com/i/api/graphql/a/SearchTimeline?v=1"
    timeline_gql = "https://x.com/i/api/graphql/a/UserOriginalsTimeline?v=1"
    browser = FakeBrowser(
        {
            "https://x.com/search": PageResult(200, "s", payloads=[Payload(gql, 200, feed)]),
            "https://x.com/NHL": PageResult(200, "t", payloads=[Payload(timeline_gql, 200, feed)]),
        }
    )
    result = run_platform(settings, X, engine, ctx, lambda p: browser, policy_for)
    assert "error" not in result, result
    assert result["searches"] == 1 and result["timelines"] == 1
    with engine.connect() as conn:
        rows = accounts.load(conn, schema.sports_x_accounts)
    assert [(r.user_id, r.status) for r in rows] == [("7", "seed")]


def test_crawl_functions_run_directly_against_a_context(engine, settings):
    """`crawl_reddit` / `crawl_x` without the platform guard around them."""
    from social_scraper.sports.matcher import Matcher
    from social_scraper.sports.sync import SyncStats
    from social_scraper.throttle import MemoryThrottleStore, Policy, Throttle

    leagues = load_leagues(settings.sports_leagues_file)
    matcher = Matcher([], subreddit_leagues(leagues))  # no entities: league subreddits only
    ctx = cycle.SportsContext(leagues, matcher, {}, SyncStats())
    throttle = Throttle(Policy("p", 0, 0, 100, 100), MemoryThrottleStore(), sleep=lambda s: None)
    now = datetime.now(UTC)
    stats = cycle.crawl_reddit(settings, ctx, engine, reddit_browser("nhl", "KHL"), throttle, now)
    assert stats.ingest.stored == 12 and not stats.errors
    browser = FakeBrowser()
    browser.default = PageResult(200, "https://x.com/search")  # no data captured
    x_stats = cycle.crawl_x(settings, ctx, engine, browser, throttle, now)
    assert x_stats.searches == 0 and x_stats.errors


def test_run_platform_skips_and_errors(engine, settings):
    ctx = prepare(settings, engine, NOW)

    def never(platform):
        raise AssertionError("browser must not launch")

    settings.sports_reddit_subs_per_cycle = 0
    assert "0" in run_platform(settings, REDDIT, engine, ctx, never, policy_for)["skipped"]
    settings.sports_x_pages_per_cycle = 0
    assert "0" in run_platform(settings, X, engine, ctx, never, policy_for)["skipped"]
    settings.sports_x_pages_per_cycle = 2
    DbThrottleStore(engine).set_block(
        X, Block(datetime.now(UTC) + timedelta(hours=1), "captcha", 1), datetime.now(UTC)
    )
    assert "paused" in run_platform(settings, X, engine, ctx, never, policy_for)["skipped"]

    def crash(platform):
        raise PlaywrightError("Chrome not found")

    settings.sports_reddit_subs_per_cycle = 1
    assert (
        "Chrome not found"
        in run_platform(settings, REDDIT, engine, ctx, crash, policy_for)["error"]
    )

    def slow(platform):
        raise PlaywrightTimeout("Page.wait_for_selector: Timeout 30000ms exceeded.")

    result = run_platform(settings, REDDIT, engine, ctx, slow, policy_for)
    assert "error" not in result and "Timeout 30000ms" in result[TIMED_OUT]


def test_hfboards_and_bluesky_are_skipped_when_configured_off(engine, settings):
    ctx = prepare(settings, engine, NOW)

    def never(platform):
        raise AssertionError("browser must not launch")

    # The test leagues list no HFBoards forum.
    assert (
        "hfboards_forums"
        in run_platform(settings, HFBOARDS, engine, ctx, never, policy_for)["skipped"]
    )
    with_forum = replace(ctx, leagues=(replace(ctx.leagues[0], hfboards_forums=("nhl.1",)),))
    assert cycle.nothing_to_do(settings, HFBOARDS, with_forum) is None
    settings.hfboards_forums_per_cycle = 0
    assert "HFBOARDS_FORUMS_PER_CYCLE" in cycle.nothing_to_do(settings, HFBOARDS, with_forum)
    settings.bluesky_sports_searches_per_cycle = 0
    assert "0" in run_platform(settings, BLUESKY, engine, ctx, never, policy_for)["skipped"]


@pytest.mark.parametrize(
    ("platform", "crawl"), [(HFBOARDS, "hfboards_scraper"), (BLUESKY, "bluesky_live")]
)
def test_hfboards_and_bluesky_crawl_through_their_modules(
    engine, settings, monkeypatch, platform, crawl
):
    ctx = prepare(settings, engine, NOW)
    ctx = replace(ctx, leagues=(replace(ctx.leagues[0], hfboards_forums=("nhl.1",)),))
    settings = settings.model_copy(
        update={
            f"{platform}_active_hours": "",
            f"{platform}_min_delay_s": 0,
            f"{platform}_max_delay_s": 0,
        }
    )
    seen = {}

    def fake(s, leagues, matcher, eng, browser, throttle):
        seen.update(leagues=leagues, matcher=matcher, browser=browser)
        return FeedRunStats(requests=2, errors=["one forum: HTTP 500"])

    monkeypatch.setattr(getattr(cycle, crawl), "crawl_sports", fake)
    browser = FakeBrowser()
    result = run_platform(settings, platform, engine, ctx, lambda p: browser, policy_for)
    assert seen == {"leagues": ctx.leagues, "matcher": ctx.matcher, "browser": browser}
    assert result["requests"] == 2 and result["error"] == "one forum: HTTP 500"


@pytest.mark.parametrize(
    ("wrapper", "module"),
    [("crawl_hfboards", "hfboards_scraper"), ("crawl_bluesky", "bluesky_live")],
)
def test_the_crawl_wrappers_hand_the_context_on(engine, settings, monkeypatch, wrapper, module):
    ctx = prepare(settings, engine, NOW)
    got = {}
    monkeypatch.setattr(
        getattr(cycle, module),
        "crawl_sports",
        lambda s, leagues, matcher, eng, browser, throttle: (
            got.update(leagues=leagues, matcher=matcher) or FeedRunStats()
        ),
    )
    crawl = {"crawl_hfboards": cycle.crawl_hfboards, "crawl_bluesky": cycle.crawl_bluesky}[wrapper]
    assert crawl(settings, ctx, engine, FakeBrowser(), None, NOW) == FeedRunStats()
    assert got == {"leagues": ctx.leagues, "matcher": ctx.matcher}


def test_a_sports_browser_that_will_not_start_is_a_skip_until_it_keeps_happening(engine, settings):
    ctx = prepare(settings, engine, NOW)

    def stalled(platform):
        raise LaunchTimedOut("Timeout 180000ms exceeded.")

    first = run_platform(settings, REDDIT, engine, ctx, stalled, policy_for)
    assert "180000ms" in first["skipped"]
    run_platform(settings, REDDIT, engine, ctx, stalled, policy_for)
    assert "error" in run_platform(settings, REDDIT, engine, ctx, stalled, policy_for)


def test_discover_names_with_nothing_due_asks_nothing(engine, settings):
    class NoCalls:
        def api(self, **params):
            raise AssertionError("no lookup expected")

    out = cycle.discover_names(settings, engine, NOW, client=NoCalls())
    assert out["looked_up"] == 0 and out["error"] is None
