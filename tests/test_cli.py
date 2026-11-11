import argparse
import json
import os
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from conftest import LAKE_BUCKET, Clock, IsolatedSettings, bucket_keys, reddit_submission
from sqlalchemy.exc import OperationalError

from social_scraper import cli, runner
from social_scraper.archive import store
from social_scraper.cli import (
    build_parser,
    cmd_export,
    archive_filter,
    cmd_bluesky_import,
    cmd_hn_import,
    cmd_lake,
    cmd_reddit_import,
    cmd_run,
    cmd_scrape,
    cmd_stocktwits_import,
    cmd_x_import,
    cmd_sports_sync,
    cmd_status,
    cmd_universe,
    main,
    platforms_of,
    run_backfill,
    utc_date,
)
from social_scraper.backfill import ArchiveFilter, ImportReport
from social_scraper.bluesky import dumps as bluesky_dumps
from social_scraper.hackernews import dumps as hn_dumps
from social_scraper.reddit import dumps
from social_scraper.stocktwits import dumps as stocktwits_dumps
from social_scraper.x import tweet_dumps
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
    x = p.parse_args(["x-import", "cashtag", "--local-only"])
    assert x.paths == [Path("cashtag")] and x.local_only and x.since is None
    for name in ("stocktwits-import", "bluesky-import", "hn-import"):
        archive = p.parse_args([name, "in", "--all-posts", "--since", "2020-01-01"])
        assert archive.paths == [Path("in")] and archive.all_posts
        assert archive.since == datetime(2020, 1, 1, tzinfo=UTC)
        assert p.parse_args([name, "in"]).since is None
    assert archive_filter(archive) == ArchiveFilter(
        since=datetime(2020, 1, 1, tzinfo=UTC), until=None, all_posts=True
    )


def test_run_backfill_hands_the_run_its_stores_and_maps_the_report_to_an_exit_code(
    settings, universe, monkeypatch, capsys
):
    monkeypatch.setattr(cli, "universe_for", lambda s: universe)
    seen = {}

    def run(got_universe, stores, spool, started):
        seen.update(universe=got_universe, stores=stores, spool_exists=spool.is_dir())
        return ImportReport(started_at=started.isoformat(), errors=["one file failed"])

    args = import_args(dry_run=True)
    assert run_backfill(settings, args, run, "demo-import.json") == 1
    assert seen == {"universe": universe, "stores": None, "spool_exists": True}
    assert "FAILED -- details in" in capsys.readouterr().out
    assert json.loads((settings.logs_dir / "demo-import.json").read_text())["ok"] is False


def test_run_backfill_sweeps_a_killed_runs_spool_but_not_a_running_ones(
    settings, universe, monkeypatch
):
    # 2026-10-08: a dry run reaped for low memory left its 29.7 GiB spool in data/ while
    # a long Reddit import, whose spool sat beside it, kept running.
    monkeypatch.setattr(cli, "universe_for", lambda s: universe)
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    killed = settings.data_dir / f"import-{gone.pid}-abcdefgh"
    running = settings.data_dir / f"import-{os.getpid()}-ijklmnop"
    for spool in (killed, running):
        (spool / "tmp").mkdir(parents=True)
        (spool / "spool.duckdb").write_bytes(b"rows")

    def run(got_universe, stores, spool, started):
        return ImportReport(started_at=started.isoformat())

    assert run_backfill(settings, import_args(dry_run=True), run, "demo-import.json") == 0
    assert sorted(p.name for p in settings.data_dir.iterdir()) == [running.name]


def test_x_import_writes_the_lake_and_its_report(
    settings, engine, universe, monkeypatch, capsys, tmp_path
):
    monkeypatch.setattr(cli, "universe_for", lambda s: universe)
    folder = tmp_path / "cashtag"
    folder.mkdir()
    (folder / "tweets.csv").write_text(
        '"id","text","user_id","in_reply_to_status_id","retweeted_status_id","lang","created_at"\n'
        '"5","$NVDA up","9","0","0","en","Thu Jun 01 14:00:00 +0000 2017"\n',
        encoding="utf-8",
    )
    assert cmd_x_import(settings, engine, import_args(folder, since=None)) == 0
    out = capsys.readouterr().out
    assert "2017: 1 posts (0 comments), 1 symbols" in out
    body = json.loads((settings.logs_dir / tweet_dumps.REPORT_FILE).read_text())
    assert body["ok"] and body["posts"] == 1


