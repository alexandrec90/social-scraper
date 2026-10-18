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
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from social_scraper import export as export_mod
from social_scraper import runner
from social_scraper.archive import store
from social_scraper.browser import TIMED_OUT, PlaywrightBrowser
from social_scraper.db.throttle_store import DbThrottleStore
from social_scraper.fetch import LaunchTimedOut, PageResult
from social_scraper.apiclient import ApiClient
from social_scraper.models import (
    API_PLATFORMS,
    BLUESKY,
    HFBOARDS,
    POLYMARKET,
    REDDIT,
    STOCKTWITS,
    TRADINGVIEW,
    X,
)
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


def test_a_machine_not_yet_logged_into_x_still_runs_a_green_cycle(settings, engine):
    # What a collector moved to another PC meets until `login x` is redone there: browser
    # profiles do not travel (`scripts/handoff.py`), and X answers search with its login
    # wall. The scheduled verdict must stay ok -- a notice naming the fix, not an error.
    settings.symbol_universe_max_age_hours = 1e9
    browser = FakeBrowser()
    browser.default = PageResult(200, "https://x.com/i/flow/login")
    report = run_cycle(settings, engine, (X,), factory=lambda p: browser, export=False)
    assert report.ok, report.errors
    assert "error" not in report.platforms[X]
    assert any("`uv run social-scraper login x`" in n for n in report.platforms[X]["notices"])


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


def test_run_platform_reports_a_timeout_apart_from_an_error(settings, engine):
    # A page load or a launch that times out never gets here (`PageResult.timed_out`,
    # `LaunchTimedOut`); any other Playwright wait that runs out does.
    def slow(platform):
        raise PlaywrightTimeout("Page.wait_for_selector: Timeout 30000ms exceeded.")

    ingestor = runner.Ingestor(engine, Universe({}))
    result = run_platform(settings, REDDIT, engine, ingestor, slow)
    assert "error" not in result
    assert result[TIMED_OUT].startswith("TimeoutError: Page.wait_for_selector")


def test_a_timed_out_crawl_is_a_timeout_of_the_cycle_not_an_error(settings, engine):
    def slow(platform):
        raise PlaywrightTimeout("Page.wait_for_selector: Timeout 30000ms exceeded.")

    report = run_cycle(settings, engine, (REDDIT,), factory=slow, export=False)
    assert report.ok and report.timeouts == [
        "reddit: TimeoutError: Page.wait_for_selector: Timeout 30000ms exceeded."
    ]


def test_record_results_files_errors_and_timeouts_apart():
    report = RunReport("t")
    runner.record_results(
        report, {"reddit": {"error": "boom"}, "x": {TIMED_OUT: "TimeoutError: t"}, "y": {}}
    )
    assert report.errors == ["reddit: boom"] and report.timeouts == ["x: TimeoutError: t"]
    assert set(report.platforms) == {"reddit", "x", "y"}


@pytest.mark.parametrize(
    ("timeouts", "previous", "streak", "errors"),
    [
        ([], 2, 0, []),
        (["x: t"], 0, 1, []),
        (["x: t"], 1, 2, []),
        (["x: t"], 2, 3, ["x: t (timed out 3 cycles running)"]),
    ],
)
def test_judge_timeouts_fails_only_at_the_streak(timeouts, previous, streak, errors):
    report = RunReport("t", timeouts=timeouts)
    runner.judge_timeouts(report, previous)
    assert report.timeout_streak == streak and report.errors == errors


def test_a_browser_that_will_not_start_is_a_skip_until_it_keeps_happening(settings, engine):
    """2026-10-08 03:56Z: Chrome's 180 s launch timeout on a loaded machine failed the scrape."""

    def stalled(platform):
        raise LaunchTimedOut(f"{platform}: Timeout 180000ms exceeded.")

    ingestor = runner.Ingestor(engine, Universe({}))
    results = [run_platform(settings, REDDIT, engine, ingestor, stalled) for _ in range(3)]
    assert "180000ms" in results[0]["skipped"] and "skipped" in results[1]
    assert "3 launches in a row" in results[2]["error"]


def test_a_browser_that_starts_ends_the_launch_streak(settings, engine):
    def stalled(platform):
        raise LaunchTimedOut("Timeout 180000ms exceeded.")

    ingestor = runner.Ingestor(engine, Universe({}))
    for _ in range(2):
        run_platform(settings, REDDIT, engine, ingestor, stalled)
    run_platform(settings, REDDIT, engine, ingestor, lambda p: reddit_browser())
    assert "skipped" in run_platform(settings, REDDIT, engine, ingestor, stalled)


def test_run_platform_surfaces_scraper_errors(settings, engine):
    browser = FakeBrowser()
    browser.default = PageResult(200, "https://x.com/account/access")  # a challenge
    ingestor = runner.Ingestor(engine, Universe({}))
    result = run_platform(settings, X, engine, ingestor, lambda p: browser)
    assert "challenged" in result["error"]


