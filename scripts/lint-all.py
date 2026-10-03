#!/usr/bin/env python3
"""Run every linter and write the failures to a single parseable artifact.

The contract this implements (see CLAUDE.md, "Failure artifacts"): an agent fixing
lint reads `logs/lint-errors.log`, never the terminal. So this script keeps the
terminal to a status line plus the artifact path, and puts everything actionable in
the file — on failure *and* on success, where it writes an empty artifact so a stale
run can't mislead the next agent.

Auto-fix runs before the reporting pass, so only genuinely unfixable errors are
reported and the agent never burns a cycle on something `ruff --fix` already solved.

Usage:
    python scripts/lint-all.py            # whole repo
    python scripts/lint-all.py --changed  # working-tree diff vs HEAD, plus untracked
    python scripts/lint-all.py --paths a.py b.md  # exactly these (/ship's branch diff)
"""

from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACT = REPO_ROOT / "logs" / "lint-errors.log"

# ruff runs over the whole repo; mypy does not. `scripts/hooks/` and
# `sync-devkit.py` are the *vendored* harness — byte-identical upstream code that
# `sync-devkit.py --check` forbids this repo from editing, so reporting type errors
# in it would hand the agent findings it is not allowed to fix. Narrowing the scope
# here is belt to `[tool.mypy] exclude`'s braces in pyproject.toml: the config covers
# directory recursion, this covers an explicit `mypy .`.
MYPY_SCOPE = ["social_scraper/", "tests/"]

# Every Python path in `sync-devkit.py`'s MANIFEST, kept out of the passes that
# *rewrite* files. Reporting on upstream code is merely unhelpful; reformatting it is
# a change `sync-devkit.py --check` fails the build for, and the agent cannot fix
# that by editing source either. The trigger is real: the harness is lint-clean only
# because `scripts/**` ignores E501, and `ruff format` does not read per-file-ignores
# — so it reflowed a long line in `harness_config.py` and the next step saw drift.
# Broader than MYPY_SCOPE's inverse: `task_branch.py` is vendored too.
NO_FIX_SCOPE = ["scripts/hooks", "scripts/sync-devkit.py", "scripts/task_branch.py"]

# dotenv-linter v4 takes a subcommand; a bare file list is rejected as an
# unrecognised one, which reaches the artifact as a usage error no source edit can
# fix. `--plain` keeps ANSI colour codes out of a file something else has to parse,
# and `--skip-updates` stops the linter making a network call on every run.
#
# UnorderedKey is ignored deliberately, and narrowly — it is the one check here with
# no correctness content. It wants every key alphabetised within its blank-line
# group, which in `.env.example` means DATABASE_URL resequenced after the POSTGRES_*
# components it is built from, ARCHIVE_BACKEND ahead of the ARCHIVE_ROOT tree it
# extends, and the host ports shuffled out of service order. That
# grouping is the file's entire documentation value. Per the lint policy in
# `.claude/rules/engineering.md`: a rule that fires on what a formatter would decide
# is misconfigured, so turn it off rather than train everyone to read past it.
DOTENV_CMD = [
    "dotenv-linter",
    "check",
    "--plain",
    "--skip-updates",
    "--ignore-checks",
    "UnorderedKey",
]


def changed_paths() -> list[str]:
    """Every tracked-but-modified plus untracked path, relative to the repo root."""
    tracked = _git("diff", "--name-only", "HEAD")
    untracked = _git("ls-files", "--others", "--exclude-standard")
    return sorted({n for n in (tracked + untracked) if (REPO_ROOT / n).exists()})


def explicit_paths(paths: list[str]) -> list[str]:
    """Exactly the paths given, normalised, minus any that no longer exist.

    `--paths` is how a caller that knows its own scope states it. `/ship` is the
    motivating one: it insists on a clean tree and then had only `--changed` to ask
    with, whose set is the working tree *versus HEAD* -- empty, by construction, for
    every commit it was about to push. The gate passed having linted nothing.

    A deleted path is dropped rather than passed on: there is nothing left to lint,
    and ruff/mypy treat a missing argument as a usage error that fails the whole run.
    """
    return sorted({n.replace("\\", "/") for n in paths if (REPO_ROOT / n).exists()})


