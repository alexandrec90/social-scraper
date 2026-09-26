"""Tests for the Dependabot auto-merge retry.

**Vendored, so nothing here may name a project.** Every input arrives in the environment
or from an API call, which is what lets these tests build the whole world as a dict and a
fake `subprocess.run`: no repository, no network, no `gh`.

What they pin is the pair of properties that make a scheduled merge safe rather than
reckless. It merges only what the event-driven job would already have merged -- the
`automerge` label something with write access applied, a successful `PR Gate` on the
*current* head SHA -- and it re-derives every one of those rather than inheriting any of
them, because a schedule carries no PR and no gate result to trust.

The regression underneath all of it: on 2026-08-17 two already-green PRs were stranded
when `gh pr merge` hit a GraphQL 503, and nothing ever retried them.
"""

import json
import re
from types import SimpleNamespace

import pytest

from conftest import REPO_ROOT, load_module

merger = load_module("scripts/merge-dependabot-prs.py")

REPO = "owner/repo"
# A git commit SHA, which is public by construction -- it is what `git log` prints. Full
# length and hex on purpose: the script slices it (`sha[:7]`) and compares it against the
# gated commit, so a placeholder shorter than 40 would not exercise either.
SHA = "a466c346037d0cf3333a1bf75c96c2f1c46a32aa"  # pragma: allowlist secret


class FakeRun:
    """Replays canned REST responses, dispatched on the path `gh api` was given."""

    def __init__(self, routes: dict | None = None, fail: dict | None = None):
        self.calls: list[list[str]] = []
        self.routes = routes or {}
        self.fail = fail or {}

    def __call__(self, argv, capture_output=False, text=False, check=False):
        self.calls.append(list(argv))
        path = next((arg for arg in argv[2:] if arg.startswith("repos/")), "")
        for fragment, (code, err) in self.fail.items():
            if fragment in path:
                return SimpleNamespace(stdout="", stderr=err, returncode=code)
        for fragment, payload in self.routes.items():
            if fragment in path:
                return SimpleNamespace(stdout=json.dumps(payload), stderr="", returncode=0)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    def merges(self) -> list[list[str]]:
        return [c for c in self.calls if "PUT" in c]


def pr(**overrides) -> dict:
    base = {
        "number": 15,
        "draft": False,
        "mergeable": True,
        "mergeable_state": "clean",
        "user": {"login": "dependabot[bot]"},
        "labels": [{"name": "dependencies"}, {"name": "automerge"}],
        "head": {"sha": SHA},
    }
    base.update(overrides)
    return base


def world(*, prs=None, gate="success", checks=("success",), **extra):
    """The four REST shapes the script reads, in one dict.

    Ordering matters: `pulls/15` must be matched before the bare `pulls` listing, and
    dicts preserve insertion order, so the specific route is inserted first.
    """
    listing = [pr()] if prs is None else prs
    routes = {
        "actions/runs": {
            "workflow_runs": (
                [{"name": "PR Gate", "event": "pull_request", "conclusion": gate}] if gate else []
            )
        },
        "check-runs": {
            "check_runs": [
                {"name": f"job{i}", "status": "completed", "conclusion": c}
                for i, c in enumerate(checks)
            ]
        },
    }
    for entry in listing:
        routes[f"pulls/{entry['number']}"] = entry
    routes["pulls"] = listing
    routes.update(extra)
    return routes


def env(**overrides) -> dict[str, str]:
    base = {"GH_REPO": REPO}
    base.update(overrides)
    return base


# --- the happy path, which is the regression ------------------------------------


def test_a_labelled_green_pr_the_event_job_dropped_is_merged():
    """**The regression test.** Both these PRs were labelled, gated and clean; the merge
    job died on a GraphQL 503 and nothing re-ran it. The sweep is what re-runs it."""
    run = FakeRun(world())
    assert merger.main(env(), run) == 0
    merges = run.merges()
    assert len(merges) == 1, run.calls
    assert f"repos/{REPO}/pulls/15/merge" in merges[0]


