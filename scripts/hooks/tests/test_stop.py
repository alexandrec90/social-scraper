"""Unit tests for the portable Stop hook.

**This file is vendored into every consuming project.** Every value that varies per
project — the control-env prefix, `app/`, the DB credentials, whether a frontend
exists — must come from `hook.CFG` (which the hook itself reads from that project's
`.devkit.toml`), never from a literal. Hard-coding carameli's values here
made the vendored suite fail in any repo shaped differently, which is what the
config seam exists to prevent.
"""

import io
import json
import sys
from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest
from conftest import load_module

hook = load_module("scripts/hooks/stop.py")

# The shape of the project this suite is running inside. Read once so the intent of
# each assertion below stays visible.
CFG = hook.CFG
APP_FILE = f"{CFG.app_dir}main.py"

# `run_db_tests` returns early when the project declares no DB tier, so the tests
# that assert it *does* work have nothing to observe there. Skipping is right rather
# than asserting the early return: that path is already covered by
# `test_run_db_tests_skips_when_down_and_not_opted_in`.
requires_db = pytest.mark.skipif(not CFG.db.enabled, reason="project has no DB test tier")


class _FakeStdin:
    """Minimal stdin stand-in exposing .buffer/.isatty()/.read() for _read_stdin."""

    def __init__(self, data: bytes, tty: bool = False):
        self.buffer = io.BytesIO(data)
        self._tty = tty

    def isatty(self):
        return self._tty

    def read(self):
        return self.buffer.read().decode("utf-8", "surrogateescape")


def test_skin_changed_detects_porcelain_lines():
    assert hook.skin_changed(" M frontend/src/skins/carameli/Tile.tsx\n") is True
    assert hook.skin_changed("") is False
    assert hook.skin_changed("\n  \n") is False


def test_read_stdin_decodes_utf8_payload(monkeypatch):
    payload = '{"transcript_path": "/x/sesión.jsonl"}'
    monkeypatch.setattr(sys, "stdin", _FakeStdin(payload.encode("utf-8")))
    assert hook._read_stdin() == payload


def test_read_stdin_survives_undecodable_byte(monkeypatch):
    # A lone 0x9d byte is undefined in cp1252 and invalid UTF-8. The reader must
    # not crash on it, and the byte must round-trip back out unchanged. Regression
    # for the stop-hook UnicodeEncodeError: 'charmap' codec can't encode '\udc9d'.
    raw = b'{"transcript_path": "/x/a\x9d.jsonl"}'
    monkeypatch.setattr(sys, "stdin", _FakeStdin(raw))
    result = hook._read_stdin()
    assert result.encode("utf-8", "surrogateescape") == raw


def test_read_stdin_empty_for_tty(monkeypatch):
    monkeypatch.setattr(sys, "stdin", _FakeStdin(b'{"x": 1}', tty=True))
    assert hook._read_stdin() == ""


# --- pre-stop verification --------------------------------------------------


def test_stop_hook_active_true_only_when_flagged():
    assert hook.stop_hook_active('{"stop_hook_active": true}') is True
    assert hook.stop_hook_active('{"stop_hook_active": false}') is False
    assert hook.stop_hook_active('{"cwd": "/repo"}') is False
    assert hook.stop_hook_active("not json") is False
    assert hook.stop_hook_active("[1, 2]") is False


def test_verify_enabled_opt_out():
    assert hook.verify_enabled({}) is True
    assert hook.verify_enabled({hook.SKIP_VERIFY_ENV: "0"}) is True
    assert hook.verify_enabled({hook.SKIP_VERIFY_ENV: "1"}) is False


def test_changed_paths_parses_status_and_renames():
    porcelain = " M app/main.py\n?? scripts/new.py\nR  app/old.py -> app/renamed.py\n\n"
    assert hook.changed_paths(porcelain) == [
        "app/main.py",
        "scripts/new.py",
        "app/renamed.py",
    ]


def test_changed_paths_empty():
    assert hook.changed_paths("") == []


# --- the diff is the branch, not just the working tree ------------------------
# Gating on `git status` alone made the gate inert the moment anything was committed,
# and `/ship` commits: the strongest verification ran before a commit and none after,
# which is backwards -- the committed branch is exactly what CI will see.


def test_committed_paths_parses_diff_names():
    assert hook.committed_paths("app/a.py\nscripts/b.py\n\n") == ["app/a.py", "scripts/b.py"]
    assert hook.committed_paths("") == []


def test_all_changed_unions_working_tree_and_commits():
    porcelain = " M app/dirty.py\n"
    diff = "app/committed.py\nscripts/also.py\n"
    assert hook.all_changed(porcelain, diff) == [
        "app/dirty.py",
        "app/committed.py",
        "scripts/also.py",
    ]


def test_all_changed_deduplicates_a_committed_then_modified_file():
    assert hook.all_changed(" M app/x.py\n", "app/x.py\n") == ["app/x.py"]


def test_all_changed_sees_a_clean_tree_with_commits():
    """The regression: a clean tree after a commit must still select checks.

    Before this, `changed_paths("")` was `[]`, `select_checks([])` was `[]`, and the
    stop passed having run nothing.
    """
    paths = hook.all_changed("", f"{CFG.app_dir}main.py\n")
    assert paths, "a committed change must be verified even with a clean working tree"
    # `in`, not equality: `app_dir` is `scripts/` in some projects (devkit's own), which
    # legitimately adds the script-tests tier. This file is vendored -- it may not assume
    # one repo's shape.
    assert hook.CHECK_LINT in hook.select_checks(paths)


def test_all_changed_empty_when_nothing_changed_anywhere():
    assert hook.all_changed("", "") == []


# --- bounded retry rounds ----------------------------------------------------
# `stop_hook_active` is a boolean, so honouring it alone meant verification ran on the
# first stop only: the gate could report a failure but never confirm the fix.


def test_blocked_rounds_starts_at_one_on_a_fresh_stop():
    assert hook.blocked_rounds(0, chain_active=False) == 1
    # A counter left behind by an abandoned chain must not spend this one's budget.
    assert hook.blocked_rounds(7, chain_active=False) == 1


def test_blocked_rounds_accumulates_within_a_chain():
    assert hook.blocked_rounds(1, chain_active=True) == 2
    assert hook.blocked_rounds(2, chain_active=True) == 3


def test_should_block_until_the_budget_is_spent():
    assert hook.should_block(1, max_rounds=3) is True
    assert hook.should_block(2, max_rounds=3) is True
    assert hook.should_block(3, max_rounds=3) is False
    assert hook.should_block(4, max_rounds=3) is False


def test_max_verify_rounds_allows_at_least_one_recheck():
    """A ceiling of 1 would restore the original one-shot behaviour."""
    assert hook.MAX_VERIFY_ROUNDS >= 2


def test_rounds_marker_round_trips(tmp_path, monkeypatch):
    marker = tmp_path / "agent-stop-rounds"
    monkeypatch.setattr(hook.task_branch, "worktree_file", lambda git, name: marker)
    assert hook.read_rounds(tmp_path) == 0
    hook.write_rounds(2, tmp_path)
    assert hook.read_rounds(tmp_path) == 2
    # Zero clears rather than writing "0", so nothing lingers after a green stop.
    hook.write_rounds(0, tmp_path)
    assert not marker.exists()
    assert hook.read_rounds(tmp_path) == 0


def test_read_rounds_survives_a_corrupt_marker(tmp_path, monkeypatch):
    marker = tmp_path / "agent-stop-rounds"
    marker.write_text("not a number", encoding="utf-8")
    monkeypatch.setattr(hook.task_branch, "worktree_file", lambda git, name: marker)
    assert hook.read_rounds(tmp_path) == 0


def test_rounds_helpers_no_op_without_a_git_path(monkeypatch):
    monkeypatch.setattr(hook.task_branch, "worktree_file", lambda git, name: None)
    assert hook.read_rounds() == 0
    hook.write_rounds(3)  # must not raise