@pytest.mark.parametrize("platform", [STOCKTWITS, TRADINGVIEW, HFBOARDS, BLUESKY, POLYMARKET])
def test_policy_for_reads_each_platforms_own_settings(settings, platform):
    tuned = settings.model_copy(
        update={
            f"{platform}_min_delay_s": 1.5,
            f"{platform}_max_delay_s": 2.5,
            f"{platform}_hourly_budget": 7,
            f"{platform}_daily_budget": 70,
            f"{platform}_active_hours": "9-17",
        }
    )
    policy = policy_for(tuned, platform)
    assert policy.platform == platform
    assert (policy.min_delay_s, policy.max_delay_s) == (1.5, 2.5)
    assert (policy.hourly_budget, policy.daily_budget) == (7, 70)
    assert policy.active_hours == (9, 17)


def test_default_browser_factory_reads_apis_without_a_browser(settings):
    for platform in API_PLATFORMS:
        client = default_browser_factory(settings)(platform)
        assert isinstance(client, ApiClient) and client.hosts == runner.API_HOSTS[platform]
    assert isinstance(default_browser_factory(settings)(STOCKTWITS), PlaywrightBrowser)


def parts_for(settings, engine, factory, ingestor=None, sports=None) -> runner.CycleParts:
    return runner.CycleParts(settings, engine, factory, policy_for, ingestor, sports)


def test_crawl_platform_runs_only_the_domains_a_platform_serves(settings, engine, monkeypatch):
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(runner, "run_platform", lambda s, p, *a: seen.append(("stocks", p)) or {})
    monkeypatch.setattr(
        runner.sports_cycle, "run_platform", lambda s, p, *a: seen.append(("sports", p)) or {}
    )
    parts = parts_for(settings, engine, None, ingestor=object(), sports=object())
    assert set(runner.crawl_platform(parts, STOCKTWITS)) == {STOCKTWITS}
    assert set(runner.crawl_platform(parts, HFBOARDS)) == {f"{HFBOARDS}:sports"}
    assert set(runner.crawl_platform(parts, BLUESKY)) == {BLUESKY, f"{BLUESKY}:sports"}
    no_sports = parts_for(settings, engine, None, ingestor=object())
    assert runner.crawl_platform(no_sports, HFBOARDS) == {}
    assert seen == [
        ("stocks", STOCKTWITS),
        ("sports", HFBOARDS),
        ("stocks", BLUESKY),
        ("sports", BLUESKY),
    ]


def test_crawl_platform_hands_polymarket_both_domains(settings, engine, monkeypatch):
    got = {}

    def fake(s, eng, factory, policies, leagues, universe):
        got.update(leagues=leagues, universe=universe)
        return {"markets": 0}

    monkeypatch.setattr(runner.polymarket_cycle, "run_platform", fake)
    universe = Universe({})
    parts = parts_for(settings, engine, None, ingestor=runner.Ingestor(engine, universe))
    assert runner.crawl_platform(parts, POLYMARKET) == {POLYMARKET: {"markets": 0}}
    assert got == {"leagues": None, "universe": universe}


def test_crawl_all_runs_platforms_side_by_side_and_keeps_their_order(settings, engine, monkeypatch):
    import threading

    barrier = threading.Barrier(3, timeout=10)
    threads: dict[str, str] = {}

    def crawl(parts, platform):
        threads[platform] = threading.current_thread().name
        barrier.wait()  # only returns once three crawls are running at the same time
        return {platform: {"ok": True}}

    monkeypatch.setattr(runner, "crawl_platform", crawl)
    parts = parts_for(settings, engine, None)
    out = runner.crawl_all(parts, (REDDIT, X, STOCKTWITS), workers=3)
    assert [list(r) for r in out] == [[REDDIT], [X], [STOCKTWITS]]
    assert all(name.startswith("crawl") for name in threads.values())


def test_crawl_all_with_one_worker_stays_in_this_thread(settings, engine, monkeypatch):
    import threading

    names = []
    monkeypatch.setattr(
        runner,
        "crawl_platform",
        lambda parts, p: names.append(threading.current_thread().name) or {p: {}},
    )
    runner.crawl_all(parts_for(settings, engine, None), (REDDIT, X), workers=1)
    assert names == [threading.current_thread().name] * 2


def test_crawl_all_lets_an_unexpected_error_fail_the_cycle(settings, engine, monkeypatch):
    def crawl(parts, platform):
        if platform == X:
            raise RuntimeError("bug")
        return {platform: {}}

    monkeypatch.setattr(runner, "crawl_platform", crawl)
    with pytest.raises(RuntimeError, match="bug"):
        runner.crawl_all(parts_for(settings, engine, None), (REDDIT, X), workers=2)


