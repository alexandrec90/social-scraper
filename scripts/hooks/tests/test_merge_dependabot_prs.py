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
    """The label is the whole authorization: the classify job applies it to Dependabot's
    PRs, a person to any other. No label means nobody authorized the merge."""
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
    """GitHub returns null while it computes the merge commit. That is "ask again" -- but
    never a merge on it now. Still null after the re-reads, the hourly pass owns it."""
    run = FakeRun(world(prs=[pr(mergeable=None)]))
    naps: list[float] = []
    assert merger.main(env(), run, sleep=naps.append) == 0
    assert not run.merges()
    assert len(naps) == merger.MERGEABLE_READS - 1


class Computing(FakeRun):
    """GitHub computing mergeability: null for the first reads of the PR, then decided."""

    def __init__(self, routes, undecided_reads: int):
        super().__init__(routes)
        self.left = undecided_reads

    def __call__(self, argv, capture_output=False, text=False, check=False):
        if any(a.endswith("pulls/15") for a in argv) and self.left > 0:
            self.left -= 1
            self.calls.append(list(argv))
            return SimpleNamespace(stdout=json.dumps(pr(mergeable=None)), stderr="", returncode=0)
        return super().__call__(argv, capture_output, text, check)


def test_mergeability_still_being_computed_is_asked_again_before_giving_up():
    """devkit #405 was stranded twice: its gate finished while `main` had just moved, so
    GitHub had not decided `mergeable` yet, and the one read said null -- at the gate's
    completion and again at the hourly retry, since fixer PRs kept moving `main`. GitHub
    computes it on request, so a few reads seconds apart get the answer."""
    run = Computing(world(), undecided_reads=2)
    naps: list[float] = []
    assert merger.main(env(HEAD_BRANCH="agent/x", RUN_HEAD_SHA=SHA), run, sleep=naps.append) == 0
    assert len(run.merges()) == 1
    assert naps == [merger.MERGEABLE_WAIT] * 2


def test_settled_reads_only_while_undecided():
    """A decided answer -- mergeable or conflicted -- is never re-read or slept on."""
    naps: list[float] = []
    for decided in (True, False):
        run = FakeRun(world())
        assert (
            merger.settled(REPO, 15, run, naps.append, pr(mergeable=decided))["mergeable"]
            is decided
        )
        assert run.calls == [] and naps == []
    never = Computing(world(), undecided_reads=merger.MERGEABLE_READS)
    assert merger.settled(REPO, 15, never, naps.append, pr(mergeable=None))["mergeable"] is None
    assert len(naps) == merger.MERGEABLE_READS - 1, "bounded: the hourly pass owns the rest"
    soon = Computing(world(), undecided_reads=1)
    assert merger.settled(REPO, 15, soon, naps.append, pr(mergeable=None))["mergeable"] is True


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


def test_a_gate_completed_on_the_default_branch_sweeps_every_open_pr():
    """devkit #629, 2026-10-12: its gate went green 40 seconds after #635 moved `main`,
    `mergeable` was still undecided after every re-read, and it sat for the hourly pass.
    What strands a PR is the base moving, and the base's own gate completing is the next
    moment the answer is decided -- so that completion sweeps, rather than looking for
    PRs whose head is `main`. The gated commit is `main`'s, never any PR's head."""
    run = FakeRun(world(prs=[pr(head={"sha": SHA, "ref": "agent/x"})]))
    main_tip = "0" * 40
    code = merger.main(env(HEAD_BRANCH="main", RUN_HEAD_SHA=main_tip, DEFAULT_BRANCH="main"), run)
    assert code == 0
    assert len(run.merges()) == 1, run.calls
    assert not [c for c in run.calls if any("head=owner:main" in a for a in c)]


