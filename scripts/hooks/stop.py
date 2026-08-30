#!/usr/bin/env python3
"""Stop dispatcher (portable replacement for copilot-settings-stop.ps1).

On every stop, best-effort and always exiting 0, typecheck the frontend when skin
files changed.

Then a pre-stop verification phase (Tiers 1-3) reproduces the PR-gate checks
locally, scoped to the working-tree diff, and exits 2 to relay any failure back
into the session so it is fixed here instead of after a CI round-trip:
  - Tier 1: `lint-all.py --changed` (ruff/mypy/vulture/eslint/... , no infra),
  - Tier 2a: host `pytest scripts/hooks/tests/` when a scripts/ file changed --
    needs no Docker, so it runs even under a stack-down-by-default policy,
  - Tier 2b: host `pytest` (app/ or tests/ Python changed). With `[db] enabled`
    it runs against db+redis -- no app container, so the footprint is just
    db+redis. Uses them if up; else, only with `*_STOP_TESTS_AUTOSTART=1`, brings
    db+redis up on demand, runs, then stops what it started. Paid-safe via
    pytest.ini's `-m "not paid"`. Without a DB the suite needs no infra and simply
    runs,
  - Tier 3: `check-lock-markers.py` when a requirements file changed, and vitest
    when frontend/src changed.

**The diff is the branch, not just the working tree.** Gating on `git status` alone
made the gate inert the moment anything was committed — and `/ship` commits — so the
strongest verification happened before a commit and none happened after, which is
backwards: the committed branch is exactly what CI will see. `changed_paths` is now
unioned with `git diff --name-only origin/<default>...HEAD`.

**And the tree is the session's, not this file's.** `CLAUDE_PROJECT_DIR` points the hook
at the static checkout, so a session whose edits were all routed into an ephemeral box
had its Stop gate verify a tree it never touched — blocking twice, in one recorded case,
on failures belonging to the branch that checkout happened to be parked on. `verify_root`
reads the workspace lease file and verifies the box this session holds; with no lease
file, no entry, or no box tier at all — which is every consuming project — it is
`REPO_ROOT` exactly as before.

**It runs up to `MAX_VERIFY_ROUNDS` rounds, blocking on all but the last.**
`stop_hook_active` is a boolean, so honouring it alone meant verification ran on the
first stop only: the agent was told what was broken, "fixed" it, stopped again, and the
second stop skipped the checks entirely — a wrong fix ended the session looking green. A
per-worktree round counter lets each fix be re-checked, and the final round reports
without blocking so the cycle always terminates.

**A check the budget stopped is unknown, and unknown does not block.** It reported
nothing about the branch, so a round holding only stopped checks stands down after
saying so — a retry would spend the same budget in the same place and ask again. A
check that finished and failed still blocks, in the same round or any other.

Failures are written to `logs/stop-verify.log` (and the tier-owned artifacts
`logs/lint-errors.log` / `logs/test-failures.log`); the terminal gets a status line and
the paths, per the failure-artifact rule in `.claude/rules/engineering.md`.

It is opt-out-able (`*_SKIP_STOP_VERIFY=1`, prefixed per project — see `[project]
env_prefix`), gated on relevant files changing, and skips cleanly when tooling/infra is
absent.

`skin_changed` and the verification helpers (`stop_hook_active`, `verify_enabled`,
`session_id`, `sessions_match`, `session_box`, `verify_root`, `changed_paths`,
`committed_paths`, `all_changed`, `blocked_rounds`, `should_block`,
`select_checks`, `run_checks`) are pure and unit-tested
(`scripts/hooks/tests/test_stop.py`); each external step is its own importable,
independently tested script.
"""

import contextlib
import functools
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path

# scripts/hooks/ and scripts/ on path so the sibling, stdlib-only helpers import
# before the venv (same pattern as branch-per-task.py's task_branch import).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_config
import task_branch

REPO_ROOT = (Path(__file__).parent / "../..").resolve()
# Everything project-specific below (env prefix, DB creds/ports, frontend layout,
# source-tree shape) is sourced from .devkit.toml so this
# script can be vendored unchanged across projects. See harness_config.py.
CFG = harness_config.load(REPO_ROOT)

# --- Pre-stop verification (Tiers 1-3) -------------------------------------
# Reproduce the PR-gate checks locally, scoped to the working-tree diff, so an
# agent fixes them in-session instead of after a CI round-trip. Each is gated on
# a relevant file changing; Tier 2b (DB tests) additionally needs db+redis
# reachable; all skip cleanly when their tooling/infra is absent.
LINT_ALL = REPO_ROOT / "scripts/lint-all.py"
CHECK_LOCK_MARKERS = REPO_ROOT / "scripts/check-lock-markers.py"
# The project's own test runner. Preferred over a bare `-m pytest` for the application
# tier because it writes `logs/test-failures.log` — the parseable artifact the agent is
# supposed to fix from, with third-party frames stripped and each failure block capped.
# Project-owned (its scope is that project's testpaths), so absence falls back to pytest.
RUN_TESTS = REPO_ROOT / "scripts/run-tests.py"

# Artifact for the tiers that have no runner of their own to write one (script-tests,
# lock-markers, vitest). Written on success too — as an empty file — so a stale artifact
# can never send the next agent chasing a failure that is already fixed.
VERIFY_ARTIFACT = "logs/stop-verify.log"
TEST_ARTIFACT = "logs/test-failures.log"

# How many verification rounds one stop-chain gets. The last round reports without
# blocking, so three means: block, re-check and block again, then re-check and stand down
# -- two chances to fix, both of them actually verified. The ceiling is what guarantees
# the fix -> stop -> block cycle terminates. Not a manifest field: this is a property of
# the interaction, not of any project's shape.
MAX_VERIFY_ROUNDS = 3