def test_run_cycle_crawls_new_platforms_in_parallel(settings, file_engine):
    engine = file_engine
    settings.symbol_universe_max_age_hours = 1e9
    settings = settings.model_copy(
        update={
            "crawl_workers": 3,
            "watchlist": "NVDA",
            "stocktwits_min_delay_s": 0,
            "stocktwits_max_delay_s": 0,
            "stocktwits_active_hours": "",
            "polymarket_stock_tags": "",
        }
    )
    stream = {
        "response": {"status": 200},
        "cursor": {"more": False},
        "messages": [
            {
                "id": 5,
                "body": "$NVDA breaking out",
                "created_at": "2026-10-10T12:00:00Z",
                "user": {"id": 1, "followers": 10, "official": False},
                "symbols": [{"symbol": "NVDA"}],
                "entities": {"sentiment": {"basic": "Bullish"}},
            }
        ],
    }
    trending = {"response": {"status": 200}, "symbols": [{"symbol": "NVDA"}]}
    st = FakeBrowser(
        {
            "https://api.stocktwits.com/api/2/streams/": PageResult(200, "u", data=stream),
            "https://api.stocktwits.com/api/2/trending/": PageResult(200, "u", data=trending),
        }
    )
    browsers = {REDDIT: reddit_browser(), STOCKTWITS: st}
    report = run_cycle(settings, engine, (REDDIT, STOCKTWITS, POLYMARKET), factory=browsers.get)
    assert report.ok, report.errors
    assert report.platforms[STOCKTWITS]["ingest"]["stored"] == 1
    assert report.platforms[REDDIT]["ingest"]["stored"] >= 1
    assert "no Polymarket tags" in report.platforms[POLYMARKET]["skipped"]


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


def test_export_deadline_is_a_later_share_of_the_same_cycle():
    start = datetime(2026, 10, 7, 20, 30, tzinfo=UTC)
    crawl = runner.cycle_deadline(start, 30)
    assert runner.export_deadline(crawl, 30) == start + timedelta(minutes=27)


def test_hard_deadline_falls_after_the_export_and_before_the_next_fire():
    start = datetime(2026, 10, 9, 19, 0, tzinfo=UTC)
    hard = runner.hard_deadline(start, 30)
    assert hard == start + timedelta(minutes=28.5)
    assert runner.export_deadline(runner.cycle_deadline(start, 30), 30) < hard
    assert hard < start + timedelta(minutes=30)


def test_a_stuck_report_is_a_timeout_and_carries_the_lake_outage(tmp_path):
    since = "2026-10-08T22:31:00+00:00"
    runner.write_report(runner.RunReport("earlier", lake_down_since=since), tmp_path)
    now = datetime(2026, 10, 9, 19, 28, 30, tzinfo=UTC)
    report = runner.stuck_report("2026-10-09T19:00:06+00:00", now, tmp_path)
    assert report.timeouts == [runner.STUCK] and report.timeout_streak == 1
    assert report.ok and report.finished_at == now.isoformat()
    assert report.lake_down_since == since, "no export ran, so the outage carries on"
    assert "19:" not in runner.STUCK, "a stable cause: no time in it"


def test_a_cycle_past_its_export_deadline_defers_the_export_and_is_ok(settings, engine):
    """A deferred export is the next run's work, not an error: nothing is wrong with the
    lake, and an error here would fail the scheduled job for another writer's backfill."""
    settings.symbol_universe_max_age_hours = 1e9
    run_cycle(settings, engine, (REDDIT,), factory=lambda p: reddit_browser(), export=False)
    # A crawl deadline this far back puts the export's past too.
    past = datetime.now(UTC) - timedelta(minutes=settings.cycle_minutes)
    report = run_cycle(settings, engine, (), deadline=past)
    assert report.ok, report.errors
    deferred = [d["dataset"] for d in report.export if d["deferred"]]
    assert "social_scraper_posts" in deferred
    assert not (settings.archive_root / "_catalog" / "social_scraper_posts.json").exists()


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


def test_a_cycle_whose_lake_is_down_is_ok_and_says_so(settings, engine, monkeypatch):
    """5354e2f6: the pool's gateway answered every put with a 500 while one PC was off."""
    settings.symbol_universe_max_age_hours = 1e9

    def down(*a, **kw):
        raise store.LakeUnavailable("s3://data-lake: put k failed: InternalError")

    monkeypatch.setattr(export_mod, "write_partition", down)
    report = run_cycle(settings, engine, (REDDIT,), factory=lambda p: reddit_browser())
    assert report.ok, report.errors
    assert report.lake_down and "InternalError" in report.lake_down
    assert all(d["deferred"] == report.lake_down for d in report.export)


def test_judge_lake_fails_an_outage_at_its_limit_and_not_before():
    now = datetime(2026, 10, 9, 22, 31, tzinfo=UTC)

    def judged(previous):
        report = runner.RunReport(
            "t", export=[{"dataset": "a"}], lake_down="lake not accepting writes"
        )
        runner.judge_lake(report, previous, now)
        return report

    first = judged(None)
    assert first.ok and first.lake_down_since == now.isoformat()
    almost = judged(now - runner.LAKE_DOWN_FAILS_AFTER + timedelta(seconds=1))
    assert almost.ok
    at_limit = judged(now - runner.LAKE_DOWN_FAILS_AFTER)
    assert at_limit.errors == [
        "export: lake not accepting writes (for 24 h, since 2026-10-08 22:31Z)"
    ]


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