def test_the_merge_goes_through_rest_and_never_graphql():
    """The outage that created this file's workload took out GraphQL while REST stayed
    up. A retry that reached for `gh pr merge` would share the fate of the attempt it is
    retrying."""
    run = FakeRun(world())
    merger.main(env(), run)
    for call in run.calls:
        assert call[1] == "api", f"{call} is not a REST call"
        assert "graphql" not in " ".join(call), call
    assert not any(c[:3] == ["gh", "pr", "merge"] for c in run.calls)


# --- every guard, re-derived rather than inherited -------------------------------


def test_a_pr_without_the_automerge_label_is_left_alone():
    """The label is the classifier's verdict, and it is the only place the
    runtime-vs-development judgement is recorded. No label means it was never made."""
    run = FakeRun(world(prs=[pr(labels=[{"name": "dependencies"}])]))
    assert merger.main(env(), run) == 0
    assert not run.merges()


def test_a_pr_labelled_for_manual_review_is_left_alone():
    run = FakeRun(world(prs=[pr(labels=[{"name": "needs-manual-merge"}])]))
    assert merger.main(env(), run) == 0
    assert not run.merges()


def test_a_labelled_pr_merges_whoever_wrote_it():
    """The label is the whole authorization, for every PR author.

    Only write access can apply a label, so an author guard adds no safety the label
    does not already carry -- and it silently excluded the routine PRs the label exists
    for (devkit upgrades, anything a human marks routine). A stray commit pushed onto
    the branch is defused by the head-SHA guard, not by the author field."""
    run = FakeRun(world(prs=[pr(user={"login": "a-human"})]))
    assert merger.main(env(), run) == 0
    assert len(run.merges()) == 1, run.calls


def test_a_pr_whose_gate_never_passed_is_not_merged():
    run = FakeRun(world(gate="failure"))
    assert merger.main(env(), run) == 0
    assert not run.merges()


BRANCH = "dependabot/pip/minor-and-patch-7def012b62"


def dispatched(head_branch: str) -> dict:
    return {
        "actions/runs": {
            "workflow_runs": [
                {
                    "name": "PR Gate",
                    "event": "workflow_dispatch",
                    "conclusion": "success",
                    "head_branch": head_branch,
                }
            ]
        }
    }


def test_a_gate_dispatched_on_the_prs_own_branch_at_its_head_is_evidence():
    """carameli #393: a lock repair pushed with `GITHUB_TOKEN`, which raises no usable
    `pull_request` event, so it dispatched the gate itself. Every check at the head was
    green, and the PR sat open because only a `pull_request` run counted."""
    run = FakeRun(world(prs=[pr(head={"sha": SHA, "ref": BRANCH})], **dispatched(BRANCH)))
    assert merger.main(env(), run) == 0
    assert len(run.merges()) == 1, run.calls


@pytest.mark.parametrize(
    ("head", "ran_on"),
    [({"sha": SHA, "ref": BRANCH}, "main"), ({"sha": SHA}, BRANCH)],
    ids=["gated-on-another-branch", "pr-ref-unknown"],
)
def test_a_dispatched_gate_that_is_not_on_the_prs_branch_is_not_evidence(head, ran_on):
    """The same commit gated on another ref is a different question, and a PR whose
    head ref cannot be read has nothing to match the run against."""
    run = FakeRun(world(prs=[pr(head=head)], **dispatched(ran_on)))
    assert merger.main(env(), run) == 0
    assert not run.merges()


def test_a_second_failing_check_blocks_the_merge():
    """A green `PR Gate` says nothing about a repo's other required workflows. The
    event-driven job merges on the gate alone because that is the event it woke for; a
    scheduled pass has no such excuse and looks at all of them."""
    run = FakeRun(world(checks=("success", "failure")))
    assert merger.main(env(), run) == 0
    assert not run.merges()


def test_a_check_still_running_defers_rather_than_merges():
    run = FakeRun(
        world(
            **{
                "check-runs": {
                    "check_runs": [{"name": "slow", "status": "in_progress", "conclusion": None}]
                }
            }
        )
    )
    assert merger.main(env(), run) == 0
    assert not run.merges()


