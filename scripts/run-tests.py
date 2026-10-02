#!/usr/bin/env python3
"""Run the application test suite and write failures to a parseable artifact.

Same contract as `lint-all.py`: the agent fixing a failure reads
`logs/test-failures.log`, not the terminal. Each failure block is capped so one
broken test cannot flood the artifact and bury the other twenty.

**The default is the tests named by what changed**, not the suite: every file
changed since the branch left `origin/<default>`, mapped to `tests/test_<stem>.py`,
plus `CONTRACT_TESTS`, which read every module and instruction file. Where `.devkit.toml` turns the `[frontend]` tier on, a changed source under its `src`
runs `vitest related` instead, which follows the imports to the tests that reach it.
The whole suite is CI's, the push gate's (`PRE_COMMIT` is in the environment under
pre-commit) and `--all`'s. Where git cannot say what changed, the suite runs.

Usage:
    python scripts/run-tests.py             # the tests for what changed
    python scripts/run-tests.py --all       # the whole application suite
    python scripts/run-tests.py --changed   # pytest's last-failed subset
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACT = REPO_ROOT / "logs" / "test-failures.log"

# Where the whole suite is the point: CI, and the push gate that mirrors it (pre-commit
# exports `PRE_COMMIT` into every hook's environment).
FULL_SUITE_ENV = ("CI", "PRE_COMMIT")
# Where a changed module's test is looked for: the project's suite, then the vendored
# tier's beside the scripts it tests.
TEST_DIRS = ("tests", "scripts/hooks/tests")
# The vendored tests that hold every module and instruction file to a contract -- the
# 500-line ceiling on `CLAUDE.md` and the rules, the structure ratchet, a test for each
# public symbol -- so no changed file's name maps to them, and a run of "the tests for
# what changed" skipped exactly what the gate then went red on: roguelike's CLAUDE.md at
# 500 lines, social-scraper's run-tests.py past the complexity limit (2026-10-01). About
# twenty seconds together; one the project does not hold is skipped.
CONTRACT_TESTS = (
    "scripts/hooks/tests/test_repo_contract.py",
    "scripts/hooks/tests/test_structure_check.py",
    "scripts/hooks/tests/test_untested_symbols.py",
)
# What the `[frontend]` tier's tests are found from. Only a `.py` names a test by its
# stem, so a TypeScript-only change used to print "no test named for" each file and run
# nothing, with the vitest tier switched on (roguelike, bfbdaba6).
FRONTEND_SUFFIXES = (
    ".ts",
    ".tsx",
    ".mts",
    ".cts",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".vue",
    ".svelte",
)
# The vendored reader of `.devkit.toml`, which owns the tier's defaults.
HARNESS_CONFIG = Path("scripts") / "hooks" / "harness_config.py"
# How much of a failed vitest run the artifact keeps: its summary is at the end.
VITEST_TAIL_LINES = 120

# Per-failure line cap. Chosen to hold a first-party traceback plus the assertion
# without letting a single deep failure crowd out the rest of the run.
MAX_LINES_PER_FAILURE = 25

# pytest's EXIT_NOTESTSCOLLECTED, which is not a failure of this runner. It matters
# because `stop.py` calls this script with explicit targets (the changed files under
# tests/): editing a helper that holds no tests of its own — a conftest.py, a support
# module — collects nothing, and reporting that as a failure blocks the stop with "no
# tests ran", which no source edit can resolve.
PYTEST_NO_TESTS_COLLECTED = 5


def filter_output(raw: str) -> str:
    """Keep the failure sections; drop passing noise and third-party frames.

    Pure, so it is unit-testable without running pytest.
    """
    lines = raw.splitlines()
    keep: list[str] = []
    in_failures = False
    for line in lines:
        if "=== FAILURES ===" in line or "= FAILURES =" in line:
            in_failures = True
        if "= short test summary info =" in line:
            in_failures = True
        if in_failures:
            # Library internals are noise: an agent cannot fix a frame inside
            # site-packages, and a 100-line SQLAlchemy traceback hides the one
            # first-party frame that matters.
            if "site-packages" in line or "/lib/python" in line:
                continue
            keep.append(line)
    return "\n".join(keep).strip()


SUMMARY_BANNER = "= short test summary info ="

# The short summary is the run's index -- one `FAILED <id>` line per failure, which is
# what devkit's fix pass builds a fix prompt's test list from -- so it is never capped
# as a block. Riding on the last failure's block, it lost every FAILED line after the
# first long message to that block's cap. Each entry keeps its headline and this many
# of the lines its message continues on; the whole message is in the block above.
SUMMARY_LINES_PER_ENTRY = 3


def cap_failure_blocks(text: str, limit: int = MAX_LINES_PER_FAILURE) -> str:
    """Truncate each `___ test_name ___` block to `limit` lines, noting the cut, and
    each short-summary entry to its headline plus `SUMMARY_LINES_PER_ENTRY` lines."""
    lines = text.splitlines()
    cut = next((i for i, line in enumerate(lines) if SUMMARY_BANNER in line), len(lines))
    return "\n".join([*_cap_blocks(lines[:cut], limit), *_cap_summary(lines[cut:])])


def _cap_summary(lines: list[str]) -> list[str]:
    """The summary with every entry's headline kept; an indented continuation is capped."""
    out: list[str] = []
    extra = kept = 0
    for line in lines:
        if line and not line[0].isspace():
            out.extend(_more(extra))
            out.append(line)
            extra = kept = 0
        elif kept < SUMMARY_LINES_PER_ENTRY:
            out.append(line)
            kept += 1
        else:
            extra += 1
    return out + _more(extra)


