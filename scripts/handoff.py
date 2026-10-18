#!/usr/bin/env python3
"""Hand the collector's database from one PC to the other through the lake bucket.

The scheduled collector (`uv run social-scraper scrape`, every 30 minutes, with the
compose `db` started first) runs on one of two Windows PCs, and its Postgres lives in
that PC's `pgdata` volume. Moving it is a `pg_dump -Fc` on one side and a restore on the
other, with the dump parked in the bucket the lake export already writes
(`ARCHIVE_BACKEND=s3`, `ARCHIVE_S3_*`, built as `social_scraper/archive/store.py` builds
it). devkit's `scripts/collectors.py move` drives it: it is stdlib-only and cannot reach
the bucket, so it removes and registers the scheduled task and calls this script, from the
checkout on the host, for everything in between:

    uv run python scripts/handoff.py export --from PC-A --to PC-B
    uv run python scripts/handoff.py pending --host PC-B
    uv run python scripts/handoff.py import --host PC-B

Two objects, under `<prefix>/handoff/social-scraper/`:

- `db.dump` -- the last dump. Kept after an import: it is the last handoff's backup, and
  the next export overwrites it.
- `marker.json` -- `{"state": "exported", "from", "to", "at", "key", "sha256", "bytes",
  "tables", "posts", "last_request_at"}`, written only after the dump uploaded; an import
  rewrites it with `"state": "imported", "by", "imported_at"` and keeps the rest.

Exit codes: 0 success, 1 failed (the reason is in the artifact), 2 cannot tell -- only
`pending` uses it, for a bucket it cannot read. A bucket that is not configured is exit 1,
not 2: devkit stays quiet about "cannot tell" and says what a failure was. Every run
overwrites `logs/handoff.json` -- first with a "running" account, so a run devkit's bound
cut short says so -- and then with `{"command", "ok", "exit_code", "detail", ...}`, on
failure too. stdout is one status line, or for `pending` the marker JSON when a handoff
waits for this host and nothing otherwise.

What does not travel, and why, is docs/operations/collector-handoff.md.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from social_scraper.archive.store import S3Store, StoreError, is_unavailable, make_s3_client
from social_scraper.launch import state_key
from social_scraper.models import LOGIN_PLATFORMS, REDDIT, X
from social_scraper.runner import REPORT_FILE

REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = REPO_ROOT / "logs" / "handoff.json"

PROJECT = "social-scraper"
DUMP_NAME = "db.dump"
MARKER_NAME = "marker.json"
CONTAINER_DUMP = "/tmp/handoff.dump"  # noqa: S108 - inside the db container, not this host
EXPORTED, IMPORTED = "exported", "imported"
# Where an import moves the receiver's own, stale `logs/scrape-run.json`.
STALE_REPORT = "scrape-run.before-handoff.json"

EXIT_OK, EXIT_FAILED, EXIT_CANNOT_TELL = 0, 1, 2

QUICK_TIMEOUT = 120.0
# Seconds each verb may spend in all, a few minutes inside devkit's bounds
# (`collector_handoff.EXPORT_TIMEOUT`, 90 min; `IMPORT_TIMEOUT`, 60 min), so a slow run
# fails here and writes why, rather than being killed there with nothing written.
EXPORT_BUDGET = 85 * 60.0
IMPORT_BUDGET = 55 * 60.0
READY_TIMEOUT = 180.0
READY_POLL = 2.0
# `pending` has 120 s in devkit, `uv run` start-up included: one GET, with botocore's
# default 60 s timeouts and retries cut down so an unreachable pool answers in under one.
PENDING_SOCKET = {"connect_timeout": 5, "read_timeout": 20, "max_attempts": 2}

#: pg_restore entries that leave the data intact. Anything else fails the import. The
#: database is dropped and created empty first, so an "already exists" can only name what
#: `CREATE DATABASE` made from the template, never a table of ours.
IGNORABLE_RESTORE_ERRORS = (
    re.compile(r'schema "public" already exists', re.IGNORECASE),
    re.compile(r"must be owner of (schema public|extension plpgsql)", re.IGNORECASE),
)
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: What each login-able platform does until its login is redone on this machine.
LOGIN_EFFECTS = {
    X: "X search is skipped until then; seed and tracked accounts are still read logged out",
    REDDIT: "optional: Reddit is read anonymously without it",
}

TABLES_SQL = (
    "SELECT count(*) FROM information_schema.tables "
    "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
)
HAS_TABLES_SQL = (
    "SELECT to_regclass('public.posts') IS NOT NULL "
    "AND to_regclass('public.request_log') IS NOT NULL"
)
POSTS_SQL = "SELECT count(*) FROM posts"
# Every page load logs a request, so this is when the database last scraped anything.
LAST_REQUEST_SQL = (
    "SELECT coalesce(to_char(max(at) AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'), '') "
    "FROM request_log"
)


class HandoffError(Exception):
    """A step failed; the message becomes the artifact's `detail`."""