def test_a_skipped_or_neutral_check_is_not_an_objection():
    """Every repo with a conditional job in its gate reports these, and reading either as
    a failure would mean the sweep never merges anything there."""
    run = FakeRun(world(checks=("success", "skipped", "neutral")))
    assert merger.main(env(), run) == 0
    assert len(run.merges()) == 1


def test_a_conflicted_pr_is_left_alone():
    run = FakeRun(world(prs=[pr(mergeable=False, mergeable_state="dirty")]))
    assert merger.main(env(), run) == 0
    assert not run.merges()


def test_an_undecided_mergeable_is_deferred_not_refused():
    """GitHub returns null while it computes the merge commit. That is "ask again", and
    the next hourly pass is exactly that -- but it must not merge on it now."""
    run = FakeRun(world(prs=[pr(mergeable=None)]))
    assert merger.main(env(), run) == 0
    assert not run.merges()


def test_a_draft_is_never_merged():
    run = FakeRun(world(prs=[pr(draft=True)]))
    assert merger.main(env(), run) == 0
    assert not run.merges()


# --- branch mode, the workflow_run path ------------------------------------------


def test_branch_mode_declines_when_the_head_moved_past_the_gated_commit():
    """The event fired for one commit. If the branch has moved since -- a rebase, or a
    human pushing to inherit the merge -- the gate that just passed did not run against
    what would be merged, and the newer head's own gate run owns it."""
    run = FakeRun(world())
    code = merger.main(env(HEAD_BRANCH="dependabot/uv/x", RUN_HEAD_SHA="0" * 40), run)
    assert code == 0
    assert not run.merges()


def test_branch_mode_merges_when_the_head_still_matches():
    run = FakeRun(world())
    assert merger.main(env(HEAD_BRANCH="dependabot/uv/x", RUN_HEAD_SHA=SHA), run) == 0
    assert len(run.merges()) == 1


def test_branch_mode_asks_the_api_for_that_branch_rather_than_scanning():
    """A repo with more open PRs than one page would otherwise hide the branch the event
    named, and the merge would silently never happen."""
    run = FakeRun(world())
    merger.main(env(HEAD_BRANCH="dependabot/uv/x", RUN_HEAD_SHA=SHA), run)
    listings = [c for c in run.calls if any("head=owner:dependabot/uv/x" in a for a in c)]
    assert listings, run.calls


# --- failure handling -------------------------------------------------------------


def test_a_missing_repo_is_an_error_not_a_silent_pass():
    run = FakeRun(world())
    assert merger.main(env(GH_REPO=""), run) == 1
    assert run.calls == []


def test_a_failed_api_call_surfaces_with_its_stderr():
    """The stderr is the only diagnosable part, and a 503 read as an empty PR list would
    make the retry quietly do nothing on exactly the day it is needed."""
    run = FakeRun(world(), fail={"pulls": (1, "HTTP 503: No server is currently available")})
    try:
        merger.main(env(), run)
    except merger.GhError as error:
        assert "503" in str(error)
    else:
        raise AssertionError("a failed listing must not read as 'no PRs to merge'")


# --- the workflow's token must cover the API surface ------------------------------


# The regression these pin: `blocking_checks` arrived in bd11890 reading
# `commits/{sha}/check-runs`, and nothing added `checks: read` to the workflow that runs
# it. A `permissions:` block grants *only* what it lists, so the omission did not narrow
# the sweep's evidence -- it made every scheduled pass that reached a candidate PR die on
# `HTTP 403: Resource not accessible by integration`, and the retry for stranded
# auto-merges had not run since in any repo holding an `automerge` PR.
#
# `scheduled-failure-issue.yml` reported it correctly and immediately -- social-scraper#3
# was opened the first night it fired, 2026-09-07, and updated hourly for the nine days
# until the PR it was stranding merged -- which is worth stating because it locates the
# gap precisely. Nothing here failed to *notice*. What was missing was the thing that
# would have made the bug impossible to ship: the permission a call needs is not in the
# file you add the call to, and no test spanned the two.
#
# Deriving the routes from the source rather than listing them is the whole point. A test
# that pinned today's five paths would pass the day a sixth is added -- which is exactly
# how the fifth got in.