# Seconds the whole verification tier gets, not one check. Every agent harness kills a
# Stop hook that outruns its own ceiling, and a killed hook is the worst of the three
# outcomes: it writes no artifact, prints nothing, and ends the session on "stop hook
# failed" with no way to tell which tier hung -- so the agent learns only that the gate
# is broken. A Codex session ended exactly 600s after its last message with
# `logs/stop-verify.log` still empty, which is that shape. `run_checks` and
# `_pytest_failures` spawned uncapped subprocesses, so a suite that waits on a prompt,
# a port or a lock had no upper bound at all.
#
# One budget across the tiers rather than one per check, because a check cannot know
# what the ones before it spent, and it is the *total* the harness measures. Well under
# a 600s ceiling: a timeout has to be reportable, which means leaving room to write the
# artifact and print.
VERIFY_BUDGET_SECONDS = 420

# How a stopped check announces itself, in the artifact and to `unfinished`. A check the
# budget stopped reported nothing about the branch, and the gate must not read it as red:
# see `verify`, where a round of nothing-but-these stands down instead of blocking.
UNFINISHED_MARK = "Stopped: "

# The pre-verification frontend typecheck, whose output goes to DEVNULL, gets its own
# smaller cap: it is a side effect, and it must not be able to spend the gate's ceiling.
TYPECHECK_TIMEOUT_SECONDS = 120

# Interpreter candidates for the verification checks, relative to the repo root.
VENV_PYTHONS = (".venv/Scripts/python.exe", ".venv/bin/python")
# PATH interpreters to try when there is no venv and the launcher cannot run the
# checks. `python3` is deliberately absent -- on Windows it is the Store shim this
# whole dance exists to escape, and on POSIX it is already `sys.executable`.
PATH_PYTHONS = ("python", "py")
# Importing this is the cheapest proof an interpreter can run the checks at all.
VERIFY_IMPORT = "pytest"

# Every capture below decodes as UTF-8 with replacement, never through the platform's
# locale codec. `text=True` alone picks cp1252 on Windows, and a byte that codepage does
# not map -- 0x9d, which ruff, pytest and docker all emit inside box-drawing and curly
# quotes -- raises `UnicodeDecodeError` *inside subprocess's reader thread*, where no
# `try` in this file can see it. `subprocess.run` then returns a CompletedProcess whose
# `stdout` and `stderr` are both **None** (`stdout[0] if stdout else None`, over the
# buffer the dead thread never filled), so the visible failure is a TypeError a hundred
# lines away in `run_checks`: `unsupported operand type(s) for +: 'NoneType' and
# 'NoneType'`, with two thread tracebacks above it and nothing naming the tool whose
# output could not be read. That is how this reached a user -- as a Stop hook crash
# pointing at the line that assembles a tail rather than at the line that decodes one.
#
# A tail is diagnostic text. A replacement character in it costs a reader nothing;
# failing to decode one costs the whole verification tier.
#
# Spelled out at every call site rather than shared through a constant, because
# `subprocess.run` is an overloaded signature: a `**kwargs` dict makes the arguments
# opaque to mypy and to `test_every_capture_in_a_hook_declares_its_codec`, which is the
# ratchet that keeps the next capture from being added without one.


