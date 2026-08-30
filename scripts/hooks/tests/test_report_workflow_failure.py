"""Tests for the scheduled-failure issue reporter.

**Vendored, so nothing here may name a project.** Every value the script reads arrives
in its environment block, which is what lets these tests build the whole world as a
literal: a fake `subprocess.run` and a dict. No repository, no network, no `gh`.

What they are really pinning is the pair of properties that make the tracker usable
rather than noisy -- it opens exactly one issue per failing workflow no matter how many
nights it fails, and a green run closes it. Either one alone produces a dashboard nobody
trusts: without dedup, a week of failure is seven issues; without the close, the issue
list is a record of things that were fixed days ago.
"""

import json
from types import SimpleNamespace

from conftest import load_module

reporter = load_module("scripts/report-workflow-failure.py")


class FakeRun:
    """Stands in for `subprocess.run`, recording argv and replaying canned results.

    Keyed by the `gh` subcommand pair (`("issue", "list")`), which is the only part of
    an invocation these tests care about.
    """

    def __init__(self, results=None):
        self.calls: list[list[str]] = []
        self.results = results or {}

    def __call__(self, argv, capture_output=False, text=False, check=False):
        self.calls.append(list(argv))
        stdout, returncode, stderr = self.results.get(tuple(argv[1:3]), ("", 0, ""))
        return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)

    def matching(self, *subcommand) -> list[list[str]]:
        width = len(subcommand)
        return [call for call in self.calls if tuple(call[1 : 1 + width]) == subcommand]


def env(**overrides) -> dict[str, str]:
    base = {
        "WORKFLOW": "Nightly",
        "CONCLUSION": "failure",
        "RUN_URL": "https://example.invalid/run/1",
        "RUN_ATTEMPT": "1",
        "HEAD_BRANCH": "main",
        "HEAD_SHA": "abc1234",
        "TRIGGER": "schedule",
        "OWNER": "owner-login",
    }
    base.update(overrides)
    return base


def listing(*issues) -> dict:
    return {("issue", "list"): (json.dumps(list(issues)), 0, "")}


CREATED = ("https://example.invalid/o/r/issues/7\n", 0, "")


# --- what each conclusion means ------------------------------------------------


def test_a_verdict_of_failure_opens_and_a_verdict_of_success_closes():
    for conclusion in ("failure", "timed_out", "startup_failure"):
        assert reporter.decide(conclusion) == "open", conclusion
    assert reporter.decide("success") == "close"


def test_a_run_that_reached_no_verdict_does_neither():
    """The asymmetry is the point, and it is why `cancelled` is not folded into either.

    Treated as success it would close an issue for a workflow still broken; treated as
    failure it would file one about somebody pressing cancel.
    """
    for conclusion in ("cancelled", "skipped", "neutral", "action_required", "stale", ""):
        assert reporter.decide(conclusion) == "ignore", conclusion


def test_an_inconclusive_run_touches_no_api_at_all():
    run = FakeRun()
    assert reporter.main(env(CONCLUSION="cancelled"), run) == 0
    assert run.calls == [], f"a cancelled run should be free, it made {run.calls}"


def test_a_run_with_no_workflow_name_is_an_error_not_a_silent_pass():
    """Every dedupe decision keys off this name; empty would make one issue per run."""
    run = FakeRun()
    assert reporter.main(env(WORKFLOW=""), run) == 1
    assert run.calls == []


# --- the dedupe key ------------------------------------------------------------


def test_the_title_is_a_pure_function_of_the_workflow_name():
    """Anything varying per run -- a number, a date, a SHA -- would defeat the lookup."""
    assert reporter.issue_title("Nightly") == reporter.issue_title("Nightly")
    assert reporter.issue_title("Nightly") != reporter.issue_title("Mutation")
    assert "Nightly" in reporter.issue_title("Nightly")


def test_the_title_carries_no_colon():
    """Not cosmetic: a `:` is a qualifier separator to GitHub's search grammar, and the
    lookup is one search away from needing to quote it."""
    assert ":" not in reporter.issue_title("Nightly")


def test_the_lookup_matches_a_title_exactly():
    title = reporter.issue_title("Nightly")
    issues = [
        {"number": 3, "title": f"Fix the flake behind '{title}'"},
        {"number": 4, "title": title},
    ]
    assert reporter.find_open_issue(issues, title) == 4


def test_the_lookup_declines_a_merely_similar_title():
    """A substring match would let a human's issue that quotes the tracker be commented
    on for a week and then closed by a green run."""
    title = reporter.issue_title("Nightly")
    assert reporter.find_open_issue([{"number": 3, "title": f"{title} again"}], title) is None
    assert reporter.find_open_issue([], title) is None