class CannotTell(HandoffError):
    """The bucket could not be read, so whether a handoff is pending is unknown."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


Runner = Callable[[list[str], float], CommandResult]


def run_command(argv: list[str], timeout: float) -> CommandResult:
    """The real runner: argv list, no shell, from the checkout so compose finds its file."""
    try:
        proc = subprocess.run(
            argv,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return CommandResult(124, "", f"timed out after {timeout:.0f}s")
    except OSError as exc:
        return CommandResult(127, "", f"cannot run {argv[0]}: {exc}")
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)


def _tail(text: str, lines: int = 5) -> str:
    return " | ".join(text.strip().splitlines()[-lines:])


@dataclass(frozen=True)
class DbIdentity:
    user: str
    database: str


class Compose:
    """`docker compose` in the checkout, through an injectable runner."""

    def __init__(self, runner: Runner):
        self.runner = runner

    def call(self, args: list[str], timeout: float = QUICK_TIMEOUT) -> CommandResult:
        return self.runner(["docker", "compose", *args], timeout)

    def run(self, args: list[str], what: str, timeout: float = QUICK_TIMEOUT) -> str:
        result = self.call(args, timeout)
        if result.returncode != 0:
            raise HandoffError(f"{what} failed (exit {result.returncode}): {_tail(result.stderr)}")
        return result.stdout

    def exec_db(self, args: list[str], what: str, timeout: float = QUICK_TIMEOUT) -> str:
        # `-T`: without it compose allocates a pseudo-TTY and the handle can outlive it.
        return self.run(["exec", "-T", "db", *args], what, timeout)

    def psql(self, ident: DbIdentity, database: str, sql: str) -> str:
        argv = ["psql", "-v", "ON_ERROR_STOP=1", "-U", ident.user, "-d", database, "-At"]
        return self.exec_db([*argv, "-c", sql], f"psql {sql!r}").strip()

    def remove_container_dump(self) -> None:
        """Best effort: a leftover only costs space in the container, and raising here
        would hide the error that sent us through the `finally`."""
        self.call(["exec", "-T", "db", "rm", "-f", CONTAINER_DUMP])


def db_identity(compose: Compose) -> DbIdentity:
    """The role and database the running `db` container was created with -- what initdb
    used, whatever `.env` says today (`POSTGRES_USER`/`POSTGRES_DB`, `social_scraper`)."""
    out = compose.exec_db(["printenv", "POSTGRES_USER", "POSTGRES_DB"], "read db identity")
    values = out.split()
    if len(values) != 2 or not all(_IDENTIFIER.fullmatch(v) for v in values):
        raise HandoffError(f"unexpected POSTGRES_USER/POSTGRES_DB in the db container: {values}")
    return DbIdentity(user=values[0], database=values[1])


@dataclass(frozen=True)
class DbFacts:
    """What a database holds, enough to check a restore and to compare two copies."""

    tables: int
    posts: int
    last_request_at: str  # ISO UTC, "" when it never loaded a page


def _count(text: str, what: str) -> int:
    if not text.isdigit():
        raise HandoffError(f"unexpected {what} count from psql: {text!r}")
    return int(text)


def read_facts(compose: Compose, ident: DbIdentity) -> DbFacts | None:
    """The database's facts, or None when it holds none of the scraper's tables."""
    if compose.psql(ident, ident.database, HAS_TABLES_SQL) != "t":
        return None
    return DbFacts(
        tables=_count(compose.psql(ident, ident.database, TABLES_SQL), "table"),
        posts=_count(compose.psql(ident, ident.database, POSTS_SQL), "posts"),
        last_request_at=compose.psql(ident, ident.database, LAST_REQUEST_SQL),
    )


def receiver_facts(compose: Compose, ident: DbIdentity) -> DbFacts | None:
    """`read_facts` of this machine's copy, or None when there is no database to read."""
    name = ident.database
    exists = f"SELECT count(*) FROM pg_database WHERE datname = '{name}'"  # noqa: S608 - db_identity admits bare identifiers only
    if compose.psql(ident, "postgres", exists) == "0":
        return None
    return read_facts(compose, ident)


