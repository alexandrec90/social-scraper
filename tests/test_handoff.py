"""Tests for `scripts/handoff.py`, the collector database handoff through the lake bucket.

No Docker, no network, no real database: the compose runner and the S3 client are fakes.
The fake runner answers each `docker compose` argv the way a healthy `db` container would
and records the sequence, so the restore order -- and the absence of `-j` -- is asserted
command by command.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any, ClassVar

import pytest
from botocore.exceptions import ClientError
from conftest import IsolatedSettings

from social_scraper.models import LOGIN_PLATFORMS

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "handoff.py"


def load_handoff() -> ModuleType:
    """Import `scripts/handoff.py`, registered first so `@dataclass` can find its module."""
    spec = importlib.util.spec_from_file_location("handoff_script", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


handoff: Any = load_handoff()

DUMP = b"PGDMP fake custom-format dump" * 100
DUMP_SHA = hashlib.sha256(DUMP).hexdigest()
NOW = datetime(2026, 10, 18, 12, 0, 0, tzinfo=UTC)
KEY = "handoff/social-scraper/db.dump"
MARKER_KEY = "handoff/social-scraper/marker.json"
SENDER_LAST = "2026-10-18T11:30:00Z"
RECEIVER_LAST = "2026-09-30T08:00:00Z"  # when this machine last ran, before it handed off
DROP = "DROP DATABASE IF EXISTS social_scraper WITH (FORCE)"
# The dump's path inside the db container, and the best-effort removal of it.
IN_DB = handoff.CONTAINER_DUMP
RM_IN_DB = ["exec", "-T", "db", "rm", "-f", IN_DB]


class MissingKey(Exception):
    response: ClassVar[dict] = {"Error": {"Code": "NoSuchKey"}}


def server_error(op: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": "InternalError"}, "ResponseMetadata": {"HTTPStatusCode": 503}}, op
    )


class FakeS3:
    """The boto3 client surface the script uses, over a dict."""

    def __init__(self, objects: dict[str, bytes] | None = None):
        self.objects = dict(objects or {})
        self.fail: dict[str, Exception] = {}
        self.calls: list[str] = []

    def _maybe_fail(self, op: str) -> None:
        self.calls.append(op)
        if op in self.fail:
            raise self.fail[op]

    def get_object(self, Bucket, Key):
        self._maybe_fail("get_object")
        if Key not in self.objects:
            raise MissingKey(Key)
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self._maybe_fail("put_object")
        self.objects[Key] = Body

    def upload_file(self, Filename, Bucket, Key):
        self._maybe_fail("upload_file")
        self.objects[Key] = Path(Filename).read_bytes()

    def download_file(self, Bucket, Key, Filename):
        self._maybe_fail("download_file")
        Path(Filename).write_bytes(self.objects[Key])


class FakeCompose:
    """Answers `docker compose ...` argvs like a healthy stack, recording each one.

    The database holds the scraper's tables (`has_tables`) with `last` as its newest page
    load, until a `pg_restore` makes it the exported one.
    """

    def __init__(
        self,
        restore=None,
        ready=None,
        tables="14",
        posts="5000",
        last=RECEIVER_LAST,
        has_tables=True,
        db_exists=True,
    ):
        self.calls: list[list[str]] = []
        self.restore = restore or handoff.CommandResult(0)
        self.ready = list(ready or [0])
        self.tables, self.posts, self.last = tables, posts, last
        self.has_tables, self.db_exists = has_tables, db_exists
        self.copied_in: bytes | None = None

    def __call__(self, argv: list[str], timeout: float):
        assert argv[:2] == ["docker", "compose"]
        assert timeout > 0
        args = argv[2:]
        self.calls.append(args)
        result = handoff.CommandResult
        if "printenv" in args:
            return result(0, "social_scraper\nsocial_scraper\n")
        if "pg_isready" in args:
            return result(self.ready.pop(0) if len(self.ready) > 1 else self.ready[0], "", "no")
        if "pg_restore" in args:
            self.has_tables, self.last = True, SENDER_LAST
            return self.restore
        if "psql" in args:
            return result(0, self.answer(args[-1]) + "\n")
        if args[0] == "cp":
            src, dst = args[1], args[2]
            if src.startswith("db:"):
                Path(dst).write_bytes(DUMP)
            else:
                self.copied_in = Path(src).read_bytes()
        return result(0)

    def answer(self, sql: str) -> str:
        if sql == handoff.HAS_TABLES_SQL:
            return "t" if self.has_tables else "f"
        if "pg_database" in sql:
            return "1" if self.db_exists else "0"
        return {
            handoff.TABLES_SQL: self.tables,
            handoff.POSTS_SQL: self.posts,
            handoff.LAST_REQUEST_SQL: self.last,
        }.get(sql, "")

    def sql(self) -> list[str]:
        return [c[-1] for c in self.calls if "psql" in c]


def exported_marker(**overrides):
    marker = {
        "state": "exported",
        "from": "pc-a",
        "to": "pc-b",
        "at": "2026-10-18T11:45:00Z",
        "key": KEY,
        "sha256": DUMP_SHA,
        "bytes": len(DUMP),
        "tables": 14,
        "posts": 5000,
        "last_request_at": SENDER_LAST,
    }
    return {**marker, **overrides}


def local_for(root: Path):
    return handoff.Local(root / "logs", lambda p: root / "data" / "browser-profiles" / p)


def make_deps(tmp_path, runner, s3, clock=None):
    tmp_root = tmp_path / "tmp"
    tmp_root.mkdir(parents=True)
    return handoff.Deps(
        compose=handoff.Compose(runner),
        store_factory=lambda: handoff.Store(client=s3, bucket="bucket"),
        local=lambda: local_for(tmp_path),
        tmp_root=tmp_root,
        now=lambda: NOW,
        clock=clock or iter(range(10_000)).__next__,
        sleep=lambda _s: None,
    )


def stored_marker(s3: FakeS3) -> dict:
    return json.loads(s3.objects[MARKER_KEY])


def with_marker(marker=None, dump=DUMP) -> FakeS3:
    s3 = FakeS3({KEY: dump})
    if marker is not None:
        s3.objects[MARKER_KEY] = json.dumps(marker).encode()
    return s3


# --- keys, markers, settings -----------------------------------------------------------


def test_object_key_joins_the_prefix_like_the_archive():
    assert handoff.object_key("", "db.dump") == KEY
    assert handoff.object_key("/lake/", "marker.json") == "lake/" + MARKER_KEY
    assert handoff.Store(client=None, bucket="b", prefix="p").key("db.dump") == "p/" + KEY


@pytest.mark.parametrize(
    ("marker", "pending"),
    [
        (None, False),
        (exported_marker(), True),
        (exported_marker(to="PC-B"), True),
        (exported_marker(to="pc-c"), False),
        (exported_marker(state="imported", by="pc-b"), False),
    ],
)
def test_pending_reason(marker, pending):
    assert (handoff.pending_reason(marker, "pc-b") is None) is pending


def test_read_marker_none_when_missing_and_cannot_tell_otherwise():
    s3 = FakeS3()
    store = handoff.Store(client=s3, bucket="b")
    assert handoff.read_marker(store) is None
    s3.objects[MARKER_KEY] = b"not json"
    with pytest.raises(handoff.CannotTell):
        handoff.read_marker(store)
    s3.objects[MARKER_KEY] = b"[1]"
    with pytest.raises(handoff.CannotTell):
        handoff.read_marker(store)
    s3.fail["get_object"] = ConnectionError("offline")
    with pytest.raises(handoff.CannotTell, match="offline"):
        handoff.read_marker(store)


def test_write_marker_failure_names_the_pool_when_the_server_failed():
    s3 = FakeS3()
    s3.fail["put_object"] = server_error("PutObject")
    with pytest.raises(handoff.HandoffError, match=r"writing the marker.*storage PC is off"):
        handoff.write_marker(handoff.Store(client=s3, bucket="b"), {"state": "x"})
    s3.fail["put_object"] = ConnectionError("down")
    with pytest.raises(handoff.HandoffError, match="writing the marker") as info:
        handoff.write_marker(handoff.Store(client=s3, bucket="b"), {"state": "x"})
    assert "storage PC" not in str(info.value)


def test_store_needs_the_s3_backend_and_says_so_as_a_failure_not_cannot_tell():
    with pytest.raises(handoff.HandoffError, match="ARCHIVE_BACKEND is 'local'") as info:
        handoff.store_from_settings(IsolatedSettings(archive_backend="local"))
    assert not isinstance(info.value, handoff.CannotTell)
    with pytest.raises(handoff.HandoffError, match="ARCHIVE_S3_BUCKET"):
        handoff.store_from_settings(IsolatedSettings(archive_backend="s3"))


def test_store_is_built_like_the_archive_store(aws_env):
    settings = IsolatedSettings(
        archive_backend="s3",
        archive_s3_bucket="data-lake",
        archive_s3_endpoint_url="http://pool.invalid:8333",
        archive_s3_prefix="/pre/",
    )
    store = handoff.store_from_settings(settings)
    assert (store.bucket, store.prefix) == ("data-lake", "pre")
    assert store.key("marker.json") == "pre/" + MARKER_KEY
    assert "http://pool.invalid:8333" in store.where


def test_pending_gets_a_client_that_answers_inside_its_bound(aws_env, monkeypatch):
    settings = IsolatedSettings(archive_backend="s3", archive_s3_bucket="data-lake")
    monkeypatch.setattr(handoff, "load_settings", lambda: settings)
    quick = handoff.settings_store(quick=True).client.meta.config
    assert (quick.connect_timeout, quick.read_timeout) == (5, 20)
    assert quick.retries["total_max_attempts"] == 2
    # Worst case, every attempt spends both timeouts: well inside devkit's 120 s.
    assert quick.retries["total_max_attempts"] * (5 + 20) < 120
    slow = handoff.settings_store(quick=False).client.meta.config
    assert slow.connect_timeout == 60  # botocore's default, for the long transfers


def test_load_settings_reports_a_bad_env_as_a_handoff_error(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("CYCLE_MINUTES=not-a-number\n", encoding="utf-8")
    monkeypatch.setattr(handoff, "REPO_ROOT", tmp_path)
    handoff.load_settings.cache_clear()
    try:
        with pytest.raises(handoff.HandoffError, match=r"\.env does not load"):
            handoff.load_settings()
    finally:
        handoff.load_settings.cache_clear()


def test_settings_local_resolves_relative_paths_in_the_checkout(monkeypatch, tmp_path):
    settings = IsolatedSettings(logs_dir=Path("logs"), data_dir=Path("data"))
    monkeypatch.setattr(handoff, "load_settings", lambda: settings)
    local = handoff.settings_local()
    assert local.logs_dir == handoff.REPO_ROOT / "logs"
    assert local.profile_dir("x") == handoff.REPO_ROOT / "data" / "browser-profiles" / "x"
    absolute = IsolatedSettings(logs_dir=tmp_path / "l", data_dir=tmp_path / "d")
    monkeypatch.setattr(handoff, "load_settings", lambda: absolute)
    assert handoff.settings_local().logs_dir == tmp_path / "l"


def test_default_deps_use_the_quick_client_for_pending_only(monkeypatch):
    seen = []
    monkeypatch.setattr(handoff, "settings_store", lambda quick: seen.append(quick))
    handoff.default_deps("pending").store_factory()
    handoff.default_deps("import").store_factory()
    assert seen == [True, False]


# --- small helpers ---------------------------------------------------------------------


def test_file_digest_and_iso_utc(tmp_path):
    path = tmp_path / "f"
    path.write_bytes(DUMP)
    assert handoff.file_digest(path) == (DUMP_SHA, len(DUMP))
    assert handoff.iso_utc(NOW) == "2026-10-18T12:00:00Z"


def test_verify_dump(tmp_path):
    path = tmp_path / "f"
    path.write_bytes(DUMP)
    handoff.verify_dump(path, exported_marker())
    with pytest.raises(handoff.HandoffError, match="bytes"):
        handoff.verify_dump(path, exported_marker(bytes=1))
    with pytest.raises(handoff.HandoffError, match="sha256"):
        handoff.verify_dump(path, exported_marker(sha256="0" * 64))


def test_db_identity_reads_the_container_env_and_refuses_odd_values():
    compose = handoff.Compose(FakeCompose())
    assert handoff.db_identity(compose) == handoff.DbIdentity("social_scraper", "social_scraper")

    def odd(argv, timeout):
        return handoff.CommandResult(0, "social_scraper\nscraper'; drop\n")

    with pytest.raises(handoff.HandoffError, match="unexpected"):
        handoff.db_identity(handoff.Compose(odd))


def test_compose_run_raises_with_the_stderr_tail():
    def failing(argv, timeout):
        return handoff.CommandResult(3, "", "line1\nboom")

    with pytest.raises(handoff.HandoffError, match=r"exit 3.*boom"):
        handoff.Compose(failing).run(["up"], "starting db")


def test_run_command_runs_argv_without_a_shell():
    ok = handoff.run_command([sys.executable, "-c", "print('hi')"], 30)
    assert (ok.returncode, ok.stdout.strip()) == (0, "hi")
    assert handoff.run_command(["definitely-not-a-binary-xyz"], 5).returncode == 127
    slow = handoff.run_command([sys.executable, "-c", "import time; time.sleep(5)"], 0.2)
    assert slow.returncode == 124


def test_wait_ready_starts_db_polls_until_ready_then_gives_up(tmp_path):
    runner = FakeCompose(ready=[2, 2, 0])
    handoff.wait_ready(make_deps(tmp_path, runner, FakeS3()))
    assert runner.calls[0] == ["up", "-d", "db"]
    assert sum("pg_isready" in c for c in runner.calls) == 3
    assert ["exec", "-T", "db", "pg_isready", "-h", "127.0.0.1"] in runner.calls

    never = FakeCompose(ready=[2])
    with pytest.raises(handoff.HandoffError, match="not ready"):
        handoff.wait_ready(make_deps(tmp_path / "b", never, FakeS3()), timeout=5)


def test_budget_hands_out_what_is_left_and_then_refuses():
    ticks = iter([0.0, 10.0, 200.0])
    budget = handoff.Budget(ticks.__next__, 100.0)
    assert budget.left("one") == 100.0
    assert budget.left("two") == 90.0
    with pytest.raises(handoff.HandoffError, match="out of time before three"):
        budget.left("three")


def test_budgets_stay_inside_devkits_bounds():
    # devkit's collector_handoff: EXPORT_TIMEOUT 90 min, IMPORT_TIMEOUT 60 min.
    assert handoff.EXPORT_BUDGET < 90 * 60
    assert handoff.IMPORT_BUDGET < 60 * 60


# --- what a database holds -------------------------------------------------------------


def test_read_facts_reads_tables_posts_and_the_last_page_load():
    compose = handoff.Compose(FakeCompose(last=SENDER_LAST))
    ident = handoff.DbIdentity("social_scraper", "social_scraper")
    assert handoff.read_facts(compose, ident) == handoff.DbFacts(14, 5000, SENDER_LAST)
    assert handoff.read_facts(handoff.Compose(FakeCompose(has_tables=False)), ident) is None
    with pytest.raises(handoff.HandoffError, match="posts count"):
        handoff.read_facts(handoff.Compose(FakeCompose(posts="ERROR")), ident)


def test_receiver_facts_is_none_without_a_database():
    ident = handoff.DbIdentity("social_scraper", "social_scraper")
    runner = FakeCompose(db_exists=False)
    assert handoff.receiver_facts(handoff.Compose(runner), ident) is None
    assert runner.sql() == ["SELECT count(*) FROM pg_database WHERE datname = 'social_scraper'"]


@pytest.mark.parametrize(
    ("marker", "refused"),
    [
        (None, False),
        (exported_marker(state="imported", by="pc-b"), False),
        (exported_marker(**{"from": "PC-B", "to": "pc-c"}), False),  # this machine's own
        (exported_marker(**{"from": "pc-c", "to": "pc-b"}), True),  # someone else's, waiting
    ],
)
def test_refuse_overwrite_keeps_an_unimported_dump_from_elsewhere(marker, refused):
    if refused:
        with pytest.raises(handoff.HandoffError, match="Import it first"):
            handoff.refuse_overwrite(marker, "pc-b")
    else:
        handoff.refuse_overwrite(marker, "pc-b")


@pytest.mark.parametrize(
    ("here", "theirs", "refused"),
    [
        (None, SENDER_LAST, False),  # a fresh machine
        (RECEIVER_LAST, SENDER_LAST, False),  # the old copy from its last turn
        (SENDER_LAST, SENDER_LAST, False),
        ("", SENDER_LAST, False),  # tables, never a page load
        ("2026-10-18T11:59:00Z", SENDER_LAST, True),  # both scraped
        ("2026-10-18T11:59:00Z", "", True),
    ],
)
def test_refuse_newer_only_when_this_copy_scraped_after_the_dump(here, theirs, refused):
    ident = handoff.DbIdentity("social_scraper", "social_scraper")
    facts = None if here is None else handoff.DbFacts(14, 1, here)
    marker = exported_marker(last_request_at=theirs)
    if refused:
        with pytest.raises(handoff.HandoffError, match="both machines scraped") as info:
            handoff.refuse_newer(facts, marker, ident)
        assert "dropdb -U social_scraper --force social_scraper" in str(info.value)
    else:
        handoff.refuse_newer(facts, marker, ident)


# --- pg_restore success criteria -------------------------------------------------------

PUBLIC_SCHEMA_ERROR = (
    'pg_restore: error: could not execute query: ERROR:  schema "public" already exists\n'
    "Command was: CREATE SCHEMA public;\n"
    "pg_restore: warning: errors ignored on restore: 1\n"
)
REAL_ERROR = (
    'pg_restore: error: could not execute query: ERROR:  relation "posts" does not exist\n'
    "Command was: COPY public.posts FROM stdin;\n"
    "pg_restore: warning: errors ignored on restore: 1\n"
)


def test_restore_errors_groups_continuation_lines():
    entries = handoff.restore_errors(PUBLIC_SCHEMA_ERROR + REAL_ERROR)
    assert len(entries) == 2
    assert "CREATE SCHEMA public" in entries[0]
    assert "COPY public.posts" in entries[1]


def test_judge_restore_clean_and_allowlisted_errors_pass():
    assert handoff.judge_restore(handoff.CommandResult(0)) == []
    assert len(handoff.judge_restore(handoff.CommandResult(1, "", PUBLIC_SCHEMA_ERROR))) == 1
    owner = "pg_restore: error: could not execute query: ERROR:  must be owner of schema public\n"
    assert len(handoff.judge_restore(handoff.CommandResult(1, "", owner))) == 1


@pytest.mark.parametrize(
    "result",
    [
        handoff.CommandResult(1, "", REAL_ERROR),
        handoff.CommandResult(1, "", PUBLIC_SCHEMA_ERROR + REAL_ERROR),
        handoff.CommandResult(1, "", "pg_restore: warning: something odd\n"),
        handoff.CommandResult(2, "", PUBLIC_SCHEMA_ERROR),
    ],
)
def test_judge_restore_anything_else_fails(result):
    with pytest.raises(handoff.HandoffError, match="pg_restore"):
        handoff.judge_restore(result)


# --- export ----------------------------------------------------------------------------


def test_export_uploads_then_writes_the_marker(tmp_path):
    runner, s3 = FakeCompose(last=SENDER_LAST), FakeS3()
    deps = make_deps(tmp_path, runner, s3)
    marker = handoff.export(deps, "pc-a", "pc-b")

    assert s3.objects[KEY] == DUMP
    assert s3.calls == ["get_object", "upload_file", "put_object"]
    assert stored_marker(s3) == marker == exported_marker(at="2026-10-18T12:00:00Z")
    assert runner.calls[:2] == [
        ["up", "-d", "db"],
        ["exec", "-T", "db", "pg_isready", "-h", "127.0.0.1"],
    ]
    dump_cmd = next(c for c in runner.calls if "pg_dump" in c)
    assert dump_cmd == (
        f"exec -T db pg_dump -Fc -U social_scraper -d social_scraper -f {IN_DB}".split()
    )
    assert runner.calls[-1] == RM_IN_DB
    assert list(deps.tmp_root.iterdir()) == []


def test_dump_to_dumps_in_the_container_copies_out_and_removes_its_copy(tmp_path):
    runner = FakeCompose()
    ident = handoff.DbIdentity("social_scraper", "social_scraper")
    local = tmp_path / "db.dump"
    budget = handoff.Budget(iter(range(100)).__next__, 1000.0)
    handoff.dump_to(handoff.Compose(runner), ident, local, budget)
    assert local.read_bytes() == DUMP
    assert [c[3] if c[0] == "exec" else c[0] for c in runner.calls] == ["pg_dump", "cp", "rm"]
    assert runner.calls[1] == ["cp", f"db:{IN_DB}", str(local)]


def test_reset_machine_streaks_clears_only_the_launch_streaks():
    runner = FakeCompose()
    ident = handoff.DbIdentity("social_scraper", "social_scraper")
    handoff.reset_machine_streaks(handoff.Compose(runner), ident)
    # `launch.state_key`'s shape, every platform: no other crawl_state key matches it.
    assert runner.sql() == ["DELETE FROM crawl_state WHERE key LIKE 'browser:%:launch_timeouts'"]


def test_export_upload_failure_writes_no_marker_and_cleans_up(tmp_path):
    runner, s3 = FakeCompose(), FakeS3()
    s3.fail["upload_file"] = server_error("UploadPart")
    deps = make_deps(tmp_path, runner, s3)
    with pytest.raises(handoff.HandoffError, match=r"upload failed.*storage PC is off"):
        handoff.export(deps, "pc-a", "pc-b")
    assert MARKER_KEY not in s3.objects
    assert "put_object" not in s3.calls
    assert list(deps.tmp_root.iterdir()) == []


def test_export_pg_dump_failure_still_removes_the_container_copy(tmp_path):
    base = FakeCompose()

    def runner(argv, timeout):
        if "pg_dump" in argv:
            return handoff.CommandResult(1, "", "disk full")
        return base(argv, timeout)

    s3 = FakeS3()
    deps = make_deps(tmp_path, runner, s3)
    with pytest.raises(handoff.HandoffError, match=r"pg_dump failed.*disk full"):
        handoff.export(deps, "pc-a", "pc-b")
    assert base.calls[-1] == RM_IN_DB
    assert s3.objects == {}
    assert list(deps.tmp_root.iterdir()) == []


def test_export_refuses_the_same_host_twice(tmp_path):
    runner = FakeCompose()
    with pytest.raises(handoff.HandoffError, match="same host"):
        handoff.export(make_deps(tmp_path, runner, FakeS3()), "pc-a", "PC-A")
    assert runner.calls == []


def test_export_refuses_to_overwrite_a_dump_waiting_for_this_machine(tmp_path):
    runner = FakeCompose()
    s3 = with_marker(exported_marker(**{"from": "pc-b", "to": "pc-a"}))
    with pytest.raises(handoff.HandoffError, match="has not been imported"):
        handoff.export(make_deps(tmp_path, runner, s3), "pc-a", "pc-b")
    assert runner.calls == []
    assert s3.calls == ["get_object"]


def test_export_of_a_database_that_never_ran_is_refused(tmp_path):
    runner, s3 = FakeCompose(has_tables=False), FakeS3()
    with pytest.raises(handoff.HandoffError, match="nothing has run here"):
        handoff.export(make_deps(tmp_path, runner, s3), "pc-a", "pc-b")
    assert not any("pg_dump" in c for c in runner.calls)


def test_export_out_of_time_stops_before_the_next_step(tmp_path):
    ticks = iter([0.0, *[handoff.EXPORT_BUDGET + 1] * 50])
    runner, s3 = FakeCompose(), FakeS3()
    deps = make_deps(tmp_path, runner, s3, clock=ticks.__next__)
    deps.sleep = lambda _s: None
    with pytest.raises(handoff.HandoffError, match="out of time before pg_dump"):
        handoff.export(deps, "pc-a", "pc-b")
    assert "upload_file" not in s3.calls


# --- import ----------------------------------------------------------------------------


def test_import_replaces_this_copy_serially_and_marks_imported(tmp_path):
    runner = FakeCompose()
    s3 = with_marker(exported_marker())
    deps = make_deps(tmp_path, runner, s3)
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "scrape-run.json").write_text('{"timeout_streak": 2}', encoding="utf-8")
    result = handoff.import_dump(deps, "PC-B")

    restore_cmd = next(c for c in runner.calls if "pg_restore" in c)
    assert restore_cmd == (
        f"exec -T db pg_restore -Fc --no-owner -U social_scraper -d social_scraper {IN_DB}".split()
    )
    assert not any(a == "-j" or a.startswith(("-j", "--jobs")) for c in runner.calls for a in c)
    assert runner.copied_in == DUMP

    def step(c):
        return ("psql", c[c.index("-d") + 1], c[-1]) if "psql" in c else tuple(c)

    restore_at = runner.calls.index(restore_cmd)
    facts = [
        ("psql", "social_scraper", handoff.HAS_TABLES_SQL),
        ("psql", "social_scraper", handoff.TABLES_SQL),
        ("psql", "social_scraper", handoff.POSTS_SQL),
        ("psql", "social_scraper", handoff.LAST_REQUEST_SQL),
    ]
    assert [step(c) for c in runner.calls] == [
        ("up", "-d", "db"),
        ("exec", "-T", "db", "pg_isready", "-h", "127.0.0.1"),
        ("exec", "-T", "db", "printenv", "POSTGRES_USER", "POSTGRES_DB"),
        (
            "psql",
            "postgres",
            "SELECT count(*) FROM pg_database WHERE datname = 'social_scraper'",
        ),
        *facts,  # this machine's copy, compared with the dump's
        ("psql", "postgres", DROP),
        ("psql", "postgres", "CREATE DATABASE social_scraper"),
        ("cp", runner.calls[restore_at - 1][1], f"db:{IN_DB}"),
        tuple(restore_cmd),
        tuple(RM_IN_DB),
        ("psql", "social_scraper", "ANALYZE"),
        *facts,  # the restored copy, checked against the marker
        (
            "psql",
            "social_scraper",
            "DELETE FROM crawl_state WHERE key LIKE 'browser:%:launch_timeouts'",
        ),
    ]

    assert stored_marker(s3) == {
        **exported_marker(),
        "state": "imported",
        "by": "PC-B",
        "imported_at": "2026-10-18T12:00:00Z",
    }
    assert (result["tables"], result["posts"]) == (14, 5000)
    assert s3.objects[KEY] == DUMP  # kept as the last handoff's backup
    assert list(deps.tmp_root.iterdir()) == []
    # This machine's own streaks are set aside, so its first run starts clean.
    assert not (tmp_path / "logs" / "scrape-run.json").exists()
    assert (tmp_path / "logs" / "scrape-run.before-handoff.json").exists()
    assert "moved" in result["scrape_report"]
    assert {n["platform"] for n in result["logins"]} == set(LOGIN_PLATFORMS)


def test_import_into_a_machine_with_no_database_yet(tmp_path):
    runner = FakeCompose(db_exists=False, has_tables=False)
    s3 = with_marker(exported_marker())
    result = handoff.import_dump(make_deps(tmp_path, runner, s3), "pc-b")
    assert result["posts"] == 5000
    assert stored_marker(s3)["state"] == "imported"


@pytest.mark.parametrize(
    ("marker", "match"),
    [
        (None, "no handoff marker"),
        (exported_marker(to="pc-c"), "addressed to pc-c"),
        (exported_marker(state="imported", by="pc-b"), "already imported"),
    ],
)
def test_import_with_nothing_pending_touches_nothing(tmp_path, marker, match):
    runner = FakeCompose()
    s3 = with_marker(marker)
    with pytest.raises(handoff.HandoffError, match=f"nothing to import: .*{match}"):
        handoff.import_dump(make_deps(tmp_path, runner, s3), "pc-b")
    assert runner.calls == []
    assert "put_object" not in s3.calls


def test_import_sha_mismatch_fails_before_docker(tmp_path):
    runner = FakeCompose()
    s3 = with_marker(exported_marker(), dump=DUMP[:-1] + b"x")
    deps = make_deps(tmp_path, runner, s3)
    with pytest.raises(handoff.HandoffError, match="sha256"):
        handoff.import_dump(deps, "pc-b")
    assert runner.calls == []
    assert stored_marker(s3)["state"] == "exported"
    assert list(deps.tmp_root.iterdir()) == []


def test_import_refuses_to_replace_a_copy_newer_than_the_dump(tmp_path):
    runner = FakeCompose(last="2026-10-18T11:59:00Z")
    s3 = with_marker(exported_marker())
    with pytest.raises(handoff.HandoffError, match="both machines scraped"):
        handoff.import_dump(make_deps(tmp_path, runner, s3), "pc-b")
    assert DROP not in runner.sql()
    assert not any("pg_restore" in c for c in runner.calls)
    assert stored_marker(s3)["state"] == "exported"


def test_import_failed_restore_keeps_the_marker_pending(tmp_path):
    runner = FakeCompose(restore=handoff.CommandResult(1, "", REAL_ERROR))
    s3 = with_marker(exported_marker())
    deps = make_deps(tmp_path, runner, s3)
    with pytest.raises(handoff.HandoffError, match="does not exist"):
        handoff.import_dump(deps, "pc-b")
    assert stored_marker(s3)["state"] == "exported"
    assert runner.calls[-1] == RM_IN_DB
    assert list(deps.tmp_root.iterdir()) == []


def test_import_allowlisted_restore_errors_pass(tmp_path):
    runner = FakeCompose(restore=handoff.CommandResult(1, "", PUBLIC_SCHEMA_ERROR))
    s3 = with_marker(exported_marker())
    result = handoff.import_dump(make_deps(tmp_path, runner, s3), "pc-b")
    assert len(result["ignored_restore_errors"]) == 1
    assert stored_marker(s3)["state"] == "imported"


@pytest.mark.parametrize(
    ("runner", "match"),
    [
        (FakeCompose(tables="13"), "13 tables, exported 14"),
        (FakeCompose(posts="4999"), "4999 posts, exported 5000"),
        (FakeCompose(has_tables=False), "no scraper tables"),
    ],
)
def test_check_restored_compares_with_the_marker(runner, match):
    ident = handoff.DbIdentity("social_scraper", "social_scraper")
    with pytest.raises(handoff.HandoffError, match=match):
        handoff.check_restored(handoff.Compose(runner), ident, exported_marker())


# --- what does not travel --------------------------------------------------------------


def test_every_login_platform_says_what_it_does_until_redone():
    assert set(handoff.LOGIN_EFFECTS) == set(LOGIN_PLATFORMS)


def test_login_notices_name_the_command_and_this_machines_profile(tmp_path):
    local = local_for(tmp_path)
    local.profile_dir("reddit").mkdir(parents=True)
    notices = {n["platform"]: n for n in handoff.login_notices(local)}
    assert notices["x"]["profile_here"] is False
    assert notices["reddit"]["profile_here"] is True
    assert notices["x"]["command"] == "uv run social-scraper login x"
    line = handoff.login_line(list(notices.values()))
    assert "x: no profile here -- run `uv run social-scraper login x`" in line
    assert "X search is skipped" in line
    assert "reddit: this machine's own profile" in line


def test_set_aside_report(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    assert "no scrape report" in handoff.set_aside_report(logs)
    logs.mkdir()
    (logs / "scrape-run.json").write_text("{}", encoding="utf-8")
    assert "moved" in handoff.set_aside_report(logs)
    assert (logs / "scrape-run.before-handoff.json").read_text(encoding="utf-8") == "{}"

    (logs / "scrape-run.json").write_text("{}", encoding="utf-8")

    def locked(self, target):
        raise PermissionError("in use")

    monkeypatch.setattr(Path, "replace", locked)
    assert "could not set" in handoff.set_aside_report(logs)


# --- the CLI: exit codes, stdout, artifact ---------------------------------------------


def test_parse_args_requires_the_hosts():
    args = handoff.parse_args(["export", "--from", "A", "--to", "B"])
    assert (args.command, args.from_host, args.to_host) == ("export", "A", "B")
    with pytest.raises(SystemExit):
        handoff.parse_args(["import"])


def run_main(tmp_path, argv, runner, s3):
    artifact = tmp_path / "logs" / "handoff.json"
    code = handoff.main(argv, make_deps(tmp_path, runner, s3), artifact)
    return code, json.loads(artifact.read_text(encoding="utf-8"))


def test_main_pending_prints_the_marker_for_this_host(tmp_path, capsys):
    s3 = with_marker(exported_marker())
    runner = FakeCompose()
    code, artifact = run_main(tmp_path, ["pending", "--host", "pc-b"], runner, s3)
    assert code == 0
    assert json.loads(capsys.readouterr().out) == exported_marker()
    assert artifact["pending"] is True and artifact["ok"] is True
    assert runner.calls == [] and s3.calls == ["get_object"]  # one GET, no Docker


def test_main_pending_elsewhere_prints_nothing(tmp_path, capsys):
    s3 = with_marker(exported_marker(to="pc-c"))
    code, artifact = run_main(tmp_path, ["pending", "--host", "pc-b"], FakeCompose(), s3)
    assert (code, capsys.readouterr().out) == (0, "")
    assert artifact["pending"] is False


def test_main_pending_unreachable_store_exits_2(tmp_path, capsys):
    s3 = FakeS3()
    s3.fail["get_object"] = ConnectionError("no route")
    code, artifact = run_main(tmp_path, ["pending", "--host", "pc-b"], FakeCompose(), s3)
    assert code == 2
    assert capsys.readouterr().out == ""
    assert artifact["ok"] is False and "no route" in artifact["detail"]


def test_main_pending_without_the_s3_backend_exits_1(tmp_path, capsys):
    deps = make_deps(tmp_path, FakeCompose(), FakeS3())
    deps.store_factory = lambda: handoff.store_from_settings(IsolatedSettings())
    artifact = tmp_path / "a.json"
    assert handoff.main(["pending", "--host", "pc-b"], deps, artifact) == 1
    assert "ARCHIVE_BACKEND" in json.loads(artifact.read_text(encoding="utf-8"))["detail"]
    assert capsys.readouterr().out == ""  # stdout is pending's answer; failures go to stderr


def test_main_export_failure_exits_1_and_writes_the_artifact(tmp_path, capsys):
    s3 = FakeS3()
    s3.fail["upload_file"] = ConnectionError("network")
    argv = ["export", "--from", "pc-a", "--to", "pc-b"]
    code, artifact = run_main(tmp_path, argv, FakeCompose(), s3)
    assert code == 1
    assert artifact["command"] == "export" and artifact["ok"] is False
    assert artifact["exit_code"] == 1 and "upload failed" in artifact["detail"]
    out = capsys.readouterr().out
    assert out.count("\n") == 1 and "FAILED" in out


def test_main_export_then_import_say_which_logins_to_redo(tmp_path, capsys):
    s3 = FakeS3()
    argv = ["export", "--from", "pc-a", "--to", "pc-b"]
    code, artifact = run_main(tmp_path / "a", argv, FakeCompose(last=SENDER_LAST), s3)
    assert code == 0 and artifact["marker"]["to"] == "pc-b"
    assert "on pc-b, `uv run social-scraper login x`" in capsys.readouterr().out

    code, artifact = run_main(tmp_path / "b", ["import", "--host", "pc-b"], FakeCompose(), s3)
    assert code == 0 and artifact["marker"]["state"] == "imported"
    assert [n["platform"] for n in artifact["logins"]] == list(LOGIN_PLATFORMS)
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    assert "run `uv run social-scraper login x`" in out


def test_main_import_nothing_pending_exits_1(tmp_path):
    code, artifact = run_main(tmp_path, ["import", "--host", "pc-b"], FakeCompose(), FakeS3())
    assert code == 1 and "nothing to import" in artifact["detail"]


def test_main_writes_a_running_account_first(tmp_path):
    artifact = tmp_path / "logs" / "handoff.json"
    seen = {}

    def store():
        seen.update(json.loads(artifact.read_text(encoding="utf-8")))
        return handoff.Store(client=FakeS3(), bucket="bucket")

    deps = make_deps(tmp_path, FakeCompose(), FakeS3())
    deps.store_factory = store
    handoff.main(["pending", "--host", "pc-b"], deps, artifact)
    assert seen["ok"] is False and seen["exit_code"] is None
    assert "cut short" in seen["detail"]
    assert json.loads(artifact.read_text(encoding="utf-8"))["ok"] is True


def test_main_unexpected_exception_leaves_the_artifact(tmp_path, capsys):
    def boom():
        raise OSError("disk")

    deps = make_deps(tmp_path, FakeCompose(), FakeS3())
    deps.store_factory = boom
    artifact = tmp_path / "a.json"

    with pytest.raises(OSError, match="disk"):  # a bug keeps its traceback: exit 1
        handoff.main(["import", "--host", "pc-b"], deps, artifact)
    written = json.loads(artifact.read_text(encoding="utf-8"))
    assert (written["exit_code"], written["ok"], written["detail"]) == (1, False, "OSError: disk")

    with pytest.raises(SystemExit) as exit_info:  # but `pending` stays "cannot tell"
        handoff.main(["pending", "--host", "pc-b"], deps, artifact)
    assert exit_info.value.code == 2
    assert json.loads(artifact.read_text(encoding="utf-8"))["exit_code"] == 2
    assert capsys.readouterr().out == ""


def test_run_reports_a_handoff_error_as_exit_1(tmp_path):
    deps = make_deps(tmp_path, FakeCompose(), FakeS3())
    args = handoff.parse_args(["export", "--from", "A", "--to", "a"])
    code, payload, line = handoff.run(args, deps)
    assert code == 1 and "same host" in payload["detail"] and "FAILED" in line


def test_write_artifact_creates_the_directory(tmp_path):
    path = tmp_path / "logs" / "handoff.json"
    handoff.write_artifact(path, {"ok": True})
    assert json.loads(path.read_text(encoding="utf-8")) == {"ok": True}