# --- the failure path ----------------------------------------------------------


def test_the_first_failure_opens_one_issue_carrying_the_run():
    run = FakeRun({**listing(), ("issue", "create"): CREATED})
    assert reporter.main(env(), run) == 0

    created = run.matching("issue", "create")
    assert len(created) == 1, run.calls
    argv = created[0]
    assert reporter.issue_title("Nightly") in argv
    body = argv[argv.index("--body") + 1]
    assert "https://example.invalid/run/1" in body, "the issue must link the run it reports"
    assert "abc1234" in body
    assert reporter.LABEL in argv


def test_opening_an_issue_creates_the_label_it_applies():
    """`gh issue create --label` fails outright on a label the repo does not have, which
    is every repo before its first failure -- so the first report would be the one that
    never lands."""
    run = FakeRun({**listing(), ("issue", "create"): CREATED})
    reporter.main(env(), run)
    assert run.matching("label", "create"), run.calls


def test_a_repeat_failure_comments_instead_of_opening_a_second_issue():
    """The property that makes this a tracker rather than a mailing list."""
    existing = {"number": 7, "title": reporter.issue_title("Nightly")}
    run = FakeRun(listing(existing))
    assert reporter.main(env(), run) == 0

    assert not run.matching("issue", "create"), "a week of failure must not be seven issues"
    commented = run.matching("issue", "comment")
    assert len(commented) == 1
    assert "7" in commented[0]


def test_the_new_issue_is_assigned_so_it_reaches_a_dashboard():
    """An issue authored by the bot is in nobody's `created` tab. Assignment is the only
    dashboard tab a workflow can put it in, which is the entire reason this file exists
    rather than a run annotation."""
    run = FakeRun({**listing(), ("issue", "create"): CREATED})
    reporter.main(env(), run)
    edits = run.matching("issue", "edit")
    assert len(edits) == 1
    assert "--add-assignee" in edits[0]
    assert "owner-login" in edits[0]


def test_an_unassignable_owner_costs_the_assignment_and_not_the_issue():
    """`repository_owner` is an organisation on an org-owned repo, and an organisation
    cannot be assigned. That must not turn a reported failure into an unreported one."""
    run = FakeRun(
        {
            **listing(),
            ("issue", "create"): CREATED,
            ("issue", "edit"): ("", 1, "could not assign: not a user"),
        }
    )
    assert reporter.main(env(), run) == 0
    assert run.matching("issue", "create"), "the issue must survive a failed assignment"


def test_a_missing_owner_is_reported_rather_than_passed_over():
    assert "unassigned" in reporter.assign("7", "", FakeRun())


# --- the recovery path ---------------------------------------------------------


def test_a_green_run_closes_the_open_issue():
    """Delete this behaviour and the tracker degrades into a list of failures that were
    fixed days ago -- which is the state that makes an aggregate view unusable."""
    existing = {"number": 7, "title": reporter.issue_title("Nightly")}
    run = FakeRun(listing(existing))
    assert reporter.main(env(CONCLUSION="success"), run) == 0

    closed = run.matching("issue", "close")
    assert len(closed) == 1
    assert "7" in closed[0]
    assert "--comment" in closed[0], "closing silently leaves no record of what fixed it"


def test_a_green_run_with_nothing_open_changes_nothing():
    run = FakeRun(listing())
    assert reporter.main(env(CONCLUSION="success"), run) == 0
    assert not run.matching("issue", "close")
    assert not run.matching("issue", "comment")
    assert not run.matching("label", "create"), "a healthy repo must not be mutated at all"


def test_a_green_run_does_not_close_another_workflows_issue():
    other = {"number": 9, "title": reporter.issue_title("Mutation")}
    run = FakeRun(listing(other))
    assert reporter.main(env(CONCLUSION="success"), run) == 0
    assert not run.matching("issue", "close")


# --- failure handling ----------------------------------------------------------


def test_gh_failures_surface_with_their_stderr():
    run = FakeRun({("issue", "list"): ("", 1, "HTTP 403: Resource not accessible")})
    try:
        reporter.open_issues(run)
    except reporter.GhError as error:
        assert "403" in str(error), "the stderr is the only diagnosable part"
    else:
        raise AssertionError("a failed gh call must not read as an empty issue list")


def test_a_failed_lookup_does_not_fall_through_to_creating_a_duplicate():
    """The dangerous direction: an unreadable issue list that reads as "none open" opens
    a fresh issue every night, on a repo already failing."""
    run = FakeRun({("issue", "list"): ("", 1, "HTTP 403")})
    try:
        reporter.main(env(), run)
    except reporter.GhError:
        pass
    assert not run.matching("issue", "create")


