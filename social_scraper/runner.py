"""One scrape cycle across platforms, and the loop that repeats it.

A cycle: load the symbol universe, crawl each platform in its own browser profile,
export to the lake, and write `logs/scrape-run.json` -- on failure as on success,
overwritten each run -- with everything needed to diagnose it. The terminal gets a
status line and that path (CLAUDE.md, "Failure artifacts").

One platform failing never stops the other, and a failed export never fails the
cycle: the lake degrades, it does not block.
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
from pathlib import Path
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from social_scraper.browser import PlaywrightBrowser
from social_scraper.config import Settings
from social_scraper.db.throttle_store import REQUEST_LOG_RETENTION_DAYS, DbThrottleStore
from social_scraper.export import export_all
from social_scraper.fetch import Browser
from social_scraper.models import REDDIT, X
from social_scraper.pipeline import Ingestor
from social_scraper.reddit.scraper import RedditOptions, RedditScraper
from social_scraper.symbols.universe import Universe, load_universe
from social_scraper.throttle import Policy, Throttle, ThrottleStop
from social_scraper.x import accounts
from social_scraper.x.scraper import XPlan, XScraper

log = logging.getLogger(__name__)

REPORT_FILE = "scrape-run.json"
# Timing jitter only; SystemRandom because nothing here needs a reproducible sequence.
_JITTER = secrets.SystemRandom()
BrowserFactory = Callable[[str], AbstractContextManager[Browser]]


@dataclass
class RunReport:
    started_at: str
    finished_at: str | None = None
    platforms: dict[str, dict[str, Any]] = field(default_factory=dict)
    export: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def policy_for(settings: Settings, platform: str) -> Policy:
    """The throttle policy for `platform`. Only X keeps human active hours."""
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
        active_hours=None if reddit else s.x_hours,
        timezone=s.scraper_timezone,
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
            settings.profile_dir(platform), settings.browser_channel, settings.browser_headless
        )

    return factory


def universe_for(settings: Settings) -> Universe:
    return load_universe(
        settings.data_dir / "symbols" / "nasdaqtraded.txt",
        settings.symbol_universe_url,
        settings.symbol_universe_max_age_hours,
        settings.extra_symbol_files,
    )


def _crawl(settings: Settings, platform: str, engine: Engine, ingestor: Ingestor, browser: Browser):
    throttle = Throttle(policy_for(settings, platform), DbThrottleStore(engine))
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
    settings: Settings, platform: str, engine: Engine, ingestor: Ingestor, factory: BrowserFactory
) -> dict[str, Any]:
    """Crawl one platform; everything that goes wrong lands in the returned dict."""
    try:
        # Check before launching Chrome: a paused platform should cost nothing.
        Throttle(policy_for(settings, platform), DbThrottleStore(engine)).check()
        with factory(platform) as browser:
            stats = _crawl(settings, platform, engine, ingestor, browser)
    except ThrottleStop as exc:
        return {"skipped": str(exc)}
    except (PlaywrightError, SQLAlchemyError, OSError) as exc:
        log.exception("%s crawl failed", platform)
        return {"error": f"{type(exc).__name__}: {exc}"}
    out = asdict(stats)
    if stats.errors:
        out["error"] = "; ".join(stats.errors[:5])
    return out


def run_cycle(
    settings: Settings,
    engine: Engine,
    platforms: tuple[str, ...] = (REDDIT, X),
    factory: BrowserFactory | None = None,
    export: bool = True,
) -> RunReport:
    report = RunReport(started_at=datetime.now(UTC).isoformat())
    ingestor = Ingestor(engine, universe_for(settings))
    for platform in platforms:
        result = run_platform(
            settings, platform, engine, ingestor, factory or default_browser_factory(settings)
        )
        report.platforms[platform] = result
        if "error" in result:
            report.errors.append(f"{platform}: {result['error']}")
    DbThrottleStore(engine).prune(datetime.now(UTC) - timedelta(days=REQUEST_LOG_RETENTION_DAYS))
    if export:
        exported = export_all(engine, settings.archive_root, datetime.now(UTC))
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