@pytest.mark.parametrize(
    ("command", "module", "function"),
    [
        (cmd_stocktwits_import, stocktwits_dumps, "import_folder"),
        (cmd_bluesky_import, bluesky_dumps, "import_files"),
        (cmd_hn_import, hn_dumps, "import_files"),
    ],
)
def test_archive_imports_hand_their_paths_and_filter_to_the_importer(
    settings, engine, universe, monkeypatch, capsys, command, module, function
):
    monkeypatch.setattr(cli, "universe_for", lambda s: universe)
    seen = {}

    def fake(paths, got_universe, flt, stores, spool, started):
        seen.update(paths=paths, flt=flt, stores=stores)
        return ImportReport(started_at=started.isoformat(), posts=3)

    monkeypatch.setattr(module, function, fake)
    since = datetime(2020, 1, 1, tzinfo=UTC)
    args = import_args(Path("in"), since=since, all_posts=True, dry_run=True)
    assert command(settings, engine, args) == 0
    assert seen == {
        "paths": [Path("in")],
        "flt": ArchiveFilter(since=since, until=None, all_posts=True),
        "stores": None,
    }
    assert json.loads((settings.logs_dir / module.REPORT_FILE).read_text())["posts"] == 3
    assert "ok -- details in" in capsys.readouterr().out


def test_hn_import_writes_the_lake_and_its_report(settings, engine, universe, monkeypatch):
    monkeypatch.setattr(cli, "universe_for", lambda s: universe)
    path = settings.data_dir.parent / "hn" / "2021-01.parquet"
    path.parent.mkdir(parents=True)
    item = {
        "id": 5,
        "deleted": 0,
        "type": 2,
        "by": "u",
        "time": datetime(2021, 1, 1, tzinfo=UTC),
        "text": "$NVDA up",
        "dead": 0,
        "parent": 4,
        "url": "",
        "score": 0,
        "title": "",
        "descendants": 0,
    }
    pq.write_table(pa.Table.from_pylist([item]), path)
    assert cmd_hn_import(settings, engine, import_args(path.parent, since=None)) == 0
    body = json.loads((settings.logs_dir / hn_dumps.REPORT_FILE).read_text())
    assert body["ok"] and body["posts"] == 1


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
    # `all` is SCRAPE_PLATFORMS; a platform named outright runs even when it is not in it.
    assert platforms_of("all", ("reddit", "polymarket")) == ("reddit", "polymarket")
    assert platforms_of("hfboards", ("reddit",)) == ("hfboards",)


def test_every_platform_can_be_scraped_but_only_reddit_and_x_logged_into():
    parser = build_parser()
    for platform in PLATFORMS:
        assert parser.parse_args(["scrape", platform]).platform == platform
    assert parser.parse_args(["login", "x"]).platform == "x"
    with pytest.raises(SystemExit):
        parser.parse_args(["login", "stocktwits"])


def test_scrape_all_reads_scrape_platforms(settings, engine, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        cli,
        "run_cycle",
        lambda s, e, platforms, **kw: seen.setdefault("p", platforms) and RunReport("t"),
    )
    narrowed = settings.model_copy(update={"scrape_platforms": "bluesky,reddit"})
    args = build_parser().parse_args(["scrape", "--no-export", "--now"])
    cli.scrape_cycle(narrowed, engine, args, datetime.now(UTC))
    assert seen["p"] == ("reddit", "bluesky")


def scrape_args(platform="x", now=True):
    return argparse.Namespace(platform=platform, no_export=True, now=now)


def report_body(settings):
    return json.loads((settings.logs_dir / runner.REPORT_FILE).read_text())


def test_a_timeout_fails_the_scrape_only_three_cycles_running(
    settings, engine, monkeypatch, capsys
):
    """2e04c832, 2795e03e: a page load or a Chrome launch timing out while a backfill had
    the machine loaded failed the scheduled job each time, and the next quiet cycle was
    clean. A timeout is retried by the next cycle; three in a row is a failure."""
    timed_out = "reddit: TimeoutError: Page.goto: Timeout 45000ms exceeded."
    monkeypatch.setattr(cli, "run_cycle", lambda *a, **kw: RunReport("t", timeouts=[timed_out]))
    for streak in (1, 2):
        assert cmd_scrape(settings, engine, scrape_args()) == 0
        out = capsys.readouterr().out
        assert f"ok -- reddit timed out, retried next run ({streak} of 3 before it fails)" in out
        assert report_body(settings)["timeout_streak"] == streak
    assert cmd_scrape(settings, engine, scrape_args()) == 1
    assert f"FAILED -- {timed_out} (timed out 3 cycles running)" in capsys.readouterr().out