@dataclass(frozen=True)
class Store:
    """The bucket: a boto3-style client plus where the handoff objects live."""

    client: Any
    bucket: str
    prefix: str = ""
    where: str = ""

    def key(self, name: str) -> str:
        return object_key(self.prefix, name)


def object_key(prefix: str, name: str) -> str:
    """`<prefix>/handoff/social-scraper/<name>`, joined like `S3Store.object_key`."""
    base = f"handoff/{PROJECT}/{name}"
    prefix = prefix.strip("/")
    return f"{prefix}/{base}" if prefix else base


def store_from_settings(settings: Any, config: Any = None) -> Store:
    """The bucket the lake export writes. Not configured is a failure, not "cannot tell"."""
    if settings.archive_backend != "s3":
        raise HandoffError(
            f"ARCHIVE_BACKEND is {settings.archive_backend!r}, not 's3': the handoff travels "
            "through the lake bucket, so set ARCHIVE_BACKEND=s3 and ARCHIVE_S3_* in .env"
        )
    try:
        s3 = S3Store(
            make_s3_client(settings, config), settings.archive_s3_bucket, settings.archive_s3_prefix
        )
    except StoreError as exc:
        raise HandoffError(str(exc)) from exc
    return Store(client=s3.client, bucket=s3.bucket, prefix=s3.prefix, where=s3.where)