def test_without_a_default_branch_a_main_completion_stays_branch_mode():
    """An older workflow copy passes no `DEFAULT_BRANCH`: the run keeps its old meaning."""
    run = FakeRun(world())
    merger.main(env(HEAD_BRANCH="main", RUN_HEAD_SHA=SHA), run)
    assert [c for c in run.calls if any("head=owner:main" in a for a in c)]


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
    # Starting the gate on what was just merged: `GITHUB_TOKEN`'s merge raises no `push`.
    "actions/workflows/*/dispatches": {("actions", "write")},
    # Deleting a merged PR's head, so a checkout on it reads as spent.
    "git/refs/heads/*": {("contents", "write")},
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


# --- one PR, one commit ----------------------------------------------------------


def test_a_merge_is_a_squash_carrying_the_prs_title_and_body():
    """One commit per PR is what makes reverting a change one `git revert`, and the body
    is the only changelog a consumer of the repository gets."""
    run = FakeRun(world(prs=[pr(title="Fix it", body="Why it was broken.")]))
    assert merger.main(env(), run) == 0
    merge = run.merges()[0]
    assert "merge_method=squash" in merge
    assert "commit_title=Fix it (#15)" in merge
    assert "commit_message=Why it was broken." in merge


def test_a_repository_with_squash_off_takes_a_merge_commit():
    class Refusing(FakeRun):
        def __call__(self, argv, **kwargs):
            if "merge_method=squash" in argv:
                self.calls.append(list(argv))
                said = "HTTP 405: Squash merges are not allowed on this repository"
                return SimpleNamespace(stdout="", stderr=said, returncode=1)
            return super().__call__(argv, **kwargs)

    run = Refusing(world())
    assert merger.main(env(), run) == 0
    assert "merge_method=merge" in run.merges()[-1]


# --- the merge queue -------------------------------------------------------------


ONTO_MAIN = {"ref": "main", "repo": {"default_branch": "main"}}


def merge_now(number: int = 15, **overrides) -> dict:
    labels = [{"name": "merge-now"}]
    return pr(number=number, labels=labels, **overrides)


def merged_numbers(run: FakeRun) -> list[int]:
    found = (re.search(r"pulls/(\d+)/merge", " ".join(call)) for call in run.merges())
    return [int(match.group(1)) for match in found if match]


def direct_project(tmp_path):
    (tmp_path / ".devkit.toml").write_text(
        '[delivery]\nflow = "direct"\n[python]\nversion = "3.13"\n', encoding="utf-8"
    )
    return tmp_path


def in_the_queue(run, tmp_path, **overrides) -> int:
    """`main` as the `sweep` job runs it in a direct project."""
    return merger.main(env(MERGE_QUEUE="yes", **overrides), run, root=direct_project(tmp_path))


def test_merge_now_merges_with_no_gate_at_all(tmp_path):
    """No gate ran, and that is the point: the direct flow checks after landing."""
    run = FakeRun(world(prs=[merge_now()], gate=""))
    assert in_the_queue(run, tmp_path) == 0
    assert merged_numbers(run) == [15]
    assert not any("actions/runs" in arg for call in run.calls for arg in call), run.calls


def test_merge_now_outside_the_queue_job_merges_nothing(tmp_path):
    """The `merge` job fires after every gate. Draining from there would merge outside the
    one-at-a-time group and with no tidy behind it, and the gate it dispatched would read
    the untidied commit as red."""
    run = FakeRun(world(prs=[merge_now()], gate=""))
    assert merger.main(env(), run, root=direct_project(tmp_path)) == 0
    assert merged_numbers(run) == []


def test_merge_now_in_a_gated_project_merges_nothing(tmp_path):
    """A label someone applies by hand must not be a way past that project's gate."""
    run = FakeRun(world(prs=[merge_now()], gate=""))
    assert merger.main(env(MERGE_QUEUE="yes"), run, root=tmp_path) == 0
    assert merged_numbers(run) == []


def test_both_labels_outside_the_queue_still_wait_for_the_gate(tmp_path):
    labels = [{"name": "automerge"}, {"name": "merge-now"}]
    run = FakeRun(world(prs=[pr(labels=labels)], gate=""))
    assert merger.main(env(), run, root=direct_project(tmp_path)) == 0
    assert merged_numbers(run) == []