# Every REST route this script reaches, normalised to its path shape, mapped to the
# token scopes GitHub requires for it. A route the script calls and this table does not
# name fails the test outright: adding an endpoint is the moment to decide what it needs,
# and the workflow is not in the file you are editing when you add one.
ROUTE_SCOPES = {
    # The gate's verdict on a head SHA, which is a workflow run.
    "actions/runs": {("actions", "read")},
    # Anything *else* wired onto the PR -- a lock repair, a licence scan.
    "commits/*/check-runs": {("checks", "read")},
    # Listing and reading open PRs.
    "pulls": {("pull-requests", "read")},
    "pulls/*": {("pull-requests", "read")},
    # Merging writes a commit to the base branch and closes the PR.
    "pulls/*/merge": {("contents", "write"), ("pull-requests", "write")},
}

# GitHub's own ordering. `write` implies `read`; an unlisted scope is `none`.
LEVELS = {"none": 0, "read": 1, "write": 2}

AUTOMERGE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "dependabot-automerge.yml"


def called_routes() -> set[str]:
    """The REST routes `merge-dependabot-prs.py` builds, as path shapes.

    Read off the source text, not by importing and calling: a route reached only on a
    branch these tests do not drive is still a route the token has to cover.
    """
    source = (REPO_ROOT / "scripts" / "merge-dependabot-prs.py").read_text(encoding="utf-8")
    routes = set()
    for raw in re.findall(r"repos/\{repo\}/([^\"']+)", source):
        path = raw.split("?", 1)[0].rstrip("/")
        # `{sha}`, `{number}` -- the variable segments are not part of the route's shape.
        routes.add("/".join("*" if "{" in part else part for part in path.split("/")))
    return routes


def granted_permissions() -> dict[str, str]:
    """The workflow's top-level `permissions:` block, as `{scope: level}`.

    Stdlib text parsing, no PyYAML, for the reason the rest of this vendored suite uses
    none: it runs as its own step and may not assume the project installed anything.
    """
    granted = {}
    inside = False
    for line in AUTOMERGE_WORKFLOW.read_text(encoding="utf-8").splitlines():
        if line.startswith("permissions:"):
            inside = True
            continue
        if inside:
            if line.strip().startswith("#") or not line.strip():
                continue
            if not line.startswith(" "):
                break
            scope, _, level = line.strip().partition(":")
            granted[scope.strip()] = level.strip()
    return granted


def test_every_route_the_script_calls_is_named_in_the_scope_table():
    undeclared = called_routes() - set(ROUTE_SCOPES)
    assert not undeclared, (
        f"merge-dependabot-prs.py calls {sorted(undeclared)}, which ROUTE_SCOPES does not "
        "name. Add the route with the scopes GitHub requires for it, and grant them in "
        f"{AUTOMERGE_WORKFLOW.name} -- an ungranted scope is a 403 at 17 past the hour, "
        "not a degraded result."
    )


def test_the_workflow_grants_every_scope_the_script_needs():
    granted = granted_permissions()
    missing = []
    for route in sorted(called_routes()):
        for scope, level in sorted(ROUTE_SCOPES.get(route, ())):
            have = LEVELS.get(granted.get(scope, "none"), 0)
            if have < LEVELS[level]:
                missing.append(
                    f"{route} needs {scope}: {level}, workflow grants {granted.get(scope, 'none')}"
                )
    assert not missing, "; ".join(missing)


def test_checks_read_specifically_is_granted():
    """The one that was missing, pinned by name so a future tidy of the block cannot drop
    it back out without saying so."""
    assert granted_permissions().get("checks") == "read"


def test_the_sweep_reads_check_runs_at_all():
    """The other half of the pair. If `blocking_checks` stops calling the endpoint, the
    permission above is dead weight and should go in the same change -- but until then,
    the reason `checks: read` is there must stay true."""
    run = FakeRun(world())
    merger.main(env(), run)
    assert any("check-runs" in arg for call in run.calls for arg in call), run.calls