def test_path_predicates():
    assert hook._is_py("app/x.py") and hook._is_py("app/y.pyi")
    assert not hook._is_py("README.md")
    # Built from `CFG.frontend.src`, per this module's docstring: the literals these
    # two lines used to carry were carameli's layout, and any project that keeps its
    # frontend anywhere else -- roguelike is its own npm root, `src/` -- failed a test
    # about a predicate that was behaving correctly. The negative case is what it
    # always was, a file in the npm project but outside the gated source tree.
    assert hook._is_frontend(f"{CFG.frontend.src}App.tsx")
    assert not hook._is_frontend(str(PurePosixPath(CFG.frontend.src).parent / "vite.config.ts"))
    assert hook._is_reqs("requirements.txt")
    assert hook._is_reqs("requirements-dev.in")
    assert not hook._is_reqs("app/requirements_notes.md")
    assert hook._is_script("scripts/hooks/stop.py")
    assert not hook._is_script("scripts/notes.md")


def test_host_test_targets_app_change_runs_whole_unit_suite():
    # An application-code change can break tests anywhere -> whole unit suite.
    unit = [CFG.unit_tests]
    assert hook.host_test_targets([APP_FILE]) == unit
    assert hook.host_test_targets([APP_FILE, f"{CFG.unit_tests}/t.py"]) == unit


def test_host_test_targets_tests_only_runs_changed_files():
    assert hook.host_test_targets(["tests/unit/test_a.py", "tests/integration/test_b.py"]) == [
        "tests/integration/test_b.py",
        "tests/unit/test_a.py",
    ]


def test_host_test_targets_no_app_or_tests_is_empty():
    # The paths must fall outside BOTH app_dir and tests_dir, and cannot be hardcoded:
    # this file is vendored, and the literal `scripts/hooks/stop.py` this test used to
    # pass *is* application code in a project whose `app_dir` is `scripts/` — devkit's
    # is, so the assertion inverted there. The precondition is asserted rather than
    # assumed, so a manifest that ever makes these paths meaningful fails loudly.
    unrelated = ["docs/notes.md", "third_party/x.py"]
    for path in unrelated:
        assert not path.startswith((CFG.app_dir, CFG.tests_dir)), f"{path} is not neutral here"
    assert hook.host_test_targets(unrelated) == []


def test_select_checks_empty_diff_runs_nothing():
    assert hook.select_checks([]) == []


def test_select_checks_never_includes_db_tier():
    # The DB tier is handled by run_db_tests, not select_checks.
    assert hook.select_checks(["app/main.py"]) == [hook.CHECK_LINT]
    assert hook.CHECK_TESTS not in hook.select_checks(["tests/unit/t.py"])


def test_select_checks_scripts_run_host_tests():
    # A scripts/ change is verified on the host with no Docker.
    assert hook.select_checks(["scripts/hooks/stop.py"]) == [
        hook.CHECK_LINT,
        hook.CHECK_SCRIPT_TESTS,
    ]


@pytest.mark.parametrize(
    "changed",
    [
        "requirements.txt",
        "requirements.in",
        "requirements-dev.txt",
        # uv/poetry projects express the same event as one lockfile. Before these,
        # _REQ_RE matched only `requirements*`, so the tier never fired for them --
        # an inert check looks exactly like a passing one.
        "uv.lock",
        "poetry.lock",
        "subdir/uv.lock",
    ],
)
def test_select_checks_dependency_change_adds_lock_markers(changed):
    checks = hook.select_checks([changed])
    assert hook.CHECK_LOCKS in checks and hook.CHECK_TESTS not in checks


@pytest.mark.parametrize(
    "changed",
    [
        "my_uv.lock",  # must anchor on a path boundary, not any substring
        "docs/uv.lock.md",  # ...and on end-of-name
        "notes/requirements.md",
        "uv.locked",
    ],
)
def test_select_checks_ignores_lockfile_lookalikes(changed):
    assert hook.CHECK_LOCKS not in hook.select_checks([changed])


@pytest.mark.skipif(not CFG.frontend.enabled, reason="project has no frontend tier")
def test_select_checks_frontend_adds_vitest():
    assert hook.select_checks([f"{CFG.frontend.src}App.tsx"]) == [
        hook.CHECK_LINT,
        hook.CHECK_FRONTEND,
    ]


def test_select_checks_non_relevant_change_runs_lint_only():
    # A docs edit still runs lint (lint-all --changed self-scopes to a no-op),
    # but never script-tests / locks / frontend.
    assert hook.select_checks(["README.md"]) == [hook.CHECK_LINT]


def test_parse_host_port_variants():
    assert hook._parse_host_port("0.0.0.0:5432\n") == "5432"
    assert hook._parse_host_port("[::]:5432") == "5432"
    assert hook._parse_host_port("127.0.0.1:5433") == "5433"
    # Multi-line (IPv4 + IPv6): last non-empty line wins, still a valid port.
    assert hook._parse_host_port("0.0.0.0:5432\n[::]:5432\n") == "5432"
    assert hook._parse_host_port("") is None
    assert hook._parse_host_port("garbage") is None


def test_run_checks_skips_missing_tool(monkeypatch):
    monkeypatch.setattr(hook, "_command_for", lambda name, root=None: None)
    assert hook.run_checks([hook.CHECK_FRONTEND]) == []


def test_run_checks_collects_failures(monkeypatch):
    import subprocess as sp

    monkeypatch.setattr(
        hook, "_command_for", lambda name, root=None: (["true"], hook.REPO_ROOT, None)
    )
    monkeypatch.setattr(
        hook.subprocess,
        "run",
        lambda *a, **k: sp.CompletedProcess([], 1, "boom\n", "bad line\n"),
    )
    failures = hook.run_checks([hook.CHECK_LINT])
    assert len(failures) == 1
    assert failures[0][0] == hook.CHECK_LINT
    assert "bad line" in failures[0][2]


def test_run_checks_oserror_is_skip_not_failure(monkeypatch):
    def boom(*a, **k):
        raise OSError("no such tool")

    monkeypatch.setattr(
        hook, "_command_for", lambda name, root=None: (["nope"], hook.REPO_ROOT, None)
    )
    monkeypatch.setattr(hook.subprocess, "run", boom)
    assert hook.run_checks([hook.CHECK_LINT]) == []


class TestOutputIsDecodedNotGuessed:
    """A tool's bytes must not be able to kill the hook that is reporting on them.

    `text=True` alone decodes through the platform's locale codec -- cp1252 here,
    strict UTF-8 on a CI runner -- and both raise on bytes real tools emit. The raise
    happens inside subprocess's reader thread, where the `try` around the call cannot
    see it, and `subprocess.run` hands back `stdout=None, stderr=None`. The failure a
    user sees is then a `TypeError` in `run_checks`, several hundred lines from the
    cause:

        tail = (result.stdout + result.stderr).strip().splitlines()[-15:]
        TypeError: unsupported operand type(s) for +: 'NoneType' and 'NoneType'

    Revert either half and one of these fails: the codec test crashes the way the
    reported session did, and the `None` test restores the `TypeError` itself.
    """

    def test_a_check_whose_output_is_not_utf8_is_reported_not_crashed(self, monkeypatch):
        # A lone 0x9d: undefined in cp1252, invalid UTF-8, and the exact byte that ended
        # a session from a lint tail. The check must still be reported as a failure.
        argv = [
            sys.executable,
            "-c",
            "import sys; sys.stderr.buffer.write(b'ruff: \\x9d line too long\\n'); sys.exit(1)",
        ]
        monkeypatch.setattr(
            hook, "_command_for", lambda name, root=None: (argv, hook.REPO_ROOT, None)
        )
        failures = hook.run_checks([hook.CHECK_LINT])
        assert len(failures) == 1
        assert "line too long" in failures[0][2]

    def test_run_checks_survives_streams_that_were_never_captured(self, monkeypatch):
        import subprocess as sp

        monkeypatch.setattr(
            hook, "_command_for", lambda name, root=None: (["true"], hook.REPO_ROOT, None)
        )
        monkeypatch.setattr(
            hook.subprocess, "run", lambda *a, **k: sp.CompletedProcess([], 1, None, None)
        )
        failures = hook.run_checks([hook.CHECK_LINT])
        assert failures == [(hook.CHECK_LINT, None, "")]

    def test_combined_output_joins_what_it_has(self):
        import subprocess as sp

        assert hook.combined_output(sp.CompletedProcess([], 1, "out\n", "err\n")) == "out\nerr\n"
        assert hook.combined_output(sp.CompletedProcess([], 1, None, "err\n")) == "err\n"
        assert hook.combined_output(sp.CompletedProcess([], 1, "out\n", None)) == "out\n"
        assert hook.combined_output(sp.CompletedProcess([], 1, None, None)) == ""