def test_the_queue_merges_oldest_first(tmp_path):
    listing = [merge_now(30), merge_now(10), merge_now(20)]  # the API lists newest first
    run = FakeRun(world(prs=listing, gate=""))
    assert in_the_queue(run, tmp_path) == 0
    assert merged_numbers(run) == [10, 20, 30]


def test_a_conflict_is_passed_over_and_the_line_behind_it_keeps_moving(tmp_path):
    """Stopping for it would hold every PR behind one resolver session."""
    listing = [
        merge_now(10),
        merge_now(20, mergeable=False, mergeable_state="dirty"),
        merge_now(30),
    ]
    run = FakeRun(world(prs=listing, gate=""))
    assert in_the_queue(run, tmp_path) == 0
    assert merged_numbers(run) == [10, 30]


def test_a_draft_is_still_unfinished_with_merge_now(tmp_path):
    run = FakeRun(world(prs=[merge_now(draft=True)], gate=""))
    assert in_the_queue(run, tmp_path) == 0
    assert merged_numbers(run) == []


def test_a_held_pr_carries_neither_label_and_stays(tmp_path):
    run = FakeRun(world(prs=[pr(labels=[])], gate=""))
    assert in_the_queue(run, tmp_path) == 0
    assert merged_numbers(run) == []


def test_a_failed_read_of_one_pr_costs_only_that_pr(tmp_path):
    """Raised past the loop, it would also lose the outputs saying what already landed,
    and those commits would get no tidy and no gate."""
    outputs = tmp_path / "outputs"
    listing = [merge_now(10, base=ONTO_MAIN), merge_now(20, base=ONTO_MAIN)]

    class Failing(FakeRun):
        def __call__(self, argv, **kwargs):
            if argv[-1].endswith("pulls/20"):
                self.calls.append(list(argv))
                return SimpleNamespace(stdout="", stderr="HTTP 502", returncode=1)
            return super().__call__(argv, **kwargs)

    run = Failing(world(prs=listing, gate=""))
    assert in_the_queue(run, tmp_path, GITHUB_OUTPUT=str(outputs)) == 1
    assert merged_numbers(run) == [10]
    assert "tidy=true" in outputs.read_text(encoding="utf-8").splitlines()


def test_one_refused_merge_does_not_cost_the_prs_behind_it_their_turn(tmp_path):
    """And the job still ends red, so the refusal is on record."""

    class Refusing(FakeRun):
        def __call__(self, argv, **kwargs):
            if any("pulls/10/merge" in arg for arg in argv):
                self.calls.append(list(argv))
                return SimpleNamespace(stdout="", stderr="HTTP 409: Conflict", returncode=1)
            return super().__call__(argv, **kwargs)

    run = Refusing(world(prs=[merge_now(10), merge_now(20)], gate=""))
    assert in_the_queue(run, tmp_path) == 1
    assert merged_numbers(run) == [10, 20]


# --- a merge commit is the answer to one refusal only -----------------------------


def refusal(said: str):
    return merger.GhError(f"gh api --method PUT merge_method=squash exited 1: {said}", said)


@pytest.mark.parametrize(
    "said",
    [
        "HTTP 405: Squash merges are not allowed on this repository.",
        "HTTP 403: refusing to allow a GitHub App to update workflow `.github/workflows/x.yml`",
    ],
)
def test_squash_refused_reads_a_refusal_of_squashing(said):
    assert merger.squash_refused(refusal(said)) is True


@pytest.mark.parametrize(
    "said",
    [
        "HTTP 405: Base branch was modified. Review and try the merge again.",
        "HTTP 405: Pull Request is not mergeable",
        "HTTP 503: No server is currently available",
    ],
)
def test_any_other_refusal_is_not_a_reason_for_a_merge_commit(said):
    """Reading the whole message matched these: the argv itself contains "squash", and a
    PR number could supply the 405. A merge commit would break one PR, one commit."""
    assert merger.squash_refused(refusal(said)) is False


