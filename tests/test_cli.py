import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from conftest import LAKE_BUCKET, Clock, IsolatedSettings, bucket_keys, reddit_submission
from sqlalchemy.exc import OperationalError

from social_scraper import cli, runner
from social_scraper.archive import store
from social_scraper.cli import (
    build_parser,
    cmd_export,
    cmd_lake,
    cmd_reddit_import,
    cmd_run,
    cmd_scrape,
    cmd_status,
    cmd_universe,
    main,
    platforms_of,
    utc_date,
)
from social_scraper.reddit import dumps
from social_scraper.db.repository import make_engine
from social_scraper.db.throttle_store import DbThrottleStore
from social_scraper.models import PLATFORMS, REDDIT, X, AuthorInfo, Post
from social_scraper.pipeline import Ingestor
from social_scraper.runner import RunReport, write_report
from social_scraper.throttle import Block


@pytest.fixture
def settings(tmp_path):
    return IsolatedSettings(
        data_dir=tmp_path / "data",
        archive_root=tmp_path / "lake",
        logs_dir=tmp_path / "logs",
    )


def sqlite_engine_at(path):
    return make_engine(f"sqlite:///{path.as_posix()}")


def seed(engine, universe):
    post = Post(
        platform="reddit",
        external_id="a",
        kind="submission",
        channel="stocks",
        created_at=datetime.now(UTC),
        author=AuthorInfo(name="u"),
        body="$NVDA calls",
    )
    Ingestor(engine, universe, Clock(datetime.now(UTC))).ingest([post])


def test_parser_accepts_every_command():
    p = build_parser()
    assert p.parse_args(["scrape"]).platform == "all"
    assert p.parse_args(["scrape", "x", "--no-export"]).no_export
    assert p.parse_args(["scrape", "--now"]).now
    assert not p.parse_args(["scrape"]).now
    assert p.parse_args(["run", "--headed"]).headed
    assert not p.parse_args(["scrape"]).headed
    assert p.parse_args(["run", "reddit", "--cycles", "2"]).cycles == 2
    assert p.parse_args(["login", "x"]).platform == "x"
    assert p.parse_args(["lake", "--days", "7"]).days == 7
    assert p.parse_args(["export", "--full"]).full
    assert not p.parse_args(["export"]).full
    with pytest.raises(SystemExit):
        p.parse_args(["login", "facebook"])
    imp = p.parse_args(["reddit-import", "a.zst", "dumps", "--until", "2026-01-01", "--dry-run"])
    assert imp.paths == [Path("a.zst"), Path("dumps")] and imp.dry_run
    assert imp.since == dumps.DEFAULT_SINCE and imp.until == datetime(2026, 1, 1, tzinfo=UTC)
    assert not (imp.all_posts or imp.local_only)
    with pytest.raises(SystemExit):
        p.parse_args(["reddit-import"])
    with pytest.raises(SystemExit):
        p.parse_args(["reddit-import", "a.zst", "--since", "last tuesday"])


def test_utc_date_reads_dates_and_aware_timestamps():
    assert utc_date("2021-01-27") == datetime(2021, 1, 27, tzinfo=UTC)
    assert utc_date("2021-01-27T05:00:00+05:00") == datetime(2021, 1, 27, tzinfo=UTC)


def dump_line(post_id, title):
    data = reddit_submission(post_id, datetime(2021, 1, 27, tzinfo=UTC).timestamp(), title=title)
    return json.dumps(data["data"]) + "\n"


def import_args(*paths, **over):
    base = {
        "since": dumps.DEFAULT_SINCE,
        "until": None,
        "all_posts": False,
        "local_only": False,
        "dry_run": False,
    }
    return argparse.Namespace(paths=list(paths), **(base | over))