class TestTheGateIsBounded:
    """A Stop hook that outruns its harness's ceiling is killed, and a killed hook
    writes no artifact and prints nothing -- the session ends on "stop hook failed"
    naming no tier. Reverting any of this restores an uncapped `subprocess.run` in the
    Stop path, and these four fail: the first two hang the suite's stub forever, the
    third stops asserting that later checks are cut off, the fourth loses the budget.
    """

    def test_a_check_that_outruns_the_budget_is_reported_not_skipped(self, monkeypatch):
        """A skip would defer to CI, which is right for a tool this machine lacks and
        wrong here: nothing is known about the check, so nothing may be assumed."""
        import subprocess as sp

        monkeypatch.setattr(
            hook, "_command_for", lambda name, root=None: (["slow"], hook.REPO_ROOT, None)
        )

        def _hang(*a, **k):
            raise sp.TimeoutExpired(["slow"], k.get("timeout") or 1)

        monkeypatch.setattr(hook.subprocess, "run", _hang)
        failures = hook.run_checks([hook.CHECK_LINT], deadline=hook.verify_deadline())
        assert len(failures) == 1
        assert failures[0][0] == hook.CHECK_LINT
        assert "slow" in failures[0][2], "the tail must name the command to re-run"

    def test_the_host_test_tier_is_bounded_too(self, monkeypatch, tmp_path):
        """The longest tier, and the one a hung fixture or an open port stalls."""
        import subprocess as sp

        monkeypatch.setattr(hook, "test_runner_argv", lambda t, r: (["pytest"], "logs/t.log"))

        def _hang(*a, **k):
            raise sp.TimeoutExpired(["pytest"], k.get("timeout") or 1)

        monkeypatch.setattr(hook.subprocess, "run", _hang)
        failures = hook._pytest_failures(
            ["tests/test_x.py"], tmp_path, deadline=hook.verify_deadline()
        )
        assert [f[0] for f in failures] == [hook.CHECK_TESTS]

    def test_an_exhausted_budget_stops_the_remaining_checks_on_arrival(self, monkeypatch):
        """The budget is shared, so the tier that spends it must not leave the ones
        after it free to spend another one each."""
        monkeypatch.setattr(
            hook, "_command_for", lambda name, root=None: ([name], hook.REPO_ROOT, None)
        )
        monkeypatch.setattr(
            hook.subprocess, "run", lambda *a, **k: pytest.fail("nothing may run past the budget")
        )
        failures = hook.run_checks(
            [hook.CHECK_LINT, hook.CHECK_SCRIPT_TESTS], deadline=hook.verify_deadline(budget=-1)
        )
        assert [f[0] for f in failures] == [hook.CHECK_LINT, hook.CHECK_SCRIPT_TESTS]

    def test_both_tiers_share_one_deadline(self, monkeypatch, sandboxed_verify):
        """Two budgets would let the gate spend twice the ceiling and still be killed."""
        seen: dict[str, float | None] = {}

        def _checks(names, root=None, deadline=None):
            seen["checks"] = deadline
            return []

        def _tests(paths, env, root=None, deadline=None):
            seen["tests"] = deadline
            return []

        monkeypatch.setattr(hook, "run_checks", _checks)
        monkeypatch.setattr(hook, "run_host_tests", _tests)
        assert hook.verify("{}", {}) == 0
        assert seen["checks"] is not None
        assert seen["checks"] == seen["tests"]

    def test_the_timeout_tail_says_what_is_unknown_and_how_to_find_out(self):
        tail = hook.timeout_tail(["python", "-m", "pytest", "tests/test_x.py"])
        assert "unknown" in tail
        assert "python -m pytest tests/test_x.py" in tail

    def test_a_stopped_check_is_recognised_as_unfinished(self):
        """`unfinished` reads the tail's prefix, so `timeout_tail` and `UNFINISHED_MARK`
        have to stay spelled the same. A drift between them is silent: every stopped
        check would read as a finished failure again, which is the defect this fixes."""
        stopped = ("tests", None, hook.timeout_tail(["pytest"]))
        red = ("lint", None, "E999 SyntaxError")
        assert hook.unfinished([stopped, red]) == ["tests"]

    def test_a_round_of_nothing_but_stopped_checks_does_not_block(
        self, monkeypatch, sandboxed_verify
    ):
        """The budget running out says nothing about the branch, and blocking on it just
        buys the same non-answer one turn later — twice in a row on devkit#237, at the
        tail of a session, with the suite already green by hand in between."""
        monkeypatch.setattr(
            hook,
            "run_checks",
            lambda names, root=None, deadline=None: [
                ("tests", "logs/test-failures.log", hook.timeout_tail(["pytest"]))
            ],
        )
        monkeypatch.setattr(hook, "run_host_tests", lambda paths, env, root=None, deadline=None: [])
        assert hook.verify("{}", {}) == 0
        # And it costs no round: the next *real* failure gets the full two chances.
        assert sandboxed_verify["value"] == 0

    def test_a_finished_failure_still_blocks_when_a_stopped_check_rides_along(
        self, monkeypatch, sandboxed_verify
    ):
        """The stand-down is for a round that learned nothing, not for one that learned
        something and ran out of time afterwards."""
        monkeypatch.setattr(
            hook,
            "run_checks",
            lambda names, root=None, deadline=None: [
                ("lint", None, "E999 SyntaxError"),
                ("tests", None, hook.timeout_tail(["pytest"])),
            ],
        )
        monkeypatch.setattr(hook, "run_host_tests", lambda paths, env, root=None, deadline=None: [])
        assert hook.verify("{}", {}) == 2

    def test_the_status_line_calls_a_stopped_check_unknown_not_failed(self, capsys):
        """`failed: tests` under an artifact reading "its result is unknown" is the gate
        contradicting its own evidence, and the agent has to open the file to find out
        which half to believe."""
        hook._print_verify_failures(
            [
                ("lint", None, "E999 SyntaxError"),
                ("tests", None, hook.timeout_tail(["pytest"])),
            ],
            blocking=True,
        )
        err = capsys.readouterr().err
        assert "failed: lint" in err
        assert "unknown" in err and "tests" in err
        assert "failed: lint, tests" not in err

    def test_the_budget_leaves_room_to_report(self):
        """Set at or above a harness ceiling and the kill happens first, which is the
        failure this exists to remove rather than relocate."""
        assert hook.VERIFY_BUDGET_SECONDS < 600
        assert hook.TYPECHECK_TIMEOUT_SECONDS < hook.VERIFY_BUDGET_SECONDS

    def test_no_deadline_means_no_cap(self, monkeypatch):
        """`None` is the default so a caller that never opted in is unchanged -- and so
        the suite's own `subprocess.run` stubs, which take no `timeout`, still work."""
        import subprocess as sp

        seen: dict = {}
        monkeypatch.setattr(
            hook, "_command_for", lambda name, root=None: (["x"], hook.REPO_ROOT, None)
        )
        monkeypatch.setattr(
            hook.subprocess,
            "run",
            lambda *a, **k: seen.update(k) or sp.CompletedProcess([], 0, "", ""),
        )
        assert hook.run_checks([hook.CHECK_LINT]) == []
        assert seen["timeout"] is None


def test_command_for_returns_none_when_a_repo_script_is_absent(tmp_path, monkeypatch):
    """Absence is decided here, not left to the OSError handler in `run_checks`.

    Both skip the check, so this changes no behaviour — it changes what the skip
    *means*. Resolved to None, "this project has no such tier" is a readable state
    that `test_repo_contract.py` can then hold the project to; reached as an OSError
    from spawning a missing file, it is indistinguishable from an installed script
    that crashed on startup.
    """
    missing = tmp_path / "gone.py"
    monkeypatch.setattr(hook, "LINT_ALL", missing)
    monkeypatch.setattr(hook, "CHECK_LOCK_MARKERS", missing)
    assert hook._command_for(hook.CHECK_LINT) is None
    assert hook._command_for(hook.CHECK_LOCKS) is None
    # The tier that needs no project-owned script is unaffected.
    assert hook._command_for(hook.CHECK_SCRIPT_TESTS) is not None


