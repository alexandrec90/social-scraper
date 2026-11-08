#!/usr/bin/env python3
"""Tidy the default branch after a merge: formatters, baselines, mirrors.

In the direct flow nothing formats a change before it lands, so this runs on what landed,
in `dependabot-automerge.yml`'s `tidy` job, and pushes the result straight to the default
branch. Merging waits for none of it. `PR Gate` is started only after it, so the gate
judges the tidied tree, and a formatter's unfinished work never reads as a red `main`.

Pushed rather than opened as a PR because there is nothing here to review or queue, and a
rewrite of this kind has no later moment that would suit it better. The subject opens with
`Tidy:`, which is how a search for the commit that broke `main` knows to step over it.

A push that loses to a merge is rebased and tried again. One that conflicts is dropped: the
next merge starts this job again, over a tree that includes both. A push still refused after
that is not a race, so the job goes red rather than call it left for later. A workflow file
is never pushed, because `GITHUB_TOKEN` is refused for that whole push and the rest would go
with it.

Vendored from devkit (`sync-devkit.py`'s MANIFEST) and byte-identical everywhere: what to
format is each project's `.pre-commit-config.yaml`, read by `ship.py --tidy`. Tested in
`scripts/hooks/tests/test_tidy_main.py`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

SUBJECT = "Tidy: formatters, baselines and mirrors"
WORKFLOWS = ".github/workflows"
BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
PUSH_TRIES = 4
CONFLICTED = "a merge conflicts with it"


def git(run, *args: str):
    return run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False)


def push(run, branch: str) -> str:
    """Push the tidy commit: "" when it landed, else why it did not."""
    said = ""
    for _attempt in range(PUSH_TRIES):
        done = git(run, "push", "origin", f"HEAD:{branch}")
        if done.returncode == 0:
            return ""
        said = (done.stderr or done.stdout or "").strip()
        if git(run, "pull", "--rebase", "origin", branch).returncode != 0:
            git(run, "rebase", "--abort")
            return f"{CONFLICTED}: {said}"
    return f"still refused after {PUSH_TRIES} tries: {said}"


def main(env: dict[str, str] | None = None, run=None) -> int:
    env = dict(os.environ) if env is None else env
    run = subprocess.run if run is None else run
    branch = env.get("TIDY_BRANCH", "").strip()
    if not branch:
        print("TIDY_BRANCH is empty -- refusing to guess where to push.", file=sys.stderr)
        return 1

    tidied = run([sys.executable, "scripts/ship.py", "--tidy"], cwd=REPO_ROOT, check=False)
    if tidied.returncode != 0:
        print(f"ship.py --tidy exited {tidied.returncode}; nothing pushed.", file=sys.stderr)
        return 1
    git(run, "checkout", "--", WORKFLOWS)
    if not (git(run, "status", "--porcelain").stdout or "").strip():
        print("Nothing to tidy.")
        return 0

    for key, value in (("user.name", BOT_NAME), ("user.email", BOT_EMAIL)):
        git(run, "config", key, value)
    git(run, "add", "-A")
    committed = git(run, "commit", "--message", SUBJECT)
    if committed.returncode != 0:
        print(f"Could not commit: {committed.stderr}", file=sys.stderr)
        return 1
    why = push(run, branch)
    if why.startswith(CONFLICTED):
        # Not a failure of anything: the next merge's tidy covers this one's files too.
        print(f"Left for the next tidy: {why}")
        return 0
    if why:
        print(f"Could not push: {why}", file=sys.stderr)
        return 1
    print(f"Tidied {branch}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