def test_reddit_import_writes_the_lake_and_its_report(
    settings, engine, universe, monkeypatch, capsys, tmp_path
):
    monkeypatch.setattr(cli, "universe_for", lambda s: universe)
    folder = tmp_path / "downloads"
    folder.mkdir()
    (folder / "wsb_submissions.jsonl").write_text(dump_line("a", "$GME") + dump_line("b", "hi"))
    assert cmd_reddit_import(settings, engine, import_args(folder)) == 0
    out = capsys.readouterr().out
    assert "2021: 1 posts (0 comments), 1 symbols" in out
    assert "1 file(s): 1 posts, 1 mentions in 2 partitions" in out
    body = json.loads((settings.logs_dir / dumps.REPORT_FILE).read_text())
    assert body["ok"] and body["files"][0]["skipped"] == {"no_symbol": 1}
    assert (settings.archive_root / "_catalog" / "social_scraper_posts.json").exists()
    # The spool is temporary: nothing is left behind in data/.
    assert list(settings.data_dir.iterdir()) == []


def test_reddit_import_dry_run_and_local_only_never_touch_the_bucket(
    settings, engine, universe, monkeypatch, capsys, tmp_path
):
    monkeypatch.setattr(cli, "universe_for", lambda s: universe)
    settings.archive_backend = "s3"  # and no bucket named: building its store would raise
    path = tmp_path / "s.jsonl"
    path.write_text(dump_line("a", "$GME"))
    assert cmd_reddit_import(settings, engine, import_args(path, dry_run=True)) == 0
    assert "would be written" in capsys.readouterr().out
    assert not settings.archive_root.exists()
    assert cmd_reddit_import(settings, engine, import_args(path, local_only=True)) == 0
    assert (settings.archive_root / "_catalog" / "social_scraper_posts.json").exists()


def test_reddit_import_exits_1_on_a_bad_file(settings, engine, universe, monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "universe_for", lambda s: universe)
    args = import_args(tmp_path / "missing.zst")
    assert cmd_reddit_import(settings, engine, args) == 1
    body = json.loads((settings.logs_dir / dumps.REPORT_FILE).read_text())
    assert "missing.zst" in body["errors"][0]


def test_reddit_import_reports_then_raises_when_it_cannot_start(
    settings, engine, monkeypatch, capsys, tmp_path
):
    settings.archive_backend = "s3"  # no bucket named
    with pytest.raises(store.StoreError):
        cmd_reddit_import(settings, engine, import_args(tmp_path / "s.zst"))
    assert "FAILED -- details in" in capsys.readouterr().out
    body = json.loads((settings.logs_dir / dumps.REPORT_FILE).read_text())
    assert body["ok"] is False and body["errors"][0].startswith("import: StoreError")


def test_platforms_of():
    assert platforms_of("all") == PLATFORMS
    assert platforms_of("x") == ("x",)


def scrape_args(platform="x", now=True):
    return argparse.Namespace(platform=platform, no_export=True, now=now)


def report_body(settings):
    return json.loads((settings.logs_dir / runner.REPORT_FILE).read_text())


def test_scrape_writes_the_report_and_sets_exit_code(settings, engine, monkeypatch, capsys):
    monkeypatch.setattr(cli, "run_cycle", lambda *a, **kw: RunReport("t", errors=["x: boom"]))
    assert cmd_scrape(settings, engine, scrape_args()) == 1
    assert "FAILED" in capsys.readouterr().out
    monkeypatch.setattr(cli, "run_cycle", lambda *a, **kw: RunReport("t"))
    assert cmd_scrape(settings, engine, scrape_args()) == 0


def test_scrape_with_the_database_down_raises_and_overwrites_the_report(settings, tmp_path):
    # Raised, not returned: the process exits 1 with the traceback, as any uncaught error.
    write_report(RunReport("earlier"), settings.logs_dir)
    unreachable = sqlite_engine_at(tmp_path / "no-such-dir" / "db.sqlite")
    with pytest.raises(OperationalError):
        cmd_scrape(settings, unreachable, scrape_args())
    body = report_body(settings)
    assert body["ok"] is False and body["started_at"] != "earlier"
    assert body["errors"][0].startswith("cycle: OperationalError")


