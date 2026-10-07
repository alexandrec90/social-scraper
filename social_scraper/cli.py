"""`uv run social-scraper <command>` -- `uv run` supplies the venv, so no activation.

init-db               create the operational tables
login reddit|x        open a visible browser to sign in once; the profile keeps it
scrape [reddit|x|all] one cycle, then export (skip with --no-export); first waits a
                      random 0-20% of CYCLE_MINUTES, for schedulers -- --now skips it
run                   cycle forever every CYCLE_MINUTES (or --cycles N)
                      scrape and run take --headed: show Chrome despite BROWSER_HEADLESS
                      and BROWSER_MINIMIZED
export [--full]       copy new rows to the lake (the local tree, and the bucket when
                      ARCHIVE_BACKEND=s3); --full re-sends every row, which is how a
                      newly configured bucket gets the history
status               budgets, pauses, row counts, trending symbols, X accounts (and the
                      sports equivalents when DOMAINS includes sports)
lake [--days N]       what the lake holds, per symbol and day (DuckDB lens)
universe              refresh the symbol directory and print its size
reddit-import PATH... backfill the lake from Pushshift / Arctic Shift dump files (or
                      folders of them); --since/--until YYYY-MM-DD (default since
                      2015-01-01), --all-posts keeps posts naming no symbol,
                      --local-only skips the bucket, --dry-run counts without writing
sports-sync [KEY...]  re-read the named leagues (default: all) from Wikidata now; the
                      outcome goes to logs/sports-sync.json, and any failure exits 1

`scrape` exits 1 when the cycle failed -- the database unreachable, a browser crash, a
platform erroring, a failed export -- and 0 otherwise. A platform deliberately left alone
(outside X active hours, budget spent, circuit breaker open) is a skip, not a failure, so
a scheduler firing outside the active hours sees 0.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from social_scraper.archive import lens
from social_scraper.archive.store import LocalStore, ObjectStore, stores_for
from social_scraper.config import SPORTS, Settings
from social_scraper.db import repository, schema
from social_scraper.db.throttle_store import DbThrottleStore
from social_scraper.export import export_to_lake
from social_scraper.models import PLATFORMS, REDDIT, X
from social_scraper.reddit import dumps
from social_scraper.runner import (
    RunReport,
    cycle_deadline,
    run_cycle,
    run_forever,
    start_jitter_s,
    universe_for,
    write_report,
)
from social_scraper.sports import registry as sports_registry
from social_scraper.sports.leagues import LeagueConfigError, load_leagues
from social_scraper.sports.sync import SyncPolicy, sync_leagues
from social_scraper.sports.wikidata import WikidataClient

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
        cmd.add_argument("--headed", action="store_true")
    sub.choices["scrape"].add_argument("--now", action="store_true")
    sub.choices["run"].add_argument("--cycles", type=int, default=None)
    sub.add_parser("export").add_argument("--full", action="store_true")
    sub.add_parser("status")
    lake = sub.add_parser("lake")
    lake.add_argument("--days", type=int, default=3)
    sub.add_parser("universe")
    imp = sub.add_parser("reddit-import")
    imp.add_argument("paths", nargs="+", type=Path)
    imp.add_argument("--since", type=utc_date, default=dumps.DEFAULT_SINCE)
    imp.add_argument("--until", type=utc_date, default=None)
    imp.add_argument("--all-posts", action="store_true")
    imp.add_argument("--local-only", action="store_true")
    imp.add_argument("--dry-run", action="store_true")
    sub.add_parser("sports-sync").add_argument("leagues", nargs="*", metavar="KEY")
    return parser


def utc_date(value: str) -> datetime:
    """`2021-01-31` (or a full ISO timestamp) as an aware UTC datetime."""
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def platforms_of(choice: str) -> tuple[str, ...]:
    return PLATFORMS if choice == "all" else (choice,)


def cmd_scrape(settings: Settings, engine: Engine, args: argparse.Namespace) -> int:
    # From before the jitter: the wait is part of the cycle the next fire is due after.
    deadline = cycle_deadline(datetime.now(UTC), settings.cycle_minutes)
    if not args.now:
        # A scheduler fires on a fixed grid, and a fixed interval is a bot signature. The
        # default, not a flag: devkit's scheduled task runs bare `scrape`, and with the
        # old opt-in `--jitter` every cycle started at :00:0x or :30:0x (2026-10-03).
        wait = start_jitter_s(settings.cycle_minutes)
        print(f"waiting {wait:.0f} s so the start is off the clock grid (--now to skip)")
        time.sleep(wait)
    started = datetime.now(UTC).isoformat()
    try:
        schema.create_all(engine)
        report = run_cycle(
            settings,
            engine,
            platforms_of(args.platform),
            export=not args.no_export,
            deadline=deadline,
        )
    except Exception as exc:
        # The database down, or a bug outside the per-platform guard. Still overwrite the
        # report: the previous run's "ok" must not be what is left on disk. Re-raised, so
        # the traceback is printed and the process exits 1 as an uncaught error does.
        report = RunReport(
            started_at=started,
            finished_at=datetime.now(UTC).isoformat(),
            errors=[f"cycle: {type(exc).__name__}: {exc}"],
        )
        print(f"FAILED -- details in {write_report(report, settings.logs_dir)}")
        raise
    path = write_report(report, settings.logs_dir)
    print(outcome_line(report, path))
    return 0 if report.ok else 1


def outcome_line(report: RunReport, path: Path) -> str:
    """The scrape's last word, which devkit's scheduler files as a failure's cause.

    A failure leads with its first error, minus the URL: a bare `FAILED` made a renamed
    X op and one dead account one ledger group, read as a fix that did not hold
    (2026-10-06). The URL, and every other error, are in the report.
    """
    if report.ok:
        return f"ok -- details in {path}"
    first = report.errors[0].split(" <- ")[0]
    return f"FAILED -- {first[:160]} -- details in {path}"


def cmd_run(settings: Settings, engine: Engine, args: argparse.Namespace) -> int:
    schema.create_all(engine)
    run_forever(
        settings, engine, platforms_of(args.platform), cycles=args.cycles, export=not args.no_export
    )
    return 0


def cmd_export(settings: Settings, engine: Engine, args: argparse.Namespace) -> int:
    if args.full:
        with engine.begin() as conn:
            repository.clear_watermarks(conn)
        print("watermarks cleared: re-exporting every row")
    result = export_to_lake(engine, settings, datetime.now(UTC))
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
        if SPORTS in settings.enabled_domains:
            print("sports:")
            for table, count in sports_registry.counts(conn).items():
                print(f"  {table}: {count}")
            hot = sports_registry.trending(conn, now - timedelta(hours=24), 15)
            print("  trending (24h): " + ", ".join(f"{name} {n}" for _, name, n in hot))
            t = schema.sports_x_accounts
            rows = conn.execute(select(t.c.status, func.count()).group_by(t.c.status)).all()
            print("  x accounts: " + ", ".join(f"{s} {n}" for s, n in rows))
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


def cmd_reddit_import(settings: Settings, engine: Engine, args: argparse.Namespace) -> int:
    started = datetime.now(UTC)
    flt = dumps.DumpFilter(
        subreddits=frozenset(s.lower() for s in settings.subreddits),
        since=args.since,
        until=args.until,
        ignore_authors=settings.ignored_reddit_authors,
        all_posts=args.all_posts,
    )
    try:
        stores: list[ObjectStore] | None = None  # a dry run writes nowhere
        if not args.dry_run:
            stores = (
                [LocalStore(settings.archive_root)] if args.local_only else stores_for(settings)
            )
        universe = universe_for(settings)
        # The spool can reach gigabytes; `data/` is on the same disk as the lake tree.
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="reddit-import-", dir=settings.data_dir) as spool:
            paths = dumps.expand_inputs(args.paths)
            report = dumps.import_dumps(paths, universe, flt, stores, Path(spool), started)
    except Exception as exc:
        # A bucket that cannot be configured, the symbol directory unreachable: the
        # previous run's report must not be what is left on disk. Re-raised, as in scrape.
        report = dumps.ImportReport(
            started_at=started.isoformat(),
            finished_at=datetime.now(UTC).isoformat(),
            dry_run=args.dry_run,
            errors=[f"import: {type(exc).__name__}: {exc}"],
        )
        print(f"FAILED -- details in {dumps.write_report(report, settings.logs_dir)}")
        raise
    path = dumps.write_report(report, settings.logs_dir)
    for row in report.by_year:
        print(
            f"  {row['year']}: {row['posts']} posts ({row['comments']} comments), "
            f"{row['symbols']} symbols"
        )
    where = (
        "would be written" if args.dry_run else f"in {sum(report.partitions.values())} partitions"
    )
    print(f"{len(report.files)} file(s): {report.posts} posts, {report.mentions} mentions {where}")
    print(f"{'ok' if report.ok else 'FAILED'} -- details in {path}")
    return 0 if report.ok else 1


SPORTS_SYNC_FILE = "sports-sync.json"


def cmd_sports_sync(settings: Settings, engine: Engine, args: argparse.Namespace) -> int:
    """Force a Wikidata refresh now; the result is an artifact, failures exit 1."""
    path = settings.logs_dir / SPORTS_SYNC_FILE
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    body: dict[str, Any] = {"started_at": datetime.now(UTC).isoformat()}
    try:
        leagues = load_leagues(settings.sports_leagues_file)
        unknown = sorted(set(args.leagues) - {lg.key for lg in leagues})
        if unknown:
            raise LeagueConfigError(f"no such league: {', '.join(unknown)}")
        chosen = tuple(lg for lg in leagues if not args.leagues or lg.key in args.leagues)
        schema.create_all(engine)
        policy = SyncPolicy(timedelta(0), settings.sports_players_per_team)
        stats = sync_leagues(engine, WikidataClient(), chosen, datetime.now(UTC), policy)
    except LeagueConfigError as exc:
        body |= {"ok": False, "error": str(exc)}
        path.write_text(json.dumps(body, indent=2), encoding="utf-8")
        print(f"FAILED -- {exc} (details in {path})")
        return 1
    body |= {"ok": not stats.errors, **asdict(stats)}
    path.write_text(json.dumps(body, indent=2), encoding="utf-8")
    for key, n in stats.synced.items():
        print(f"{key}: {n} entities")
    for key, error in stats.errors.items():
        print(f"{key}: FAILED {error}")
    print(f"{'ok' if not stats.errors else 'FAILED'} -- details in {path}")
    return 0 if not stats.errors else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings()
    if getattr(args, "headed", False):
        settings.browser_headless = False
        settings.browser_minimized = False  # --headed is for watching the window
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
        "reddit-import": cmd_reddit_import,
        "sports-sync": cmd_sports_sync,
    }[args.command]
    return handler(settings, engine, args)


if __name__ == "__main__":
    sys.exit(main())
