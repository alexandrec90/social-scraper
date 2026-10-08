"""One scrape cycle across platforms, and the loop that repeats it.

A cycle: prepare each enabled domain (`DOMAINS`: the stock symbol universe; the sports
league list, Wikidata refresh and matcher), crawl each platform in its own browser
profile -- the stock domain, then sports, under one per-platform throttle -- export to
the lake, and write `logs/scrape-run.json` -- on failure as on success, overwritten
each run -- with everything needed to diagnose it. The terminal gets a status line and
that path (CLAUDE.md, "Failure artifacts").

One platform or domain failing never stops another, and a failed export never fails
the cycle: the lake degrades, it does not block.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from social_scraper.browser import PlaywrightBrowser
from social_scraper.config import SPORTS, STOCKS, Settings
from social_scraper.db.throttle_store import REQUEST_LOG_RETENTION_DAYS, DbThrottleStore
from social_scraper.export import export_to_lake
from social_scraper.fetch import Browser
from social_scraper.models import REDDIT, X
from social_scraper.pipeline import Ingestor
from social_scraper.reddit.scraper import RedditOptions, RedditScraper
from social_scraper.sports import cycle as sports_cycle
from social_scraper.sports.cycle import PolicyFor, SportsContext
from social_scraper.sports.leagues import LeagueConfigError
from social_scraper.symbols.universe import Universe, load_universe
from social_scraper.throttle import Policy, Throttle, ThrottleStop
from social_scraper.x import accounts
from social_scraper.x.scraper import XPlan, XScraper

log = logging.getLogger(__name__)

REPORT_FILE = "scrape-run.json"
# Timing jitter only; SystemRandom because nothing here needs a reproducible sequence.
_JITTER = secrets.SystemRandom()
BrowserFactory = Callable[[str], AbstractContextManager[Browser]]
# The share of a scheduled cycle in which a page load may start (`cycle_deadline`). The
# rest is the tail -- the load in flight, closing Chrome, the export -- so the run ends
# before the scheduler's next fire, which it skips while one is still going. The start
# jitter, Reddit and X fill 20-25 minutes of a 30-minute cycle, so under load they did
# not fit: 2026-10-07, a 29-minute run, and the second skipped fire that day.
CYCLE_DEADLINE_SHARE = 0.75
# The share of a scheduled cycle by which the export stops (`export_deadline`); what is
# left over waits for the next run (`export.py`). The rest -- one partition write in
# flight, the report, the process exiting -- fits in the last 10%. 2026-10-07: a
# backfill held the lake's locks and the 20:30 export ran until the wrapper's 55-minute
# timeout ended it, with the 21:00 fire skipped behind it.
EXPORT_DEADLINE_SHARE = 0.9
# When devkit's scheduler fired this `scrape`, as an ISO timestamp (devkit's
# `collector_tasks.FIRED_AT`). The scrape's own start is minutes later -- the db coming
# up, `uv run` starting -- and the next fire is due one cycle after the fire, not after
# that: 2026-10-07 17:30, the scrape started at 17:35:44 (compose 3m24s, uv 2m20s under
# load), so a deadline counted from it let the run reach past the 18:00 fire.
FIRED_AT_ENV = "DEVKIT_FIRED_AT"


@dataclass
class RunReport:
    started_at: str
    finished_at: str | None = None
    # Keyed by platform for stocks (`reddit`, `x`), `<platform>:sports` for sports.
    platforms: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Sports only: league and entity counts, the Wikidata sync, discovery.
    sports: dict[str, Any] = field(default_factory=dict)
    export: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def cycle_deadline(started: datetime, cycle_minutes: float) -> datetime:
    """When the last page load of a cycle that started at `started` may start."""
    return started + timedelta(minutes=cycle_minutes * CYCLE_DEADLINE_SHARE)


def export_deadline(crawl_deadline: datetime, cycle_minutes: float) -> datetime:
    """When the export stops and defers the rest, for the cycle whose last page load may
    start at `crawl_deadline` (`cycle_deadline`): the same start, a later share."""
    later = EXPORT_DEADLINE_SHARE - CYCLE_DEADLINE_SHARE
    return crawl_deadline + timedelta(minutes=cycle_minutes * later)


def cycle_start(now: datetime, fired: str | None) -> datetime:
    """When this cycle started: the scheduler's fire (`FIRED_AT_ENV`'s value) when it
    names one no later than `now`, else `now` -- a run by hand, or a value that is not a
    timestamp or is in the future, which is a clock that moved, not a fire."""
    try:
        at = datetime.fromisoformat((fired or "").strip())
    except ValueError:
        return now
    at = at.replace(tzinfo=UTC) if at.tzinfo is None else at
    return at if at <= now else now


def policy_for(settings: Settings, platform: str, deadline: datetime | None = None) -> Policy:
    """The throttle policy for `platform`: its own pace, budgets, hours and deadline."""
    reddit = platform == REDDIT
    s = settings
    return Policy(
        platform=platform,
        min_delay_s=s.reddit_min_delay_s if reddit else s.x_min_delay_s,
        max_delay_s=s.reddit_max_delay_s if reddit else s.x_max_delay_s,
        hourly_budget=s.reddit_hourly_budget if reddit else s.x_hourly_budget,
        daily_budget=s.reddit_daily_budget if reddit else s.x_daily_budget,
        backoff_base_minutes=s.backoff_base_minutes,
        backoff_max_minutes=s.backoff_max_minutes,
        block_cooldown_hours=s.block_cooldown_hours,
        block_cooldown_max_hours=s.block_cooldown_max_hours,
        active_hours=s.reddit_hours if reddit else s.x_hours,
        timezone=s.scraper_timezone,
        deadline=deadline,
    )


def x_plan(settings: Settings) -> XPlan:
    return XPlan(
        watchlist=settings.watchlist_symbols,
        queries=settings.x_queries,
        pages=settings.x_pages_per_cycle,
        scrolls=settings.x_scrolls_per_page,
        search_share=settings.x_search_share,
        max_tracked=settings.x_max_tracked_accounts,
        min_followers=settings.x_min_followers,
    )


def default_browser_factory(settings: Settings) -> BrowserFactory:
    def factory(platform: str) -> AbstractContextManager[Browser]:
        return PlaywrightBrowser(
            settings.profile_dir(platform),
            settings.browser_channel,
            settings.browser_headless,
            minimized=settings.browser_minimized,
        )

    return factory


def universe_for(settings: Settings) -> Universe:
    return load_universe(
        settings.data_dir / "symbols" / "nasdaqtraded.txt",
        settings.symbol_universe_url,
        settings.symbol_universe_max_age_hours,
        settings.extra_symbol_files,
    )


def _crawl(
    settings: Settings,
    platform: str,
    engine: Engine,
    ingestor: Ingestor,
    browser: Browser,
    policies: PolicyFor,
):
    throttle = Throttle(policies(settings, platform), DbThrottleStore(engine))
    if platform == REDDIT:
        options = RedditOptions(
            max_pages=settings.reddit_max_pages,
            refresh_hours=settings.reddit_refresh_hours,
            ignore_authors=settings.ignored_reddit_authors,
        )
        return RedditScraper(browser, throttle, ingestor, engine, options).run(settings.subreddits)
    with engine.begin() as conn:
        accounts.add_seeds(conn, settings.x_seeds, datetime.now(UTC))
    return XScraper(browser, throttle, ingestor, engine, x_plan(settings)).run()


def run_platform(
    settings: Settings,
    platform: str,
    engine: Engine,
    ingestor: Ingestor,
    factory: BrowserFactory,
    policies: PolicyFor = policy_for,
) -> dict[str, Any]:
    """Crawl one platform; everything that goes wrong lands in the returned dict."""
    try:
        # Check before launching Chrome: a paused platform should cost nothing.
        Throttle(policies(settings, platform), DbThrottleStore(engine)).check()
        with factory(platform) as browser:
            stats = _crawl(settings, platform, engine, ingestor, browser, policies)
    except ThrottleStop as exc:
        return {"skipped": str(exc)}
    except (PlaywrightError, SQLAlchemyError, OSError) as exc:
        log.exception("%s crawl failed", platform)
        return {"error": f"{type(exc).__name__}: {exc}"}
    out = asdict(stats)
    if stats.errors:
        out["error"] = "; ".join(stats.errors[:5])
    return out


def prepare_sports(settings: Settings, engine: Engine, report: RunReport) -> SportsContext | None:
    """The sports context, or None with the reason in `report` -- never raises."""
    try:
        ctx = sports_cycle.prepare(settings, engine, datetime.now(UTC))
    except LeagueConfigError as exc:
        report.errors.append(f"sports: {exc}")
        return None
    report.sports["leagues"] = len(ctx.leagues)
    report.sports["entities"] = len(ctx.matcher)
    report.sports["sync"] = asdict(ctx.sync)
    if ctx.sync.errors:
        # Wikidata down degrades freshness, not the crawl: a notice, not a failed cycle.
        report.sports["notice"] = "wikidata sync failed for some leagues; previous entities kept"
    return ctx


def run_cycle(
    settings: Settings,
    engine: Engine,
    platforms: tuple[str, ...] = (REDDIT, X),
    factory: BrowserFactory | None = None,
    export: bool = True,
    deadline: datetime | None = None,
) -> RunReport:
    """One cycle. No page load starts after `deadline`; the export still runs, and
    stops at `export_deadline`, deferring what is left to the next cycle."""
    report = RunReport(started_at=datetime.now(UTC).isoformat())
    domains = settings.enabled_domains
    factory = factory or default_browser_factory(settings)
    policies = partial(policy_for, deadline=deadline)
    ingestor = Ingestor(engine, universe_for(settings)) if STOCKS in domains else None
    sports = prepare_sports(settings, engine, report) if SPORTS in domains else None
    for platform in platforms:
        results: dict[str, dict[str, Any]] = {}
        if ingestor is not None:
            results[platform] = run_platform(
                settings, platform, engine, ingestor, factory, policies
            )
        if sports is not None:
            results[f"{platform}:{SPORTS}"] = sports_cycle.run_platform(
                settings, platform, engine, sports, factory, policies
            )
        for label, result in results.items():
            report.platforms[label] = result
            if "error" in result:
                report.errors.append(f"{label}: {result['error']}")
    if sports is not None:
        report.sports["discovery"] = sports_cycle.discover_names(
            settings, engine, datetime.now(UTC)
        )
    # Timestamped in the scheduled log, so a long tail shows which side of it the time went.
    log.info("crawls done; pruning and exporting")
    DbThrottleStore(engine).prune(datetime.now(UTC) - timedelta(days=REQUEST_LOG_RETENTION_DAYS))
    if export:
        until = export_deadline(deadline, settings.cycle_minutes) if deadline else None
        exported = export_to_lake(engine, settings, datetime.now(UTC), until=until)
        report.export = [asdict(d) for d in exported.datasets]
        report.errors.extend(f"export {d.dataset}: {d.error}" for d in exported.datasets if d.error)
    report.finished_at = datetime.now(UTC).isoformat()
    return report


def write_report(report: RunReport, logs_dir: Path) -> Path:
    logs_dir.mkdir(parents=True, exist_ok=True)
    path = logs_dir / REPORT_FILE
    body = {**asdict(report), "ok": report.ok}
    path.write_text(json.dumps(body, indent=2, default=str), encoding="utf-8")
    return path


def start_jitter_s(cycle_minutes: float) -> float:
    """A random 0-20% of a cycle, in seconds: the wait before a scheduled `scrape`.

    On a fixed scheduler grid that spreads the gap between cycles over 0.8-1.2x
    `CYCLE_MINUTES` -- the same spread `run_forever` sleeps.
    """
    return cycle_minutes * 60 * _JITTER.uniform(0.0, 0.2)


def run_forever(
    settings: Settings,
    engine: Engine,
    platforms: tuple[str, ...],
    cycles: int | None = None,
    export: bool = True,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Repeat `run_cycle` every `CYCLE_MINUTES` (+/- 20% jitter), `cycles` times or forever."""
    done = 0
    while cycles is None or done < cycles:
        report = run_cycle(settings, engine, platforms, export=export)
        path = write_report(report, settings.logs_dir)
        log.info("cycle %s -- report at %s", "ok" if report.ok else "had errors", path)
        done += 1
        if cycles is None or done < cycles:
            sleep(settings.cycle_minutes * 60 * _JITTER.uniform(0.8, 1.2))