def test_the_body_names_the_workflow_and_the_trigger():
    body = reporter.failure_body(env())
    assert "Nightly" in body
    assert "schedule" in body
    assert "main" in body


# --- sweep mode ----------------------------------------------------------------
#
# The event-driven mode above watches whatever `on.workflow_run` names, which is one
# title, because the workflow carrying it is vendored byte-identical and a title list is
# per-project. Everything below is the half that does not need to be told what exists.


class SweepRun(FakeRun):
    """`FakeRun` plus the two API shapes the sweep issues, dispatched on the URL.

    `FakeRun` keys its canned results on `argv[1:3]`, which cannot tell one `gh api` call
    from another -- the interesting part of these is the path, not the subcommand.
    """

    def __init__(self, *, workflows: str = "", runs: dict | None = None, issues=()):
        super().__init__(listing(*issues))
        self.workflows = workflows
        self.runs = runs or {}

    def __call__(self, argv, **kwargs):
        if len(argv) > 1 and argv[1] == "api":
            self.calls.append(list(argv))
            url = next(arg for arg in argv[2:] if arg.startswith("repos/"))
            if url.endswith("/actions/workflows"):
                return SimpleNamespace(stdout=self.workflows, stderr="", returncode=0)
            name = url.split("/actions/workflows/", 1)[1].split("/runs", 1)[0]
            found = self.runs.get(name)
            body = json.dumps({"workflow_runs": [found] if found else []})
            return SimpleNamespace(stdout=body, stderr="", returncode=0)
        return super().__call__(argv, **kwargs)


def workflow_file(directory, name: str, text: str):
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


SCHEDULED = """\
name: {title}
on:
  schedule:
    - cron: "0 3 * * 1"
  workflow_dispatch:
permissions:
  contents: read
jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
"""

ON_PULL_REQUEST = """\
name: PR Gate
on:
  pull_request:
jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
"""


def sweep_env(directory, **overrides) -> dict[str, str]:
    base = {
        "MODE": "sweep",
        "GH_REPO": "owner/repo",
        "DEFAULT_BRANCH": "main",
        "OWNER": "owner-login",
        "WORKFLOWS_DIR": str(directory),
    }
    base.update(overrides)
    return base


def api_run(**overrides) -> dict:
    base = {
        "conclusion": "failure",
        "html_url": "https://example.invalid/run/9",
        "run_attempt": 1,
        "head_branch": "main",
        "head_sha": "def5678",
        "event": "schedule",
    }
    base.update(overrides)
    return base


def states(*pairs) -> str:
    return "".join(f".github/workflows/{name}\t{state}\n" for name, state in pairs)


# --- reading the workflow directory ---------------------------------------------


def test_the_three_spellings_of_a_trigger_block_all_parse():
    assert "schedule" in reporter.workflow_triggers("on:\n  schedule:\n    - cron: '0 1 * * *'\n")
    assert reporter.workflow_triggers("on: [push, schedule]\n") == {"push", "schedule"}
    assert reporter.workflow_triggers("on: push\n") == {"push"}


def test_the_yaml_boolean_spelling_of_on_is_understood():
    """`on` is the YAML 1.1 boolean `true`, so a file that has been round-tripped through
    a YAML library comes back with the key spelled `True:` -- and GitHub still runs it."""
    assert "schedule" in reporter.workflow_triggers("True:\n  schedule:\n    - cron: '1 1 * * *'\n")


def test_a_key_nested_under_a_trigger_is_not_itself_a_trigger():
    """`cron:` sits one level under `schedule:`. Reading it as a trigger would not break
    this scan, but the same mistake one level down turns every job name into an event."""
    triggers = reporter.workflow_triggers(SCHEDULED.format(title="Weekly"))
    assert triggers == {"schedule", "workflow_dispatch"}, triggers


def test_a_workflows_title_beats_a_jobs_and_falls_back_to_the_file_name():
    assert reporter.workflow_title(SCHEDULED.format(title="Weekly Hardening"), "x.yml") == (
        "Weekly Hardening"
    )
    assert reporter.workflow_title("on:\n  schedule:\n", "weekly.yml") == "weekly.yml"