def test_a_refused_merge_is_not_retried_as_a_merge_commit(tmp_path):
    class Moved(FakeRun):
        def __call__(self, argv, **kwargs):
            if "merge_method=squash" in argv:
                self.calls.append(list(argv))
                said = "HTTP 405: Base branch was modified. Review and try the merge again."
                return SimpleNamespace(stdout="", stderr=said, returncode=1)
            return super().__call__(argv, **kwargs)

    run = Moved(world(prs=[merge_now()], gate=""))
    assert in_the_queue(run, tmp_path) == 1
    assert not any("merge_method=merge" in call for call in run.merges())


# --- a merged branch goes --------------------------------------------------------


def head_of(ref: str, repo: str = REPO) -> dict:
    return {"ref": ref, "sha": SHA, "repo": {"full_name": repo}}


def deletes(run: FakeRun) -> list[list[str]]:
    return [call for call in run.calls if "DELETE" in call]


def test_a_merged_branch_is_deleted(tmp_path):
    """A squash leaves a live branch that is no ancestor of the base, and a checkout still
    on it reads as `needs-pr` for good, its home branch no longer advancing."""
    run = FakeRun(world(prs=[merge_now(head=head_of("agent/x"), base=ONTO_MAIN)], gate=""))
    assert in_the_queue(run, tmp_path) == 0
    assert [f"repos/{REPO}/git/refs/heads/agent/x" in call for call in deletes(run)] == [True]


@pytest.mark.parametrize(
    "head",
    [head_of("main"), head_of("green"), head_of("agent/x", "someone/fork"), {"sha": SHA}],
)
def test_delete_branch_leaves_what_is_not_this_prs_to_delete(head):
    run = FakeRun({})
    merger.delete_branch(REPO, {"head": head, "base": ONTO_MAIN}, run)
    assert run.calls == []


def test_a_branch_that_will_not_delete_costs_nothing_else(tmp_path):
    run = FakeRun(
        world(prs=[merge_now(head=head_of("agent/x"), base=ONTO_MAIN)], gate=""),
        fail={"git/refs/heads": (1, "HTTP 422: Reference does not exist")},
    )
    assert in_the_queue(run, tmp_path) == 0
    assert merged_numbers(run) == [15]


# --- what a merge starts ---------------------------------------------------------


def dispatches(run: FakeRun) -> list[list[str]]:
    return [call for call in run.calls if any("dispatches" in arg for arg in call)]


def test_every_merge_to_the_default_branch_dispatches_the_gate(tmp_path):
    """A `GITHUB_TOKEN` merge raises no `push`, so nothing else would judge that commit."""
    listing = [pr(number=10, base=ONTO_MAIN), pr(number=20, base=ONTO_MAIN)]
    run = FakeRun(world(prs=listing))
    assert merger.main(env(), run, root=tmp_path) == 0
    assert len(dispatches(run)) == 2, "one per merge, or a culprit cannot be told apart"
    assert "ref=main" in dispatches(run)[0]


def test_a_merge_elsewhere_dispatches_nothing(tmp_path):
    other = {"ref": "develop", "repo": {"default_branch": "main"}}
    run = FakeRun(world(prs=[pr(base=other)]))
    assert merger.main(env(), run, root=tmp_path) == 0
    assert dispatches(run) == []


def test_a_refused_dispatch_costs_a_verdict_and_never_the_merge(tmp_path):
    run = FakeRun(
        world(prs=[pr(base=ONTO_MAIN)]),
        fail={"dispatches": (1, "HTTP 403: Resource not accessible by integration")},
    )
    assert merger.main(env(), run, root=tmp_path) == 0
    assert merged_numbers(run) == [15]


