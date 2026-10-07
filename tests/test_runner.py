import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import (
    LAKE_BUCKET,
    FakeBrowser,
    IsolatedSettings,
    bucket_keys,
    reddit_listing,
    reddit_submission,
)
from playwright.sync_api import Error as PlaywrightError

from social_scraper import runner
from social_scraper.archive import store
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
    start_jitter_s,
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
    assert browser.headless is settings.browser_headless is True
    assert browser.minimized is False  # headless: no window to minimize
    headed = settings.model_copy(update={"browser_headless": False})
    assert default_browser_factory(headed)(X).minimized is settings.browser_minimized is True


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


def test_run_platform_skips_when_the_budget_is_spent(settings, engine):
    settings.x_hourly_budget = 0
    ingestor = runner.Ingestor(engine, Universe({}))
    result = run_platform(settings, X, engine, ingestor, lambda p: FakeBrowser())
    assert "budget" in result["skipped"] and "error" not in result


def test_budget_running_out_mid_crawl_is_a_skip_not_an_error(settings, engine):
    settings.symbol_universe_max_age_hours = 1e9
    settings.reddit_hourly_budget = 1  # the warm-up spends it; the first listing stops
    report = run_cycle(
        settings, engine, (REDDIT,), factory=lambda p: reddit_browser(), export=False
    )
    assert report.ok, report.errors
    assert "budget" in report.platforms[REDDIT]["skipped"]


def test_run_platform_reports_browser_crash(settings, engine):
    def crash(platform):
        raise PlaywrightError("Chrome not found")

    ingestor = runner.Ingestor(engine, Universe({}))
    result = run_platform(settings, REDDIT, engine, ingestor, crash)
    assert "Chrome not found" in result["error"]


def test_run_platform_surfaces_scraper_errors(settings, engine):
    browser = FakeBrowser()
    browser.default = PageResult(200, "https://x.com/account/access")  # a challenge
    ingestor = runner.Ingestor(engine, Universe({}))
    result = run_platform(settings, X, engine, ingestor, lambda p: browser)
    assert "challenged" in result["error"]


def test_policy_for_carries_the_cycle_deadline(settings):
    deadline = datetime.now(UTC)
    assert policy_for(settings, X).deadline is None
    assert policy_for(settings, REDDIT, deadline).deadline == deadline


def test_cycle_deadline_is_a_share_of_the_cycle():
    start = datetime(2026, 10, 7, 20, 30, tzinfo=UTC)
    assert runner.cycle_deadline(start, 30) == start + timedelta(minutes=22.5)


def test_a_cycle_starts_at_the_schedulers_fire_when_it_names_one():
    now = datetime(2026, 10, 7, 21, 35, 44, tzinfo=UTC)
    fired = datetime(2026, 10, 7, 21, 30, tzinfo=UTC)
    assert runner.cycle_start(now, "2026-10-07T21:30:00+00:00") == fired
    assert runner.cycle_start(now, "2026-10-07T17:30:00-04:00") == fired
    assert runner.cycle_start(now, "2026-10-07T21:30:00") == fired, "naive reads as UTC"


@pytest.mark.parametrize("fired", [None, "", "  ", "not a time", "2026-10-07T21:40:00+00:00"])
def test_a_cycle_with_no_usable_fire_starts_now(fired):
    """A run by hand names no fire; a garbled value or one in the future is not a fire."""
    now = datetime(2026, 10, 7, 21, 35, 44, tzinfo=UTC)
    assert runner.cycle_start(now, fired) == now


def test_a_cycle_past_its_deadline_launches_no_browser_and_still_exports(settings, engine):
    settings.symbol_universe_max_age_hours = 1e9

    def never(platform):
        raise AssertionError("browser must not launch")

    past = datetime.now(UTC) - timedelta(seconds=1)
    report = run_cycle(settings, engine, (REDDIT, X), factory=never, deadline=past)
    assert report.ok, report.errors
    assert all("out of cycle time" in report.platforms[p]["skipped"] for p in (REDDIT, X))
    assert report.export


def test_failed_export_is_an_error_but_not_a_crash(settings, engine, monkeypatch):
    settings.symbol_universe_max_age_hours = 1e9
    (settings.archive_root).parent.mkdir(parents=True, exist_ok=True)
    settings.archive_root.write_text("a file where the lake directory should be")
    report = run_cycle(settings, engine, (REDDIT,), factory=lambda p: reddit_browser())
    assert not report.ok
    assert any(e.startswith("export") for e in report.errors)


def test_run_cycle_exports_to_the_bucket_when_configured(settings, engine, s3_client, monkeypatch):
    settings.symbol_universe_max_age_hours = 1e9
    settings.archive_backend = "s3"
    settings.archive_s3_bucket = LAKE_BUCKET
    monkeypatch.setattr(store, "make_s3_client", lambda s: s3_client)
    report = run_cycle(settings, engine, (REDDIT,), factory=lambda p: reddit_browser())
    assert report.ok, report.errors
    assert "_catalog/social_scraper_posts.json" in bucket_keys(s3_client)
    assert (settings.archive_root / "_catalog" / "social_scraper_posts.json").exists()


