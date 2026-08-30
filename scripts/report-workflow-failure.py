#!/usr/bin/env python3
"""Turn a failed scheduled workflow run into a GitHub issue, and close it when green.

**Why an issue at all.** A workflow run is a per-repository artifact. GitHub's
cross-repository dashboards -- `github.com/issues` and `github.com/pulls` -- aggregate
issues and pull requests and nothing else, so a red nightly is visible only to someone
who opens that one repo's Actions tab and looks. Across a workspace of several repos
that is a poll nobody performs, which makes a failing nightly indistinguishable from a
nightly that has not run: both are silent. An issue is the only artifact a scheduled run
can leave that surfaces without being looked for.

**Why it is assigned.** An issue opened by `github-actions[bot]` is authored by the bot,
so it appears in the dashboard's *Created* tab for nobody. Every tab there is keyed to
the viewer -- created, assigned, mentioned -- and assignment is the only one a workflow
can set on someone else's behalf. Opening the issue and leaving it unassigned reproduces
the invisibility this script exists to remove, which is why the assignment step is here
and why its failure is reported rather than swallowed.

**Why it closes itself.** A tracker that only ever opens issues becomes a list of
already-fixed failures within a week, and a stale aggregate view is worse than none: it
is one that has to be re-verified item by item before it can be believed. The success
path is not an optimisation, it is the half that keeps the failure path meaningful.

Deduplication is by exact issue title, one open issue per workflow name. The label is
for filtering only -- never for lookup, because `gh issue list --label` fails outright on
a repo that has never carried the label, which is every repo before its first failure.

**Two modes, because one event cannot see the whole problem.**

`report` is the original: a `workflow_run` completion arrives, and this reconciles that
one workflow's tracker against it. It is immediate, and it is what closes an issue the
moment a fix goes green. What it cannot be is *complete*. `on.workflow_run` selects the
workflows it watches **by title**, and a title list is exactly the per-project value a
vendored file may not carry -- so the file shipped watching `Nightly` and nothing else.
In the workspace that produced it, one repo had since grown two more scheduled
workflows, and one of those had failed three consecutive weeks with nothing filed: the
reporter had never been told it existed. Every project is one `nightly.yml` sibling away
from the same silence, and nothing goes red when it happens.

`sweep` answers that by enumerating rather than subscribing. It reads the checked-out
`.github/workflows/` for **every** file declaring a `schedule:` trigger and reconciles
each against its latest run on the default branch, so a workflow added last week is
covered the first time the sweep runs, with no list to update anywhere.

Sweeping also reaches the failure the event-driven mode is structurally blind to: a
workflow that is not failing because it is **not running**. GitHub disables scheduled
workflows in a repository after 60 days without activity, and a disabled workflow emits
no completion event -- so the reporter goes quiet at exactly the moment there is
something to report, and that quiet is indistinguishable from health. The workflows API
says so outright in `state`, so the sweep reads it rather than inferring staleness from
run timestamps.

Both modes converge on the same trackers -- same titles, same dedup -- so running both
costs nothing and either alone still works.

Vendored from devkit (`sync-devkit.py`'s MANIFEST) and byte-identical everywhere:
nothing in it names a project. The repository, the workflow, the run and the owner all
arrive as environment variables; `sweep` additionally reads the workflow directory, which
is at the same path in every repository GitHub will run.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

LABEL = "ci-failure"
LABEL_COLOR = "b60205"
LABEL_DESCRIPTION = "A scheduled workflow is failing; opened and closed automatically"

# Conclusions that are evidence the run reached a verdict. Everything else --
# `cancelled`, `skipped`, `neutral`, `action_required`, `stale` -- is deliberately
# neither: a cancelled run is not evidence of health, so closing on it would hide a
# real failure, and it is not evidence of breakage either, so opening on it would file
# an issue about somebody pressing the cancel button.
FAILING = ("failure", "timed_out", "startup_failure")
PASSING = ("success",)

# How many open issues to scan for the tracking one. Dedup is by title over this
# window, so a repo with more open issues than this opens a second tracker rather than
# commenting on the first -- a duplicate, which is the harmless direction. The other
# direction, matching the wrong issue and then closing it, is impossible: the match is
# exact and the title names the workflow.
ISSUE_SCAN_LIMIT = 200


class GhError(RuntimeError):
    """A `gh` invocation that failed, with its stderr attached."""


def decide(conclusion: str) -> str:
    """`open`, `close`, or `ignore` for a run conclusion."""
    if conclusion in PASSING:
        return "close"
    if conclusion in FAILING:
        return "open"
    return "ignore"


def issue_title(workflow: str) -> str:
    """The dedupe key, and the reason it carries no `:`.

    It is matched exactly, so it must be a pure function of the workflow name -- no run
    number, no date, nothing that differs between two failures of the same workflow.
    """
    return f"{workflow} workflow is failing"


def run_facts(env: dict[str, str]) -> list[tuple[str, str]]:
    """The run's identifying details, in the order they are worth reading."""
    return [
        ("Run", env.get("RUN_URL", "(unknown)")),
        ("Attempt", env.get("RUN_ATTEMPT", "(unknown)")),
        ("Conclusion", env.get("CONCLUSION", "(unknown)")),
        ("Branch", env.get("HEAD_BRANCH", "(unknown)")),
        ("Commit", env.get("HEAD_SHA", "(unknown)")),
        ("Triggered by", env.get("TRIGGER", "(unknown)")),
    ]