def test_the_frontend_tier_skips_a_tree_that_was_never_provisioned(tmp_path, monkeypatch):
    """A missing `node_modules` is a missing toolchain, and this tier already skips those.

    A worktree checks out tracked files only, so a freshly cut box has no
    `node_modules`; `npm run test:run` there exits non-zero with `'vitest' is not
    recognized`, and that was reported as `failed: frontend -- would fail CI`. It fired
    twice on one box while the same suite was green (1393 passed) in the checkout. The
    verdict sends the session to audit a diff that is fine, and `run_checks`'s own
    contract already says a tooling gap must never block the agent -- `shutil.which` was
    just asking about the wrong half of the toolchain.
    """
    monkeypatch.setattr(hook.shutil, "which", lambda _name: "npm")
    frontend = tmp_path / hook.CFG.frontend.dir
    # `exist_ok` because `dir` is legitimately "." in a repo that *is* its frontend, and
    # `tmp_path / "."` is `tmp_path`, which pytest has already created.
    frontend.mkdir(parents=True, exist_ok=True)

    assert hook._command_for(hook.CHECK_FRONTEND, tmp_path) is None

    (frontend / "node_modules").mkdir()
    spec = hook._command_for(hook.CHECK_FRONTEND, tmp_path)
    assert spec is not None
    assert spec[1] == frontend


def test_the_frontend_tier_holds_for_a_repo_that_is_its_own_npm_root(tmp_path, monkeypatch):
    """A layout devkit does not have, asserted explicitly rather than by luck.

    Every other test here reads `hook.CFG`, which in devkit is the defaults -- so a
    literal that matches the defaults passes the whole vendored suite while failing in
    the consumer it was vendored to. That is not hypothetical: two tests in this file
    carried `frontend/src/...` literals and were found by roguelike, which keeps its
    `package.json`, `vite.config.ts` and `src/` at the repository root, because the
    repository *is* the frontend. `dir = "."` is the case with no subdirectory at all,
    which is why it is the one pinned here.

    Constructing the layout rather than reading the project's is what makes this a
    guarantee: it holds in every consumer, including the ones shaped like the defaults.
    """
    layout = replace(hook.CFG.frontend, enabled=True, dir=".", src="src/", skin="src/")
    monkeypatch.setattr(hook, "CFG", replace(hook.CFG, frontend=layout))
    monkeypatch.setattr(hook.shutil, "which", lambda _name: "npm")

    assert hook._is_frontend("src/game/field.ts")
    assert not hook._is_frontend("vite.config.ts")  # beside the source tree, not in it

    (tmp_path / "node_modules").mkdir()
    spec = hook._command_for(hook.CHECK_FRONTEND, tmp_path)
    assert spec is not None
    # `base / "."` is `base`: the npm project is the repository root itself.
    assert spec[1] == tmp_path


@pytest.fixture
def sandboxed_verify(monkeypatch, tmp_path):
    """`verify` with git, the artifact and the round marker all confined to tmp_path.

    Returns the mutable round-counter store so a test can set the starting round and
    read back what `verify` persisted, without touching the real repo.
    """
    rounds = {"value": 0}
    monkeypatch.setattr(hook, "_git_status_porcelain", lambda root: " M app/main.py\n")
    monkeypatch.setattr(hook, "_git_branch_diff", lambda root: "")
    monkeypatch.setattr(hook, "write_verify_artifact", lambda failures, repo_root=None: None)
    monkeypatch.setattr(hook, "read_rounds", lambda *a, **k: rounds["value"])
    monkeypatch.setattr(
        hook, "write_rounds", lambda value, *a, **k: rounds.__setitem__("value", value)
    )
    return rounds


def test_verify_skips_when_opted_out(monkeypatch):
    monkeypatch.setattr(
        hook, "run_checks", lambda names, root=None, deadline=None: [("lint", None, "x")]
    )
    assert hook.verify("{}", {hook.SKIP_VERIFY_ENV: "1"}) == 0


def test_verify_returns_two_on_failure(monkeypatch, sandboxed_verify):
    monkeypatch.setattr(hook, "run_host_tests", lambda *a, **k: [])
    monkeypatch.setattr(
        hook,
        "run_checks",
        lambda names, root=None, deadline=None: [("lint", "logs/lint-errors.log", "")],
    )
    assert hook.verify("{}", {}) == 2


def _read_only(tmp_path: Path) -> str:
    """A stop payload whose transcript shows no file-writing tool use."""
    path = tmp_path / "transcript.jsonl"
    record = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read"}]}}
    path.write_text(json.dumps(record) + "\n", encoding="utf-8", newline="\n")
    return json.dumps({"session_id": "s1", "transcript_path": str(path)})


def test_verify_reports_rather_than_blocks_when_the_session_wrote_nothing(
    monkeypatch, sandboxed_verify, tmp_path
):
    """A static checkout is not one session's. Two sessions sharing one had the gate
    demand that the read-only one fix a partial rename the other was mid-way through
    committing -- four times, with the skip variable the only escape."""
    monkeypatch.setattr(hook, "run_host_tests", lambda *a, **k: [])
    monkeypatch.setattr(
        hook, "run_checks", lambda names, root=None, deadline=None: [("lint", None, "not mine")]
    )

    assert hook.verify(_read_only(tmp_path), {}) == 0
    assert sandboxed_verify["value"] == 0, "a foreign failure must not spend this session's budget"


def test_verify_still_blocks_when_the_transcript_cannot_say(monkeypatch, sandboxed_verify):
    """The probe's own cases are in `test_stop_session.py`; this asserts the *wiring*
    still blocks when it answers no."""
    monkeypatch.setattr(hook, "run_host_tests", lambda *a, **k: [])
    monkeypatch.setattr(
        hook, "run_checks", lambda names, root=None, deadline=None: [("lint", None, "x")]
    )
    assert hook.verify("{}", {}) == 2


def test_verify_returns_two_when_db_tests_fail(monkeypatch, sandboxed_verify):
    monkeypatch.setattr(hook, "run_checks", lambda names, root=None, deadline=None: [])
    monkeypatch.setattr(hook, "run_host_tests", lambda *a, **k: [("tests", None, "F app/x")])
    assert hook.verify("{}", {}) == 2


def test_verify_returns_zero_when_clean(monkeypatch, sandboxed_verify):
    monkeypatch.setattr(hook, "run_checks", lambda names, root=None, deadline=None: [])
    monkeypatch.setattr(hook, "run_host_tests", lambda *a, **k: [])
    assert hook.verify("{}", {}) == 0


def test_verify_still_runs_the_checks_on_a_continuation_stop(monkeypatch, sandboxed_verify):
    """The regression this replaced `test_verify_skips_when_loop_active` with.

    Skipping on `stop_hook_active` meant the agent was told what was broken, "fixed" it,
    stopped again, and the second stop ran nothing -- so a wrong fix ended the session
    looking green. The flag now only decides whether the round counter restarts.
    """
    ran = []
    monkeypatch.setattr(
        hook, "run_checks", lambda names, root=None, deadline=None: ran.append(names) or []
    )
    monkeypatch.setattr(hook, "run_host_tests", lambda *a, **k: [])
    assert hook.verify('{"stop_hook_active": true}', {}) == 0
    assert ran, "a continuation stop must still verify -- otherwise a fix is never checked"


def test_verify_blocks_repeatedly_then_stands_down(monkeypatch, sandboxed_verify):
    monkeypatch.setattr(
        hook, "run_checks", lambda names, root=None, deadline=None: [("lint", None, "still broken")]
    )
    monkeypatch.setattr(hook, "run_host_tests", lambda *a, **k: [])

    # First failing stop of a chain blocks and records round 1.
    codes = [hook.verify("{}", {})]
    assert codes == [2]
    assert sandboxed_verify["value"] == 1

    # Continuations keep verifying, and keep blocking while the budget lasts. Bounded
    # well above MAX_VERIFY_ROUNDS so a gate that never terminated fails the test rather
    # than hanging the suite.
    while codes[-1] == 2 and len(codes) <= hook.MAX_VERIFY_ROUNDS + 3:
        codes.append(hook.verify('{"stop_hook_active": true}', {}))

    assert codes[-1] == 0, f"the gate must terminate on its own, got {codes}"
    # The last round reports without blocking, so blocks are one fewer than rounds --
    # and there is at least one re-check, which is the whole point of the change.
    assert codes.count(2) == hook.MAX_VERIFY_ROUNDS - 1, codes
    assert sandboxed_verify["value"] == 0, "standing down must clear the counter"