def stuck_cycle(monkeypatch):
    """`run_cycle` blocked as on a frozen database, and a hard deadline a second away; the
    codes the watchdog ends the process with, which releases the block."""
    release, ended = threading.Event(), []

    def blocked_on_a_frozen_db(*a, **kw):
        release.wait(30)
        return RunReport("t")

    def end(code):
        ended.append(code)
        release.set()

    monkeypatch.setattr(cli, "run_cycle", blocked_on_a_frozen_db)
    monkeypatch.setattr(
        cli, "hard_deadline", lambda started, minutes: datetime.now(UTC) + timedelta(seconds=1)
    )
    monkeypatch.setattr(cli, "end_process", end)
    return ended


def test_a_cycle_stuck_past_its_hard_deadline_ends_as_a_timeout(
    settings, engine, monkeypatch, capsys
):
    """1590bcaa: 2026-10-09 Docker's VM froze from 19:05 to 19:32 UTC, the 19:00 cycle's db
    call blocked until it thawed, and the 19:30 fire was skipped behind it."""
    ended = stuck_cycle(monkeypatch)
    assert cmd_scrape(settings, engine, scrape_args()) == 0
    assert ended == [0]
    out = capsys.readouterr().out
    assert "blocked_on_a_frozen_db" in out, "the stack names where it was stuck"
    last = out.rstrip().splitlines()[-1]
    assert last.startswith("ok -- cycle timed out, retried next run (1 of 3 before it fails)")
    body = report_body(settings)
    assert body["timeouts"] == [runner.STUCK] and body["timeout_streak"] == 1


def test_a_third_stuck_cycle_running_fails_the_scrape(settings, engine, monkeypatch, capsys):
    write_report(RunReport("earlier", timeout_streak=2), settings.logs_dir)
    ended = stuck_cycle(monkeypatch)
    assert cmd_scrape(settings, engine, scrape_args()) == 1
    assert ended == [1]
    last = capsys.readouterr().out.rstrip().splitlines()[-1]
    assert last.startswith(f"FAILED -- {runner.STUCK} (timed out 3 cycles running)")


def test_end_stuck_writes_the_report_and_answers_the_exit_code(settings, capsys):
    assert cli.end_stuck(settings, "2026-10-09T19:00:06+00:00") == 0
    assert report_body(settings)["started_at"] == "2026-10-09T19:00:06+00:00"
    assert capsys.readouterr().out.rstrip().endswith(runner.REPORT_FILE)


def test_end_process_ends_it_with_the_code(monkeypatch):
    codes = []
    monkeypatch.setattr(cli.os, "_exit", codes.append)
    cli.end_process(1)
    assert codes == [1]


def test_scrape_cycle_runs_one_cycle_under_its_deadline(settings, engine, monkeypatch):
    seen = {}

    def cycle(*a, **kw):
        seen.update(kw)
        return RunReport("t")

    monkeypatch.setattr(cli, "run_cycle", cycle)
    deadline = datetime(2026, 10, 9, 19, 22, 30, tzinfo=UTC)
    assert cli.scrape_cycle(settings, engine, scrape_args(), deadline).started_at == "t"
    assert seen["deadline"] == deadline and seen["export"] is False


def test_a_cycle_that_ends_in_time_is_not_ended(settings, engine, monkeypatch):
    monkeypatch.setattr(cli, "run_cycle", lambda *a, **kw: RunReport("t"))
    monkeypatch.setattr(cli, "end_process", lambda code: pytest.fail("the watchdog fired"))
    assert cmd_scrape(settings, engine, scrape_args()) == 0
    assert report_body(settings)["timeouts"] == []


