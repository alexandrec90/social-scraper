"""The sports domain's part of a cycle: prepare once, crawl each platform, discover.

`prepare` reads the league list, refreshes stale leagues from Wikidata and builds the
matcher; `run_platform` crawls one platform in its own browser session, after the stock
domain's, under the same per-platform throttle (so one budget covers both domains);
`discover_names` spends a few Wikidata lookups on names that keep coming up.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from social_scraper.config import Settings
from social_scraper.db import repository, schema
from social_scraper.db.throttle_store import DbThrottleStore
from social_scraper.fetch import Browser
from social_scraper.models import REDDIT
from social_scraper.reddit.scraper import RedditOptions, RedditRunStats, RedditScraper
from social_scraper.sports import discover, registry
from social_scraper.sports.leagues import League, load_leagues, subreddit_leagues
from social_scraper.sports.matcher import Matcher
from social_scraper.sports.ingest import SportsIngestor
from social_scraper.sports.sync import SyncPolicy, SyncStats, league_handles, sync_leagues
from social_scraper.sports.targets import pick_subreddits, subreddit_targets
from social_scraper.sports.wikidata import WikidataClient
from social_scraper.sports.x import SportsXPlan, SportsXScraper
from social_scraper.throttle import Policy, Throttle, ThrottleStop
from social_scraper.x import accounts
from social_scraper.x.scraper import XRunStats

log = logging.getLogger(__name__)

VISITED_PREFIX = "sports:reddit:visited:"
MENTION_WINDOW = timedelta(hours=24)
BrowserFactory = Callable[[str], AbstractContextManager[Browser]]
PolicyFor = Callable[[Settings, str], Policy]


@dataclass
class SportsContext:
    leagues: tuple[League, ...]
    matcher: Matcher
    team_subs: dict[str, str]
    sync: SyncStats


def build_matcher(engine: Engine, leagues: tuple[League, ...]) -> tuple[Matcher, dict[str, str]]:
    """The matcher, and `{team subreddit: entity id}` for leagues that crawl them."""
    keys = {lg.key for lg in leagues}
    crawl_teams = [lg.key for lg in leagues if lg.team_subreddits]
    with engine.connect() as conn:
        entities = [e for e in registry.load_entities(conn) if e.leagues & keys]
        all_team_subs = registry.team_subreddits(conn, keys)
    by_id = {e.entity_id: e for e in entities}
    channels = dict(subreddit_leagues(leagues))
    for sub, (eid, _league) in all_team_subs.items():
        if eid in by_id:
            channels.setdefault(sub.lower(), by_id[eid].leagues & keys)
    team_subs = {
        sub: eid
        for sub, (eid, league) in all_team_subs.items()
        if league in crawl_teams and eid in by_id
    }
    return Matcher(entities, channels), team_subs


def prepare(
    settings: Settings, engine: Engine, now: datetime, client: WikidataClient | None = None
) -> SportsContext:
    """League list, Wikidata refresh, matcher. Raises `LeagueConfigError` on a bad list."""
    leagues = load_leagues(settings.sports_leagues_file)
    policy = SyncPolicy(
        max_age=timedelta(hours=settings.sports_refresh_hours),
        players_per_team=settings.sports_players_per_team,
        limit=settings.sports_sync_leagues_per_cycle,
    )
    sync = sync_leagues(engine, client or WikidataClient(), leagues, now, policy)
    matcher, team_subs = build_matcher(engine, leagues)
    return SportsContext(leagues, matcher, team_subs, sync)


def crawl_reddit(
    settings: Settings,
    ctx: SportsContext,
    engine: Engine,
    browser: Browser,
    throttle: Throttle,
    now: datetime,
) -> RedditRunStats:
    with engine.connect() as conn:
        mentions = {eid: n for eid, _, n in registry.trending(conn, now - MENTION_WINDOW, 10_000)}
        visited = {
            k.lower(): datetime.fromisoformat(v)
            for k, v in repository.states_with_prefix(conn, VISITED_PREFIX).items()
        }
    league_subs = [s for lg in ctx.leagues for s in lg.subreddits]
    targets = subreddit_targets(league_subs, ctx.team_subs, mentions)
    chosen = pick_subreddits(targets, visited, now, settings.sports_reddit_subs_per_cycle)
    options = RedditOptions(
        max_pages=settings.sports_reddit_max_pages,
        refresh_hours=settings.reddit_refresh_hours,
        ignore_authors=settings.ignored_reddit_authors,
    )
    ingestor = SportsIngestor(engine, ctx.matcher)
    stats = RedditScraper(browser, throttle, ingestor, engine, options).run(tuple(chosen))
    with engine.begin() as conn:
        for sub in chosen:
            repository.set_state(conn, VISITED_PREFIX + sub.lower(), now.isoformat(), now)
    return stats


def x_plan(settings: Settings, leagues: tuple[League, ...]) -> SportsXPlan:
    return SportsXPlan(
        pages=settings.sports_x_pages_per_cycle,
        scrolls=settings.x_scrolls_per_page,
        search_share=settings.x_search_share,
        max_tracked=settings.sports_x_max_tracked_accounts,
        min_followers=settings.sports_x_min_followers,
        leagues=leagues,
    )


def crawl_x(
    settings: Settings,
    ctx: SportsContext,
    engine: Engine,
    browser: Browser,
    throttle: Throttle,
    now: datetime,
) -> XRunStats:
    seeds = [*settings.sports_x_seeds, *league_handles(engine, ctx.leagues)]
    with engine.begin() as conn:
        accounts.add_seeds(conn, seeds, now, schema.sports_x_accounts)
    ingestor = SportsIngestor(engine, ctx.matcher)
    plan = x_plan(settings, ctx.leagues)
    return SportsXScraper(browser, throttle, ingestor, engine, plan).run()


def run_platform(
    settings: Settings,
    platform: str,
    engine: Engine,
    ctx: SportsContext,
    factory: BrowserFactory,
    policy_for: PolicyFor,
) -> dict[str, Any]:
    """Crawl one platform for sports; everything that goes wrong lands in the dict."""
    throttle = Throttle(policy_for(settings, platform), DbThrottleStore(engine))
    if platform == REDDIT and settings.sports_reddit_subs_per_cycle <= 0:
        return {"skipped": "SPORTS_REDDIT_SUBS_PER_CYCLE is 0"}
    if platform != REDDIT and settings.sports_x_pages_per_cycle <= 0:
        return {"skipped": "SPORTS_X_PAGES_PER_CYCLE is 0"}
    try:
        throttle.check()
        with factory(platform) as browser:
            now = datetime.now(UTC)
            crawl = crawl_reddit if platform == REDDIT else crawl_x
            stats = crawl(settings, ctx, engine, browser, throttle, now)
    except ThrottleStop as exc:
        return {"skipped": str(exc)}
    except (PlaywrightError, SQLAlchemyError, OSError) as exc:
        log.exception("sports %s crawl failed", platform)
        return {"error": f"{type(exc).__name__}: {exc}"}
    out = asdict(stats)
    if stats.errors:
        out["error"] = "; ".join(stats.errors[:5])
    return out


def discover_names(
    settings: Settings, engine: Engine, now: datetime, client: WikidataClient | None = None
) -> dict[str, Any]:
    stats = discover.run_discovery(
        engine, client or WikidataClient(), now, settings.sports_lookups_per_cycle
    )
    return asdict(stats)