def test_scrape_reports_a_crash_outside_the_platform_guard(settings, engine, monkeypatch, capsys):
    def crash(*a, **kw):
        raise KeyError("payload shape changed")

    monkeypatch.setattr(cli, "run_cycle", crash)
    with pytest.raises(KeyError):
        cmd_scrape(settings, engine, scrape_args())
    assert "KeyError" in report_body(settings)["errors"][0]
    assert "FAILED -- details in" in capsys.readouterr().out


def test_scrape_exits_0_when_every_platform_is_deliberately_paused(settings, engine):
    """Outside X hours and Reddit's circuit open: nothing done, nothing failed."""
    cache = settings.data_dir / "symbols" / "nasdaqtraded.txt"
    cache.parent.mkdir(parents=True)
    cache.write_text("Nasdaq Traded|Symbol|Security Name|Test Issue\n")
    # Both windows are pinned to the wall clock, which this path reads directly. Reddit's
    # default rest (6-24) would otherwise skip it as "outside active hours" before its
    # circuit is consulted, red only overnight. X's window starts two hours out, so an
    # hour that turns over mid-test cannot open it.
    settings.reddit_active_hours = ""
    hour = datetime.now(ZoneInfo(settings.scraper_timezone)).hour
    start = (hour + 2) % 24
    settings.x_active_hours = f"{start}-{start + 1}"
    now = datetime.now(UTC)
    DbThrottleStore(engine).set_block(REDDIT, Block(now + timedelta(hours=1), "captcha", 1), now)
    assert cmd_scrape(settings, engine, scrape_args("all")) == 0
    platforms = report_body(settings)["platforms"]
    assert "outside active hours" in platforms[X]["skipped"]
    assert "paused" in platforms[REDDIT]["skipped"]


def test_scrape_waits_up_to_a_fifth_of_a_cycle_unless_now(settings, engine, monkeypatch, capsys):
    """The scheduler fires bare `scrape` on a fixed grid, so the wait is the default."""
    monkeypatch.setattr(cli, "run_cycle", lambda *a, **kw: RunReport("t"))
    slept = []
    monkeypatch.setattr(cli.time, "sleep", slept.append)
    cmd_scrape(settings, engine, build_parser().parse_args(["scrape", "x", "--no-export"]))
    assert len(slept) == 1 and 0 <= slept[0] <= 0.2 * 60 * settings.cycle_minutes
    assert "--now to skip" in capsys.readouterr().out
    cmd_scrape(settings, engine, build_parser().parse_args(["scrape", "x", "--no-export", "--now"]))
    assert len(slept) == 1


def test_run_passes_options_through(settings, engine, monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "run_forever", lambda *a, **kw: seen.update(kw))
    cmd_run(settings, engine, argparse.Namespace(platform="reddit", no_export=True, cycles=3))
    assert seen == {"cycles": 3, "export": False}


def test_scrape_exits_1_when_the_bucket_export_fails(
    settings, engine, universe, s3_client, monkeypatch, capsys
):
    """README: a failed export is a failed cycle -- the bucket's included."""
    seed(engine, universe)
    cache = settings.data_dir / "symbols" / "nasdaqtraded.txt"
    cache.parent.mkdir(parents=True)
    cache.write_text("Nasdaq Traded|Symbol|Security Name|Test Issue\n")
    settings.symbol_universe_max_age_hours = 1e9
    settings.archive_backend = "s3"
    settings.archive_s3_bucket = "no-such-bucket"
    monkeypatch.setattr(store, "make_s3_client", lambda s: s3_client)
    # No platforms, so no browser: the cycle is the export alone.
    monkeypatch.setattr(
        cli, "run_cycle", lambda s, e, p, export: runner.run_cycle(s, e, (), export=export)
    )
    args = argparse.Namespace(platform="all", no_export=False, now=True)
    assert cmd_scrape(settings, engine, args) == 1
    assert "FAILED" in capsys.readouterr().out
    assert any("StoreError" in e for e in report_body(settings)["errors"])
    # The local tree still took the rows: the lake degrades, it does not block.
    assert (settings.archive_root / "_catalog" / "social_scraper_posts.json").exists()


