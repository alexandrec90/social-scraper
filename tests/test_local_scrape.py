"""The local end-to-end tier: one real `social-scraper scrape`, start to finish.

The scrape is marked `local`, which `addopts` deselects everywhere but `scripts/e2e.py`
(`.claude/rules/local-e2e.md`): it needs this workstation's Google Chrome and the live
StockTwits API, neither of which a hosted runner has. Every other test drives the
scrapers through `conftest.FakeBrowser`; this one is what proves the parts still meet --
the CLI, `Settings` from the environment, a headless Chrome launched by Playwright,
StockTwits answering it, the parser, the database and the export to the lake, read back
through the DuckDB lens.

StockTwits, because it is the browser platform that needs no account (measured
2026-10-10, `stocktwits/scraper.py`) and costs two loads: the trending list and one
symbol's stream. Reddit and X ban, and Bluesky and Polymarket do not use the browser.

Isolation: the scrape runs in a subprocess whose working directory is `tmp_path`, so no
`.env` is read (`Settings` reads it relative to the cwd), with every path, the database
(SQLite) and the lake (local) under `tmp_path`. The checkout's own profiles, database and
bucket are never touched. Anything missing on the machine fails with what is missing;
there is no skip, since the run that selects this test is the one it was written for.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from social_scraper.archive import lens
from social_scraper.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[1]
SYMBOL = "AAPL"  # busy enough that its newest 30 messages are hours old at most
# Chrome's first start in a fresh profile, the symbol directory's download, two paced
# loads and the export: minutes, not ten of them.
TIMEOUT_S = 600


def scrape_env(root: Path, base: dict[str, str]) -> dict[str, str]:
    """`base` with every `Settings` variable dropped, then this test's set over it.

    Dropped first so nothing the machine exports (a `DATABASE_URL`, an
    `ARCHIVE_BACKEND=s3`) reaches the scrape: pydantic-settings matches names case-blind.
    """
    fields = set(Settings.model_fields)
    env = {k: v for k, v in base.items() if k.lower() not in fields}
    env.update(
        {
            "PYTHONPATH": str(REPO_ROOT),
            "PYTHONIOENCODING": "utf-8",
            "DATABASE_URL": f"sqlite:///{(root / 'e2e.db').as_posix()}",
            "DATA_DIR": str(root / "data"),
            "LOGS_DIR": str(root / "logs"),
            "ARCHIVE_ROOT": str(root / "archive"),
            "ARCHIVE_BACKEND": "local",
            "DOMAINS": "stocks",
            "SCRAPE_PLATFORMS": "stocktwits",
            "CRAWL_WORKERS": "1",
            "WATCHLIST": SYMBOL,
            "SYMBOL_EXTRA_FILES": "",
            "BROWSER_CHANNEL": "chrome",
            "BROWSER_HEADLESS": "true",
            # The scheduler runs this at night; StockTwits' default hours would skip it.
            "STOCKTWITS_ACTIVE_HOURS": "",
            "STOCKTWITS_SYMBOLS_PER_CYCLE": "1",
            "STOCKTWITS_MIN_DELAY_S": "1",
            "STOCKTWITS_MAX_DELAY_S": "3",
        }
    )
    return env


def test_scrape_env_isolates_every_setting() -> None:
    """The scrape sees this test's values, never the machine's, whatever their case."""
    root = Path("/work/e2e")  # never created: the function only builds strings
    env = scrape_env(root, {"database_url": "postgresql://x", "ARCHIVE_ROOT": "/x", "PATH": "p"})
    assert env["PATH"] == "p"
    assert "database_url" not in env
    assert env["DATABASE_URL"] == "sqlite:////work/e2e/e2e.db"
    assert env["ARCHIVE_ROOT"] == str(root / "archive")
    assert env["STOCKTWITS_ACTIVE_HOURS"] == ""
    # Every name the test sets is one `Settings` reads: a typo would be silently ignored.
    ours = set(scrape_env(root, {})) - {"PYTHONPATH", "PYTHONIOENCODING"}
    assert {k.lower() for k in ours} <= set(Settings.model_fields)


def describe(done: subprocess.CompletedProcess[str], report: Path) -> str:
    """What a failing assertion shows: the run's last output and its report."""
    shown = report.read_text(encoding="utf-8") if report.is_file() else "(no report written)"
    return (
        f"exit {done.returncode}\n--- stdout (tail)\n{done.stdout[-3000:]}\n"
        f"--- stderr (tail)\n{done.stderr[-3000:]}\n--- {report.name}\n{shown[-3000:]}"
    )


@pytest.mark.local
def test_stocktwits_scrape_reaches_the_lake(tmp_path: Path) -> None:
    done = subprocess.run(
        [sys.executable, "-m", "social_scraper", "scrape", "stocktwits", "--now"],
        cwd=tmp_path,
        env=scrape_env(tmp_path, dict(os.environ)),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=TIMEOUT_S,
        check=False,
    )
    report_path = tmp_path / "logs" / "scrape-run.json"
    why = describe(done, report_path)
    assert done.returncode == 0, why
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["ok"], why
    # A skip, an offline verdict or an empty stream is an ok cycle that proves nothing.
    crawl = report["platforms"].get("stocktwits", {})
    assert crawl.get("ingest", {}).get("stored", 0) > 0, why

    engine = create_engine(f"sqlite:///{(tmp_path / 'e2e.db').as_posix()}")
    try:
        with engine.connect() as conn:
            stored = conn.execute(
                text("SELECT count(*) FROM posts WHERE platform = 'stocktwits'")
            ).scalar_one()
    finally:
        engine.dispose()
    assert stored > 0, why

    con = lens.connect(tmp_path / "archive")
    try:
        exported = con.execute(
            "SELECT count(*) FROM social_posts WHERE platform = 'stocktwits'"
        ).fetchone()
        daily = lens.symbol_daily(con, datetime.now(UTC) - timedelta(days=30), limit=500)
    finally:
        con.close()
    assert exported is not None and exported[0] > 0, why
    assert SYMBOL in {row[0] for row in daily}, why