def facts_table(env: dict[str, str]) -> str:
    rows = "\n".join(f"| {key} | {value} |" for key, value in run_facts(env))
    return f"| | |\n| --- | --- |\n{rows}"


def failure_body(env: dict[str, str]) -> str:
    workflow = env.get("WORKFLOW", "the workflow")
    return (
        f"`{workflow}` failed, and no change was pushed to trigger it -- so nothing "
        "else was going to report this.\n\n"
        f"{facts_table(env)}\n\n"
        "The usual causes are the ones a PR gate is structurally blind to: a dependency "
        "published inside this project's version bounds, a runner image bump, an "
        "expired credential, or a test that is flaky rather than broken.\n\n"
        "---\n\n"
        "This issue is opened, updated and closed by "
        "`.github/workflows/scheduled-failure-issue.yml`. It closes itself on the next "
        "successful run of the same workflow. Closing it by hand is fine -- the next "
        "failure opens a fresh one."
    )


def recurrence_comment(env: dict[str, str]) -> str:
    return f"Still failing.\n\n{facts_table(env)}"


def recovery_comment(env: dict[str, str]) -> str:
    workflow = env.get("WORKFLOW", "The workflow")
    return f"`{workflow}` succeeded, so this is closing itself.\n\n{facts_table(env)}"


def find_open_issue(issues: list[dict], title: str) -> int | None:
    """The number of the open issue tracking `title`, or None.

    Exact match: a substring match would let an unrelated issue that quotes the title in
    passing be commented on, and then closed by a green run.
    """
    for issue in issues:
        if issue.get("title") == title:
            number = issue.get("number")
            return None if number is None else int(number)
    return None


