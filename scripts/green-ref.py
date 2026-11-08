#!/usr/bin/env python3
"""Fast-forward `green`, the newest commit of the default branch whose gate passed.

Merging into the default branch stops meaning "this works" once a project checks a change
after it lands, so something else has to say that. `green` does, and whatever must not act
on an unjudged commit -- the harness updating itself, a deploy, a queued rebuild -- reads it
instead of the tip.

`dependabot-automerge.yml` runs this when `PR Gate` passes on a commit of the default
branch. A branch rather than a tag, because it moves and because GitHub itself refuses to
move a branch backwards when asked with `force=false`. A run that finishes late, for a
commit `green` has already passed, leaves it where it is: two runs finish in any order.

Every project has one whichever flow it is in; it costs one API call and nothing reads it
until something is told to.

Vendored from devkit (`sync-devkit.py`'s MANIFEST) and byte-identical everywhere: all it
knows arrives in the environment. Tested in `scripts/hooks/tests/test_green_ref.py`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

GREEN = "green"

# `compare/{a}...{b}` answers how `b` stands to `a`.
AHEAD = "ahead"
IDENTICAL = "identical"


class GhError(RuntimeError):
    """A `gh` invocation that failed, with its stderr attached."""


def gh(args: list[str], run) -> str:
    result = run(["gh", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise GhError(f"gh {' '.join(args)} exited {result.returncode}: {result.stderr.strip()}")
    return result.stdout


def relation(repo: str, older: str, newer: str, run) -> str:
    """How `newer` stands to `older`: `ahead`, `identical`, `behind` or `diverged`."""
    payload = json.loads(gh(["api", f"repos/{repo}/compare/{older}...{newer}"], run) or "{}")
    return str(payload.get("status") or "")


def current_green(repo: str, run) -> str:
    """The commit `green` is at; "" when it does not exist yet. Any other failure raises:
    a read that failed is not a branch that is missing, and creating one over it would
    be refused anyway."""
    try:
        payload = json.loads(gh(["api", f"repos/{repo}/git/ref/heads/{GREEN}"], run) or "{}")
    except GhError as failed:
        if "404" in str(failed):
            return ""
        raise
    return str((payload.get("object") or {}).get("sha") or "")


def advance(repo: str, sha: str, base: str, run) -> str:
    """Move `green` to `sha` if that is forward: a line saying what happened."""
    if relation(repo, sha, base, run) not in (AHEAD, IDENTICAL):
        return f"{sha[:9]} is not on {base}; {GREEN} stays where it is"
    current = current_green(repo, run)
    if not current:
        path = f"repos/{repo}/git/refs"
        ref = f"ref=refs/heads/{GREEN}"
        gh(["api", "--method", "POST", path, "-f", ref, "-f", f"sha={sha}"], run)
        return f"{GREEN} created at {sha[:9]}"
    if current == sha:
        return f"{GREEN} is already at {sha[:9]}"
    if relation(repo, current, sha, run) != AHEAD:
        return f"{GREEN} is at {current[:9]}, past {sha[:9]}; a late run moves nothing"
    path = f"repos/{repo}/git/refs/heads/{GREEN}"
    gh(["api", "--method", "PATCH", path, "-f", f"sha={sha}", "-F", "force=false"], run)
    return f"{GREEN} moved {current[:9]}..{sha[:9]}"


def main(env: dict[str, str] | None = None, run=None) -> int:
    env = dict(os.environ) if env is None else env
    run = subprocess.run if run is None else run
    repo = env.get("GH_REPO", "").strip()
    sha = env.get("RUN_HEAD_SHA", "").strip()
    base = env.get("DEFAULT_BRANCH", "").strip()
    if not (repo and sha and base):
        print("GH_REPO, RUN_HEAD_SHA and DEFAULT_BRANCH are all required.", file=sys.stderr)
        return 1
    print(advance(repo, sha, base, run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