def test_only_scheduled_workflow_files_are_swept(tmp_path):
    """A gate is excluded because it is not unattended, and `.yml.disabled` because
    GitHub does not run it either -- both would otherwise be judged on a stale run."""
    workflow_file(tmp_path, "nightly.yml", SCHEDULED.format(title="Nightly"))
    workflow_file(tmp_path, "weekly.yml", SCHEDULED.format(title="Weekly Hardening"))
    workflow_file(tmp_path, "pr-gate.yml", ON_PULL_REQUEST)
    workflow_file(tmp_path, "old.yml.disabled", SCHEDULED.format(title="Retired"))

    found = reporter.scheduled_workflows(tmp_path)
    assert found == [("nightly.yml", "Nightly"), ("weekly.yml", "Weekly Hardening")], found


def test_a_missing_workflow_directory_is_empty_rather_than_an_error(tmp_path):
    assert reporter.scheduled_workflows(tmp_path / "nope") == []


# --- the bug this mode exists for ------------------------------------------------


def test_a_scheduled_workflow_the_event_mode_never_watched_is_still_reported(tmp_path):
    """**The regression test.** `on.workflow_run` named `Nightly` and nothing else, so a
    repo's second scheduled workflow failed into silence -- three consecutive Sundays, in
    the repo that prompted this. The sweep never learns which workflows to watch; it
    reads the directory."""
    workflow_file(tmp_path, "weekly.yml", SCHEDULED.format(title="Weekly Hardening"))
    run = SweepRun(
        workflows=states(("weekly.yml", "active")),
        runs={"weekly.yml": api_run()},
    )
    run.results[("issue", "create")] = CREATED

    assert reporter.main(sweep_env(tmp_path), run) == 0

    created = run.matching("issue", "create")
    assert len(created) == 1, run.calls
    assert reporter.issue_title("Weekly Hardening") in created[0]
    body = created[0][created[0].index("--body") + 1]
    assert "https://example.invalid/run/9" in body


def test_the_sweep_assigns_what_it_opens(tmp_path):
    """Same reason as the event mode: an unassigned issue reaches no dashboard, which is
    the entire point of converting a run into an issue."""
    workflow_file(tmp_path, "weekly.yml", SCHEDULED.format(title="Weekly Hardening"))
    run = SweepRun(workflows=states(("weekly.yml", "active")), runs={"weekly.yml": api_run()})
    run.results[("issue", "create")] = CREATED

    reporter.main(sweep_env(tmp_path), run)
    edits = run.matching("issue", "edit")
    assert len(edits) == 1 and "owner-login" in edits[0], run.calls


def test_the_sweep_dedupes_against_an_already_open_tracker(tmp_path):
    workflow_file(tmp_path, "weekly.yml", SCHEDULED.format(title="Weekly Hardening"))
    open_one = {"number": 12, "title": reporter.issue_title("Weekly Hardening")}
    run = SweepRun(
        workflows=states(("weekly.yml", "active")),
        runs={"weekly.yml": api_run()},
        issues=(open_one,),
    )
    assert reporter.main(sweep_env(tmp_path), run) == 0
    assert not run.matching("issue", "create"), "a daily sweep must not file a daily issue"
    assert len(run.matching("issue", "comment")) == 1


def test_the_sweep_closes_a_tracker_whose_workflow_went_green(tmp_path):
    workflow_file(tmp_path, "weekly.yml", SCHEDULED.format(title="Weekly Hardening"))
    open_one = {"number": 12, "title": reporter.issue_title("Weekly Hardening")}
    run = SweepRun(
        workflows=states(("weekly.yml", "active")),
        runs={"weekly.yml": api_run(conclusion="success")},
        issues=(open_one,),
    )
    assert reporter.main(sweep_env(tmp_path), run) == 0
    closed = run.matching("issue", "close")
    assert len(closed) == 1 and "12" in closed[0], run.calls


def test_the_sweep_judges_each_workflow_on_its_own_run(tmp_path):
    """One green workflow must not close another's tracker, and the failing one must
    still be filed in the same pass."""
    workflow_file(tmp_path, "nightly.yml", SCHEDULED.format(title="Nightly"))
    workflow_file(tmp_path, "weekly.yml", SCHEDULED.format(title="Weekly Hardening"))
    nightly_issue = {"number": 5, "title": reporter.issue_title("Nightly")}
    run = SweepRun(
        workflows=states(("nightly.yml", "active"), ("weekly.yml", "active")),
        runs={
            "nightly.yml": api_run(conclusion="success"),
            "weekly.yml": api_run(conclusion="failure"),
        },
        issues=(nightly_issue,),
    )
    run.results[("issue", "create")] = CREATED

    assert reporter.main(sweep_env(tmp_path), run) == 0
    assert "5" in run.matching("issue", "close")[0]
    assert reporter.issue_title("Weekly Hardening") in run.matching("issue", "create")[0]