def test_export_writes_the_bucket_and_full_resends_every_row(
    settings, engine, universe, s3_client, monkeypatch, capsys
):
    seed(engine, universe)
    settings.archive_backend = "s3"
    settings.archive_s3_bucket = LAKE_BUCKET
    monkeypatch.setattr(store, "make_s3_client", lambda s: s3_client)
    assert cmd_export(settings, engine, argparse.Namespace(full=False)) == 0
    assert "_catalog/social_scraper_posts.json" in bucket_keys(s3_client)
    assert cmd_export(settings, engine, argparse.Namespace(full=False)) == 0
    assert "social_scraper_posts: 0 rows" in capsys.readouterr().out
    assert cmd_export(settings, engine, argparse.Namespace(full=True)) == 0
    out = capsys.readouterr().out
    assert "watermarks cleared" in out and "social_scraper_posts: 1 rows" in out


def test_export_exits_1_when_it_fails(settings, engine, universe):
    seed(engine, universe)
    settings.archive_backend = "s3"  # and no bucket named
    assert cmd_export(settings, engine, argparse.Namespace(full=False)) == 1


def test_export_status_and_lake(settings, engine, universe, capsys):
    seed(engine, universe)
    assert cmd_export(settings, engine, argparse.Namespace(full=False)) == 0
    assert cmd_status(settings, engine, argparse.Namespace()) == 0
    assert cmd_lake(settings, engine, argparse.Namespace(days=2)) == 0
    out = capsys.readouterr().out
    assert "social_scraper_posts: 1 rows" in out
    assert "trending (24h): NVDA 1" in out
    assert "NVDA" in out.splitlines()[-1]


def test_universe_forces_a_refresh(settings, engine, monkeypatch, capsys):
    seen = {}

    def fake(s):
        seen["age"] = s.symbol_universe_max_age_hours
        return [1, 2, 3]

    monkeypatch.setattr(cli, "universe_for", fake)
    assert cmd_universe(settings, engine, argparse.Namespace()) == 0
    assert seen["age"] == 0 and "3 symbols" in capsys.readouterr().out


def test_main_init_db_and_dispatch(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'db.sqlite').as_posix()}")
    monkeypatch.setenv("ARCHIVE_ROOT", str(tmp_path / "lake"))
    monkeypatch.chdir(tmp_path)
    assert main(["init-db"]) == 0
    assert main(["status"]) == 0
    assert "tables ready" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("argv", "headless", "minimized"),
    [(["scrape"], True, True), (["scrape", "--headed"], False, False)],
)
def test_main_scrape_is_headless_unless_headed(tmp_path, monkeypatch, argv, headless, minimized):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'db.sqlite').as_posix()}")
    monkeypatch.delenv("BROWSER_HEADLESS", raising=False)
    monkeypatch.delenv("BROWSER_MINIMIZED", raising=False)
    monkeypatch.chdir(tmp_path)
    seen = {}

    def fake_cycle(s, *a, **kw):
        seen.update(headless=s.browser_headless, minimized=s.browser_minimized)
        return RunReport("t")

    monkeypatch.setattr(cli, "run_cycle", fake_cycle)
    assert main(argv) == 0
    # --headed is for watching the window, so it is neither headless nor minimized.
    assert seen == {"headless": headless, "minimized": minimized}


def test_main_login_opens_the_profile(tmp_path, monkeypatch):
    opened = {}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "social_scraper.browser.open_for_login",
        lambda profile, channel, url: opened.update(profile=profile, url=url),
    )
    assert main(["login", "x"]) == 0
    assert opened["url"] == cli.LOGIN_URLS["x"]
    assert opened["profile"].name == "x"
