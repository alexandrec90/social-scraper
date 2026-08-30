#!/usr/bin/env python3
"""Mechanical checks, lint and push for /ship.

The shipped-marker half is gone with `branch-per-task.py`. The marker existed to tell
the *next prompt* that this branch was spent, so the branch hook could leave it — and
with agent work happening in a box that is destroyed after its PR merges, there is no
next prompt on a spent branch to warn.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import task_branch as tb

REPO_ROOT = Path(__file__).resolve().parents[1]
LINT_ALL = REPO_ROOT / "scripts" / "lint-all.py"
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NOT_SHIPPABLE = 3
EXIT_DIRTY_TREE = 4
EXIT_LINT_FAILED = 5
EXIT_PUSH_FAILED = 6


def is_shippable(branch: str, default: str) -> tuple[bool, str]:
    """Return whether branch is an isolated, namespaced task branch suitable for a PR.

    The namespace identifies a short-lived task branch without coupling shipping to
    whichever agent created it (for example ``agent/``, ``claude/`` or ``codex/``).
    Unnamespaced branches remain reserved for default and long-lived home branches.
    """
    if not branch:
        return False, "HEAD is detached; check out a task branch before shipping."
    if branch == default:
        return False, f"'{default}' is the default branch; ship from a namespaced task branch."
    namespace, separator, topic = branch.partition("/")
    if not separator or not namespace or not topic:
        return False, (
            f"'{branch}' is not a namespaced task branch; refusing to ship it. "
            "Use a branch such as agent/fix-thing."
        )
    return True, ""


def tree_clean(porcelain: str) -> bool:
    return not porcelain.strip()


def backoff_delays() -> list[int]:
    return [2, 4, 8, 16]


def _git(*args: str, capture: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=capture,
        text=True,
        check=False,
    )


def current_branch() -> str:
    result = _git("branch", "--show-current")
    return result.stdout.strip() if result.returncode == 0 else ""


def default_branch() -> str:
    return tb.detect_default_branch(_git, fallback="main")


def _porcelain() -> str:
    result = _git("status", "--porcelain")
    return result.stdout if result.returncode == 0 else ""


def branch_diff_files(base: str, git=_git) -> list[str]:
    """The files this branch changed, measured from where it left the default branch.

    The gate used to ask for `--changed`, whose set is the working tree versus HEAD --
    and `main()` has just refused to proceed unless that set is *empty*. So the lint
    gate ran on nothing, every time, and reported LINT PASSED for it. A branch about to
    become a PR is reviewed as a whole, so the whole branch is what to lint.

    Returns [] when the merge base cannot be found (a base ref absent locally, a shallow
    clone); the caller then falls back to the old behaviour rather than linting nothing
    silently.
    """
    ref = f"origin/{base}"
    if git("rev-parse", "--verify", "--quiet", ref).returncode != 0:
        ref = base
    merge_base = git("merge-base", ref, "HEAD")
    if merge_base.returncode != 0 or not merge_base.stdout.strip():
        return []
    # --diff-filter=d: a path deleted on this branch has nothing left to lint, and a
    # linter handed a missing file fails the run on a usage error.
    diff = git("diff", "--name-only", "--diff-filter=d", merge_base.stdout.strip(), "HEAD")
    if diff.returncode != 0:
        return []
    return [line.strip() for line in diff.stdout.splitlines() if line.strip()]


def runner_supports_paths(help_text: str) -> bool:
    """Whether this project's lint runner accepts `--paths`.

    `lint-all.py` is project-owned, not vendored, so a project can be older than this
    file. Probing `--help` beats parsing argparse's exit-2 usage error out of a stream
    the gate otherwise passes straight through to the terminal.
    """
    return "--paths" in help_text


def _lint_argv(paths: list[str], help_text: str) -> list[str]:
    """The lint command to run, given the branch's files and the runner's capabilities."""
    if paths and runner_supports_paths(help_text):
        return [sys.executable, str(LINT_ALL), "--paths", *paths]
    return [sys.executable, str(LINT_ALL), "--changed"]


def _lint_help() -> str:
    try:
        probe = subprocess.run(
            [sys.executable, str(LINT_ALL), "--help"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return ""
    return (probe.stdout or "") + (probe.stderr or "")


def _run_lint(base: str = "") -> bool:
    if not LINT_ALL.is_file():
        print(f"ship: required lint runner is missing: {LINT_ALL}", file=sys.stderr)
        return False
    paths = branch_diff_files(base) if base else []
    argv = _lint_argv(paths, _lint_help())
    if paths and "--paths" not in argv:
        # Say it rather than quietly linting the empty working tree: the gate is about
        # to run, pass, and mean nothing, and only this line explains why.
        print(
            f"ship: {LINT_ALL.name} has no --paths, so the gate can only see the working "
            "tree -- which is clean. It will check nothing. Add --paths to this "
            "project's lint runner (see devkit's) to lint the branch diff.",
            file=sys.stderr,
        )
    try:
        result = subprocess.run(argv, cwd=REPO_ROOT, check=False)
    except OSError as exc:
        print(f"ship: could not run lint gate: {exc}", file=sys.stderr)
        return False
    return result.returncode == 0


def _push(branch: str, sleep=time.sleep) -> bool:
    """Push the task branch, retrying only recognizably transient failures."""
    delays = backoff_delays()
    for attempt in range(len(delays) + 1):
        result = _git("push", "-u", "origin", branch)
        if result.returncode == 0:
            return True
        stderr = (result.stderr or "").lower()
        transient = any(
            marker in stderr
            for marker in ("could not resolve", "timed out", "connection", "network")
        )
        if not transient or attempt == len(delays):
            if result.stderr:
                print(result.stderr.rstrip(), file=sys.stderr)
            return False
        sleep(delays[attempt])
    return False


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv not in ([], ["--preflight"]):
        print("usage: ship.py [--preflight]", file=sys.stderr)
        return EXIT_USAGE

    branch = current_branch()
    base = default_branch()
    ok, reason = is_shippable(branch, base)
    if not ok:
        print(f"ship: {reason}", file=sys.stderr)
        return EXIT_NOT_SHIPPABLE

    if argv == ["--preflight"]:
        print(f"ship: branch={branch} base={base}")
        return EXIT_OK

    if not tree_clean(_porcelain()):
        print("ship: working tree is dirty; commit the intended changes first.", file=sys.stderr)
        return EXIT_DIRTY_TREE
    if not _run_lint(base):
        print("ship: branch-scope lint failed; see logs/lint-errors.log.", file=sys.stderr)
        return EXIT_LINT_FAILED
    if not _push(branch):
        print("ship: push failed after retries.", file=sys.stderr)
        return EXIT_PUSH_FAILED

    print(f"ship: pushed branch={branch} base={base}; open or reuse its PR before marking shipped.")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