def test_verify_clears_the_counter_once_green(monkeypatch, sandboxed_verify):
    sandboxed_verify["value"] = 2
    monkeypatch.setattr(hook, "run_checks", lambda names, root=None, deadline=None: [])
    monkeypatch.setattr(hook, "run_host_tests", lambda *a, **k: [])
    assert hook.verify("{}", {}) == 0
    assert sandboxed_verify["value"] == 0, "the next failure must start from a full budget"


def test_verify_verifies_committed_work(monkeypatch, tmp_path):
    """End-to-end on the union: a clean tree with commits must still select checks."""
    seen: list[list[str]] = []
    monkeypatch.setattr(hook, "_git_status_porcelain", lambda root: "")
    monkeypatch.setattr(hook, "_git_branch_diff", lambda root: f"{CFG.app_dir}main.py\n")
    monkeypatch.setattr(hook, "write_verify_artifact", lambda failures, repo_root=None: None)
    monkeypatch.setattr(hook, "read_rounds", lambda *a, **k: 0)
    monkeypatch.setattr(hook, "write_rounds", lambda *a, **k: None)
    monkeypatch.setattr(
        hook, "run_checks", lambda names, root=None, deadline=None: seen.append(names) or []
    )
    monkeypatch.setattr(hook, "run_host_tests", lambda *a, **k: [])
    assert hook.verify("{}", {}) == 0
    assert seen and hook.CHECK_LINT in seen[0], (
        "a committed-only diff must still select checks; before the union it selected none"
    )


# --- the failure artifact -----------------------------------------------------
# `.claude/rules/engineering.md`: failures an agent acts on go in a parseable file, not
# streamed to a terminal that scrolls away. Only the lint tier used to have one.


def test_artifact_body_holds_every_tier_and_its_output():
    body = hook.artifact_body(
        [
            (hook.CHECK_SCRIPT_TESTS, None, "E   assert 1 == 2"),
            (hook.CHECK_TESTS, hook.TEST_ARTIFACT, "F tests/test_x.py"),
        ]
    )
    assert hook.CHECK_SCRIPT_TESTS in body and "assert 1 == 2" in body
    assert hook.CHECK_TESTS in body and "F tests/test_x.py" in body
    assert hook.TEST_ARTIFACT in body, "a tier with its own artifact should still be named"
    assert "scripts/hooks/stop.py" in body, "the artifact must say what wrote it"


def test_artifact_body_is_empty_when_nothing_failed():
    assert hook.artifact_body([]) == ""


def test_write_verify_artifact_clears_on_success(tmp_path):
    hook.write_verify_artifact([(hook.CHECK_LINT, None, "boom")], tmp_path)
    target = tmp_path / hook.VERIFY_ARTIFACT
    assert "boom" in target.read_text(encoding="utf-8")
    # A stale artifact must never outlive the failure it describes.
    hook.write_verify_artifact([], tmp_path)
    assert target.read_text(encoding="utf-8") == ""