def python_targets(paths: list[str]) -> list[str]:
    """The lintable .py files among `paths`."""
    return [n for n in paths if n.endswith(".py")]


def changed_python_files() -> list[str]:
    """Tracked-but-modified plus untracked .py files, relative to the repo root."""
    return python_targets(changed_paths())


def workflow_files(limit_to: list[str] | None = None) -> list[str]:
    """`.github/workflows/*.yml`, optionally narrowed to a changed-file list.

    Explicit paths rather than a bare `actionlint`, which discovers workflows itself:
    discovery only finds them when the cwd is the repo root, and reports success
    having checked nothing anywhere else. Returning [] when there are none is what
    keeps the pass from turning "no workflows" into a usage error in the artifact.
    """
    found = sorted(
        p.relative_to(REPO_ROOT).as_posix()
        for p in (REPO_ROOT / ".github" / "workflows").glob("*.yml")
    )
    return found if limit_to is None else [p for p in found if p in set(limit_to)]


def env_files(limit_to: list[str] | None = None) -> list[str]:
    """Root-level `.env*` files, optionally narrowed to a changed-file list.

    `.env` itself is gitignored and machine-local, so in practice this is
    `.env.example` — the file every new clone copies, and therefore the one whose
    typos cost the most. Read from the filesystem rather than hardcoded, so the pass
    is simply inert in a project that has neither.
    """
    found = sorted(p.name for p in REPO_ROOT.glob(".env*") if p.is_file())
    return found if limit_to is None else [p for p in found if p in set(limit_to)]


def _git(*args: str) -> list[str]:
    result = subprocess.run(["git", "-C", str(REPO_ROOT), *args], capture_output=True, text=True)
    return result.stdout.splitlines() if result.returncode == 0 else []


def _missing_module(cmd: list[str]) -> bool:
    """True when `cmd` is a `-m` invocation of a module this interpreter lacks.

    The linters run as `[sys.executable, "-m", tool, ...]`, so the executable always
    exists and `subprocess.run` never raises FileNotFoundError — the interpreter
    itself exits 1 with "No module named mypy" on stderr. Without this probe that
    text lands in the artifact as an unfixable finding, which is the exact outcome
    run_tool's contract exists to prevent. Probing beats matching the message: the
    subprocess runs under sys.executable, so find_spec here answers for the very
    interpreter that would run it.
    """
    if len(cmd) < 3 or cmd[0] != sys.executable or cmd[1] != "-m":
        return False
    try:
        return importlib.util.find_spec(cmd[2]) is None
    except (ImportError, ValueError):
        return True