# --- the sports domain ------------------------------------------------------------------
# A league with no Wikidata id: the cycle's sync asks the network nothing.
SPORTS_LEAGUES = '[[league]]\nkey = "nhl"\nname = "NHL"\nsport = "hockey"\nsubreddits = ["nhl"]\n'


def both_browser():
    """r/wallstreetbets for stocks and r/nhl for sports, in one fake."""
    browser = reddit_browser()
    now = datetime.now(UTC).timestamp()
    listing = PageResult(
        200, "u", data=reddit_listing([reddit_submission("h", now, subreddit="nhl")])
    )
    base = "https://www.reddit.com/r/nhl/"
    browser.pages |= {base: PageResult(200, base)}
    browser.pages |= {base + f"{n}.json": listing for n in ("new", "comments", "top")}
    return browser


@pytest.fixture
def sports_settings(settings, tmp_path):
    leagues = tmp_path / "sports-leagues.toml"
    leagues.write_text(SPORTS_LEAGUES, encoding="utf-8")
    settings.symbol_universe_max_age_hours = 1e9
    settings.sports_leagues_file = leagues
    settings.domains = "stocks,sports"
    return settings


def test_run_cycle_runs_both_domains_into_their_own_datasets(sports_settings, engine):
    report = run_cycle(sports_settings, engine, (REDDIT,), factory=lambda p: both_browser())
    assert report.ok, report.errors
    assert report.platforms[REDDIT]["ingest"]["stored"] >= 1
    assert report.platforms["reddit:sports"]["ingest"]["stored"] >= 1
    assert report.sports["leagues"] == 1 and report.sports["sync"]["synced"] == {"nhl": 0}
    assert report.sports["discovery"]["looked_up"] == 0
    catalog = sports_settings.archive_root / "_catalog"
    assert (catalog / "social_scraper_posts.json").exists()
    assert (catalog / "social_scraper_sports_posts.json").exists()


def test_run_cycle_sports_only_never_loads_the_symbol_universe(
    sports_settings, engine, monkeypatch
):
    sports_settings.domains = "sports"

    def no_universe(settings):
        raise AssertionError("stocks is off")

    monkeypatch.setattr(runner, "universe_for", no_universe)
    report = run_cycle(sports_settings, engine, (REDDIT,), factory=lambda p: both_browser())
    assert report.ok, report.errors
    assert list(report.platforms) == ["reddit:sports"]


def test_a_bad_league_list_fails_sports_but_not_stocks(sports_settings, engine):
    sports_settings.sports_leagues_file.write_text("[[league]]\nkey = 1\n", encoding="utf-8")
    report = run_cycle(
        sports_settings, engine, (REDDIT,), factory=lambda p: both_browser(), export=False
    )
    assert not report.ok
    assert any(e.startswith("sports:") for e in report.errors)
    assert report.platforms[REDDIT]["ingest"]["stored"] >= 1
    assert "reddit:sports" not in report.platforms


def test_a_wikidata_outage_is_a_notice_not_an_error(sports_settings, engine, monkeypatch):
    from social_scraper.sports import cycle as sports_cycle
    from social_scraper.sports.sync import SyncStats

    real = sports_cycle.sync_leagues

    def failing(*args, **kwargs):
        real(*args, **kwargs)
        return SyncStats(errors={"nhl": "HTTP 504"})

    monkeypatch.setattr(sports_cycle, "sync_leagues", failing)
    report = run_cycle(
        sports_settings, engine, (REDDIT,), factory=lambda p: both_browser(), export=False
    )
    assert report.ok, report.errors
    assert "wikidata" in report.sports["notice"]


def test_prepare_sports_records_its_summary_or_its_error(sports_settings, engine):
    report = RunReport("t")
    ctx = runner.prepare_sports(sports_settings, engine, report)
    assert ctx is not None and report.errors == []
    assert report.sports["leagues"] == 1 and report.sports["entities"] == 0
    sports_settings.sports_leagues_file.unlink()
    report = RunReport("t")
    assert runner.prepare_sports(sports_settings, engine, report) is None
    assert report.errors[0].startswith("sports: cannot read")


def test_unknown_domain_is_a_configuration_error(settings, engine):
    settings.domains = "stocks,crypto"
    with pytest.raises(ValueError, match="crypto"):
        run_cycle(settings, engine, (REDDIT,), factory=lambda p: reddit_browser())


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


def test_start_jitter_is_up_to_a_fifth_of_a_cycle():
    waits = [start_jitter_s(30) for _ in range(200)]
    assert all(0 <= w <= 0.2 * 30 * 60 for w in waits)
    assert len(set(waits)) > 1
    assert start_jitter_s(0) == 0
