#!/usr/bin/env python3
"""Run the application test suite and write failures to a parseable artifact.

Same contract as `lint-all.py`: the agent fixing a failure reads
`logs/test-failures.log`, not the terminal. Each failure block is capped so one
broken test cannot flood the artifact and bury the other twenty.

Usage:
    python scripts/run-tests.py             # the whole application suite
    python scripts/run-tests.py --changed   # pytest's last-failed subset
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACT = REPO_ROOT / "logs" / "test-failures.log"

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


def cap_failure_blocks(text: str, limit: int = MAX_LINES_PER_FAILURE) -> str:
    """Truncate each `___ test_name ___` block to `limit` lines, noting the cut."""
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in text.splitlines():
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
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--changed", action="store_true", help="run pytest's last-failed subset")
    args, extra = parser.parse_known_args(argv)

    cmd = [sys.executable, "-m", "pytest", "--tb=short", "-q"]
    if args.changed:
        cmd += ["--last-failed", "--last-failed-no-failures", "all"]
    cmd += [a for a in extra if a]

    print(f"run-tests: {' '.join(cmd[2:])}")
    result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
    raw = result.stdout + result.stderr

    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    if result.returncode in (0, PYTEST_NO_TESTS_COLLECTED):
        # Clear on pass, so a stale artifact never sends the next agent chasing a
        # failure that is already fixed.
        ARTIFACT.write_text("", encoding="utf-8")
        print(f"run-tests: passed (artifact cleared: {ARTIFACT.relative_to(REPO_ROOT)})")
        return 0

    body = cap_failure_blocks(filter_output(raw))
    # Never leave the agent with nothing: if filtering stripped everything (an
    # unexpected pytest output shape, a collection error), fall back to raw.
    if not body.strip():
        body = raw.strip()
    ARTIFACT.write_text(
        "# source: scripts/run-tests.py\n"
        "# fix: pytest <the failing test id> --tb=long\n" + body + "\n",
        encoding="utf-8",
    )
    print(f"run-tests: FAILED — details in {ARTIFACT.relative_to(REPO_ROOT)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