def test_a_workflow_with_no_run_on_the_default_branch_is_left_alone(tmp_path):
    """A workflow added yesterday has not run yet. Reporting that as a failure would make
    the first sweep after any addition a false alarm."""
    workflow_file(tmp_path, "weekly.yml", SCHEDULED.format(title="Weekly Hardening"))
    run = SweepRun(workflows=states(("weekly.yml", "active")), runs={})
    assert reporter.main(sweep_env(tmp_path), run) == 0
    assert not run.matching("issue", "create")


def test_an_inconclusive_latest_run_is_neither_opened_nor_closed(tmp_path):
    """The sweep's own run is `in_progress` while it sweeps, so this is the ordinary case
    rather than an exotic one."""
    workflow_file(tmp_path, "weekly.yml", SCHEDULED.format(title="Weekly Hardening"))
    run = SweepRun(
        workflows=states(("weekly.yml", "active")),
        runs={"weekly.yml": api_run(conclusion=None)},
    )
    assert reporter.main(sweep_env(tmp_path), run) == 0
    assert not run.matching("issue", "create")
    assert not run.matching("issue", "close")


# --- the workflow that stopped running -------------------------------------------


def test_a_workflow_github_disabled_for_inactivity_is_reported_as_stopped(tmp_path):
    """The failure the event-driven mode cannot see by construction: a disabled workflow
    emits no completion, so the reporter goes silent at the moment there is something to
    report, and that silence is indistinguishable from a green run."""
    workflow_file(tmp_path, "weekly.yml", SCHEDULED.format(title="Weekly Hardening"))
    run = SweepRun(workflows=states(("weekly.yml", "disabled_inactivity")))
    run.results[("issue", "create")] = CREATED

    assert reporter.main(sweep_env(tmp_path), run) == 0
    created = run.matching("issue", "create")
    assert len(created) == 1
    assert reporter.stopped_title("Weekly Hardening") in created[0]
    assert not run.matching("api", "--paginate")[1:], "a disabled workflow needs no run lookup"


def test_the_stopped_tracker_is_a_different_issue_from_the_failing_one():
    """ "Failing" and "not running" are different repairs. One tracker flipping between
    them would rewrite the title under a reader mid-investigation."""
    assert reporter.stopped_title("Nightly") != reporter.issue_title("Nightly")
    assert ":" not in reporter.stopped_title("Nightly")


def test_a_manually_disabled_workflow_is_somebody_s_decision_and_is_left_alone(tmp_path):
    """`disabled_manually` was a deliberate act. Filing an issue about it would refile the
    same objection on every sweep, forever, and train the reader to close them unread."""
    workflow_file(tmp_path, "weekly.yml", SCHEDULED.format(title="Weekly Hardening"))
    run = SweepRun(
        workflows=states(("weekly.yml", "disabled_manually")),
        runs={"weekly.yml": api_run(conclusion="success")},
    )
    assert reporter.main(sweep_env(tmp_path), run) == 0
    creates = run.matching("issue", "create")
    assert not any(reporter.stopped_title("Weekly Hardening") in call for call in creates)


def test_a_re_enabled_workflow_closes_its_stopped_tracker(tmp_path):
    workflow_file(tmp_path, "weekly.yml", SCHEDULED.format(title="Weekly Hardening"))
    stopped = {"number": 21, "title": reporter.stopped_title("Weekly Hardening")}
    run = SweepRun(
        workflows=states(("weekly.yml", "active")),
        runs={"weekly.yml": api_run(conclusion="success")},
        issues=(stopped,),
    )
    assert reporter.main(sweep_env(tmp_path), run) == 0
    closed = run.matching("issue", "close")
    assert len(closed) == 1 and "21" in closed[0], run.calls


# --- mode selection --------------------------------------------------------------


def test_the_default_mode_is_the_event_driven_report():
    """A project whose vendored workflow predates the sweep passes no MODE at all, and
    must keep reporting exactly as before rather than fail on an unset variable."""
    run = FakeRun({**listing(), ("issue", "create"): CREATED})
    assert reporter.main(env(), run) == 0
    assert run.matching("issue", "create")


def test_the_sweep_refuses_to_guess_its_repository_or_branch(tmp_path):
    """Both arrive from the workflow's `env:` block. Defaulting either one would make the
    sweep judge some other branch's runs and close trackers it never read."""
    for missing in ("GH_REPO", "DEFAULT_BRANCH"):
        run = SweepRun()
        assert reporter.main(sweep_env(tmp_path, **{missing: ""}), run) == 1
        assert run.calls == []