@functools.cache
def _can_verify(executable: str) -> bool:
    """True when `executable` can import what the verification checks need.

    Cached: `verify_python` is called once per check, and this spawns a process.
    """
    try:
        completed = subprocess.run(
            [executable, "-c", f"import {VERIFY_IMPORT}"],
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False  # not executable, or hung -- either way, unusable.
    return completed.returncode == 0


def verify_python(repo_root: Path | None = None) -> str:
    """Interpreter for the verification checks -- one that can actually run them.

    The hooks are wired as `python3 <script>`, and on Windows `python3` resolves
    to the Microsoft Store shim: an interpreter with none of the project's
    dependencies installed. Running the checks under it fails on tooling rather
    than on the code -- `-m pytest` dies with "No module named pytest" and
    lint-all.py cannot import its linters -- which reads as a real CI failure and
    is unfixable by editing source.

    A venv wins outright: if one exists it is the project's declared environment,
    and a missing dependency *there* is a real provisioning failure worth
    surfacing. Without one, `sys.executable` cannot be trusted blindly -- it is
    the shim. A project with no venv is the normal case for a stdlib-only repo,
    not just the "fresh clone, CI" edge this once assumed, and on a Windows
    desktop the launcher is exactly the interpreter to avoid. So probe it, and
    fall through to PATH when it cannot import the tooling.

    Returns `sys.executable` when nothing can: the checks then fail loudly on
    tooling rather than silently reporting green having run nothing.

    `repo_root` defaults to REPO_ROOT at call time, not import time, so the
    module-level constant stays overridable.
    """
    root = REPO_ROOT if repo_root is None else repo_root
    for rel in VENV_PYTHONS:
        candidate = root / rel
        if candidate.exists():
            return str(candidate)
    if _can_verify(sys.executable):
        return sys.executable
    for name in PATH_PYTHONS:
        found = shutil.which(name)
        if found and found != sys.executable and _can_verify(found):
            return found
    return sys.executable


# pytest's EXIT_NOTESTSCOLLECTED. Not a failure for this hook -- see _pytest_failures.
PYTEST_NO_TESTS_COLLECTED = 5

CHECK_LINT = "lint"  # Tier 1: lint-all.py --changed (no infra)
CHECK_SCRIPT_TESTS = "script-tests"  # Tier 2a: host pytest scripts/hooks/tests (no infra)
CHECK_TESTS = "tests"  # Tier 2b: host pytest tests/ against db+redis (paid-safe)
CHECK_LOCKS = "lock-markers"  # Tier 3: check-lock-markers.py (deps changed)
CHECK_FRONTEND = "frontend"  # Tier 3: vitest (frontend/src changed)

# Files whose change means "dependencies moved, re-verify the locks". Covers both
# dependency models the harness supports: pip-tools (`requirements*.in/.txt`) and
# uv/poetry (a single committed lockfile). Matching only `requirements*` left the
# lock-marker tier silently inert in every uv-native project — it never fired, so
# nothing looked broken.
_REQ_RE = re.compile(r"(^|/)(requirements[^/]*\.(in|txt)|uv\.lock|poetry\.lock)$")

# Harness control env vars, prefixed per project (CFG.env_prefix, e.g. CARAMELI):
#   *_SKIP_STOP_VERIFY -- opt out of pre-stop verification entirely.
#   *_STOP_TESTS_AUTOSTART -- opt in to Tier 2b bringing up ONLY db+redis on
#     demand when app/tests changed and they are down, running host pytest, then
#     stopping only what it started. Off by default so the stack-down-to-save-
#     memory policy holds. Host pytest (no app container) keeps the peak footprint
#     at db+redis (~0.75 GB) vs the full stack (~4 GB); tests inherit pytest.ini's
#     `-m "not paid"` so they can never bill a live provider.
SKIP_VERIFY_ENV = CFG.env("SKIP_STOP_VERIFY")
AUTOSTART_ENV = CFG.env("STOP_TESTS_AUTOSTART")


def _read_stdin() -> str:
    """Best-effort read of the hook payload; '' when stdin is a tty or unreadable.

    Decodes the raw stdin bytes as UTF-8 (the encoding Claude Code writes the hook
    payload in) with surrogateescape, instead of trusting the process locale. On
    Windows that locale is cp1252, which mis-decodes non-ASCII payload bytes into
    lone surrogates on read and then raises UnicodeEncodeError when the same string
    is written to the archive child (position-2642 '\\udc9d' crash). surrogateescape
    lets any byte round-trip back out unchanged when re-encoded for the child.
    """
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return ""
        buffer = getattr(sys.stdin, "buffer", None)
        if buffer is not None:
            return buffer.read().decode("utf-8", errors="surrogateescape")
        return sys.stdin.read()
    except (OSError, ValueError):
        return ""


def skin_changed(porcelain: str) -> bool:
    """True when `git status --porcelain -- frontend/src/skins` reported changes."""
    return any(line.strip() for line in porcelain.splitlines())


def _git_skin_status(repo_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--", CFG.frontend.skin],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return ""
    return result.stdout if result.returncode == 0 else ""


# --- Pre-stop verification helpers (pure; unit-tested in test_stop.py) ------


def stop_hook_active(raw_stdin: str) -> bool:
    """True when this stop is already a continuation triggered by a stop hook.

    Claude Code sets `stop_hook_active` on the payload once a Stop hook has
    blocked and the agent resumed. Honouring it prevents an infinite
    fix -> stop -> block loop: verification runs on the first stop only.
    """
    try:
        payload = json.loads(raw_stdin)
    except (json.JSONDecodeError, TypeError):
        return False
    return bool(isinstance(payload, dict) and payload.get("stop_hook_active"))


def verify_enabled(env: Mapping[str, str]) -> bool:
    """False when the operator has opted out of pre-stop verification."""
    return env.get(SKIP_VERIFY_ENV) != "1"


# --- Which tree this stop verifies -----------------------------------------
#
# `REPO_ROOT` is where *this file* lives, and for a whole class of session that is not
# where the work is. Claude Code resolves the hook command through `CLAUDE_PROJECT_DIR`,
# which is the session's static checkout, so an agent whose every edit was routed into an
# ephemeral box got a Stop gate pointed at a checkout it never touched: it verified
# whatever branch that checkout happened to be parked on, and blocked on failures
# belonging to somebody else's work. That is not a hypothetical -- a session whose PR gate
# was fully green was blocked twice by two failures on the checkout's `release/…` branch,
# with no edit of its own anywhere in the tree being checked.
#
# The box tier is a *workspace* facility rather than a project one, so the lookup below is
# the stdlib-only half of it: read the lease file, match on the session id, take the
# directory. `worktree.py` owns these names, and a hook cannot import it -- hooks run
# before a venv exists and from a checkout that has no workspace scripts at all. The
# duplication is the same trade `harness_events.BOX_NAME_SEP` makes, for the same reason.
#
# **Absence is the ordinary case, not an error.** Every consuming project has no
# `.worktrees/` at all; so does a CI runner, a fresh clone, and any session working
# directly in its checkout. All of them fall through to `REPO_ROOT` and behave exactly as
# they did before this existed.
BOXES_DIR_NAME = ".worktrees"
LEASE_FILE_NAME = "leases.json"
# `worktree.SESSION_PREFIX_MIN`: a box cut by hand carries `--session <first 8 hex>`, and
# the abbreviation has to keep naming the session that abbreviated it.
SESSION_PREFIX_MIN = 8
# Only a task box holds a session's work. A `preview` box is a throwaway copy of somebody
# else's branch, brought up to be looked at in a browser -- verifying it would block this
# session on a tree it did not write and cannot fix.
BOX_KIND_TASK = "task"


def session_id(raw_stdin: str) -> str:
    """The session id from the hook payload, or '' when there is none to read."""
    try:
        payload = json.loads(raw_stdin)
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    value = payload.get("session_id")
    return value.strip() if isinstance(value, str) else ""


def sessions_match(recorded: str, session: str) -> bool:
    """Does a lease's session id name this session?

    Exact, or either id a prefix of the other with the shorter side at least
    `SESSION_PREFIX_MIN` characters. Mirrors `worktree.sessions_match` -- see the note
    above for why this is a copy rather than an import.
    """
    if not recorded or not session:
        return False
    if recorded == session:
        return True
    short, long = sorted((recorded, session), key=len)
    return len(short) >= SESSION_PREFIX_MIN and long.startswith(short)


def session_box(session: str, repo_root: Path = REPO_ROOT) -> Path | None:
    """The ephemeral box holding this session's work for `repo_root`, or None.

    Best-effort by contract, in every direction: no lease file, unreadable JSON, an
    entry whose directory has since been reaped, no session id on the payload -- each
    returns None, and the caller verifies the checkout as before. A Stop gate must never
    fail *because* of this lookup; the worst it may do is decline to improve on the
    default.
    """
    if not session:
        return None
    project = repo_root.name
    try:
        raw = (repo_root.parent / BOXES_DIR_NAME / LEASE_FILE_NAME).read_text(encoding="utf-8")
        boxes = json.loads(raw).get("boxes", {})
        if not isinstance(boxes, dict):
            return None
        for name, lease in boxes.items():
            if not isinstance(lease, dict):
                continue
            if lease.get("project") != project:
                continue
            if lease.get("kind", BOX_KIND_TASK) != BOX_KIND_TASK:
                continue
            if not sessions_match(str(lease.get("session", "")), session):
                continue
            path = repo_root.parent / BOXES_DIR_NAME / name
            # A husk -- a box whose `git worktree remove` died partway -- is a directory
            # with no `.git`, and every check below would run against a tree git has
            # stopped tracking. Fall back to the checkout instead.
            if (path / ".git").exists():
                return path
    except (OSError, ValueError, AttributeError):
        return None
    return None


def verify_root(raw_stdin: str, repo_root: Path = REPO_ROOT) -> Path:
    """The tree this stop should verify: the session's box when it has one."""
    return session_box(session_id(raw_stdin), repo_root) or repo_root


def _repo_script(root: Path, default: Path) -> Path:
    """`default`, re-rooted at `root` when that is a different tree.

    The module constants stay the single spelling of each script's location -- and stay
    monkeypatchable, which is how the "this project has no such script" tiers are
    tested. When the tree being verified is a box, the same relative path inside it is
    what the checks must run, because the box is the copy holding the change.
    """
    if root == REPO_ROOT:
        return default
    try:
        return root / default.relative_to(REPO_ROOT)
    except ValueError:
        return default


def changed_paths(porcelain: str) -> list[str]:
    """Repo-relative paths from `git status --porcelain` (handles renames/quotes)."""
    paths: list[str] = []
    for line in porcelain.splitlines():
        if not line.strip():
            continue
        rest = line[3:] if len(line) > 3 else line.strip()
        if " -> " in rest:  # rename: "R  old -> new" -> keep the new path
            rest = rest.split(" -> ", 1)[1]
        paths.append(rest.strip().strip('"'))
    return paths


def committed_paths(diff_output: str) -> list[str]:
    """Repo-relative paths from `git diff --name-only` output."""
    return [line.strip() for line in diff_output.splitlines() if line.strip()]


def all_changed(porcelain: str, diff_output: str) -> list[str]:
    """Everything this stop should verify: the working tree plus the branch's commits.

    The working tree alone is not the diff CI will run. Once the agent commits — and
    `/ship` commits — `git status` is empty, `select_checks` returns nothing, and the
    gate passes having run nothing at the exact point where the branch is finally in the
    state the PR gate will see. Ordering is working-tree-first and de-duplicated so a
    file that is both committed and further modified is verified once.
    """
    merged = dict.fromkeys(changed_paths(porcelain))
    merged.update(dict.fromkeys(committed_paths(diff_output)))
    return list(merged)


def blocked_rounds(previous: int, chain_active: bool) -> int:
    """Block count for this stop, counting the block currently being considered.

    `chain_active` is `stop_hook_active`: false means this is a fresh stop rather than a
    continuation, so the count restarts regardless of what the marker says -- a counter
    left behind by an abandoned chain must not spend this one's budget.

    Returns 1 on the first failing stop of a chain, so `should_block` can be a plain
    `< max_rounds` with no off-by-one at the call site.
    """
    return (previous if chain_active else 0) + 1


def should_block(rounds_used: int, max_rounds: int = MAX_VERIFY_ROUNDS) -> bool:
    """True while the gate may still block; False once its budget is spent.

    `rounds_used` is the count *including* the block being considered, so with
    max_rounds=3 the third failing stop reports and stands down instead of blocking a
    fourth time.
    """
    return rounds_used < max_rounds


def _is_py(path: str) -> bool:
    return path.endswith((".py", ".pyi"))


def _is_frontend(path: str) -> bool:
    return path.startswith(CFG.frontend.src)


def _is_reqs(path: str) -> bool:
    return bool(_REQ_RE.search(path))


def _is_script(path: str) -> bool:
    """A Python file under scripts/ -- covered by the infra-free host test suite."""
    return path.startswith("scripts/") and _is_py(path)


def host_test_targets(paths: list[str]) -> list[str]:
    """pytest targets for the DB tier, or [] when no app/tests Python changed.

    An app/ change can break tests anywhere, and without testmon we cannot map it
    to specific tests -- run the whole unit suite. A tests-only change runs just
    the changed test files (fast, precise).
    """
    if any(_is_py(p) and p.startswith(CFG.app_dir) for p in paths):
        return [CFG.unit_tests]
    return sorted(p for p in paths if p.startswith(CFG.tests_dir) and _is_py(p))


def select_checks(paths: list[str]) -> list[str]:
    """The infra-light checks to run for this diff (the DB tier is separate).

    - lint runs whenever anything changed (lint-all.py --changed self-scopes
      internally, so an irrelevant edit is a fast no-op).
    - script-tests (host pytest scripts/hooks/tests) run when a scripts/ file
      changed. These need no Docker, so they run even with the stack down.
    - lock-markers run when a requirements file changed.
    - frontend (vitest) runs when frontend/src changed; tsc/eslint are already
      covered by lint's changed-scope, so this adds only the unit tests.

    The DB-backed tier (app/ or tests/ Python) is handled by run_db_tests(),
    which needs db+redis and so has its own reachability/autostart gating.
    """
    if not paths:
        return []
    checks = [CHECK_LINT]
    if any(_is_script(p) for p in paths):
        checks.append(CHECK_SCRIPT_TESTS)
    if any(_is_reqs(p) for p in paths):
        checks.append(CHECK_LOCKS)
    if CFG.frontend.enabled and any(_is_frontend(p) for p in paths):
        checks.append(CHECK_FRONTEND)
    return checks


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run git in `repo_root`, never raising -- a failed CompletedProcess stands in.

    Shaped to satisfy `task_branch`'s injected-git contract (via `_git_at`) so the
    marker-path and default-branch helpers are shared rather than reimplemented here.
    """
    try:
        return subprocess.run(
            ["git", *args],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return subprocess.CompletedProcess(list(args), 1, "", "")


def _git_at(repo_root: Path) -> Callable[..., subprocess.CompletedProcess[str]]:
    return lambda *args: _git(repo_root, *args)


def _git_status_porcelain(repo_root: Path) -> str:
    result = _git(repo_root, "status", "--porcelain")
    return result.stdout if result.returncode == 0 else ""


def _git_branch_diff(repo_root: Path) -> str:
    """`git diff --name-only origin/<default>...HEAD`, or '' when it cannot be taken.

    Three dots: the diff from the merge base, i.e. exactly what this branch changed and
    nothing origin moved underneath it. Returns '' on the default branch (no divergence),
    in a fresh repo, and when origin/<default> is missing -- all of which correctly
    degrade to verifying the working tree alone.
    """
    default_branch = task_branch.detect_default_branch(_git_at(repo_root))
    upstream = f"origin/{default_branch}"
    if _git(repo_root, "rev-parse", "--verify", "--quiet", f"refs/remotes/{upstream}").returncode:
        return ""
    result = _git(repo_root, "diff", "--name-only", f"{upstream}...HEAD")
    return result.stdout if result.returncode == 0 else ""


def _rounds_path(repo_root: Path) -> Path | None:
    return task_branch.worktree_file(_git_at(repo_root), task_branch.STOP_ROUNDS_MARKER_NAME)


def read_rounds(repo_root: Path = REPO_ROOT) -> int:
    """Blocked-stop count for this worktree; 0 when absent or unparseable."""
    path = _rounds_path(repo_root)
    if path is None or not path.exists():
        return 0
    try:
        return max(0, int(path.read_text(encoding="utf-8").strip()))
    except (OSError, ValueError):
        return 0


def write_rounds(value: int, repo_root: Path = REPO_ROOT) -> None:
    """Persist the blocked-stop count, or clear it when `value` is 0. Best-effort."""
    path = _rounds_path(repo_root)
    if path is None:
        return
    with contextlib.suppress(OSError):
        if value <= 0:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(value), encoding="utf-8")


def autostart_enabled(env: Mapping[str, str]) -> bool:
    """True when the operator opted into on-demand db+redis autostart."""
    return env.get(AUTOSTART_ENV) == "1"


def services_to_stop(before: set[str], after: set[str]) -> list[str]:
    """Services this hook started (running now, not before) -- what to stop again."""
    return sorted(after - before)


def _compose_running_services(repo_root: Path = REPO_ROOT) -> set[str]:
    try:
        result = subprocess.run(
            ["docker", "compose", "ps", "--services", "--status", "running"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    return set(result.stdout.split()) if result.returncode == 0 else set()


def db_redis_running(repo_root: Path = REPO_ROOT) -> bool:
    """True when both db and redis are up (whether via the full stack or db+redis
    alone) -- host pytest can reach them either way."""
    return set(CFG.db.services).issubset(_compose_running_services(repo_root))


def _compose_up_db_redis(repo_root: Path = REPO_ROOT) -> bool:
    """Bring up ONLY db+redis, waiting for health. False on failure (daemon down,
    timeout) so the DB tier skips instead of blocking."""
    try:
        result = subprocess.run(
            ["docker", "compose", "up", "-d", "--wait", *CFG.db.services],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _compose_stop(services: list[str], repo_root: Path = REPO_ROOT) -> None:
    """Stop (not remove) the given services, freeing their memory. Best-effort."""
    if not services:
        return
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(
            ["docker", "compose", "stop", *services],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )


def _parse_host_port(output: str) -> str | None:
    """Extract the host port from `docker compose port` output (e.g. '0.0.0.0:5432',
    '[::]:5432', '127.0.0.1:5433')."""
    line = next((ln for ln in reversed(output.splitlines()) if ln.strip()), "")
    if ":" not in line:
        return None
    port = line.rsplit(":", 1)[1].strip()
    return port if port.isdigit() else None


def _compose_host_port(
    service: str, container_port: int, repo_root: Path = REPO_ROOT
) -> str | None:
    try:
        result = subprocess.run(
            ["docker", "compose", "port", service, str(container_port)],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return _parse_host_port(result.stdout) if result.returncode == 0 else None


def host_db_env(repo_root: Path = REPO_ROOT) -> dict[str, str] | None:
    """Env for host pytest to reach the containers over their published ports, or
    None when a port cannot be resolved (containers not actually up)."""
    db = CFG.db
    db_port = _compose_host_port(db.db_service, db.db_port, repo_root)
    redis_port = _compose_host_port(db.redis_service, db.redis_port, repo_root)
    if not db_port or not redis_port:
        return None
    db_url = f"{db.url_scheme}://{db.user}:{db.password}@localhost:{db_port}/{db.name}"
    env: dict[str, str] = {name: db_url for name in db.url_env}
    env[db.redis_env] = f"redis://localhost:{redis_port}"
    # Extra secrets host pytest needs; an already-set os.environ value wins.
    for name, default in db.test_env.items():
        env[name] = os.environ.get(name, default)
    return env


def test_runner_argv(targets: list[str], root: Path | None = None) -> tuple[list[str], str | None]:
    """(argv, artifact) for the application test tier.

    Prefers the project's own `run-tests.py`: it writes `logs/test-failures.log` with
    third-party frames stripped and each failure block capped, which is the artifact the
    agent is meant to fix from. A bare `-m pytest` is the fallback for a project that
    does not ship one, and reports through `logs/stop-verify.log` instead.

    NB `run-tests.py` is invoked under `verify_python()`, and re-invokes pytest as
    `sys.executable -m pytest` -- which is that same interpreter, so the venv is
    preserved through the extra hop.
    """
    base = REPO_ROOT if root is None else root
    runner = _repo_script(base, RUN_TESTS)
    if runner.exists():
        return [verify_python(base), str(runner), *targets], TEST_ARTIFACT
    return [verify_python(base), "-m", "pytest", *targets, "-q"], None


def verify_deadline(budget: float = VERIFY_BUDGET_SECONDS) -> float:
    """A `time.monotonic()` stamp `budget` seconds from now, shared by every tier.

    Monotonic on purpose: the gate has to survive a clock the OS steps under it, and a
    wall-clock deadline can go backwards mid-run.
    """
    return time.monotonic() + budget


def timeout_tail(argv: list[str]) -> str:
    """What `logs/stop-verify.log` says about a check the budget stopped.

    Says nothing about the code, because nothing is known about it: the check did not
    finish. Naming the command is the whole point -- the agent can run it by hand and
    see for itself, which is the one thing a killed hook never let it do.
    """
    return (
        f"{UNFINISHED_MARK}the {VERIFY_BUDGET_SECONDS}s budget for the whole verification "
        "tier ran out while this check was running, so its result is unknown (an earlier "
        "tier may have spent it).\nRe-run it by hand: " + " ".join(argv)
    )


def unfinished(failures: list[tuple[str, str | None, str]]) -> list[str]:
    """The names of the checks the budget stopped, in order.

    Recognised by the tail's prefix rather than by a fourth tuple field, so the shape
    `run_checks` and `run_host_tests` both return -- and every test that builds one by
    hand -- is unchanged.
    """
    return [name for name, _artifact, tail in failures if tail.startswith(UNFINISHED_MARK)]


def _bounded_run(
    argv: list[str], cwd: Path, deadline: float | None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess | None:
    """`subprocess.run` capped by the tier's shared deadline; None when it ran out.

    A `None` is not the same as the `OSError` skip its callers also handle: a tool this
    machine cannot run is a local gap and defers to CI, while a check that would not
    finish is a fact about this branch worth reporting. Both stay non-fatal here --
    `OSError` still propagates to the caller that skips it.
    """
    left = None if deadline is None else deadline - time.monotonic()
    if left is not None and left <= 0:
        return None
    try:
        return subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=left,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return None


def combined_output(result: subprocess.CompletedProcess) -> str:
    """`stdout` and `stderr` as one string, tolerating either being `None`.

    The codec above removes the only known way a capture returns `None` streams, and
    this stays anyway, because the two failures are not the same size. An undecodable
    byte in a lint tail is a cosmetic loss; a `TypeError` raised while *reporting* a
    failure takes the whole Stop hook down, and a hook that dies writes no artifact,
    prints nothing, and ends the session on "stop hook failed" -- the same worst-of-three
    outcome `VERIFY_BUDGET_SECONDS` exists to prevent, arriving from the other end.

    So: nothing on the reporting path may assume a stream was captured. `stdout=DEVNULL`
    on a future call, or a test stubbing `subprocess.run`, produces `None` here too, and
    neither should be able to end a session.
    """
    return (result.stdout or "") + (result.stderr or "")


def _pytest_failures(
    targets: list[str],
    repo_root: Path,
    extra_env: dict[str, str] | None = None,
    deadline: float | None = None,
) -> list[tuple[str, str | None, str]]:
    """Run the host test tier over `targets`; [] on pass, one CHECK_TESTS failure else.

    Shared by both shapes of Tier 2b (with and without a DB), so the two differ only
    in the infra they arrange and the env they inject -- not in how a failure is
    captured and reported. An OS error is a skip: verification never blocks the agent
    over a local tooling gap. Running out of `deadline` is not -- see `_bounded_run`.
    """
    argv, artifact = test_runner_argv(targets, repo_root)
    try:
        result = _bounded_run(
            argv,
            repo_root,
            deadline,
            env={**os.environ, **extra_env} if extra_env else None,
        )
    except OSError:
        return []
    if result is None:
        return [(CHECK_TESTS, artifact, timeout_tail(argv))]
    # 5 is pytest's EXIT_NOTESTSCOLLECTED, and it is not a failure here. Targets come
    # from `host_test_targets`, which selects *changed files* under tests/ -- so editing
    # a helper that holds no tests of its own (conftest.py, a support module) hands
    # pytest a file with nothing to collect. Treating that as a failure blocks the stop
    # with "no tests ran", which no source edit can resolve.
    if result.returncode in (0, PYTEST_NO_TESTS_COLLECTED):
        return []
    tail = combined_output(result).strip().splitlines()[-20:]
    return [(CHECK_TESTS, artifact, "\n".join(tail))]


def run_host_tests(
    paths: list[str],
    env: Mapping[str, str],
    repo_root: Path = REPO_ROOT,
    deadline: float | None = None,
) -> list[tuple[str, str | None, str]]:
    """Tier 2b: host pytest for changed app/tests code, DB or no DB.

    Two shapes, one tier, and the second one is why this wrapper exists. With a DB
    configured the suite needs db+redis, so it goes through `run_db_tests`'s
    reachability/autostart/teardown dance. With `[db] enabled = false` the suite needs
    no infra at all and simply runs.

    Without that second branch a DB-less project ran **no application tests at all**
    on Stop: this is the only tier that consults `host_test_targets`, and it returned
    early on `not CFG.db.enabled`. So every `bare`-preset project -- and devkit itself,
    whose `tests/` tree is the generator's entire safety net -- got lint and the
    vendored hook tests, silently never its own suite, and only found a broken test in
    CI. The DB tier's gating is about *infra*; it should never have been the thing that
    decided whether tests run.
    """
    if CFG.db.enabled:
        return run_db_tests(paths, env, repo_root, deadline)
    targets = host_test_targets(paths)
    if not targets:
        return []
    return _pytest_failures(targets, repo_root, deadline=deadline)


def run_db_tests(
    paths: list[str],
    env: Mapping[str, str],
    repo_root: Path = REPO_ROOT,
    deadline: float | None = None,
) -> list[tuple[str, str | None, str]]:
    """Tier 2b with a DB: host pytest for changed app/tests against db+redis.

    Runs on the host (no app container) so the footprint is just db+redis. Uses
    them if already up; otherwise, only with autostart opted in, brings up db+redis,
    runs, then stops exactly what it started. Any infra gap (daemon down, up
    failure, unresolved ports) is a clean skip -> deferred to CI, never a block.

    Callers should prefer `run_host_tests`, which routes to this only when a DB is
    actually configured.
    """
    if not CFG.db.enabled:
        return []
    targets = host_test_targets(paths)
    if not targets:
        return []

    started: list[str] = []
    if not db_redis_running(repo_root):
        if not autostart_enabled(env):
            return []
        before = _compose_running_services(repo_root)
        if not _compose_up_db_redis(repo_root):
            return []
        started = services_to_stop(before, _compose_running_services(repo_root))

    try:
        db_env = host_db_env(repo_root)
        if db_env is None:
            return []
        return _pytest_failures(targets, repo_root, db_env, deadline)
    finally:
        _compose_stop(started, repo_root)


def _command_for(name: str, root: Path | None = None) -> tuple[list[str], Path, str | None] | None:
    """(argv, cwd, artifact_path) for a check, or None when its tool is absent.

    `root` is the tree to check -- `verify_root`'s answer, defaulting to this checkout.

    "Absent" includes a *script* that this project does not have. Returning an argv
    for a missing script does not skip the check -- the interpreter exits 2 with
    "can't open file", which `run_checks` cannot tell apart from a real finding and
    reports as a failure the agent has no way to fix. That is not hypothetical: no
    generated project ships `check-lock-markers.py`, so every one of them blocked its
    own Stop with a bogus `lock-markers` failure the moment a lockfile changed.

    Deciding absence here also makes it a readable state rather than an accident, which
    is what lets CI hold a project to it: `scripts/hooks/tests/test_repo_contract.py`
    fails when a script this project's own config makes reachable is missing.
    """
    base = REPO_ROOT if root is None else root
    lint_all = _repo_script(base, LINT_ALL)
    lock_markers = _repo_script(base, CHECK_LOCK_MARKERS)
    if name == CHECK_LINT:
        if not lint_all.exists():
            return None
        # --no-secrets is part of the contract between this hook and lint-all.py: a
        # project whose lint runner has a detect-secrets pass skips it here (it is the
        # pre-commit hook's job, and running it churns .secrets.baseline every turn),
        # and one without a secrets pass accepts the flag as a documented no-op. Either
        # way lint-all.py must *parse* it -- argparse rejecting it exits 2, which reads
        # as a permanent lint failure on every single Stop.
        return (
            [verify_python(base), str(lint_all), "--changed", "--no-secrets"],
            base,
            "logs/lint-errors.log",
        )
    if name == CHECK_SCRIPT_TESTS:
        return ([verify_python(base), "-m", "pytest", "scripts/hooks/tests/", "-q"], base, None)
    if name == CHECK_LOCKS:
        # Optional tier: the script is project-owned (its sentinels name that
        # project's lockfiles), so a project without one simply has no tier.
        if not lock_markers.exists():
            return None
        return ([verify_python(base), str(lock_markers)], base, None)
    if name == CHECK_FRONTEND:
        npm = shutil.which("npm")
        if not npm:
            return None
        # An unprovisioned tree is a missing toolchain, not a failing suite. A worktree
        # checks out tracked files only, so a freshly cut box has no `node_modules` and
        # `npm run test:run` exits non-zero with `'vitest' is not recognized` -- which
        # `run_checks` reported as `failed: frontend / would fail CI`, twice, while the
        # same suite was green in the checkout. That verdict points the session at its
        # own diff, which is the one place the problem is not, and the fix it implies
        # (change the code) is the wrong one. `shutil.which("npm")` was already this
        # tier's "is the toolchain here" test; it was just asking about the wrong half.
        if not (base / CFG.frontend.dir / "node_modules").is_dir():
            return None
        return ([npm, *CFG.frontend.test_cmd], base / CFG.frontend.dir, None)
    return None


def run_checks(
    names: list[str], root: Path | None = None, deadline: float | None = None
) -> list[tuple[str, str | None, str]]:
    """Run selected checks; return (name, artifact, tail) for each that failed.

    A missing tool or an OS error is a skip, never a failure: verification must
    never block the agent because of a local tooling gap.

    `deadline` is the shared budget from `verify_deadline`; a check that outruns it is
    reported rather than skipped, and the checks after it are stopped on arrival with
    the same message. `None` leaves every check uncapped, which is what the unit tests
    that stub `subprocess.run` want.
    """
    failures: list[tuple[str, str | None, str]] = []
    for name in names:
        spec = _command_for(name, root)
        if spec is None:
            continue
        argv, cwd, artifact = spec
        try:
            result = _bounded_run(argv, cwd, deadline)
        except OSError:
            continue
        if result is None:
            failures.append((name, artifact, timeout_tail(argv)))
            continue
        if result.returncode != 0:
            tail = combined_output(result).strip().splitlines()[-15:]
            failures.append((name, artifact, "\n".join(tail)))
    return failures


def artifact_body(failures: list[tuple[str, str | None, str]]) -> str:
    """The full text of `logs/stop-verify.log` for this run.

    Everything needed to diagnose goes in the file, including the tails of tiers that
    also wrote their own artifact -- one file the agent can open beats a decision about
    which of three to read. Empty string when nothing failed, which is written verbatim
    so a stale artifact cannot outlive the failure it describes.
    """
    if not failures:
        return ""
    lines = [
        "# source: scripts/hooks/stop.py (pre-stop verification)",
        "# fix: python scripts/lint-all.py --changed | python scripts/run-tests.py --changed",
    ]
    for name, artifact, tail in failures:
        lines.append("")
        lines.append(f"=== {name} ===" + (f" (see also {artifact})" if artifact else ""))
        lines.append(tail.strip() or "(no output captured)")
    return "\n".join(lines) + "\n"


def write_verify_artifact(
    failures: list[tuple[str, str | None, str]], repo_root: Path = REPO_ROOT
) -> None:
    """Persist the failure detail. Best-effort: an unwritable logs/ never blocks."""
    target = repo_root / VERIFY_ARTIFACT
    with contextlib.suppress(OSError):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(artifact_body(failures), encoding="utf-8")


def _print_verify_failures(
    failures: list[tuple[str, str | None, str]],
    blocking: bool = True,
    root: Path = REPO_ROOT,
) -> None:
    """Status line plus artifact paths -- never the failure text itself.

    The detail lives in the artifacts (see the failure-artifact rule in
    `.claude/rules/engineering.md`): streamed output scrolls away, and a 20-line tail
    inlined here is both too little to diagnose from and too much to skim.
    """
    stopped = unfinished(failures)
    ran = [name for name, _artifact, _tail in failures if name not in stopped]
    if blocking:
        verdict = "Pre-stop verification found issues that would fail CI -- fix before finishing:"
    elif ran:
        verdict = (
            f"Pre-stop verification still failing after {MAX_VERIFY_ROUNDS} attempts. "
            "Not blocking again -- but the branch is red and CI will say so:"
        )
    else:
        verdict = (
            "Pre-stop verification did not finish, so it found nothing -- not blocking. "
            "Run the command in the artifact if you have not already:"
        )
    lines = [verdict]
    if ran:
        lines.append(f"  failed: {', '.join(ran)}")
    if stopped:
        lines.append(
            f"  unknown -- the {VERIFY_BUDGET_SECONDS}s budget stopped it: {', '.join(stopped)}"
        )
    # Artifact paths are relative to the tree that was checked, and that is not always
    # this one -- say which, or the agent opens the checkout's stale copy of the file.
    if root != REPO_ROOT:
        lines.append(f"  checked: {root} (this session's box)")
    for path in dict.fromkeys(
        [VERIFY_ARTIFACT] + [a for _n, a, _t in failures if a and (root / a).exists()]
    ):
        lines.append(f"  details: {path}")
    lines.append(
        "Re-run locally: python scripts/lint-all.py --changed | python scripts/run-tests.py --changed"
    )
    lines.append(f"(Set {SKIP_VERIFY_ENV}=1 to skip this gate.)")
    print("\n".join(lines), file=sys.stderr)


def verify(raw_stdin: str, env: Mapping[str, str]) -> int:
    """Run pre-stop verification. Returns 2 (block, relay) on failure, else 0.

    Unlike the original, `stop_hook_active` does not skip the run -- it only says that
    this stop is a continuation, which is what decides whether the round counter
    restarts. Skipping on it meant the gate could report a failure but never confirm
    the fix, so a wrong fix ended the session looking green.
    """
    if not verify_enabled(env):
        return 0
    # Not REPO_ROOT: the session's edits may all be in a box, and verifying the checkout
    # instead means blocking this session on another branch's failures. See `verify_root`.
    root = verify_root(raw_stdin, REPO_ROOT)
    paths = all_changed(_git_status_porcelain(root), _git_branch_diff(root))
    # Infra-light tiers (lint, script-tests, locks, frontend) plus the application
    # test tier, which arranges its own db+redis reachability/autostart/teardown when
    # this project has a DB and just runs pytest when it does not.
    deadline = verify_deadline()
    failures = run_checks(select_checks(paths), root, deadline)
    failures += run_host_tests(paths, env, root, deadline)
    write_verify_artifact(failures, root)
    if not failures:
        write_rounds(0, root)  # green: the next failure starts from a full budget.
        return 0

    # A round with nothing but stopped checks is not a red branch -- it is a gate that ran
    # out of time, and blocking on it buys nothing: the retry gets the same budget, spends
    # it in the same place, and asks again. That happened twice in a row on devkit#237,
    # each time at the tail of a long session where a turn is at its most expensive, with
    # the suite already run green by hand in between. So report it, keep the artifact
    # naming the command, and leave the answer to that run or to CI. A round that also
    # holds a check which *did* finish and fail still blocks on that check.
    if unfinished(failures) == [name for name, _artifact, _tail in failures]:
        write_rounds(0, root)
        _print_verify_failures(failures, blocking=False, root=root)
        return 0

    rounds_used = blocked_rounds(read_rounds(root), stop_hook_active(raw_stdin))
    if not should_block(rounds_used):
        write_rounds(0, root)
        _print_verify_failures(failures, blocking=False, root=root)
        return 0
    write_rounds(rounds_used, root)
    _print_verify_failures(failures, blocking=True, root=root)
    return 2


def main() -> int:
    raw_stdin = _read_stdin()

    root = verify_root(raw_stdin, REPO_ROOT)
    if CFG.frontend.enabled and skin_changed(_git_skin_status(root)):
        npm = shutil.which("npm")
        if npm:
            # Capped like the verification tiers, and for the same reason: this runs
            # before them, inside the same hook, and its output is discarded -- so a
            # typecheck that hangs spends the harness's whole ceiling on a side effect
            # nobody reads. Its own budget rather than a share of the gate's, because
            # the gate is the part whose result matters.
            with contextlib.suppress(subprocess.TimeoutExpired):
                subprocess.run(
                    [npm, *CFG.frontend.typecheck_cmd],
                    cwd=root / CFG.frontend.dir,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=TYPECHECK_TIMEOUT_SECONDS,
                )

    # Pre-stop verification runs last: it may exit 2 to block the stop and relay
    # failures back into the session. All best-effort side effects above have
    # already run and always exit 0, so a blocked stop never loses that work.
    return verify(raw_stdin, os.environ)


if __name__ == "__main__":
    sys.exit(main())