def run_tool(name: str, cmd: list[str], fix_hint: str) -> str:
    """Run one linter; return its artifact section, or "" when it passed or was absent.

    A missing tool is NOT a failure. Writing "command not found" into the artifact
    would hand the agent something it cannot fix in the source tree, so it degrades
    to a terminal note instead.
    """
    if _missing_module(cmd):
        print(f"  {name}: not installed — skipped")
        return ""
    try:
        result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
    except FileNotFoundError:
        print(f"  {name}: not installed — skipped")
        return ""
    if result.returncode == 0:
        print(f"  {name}: ok")
        return ""
    body = (result.stdout + result.stderr).strip()
    print(f"  {name}: FAILED")
    return f"# {name}\n# fix: {fix_hint}\n{body}\n\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--changed", action="store_true", help="lint only the working-tree diff")
    # `/ship` passes the branch's diff here. It is part of the same contract as
    # `--no-secrets` below: ship.py is vendored byte-identical and probes `--help` for
    # this flag, falling back to `--changed` -- which, on the clean tree ship demands,
    # is empty. A project that drops `--paths` still ships; its gate just checks nothing.
    parser.add_argument(
        "--paths",
        nargs="+",
        metavar="FILE",
        default=[],
        help="lint exactly these files, scoped as --changed is (used by /ship)",
    )
    # Accepted, and a no-op here: this project has no detect-secrets pass to skip.
    # The Stop hook (`scripts/hooks/stop.py`) passes `--no-secrets` unconditionally,
    # because a project whose lint runner *does* have one must skip it on every turn —
    # secrets scanning is the pre-commit hook's job and running it churns
    # `.secrets.baseline`. stop.py is vendored byte-identical and cannot introspect
    # this file, so parsing the flag is part of the contract between the two. Omitting
    # it is not a silent no-op: argparse rejects the unknown flag and exits 2, which
    # the Stop hook reports as a lint failure on *every* stop, with a usage message
    # instead of a finding and nothing in the source tree that can fix it.
    parser.add_argument(
        "--no-secrets",
        action="store_true",
        help="skip the secrets pass (accepted for Stop-hook compatibility; no-op here)",
    )
    args = parser.parse_args(argv)

    # `--paths` and `--changed` are the same narrowing, differing only in who names the
    # files; everything downstream treats them identically.
    scoped = args.changed or bool(args.paths)
    selected: list[str] = []
    if scoped:
        selected = explicit_paths(args.paths) if args.paths else changed_paths()
    changed = selected if scoped else None
    targets = python_targets(selected)
    workflows = workflow_files(changed)
    envs = env_files(changed)
    if scoped and not (targets or workflows or envs):
        print("lint-all: no changed files this run lints; nothing to do.")
        _write_artifact("")
        return 0
    scope = targets or ["."]

    label = f"{len(selected)} file(s)" if scoped else "whole repo"
    print(f"lint-all: {label}")

    sections = ""
    # A narrowed run with only a workflow or `.env` edit leaves `targets` empty, and
    # `scope` then falls back to `["."]` — which would silently widen a per-turn
    # check into a whole-repo pass. Gate the Python passes on having Python to lint.
    if targets or not scoped:
        # Auto-fix first, then report. Both ruff passes mutate the same files, so they
        # must stay sequential relative to each other, and both skip the vendored
        # harness — see NO_FIX_SCOPE.
        no_fix = [arg for path in NO_FIX_SCOPE for arg in ("--exclude", path)]
        subprocess.run(
            [sys.executable, "-m", "ruff", "check", *scope, "--fix", "--unsafe-fixes", *no_fix],
            cwd=REPO_ROOT,
            capture_output=True,
        )
        subprocess.run(
            [sys.executable, "-m", "ruff", "format", *scope, *no_fix],
            cwd=REPO_ROOT,
            capture_output=True,
        )

        sections += run_tool(
            "ruff",
            [sys.executable, "-m", "ruff", "check", *scope, "--output-format=full"],
            "ruff check . --fix --unsafe-fixes",
        )
        sections += run_tool(
            "mypy",
            [sys.executable, "-m", "mypy", *(targets or MYPY_SCOPE), "--show-error-codes"],
            f"mypy {' '.join(MYPY_SCOPE)} --show-error-codes",
        )

    # `.claude/hooks/session-start.sh` installs both of these into every session, so
    # a provisioned checkout has them. They are real executables rather than `-m`
    # modules, so run_tool's FileNotFoundError branch is what degrades a missing one
    # to a terminal note instead of an unfixable artifact entry.
    if workflows:
        sections += run_tool(
            "actionlint",
            ["actionlint", *workflows],
            f"actionlint {' '.join(workflows)}",
        )
    if envs:
        sections += run_tool("dotenv-linter", [*DOTENV_CMD, *envs], " ".join([*DOTENV_CMD, *envs]))

    _write_artifact(sections)
    if sections:
        print(f"\nlint-all: FAILED — details in {ARTIFACT.relative_to(REPO_ROOT)}")
        return 1
    print(f"\nlint-all: clean (artifact cleared: {ARTIFACT.relative_to(REPO_ROOT)})")
    return 0


def _write_artifact(sections: str) -> None:
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    header = "# source: scripts/lint-all.py\n" if sections else ""
    ARTIFACT.write_text(header + sections, encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