def test_a_cycle_without_a_timeout_ends_the_streak(settings, engine, monkeypatch):
    write_report(RunReport("earlier", timeout_streak=2), settings.logs_dir)
    monkeypatch.setattr(cli, "run_cycle", lambda *a, **kw: RunReport("t"))
    assert cmd_scrape(settings, engine, scrape_args()) == 0
    assert report_body(settings)["timeout_streak"] == 0


@pytest.mark.parametrize(
    "body", ["not json", "[]", '{"timeout_streak": "2"}', '{"timeout_streak": -1}']
)
def test_an_unreadable_previous_report_starts_the_streak_over(settings, body):
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    (settings.logs_dir / runner.REPORT_FILE).write_text(body, encoding="utf-8")
    assert runner.previous_streak(settings.logs_dir) == 0


OFFLINE = "no answer from the network: stocktwits (net::ERR_NAME_NOT_RESOLVED)"
HELD = "stocktwits: trending: server_error: HTTP None"


def offline_report(*a, **kw):
    return RunReport("t", offline=OFFLINE, offline_errors=[HELD], unanswered_loads=1)


def test_an_offline_machine_fails_the_scrape_only_at_the_streak(
    settings, engine, monkeypatch, capsys
):
    """3703e9d6, 3a72cc5e: this PC had no network for two days, and every cycle that ran
    then failed the scheduled job, each a ledger row that sent a fixer at nothing."""
    monkeypatch.setattr(cli, "run_cycle", offline_report)
    assert cmd_scrape(settings, engine, scrape_args()) == 0
    limit = runner.OFFLINE_STREAK_FAILS
    assert f"ok -- offline: {OFFLINE} (1 of {limit} before it fails)" in capsys.readouterr().out
    assert report_body(settings)["offline_streak"] == 1
    write_report(RunReport("earlier", offline_streak=limit - 1), settings.logs_dir)
    assert cmd_scrape(settings, engine, scrape_args()) == 1
    assert f"FAILED -- {runner.OFFLINE} -- details in" in capsys.readouterr().out
    assert report_body(settings)["errors"] == [runner.OFFLINE, HELD]


def test_a_cycle_the_network_answered_ends_the_offline_streak(settings, engine, monkeypatch):
    write_report(RunReport("earlier", offline_streak=7), settings.logs_dir)
    monkeypatch.setattr(cli, "run_cycle", lambda *a, **kw: RunReport("t", answered_loads=3))
    assert cmd_scrape(settings, engine, scrape_args()) == 0
    assert report_body(settings)["offline_streak"] == 0


LAKE_DOWN = "lake not accepting writes: s3://data-lake: put k failed: InternalError"


def lake_down_report(*a, **kw):
    return RunReport("t", export=[{"dataset": "a", "deferred": LAKE_DOWN}], lake_down=LAKE_DOWN)


def test_a_lake_refusing_writes_fails_the_scrape_only_after_a_day(
    settings, engine, monkeypatch, capsys
):
    """5354e2f6: the storage pool takes no writes while either of its PCs is off, and each
    scrape that night failed on it. The rows wait in the database; a day of it fails."""
    monkeypatch.setattr(cli, "run_cycle", lake_down_report)
    assert cmd_scrape(settings, engine, scrape_args()) == 0
    out = capsys.readouterr().out
    since = report_body(settings)["lake_down_since"]
    assert f"ok -- lake not accepting writes since {since[:16]}Z -- export of 1" in out
    assert cmd_scrape(settings, engine, scrape_args()) == 0
    assert report_body(settings)["lake_down_since"] == since, "the outage keeps its start"
    day_ago = datetime.now(UTC) - runner.LAKE_DOWN_FAILS_AFTER
    write_report(RunReport("earlier", lake_down_since=day_ago.isoformat()), settings.logs_dir)
    assert cmd_scrape(settings, engine, scrape_args()) == 1
    assert f"FAILED -- export: {LAKE_DOWN} (for 24 h, since" in capsys.readouterr().out


def test_an_export_the_lake_took_ends_the_outage(settings, engine, monkeypatch):
    write_report(
        RunReport("earlier", lake_down_since="2026-10-08T22:31:00+00:00"), settings.logs_dir
    )
    monkeypatch.setattr(
        cli, "run_cycle", lambda *a, **kw: RunReport("t", export=[{"dataset": "a"}])
    )
    assert cmd_scrape(settings, engine, scrape_args()) == 0
    assert report_body(settings)["lake_down_since"] is None


