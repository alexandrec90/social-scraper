#!/usr/bin/env python3
"""Retry automerge-labelled PRs the event-driven job dropped.

**The failure this exists for.** `dependabot-automerge.yml`'s merge job is a one-shot
reaction to a `workflow_run` completion: the gate goes green, the job fires once, and if
that single attempt fails for any reason the PR is stranded for good. Nothing re-runs it,
because nothing will complete that `PR Gate` run a second time.

It is not a theoretical window. On 2026-08-17 GitHub's GraphQL API served 503s for
roughly an hour while REST stayed up. Two `sports_betting` PRs were labelled `automerge`,
had a clean gate, and their merge job died on `HTTP 503` from `gh pr merge` -- which
speaks GraphQL. Both sat open afterwards looking exactly like PRs nobody had got to, with
no failing check on them and the auto-merge run's red buried in the Actions tab.

So this is the same lesson as `report-workflow-failure.py`'s sweep, one mechanism over:
**an event handler cannot be the only path to a state you care about.** A scheduled pass
that reconciles the world against what it should be picks up whatever the event dropped,
whether it was dropped by an outage, a rate limit, a cancelled run or a race.

**Everything here is REST.** That is the other half of the fix rather than an
implementation detail: the outage that stranded those PRs took out GraphQL only, and
`gh pr list` / `gh pr merge` are both GraphQL. Every call below goes through `gh api` to
a REST endpoint, so the retry survives the exact failure that created the work for it.

**The guards are re-derived, never inherited.** A schedule carries no PR, so each
candidate is re-checked from scratch: carrying the `automerge` label, and with a
*successful `PR Gate` run on its current head SHA*. That last one is what keeps this
honest -- it is the same condition the event job waits for, so the sweep can only ever
complete a merge that was already earned, and a push to the branch moves the head past
the gated SHA and disqualifies it. There is deliberately no author guard: only write
access can apply the label, so the label is the whole authorization, whoever wrote the
PR -- Dependabot bumps, devkit upgrades, anything a human marks routine.

Vendored from devkit (`sync-devkit.py`'s MANIFEST) and byte-identical everywhere: the
repository arrives in the environment and the gate's title is one every project shares.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

# The label `dependabot-automerge.yml`'s classify job applies to routine Dependabot
# bumps, and anything with write access may apply by hand. Only a PR carrying it is a
# candidate here -- this file never classifies, because the Dependabot metadata that
# decides the label is available on the pull_request event and nowhere else.
AUTOMERGE_LABEL = "automerge"

# The gate whose success the event-driven job waits on. Vendored byte-identical, so the
# title cannot be parameterised; `test_ci_workflow_contract.py` requires it per project.
GATE_WORKFLOW = "PR Gate"

# Check conclusions that are not an objection. `neutral` and `skipped` are how a job that
# opted out of this PR reports, and treating either as a failure would block every repo
# whose gate has a conditional job.
PASSING_CONCLUSIONS = ("success", "skipped", "neutral")

# How many open PRs to scan. A repo with more open PRs than this simply reconciles the
# first page and picks the rest up on the next pass -- the sweep is idempotent, so a
# partial pass costs a delay and never a wrong decision.
PR_SCAN_LIMIT = 100


class GhError(RuntimeError):
    """A `gh` invocation that failed, with its stderr attached."""


def gh(args: list[str], run) -> str:
    """Run `gh` and return stdout, raising with stderr attached on a non-zero exit."""
    result = run(["gh", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise GhError(f"gh {' '.join(args)} exited {result.returncode}: {result.stderr.strip()}")
    return result.stdout


def gh_json(path: str, run):
    """GET a REST path and parse it. Never GraphQL -- see the module docstring."""
    return json.loads(gh(["api", path], run) or "null")


def labels_of(pr: dict) -> list[str]:
    return [label.get("name", "") for label in pr.get("labels") or []]


def gate_passed(repo: str, sha: str, run) -> bool:
    """True when `PR Gate` has a successful `pull_request` run on exactly this SHA.

    Scoped to the SHA rather than the branch, and to `pull_request` rather than any event,
    because both are load-bearing. A branch-scoped answer would let an older commit's green
    gate merge code it never ran against, and a hand-dispatched `workflow_dispatch` run of
    the gate is not the same evidence -- it is somebody re-running a workflow, possibly on
    a different ref, and the event job has never accepted it either.
    """
    if not sha:
        return False
    payload = gh_json(f"repos/{repo}/actions/runs?head_sha={sha}&per_page=100", run) or {}
    for entry in payload.get("workflow_runs") or []:
        if (
            entry.get("name") == GATE_WORKFLOW
            and entry.get("event") == "pull_request"
            and entry.get("conclusion") == "success"
        ):
            return True
    return False


def blocking_checks(repo: str, sha: str, run) -> list[str]:
    """Names of check runs on `sha` that are unfinished or failed.

    The gate's own success is checked separately; this is the belt to that braces. A repo
    can wire a second required workflow onto Dependabot PRs -- a lock repair, a licence
    scan -- and a green `PR Gate` says nothing about it. The event-driven job merges on
    the gate alone, which is right for it: it fires *because* the gate completed and has
    no other moment to wait for. A scheduled pass has no such excuse.
    """
    payload = gh_json(f"repos/{repo}/commits/{sha}/check-runs?per_page=100", run) or {}
    blocking = []
    for check in payload.get("check_runs") or []:
        if check.get("status") != "completed":
            blocking.append(f"{check.get('name')} (still {check.get('status')})")
        elif check.get("conclusion") not in PASSING_CONCLUSIONS:
            blocking.append(f"{check.get('name')} ({check.get('conclusion')})")
    return blocking


def merge_pr(repo: str, number: int, run) -> None:
    """Merge via REST.

    `gh pr merge` is GraphQL, and a GraphQL outage is the exact failure that created this
    file's workload. Using it here would make the retry share the fate of the attempt it
    is retrying.
    """
    gh(
        [
            "api",
            "--method",
            "PUT",
            f"repos/{repo}/pulls/{number}/merge",
            "-f",
            "merge_method=merge",
        ],
        run,
    )


def verdict(repo: str, pr: dict, run, env_sha: str = "") -> tuple[bool, str]:
    """`(should_merge, why)` for one open PR. Every guard re-derived from scratch.

    `env_sha` is the commit the triggering gate run actually tested, and is passed only in
    branch mode; the sweep has no such commit and judges the head as it finds it.
    """
    # No author guard, deliberately: only write access can apply the label, so the label
    # is the whole authorization -- see the module docstring. The head-SHA check below is
    # what defuses a stray human commit on the branch, not the author field.
    if AUTOMERGE_LABEL not in labels_of(pr):
        return False, f"not labelled {AUTOMERGE_LABEL!r} (unclassified, or held for review)"
    if pr.get("draft"):
        return False, "still a draft"
    # Anything but an explicit `true` declines. `false` is a conflict; `null` means GitHub
    # has not finished computing the merge commit yet, which is a "come back later" rather
    # than a refusal -- and the next hourly pass is exactly that. Testing `is False` here
    # instead would merge on the undecided case, which is the one where nobody, including
    # GitHub, yet knows whether the merge is clean.
    if pr.get("mergeable") is not True:
        state = pr.get("mergeable_state") or "undecided"
        return False, f"not known-mergeable ({state})"
    sha = ((pr.get("head") or {}).get("sha")) or ""
    # Branch mode only. The event fired for one commit, and by the time this runs the
    # branch may already carry another -- a Dependabot rebase, or a human pushing to a
    # `dependabot/` branch to inherit the merge. Either way the gate that just passed did
    # not run against what would be merged, and the newer head's own gate run owns it.
    gated = (env_sha or "").strip()
    if gated and sha != gated:
        return False, f"head moved to {sha[:7]}, past the gated {gated[:7]}"
    if not gate_passed(repo, sha, run):
        return False, f"no successful {GATE_WORKFLOW} run on {sha[:7]}"
    blocking = blocking_checks(repo, sha, run)
    if blocking:
        return False, f"checks not all green: {', '.join(blocking)}"
    return True, f"{GATE_WORKFLOW} passed on {sha[:7]} and it is labelled {AUTOMERGE_LABEL}"


def open_prs(repo: str, run) -> list[dict]:
    listing = gh_json(f"repos/{repo}/pulls?state=open&per_page={PR_SCAN_LIMIT}", run) or []
    return [entry for entry in listing if isinstance(entry, dict)]


def prs_for_branch(repo: str, branch: str, run) -> list[dict]:
    """Open PRs whose head is `branch`.

    Filtered by the API rather than by scanning `open_prs`, so a repository with more
    open PRs than one page cannot hide the branch the event named.
    """
    owner = repo.split("/", 1)[0]
    listing = gh_json(f"repos/{repo}/pulls?state=open&head={owner}:{branch}&per_page=10", run) or []
    return [entry for entry in listing if isinstance(entry, dict)]


def candidates(env: dict[str, str], repo: str, run) -> tuple[list[dict], str]:
    """The PRs to consider, and a word for which mode produced them.

    Branch mode is the `workflow_run` path: the event names a branch, and only that
    branch's PR is in scope. Sweep mode is the scheduled retry, where every open
    open PR is a candidate because there is no event to narrow it.
    """
    branch = env.get("HEAD_BRANCH", "").strip()
    if branch:
        return prs_for_branch(repo, branch, run), f"branch {branch!r}"
    return open_prs(repo, run), "sweep"


def main(env: dict[str, str] | None = None, run=None) -> int:
    env = dict(os.environ) if env is None else env
    run = subprocess.run if run is None else run

    repo = env.get("GH_REPO", "").strip()
    if not repo:
        print("GH_REPO is empty -- refusing to guess which repository to sweep.", file=sys.stderr)
        return 1

    found, scope = candidates(env, repo, run)
    # Prefiltered on the label so the per-PR detail read below only spends a call on
    # actual candidates; `verdict` re-checks it against the fresh detail regardless.
    considered = [pr for pr in found if AUTOMERGE_LABEL in labels_of(pr)]
    if not considered:
        print(f"No open {AUTOMERGE_LABEL!r}-labelled PRs for {scope}; nothing to do.")
        return 0

    gated_sha = env.get("RUN_HEAD_SHA", "").strip()
    merged = 0
    for pr in considered:
        number = pr.get("number")
        if number is None:
            print("A PR arrived with no number; skipping it rather than guessing.")
            continue
        # The listing endpoint omits `mergeable`, so re-read each PR on its own. It is
        # also the freshest possible view, which matters on a pass that may have spent a
        # while on the PRs before this one.
        detail = gh_json(f"repos/{repo}/pulls/{number}", run) or pr
        should_merge, why = verdict(repo, detail, run, gated_sha)
        if not should_merge:
            print(f"#{number}: leaving open -- {why}.")
            continue
        print(f"#{number}: {why}; merging.")
        merge_pr(repo, int(number), run)
        merged += 1

    print(f"Considered {len(considered)} automerge PR(s) for {scope}; merged {merged}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