def test_the_direct_flow_leaves_the_gate_to_the_tidy_job(tmp_path):
    """Gated after tidying, it judges the tree tidy leaves, so a formatter's unfinished
    work never reads as a red main."""
    outputs = tmp_path / "outputs"
    run = FakeRun(world(prs=[merge_now(base=ONTO_MAIN)], gate=""))
    assert in_the_queue(run, tmp_path, GITHUB_OUTPUT=str(outputs)) == 0
    assert dispatches(run) == []
    written = outputs.read_text(encoding="utf-8").splitlines()
    assert {"tidy=true", "branch=main", "python=3.13"} <= set(written)


def test_a_job_with_no_tidy_behind_it_gates_a_direct_projects_merge(tmp_path):
    """The `merge` job takes `automerge` PRs in a direct project too, Dependabot's among
    them, and it cannot leave the commit to a tidy that will not run."""
    run = FakeRun(world(prs=[pr(base=ONTO_MAIN)]))
    assert merger.main(env(), run, root=direct_project(tmp_path)) == 0
    assert len(dispatches(run)) == 1


def test_nothing_merged_asks_for_no_tidy(tmp_path):
    outputs = tmp_path / "outputs"
    conflicted = merge_now(mergeable=False, mergeable_state="dirty")
    run = FakeRun(world(prs=[conflicted], gate=""))
    in_the_queue(run, tmp_path, GITHUB_OUTPUT=str(outputs))
    assert "tidy=false" in outputs.read_text(encoding="utf-8").splitlines()


def test_a_gated_project_is_never_tidied(tmp_path):
    outputs = tmp_path / "outputs"
    run = FakeRun(world(prs=[pr(base=ONTO_MAIN)]))
    merger.main(env(MERGE_QUEUE="yes", GITHUB_OUTPUT=str(outputs)), run, root=tmp_path)
    assert "tidy=false" in outputs.read_text(encoding="utf-8").splitlines()


def test_a_broken_devkit_toml_reads_as_gated(tmp_path):
    (tmp_path / ".devkit.toml").write_text("not [ toml", encoding="utf-8")
    assert merger.flows_directly(merger.project_settings(tmp_path)) is False


def test_the_tidy_job_can_start_the_gate_on_its_own():
    run = FakeRun({})
    assert merger.main(env(GATE_ONLY="main"), run) == 0
    assert len(dispatches(run)) == 1
    assert len(run.calls) == 1, "the tidy job's last step merges nothing and reads no PR"


def test_a_merge_only_into_another_branch_asks_for_no_tidy(tmp_path):
    """There is no default branch to tidy, and an empty ref would fail the job."""
    outputs = tmp_path / "outputs"
    other = {"ref": "develop", "repo": {"default_branch": "main"}}
    run = FakeRun(world(prs=[merge_now(base=other)], gate=""))
    in_the_queue(run, tmp_path, GITHUB_OUTPUT=str(outputs))
    assert "tidy=false" in outputs.read_text(encoding="utf-8").splitlines()


def test_default_branch_of_answers_only_for_the_default():
    assert merger.default_branch_of({"base": ONTO_MAIN}) == "main"
    other = {"ref": "develop", "repo": {"default_branch": "main"}}
    assert merger.default_branch_of({"base": other}) == ""
    assert merger.default_branch_of({}) == ""


def test_python_version_comes_from_the_manifest_or_the_default():
    assert merger.python_version({"python": {"version": "3.13"}}) == "3.13"
    assert merger.python_version({}) == merger.DEFAULT_PYTHON


def test_write_outputs_appends_and_is_silent_outside_actions(tmp_path):
    target = tmp_path / "out"
    merger.write_outputs({"GITHUB_OUTPUT": str(target)}, {"a": "1"})
    merger.write_outputs({"GITHUB_OUTPUT": str(target)}, {"b": "2"})
    assert target.read_text(encoding="utf-8") == "a=1\nb=2\n"
    merger.write_outputs({}, {"c": "3"})  # no variable, no file, no error


def test_dispatch_gate_names_the_gates_file_and_the_branch():
    run = FakeRun({})
    merger.dispatch_gate(REPO, "main", run)
    call = run.calls[0]
    assert f"repos/{REPO}/actions/workflows/pr-gate.yml/dispatches" in call
    assert "ref=main" in call