def test_a_cycle_with_no_export_carries_the_outage_on(settings, engine, monkeypatch):
    since = "2026-10-08T22:31:00+00:00"
    write_report(RunReport("earlier", lake_down_since=since), settings.logs_dir)
    monkeypatch.setattr(cli, "run_cycle", lambda *a, **kw: RunReport("t"))
    assert cmd_scrape(settings, engine, scrape_args()) == 0
    assert report_body(settings)["lake_down_since"] == since


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        "[]",
        "{}",
        '{"lake_down_since": 3}',
        '{"lake_down_since": "soon"}',
        '{"lake_down_since": "2026-10-08T22:31:00"}',
    ],
)
def test_an_unreadable_previous_report_knows_of_no_outage(settings, body):
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    (settings.logs_dir / runner.REPORT_FILE).write_text(body, encoding="utf-8")
    assert runner.previous_lake_down(settings.logs_dir) is None


def test_scrape_writes_the_report_and_sets_exit_code(settings, engine, monkeypatch, capsys):
    monkeypatch.setattr(cli, "run_cycle", lambda *a, **kw: RunReport("t", errors=["x: boom"]))
    assert cmd_scrape(settings, engine, scrape_args()) == 1
    assert "FAILED -- x: boom -- details in" in capsys.readouterr().out
    monkeypatch.setattr(cli, "run_cycle", lambda *a, **kw: RunReport("t"))
    assert cmd_scrape(settings, engine, scrape_args()) == 0
    assert "ok -- details in" in capsys.readouterr().out


def test_a_failed_scrape_names_its_first_error_without_the_url(tmp_path):
    """devkit files this line as the failure's cause: a bare `FAILED` made two
    unrelated X failures one ledger group (2026-10-06)."""
    report = RunReport(
        "t",
        errors=[
            "x: error: no timeline data captured (layout change or empty page) <- https://x.com/a",
            "reddit: later",
        ],
    )
    line = cli.outcome_line(report, tmp_path / "scrape-run.json")
    assert line.startswith("FAILED -- x: error: no timeline data captured (layout change")
    assert "x.com/a" not in line and "reddit" not in line
    assert line.endswith("scrape-run.json")


def test_an_ok_scrape_names_a_deferred_export(tmp_path):
    """A deferred export is not a failure, but a lake falling behind must show in the log."""
    export = [{"dataset": "a", "deferred": "out of export time"}, {"dataset": "b"}]
    line = cli.outcome_line(RunReport("t", export=export), tmp_path / "scrape-run.json")
    assert line.startswith("ok -- export of 1 dataset(s) deferred to the next run -- details")
    done = RunReport("t", export=[{"dataset": "b", "deferred": None}])
    assert cli.outcome_line(done, tmp_path).startswith("ok -- details in")


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


def test_scrape_ends_its_page_loads_inside_its_own_cycle(settings, engine, monkeypatch):
    """The deadline counts from before the jitter: the scheduler's next fire is due one
    cycle after this one, however long the wait was."""
    seen = {}
    monkeypatch.setattr(cli, "run_cycle", lambda *a, **kw: seen.update(kw) or RunReport("t"))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    monkeypatch.delenv(runner.FIRED_AT_ENV, raising=False)
    before = datetime.now(UTC)
    cmd_scrape(settings, engine, build_parser().parse_args(["scrape", "x", "--no-export"]))
    share = timedelta(minutes=settings.cycle_minutes * runner.CYCLE_DEADLINE_SHARE)
    assert before + share <= seen["deadline"] <= datetime.now(UTC) + share
    assert runner.CYCLE_DEADLINE_SHARE < 1


def test_a_scheduled_scrape_counts_its_deadline_from_the_fire(settings, engine, monkeypatch):
    """2026-10-07: the 17:30 fire reached `scrape` at 17:35:44 -- the db took 3m24s to
    come up and `uv run` 2m20s under load -- and a deadline counted from there let the
    run reach past the 18:00 fire. devkit names the fire's moment in the environment."""
    seen = {}
    monkeypatch.setattr(cli, "run_cycle", lambda *a, **kw: seen.update(kw) or RunReport("t"))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    fired = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=6)
    monkeypatch.setenv(runner.FIRED_AT_ENV, fired.isoformat())
    cmd_scrape(settings, engine, build_parser().parse_args(["scrape", "x", "--no-export"]))
    share = timedelta(minutes=settings.cycle_minutes * runner.CYCLE_DEADLINE_SHARE)
    assert seen["deadline"] == fired + share
    export_share = timedelta(minutes=settings.cycle_minutes * runner.EXPORT_DEADLINE_SHARE)
    assert runner.export_deadline(seen["deadline"], settings.cycle_minutes) == fired + export_share


