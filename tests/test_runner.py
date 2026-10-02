import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import FakeBrowser, IsolatedSettings, reddit_listing, reddit_submission
from playwright.sync_api import Error as PlaywrightError

from social_scraper import runner
from social_scraper.browser import PlaywrightBrowser
from social_scraper.db.throttle_store import DbThrottleStore
from social_scraper.fetch import PageResult
from social_scraper.models import REDDIT, X
from social_scraper.runner import (
    RunReport,
    default_browser_factory,
    policy_for,
    run_cycle,
    run_forever,
    run_platform,
    universe_for,
    write_report,
    x_plan,
)
from social_scraper.symbols.universe import Universe
from social_scraper.throttle import Block

DIRECTORY = (
    "Nasdaq Traded|Symbol|Security Name|Listing Exchange|Market Category|ETF|Round Lot Size|Test Issue\n"
    "Y|NVDA|NVIDIA Corporation Common Stock|Q|Q|N|100|N\n"
)


@pytest.fixture
def settings(tmp_path):
    cache = tmp_path / "data" / "symbols" / "nasdaqtraded.txt"
    cache.parent.mkdir(parents=True)
    cache.write_text(DIRECTORY)
    return IsolatedSettings(
        data_dir=tmp_path / "data",
        archive_root=tmp_path / "lake",
        logs_dir=tmp_path / "logs",
        reddit_subreddits="wallstreetbets",
        reddit_min_delay_s=0,
        reddit_max_delay_s=0,
        reddit_active_hours="",
        x_active_hours="",
        x_pages_per_cycle=1,
        x_min_delay_s=0,
        x_max_delay_s=0,
    )


def reddit_browser():
    now = datetime.now(UTC).timestamp()
    listing = PageResult(
        200, "u", data=reddit_listing([reddit_submission("a", now, title="$NVDA")])
    )
    base = "https://www.reddit.com/r/wallstreetbets/"
    return FakeBrowser(
        {
            base: PageResult(200, base),
            base + "new.json": listing,
            base + "comments.json": listing,
            base + "top.json": listing,
        }
    )


def test_policy_for_each_platform(settings):
    reddit = policy_for(settings, REDDIT)
    x = policy_for(settings, X)
    assert reddit.platform == REDDIT and reddit.active_hours is None
    assert x.platform == X and x.daily_budget == settings.x_daily_budget
    assert x.timezone == settings.scraper_timezone
    assert x.block_cooldown_max_hours == settings.block_cooldown_max_hours


def test_policy_for_reddit_keeps_its_own_active_hours(settings):
    rested = settings.model_copy(update={"reddit_active_hours": "6-24"})
    assert policy_for(rested, REDDIT).active_hours == (6, 24)
    assert policy_for(rested, X).active_hours is None


def test_x_plan_mirrors_settings(settings):
    plan = x_plan(settings)
    assert plan.pages == 1 and plan.queries == settings.x_queries


def test_default_browser_factory_builds_a_profile_per_platform(settings):
    browser = default_browser_factory(settings)(X)
    assert isinstance(browser, PlaywrightBrowser)
    assert browser.profile_dir == settings.profile_dir(X)
    assert browser.minimized is settings.browser_minimized


def test_universe_for_reads_the_cache(settings):
    settings.symbol_universe_max_age_hours = 1e9
    u = universe_for(settings)
    assert isinstance(u, Universe) and "NVDA" in u


def test_run_cycle_scrapes_exports_and_reports(settings, engine):
    settings.symbol_universe_max_age_hours = 1e9
    report = run_cycle(settings, engine, (REDDIT,), factory=lambda p: reddit_browser())
    assert report.ok, report.errors
    assert report.platforms[REDDIT]["ingest"]["stored"] >= 1
    assert any(d["rows"] for d in report.export)
    assert (settings.archive_root / "_catalog" / "social_scraper_posts.json").exists()


def test_run_platform_skips_a_paused_platform_without_a_browser(settings, engine):
    DbThrottleStore(engine).set_block(
        X, Block(datetime.now(UTC) + timedelta(hours=1), "captcha", 1), datetime.now(UTC)
    )

    def never(platform):
        raise AssertionError("browser must not launch")

    ingestor = runner.Ingestor(engine, Universe({}))
    assert "paused" in run_platform(settings, X, engine, ingestor, never)["skipped"]


def test_run_platform_reports_browser_crash(settings, engine):
    def crash(platform):
        raise PlaywrightError("Chrome not found")

    ingestor = runner.Ingestor(engine, Universe({}))
    result = run_platform(settings, REDDIT, engine, ingestor, crash)
    assert "Chrome not found" in result["error"]


def test_run_platform_surfaces_scraper_errors(settings, engine):
    browser = FakeBrowser()
    browser.default = PageResult(200, "https://x.com/search")  # no data: an error
    ingestor = runner.Ingestor(engine, Universe({}))
    result = run_platform(settings, X, engine, ingestor, lambda p: browser)
    assert "no timeline data" in result["error"]


def test_failed_export_is_an_error_but_not_a_crash(settings, engine, monkeypatch):
    settings.symbol_universe_max_age_hours = 1e9
    (settings.archive_root).parent.mkdir(parents=True, exist_ok=True)
    settings.archive_root.write_text("a file where the lake directory should be")
    report = run_cycle(settings, engine, (REDDIT,), factory=lambda p: reddit_browser())
    assert not report.ok
    assert any(e.startswith("export") for e in report.errors)


def test_write_report_overwrites_and_is_parseable(settings):
    path = write_report(RunReport(started_at="t0", errors=["boom"]), settings.logs_dir)
    write_report(RunReport(started_at="t1"), settings.logs_dir)
    body = json.loads(path.read_text())
    assert body["started_at"] == "t1" and body["ok"] is True
    assert path.name == runner.REPORT_FILE


def test_run_forever_runs_n_cycles_and_sleeps_between(settings, engine, monkeypatch):
    calls = []
    monkeypatch.setattr(runner, "run_cycle", lambda *a, **kw: calls.append(kw) or RunReport("t"))
    slept = []
    run_forever(settings, engine, (REDDIT,), cycles=2, export=False, sleep=slept.append)
    assert len(calls) == 2 and calls[0]["export"] is False
    assert len(slept) == 1
    assert 0.8 * 60 * settings.cycle_minutes <= slept[0] <= 1.2 * 60 * settings.cycle_minutes
    assert (settings.logs_dir / runner.REPORT_FILE).exists()
