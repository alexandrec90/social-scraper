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

That event is not enough alone. A gate the workflow's own merge dispatched completes
without raising a `workflow_run`: on 2026-11-23 devkit's `green` sat two merges behind a
default branch whose gate had passed on both, for eleven hours, and the harness kept
running the code a merged fix had replaced. So the scheduled run asks which commit
passed (`CATCH_UP=true`, `catch_up`) and needs no event to tell it.

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

# How `advance` ends a line that moved nothing because the commit was the wrong one, not
# because `green` was already right: `catch_up` tries the next commit after either.
LATE = "a late run moves nothing"
OFF_BRANCH = "stays where it is"

# The gate's file, the path every consumer's lives at, and the events a run of the
# default branch's own commit arrives by. How many runs `catch_up` reads: each commit
# costs up to three calls, and `green` trails by a merge or two, never by a page.
GATE_FILE = "pr-gate.yml"
GATE_EVENTS = ("push", "workflow_dispatch")
CATCH_UP_RUNS = 10


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


def passed(repo: str, base: str, gate: str, run) -> list[str]:
    """The commits of `base` whose gate run passed, newest run first, each once.

    Only a `push` or `workflow_dispatch` run: a fork's branch can share the name, and its
    run arrives as a `pull_request`."""
    path = f"repos/{repo}/actions/workflows/{gate}/runs?branch={base}&status=success"
    payload = json.loads(gh(["api", f"{path}&per_page={CATCH_UP_RUNS}"], run) or "{}")
    found: list[str] = []
    for entry in payload.get("workflow_runs") or []:
        sha = str(entry.get("head_sha") or "")
        if sha and sha not in found and entry.get("event") in GATE_EVENTS:
            found.append(sha)
    return found


def catch_up(repo: str, base: str, gate: str, run) -> str:
    """Move `green` to the newest commit of `base` whose gate passed, with no event to
    name it: a line saying what happened.

    A gate the merge job dispatched completes without raising a `workflow_run`, so the
    run that should have moved `green` never starts. The scheduled pass asks instead.
    Newest first, and the first that is not behind `green` settles it: a re-run of an
    older commit can be the newest run, and what passed before it is still ahead."""
    line = f"no gate run has passed on {base}; {GREEN} stays where it is"
    for sha in passed(repo, base, gate, run):
        line = advance(repo, sha, base, run)
        if not line.endswith((LATE, OFF_BRANCH)):
            break
    return line


def advance(repo: str, sha: str, base: str, run) -> str:
    """Move `green` to `sha` if that is forward: a line saying what happened."""
    if relation(repo, sha, base, run) not in (AHEAD, IDENTICAL):
        return f"{sha[:9]} is not on {base}; {GREEN} {OFF_BRANCH}"
    current = current_green(repo, run)
    if not current:
        path = f"repos/{repo}/git/refs"
        ref = f"ref=refs/heads/{GREEN}"
        gh(["api", "--method", "POST", path, "-f", ref, "-f", f"sha={sha}"], run)
        return f"{GREEN} created at {sha[:9]}"
    if current == sha:
        return f"{GREEN} is already at {sha[:9]}"
    if relation(repo, current, sha, run) != AHEAD:
        return f"{GREEN} is at {current[:9]}, past {sha[:9]}; {LATE}"
    path = f"repos/{repo}/git/refs/heads/{GREEN}"
    gh(["api", "--method", "PATCH", path, "-f", f"sha={sha}", "-F", "force=false"], run)
    return f"{GREEN} moved {current[:9]}..{sha[:9]}"


def main(env: dict[str, str] | None = None, run=None) -> int:
    env = dict(os.environ) if env is None else env
    run = subprocess.run if run is None else run
    repo = env.get("GH_REPO", "").strip()
    sha = env.get("RUN_HEAD_SHA", "").strip()
    base = env.get("DEFAULT_BRANCH", "").strip()
    if repo and base and not sha and env.get("CATCH_UP", "").strip() == "true":
        print(catch_up(repo, base, env.get("GATE_FILE", "").strip() or GATE_FILE, run))
        return 0
    if not (repo and sha and base):
        print(
            "GH_REPO, RUN_HEAD_SHA and DEFAULT_BRANCH are all required "
            "(CATCH_UP=true stands in for RUN_HEAD_SHA).",
            file=sys.stderr,
        )
        return 1
    print(advance(repo, sha, base, run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