def test_the_export_stops_after_the_page_loads_and_before_the_next_fire():
    """2026-10-07: a backfill held the lake's locks and the 20:30 export ran 35 minutes,
    so the 21:00 fire was skipped. The export ends inside the cycle, after the crawl's."""
    assert runner.CYCLE_DEADLINE_SHARE < runner.EXPORT_DEADLINE_SHARE < 1


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
        cli,
        "run_cycle",
        lambda s, e, p, export, deadline: runner.run_cycle(s, e, (), export=export),
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


SPORTS_LEAGUES = (
    '[[league]]\nkey = "nhl"\nname = "NHL"\nsport = "hockey"\nwikidata = "Q1215892"\n'
    '[[league]]\nkey = "lol"\nname = "LoL"\nsport = "esports"\n'
)


class FakeWikidata:
    """One team for any league; `fail` makes every query a 504."""

    fail = False

    def sparql(self, query):
        if FakeWikidata.fail:
            from social_scraper.sports.wikidata import WikidataError

            raise WikidataError("HTTP 504")
        if "ps:P118" in query:
            return [{"team": "http://www.wikidata.org/entity/Q1", "en": "Montreal Canadiens"}]
        return []


@pytest.fixture
def sports_settings(settings, tmp_path, monkeypatch):
    leagues = tmp_path / "sports-leagues.toml"
    leagues.write_text(SPORTS_LEAGUES, encoding="utf-8")
    settings.sports_leagues_file = leagues
    FakeWikidata.fail = False
    monkeypatch.setattr(cli, "WikidataClient", FakeWikidata)
    return settings


def sync_artifact(settings):
    return json.loads((settings.logs_dir / cli.SPORTS_SYNC_FILE).read_text(encoding="utf-8"))


def test_sports_sync_refreshes_the_named_leagues_now(sports_settings, engine, capsys):
    args = argparse.Namespace(leagues=["nhl"])
    assert cmd_sports_sync(sports_settings, engine, args) == 0
    assert "nhl: 1 entities" in capsys.readouterr().out
    assert sync_artifact(sports_settings)["synced"] == {"nhl": 1}
    # Fresh or not, a forced sync re-reads every league.
    assert cmd_sports_sync(sports_settings, engine, argparse.Namespace(leagues=[])) == 0
    assert sync_artifact(sports_settings)["synced"] == {"nhl": 1, "lol": 0}


def test_sports_sync_failures_exit_1_with_the_artifact(sports_settings, engine, capsys):
    FakeWikidata.fail = True
    assert cmd_sports_sync(sports_settings, engine, argparse.Namespace(leagues=["nhl"])) == 1
    assert sync_artifact(sports_settings)["errors"] == {"nhl": "HTTP 504"}
    assert cmd_sports_sync(sports_settings, engine, argparse.Namespace(leagues=["nope"])) == 1
    assert "no such league: nope" in sync_artifact(sports_settings)["error"]
    assert "FAILED" in capsys.readouterr().out


def test_status_shows_the_sports_domain_when_enabled(sports_settings, engine, capsys):
    cmd_sports_sync(sports_settings, engine, argparse.Namespace(leagues=["nhl"]))
    capsys.readouterr()
    assert cmd_status(sports_settings, engine, argparse.Namespace()) == 0
    assert "sports:" not in capsys.readouterr().out
    sports_settings.domains = "stocks,sports"
    assert cmd_status(sports_settings, engine, argparse.Namespace()) == 0
    out = capsys.readouterr().out
    assert "sports_entities: 1" in out and "trending (24h): " in out


def test_parser_accepts_sports_sync():
    assert build_parser().parse_args(["sports-sync"]).leagues == []
    assert build_parser().parse_args(["sports-sync", "nhl", "epl"]).leagues == ["nhl", "epl"]


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
    # A bare `scrape` first waits a random 0-20% of a cycle -- up to six real minutes.
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
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