def _is_missing(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    error = response.get("Error", {}) if isinstance(response, dict) else {}
    return isinstance(error, dict) and str(error.get("Code")) in {"404", "NoSuchKey", "NotFound"}


def _store_failure(action: str, store: Store, exc: Exception) -> HandoffError:
    hint = ""
    if is_unavailable(exc):
        hint = " -- the pool behind the bucket refuses writes while either storage PC is off"
    where = store.where or store.bucket
    return HandoffError(f"{action} failed at {where}: {type(exc).__name__}: {exc}{hint}")


def read_marker(store: Store) -> dict[str, Any] | None:
    """The marker, or None when there is none. One GET; CannotTell on anything else."""
    try:
        response = store.client.get_object(Bucket=store.bucket, Key=store.key(MARKER_NAME))
        body = response["Body"].read()
    except Exception as exc:
        if _is_missing(exc):
            return None
        raise CannotTell(f"cannot read the marker: {type(exc).__name__}: {exc}") from exc
    try:
        marker = json.loads(body)
    except ValueError as exc:
        raise CannotTell(f"the marker is not JSON: {exc}") from exc
    if not isinstance(marker, dict):
        raise CannotTell("the marker is not a JSON object")
    return marker


def write_marker(store: Store, marker: dict[str, Any]) -> None:
    try:
        store.client.put_object(
            Bucket=store.bucket,
            Key=store.key(MARKER_NAME),
            Body=json.dumps(marker, indent=2).encode(),
            ContentType="application/json",
        )
    except Exception as exc:
        raise _store_failure("writing the marker", store, exc) from exc


def pending_reason(marker: dict[str, Any] | None, host: str) -> str | None:
    """None when `marker` is a handoff waiting for `host`, else why it is not."""
    if marker is None:
        return "no handoff marker"
    if marker.get("state") != EXPORTED:
        return f"last handoff already {marker.get('state')} by {marker.get('by')}"
    if str(marker.get("to", "")).casefold() != host.casefold():
        return f"handoff addressed to {marker.get('to')}, not {host}"
    return None


def refuse_overwrite(marker: dict[str, Any] | None, from_host: str) -> None:
    """An export must not overwrite another machine's dump nobody has imported yet: that
    dump is newer than this machine's copy, which is why it was handed over."""
    if marker is None or marker.get("state") != EXPORTED:
        return
    if str(marker.get("from", "")).casefold() == from_host.casefold():
        return
    raise HandoffError(
        f"a handoff from {marker.get('from')} to {marker.get('to')} (exported {marker.get('at')}) "
        "has not been imported; exporting now would overwrite that newer dump. Import it first"
    )


def refuse_newer(here: DbFacts | None, marker: dict[str, Any], ident: DbIdentity) -> None:
    """The dump replaces this machine's copy, which is right while this copy is the older
    one -- the normal case: it is what this machine had when it last handed off. A copy
    with page loads after the dump's last means both machines scraped, and the restore
    would discard this one's; that needs a person, so nothing is touched."""
    ours = here.last_request_at if here else ""
    theirs = str(marker.get("last_request_at") or "")
    if not ours or ours <= theirs:
        return
    drop = f"docker compose exec -T db dropdb -U {ident.user} --force {ident.database}"
    raise HandoffError(
        f"this machine's database loaded pages up to {ours}, after the dump's last "
        f"({theirs or 'none'}) from {marker.get('from')}: both machines scraped, and the "
        f"restore would discard this one's rows. Nothing was changed. Hand off from here "
        f"instead, or drop this copy (`{drop}`) and import again"
    )


def file_digest(path: Path) -> tuple[str, int]:
    with path.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    return digest, path.stat().st_size


def iso_utc(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class Local:
    """What this checkout keeps outside the database: its logs, its browser profiles."""

    logs_dir: Path
    profile_dir: Callable[[str], Path]


@dataclass
class Deps:
    """Everything with a side effect, so a test can replace each one. `local` has no
    default on purpose: one pointing at this checkout would let a test move its logs."""

    compose: Compose
    store_factory: Callable[[], Store]
    local: Callable[[], Local]
    tmp_root: Path | None = None
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


@dataclass(frozen=True)
class Budget:
    """What is left of a verb's time, handed to each long step as its timeout."""

    clock: Callable[[], float]
    ends: float

    @classmethod
    def of(cls, deps: Deps, seconds: float) -> Budget:
        return cls(deps.clock, deps.clock() + seconds)

    def left(self, what: str) -> float:
        remaining = self.ends - self.clock()
        if remaining <= 0:
            raise HandoffError(f"out of time before {what}; nothing after it was done")
        return remaining


def wait_ready(deps: Deps, timeout: float = READY_TIMEOUT) -> None:
    """Start `db` and wait until Postgres accepts TCP connections, bounded.

    TCP, not the socket: on a fresh volume the image's initdb runs a temporary server that
    listens on the socket only, and would read as ready before the real one starts.
    """
    deps.compose.run(["up", "-d", "db"], "starting db")
    deadline = deps.clock() + timeout
    while True:
        result = deps.compose.call(["exec", "-T", "db", "pg_isready", "-h", "127.0.0.1"])
        if result.returncode == 0:
            return
        if deps.clock() >= deadline:
            raise HandoffError(f"db not ready after {timeout:.0f}s: {_tail(result.stderr)}")
        deps.sleep(READY_POLL)


def dump_to(compose: Compose, ident: DbIdentity, local: Path, budget: Budget) -> None:
    """`pg_dump -Fc` inside `db`, copied out to `local`; the container copy always goes."""
    try:
        compose.exec_db(
            ["pg_dump", "-Fc", "-U", ident.user, "-d", ident.database, "-f", CONTAINER_DUMP],
            "pg_dump",
            budget.left("pg_dump"),
        )
        compose.run(
            ["cp", f"db:{CONTAINER_DUMP}", str(local)],
            "copying the dump out",
            budget.left("copying the dump out"),
        )
    finally:
        compose.remove_container_dump()


def export(deps: Deps, from_host: str, to_host: str) -> dict[str, Any]:
    """Dump `db`, upload it, then -- only then -- write the marker addressed to `to_host`."""
    if from_host.casefold() == to_host.casefold():
        raise HandoffError(f"--from and --to are the same host ({from_host})")
    budget = Budget.of(deps, EXPORT_BUDGET)
    store = deps.store_factory()
    refuse_overwrite(read_marker(store), from_host)
    wait_ready(deps)
    ident = db_identity(deps.compose)
    facts = read_facts(deps.compose, ident)
    if facts is None:
        raise HandoffError("the database holds no scraper tables: nothing has run here to hand off")
    workdir = Path(tempfile.mkdtemp(prefix="scraper-handoff-", dir=deps.tmp_root))
    try:
        local = workdir / DUMP_NAME
        dump_to(deps.compose, ident, local, budget)
        sha256, size = file_digest(local)
        key = store.key(DUMP_NAME)
        try:
            store.client.upload_file(str(local), store.bucket, key)
        except Exception as exc:
            raise _store_failure("upload", store, exc) from exc
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    marker = {
        "state": EXPORTED,
        "from": from_host,
        "to": to_host,
        "at": iso_utc(deps.now()),
        "key": key,
        "sha256": sha256,
        "bytes": size,
        "tables": facts.tables,
        "posts": facts.posts,
        "last_request_at": facts.last_request_at,
    }
    write_marker(store, marker)
    return marker


def verify_dump(path: Path, marker: dict[str, Any]) -> None:
    sha256, size = file_digest(path)
    if size != marker.get("bytes"):
        raise HandoffError(f"dump is {size} bytes, the marker says {marker.get('bytes')}")
    if sha256 != marker.get("sha256"):
        raise HandoffError(f"dump sha256 {sha256} does not match the marker's")


def restore_errors(stderr: str) -> list[str]:
    """Each `pg_restore: error:` entry with its continuation lines (`Command was: ...`)."""
    entries: list[str] = []
    current: list[str] | None = None
    for line in stderr.splitlines():
        if line.startswith("pg_restore:"):
            if current:
                entries.append(" ".join(current))
            current = [line] if line.startswith("pg_restore: error:") else None
        elif current is not None:
            current.append(line.strip())
    if current:
        entries.append(" ".join(current))
    return entries


def judge_restore(result: CommandResult) -> list[str]:
    """The ignored pg_restore errors when the restore counts as good; raises otherwise.

    pg_restore without `--exit-on-error` keeps going past a failed statement, prints each
    as a `pg_restore: error:` entry and exits 1. Exit 1 passes only when every entry is in
    `IGNORABLE_RESTORE_ERRORS`; any other entry, any other exit, or exit 1 with no entry
    at all fails.
    """
    if result.returncode == 0:
        return []
    entries = restore_errors(result.stderr)
    if result.returncode != 1 or not entries:
        raise HandoffError(f"pg_restore failed (exit {result.returncode}): {_tail(result.stderr)}")
    fatal = [e for e in entries if not any(p.search(e) for p in IGNORABLE_RESTORE_ERRORS)]
    if fatal:
        raise HandoffError(f"pg_restore: {len(fatal)} error(s), first: {fatal[0][:400]}")
    return entries


def restore(compose: Compose, ident: DbIdentity, local: Path, budget: Budget) -> list[str]:
    """Replace the database with `local`, serially: drop it, create it empty, restore.
    Returns the ignored errors."""
    db = ident.database
    compose.psql(ident, "postgres", f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
    compose.psql(ident, "postgres", f"CREATE DATABASE {db}")
    try:
        compose.run(
            ["cp", str(local), f"db:{CONTAINER_DUMP}"],
            "copying the dump in",
            budget.left("copying the dump in"),
        )
        argv = ["pg_restore", "-Fc", "--no-owner", "-U", ident.user, "-d", db, CONTAINER_DUMP]
        result = compose.call(["exec", "-T", "db", *argv], budget.left("pg_restore"))
        ignored = judge_restore(result)
    finally:
        compose.remove_container_dump()
    compose.psql(ident, db, "ANALYZE")
    return ignored


def check_restored(compose: Compose, ident: DbIdentity, marker: dict[str, Any]) -> DbFacts:
    """The restored database holds what the exported one did: its tables and its posts."""
    facts = read_facts(compose, ident)
    if facts is None:
        raise HandoffError("the restored database holds no scraper tables")
    if facts.tables != marker.get("tables"):
        raise HandoffError(f"restored {facts.tables} tables, exported {marker.get('tables')}")
    if facts.posts != marker.get("posts"):
        raise HandoffError(f"restored {facts.posts} posts, exported {marker.get('posts')}")
    return facts


def reset_machine_streaks(compose: Compose, ident: DbIdentity) -> None:
    """Clear the sender's browser-launch streaks (`launch.py`): a slow Chrome start
    measures the machine, and this is another one."""
    pattern = state_key("%")  # a constant of `launch.py`, not input
    sql = f"DELETE FROM crawl_state WHERE key LIKE '{pattern}'"  # noqa: S608 - constant pattern
    compose.psql(ident, ident.database, sql)


def set_aside_report(logs_dir: Path) -> str:
    """Move this machine's own last `scrape-run.json` aside, so the first run here starts
    clean: its `timeout_streak` and `lake_down_since` are from whenever this machine last
    ran, and would count a first slow load or bucket refusal as a long streak."""
    report = logs_dir / REPORT_FILE
    if not report.exists():
        return "no scrape report here to set aside"
    try:
        report.replace(logs_dir / STALE_REPORT)
    except OSError as exc:
        return f"could not set {report} aside ({exc}); its streaks may carry into the first run"
    return f"this machine's last scrape report moved to {logs_dir / STALE_REPORT}"


def login_notices(local: Local) -> list[dict[str, Any]]:
    """Per `LOGIN_PLATFORMS`: the browser profile is this machine's own (they do not
    travel), whether there is one, and what redoing its login takes."""
    notices = []
    for platform in LOGIN_PLATFORMS:
        profile = local.profile_dir(platform)
        notices.append(
            {
                "platform": platform,
                "profile": str(profile),
                "profile_here": profile.is_dir(),
                "command": f"uv run social-scraper login {platform}",
                "until_then": LOGIN_EFFECTS[platform],
            }
        )
    return notices


def login_line(notices: list[dict[str, Any]]) -> str:
    """The notices as the one sentence the import's status line ends with."""
    parts = []
    for n in notices:
        name, command = n["platform"], n["command"]
        if n["profile_here"]:
            parts.append(f"{name}: this machine's own profile, if signed in (`{command}` if not)")
        else:
            parts.append(f"{name}: no profile here -- run `{command}` ({n['until_then']})")
    return "logins do not travel -- " + "; ".join(parts)


def import_dump(deps: Deps, host: str) -> dict[str, Any]:
    """Restore the dump addressed to `host`, then mark the handoff imported. Leaves `db`
    up and registers nothing: the scheduled task is devkit's."""
    budget = Budget.of(deps, IMPORT_BUDGET)
    store = deps.store_factory()
    marker = read_marker(store)
    reason = pending_reason(marker, host)
    if marker is None or reason is not None:
        raise HandoffError(f"nothing to import: {reason}")
    workdir = Path(tempfile.mkdtemp(prefix="scraper-handoff-", dir=deps.tmp_root))
    try:
        local = workdir / DUMP_NAME
        key = str(marker.get("key") or store.key(DUMP_NAME))
        try:
            store.client.download_file(store.bucket, key, str(local))
        except Exception as exc:
            raise _store_failure("download", store, exc) from exc
        verify_dump(local, marker)
        wait_ready(deps)
        ident = db_identity(deps.compose)
        refuse_newer(receiver_facts(deps.compose, ident), marker, ident)
        ignored = restore(deps.compose, ident, local, budget)
        facts = check_restored(deps.compose, ident, marker)
        reset_machine_streaks(deps.compose, ident)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    updated = {**marker, "state": IMPORTED, "by": host, "imported_at": iso_utc(deps.now())}
    write_marker(store, updated)
    here = deps.local()
    return {
        "marker": updated,
        "tables": facts.tables,
        "posts": facts.posts,
        "ignored_restore_errors": ignored,
        "scrape_report": set_aside_report(here.logs_dir),
        "logins": login_notices(here),
    }


def write_artifact(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _pending(args: argparse.Namespace, deps: Deps) -> tuple[int, dict[str, Any], str]:
    marker = read_marker(deps.store_factory())
    reason = pending_reason(marker, args.host)
    payload = {"ok": True, "pending": reason is None, "marker": marker}
    line = json.dumps(marker) if reason is None else ""
    return EXIT_OK, {**payload, "detail": reason or f"handoff pending for {args.host}"}, line


def _export(args: argparse.Namespace, deps: Deps) -> tuple[int, dict[str, Any], str]:
    marker = export(deps, args.from_host, args.to_host)
    detail = f"exported {marker['bytes']} bytes, {marker['posts']} posts, for {args.to_host}"
    logins = (
        f"logins do not travel: on {args.to_host}, `uv run social-scraper login x` unless its "
        "own profile is still signed in (X search is skipped until then; reddit is optional)"
    )
    payload = {"ok": True, "detail": detail, "marker": marker, "logins": logins}
    return EXIT_OK, payload, f"handoff: {detail} -- {logins}"


def _import(args: argparse.Namespace, deps: Deps) -> tuple[int, dict[str, Any], str]:
    result = import_dump(deps, args.host)
    detail = f"imported {result['marker']['bytes']} bytes, {result['tables']} tables, {result['posts']} posts"
    line = f"handoff: {detail} -- {login_line(result['logins'])}"
    return EXIT_OK, {"ok": True, "detail": detail, **result}, line


VERBS = {"pending": _pending, "export": _export, "import": _import}


def run(args: argparse.Namespace, deps: Deps) -> tuple[int, dict[str, Any], str]:
    """One subcommand: (exit code, artifact payload, stdout line or "")."""
    try:
        return VERBS[args.command](args, deps)
    except HandoffError as exc:
        unknown = args.command == "pending" and isinstance(exc, CannotTell)
        code = EXIT_CANNOT_TELL if unknown else EXIT_FAILED
        return code, {"ok": False, "detail": str(exc)}, f"handoff {args.command} FAILED: {exc}"


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    exp = sub.add_parser("export", help="dump db, upload it, mark it for --to")
    exp.add_argument("--from", dest="from_host", required=True)
    exp.add_argument("--to", dest="to_host", required=True)
    pend = sub.add_parser("pending", help="print the marker if a handoff waits for --host")
    pend.add_argument("--host", required=True)
    imp = sub.add_parser("import", help="restore the handoff addressed to --host")
    imp.add_argument("--host", required=True)
    return parser.parse_args(argv)


@functools.cache
def load_settings() -> Any:
    from pydantic import ValidationError

    from social_scraper.config import Settings

    try:
        # pydantic-settings ranks the process environment above this file, as everywhere else.
        env = REPO_ROOT / ".env"
        return Settings(_env_file=env)  # type: ignore[call-arg]  # pydantic-settings' init keyword, unseen by mypy
    except ValidationError as exc:
        raise HandoffError(f".env does not load: {exc}") from exc


def _in_checkout(path: Path) -> Path:
    return path if path.is_absolute() else REPO_ROOT / path


def settings_local() -> Local:
    settings = load_settings()
    return Local(_in_checkout(settings.logs_dir), lambda p: _in_checkout(settings.profile_dir(p)))


def settings_store(quick: bool) -> Store:
    config = None
    if quick:
        from botocore.config import Config

        s = PENDING_SOCKET
        config = Config(
            connect_timeout=s["connect_timeout"],
            read_timeout=s["read_timeout"],
            retries={"total_max_attempts": s["max_attempts"], "mode": "standard"},
        )
    return store_from_settings(load_settings(), config)


def default_deps(command: str) -> Deps:
    return Deps(
        compose=Compose(run_command),
        store_factory=lambda: settings_store(quick=command == "pending"),
        local=settings_local,
    )


def main(
    argv: list[str] | None = None,
    deps: Deps | None = None,
    artifact: Path = ARTIFACT,
) -> int:
    args = parse_args(argv)
    deps = deps or default_deps(args.command)
    head = {"command": args.command, **{k: v for k, v in vars(args).items() if k != "command"}}
    started = iso_utc(deps.now())
    running = f"running since {started}; if this is still here, the run was cut short"
    write_artifact(artifact, {**head, "exit_code": None, "ok": False, "detail": running})
    try:
        code, payload, line = run(args, deps)
    except Exception as exc:
        # A bug, not a reported failure: leave the artifact, then let the traceback out --
        # except from `pending`, whose caller reads any exit but 0 and 1 as "cannot tell".
        code = EXIT_CANNOT_TELL if args.command == "pending" else EXIT_FAILED
        detail = f"{type(exc).__name__}: {exc}"
        write_artifact(artifact, {**head, "exit_code": code, "ok": False, "detail": detail})
        if code == EXIT_CANNOT_TELL:
            print(f"handoff pending FAILED: {detail} ({artifact})", file=sys.stderr)
            raise SystemExit(code) from exc
        raise
    write_artifact(artifact, {**head, "exit_code": code, "started_at": started, **payload})
    if code == EXIT_OK:
        if line:
            print(line)
    elif args.command == "pending":
        print(f"{line} ({artifact})", file=sys.stderr)
    else:
        print(f"{line} ({artifact})")
    return code


if __name__ == "__main__":
    sys.exit(main())
