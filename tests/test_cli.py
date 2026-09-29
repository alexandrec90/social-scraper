import argparse
from datetime import UTC, datetime

import pytest
from conftest import Clock, IsolatedSettings

from social_scraper import cli
from social_scraper.cli import (
    build_parser,
    cmd_export,
    cmd_lake,
    cmd_run,
    cmd_scrape,
    cmd_status,
    cmd_universe,
    main,
    platforms_of,
)
from social_scraper.models import PLATFORMS, AuthorInfo, Post
from social_scraper.pipeline import Ingestor
from social_scraper.runner import RunReport


@pytest.fixture
def settings(tmp_path):
    return IsolatedSettings(
        data_dir=tmp_path / "data",
        archive_root=tmp_path / "lake",
        logs_dir=tmp_path / "logs",
    )


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
    assert p.parse_args(["run", "reddit", "--cycles", "2"]).cycles == 2
    assert p.parse_args(["login", "x"]).platform == "x"
    assert p.parse_args(["lake", "--days", "7"]).days == 7
    with pytest.raises(SystemExit):
        p.parse_args(["login", "facebook"])


def test_platforms_of():
    assert platforms_of("all") == PLATFORMS
    assert platforms_of("x") == ("x",)


def test_scrape_writes_the_report_and_sets_exit_code(settings, engine, monkeypatch, capsys):
    monkeypatch.setattr(cli, "run_cycle", lambda *a, **kw: RunReport("t", errors=["x: boom"]))
    args = argparse.Namespace(platform="x", no_export=True)
    assert cmd_scrape(settings, engine, args) == 1
    assert "FAILED" in capsys.readouterr().out
    monkeypatch.setattr(cli, "run_cycle", lambda *a, **kw: RunReport("t"))
    assert cmd_scrape(settings, engine, args) == 0


def test_run_passes_options_through(settings, engine, monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "run_forever", lambda *a, **kw: seen.update(kw))
    cmd_run(settings, engine, argparse.Namespace(platform="reddit", no_export=True, cycles=3))
    assert seen == {"cycles": 3, "export": False}


def test_export_status_and_lake(settings, engine, universe, capsys):
    seed(engine, universe)
    assert cmd_export(settings, engine, argparse.Namespace()) == 0
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
