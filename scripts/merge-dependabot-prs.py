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
*successful `PR Gate` run on its current head SHA* (`gate_passed` says which runs
count). That last one is what keeps this
honest -- it is the same condition the event job waits for, so the sweep can only ever
complete a merge that was already earned, and a push to the branch moves the head past
the gated SHA and disqualifies it. There is deliberately no author guard: only write
access can apply the label, so the label is the whole authorization, whoever wrote the
PR -- Dependabot bumps, devkit upgrades, anything a human marks routine.

**`merge-now` is the other label, and it asks for no gate at all.** A project in the
direct flow (`[delivery] flow = "direct"` in `.devkit.toml`) checks a change after it has
landed, so every wait in front of `main` is one the person loses track of. Its PRs carry
`merge-now`, and this script drains them oldest first, one repository's at a time. A
conflicted one is passed over rather than waited on: its resolver's push fires the next
drain, and the rest of the line has no reason to stop for it.

**Everything it merges it also gets checked.** A merge made with `GITHUB_TOKEN` raises no
`push`, so without `dispatch_gate` the only run a bot merge ever got was the fix pass's
half-hourly re-gate of whatever the tip was by then, which cannot say which merge broke it.

Vendored from devkit (`sync-devkit.py`'s MANIFEST) and byte-identical everywhere: the
repository arrives in the environment and the gate's title is one every project shares.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# The label `dependabot-automerge.yml`'s classify job applies to every Dependabot PR,
# and anything with write access may apply by hand. Only a PR carrying it is a
# candidate here -- this file never labels, because the event that says Dependabot
# opened the PR is the pull_request one, and this runs on workflow_run and schedule.
AUTOMERGE_LABEL = "automerge"

# Merge with no gate: the direct flow's label, applied by the ship task and by nothing
# in this file. Spelled again in `dependabot-automerge.yml`'s `sweep` condition.
MERGE_NOW_LABEL = "merge-now"
MERGE_LABELS = (AUTOMERGE_LABEL, MERGE_NOW_LABEL)

# `green-ref.py`'s branch, which a PR's head must never be deleted as.
GREEN_BRANCH = "green"

# The gate's file, the path every consumer's lives at (`sync-devkit.py`'s `PR_GATE_FILE`):
# the dispatch endpoint takes a file name, not a workflow's title.
GATE_FILE = "pr-gate.yml"

# What became of one PR's turn in the line.
MERGED = "merged"
REFUSED = "refused"
LEFT = "left"

# What `.devkit.toml` says for a project whose checks run after merge.
DIRECT_FLOW = "direct"
# The interpreter the tidy job asks for when `.devkit.toml` names none. The setup action
# is copied rather than rendered, so it cannot know; this file cannot be rendered either.
DEFAULT_PYTHON = "3.12"

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

# GitHub computes `mergeable` lazily, on request, and forgets it whenever the base moves.
# A gate that finishes just after another PR merged reads null, and so does the hourly
# retry when merges keep coming -- devkit #405 was stranded twice that way while fixer
# PRs landed minutes apart. A few reads seconds apart get the answer.
MERGEABLE_READS = 5
MERGEABLE_WAIT = 3.0


class GhError(RuntimeError):
    """A `gh` invocation that failed, with its stderr attached.

    `stderr` alone is GitHub's answer. The message also quotes the command, so anything
    judging what GitHub said has to read this: the argv of a squash contains "squash".
    """

    def __init__(self, message: str, stderr: str = ""):
        super().__init__(message)
        self.stderr = stderr


def gh(args: list[str], run) -> str:
    """Run `gh` and return stdout, raising with stderr attached on a non-zero exit."""
    result = run(["gh", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        said = result.stderr.strip()
        raise GhError(f"gh {' '.join(args)} exited {result.returncode}: {said}", said)
    return result.stdout


def gh_json(path: str, run):
    """GET a REST path and parse it. Never GraphQL -- see the module docstring."""
    return json.loads(gh(["api", path], run) or "null")


def labels_of(pr: dict) -> list[str]:
    return [label.get("name", "") for label in pr.get("labels") or []]


def gate_passed(repo: str, sha: str, run, branch: str = "") -> bool:
    """True when `PR Gate` has a successful run on exactly this SHA, for this PR.

    Scoped to the SHA rather than the branch: a branch-scoped answer would let an older
    commit's green gate merge code it never ran against. A `pull_request` run is the
    PR's by construction. A `workflow_dispatch` run counts too, when it ran on the PR's
    own head `branch`: a push made with `GITHUB_TOKEN` -- a lock repair, a generated-file
    sync -- raises no usable `pull_request` event, so the workflow that pushed dispatches
    the gate itself, and that run is the only verdict the commit will ever get.
    carameli #393 sat open with every check green at its head because this accepted
    `pull_request` alone.
    """
    if not sha:
        return False
    payload = gh_json(f"repos/{repo}/actions/runs?head_sha={sha}&per_page=100", run) or {}
    for entry in payload.get("workflow_runs") or []:
        if entry.get("name") != GATE_WORKFLOW or entry.get("conclusion") != "success":
            continue
        event = entry.get("event")
        if event == "pull_request":
            return True
        if event == "workflow_dispatch" and branch and entry.get("head_branch") == branch:
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


def merge_pr(repo: str, number: int, run, title: str = "", body: str = "") -> None:
    """Squash-merge via REST, so one PR is one commit and reverting it is one `git revert`.

    `gh pr merge` is GraphQL, and a GraphQL outage is the exact failure that created this
    file's workload. Using it here would make the retry share the fate of the attempt it
    is retrying.

    The title and body are given because GitHub's default for a squash can be a list of
    the branch's commits, and the PR body is the only changelog a consumer gets. A merge
    commit is the answer only to a refusal of squashing itself (`squash_refused`): any
    other refusal is a PR that cannot merge right now, which a merge commit would not fix
    and which would break one PR, one commit whenever the retry happened to succeed.
    """
    path = f"repos/{repo}/pulls/{number}/merge"
    squash = ["api", "--method", "PUT", path, "-f", "merge_method=squash"]
    if title:
        squash += ["-f", f"commit_title={title} (#{number})", "-f", f"commit_message={body}"]
    try:
        gh(squash, run)
    except GhError as refused:
        if not squash_refused(refused):
            raise
        gh(["api", "--method", "PUT", path, "-f", "merge_method=merge"], run)


def squash_refused(error: GhError) -> bool:
    """GitHub refused to squash here, rather than refusing this PR.

    Two ways: the repository has squash switched off, and `GITHUB_TOKEN` writing a new
    commit that touches a workflow file, which this token is known to merge as a merge
    commit (an adoption PR always carries one).
    """
    said = error.stderr.lower()
    return "squash merges are not allowed" in said or ("403" in said and "workflow" in said)


def delete_branch(repo: str, pr: dict, run) -> None:
    """Delete a merged PR's head branch, as a squash through `gh` does with `--delete-branch`.

    A merge commit leaves the branch an ancestor of the base, so `sweep` reads a checkout
    still on it as spent. A squash does not: with the branch alive on origin the checkout
    reads as `needs-pr`, which has no way out, and its home branch stops advancing. Most
    repositories here do not delete on merge. A failure costs that one branch and says so.
    """
    head = pr.get("head") or {}
    ref = str(head.get("ref") or "")
    if not ref or ref == GREEN_BRANCH or ref == default_branch_of(pr):
        return
    if (head.get("repo") or {}).get("full_name") != repo:
        return  # a fork's branch is not this repository's to delete
    try:
        gh(["api", "--method", "DELETE", f"repos/{repo}/git/refs/heads/{ref}"], run)
    except GhError as failed:
        print(f"Could not delete {ref}: {failed}")


def dispatch_gate(repo: str, branch: str, run) -> None:
    """Start `PR Gate` on `branch`, for the commit just merged there.

    `workflow_dispatch` is the one event `GITHUB_TOKEN` may raise, and the run starts at
    the branch's tip as of now, which is the merge this follows: the job merges one PR at
    a time. A refusal costs one commit's verdict and the merge is already done, so it is
    said and left for the fix pass, which re-gates a tip with no verdict.
    """
    try:
        gh(
            [
                "api",
                "--method",
                "POST",
                f"repos/{repo}/actions/workflows/{GATE_FILE}/dispatches",
                "-f",
                f"ref={branch}",
            ],
            run,
        )
    except GhError as refused:
        print(f"Could not start {GATE_WORKFLOW} on {branch}: {refused}")


def project_settings(root: Path = REPO_ROOT) -> dict:
    """`.devkit.toml`, or nothing: unreadable reads as the gated flow, which waits."""
    try:
        return tomllib.loads((root / ".devkit.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return {}


def flows_directly(settings: dict) -> bool:
    delivery = settings.get("delivery")
    return isinstance(delivery, dict) and delivery.get("flow") == DIRECT_FLOW


def python_version(settings: dict) -> str:
    python = settings.get("python")
    version = python.get("version") if isinstance(python, dict) else ""
    return str(version or DEFAULT_PYTHON)


def write_outputs(env: dict[str, str], outputs: dict[str, str]) -> None:
    """Hand `outputs` to the workflow's later jobs; nothing outside Actions reads them."""
    target = env.get("GITHUB_OUTPUT", "").strip()
    if not target:
        return
    with open(target, "a", encoding="utf-8") as handle:
        handle.writelines(f"{key}={value}\n" for key, value in outputs.items())


def verdict(repo: str, pr: dict, run, env_sha: str = "", queue: bool = False) -> tuple[bool, str]:
    """`(should_merge, why)` for one open PR. Every guard re-derived from scratch.

    `env_sha` is the commit the triggering gate run actually tested, and is passed only in
    branch mode; the sweep has no such commit and judges the head as it finds it.

    `merge-now` skips the gate and nothing else: a draft is still unfinished, and a merge
    nobody can compute is still a conflict. It counts only in `queue`, the job a direct
    project's merges go through: anywhere else it would merge outside the one-at-a-time
    group and with no tidy behind it, and in a gated project it would merge with no gate.
    """
    # No author guard, deliberately: only write access can apply the label, so the label
    # is the whole authorization -- see the module docstring. The head-SHA check below is
    # what defuses a stray human commit on the branch, not the author field.
    labels = labels_of(pr)
    if not any(label in labels for label in MERGE_LABELS):
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
    if queue and MERGE_NOW_LABEL in labels:
        return True, f"labelled {MERGE_NOW_LABEL}: its checks run after it lands"
    return gate_verdict(repo, pr, run, env_sha)


def gate_verdict(repo: str, pr: dict, run, env_sha: str) -> tuple[bool, str]:
    """`verdict`'s half for a PR that waits for its gate."""
    sha = ((pr.get("head") or {}).get("sha")) or ""
    # Branch mode only. The event fired for one commit, and by the time this runs the
    # branch may already carry another -- a Dependabot rebase, or a human pushing to a
    # `dependabot/` branch to inherit the merge. Either way the gate that just passed did
    # not run against what would be merged, and the newer head's own gate run owns it.
    gated = (env_sha or "").strip()
    if gated and sha != gated:
        return False, f"head moved to {sha[:7]}, past the gated {gated[:7]}"
    if not gate_passed(repo, sha, run, (pr.get("head") or {}).get("ref") or ""):
        return False, f"no successful {GATE_WORKFLOW} run on {sha[:7]}"
    blocking = blocking_checks(repo, sha, run)
    if blocking:
        return False, f"checks not all green: {', '.join(blocking)}"
    return True, f"{GATE_WORKFLOW} passed on {sha[:7]} and it is labelled {AUTOMERGE_LABEL}"


def settled(repo: str, number: int, run, sleep, detail: dict) -> dict:
    """The PR re-read until GitHub has decided `mergeable`, up to `MERGEABLE_READS` reads;
    still undecided after that, `verdict` defers it to the next pass."""
    for _ in range(MERGEABLE_READS - 1):
        if detail.get("mergeable") is not None:
            break
        sleep(MERGEABLE_WAIT)
        detail = gh_json(f"repos/{repo}/pulls/{number}", run) or detail
    return detail


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
    PR is a candidate because there is no event to narrow it -- and so is a gate that
    completed on the default branch (`DEFAULT_BRANCH`): the base moving is what leaves
    every open PR's `mergeable` undecided at its own gate's completion, and the base's
    gate finishing is the next moment it is decided (devkit #629 waited an hour on it).
    """
    branch = env.get("HEAD_BRANCH", "").strip()
    if branch and branch == env.get("DEFAULT_BRANCH", "").strip():
        return open_prs(repo, run), f"sweep after {branch!r} moved"
    if branch:
        return prs_for_branch(repo, branch, run), f"branch {branch!r}"
    return open_prs(repo, run), "sweep"


def default_branch_of(pr: dict) -> str:
    """The branch `pr` targets when that is its repository's default, else "".

    A PR into anything else moved no branch the gate's verdict is read from.
    """
    base = pr.get("base") or {}
    ref = str(base.get("ref") or "")
    default = str((base.get("repo") or {}).get("default_branch") or "")
    return ref if ref and ref == default else ""


def in_line(found: list[dict], queue: bool = False) -> list[dict]:
    """The PRs this job may merge, oldest first: the listing is newest first, and a queue
    is the other way round. On the label only, so the per-PR read that follows is spent on
    candidates alone; `verdict` re-checks it against the fresh detail regardless."""
    labels = MERGE_LABELS if queue else (AUTOMERGE_LABEL,)
    labelled = (pr for pr in found if any(label in labels_of(pr) for label in labels))
    return sorted(labelled, key=lambda pr: pr.get("number") or 0)


def take_turn(
    repo: str, pr: dict, run, sleep, gated_sha: str, queue: bool = False
) -> tuple[str, dict]:
    """Judge and merge one PR: `(MERGED, REFUSED or LEFT, the PR as last read)`.

    One failed call must not cost the PRs behind it their merge, nor the job the outputs
    that say what already landed. The job still ends red, so the failure is on record, and
    the next pass retries this one.
    """
    number = int(pr["number"])
    detail = pr
    try:
        # The listing endpoint omits `mergeable`, so re-read each PR on its own. It is also
        # the freshest possible view, which matters on a pass that may have spent a while
        # on the PRs before this one.
        detail = gh_json(f"repos/{repo}/pulls/{number}", run) or pr
        detail = settled(repo, number, run, sleep, detail)
        should_merge, why = verdict(repo, detail, run, gated_sha, queue)
        if not should_merge:
            print(f"#{number}: leaving open -- {why}.")
            return LEFT, detail
        print(f"#{number}: {why}; merging.")
        title, body = str(detail.get("title") or ""), str(detail.get("body") or "")
        merge_pr(repo, number, run, title, body)
    except GhError as failed:
        print(f"#{number}: {failed}", file=sys.stderr)
        return REFUSED, detail
    delete_branch(repo, detail, run)
    return MERGED, detail


def drain(
    repo: str, line: list[dict], run, sleep, gated_sha: str, queue: bool
) -> tuple[int, int, str]:
    """Give each PR its turn: `(merged, refused, the default branch that moved, or "")`.

    Each merge to the default branch gets its gate here, unless this is the queue, whose
    tidy job starts the gate after it (`main`'s comment says why)."""
    merged, refused, moved = 0, 0, ""
    for pr in line:
        if pr.get("number") is None:
            print("A PR arrived with no number; skipping it rather than guessing.")
            continue
        outcome, detail = take_turn(repo, pr, run, sleep, gated_sha, queue)
        refused += outcome == REFUSED
        if outcome != MERGED:
            continue
        merged += 1
        branch = default_branch_of(detail)
        moved = branch or moved
        if branch and not queue:
            dispatch_gate(repo, branch, run)
    return merged, refused, moved


def main(
    env: dict[str, str] | None = None, run=None, sleep=time.sleep, root: Path = REPO_ROOT
) -> int:
    env = dict(os.environ) if env is None else env
    run = subprocess.run if run is None else run

    repo = env.get("GH_REPO", "").strip()
    if not repo:
        print("GH_REPO is empty -- refusing to guess which repository to sweep.", file=sys.stderr)
        return 1

    # The tidy job's last step: whatever it managed, the tip gets its verdict.
    if only := env.get("GATE_ONLY", "").strip():
        dispatch_gate(repo, only, run)
        return 0

    found, scope = candidates(env, repo, run)
    # The `sweep` job in a direct project, and nothing else: it is one at a time and has
    # tidy behind it. Tidying first means the gate reads the tree tidy leaves, so a
    # formatter's work never comes back as a red main.
    settings = project_settings(root)
    queue = flows_directly(settings) and bool(env.get("MERGE_QUEUE", "").strip())
    considered = in_line(found, queue)
    if not considered:
        print(f"No open {AUTOMERGE_LABEL!r}-labelled PRs for {scope}; nothing to do.")
        return 0

    # Only branch mode has a gated commit of the PR's: a sweep's run gated the base.
    gated_sha = "" if scope.startswith("sweep") else env.get("RUN_HEAD_SHA", "").strip()
    merged, refused, moved = drain(repo, considered, run, sleep, gated_sha, queue)
    write_outputs(
        env,
        {
            "tidy": "true" if queue and moved else "false",
            "branch": moved,
            "python": python_version(settings),
        },
    )
    print(f"Considered {len(considered)} automerge PR(s) for {scope}; merged {merged}.")
    return 1 if refused else 0


if __name__ == "__main__":
    raise SystemExit(main())