def test_write_verify_artifact_survives_an_unwritable_target(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("read-only")

    monkeypatch.setattr(hook.Path, "write_text", boom)
    hook.write_verify_artifact([(hook.CHECK_LINT, None, "x")], tmp_path)  # must not raise


def test_failure_report_names_the_artifact_not_the_output(capsys):
    hook._print_verify_failures([(hook.CHECK_SCRIPT_TESTS, None, "E   assert 1 == 2")])
    err = capsys.readouterr().err
    assert hook.VERIFY_ARTIFACT in err
    assert hook.CHECK_SCRIPT_TESTS in err
    assert "assert 1 == 2" not in err, "detail belongs in the artifact, not the terminal"


def test_failure_report_says_when_it_has_stopped_blocking(capsys):
    hook._print_verify_failures([(hook.CHECK_LINT, None, "x")], blocking=False)
    err = capsys.readouterr().err
    assert str(hook.MAX_VERIFY_ROUNDS) in err
    assert "Not blocking again" in err


# --- the application tier runs through the project's own runner ---------------


def test_test_runner_argv_prefers_run_tests_and_its_artifact(tmp_path, monkeypatch):
    runner = tmp_path / "run-tests.py"
    runner.write_text("", encoding="utf-8")
    monkeypatch.setattr(hook, "RUN_TESTS", runner)
    argv, artifact = hook.test_runner_argv(["tests/test_x.py"])
    assert str(runner) in argv
    assert argv[-1] == "tests/test_x.py"
    assert artifact == hook.TEST_ARTIFACT


def test_test_runner_argv_falls_back_to_pytest_without_a_runner(tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "RUN_TESTS", tmp_path / "absent.py")
    argv, artifact = hook.test_runner_argv(["tests/test_x.py"])
    assert argv[1:3] == ["-m", "pytest"]
    assert artifact is None


def test_test_runner_runs_under_the_verify_interpreter(tmp_path, monkeypatch):
    py = tmp_path / ".venv/Scripts/python.exe"
    py.parent.mkdir(parents=True)
    py.write_text("")
    monkeypatch.setattr(hook, "REPO_ROOT", tmp_path)
    hook._can_verify.cache_clear()
    try:
        argv, _artifact = hook.test_runner_argv(["tests/test_x.py"])
    finally:
        hook._can_verify.cache_clear()
    assert argv[0] == str(py), "the app tier must not run under the python3 shim"


# --- Tier 2b: host pytest against db+redis (+ opt-in autostart) -------------


def test_autostart_enabled_opt_in():
    assert hook.autostart_enabled({}) is False
    assert hook.autostart_enabled({hook.AUTOSTART_ENV: "0"}) is False
    assert hook.autostart_enabled({hook.AUTOSTART_ENV: "1"}) is True


def test_services_to_stop_only_newly_started():
    assert hook.services_to_stop({"redis"}, {"redis", "db"}) == ["db"]
    assert hook.services_to_stop({"db", "redis"}, {"db", "redis"}) == []


def test_db_redis_running_needs_every_configured_service(monkeypatch):
    configured = set(CFG.db.services)
    monkeypatch.setattr(
        hook, "_compose_running_services", lambda *a, **k: configured | {"unrelated"}
    )
    assert hook.db_redis_running() is True
    # Drop one required service: the tier must not run against a half-up stack.
    partial = configured - {CFG.db.db_service}
    monkeypatch.setattr(hook, "_compose_running_services", lambda *a, **k: partial)
    assert hook.db_redis_running() is False


def test_host_db_env_builds_urls_from_ports(monkeypatch):
    db = CFG.db
    monkeypatch.setattr(
        hook, "_compose_host_port", lambda svc, port, *a: "5599" if svc == db.db_service else "6699"
    )
    env = hook.host_db_env()
    expected = f"{db.url_scheme}://{db.user}:{db.password}@localhost:5599/{db.name}"
    # Every configured alias gets the same URL — carameli exposes two.
    for name in db.url_env:
        assert env[name] == expected
    if db.redis_service in db.services:
        assert env[db.redis_env] == "redis://localhost:6699"


def test_host_db_env_none_when_port_unresolved(monkeypatch):
    monkeypatch.setattr(hook, "_compose_host_port", lambda svc, port, *a: None)
    assert hook.host_db_env() is None


def test_run_db_tests_no_targets_never_touches_docker(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("should not run")

    monkeypatch.setattr(hook, "db_redis_running", boom)
    assert hook.run_db_tests(["scripts/x.py", "README.md"], {}) == []


@requires_db
def test_run_db_tests_runs_when_db_up_no_autostart(monkeypatch):
    import subprocess as sp

    monkeypatch.setattr(hook, "db_redis_running", lambda *a, **k: True)
    monkeypatch.setattr(hook, "host_db_env", lambda *a, **k: {"DATABASE_URL": "x"})
    up = {"called": False}
    monkeypatch.setattr(
        hook, "_compose_up_db_redis", lambda *a, **k: up.__setitem__("called", True)
    )
    stopped = {}
    monkeypatch.setattr(hook, "_compose_stop", lambda svc, *a, **k: stopped.setdefault("svc", svc))
    monkeypatch.setattr(hook.subprocess, "run", lambda *a, **k: sp.CompletedProcess([], 0))

    assert hook.run_db_tests([APP_FILE], {}) == []
    assert up["called"] is False  # already up -> no autostart
    assert stopped["svc"] == []  # started nothing -> stop nothing


@requires_db
def test_run_db_tests_reports_pytest_failure(monkeypatch):
    import subprocess as sp

    monkeypatch.setattr(hook, "db_redis_running", lambda *a, **k: True)
    monkeypatch.setattr(hook, "host_db_env", lambda *a, **k: {"DATABASE_URL": "x"})
    monkeypatch.setattr(hook, "_compose_stop", lambda *a, **k: None)
    monkeypatch.setattr(
        hook.subprocess, "run", lambda *a, **k: sp.CompletedProcess([], 1, "F tests/unit/x\n", "")
    )
    failures = hook.run_db_tests(["tests/unit/x.py"], {})
    assert failures and failures[0][0] == hook.CHECK_TESTS
    assert "tests/unit/x" in failures[0][2]


def test_run_db_tests_skips_when_down_and_not_opted_in(monkeypatch):
    monkeypatch.setattr(hook, "db_redis_running", lambda *a, **k: False)
    up = {"called": False}
    monkeypatch.setattr(
        hook, "_compose_up_db_redis", lambda *a, **k: up.__setitem__("called", True)
    )
    assert hook.run_db_tests(["app/main.py"], {}) == []
    assert up["called"] is False  # no opt-in -> never autostarts


@requires_db
def test_run_db_tests_autostarts_and_stops_only_started(monkeypatch):
    import subprocess as sp

    # The invariant: the hook leaves the stack as it found it. Whatever was already
    # running stays running; only what this run started is stopped again.
    configured = set(CFG.db.services)
    already_up = configured - {CFG.db.db_service}
    monkeypatch.setattr(hook, "db_redis_running", lambda *a, **k: False)
    running = iter([already_up, configured])  # before up, after up
    monkeypatch.setattr(hook, "_compose_running_services", lambda *a, **k: next(running))
    monkeypatch.setattr(hook, "_compose_up_db_redis", lambda *a, **k: True)
    monkeypatch.setattr(hook, "host_db_env", lambda *a, **k: {"DATABASE_URL": "x"})
    monkeypatch.setattr(hook.subprocess, "run", lambda *a, **k: sp.CompletedProcess([], 0))
    stopped = {}
    monkeypatch.setattr(hook, "_compose_stop", lambda svc, *a, **k: stopped.setdefault("svc", svc))

    assert hook.run_db_tests([APP_FILE], {hook.AUTOSTART_ENV: "1"}) == []
    assert stopped["svc"] == [CFG.db.db_service]  # only the newly-started service


@requires_db
def test_run_db_tests_up_failure_skips_and_stops_nothing(monkeypatch):
    monkeypatch.setattr(hook, "db_redis_running", lambda *a, **k: False)
    monkeypatch.setattr(hook, "_compose_running_services", lambda *a, **k: set())
    monkeypatch.setattr(hook, "_compose_up_db_redis", lambda *a, **k: False)  # daemon down
    stopped = {}
    monkeypatch.setattr(hook, "_compose_stop", lambda svc, *a, **k: stopped.setdefault("svc", svc))

    assert hook.run_db_tests([APP_FILE], {hook.AUTOSTART_ENV: "1"}) == []
    assert "svc" not in stopped  # returns before try/finally -> _compose_stop never called


# --- verify_python: run the checks in the venv, not the launching interpreter ---
# The hooks are wired as `python3 <script>`; on Windows that resolves to the
# Microsoft Store shim, which has no pytest and none of the project's linters, so
# every check failed on tooling rather than on the code.


def test_verify_python_prefers_windows_venv(tmp_path):
    py = tmp_path / ".venv/Scripts/python.exe"
    py.parent.mkdir(parents=True)
    py.write_text("")

    assert hook.verify_python(tmp_path) == str(py)


def test_verify_python_prefers_posix_venv(tmp_path):
    py = tmp_path / ".venv/bin/python"
    py.parent.mkdir(parents=True)
    py.write_text("")

    assert hook.verify_python(tmp_path) == str(py)


def test_verify_python_falls_back_to_launcher_without_venv(tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "_can_verify", lambda exe: True)
    assert hook.verify_python(tmp_path) == sys.executable


def test_verify_python_skips_a_launcher_that_cannot_import_the_tooling(tmp_path, monkeypatch):
    # Regression: with no venv, the fallback returned sys.executable unconditionally
    # -- which on Windows IS the Store shim the function exists to avoid, so every
    # check died with "No module named pytest". A stdlib-only project has no venv as
    # its normal state, so this is not just a fresh-clone edge.
    usable = str(tmp_path / "real-python")
    monkeypatch.setattr(hook, "_can_verify", lambda exe: exe != sys.executable)
    monkeypatch.setattr(hook.shutil, "which", lambda name: usable if name == "python" else None)

    assert hook.verify_python(tmp_path) == usable


def test_verify_python_never_probes_python3(tmp_path, monkeypatch):
    # `python3` is the shim being escaped; probing it could hand back the very
    # interpreter that has no pytest.
    assert "python3" not in hook.PATH_PYTHONS

    probed = []
    monkeypatch.setattr(hook, "_can_verify", lambda exe: False)
    monkeypatch.setattr(hook.shutil, "which", lambda name: probed.append(name) or None)
    hook.verify_python(tmp_path)

    assert "python3" not in probed


def test_verify_python_tries_py_launcher_when_python_is_absent(tmp_path, monkeypatch):
    usable = str(tmp_path / "py-launcher")
    monkeypatch.setattr(hook, "_can_verify", lambda exe: exe == usable)
    monkeypatch.setattr(hook.shutil, "which", lambda name: usable if name == "py" else None)

    assert hook.verify_python(tmp_path) == usable


def test_verify_python_returns_launcher_when_nothing_can_verify(tmp_path, monkeypatch):
    # Better to fail loudly on tooling than to report green having run nothing.
    monkeypatch.setattr(hook, "_can_verify", lambda exe: False)
    monkeypatch.setattr(hook.shutil, "which", lambda name: None)

    assert hook.verify_python(tmp_path) == sys.executable


def test_verify_python_prefers_venv_without_probing(tmp_path, monkeypatch):
    # A venv is the project's declared environment: a missing dep there is a real
    # provisioning failure to surface, not something to route around via PATH.
    py = tmp_path / ".venv/bin/python"
    py.parent.mkdir(parents=True)
    py.write_text("")
    monkeypatch.setattr(hook, "_can_verify", lambda exe: pytest.fail("venv must not be probed"))

    assert hook.verify_python(tmp_path) == str(py)


def test_can_verify_detects_a_missing_import(tmp_path):
    # Exercised against real interpreters, so the probe itself is not just a mock.
    hook._can_verify.cache_clear()
    try:
        assert hook._can_verify(sys.executable) is True  # runs pytest right now
        assert hook._can_verify(str(tmp_path / "does-not-exist")) is False
    finally:
        hook._can_verify.cache_clear()


def test_verify_python_used_by_lint_and_test_checks(tmp_path, monkeypatch):
    py = tmp_path / ".venv/Scripts/python.exe"
    py.parent.mkdir(parents=True)
    py.write_text("")
    monkeypatch.setattr(hook, "REPO_ROOT", tmp_path)
    # The script constants resolve at import against the *real* repo root, so moving
    # REPO_ROOT does not move them, and the script-backed checks now skip when their
    # script is absent. Point each at a real file in the sandbox -- otherwise this
    # asserts nothing about two of the three.
    for attr, rel in (
        ("LINT_ALL", "scripts/lint-all.py"),
        ("CHECK_LOCK_MARKERS", "scripts/clm.py"),
    ):
        script = tmp_path / rel
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("")
        monkeypatch.setattr(hook, attr, script)

    for check in (hook.CHECK_LINT, hook.CHECK_SCRIPT_TESTS, hook.CHECK_LOCKS):
        spec = hook._command_for(check)
        assert spec is not None, f"{check} must be runnable when its script exists"
        argv, _cwd, _artifact = spec
        assert argv[0] == str(py), f"{check} must run under the venv interpreter"


# --- a check whose script this project does not have is a SKIP, not a failure ---
# Returning an argv for a missing script does not skip it: the interpreter exits 2
# with "can't open file", which run_checks reports as a failure with a usage message
# in place of a finding and nothing in the source tree that can fix it. No generated
# project ships check-lock-markers.py, so this fired on every lockfile change.


def test_command_for_skips_checks_whose_script_is_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "LINT_ALL", tmp_path / "nope-lint-all.py")
    monkeypatch.setattr(hook, "CHECK_LOCK_MARKERS", tmp_path / "nope-check-locks.py")
    assert hook._command_for(hook.CHECK_LINT) is None
    assert hook._command_for(hook.CHECK_LOCKS) is None
    # The infra-free pytest tier needs no project script, so it still runs.
    assert hook._command_for(hook.CHECK_SCRIPT_TESTS) is not None


def test_lint_check_passes_no_secrets_to_the_lint_runner(tmp_path, monkeypatch):
    """`--no-secrets` is contractual: lint-all.py must accept it, so it must be sent.

    A lint runner that does not parse the flag exits 2 from argparse, which is a
    permanent Tier 1 failure on every stop.
    """
    script = tmp_path / "lint-all.py"
    script.write_text("")
    monkeypatch.setattr(hook, "LINT_ALL", script)
    spec = hook._command_for(hook.CHECK_LINT)
    assert spec is not None
    argv, _cwd, artifact = spec
    assert "--changed" in argv and "--no-secrets" in argv
    assert artifact == "logs/lint-errors.log"


def _set_db_enabled(monkeypatch, enabled: bool) -> None:
    """Swap in a Config whose DB tier is on/off, whatever this project's manifest says.

    `Config`/`DbConfig` are frozen, so the flag cannot be poked in place — and it must
    not be, since these tests are vendored and run against every project's real
    manifest. Replacing the whole config keeps them shape-agnostic.
    """
    monkeypatch.setattr(hook, "CFG", replace(hook.CFG, db=replace(hook.CFG.db, enabled=enabled)))


# --- Tier 2b without a DB: the suite still has to run -------------------------
# `run_db_tests` returns [] when `[db] enabled = false`, and it is the only tier that
# consults `host_test_targets`. So a DB-less project used to run lint and the vendored
# hook tests and silently never its own test suite. `run_host_tests` is the seam that
# fixes it; these tests pin both branches without asserting any one project's shape.


def test_run_host_tests_runs_pytest_without_a_db(monkeypatch, tmp_path):
    _set_db_enabled(monkeypatch, False)
    calls: list[list[str]] = []

    def fake_pytest(targets, repo_root, extra_env=None, deadline=None):
        calls.append(list(targets))
        assert extra_env is None, "a DB-less run must not inject DB env"
        return []

    monkeypatch.setattr(hook, "_pytest_failures", fake_pytest)
    changed = [f"{CFG.tests_dir}test_something.py"]
    assert hook.run_host_tests(changed, {}, tmp_path) == []
    assert calls == [changed], "the changed test file should have been run"


def test_run_host_tests_reports_a_failure_without_a_db(monkeypatch, tmp_path):
    _set_db_enabled(monkeypatch, False)
    monkeypatch.setattr(
        hook, "_pytest_failures", lambda *a, **k: [(hook.CHECK_TESTS, None, "1 failed")]
    )
    failures = hook.run_host_tests([f"{CFG.tests_dir}test_x.py"], {}, tmp_path)
    assert [name for name, _artifact, _tail in failures] == [hook.CHECK_TESTS]


def test_run_host_tests_is_a_no_op_when_no_test_code_changed(monkeypatch, tmp_path):
    _set_db_enabled(monkeypatch, False)
    monkeypatch.setattr(hook, "_pytest_failures", lambda *a, **k: pytest.fail("should not run"))
    assert hook.run_host_tests(["README.md", "docs/x.md"], {}, tmp_path) == []


def test_pytest_failures_treats_no_tests_collected_as_a_pass(monkeypatch, tmp_path):
    """pytest exit 5 must not block the stop.

    Targets are *changed files* under tests/, so editing a helper that holds no tests
    of its own (conftest.py, a support module) yields "no tests ran" — a failure the
    agent cannot fix by editing source.
    """

    class _Result:
        returncode = hook.PYTEST_NO_TESTS_COLLECTED
        stdout = "no tests ran in 0.01s"
        stderr = ""

    monkeypatch.setattr(hook.subprocess, "run", lambda *a, **k: _Result())
    assert hook._pytest_failures(["tests/support.py"], tmp_path) == []


def test_pytest_failures_reports_a_real_failure(monkeypatch, tmp_path):
    class _Result:
        returncode = 1
        stdout = "1 failed"
        stderr = ""

    monkeypatch.setattr(hook.subprocess, "run", lambda *a, **k: _Result())
    failures = hook._pytest_failures(["tests/test_x.py"], tmp_path)
    assert [name for name, _artifact, _tail in failures] == [hook.CHECK_TESTS]


def test_run_host_tests_delegates_to_the_db_tier_when_a_db_is_configured(monkeypatch, tmp_path):
    _set_db_enabled(monkeypatch, True)
    seen: list[tuple[list[str], Path]] = []
    monkeypatch.setattr(
        hook,
        "run_db_tests",
        lambda paths, env, repo_root, deadline=None: seen.append((paths, repo_root)) or [],
    )
    hook.run_host_tests(["app/main.py"], {}, tmp_path)
    assert seen == [(["app/main.py"], tmp_path)], "the DB tier owns infra gating, not this"


# --- Which tree the gate verifies ------------------------------------------
#
# The reported defect: a session whose every edit was routed into an ephemeral box had
# its Stop gate verify the *static checkout* instead, because `CLAUDE_PROJECT_DIR` — and
# so `REPO_ROOT` — names the checkout. It blocked twice on two failures belonging to the
# branch that checkout was parked on, work the session had never touched and could not
# fix. Every test below is written against tmp_path and the payload, never against this
# machine's layout, so it passes in a consumer that has no box tier at all.


def _lease_workspace(tmp_path, project="proj", box="proj--task-0824", **lease):
    """A checkout with a workspace lease file beside it. Returns (repo_root, box_path)."""
    repo_root = tmp_path / project
    repo_root.mkdir()
    box_path = tmp_path / hook.BOXES_DIR_NAME / box
    (box_path / ".git").mkdir(parents=True)
    entry = {"project": project, "kind": "task", "session": "s" * 36, "branch": "agent/x"}
    entry.update(lease)
    (tmp_path / hook.BOXES_DIR_NAME / hook.LEASE_FILE_NAME).write_text(
        json.dumps({"boxes": {box: entry}}), encoding="utf-8"
    )
    return repo_root, box_path


class TestSessionId:
    def test_reads_the_payload_field(self):
        assert hook.session_id('{"session_id": "abc123"}') == "abc123"

    def test_whitespace_is_stripped(self):
        assert hook.session_id('{"session_id": "  abc  "}') == "abc"

    def test_absent_field_is_empty(self):
        assert hook.session_id('{"stop_hook_active": true}') == ""

    def test_unparseable_payload_is_empty(self):
        assert hook.session_id("") == ""
        assert hook.session_id("not json") == ""
        assert hook.session_id("[1, 2]") == ""

    def test_a_non_string_id_is_empty(self):
        assert hook.session_id('{"session_id": 7}') == ""


class TestSessionsMatch:
    """Mirrors `worktree.sessions_match`; a box cut by hand carries an abbreviated id."""

    def test_exact(self):
        # Spelled as a real session id rather than a bare hex run: detect-secrets reads
        # an undashed one as a high-entropy string and fails the commit.
        full = "250c0cdc-f240-4813-9105-8a9503778a59"
        assert hook.sessions_match(full, full)

    def test_an_abbreviation_still_names_its_session(self):
        full = "250c0cdc-f240-4813-9105-8a9503778a59"
        assert hook.sessions_match("250c0cdc", full)
        assert hook.sessions_match(full, "250c0cdc")

    def test_too_short_a_prefix_is_not_a_match(self):
        short = "a" * (hook.SESSION_PREFIX_MIN - 1)
        assert not hook.sessions_match(short, short + "bbbbbbbb")

    def test_an_unowned_lease_matches_nobody(self):
        assert not hook.sessions_match("", "abcdefgh")
        assert not hook.sessions_match("abcdefgh", "")


class TestSessionBox:
    def test_finds_the_box_this_session_holds(self, tmp_path):
        repo_root, box_path = _lease_workspace(tmp_path, session="a" * 36)
        assert hook.session_box("a" * 36, repo_root) == box_path

    def test_an_abbreviated_lease_still_matches(self, tmp_path):
        repo_root, box_path = _lease_workspace(tmp_path, session="abcdefgh")
        assert hook.session_box("abcdefgh-1111-2222", repo_root) == box_path

    def test_another_sessions_box_is_not_this_ones(self, tmp_path):
        repo_root, _box = _lease_workspace(tmp_path, session="a" * 36)
        assert hook.session_box("b" * 36, repo_root) is None

    def test_a_box_for_another_project_is_ignored(self, tmp_path):
        _lease_workspace(tmp_path, project="proj", session="a" * 36)
        (tmp_path / "other").mkdir()
        assert hook.session_box("a" * 36, tmp_path / "other") is None

    def test_a_preview_box_is_never_verified(self, tmp_path):
        """A preview is a throwaway copy of somebody else's branch, brought up to be
        looked at. Verifying it blocks this session on a tree it cannot fix."""
        repo_root, _box = _lease_workspace(tmp_path, kind="preview", session="a" * 36)
        assert hook.session_box("a" * 36, repo_root) is None

    def test_a_reaped_box_falls_back(self, tmp_path):
        """`reconcile` destroys boxes under disk pressure; the lease can outlive one."""
        repo_root, box_path = _lease_workspace(tmp_path, session="a" * 36)
        (box_path / ".git").rmdir()
        assert hook.session_box("a" * 36, repo_root) is None

    def test_no_lease_file_is_the_ordinary_case(self, tmp_path):
        """Every consuming project, every CI runner, every fresh clone."""
        repo_root = tmp_path / "proj"
        repo_root.mkdir()
        assert hook.session_box("a" * 36, repo_root) is None

    def test_unreadable_json_never_raises(self, tmp_path):
        repo_root, _box = _lease_workspace(tmp_path, session="a" * 36)
        (tmp_path / hook.BOXES_DIR_NAME / hook.LEASE_FILE_NAME).write_text(
            "{ truncated", encoding="utf-8"
        )
        assert hook.session_box("a" * 36, repo_root) is None

    def test_a_malformed_entry_is_skipped_not_fatal(self, tmp_path):
        repo_root, _box = _lease_workspace(tmp_path, session="a" * 36)
        (tmp_path / hook.BOXES_DIR_NAME / hook.LEASE_FILE_NAME).write_text(
            json.dumps({"boxes": {"junk": "not a mapping"}}), encoding="utf-8"
        )
        assert hook.session_box("a" * 36, repo_root) is None

    def test_no_session_id_means_no_lookup(self, tmp_path):
        repo_root, _box = _lease_workspace(tmp_path, session="a" * 36)
        assert hook.session_box("", repo_root) is None


class TestVerifyRoot:
    def test_the_session_box_wins(self, tmp_path):
        repo_root, box_path = _lease_workspace(tmp_path, session="a" * 36)
        payload = json.dumps({"session_id": "a" * 36})
        assert hook.verify_root(payload, repo_root) == box_path

    def test_no_box_is_the_checkout_exactly_as_before(self, tmp_path):
        repo_root = tmp_path / "proj"
        repo_root.mkdir()
        payload = json.dumps({"session_id": "a" * 36})
        assert hook.verify_root(payload, repo_root) == repo_root

    def test_an_empty_payload_is_the_checkout(self, tmp_path):
        repo_root, _box = _lease_workspace(tmp_path, session="a" * 36)
        assert hook.verify_root("", repo_root) == repo_root


class TestChecksFollowTheRoot:
    """The behaviour that matters: the commands run against the tree that was resolved."""

    def test_a_box_root_re_roots_every_project_owned_script(self, tmp_path):
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "lint-all.py").write_text("", encoding="utf-8")
        argv, cwd, _artifact = hook._command_for(hook.CHECK_LINT, tmp_path)
        assert cwd == tmp_path
        assert str(hook.LINT_ALL) not in argv
        assert str(tmp_path / "scripts" / "lint-all.py") in argv

    def test_the_default_root_is_unchanged(self, tmp_path, monkeypatch):
        """The constants stay the single spelling, and stay monkeypatchable."""
        script = tmp_path / "lint-all.py"
        script.write_text("", encoding="utf-8")
        monkeypatch.setattr(hook, "LINT_ALL", script)
        argv, cwd, _artifact = hook._command_for(hook.CHECK_LINT)
        assert cwd == hook.REPO_ROOT
        assert str(script) in argv

    def test_the_lock_tier_reads_the_boxs_copy(self, tmp_path):
        """Absent there, absent for this run -- the box is what holds the change."""
        assert hook._command_for(hook.CHECK_LOCKS, tmp_path) is None
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "check-lock-markers.py").write_text("", encoding="utf-8")
        spec = hook._command_for(hook.CHECK_LOCKS, tmp_path)
        assert spec is not None and spec[1] == tmp_path

    def test_the_test_runner_follows_the_root(self, tmp_path):
        (tmp_path / "scripts").mkdir()
        runner = tmp_path / "scripts" / "run-tests.py"
        runner.write_text("", encoding="utf-8")
        argv, artifact = hook.test_runner_argv(["tests/test_x.py"], tmp_path)
        assert str(runner) in argv
        assert artifact == hook.TEST_ARTIFACT

    def test_run_checks_passes_the_root_down(self, monkeypatch, tmp_path):
        seen: list[Path | None] = []
        monkeypatch.setattr(hook, "_command_for", lambda name, root=None: seen.append(root) or None)
        hook.run_checks([hook.CHECK_LINT], tmp_path)
        assert seen == [tmp_path]