def test_in_line_keeps_either_label_only_in_the_queue_and_orders_by_age():
    listing = [merge_now(9), pr(number=3), pr(number=5, labels=[])]
    assert [entry["number"] for entry in merger.in_line(listing, queue=True)] == [3, 9]
    assert [entry["number"] for entry in merger.in_line(listing)] == [3]


def test_take_turn_says_what_became_of_the_pr():
    run = FakeRun(world(prs=[merge_now()], gate=""))
    outcome, detail = merger.take_turn(REPO, merge_now(), run, lambda _: None, "", queue=True)
    assert outcome == merger.MERGED and detail["number"] == 15


def test_drain_counts_and_names_the_branch_that_moved():
    listing = [merge_now(10, base=ONTO_MAIN), merge_now(20, mergeable=False)]
    run = FakeRun(world(prs=listing, gate=""))
    assert merger.drain(REPO, listing, run, lambda _: None, "", True) == (1, 0, "main")
    assert dispatches(run) == [], "the queue's tidy job gates after it"


def test_gate_verdict_is_the_half_merge_now_skips():
    run = FakeRun(world(gate=""))
    should, why = merger.gate_verdict(REPO, pr(), run, "")
    assert should is False and "PR Gate" in why


# --- the workflow's shape --------------------------------------------------------


def _job(name: str) -> str:
    text = AUTOMERGE_WORKFLOW.read_text(encoding="utf-8")
    body = text.split(f"\n  {name}:\n", 1)[1]
    return re.split(r"\n  [a-z-]+:\n", body, maxsplit=1)[0]


def test_the_drain_runs_one_at_a_time_and_runs_the_default_branchs_script():
    """On `pull_request` the event's ref is the PR's own tree, and this job holds a token
    that can merge: running the PR's copy of the merger would let a PR rewrite its gate."""
    job = _job("sweep")
    assert "group: merge-queue" in job
    assert "cancel-in-progress: false" in job
    assert "ref: ${{ github.event.repository.default_branch }}" in job
    assert "merge-now" in job


def test_tidying_never_shares_the_drains_group():
    """Sharing it would make every merge wait on a formatter, the wait this flow removes."""
    assert "group: tidy-main" in _job("tidy")


def test_only_the_sweep_job_is_the_queue():
    """The `merge` job fires after every gate, outside the one-at-a-time group."""
    assert "MERGE_QUEUE" in _job("sweep")
    assert "MERGE_QUEUE" not in _job("merge")


def test_tidy_follows_a_drain_that_ended_red():
    """A drain that merged two PRs and had a third refused ends red. The implied `success()`
    would skip tidy, and what landed would get no tidy and no gate."""
    assert "!cancelled()" in _job("tidy")


def test_a_fork_cannot_start_the_drain():
    """Its token is read-only, so all it could do is replace a good pending run."""
    assert "head.repo.full_name == github.repository" in _job("sweep")


def test_every_gate_completion_gets_a_run_of_its_own():
    """GitHub keeps one pending run per group. Keyed on the branch, a failed gate's run
    replaced a passing one's, and `green` stayed behind a commit that passed."""
    text = AUTOMERGE_WORKFLOW.read_text(encoding="utf-8")
    assert "group: dependabot-automerge-${{ github.event.workflow_run.head_sha" in text


def test_the_gate_starts_even_when_tidying_failed():
    assert "if: always()" in _job("tidy").split("Start the gate", 1)[1]


def test_green_follows_only_a_default_branch_run_that_passed():
    """A fork's branch can share the default branch's name, and its run arrives as a
    `pull_request`, so the event list is what keeps it from moving `green`."""
    job = _job("green")
    assert "conclusion == 'success'" in job
    assert "head_branch == github.event.repository.default_branch" in job
    condition = job.split("if: >-", 1)[1].split("runs-on:", 1)[0]
    assert '["push", "workflow_dispatch"]' in condition
    assert "pull_request" not in condition