def _more(count: int) -> list[str]:
    return [f"  ... ({count} more lines, truncated)"] if count else []


def _cap_blocks(lines: list[str], limit: int) -> list[str]:
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line.startswith("_" * 5) and current:
            blocks.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append(current)

    out: list[str] = []
    for block in blocks:
        if len(block) > limit:
            out.extend(block[:limit])
            out.append(f"... ({len(block)} lines total, truncated)")
        else:
            out.extend(block)
    return out


def default_branch(root: Path, run=subprocess.run) -> str:
    """`origin/HEAD`'s branch, else whichever of `main` and `master` origin has, else `main`."""

    def git(*args: str):
        return run(["git", *args], cwd=root, capture_output=True, text=True, check=False)

    head = git("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    ref = (head.stdout or "").strip()
    if head.returncode == 0 and ref.startswith("refs/remotes/origin/"):
        return ref.rsplit("/", 1)[1]
    for candidate in ("main", "master"):
        if (
            git("rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{candidate}").returncode
            == 0
        ):
            return candidate
    return "main"


def changed_paths(root: Path, run=subprocess.run) -> list[str] | None:
    """Every path changed since the branch left origin's default: committed, staged,
    unstaged and untracked. None when git cannot say -- no repository, no origin."""

    def git(*args: str):
        return run(["git", *args], cwd=root, capture_output=True, text=True, check=False)

    base = git("merge-base", "HEAD", f"origin/{default_branch(root, run)}")
    if base.returncode != 0 or not (base.stdout or "").strip():
        return None
    diff = git("diff", "--name-only", base.stdout.strip())
    untracked = git("ls-files", "--others", "--exclude-standard")
    if diff.returncode != 0 or untracked.returncode != 0:
        return None
    seen = (diff.stdout or "").splitlines() + (untracked.stdout or "").splitlines()
    return sorted({line.strip() for line in seen if line.strip()})


def tests_for(paths: list[str], root: Path = REPO_ROOT) -> tuple[list[str], list[str]]:
    """`(test files to run, changed files that name none)`.

    A test file names itself; any other `.py` names `test_<stem>.py` in each of
    `TEST_DIRS` with hyphens read as underscores, when that file exists. Everything
    else -- a document, a migration, a module with no test of its own -- is reported so
    the caller can see what the run did not cover.
    """
    tests: list[str] = []
    unnamed: list[str] = []
    for path in paths:
        posix = path.replace("\\", "/")
        found = [name for name in _named_tests(posix) if (root / name).is_file()]
        tests.extend(name for name in found if name not in tests)
        if not found:
            unnamed.append(posix)
    return tests, unnamed


def with_contracts(tests: list[str], root: Path = REPO_ROOT) -> list[str]:
    """`tests` followed by every `CONTRACT_TESTS` file `root` holds that it lacks."""
    extra = [t for t in CONTRACT_TESTS if t not in tests and (root / t).is_file()]
    return [*tests, *extra]


def by_test_root(targets: list[str]) -> list[list[str]]:
    """`targets` split into one list per `TEST_DIRS` root, in first-seen order.

    Each root may hold a top-level `conftest.py`, and pytest's default import mode loads
    every one of them as the single module `conftest`: handed both trees in one process,
    `tests/`'s `from conftest import IsolatedSettings` resolved against
    `scripts/hooks/tests/conftest.py`, and every run errored with no test collected
    (social-scraper, 659c4f62). One process per root is how the gate runs them anyway.
    """
    roots = sorted(TEST_DIRS, key=len, reverse=True)
    groups: dict[str, list[str]] = {}
    for target in targets:
        posix = target.replace("\\", "/")
        root = next((d for d in roots if posix.startswith(f"{d}/")), "")
        groups.setdefault(root, []).append(target)
    return list(groups.values())


def _named_tests(posix: str) -> list[str]:
    """The test files the changed file `posix` could name, existing or not."""
    stem = posix.rsplit("/", 1)[-1]
    if not stem.endswith(".py"):
        return []
    if stem.startswith("test_") and any(posix.startswith(f"{d}/") for d in TEST_DIRS):
        return [posix]
    return [f"{d}/test_{stem[:-3].replace('-', '_')}.py" for d in TEST_DIRS]


def frontend_tier(root: Path) -> tuple[str, str] | None:
    """`(dir, src)` of the `[frontend]` tier where `.devkit.toml` turns it on, else None.

    Read through the vendored `harness_config`, which owns the tier's defaults; a tree
    without it has no tier to read.
    """
    path = root / HARNESS_CONFIG
    spec = importlib.util.spec_from_file_location("_run_tests_harness_config", path)
    if spec is None or spec.loader is None or not path.is_file():
        return None
    module = importlib.util.module_from_spec(spec)
    # Registered before it runs: `@dataclass` looks its defining module up by name.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        front = module.load(root).frontend
    except (ImportError, OSError, AttributeError):
        return None
    return (front.dir, front.src) if front.enabled else None


def frontend_sources(paths: list[str], src: str) -> list[str]:
    """The changed paths under the tier's `src` that vitest can trace to a test."""
    prefix = src.replace("\\", "/").strip("/")
    prefix = "" if prefix in ("", ".") else prefix + "/"
    return [
        path
        for path in (p.replace("\\", "/") for p in paths)
        if path.startswith(prefix) and path.endswith(FRONTEND_SUFFIXES)
    ]


def vitest_related(root: Path, front_dir: str, sources: list[str]) -> list[str] | None:
    """`vitest related --run <sources>` from the tier's own `node_modules`, or None
    where the tree has none to run -- an unprovisioned tree, which is not a pass."""
    base = root / front_dir
    vitest = shutil.which("vitest", path=str(base / "node_modules" / ".bin"))
    if not vitest:
        return None
    return [vitest, "related", "--run", *(os.path.relpath(root / s, base) for s in sources)]


def frontend_run(
    changed: list[str], unnamed: list[str], root: Path
) -> tuple[list[str], list[str], str]:
    """`(vitest command, still unnamed, failure)` for the frontend half of a change.

    The command is empty where the tier is off or nothing under it changed. `failure` is
    the artifact's text when the tier's tests are owed and cannot run: a tree with no
    `node_modules` answered "nothing to run" for a change it had not tested.
    """
    tier = frontend_tier(root)
    sources = frontend_sources(changed, tier[1]) if tier else []
    if not tier or not sources:
        return [], unnamed, ""
    rest = [path for path in unnamed if path not in sources]
    cmd = vitest_related(root, tier[0], sources)
    if cmd is None:
        where = (root / tier[0] / "node_modules" / ".bin").relative_to(root).as_posix()
        failure = (
            f"# vitest: {len(sources)} frontend file(s) changed and {where} has no vitest.\n"
            "# fix: provision the tree (`npm ci` in the frontend dir), then run this again.\n"
        )
        return [], rest, failure
    print(f"run-tests: vitest related for {len(sources)} frontend file(s)")
    return cmd, rest, ""


def run_vitest(cmd: list[str], cwd: Path) -> str:
    """Run `cmd`; the artifact's text for a failure, empty for a pass."""
    print(f"run-tests: vitest {' '.join(cmd[1:])}")
    try:
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
    except OSError as exc:
        return f"# vitest could not start: {exc}\n"
    if result.returncode == 0:
        return ""
    tail = (result.stdout + result.stderr).strip().splitlines()[-VITEST_TAIL_LINES:]
    return "# source: vitest related\n# fix: npx vitest related --run <file>\n" + "\n".join(tail)


def with_basetemp(cmd: list[str], basetemp: str) -> list[str]:
    """`cmd` given a pytest temp root of its own, right after `-m pytest`.

    Left to itself pytest roots every run on the machine under one `pytest-of-<user>`,
    and on the way out stats every link there -- `pytest-current` included, which a run
    in another session is replacing or holding. On Windows that stat is an access-denied
    error raised after the last test, so a green suite exits 1 with no failed test to
    name. A directory only this run knows about cannot be contended; the caller creates
    and removes it.
    """
    return [*cmd[:3], f"--basetemp={basetemp}", *cmd[3:]]


def rerun_in_tree_venv(module: str, argv: list[str] | None) -> int | None:
    """This run's exit code under the tree's `.venv`, or None to carry on here.

    `python scripts/run-tests.py` from a fresh `claude --worktree` tree runs an
    interpreter with no pytest and a tree with no `.venv`. The vendored
    `scripts/hooks/toolchain.py` provisions it with this project's own install command
    and runs this script again inside it; where this interpreter already has `module`,
    or the vendored copy predates that, nothing changes.
    """
    hooks = REPO_ROOT / "scripts" / "hooks"
    if not (hooks / "toolchain.py").is_file():
        return None
    sys.path.insert(0, str(hooks))
    try:
        import toolchain

        rerun = toolchain.rerun_in_venv
    except (ImportError, AttributeError):
        return None
    return rerun(
        REPO_ROOT, module, Path(__file__).resolve(), sys.argv[1:] if argv is None else argv
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="run the whole suite")
    parser.add_argument("--changed", action="store_true", help="run pytest's last-failed subset")
    args, extra = parser.parse_known_args(argv)
    # After parsing, so `--help` answers without installing anything.
    rerun = rerun_in_tree_venv("pytest", argv)
    if rerun is not None:
        return rerun
    targets = [a for a in extra if a]

    cmd = [sys.executable, "-m", "pytest", "--tb=short", "-q"]
    if args.changed:
        cmd += ["--last-failed", "--last-failed-no-failures", "all"]
    whole = args.all or args.changed or targets or any(os.environ.get(k) for k in FULL_SUITE_ENV)
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    plan = None if whole else _plan_changed(REPO_ROOT)
    if plan is None:
        return _finish([run_pytest(cmd + targets)])
    return _run_planned(cmd, *plan)


# `main`'s helpers are out of it for `structure_check`'s complexity limit, which the
# project's own gate holds this file to: it is the project's, rendered from devkit's
# template, so a branch added here reddens every project that refreshes it.


def _plan_changed(root: Path) -> tuple[list[str], list[str], list[str]] | None:
    """`(pytest targets, vitest command, failures)` for what changed; None runs the suite."""
    changed = changed_paths(root)
    if changed is None:
        print("run-tests: git cannot say what changed; running the suite")
        return None
    targets, unnamed = tests_for(changed, root)
    front, unnamed, owed = frontend_run(changed, unnamed, root)
    print(
        f"run-tests: {len(targets)} test file(s) for {len(changed)} changed path(s), "
        "and the contract tests; --all runs the suite"
    )
    if changed:
        targets = with_contracts(targets, root)
    for path in unnamed:
        print(f"run-tests:   no test named for {path}")
    return targets, front, [owed] if owed else []


def _run_planned(cmd: list[str], targets: list[str], front: list[str], failures: list[str]) -> int:
    """Run the tests `_plan_changed` named, and nothing where it named none."""
    if not targets and not front and not failures:
        ARTIFACT.write_text("", encoding="utf-8")
        print("run-tests: nothing to run (artifact cleared)")
        return 0
    failures = [*failures, *(run_pytest(cmd + group) for group in by_test_root(targets))]
    if front:
        failures = [*failures, run_vitest(front, REPO_ROOT)]
    return _finish(failures)


def _finish(failures: list[str]) -> int:
    """Write the artifact for what failed -- an empty text is a pass -- and the exit code."""
    failures = [text for text in failures if text]
    if not failures:
        # Clear on pass, so a stale artifact never sends the next agent chasing a
        # failure that is already fixed.
        ARTIFACT.write_text("", encoding="utf-8")
        print(f"run-tests: passed (artifact cleared: {ARTIFACT.relative_to(REPO_ROOT)})")
        return 0
    ARTIFACT.write_text("\n\n".join(failures) + "\n", encoding="utf-8")
    print(f"run-tests: FAILED — details in {ARTIFACT.relative_to(REPO_ROOT)}")
    return 1


def run_pytest(cmd: list[str]) -> str:
    """Run pytest; the artifact's text for a failure, empty for a pass."""
    print(f"run-tests: {' '.join(cmd[2:])}")
    with tempfile.TemporaryDirectory(prefix="pytest-", ignore_cleanup_errors=True) as basetemp:
        run = with_basetemp(cmd, basetemp)
        result = subprocess.run(run, cwd=REPO_ROOT, capture_output=True, text=True)
    raw = result.stdout + result.stderr
    if result.returncode in (0, PYTEST_NO_TESTS_COLLECTED):
        return ""
    body = cap_failure_blocks(filter_output(raw))
    # Never leave the agent with nothing: if filtering stripped everything (an
    # unexpected pytest output shape, a collection error), fall back to raw.
    if not body.strip():
        body = raw.strip()
    return "# source: scripts/run-tests.py\n# fix: pytest <the failing test id> --tb=long\n" + body


if __name__ == "__main__":
    sys.exit(main())
