"""`uv run social-scraper <command>` -- `uv run` supplies the venv, so no activation.

init-db               create the operational tables
login reddit|x        open a visible browser to sign in once; the profile keeps it
scrape [reddit|x|all] one cycle now, then export (skip with --no-export)
run                   cycle forever every CYCLE_MINUTES (or --cycles N)
export                copy new rows to the lake
status                budgets, pauses, row counts, trending symbols, X accounts
lake [--days N]       what the lake holds, per symbol and day (DuckDB lens)
universe              refresh the symbol directory and print its size
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from social_scraper.archive import lens
from social_scraper.config import Settings
from social_scraper.db import repository, schema
from social_scraper.db.throttle_store import DbThrottleStore
from social_scraper.export import export_all
from social_scraper.models import PLATFORMS, REDDIT, X
from social_scraper.runner import run_cycle, run_forever, universe_for, write_report

LOGIN_URLS = {REDDIT: "https://www.reddit.com/login/", X: "https://x.com/i/flow/login"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="social-scraper", description=__doc__.splitlines()[0])
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db")
    login = sub.add_parser("login")
    login.add_argument("platform", choices=PLATFORMS)
    for name in ("scrape", "run"):
        cmd = sub.add_parser(name)
        cmd.add_argument("platform", nargs="?", default="all", choices=(*PLATFORMS, "all"))
        cmd.add_argument("--no-export", action="store_true")
    sub.choices["run"].add_argument("--cycles", type=int, default=None)
    sub.add_parser("export")
    sub.add_parser("status")
    lake = sub.add_parser("lake")
    lake.add_argument("--days", type=int, default=3)
    sub.add_parser("universe")
    return parser


def platforms_of(choice: str) -> tuple[str, ...]:
    return PLATFORMS if choice == "all" else (choice,)


def cmd_scrape(settings: Settings, engine: Engine, args: argparse.Namespace) -> int:
    schema.create_all(engine)
    report = run_cycle(settings, engine, platforms_of(args.platform), export=not args.no_export)
    path = write_report(report, settings.logs_dir)
    print(f"{'ok' if report.ok else 'FAILED'} -- details in {path}")
    return 0 if report.ok else 1


def cmd_run(settings: Settings, engine: Engine, args: argparse.Namespace) -> int:
    schema.create_all(engine)
    run_forever(
        settings, engine, platforms_of(args.platform), cycles=args.cycles, export=not args.no_export
    )
    return 0


def cmd_export(settings: Settings, engine: Engine, args: argparse.Namespace) -> int:
    result = export_all(engine, settings.archive_root, datetime.now(UTC))
    for d in result.datasets:
        state = d.error or f"{d.rows} rows, {d.partitions} partitions"
        extra = f", {d.dropped_secrets} dropped (secret pattern)" if d.dropped_secrets else ""
        print(f"{d.dataset}: {state}{extra}")
    return 0 if result.ok else 1


def cmd_status(settings: Settings, engine: Engine, args: argparse.Namespace) -> int:
    now = datetime.now(UTC)
    store = DbThrottleStore(engine)
    for platform in PLATFORMS:
        hour = store.count_since(platform, now - timedelta(hours=1))
        day = store.count_since(platform, now - timedelta(days=1))
        block = store.get_block(platform)
        paused = (
            f" PAUSED until {block.blocked_until:%Y-%m-%d %H:%M}Z ({block.reason})"
            if block.blocked_until and block.blocked_until > now
            else ""
        )
        print(f"{platform}: {hour} requests last hour, {day} last day{paused}")
    with engine.connect() as conn:
        for table, count in repository.table_counts(conn).items():
            print(f"  {table}: {count}")
        trending = repository.trending_symbols(conn, now - timedelta(hours=24), 15)
        print("trending (24h): " + ", ".join(f"{s} {n}" for s, n in trending))
        t = schema.x_accounts
        rows = conn.execute(select(t.c.status, func.count()).group_by(t.c.status)).all()
        print("x accounts: " + ", ".join(f"{s} {n}" for s, n in rows))
    return 0


def cmd_lake(settings: Settings, engine: Engine, args: argparse.Namespace) -> int:
    con = lens.connect(settings.archive_root)
    since = datetime.now(UTC) - timedelta(days=args.days)
    try:
        rows = lens.symbol_daily(con, since)
    finally:
        con.close()
    print(f"{'symbol':<8} {'day':<10} {'mentions':>8} {'authors':>7} {'stance':>7}")
    for symbol, day, mentions, authors, stance, _engagement in rows:
        print(f"{symbol:<8} {day!s:<10} {mentions:>8} {authors:>7} {stance!s:>7}")
    return 0


def cmd_universe(settings: Settings, engine: Engine, args: argparse.Namespace) -> int:
    settings.symbol_universe_max_age_hours = 0
    print(f"{len(universe_for(settings))} symbols")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings()
    if args.command == "login":
        from social_scraper.browser import open_for_login

        print(f"Sign in to {args.platform} in the window that opens, then close it.")
        open_for_login(
            settings.profile_dir(args.platform), settings.browser_channel, LOGIN_URLS[args.platform]
        )
        return 0
    engine = repository.make_engine(settings.database_url)
    if args.command == "init-db":
        schema.create_all(engine)
        print("tables ready")
        return 0
    handler = {
        "scrape": cmd_scrape,
        "run": cmd_run,
        "export": cmd_export,
        "status": cmd_status,
        "lake": cmd_lake,
        "universe": cmd_universe,
    }[args.command]
    return handler(settings, engine, args)


if __name__ == "__main__":
    sys.exit(main())