def test_verify_checks_the_session_box_not_the_checkout(monkeypatch, tmp_path):
    """The regression test for the report: revert `verify_root` out of `verify` and the
    roots below are `REPO_ROOT`, which is the checkout the session never wrote to."""
    repo_root, box_path = _lease_workspace(tmp_path, session="a" * 36)
    monkeypatch.setattr(hook, "REPO_ROOT", repo_root)
    monkeypatch.setattr(hook, "verify_enabled", lambda env: True)
    seen: dict[str, object] = {}

    def _note(key, value, result=None):
        seen[key] = value
        return result

    monkeypatch.setattr(hook, "_git_status_porcelain", lambda root: _note("status", root, ""))
    monkeypatch.setattr(hook, "_git_branch_diff", lambda root: "")
    monkeypatch.setattr(
        hook, "run_checks", lambda names, root=None, deadline=None: _note("checks", root, [])
    )
    monkeypatch.setattr(
        hook,
        "run_host_tests",
        lambda paths, env, root=None, deadline=None: _note("tests", root, []),
    )
    monkeypatch.setattr(
        hook, "write_verify_artifact", lambda failures, root=None: _note("artifact", root)
    )
    monkeypatch.setattr(hook, "write_rounds", lambda value, root=None: _note("rounds", root))

    assert hook.verify(json.dumps({"session_id": "a" * 36}), {}) == 0
    assert seen["status"] == box_path
    assert seen["checks"] == box_path
    assert seen["tests"] == box_path
    assert seen["artifact"] == box_path
    assert seen["rounds"] == box_path


def test_a_blocked_stop_says_which_tree_it_checked(capsys, tmp_path):
    """Artifact paths are relative to the tree that was checked. Without the line, the
    agent opens the checkout's stale `logs/stop-verify.log` and sees another run."""
    hook._print_verify_failures([("lint", None, "boom")], blocking=True, root=tmp_path)
    err = capsys.readouterr().err
    assert str(tmp_path) in err and "box" in err


def test_an_ordinary_stop_says_nothing_about_boxes(capsys):
    hook._print_verify_failures([("lint", None, "boom")], blocking=True)
    assert "box" not in capsys.readouterr().err