def gh(args: list[str], run) -> str:
    """Run `gh` and return stdout, raising with stderr attached on a non-zero exit.

    `check=False` plus an explicit raise rather than `check=True`: a
    `CalledProcessError` prints the argv and drops the stderr, and every failure mode
    here (a missing label, an unassignable owner, a token without `issues: write`) is
    diagnosable only from what `gh` wrote to stderr.
    """
    result = run(["gh", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise GhError(f"gh {' '.join(args)} exited {result.returncode}: {result.stderr.strip()}")
    return result.stdout


def open_issues(run) -> list[dict]:
    raw = gh(
        [
            "issue",
            "list",
            "--state",
            "open",
            "--limit",
            str(ISSUE_SCAN_LIMIT),
            "--json",
            "number,title",
        ],
        run,
    )
    return json.loads(raw or "[]")


def ensure_label(run) -> None:
    """Create the label if it is absent.

    `--force` is what makes this both idempotent and safe on a repo that has never had
    the label -- the same reason `dependabot-automerge.yml` uses it before its first
    `gh pr edit --add-label`, where a missing label failed the whole job on something
    unrelated to the bump it was judging.
    """
    _ = gh(
        [
            "label",
            "create",
            LABEL,
            "--color",
            LABEL_COLOR,
            "--force",
            "--description",
            LABEL_DESCRIPTION,
        ],
        run,
    )


def assign(issue: str, owner: str, run) -> str:
    """Assign `issue` to `owner`, returning a line describing what happened.

    Tolerant by design, and a separate call from creation for that reason.
    `repository_owner` is an organisation for an organisation-owned repo, and an
    organisation cannot be assigned; a user without write access cannot be either. Both
    are configuration facts this workflow has no way to know and no business failing the
    run over -- but they cost the issue its place in the owner's dashboard, so they are
    reported rather than passed over.
    """
    if not owner:
        return "no owner given, so it is unassigned and will not reach a dashboard"
    try:
        gh(["issue", "edit", issue, "--add-assignee", owner], run)
    except GhError as error:
        return (
            f"could not assign to {owner!r} ({error}). The issue exists but appears in "
            "nobody's assigned view -- assign it by hand, or point OWNER at an "
            "assignable user."
        )
    return f"assigned to {owner}"


def apply(
    action: str,
    title: str,
    issues: list[dict],
    *,
    body: str,
    comment: str,
    owner: str,
    run,
) -> str:
    """Bring the tracker for `title` into line with `action`, returning a log line.

    Shared by both modes so they cannot drift into filing two differently-shaped issues
    about the same thing. `issues` is passed in rather than fetched here because the
    sweep reconciles many workflows against a single listing.
    """
    existing = find_open_issue(issues, title)

    if action == "close":
        if existing is None:
            return "passed, and nothing was open."
        gh(["issue", "close", str(existing), "--reason", "completed", "--comment", comment], run)
        return f"passed; closed #{existing}."

    if existing is not None:
        gh(["issue", "comment", str(existing), "--body", comment], run)
        return f"still failing; commented on #{existing}."

    ensure_label(run)
    url = gh(["issue", "create", "--title", title, "--body", body, "--label", LABEL], run).strip()
    return f"opened {url} ({assign(url, owner, run)})."


# --- sweep mode: every scheduled workflow, not only the one that just finished ---

DEFAULT_WORKFLOWS_DIR = ".github/workflows"

SWEEP_MODE = "sweep"

# The `state` the workflows API reports when GitHub turned a scheduled workflow off by
# itself, after 60 days without repository activity. `disabled_manually` is deliberately
# not here: that switch was flipped on purpose, and reporting it would file the same
# issue about the same decision on every sweep, forever.
STOPPED_STATE = "disabled_inactivity"

# `on` is the YAML 1.1 boolean `true`, so a workflow may spell that key any of these
# ways and GitHub accepts all of them. A file that writes `True:` is not hypothetical --
# it is what a YAML library emits when it round-trips a workflow.
_ON_KEY = re.compile(r"^(?:on|\"on\"|'on'|True|true):\s*(.*?)\s*$")
_NAME_KEY = re.compile(r"^name:\s*(.*?)\s*$")
_MAPPING_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):")


def _unquote(value: str) -> str:
    return value.strip().strip("\"'")


def _block_keys(lines: list[str]) -> set[str]:
    """The mapping keys of one indented block, ignoring anything nested deeper.

    Only keys at the block's *own* indent count, which is what stops `cron:` under
    `schedule:` from being read as a trigger in its own right.
    """
    keys: set[str] = set()
    depth: int | None = None
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            break
        if depth is None:
            depth = indent
        if indent != depth:
            continue
        match = _MAPPING_KEY.match(line.strip())
        if match:
            keys.add(match.group(1))
    return keys


def workflow_triggers(text: str) -> set[str]:
    """The event names under a workflow's `on:`.

    Text parsing rather than PyYAML, for the same reason every hook here is stdlib-only:
    this runs on the runner's bare Python, before any project install and often instead
    of one. All three spellings that appear in real workflows are handled -- a block
    mapping, an inline `[a, b]` list, and a bare `on: push`.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = _ON_KEY.match(line)
        if not match:
            continue
        inline = match.group(1)
        if inline.startswith("["):
            return {_unquote(part) for part in inline.strip("[]").split(",") if part.strip()}
        if inline and not inline.startswith("#"):
            return {_unquote(inline)}
        return _block_keys(lines[index + 1 :])
    return set()


def workflow_title(text: str, fallback: str) -> str:
    """A workflow's `name:`, or the fallback GitHub itself uses -- the file path.

    The regex is anchored at column 0 so a job's or a step's `name:` cannot win: those
    are indented, and the first one in a file is usually only a few lines below.
    """
    for line in text.splitlines():
        match = _NAME_KEY.match(line)
        if match:
            return _unquote(match.group(1))
    return fallback


def scheduled_workflows(directory: Path) -> list[tuple[str, str]]:
    """`(file name, title)` for every workflow in `directory` declaring a `schedule:`.

    Sorted, so two sweeps read the same way and a diff between them is about what
    changed. Anything not `.yml`/`.yaml` is skipped, which is also what excludes the
    `*.yml.disabled` spelling projects use to park a workflow without deleting it --
    GitHub does not run those either.
    """
    found: list[tuple[str, str]] = []
    if not directory.is_dir():
        return found
    for path in sorted(directory.iterdir()):
        if path.suffix not in (".yml", ".yaml") or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "schedule" not in workflow_triggers(text):
            continue
        found.append((path.name, workflow_title(text, path.name)))
    return found


def api_workflow_states(repo: str, run) -> dict[str, str]:
    """File name -> `state`, for every workflow GitHub knows about in `repo`.

    Keyed by file name rather than by title because a title is what the *file* says and
    a state is what *GitHub* says -- and the whole point of reading this is the case
    where those two have come apart.
    """
    raw = gh(
        [
            "api",
            "--paginate",
            f"repos/{repo}/actions/workflows",
            "--jq",
            ".workflows[] | [.path, .state] | @tsv",
        ],
        run,
    )
    states: dict[str, str] = {}
    for line in raw.splitlines():
        if "\t" not in line:
            continue
        path, state = line.split("\t", 1)
        states[path.strip().rsplit("/", 1)[-1]] = state.strip()
    return states


def latest_run(repo: str, file_name: str, branch: str, run) -> dict | None:
    """The most recent run of one workflow on `branch`, or None if it has never run there.

    `exclude_pull_requests` keeps a workflow that is *also* wired to `pull_request` from
    being judged on a PR's run, and the branch filter is what confines the verdict to the
    unattended tier -- a scheduled run only ever happens on the default branch.
    """
    raw = gh(
        [
            "api",
            f"repos/{repo}/actions/workflows/{file_name}/runs"
            f"?branch={branch}&per_page=1&exclude_pull_requests=true",
        ],
        run,
    )
    runs = json.loads(raw or "{}").get("workflow_runs") or []
    return runs[0] if runs else None


def stopped_title(workflow: str) -> str:
    """The tracker for a workflow that is not running at all.

    A different title from `issue_title` on purpose: "failing" and "not running" are
    different repairs, and one tracker flipping between them would lose the distinction
    at exactly the moment the second is true.
    """
    return f"{workflow} workflow has stopped running"


def stopped_body(workflow: str, repo: str) -> str:
    return (
        f"`{workflow}` is still in this repository, but GitHub has **disabled** it -- so "
        "it is not running on its schedule, and it will report nothing at all, including "
        "the failures it exists to catch.\n\n"
        "GitHub disables scheduled workflows in a repository after 60 days without "
        "activity. Nothing goes red when it happens: the workflow simply stops, and a "
        "workflow that stopped looks exactly like a workflow that keeps passing.\n\n"
        "Re-enable it from the Actions tab, or with:\n\n"
        f"    gh workflow enable {workflow!r} --repo {repo}\n\n"
        "---\n\n"
        "Opened by the `sweep` mode of `scripts/report-workflow-failure.py`, which closes "
        "it again once the workflow is active."
    )


def run_env(info: dict, title: str) -> dict[str, str]:
    """The event mode's environment block, rebuilt from an API run object.

    So the sweep files issues through `failure_body` and `recovery_comment` rather than
    growing a second set: two report shapes for one condition is how a reader learns to
    distrust both.
    """
    return {
        "WORKFLOW": title,
        "RUN_URL": info.get("html_url") or "(unknown)",
        "RUN_ATTEMPT": str(info.get("run_attempt") or "(unknown)"),
        "CONCLUSION": info.get("conclusion") or "(unknown)",
        "HEAD_BRANCH": info.get("head_branch") or "(unknown)",
        "HEAD_SHA": info.get("head_sha") or "(unknown)",
        "TRIGGER": info.get("event") or "(unknown)",
    }


def sweep(env: dict[str, str], run) -> int:
    repo = env.get("GH_REPO", "").strip()
    branch = env.get("DEFAULT_BRANCH", "").strip()
    if not repo or not branch:
        print("sweep needs GH_REPO and DEFAULT_BRANCH -- refusing to guess.", file=sys.stderr)
        return 1

    directory = Path(env.get("WORKFLOWS_DIR") or DEFAULT_WORKFLOWS_DIR)
    scheduled = scheduled_workflows(directory)
    if not scheduled:
        print(f"No workflow in {directory} declares a `schedule:` trigger; nothing to sweep.")
        return 0

    states = api_workflow_states(repo, run)
    owner = env.get("OWNER", "")
    # One listing for the whole sweep. Each workflow's title is distinct, so an issue
    # opened for one cannot be missed when looking up the next; a workflow sharing
    # another's `name:` would open a duplicate, which is the harmless direction.
    issues = open_issues(run)

    for file_name, title in scheduled:
        if states.get(file_name) == STOPPED_STATE:
            # Hoisted out of the f-string: a line break inside a replacement field is
            # Python 3.12 syntax, and this file ships to consumers whose ruff still
            # targets 3.11 -- where it is a lint-time syntax error that refuses the
            # adopting commit.
            stopped_outcome = apply(
                "open",
                stopped_title(title),
                issues,
                body=stopped_body(title, repo),
                comment=f"`{title}` is still disabled.",
                owner=owner,
                run=run,
            )
            print(f"{title}: {stopped_outcome}")
            continue

        # Active again: retire the stopped tracker before judging any run, so a workflow
        # that was re-enabled and then failed does not carry both issues at once.
        outcome = apply(
            "close",
            stopped_title(title),
            issues,
            body="",
            comment=f"`{title}` is running again, so this is closing itself.",
            owner=owner,
            run=run,
        )
        if "closed" in outcome:
            print(f"{title}: re-enabled; {outcome}")

        info = latest_run(repo, file_name, branch, run)
        if info is None:
            print(f"{title}: has never run on {branch}; nothing to judge.")
            continue

        conclusion = (info.get("conclusion") or "").strip()
        action = decide(conclusion)
        if action == "ignore":
            print(f"{title}: last run on {branch} concluded {conclusion!r}; no verdict.")
            continue

        facts = run_env(info, title)
        # Same 3.11 constraint as the stopped-workflow print above.
        verdict_outcome = apply(
            action,
            issue_title(title),
            issues,
            body=failure_body(facts),
            comment=recurrence_comment(facts) if action == "open" else recovery_comment(facts),
            owner=owner,
            run=run,
        )
        print(f"{title}: {verdict_outcome}")

    return 0


def report(env: dict[str, str], run) -> int:
    workflow = env.get("WORKFLOW", "").strip()
    if not workflow:
        print("WORKFLOW is empty -- nothing to track.", file=sys.stderr)
        return 1

    conclusion = env.get("CONCLUSION", "").strip()
    action = decide(conclusion)
    if action == "ignore":
        print(f"{workflow} concluded {conclusion!r}: no verdict either way, doing nothing.")
        return 0

    title = issue_title(workflow)
    existing = find_open_issue(open_issues(run), title)

    if action == "close":
        if existing is None:
            print(f"{workflow} passed and nothing was open. Nothing to do.")
            return 0
        gh(
            [
                "issue",
                "close",
                str(existing),
                "--reason",
                "completed",
                "--comment",
                recovery_comment(env),
            ],
            run,
        )
        print(f"{workflow} passed; closed #{existing}.")
        return 0

    if existing is not None:
        gh(["issue", "comment", str(existing), "--body", recurrence_comment(env)], run)
        print(f"{workflow} failed again; commented on #{existing}.")
        return 0

    ensure_label(run)
    url = gh(
        ["issue", "create", "--title", title, "--body", failure_body(env), "--label", LABEL],
        run,
    ).strip()
    print(f"{workflow} failed; opened {url} ({assign(url, env.get('OWNER', ''), run)}).")
    return 0


def main(env: dict[str, str] | None = None, run=None) -> int:
    """Dispatch on `MODE`, defaulting to the event-driven report.

    The default matters: a project whose vendored workflow predates the sweep passes no
    `MODE` at all, and it must keep reporting exactly as it did rather than fail on an
    unset variable.
    """
    env = dict(os.environ) if env is None else env
    run = subprocess.run if run is None else run
    if env.get("MODE", "").strip() == SWEEP_MODE:
        return sweep(env, run)
    return report(env, run)


if __name__ == "__main__":
    raise SystemExit(main())
