"""Contract: every project carries the same baseline CI surface.

**This file is vendored into every consuming project**, so nothing here may assert a
value specific to one project. What it asserts is devkit's policy about *which* GitHub
Actions files every repo has, plus the cross-cutting settings that make a workflow safe
to leave running unattended.

Why a contract test rather than more vendoring. Some of the required files carry no
per-project content and are therefore shipped whole: `dependabot-automerge.yml` and
`scheduled-failure-issue.yml` are in `sync-devkit.py`'s MANIFEST and drift-checked
byte-for-byte -- what each waits on is a title every project shares, and neither names
anything else about the repo it runs in. That constraint is also the reporter's limit:
`on.workflow_run` can only ever name `Nightly`, so its second, scheduled job enumerates
the workflow directory instead of subscribing to a list it is not allowed to hold. The
rest cannot be vendored at all. A
`dependabot.yml` names the ecosystems this project actually has; a gate or a nightly
names its services, its migrations and its frontend tier, and carameli's five-job gate
is the standing proof that a shared one would have to delete real work or live
permanently exempted.

`setup-python-env/action.yml` was briefly in that first group and is not any more: how
a project installs its dependencies is exactly the kind of thing that varies. Vendoring
it deleted apt-finder's private-sibling clone and handed carameli a `uv sync` it has no
lock for. Rendered from `templates/` now, like the gate.

So those two are rendered from `templates/`, which is a **one-shot copy**: `--pull`
never looks at a template again. Nothing checked that a project still had one, or had
ever been handed one. It showed. Across the six repos in this workspace: one had a
nightly, five had a dependency-update config, and two had no way to merge a Dependabot
PR at all. Every one of those gaps is silent -- a repo with no nightly does not fail,
it simply never learns that the world moved underneath a default branch nobody pushed
to, and a repo with no auto-merge accumulates bot PRs that pass their gate and sit.

This file is the half `--pull` cannot do: it does not supply the workflow, it refuses
to let a project go without one.

Stdlib-only text parsing, no PyYAML -- the vendored suite runs as its own step and may
not assume the project's environment holds anything but the standard library.
"""

import re
import sys
from pathlib import Path

from conftest import REPO_ROOT

WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
DEPENDABOT = REPO_ROOT / ".github" / "dependabot.yml"

PR_GATE = "pr-gate.yml"
AUTOMERGE = "dependabot-automerge.yml"
NIGHTLY = "nightly.yml"
FAILURE_REPORTER = "scheduled-failure-issue.yml"

# The title `dependabot-automerge.yml` waits on. That file is vendored byte-identical,
# so it cannot parameterise the name -- every project's gate is titled `PR Gate`.
GATE_TITLE = "PR Gate"
# And the title `scheduled-failure-issue.yml` waits on, for the same reason.
NIGHTLY_TITLE = "Nightly"

# What a project is told to write when it has no nightly. It lives in the failing
# assertion because the remedy for a missing *template* render is the one thing
# `--pull` can never deliver.
NIGHTLY_REMEDY = """\
Add .github/workflows/nightly.yml -- the scheduled full run against the default
branch. The PR gate only fires on a change, so it is structurally blind to every
failure that arrives without one: a dependency published inside this project's
version bounds, a runner image bump, an expired credential, a test that is flaky
rather than broken. At minimum:

    name: Nightly
    on:
      schedule:
        - cron: "0 2 * * *"
      workflow_dispatch:
    permissions:
      contents: read
    concurrency:
      group: ${{ github.workflow }}
      cancel-in-progress: false
    jobs:
      full-suite:
        runs-on: ubuntu-latest
        steps:
          - uses: actions/checkout@v7
          - uses: ./.github/actions/setup-python-env
            with:
              python-version: "3.12"
          - run: uv run python scripts/run-tests.py
          - run: uv run python -m pytest scripts/hooks/tests/ -q

Add this project's own tiers (services, migrations, a frontend suite) the way its
PR gate does. devkit renders one for new projects; an older project predates that
and has to be given one by hand, because templates/ is a one-shot copy."""


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _code_lines(text: str) -> list[str]:
    """Lines whose first non-space character is not `#`, blanks dropped.

    Every scan below runs over these. Workflow headers in this harness deliberately
    *name* the trigger they must never grow ("it must NEVER gain a `schedule:`"), so a
    scan that read comments would find the word it was looking for inside the very
    paragraph forbidding it.
    """
    return [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def _workflows() -> list[Path]:
    return sorted(WORKFLOWS_DIR.glob("*.yml"))


def _top_level_block(text: str, key: str) -> list[str] | None:
    """The lines under a column-0 `key:`, indentation preserved, or None if absent.

    An empty list means the key is present with no indented body (`permissions: {}`),
    which is a real shape and distinct from the key not being there at all -- so
    callers test against None, never against falsiness.
    """
    lines = _code_lines(text)
    for index, line in enumerate(lines):
        if not re.fullmatch(rf"{re.escape(key)}:\s*(?:\S.*)?", line):
            continue
        body = []
        for following in lines[index + 1 :]:
            if not following.startswith((" ", "\t")):
                break
            body.append(following)
        return body
    return None


def _direct_keys(body: list[str]) -> list[str]:
    """The mapping keys directly under a block, ignoring anything nested deeper.

    `on:` carries `push:` and, under it, `branches:`. Both look like keys to a flat
    regex, and "the workflow has a `branches` trigger" is the sort of nonsense that
    makes a scan pass for the wrong reason.
    """
    indents = [len(ln) - len(ln.lstrip()) for ln in body]
    if not indents:
        return []
    depth = min(indents)
    return [
        match.group(1)
        for ln in body
        if len(ln) - len(ln.lstrip()) == depth and (match := re.match(r"\s*([A-Za-z_][\w-]*):", ln))
    ]


def _triggers(text: str) -> list[str]:
    return _direct_keys(_top_level_block(text, "on") or [])


# --- the files every project has ----------------------------------------------


def test_the_workflow_scan_is_not_vacuous():
    """Guard against every loop below passing over an empty set.

    Not a skip: a repo running this suite is a repo whose PR gate invoked it, so an
    absent workflow directory is not "no CI tier here", it is the tier having moved
    somewhere nothing checks.
    """
    assert WORKFLOWS_DIR.is_dir(), f"{WORKFLOWS_DIR.relative_to(REPO_ROOT)} does not exist"
    assert _workflows(), "no *.yml under .github/workflows -- the layout changed"


def test_dependency_updates_are_configured():
    """`.github/dependabot.yml`, with the actions ecosystem in it.

    The actions entry is asserted specifically because it is the one every project has
    and the one most often left out: a workflow pins `actions/checkout@v7`, and with no
    `github-actions` ecosystem declared nothing on earth moves that pin. The Python,
    npm and docker entries are the project's own call -- they follow from which
    manifests it actually ships.
    """
    assert DEPENDABOT.is_file(), (
        ".github/dependabot.yml is missing. Without it nothing opens an update PR, so "
        "the auto-merge workflow this project already carries has nothing to act on, "
        "and every pin ages in place -- security advisories included."
    )
    text = _read(DEPENDABOT)
    assert re.search(r"^version:\s*2\s*$", text, re.M), (
        ".github/dependabot.yml declares no `version: 2`; GitHub ignores the file"
    )
    ecosystems = set(re.findall(r"package-ecosystem:\s*['\"]?([\w-]+)", text))
    assert "github-actions" in ecosystems, (
        f".github/dependabot.yml covers {sorted(ecosystems)} but not `github-actions`. "
        "Every workflow here pins actions by major, and nothing else moves those pins."
    )


def test_the_pr_gate_exists_and_carries_the_title_automerge_waits_on():
    """`workflow_run` matches by title, so a rename makes the merge job inert.

    Nothing goes red when it does: an unmatched `workflow_run` produces no run at all,
    so bot PRs simply sit open behind a gate that passed.
    """
    gate = WORKFLOWS_DIR / PR_GATE
    assert gate.is_file(), f".github/workflows/{PR_GATE} is missing"
    assert re.search(rf"^name:\s*{re.escape(GATE_TITLE)}\s*$", _read(gate), re.M), (
        f"{PR_GATE} is not titled {GATE_TITLE!r}. {AUTOMERGE} is vendored "
        "byte-identical and waits on that exact title in every project."
    )


def test_dependabot_prs_have_something_that_merges_them():
    assert (WORKFLOWS_DIR / AUTOMERGE).is_file(), (
        f".github/workflows/{AUTOMERGE} is missing -- run "
        "`python scripts/sync-devkit.py --pull`. It is a MANIFEST file, so it arrives "
        "whole; a project without it collects passing bot PRs nobody merges, which is "
        "the state that makes people turn Dependabot off."
    )


def test_the_merge_job_is_gated_by_label_not_by_author():
    """The `automerge` label is the whole authorization, for every PR author.

    The merge job once required Dependabot as both the run's actor and the PR's
    author, which silently excluded the routine PRs the label exists for -- devkit
    upgrades, and anything a human labels instead of babysitting the gate. Only
    write access can apply a label, so an author condition adds no safety the label
    does not already carry; reintroducing one turns labelled PRs back into ones
    that sit open behind a passed gate.
    """
    text = _read(WORKFLOWS_DIR / AUTOMERGE)
    assert "workflow_run.actor" not in text, (
        f"{AUTOMERGE}'s merge job conditions on the workflow_run actor again; the "
        "`automerge` label is the authorization, and an actor condition makes the "
        "job skip every labelled non-Dependabot PR."
    )
    assert "scripts/merge-dependabot-prs.py" in text, (
        f"{AUTOMERGE} no longer delegates to scripts/merge-dependabot-prs.py -- that "
        "script is where the `automerge`-label check lives (pinned by "
        "test_merge_dependabot_prs.py), so without it every PR whose gate passes "
        "merges itself."
    )


def test_dependency_update_prs_are_assigned_to_someone():
    """Otherwise they are visible in no aggregate view, in any repo.

    Every tab on `github.com/pulls` is keyed to the viewer -- created, assigned,
    mentioned, review-requested -- and a Dependabot PR is none of those: the bot is the
    author, it mentions nobody, and it requests no review. The PRs are therefore
    reachable only by opening each repository in turn, which is exactly the poll that
    does not happen, and the state that ends with Dependabot turned off because "it just
    piles up".

    `assignees` is the one qualifier a config file can set that lands the PR in a tab
    somebody already reads. Which login goes there is the project's own business; that
    one is named is not.
    """
    text = _read(DEPENDABOT)
    assert re.search(r"^\s*assignees:\s*$", text, re.M), (
        ".github/dependabot.yml sets no `assignees:`, so its PRs appear under none of "
        "the tabs on github.com/pulls -- not created, not assigned, not mentioned, not "
        "review-requested -- and can only be found by opening this repo. Add it to "
        "every `updates:` entry:\n\n"
        "    assignees:\n"
        "      - <your-github-login>\n\n"
        "It is per-entry, not global: an entry without it is an ecosystem whose bumps "
        "stay invisible."
    )
    entries = len(re.findall(r"^\s*-\s*package-ecosystem:", text, re.M))
    assigned = len(re.findall(r"^\s*assignees:\s*$", text, re.M))
    assert assigned == entries, (
        f".github/dependabot.yml declares {entries} ecosystems but assigns {assigned} "
        "of them. `assignees` is a per-update-entry key, so the unassigned ones open "
        "PRs that reach no dashboard."
    )


def test_a_failed_scheduled_run_becomes_an_issue():
    """A workflow run is the least visible artifact GitHub has.

    It lives in one repo's Actions tab, and the cross-repository dashboards aggregate
    issues and pull requests and nothing else -- so a red nightly and a nightly that
    silently stopped running are the same observation: nothing. This file converts the
    first into an assigned issue, and closes it again when the workflow goes green.
    """
    assert (WORKFLOWS_DIR / FAILURE_REPORTER).is_file(), (
        f".github/workflows/{FAILURE_REPORTER} is missing -- run "
        "`python scripts/sync-devkit.py --pull`. It is a MANIFEST file, so it arrives "
        "whole, with the script it runs. Without it a scheduled failure is visible only "
        "to someone who opens this repository's Actions tab and looks, which is the "
        "poll a nightly exists to make unnecessary."
    )


def test_a_scheduled_full_run_exists():
    nightly = WORKFLOWS_DIR / NIGHTLY
    assert nightly.is_file(), NIGHTLY_REMEDY
    triggers = _triggers(_read(nightly))
    assert "schedule" in triggers, (
        f"{NIGHTLY} declares {sorted(triggers)} and no `schedule:` -- it is the "
        "scheduled tier or it is nothing."
    )
    assert "workflow_dispatch" in triggers, (
        f"{NIGHTLY} has no `workflow_dispatch:`. A scheduled run that cannot also be "
        "triggered by hand can only be debugged one attempt per day."
    )


def test_the_nightly_carries_the_title_the_failure_reporter_waits_on():
    """`workflow_run` matches by title, and an unmatched one produces no run at all.

    The same trap as the gate's title, with a worse symptom: a renamed nightly still
    fails visibly *in its own repo*, so nothing looks broken, while the reporter that
    was supposed to carry that failure into a dashboard silently stops firing. The
    failure of a failure-reporter is the one nobody notices.
    """
    nightly = WORKFLOWS_DIR / NIGHTLY
    assert nightly.is_file(), NIGHTLY_REMEDY
    assert re.search(rf"^name:\s*{re.escape(NIGHTLY_TITLE)}\s*$", _read(nightly), re.M), (
        f"{NIGHTLY} is not titled {NIGHTLY_TITLE!r}. {FAILURE_REPORTER} is vendored "
        "byte-identical and waits on that exact title in every project. Add the tiers "
        "you want to this file, but keep the name."
    )


def test_the_failure_reporter_also_sweeps_on_its_own_schedule():
    """The title above buys coverage for `Nightly` and for nothing else.

    `on.workflow_run` selects by title, and a title list is precisely the per-project
    value a vendored file may not carry -- so the reporter's event-driven half watches
    one workflow, permanently. A project's *second* scheduled workflow is therefore
    uncovered the moment it is added, and uncovered silently: it fails in an Actions tab,
    files nothing, and the reporter that was supposed to carry it looks healthy because
    the workflow it does watch is green.

    That is not hypothetical. It is how a `Weekly Hardening` came to fail three
    consecutive Sundays in one of the repos this harness ships to, with no issue, while
    that repo's nightly tracker sat correctly closed.

    The `schedule:` trigger is the sweep half, which enumerates every scheduled workflow
    in this directory instead of subscribing to one. A copy that has lost it still
    reports nightlies, so nothing here looks broken -- which is exactly why it is gated.
    """
    reporter = WORKFLOWS_DIR / FAILURE_REPORTER
    assert reporter.is_file(), f".github/workflows/{FAILURE_REPORTER} is missing."
    triggers = _triggers(_read(reporter))
    assert "schedule" in triggers, (
        f"{FAILURE_REPORTER} declares {sorted(triggers)} and no `schedule:`. Without it "
        "only the workflow named in its `workflow_run` filter is ever reported on, and "
        "every other scheduled workflow in this repo fails into silence. Run "
        "`python scripts/sync-devkit.py --pull`."
    )
    assert "workflow_run" in triggers, (
        f"{FAILURE_REPORTER} has no `workflow_run:` trigger, so a nightly failure waits "
        "for the next sweep instead of filing immediately, and a fix does not close its "
        "issue until then either."
    )


# --- the settings that make an unattended run safe -----------------------------


def test_every_workflow_declares_top_level_permissions():
    for path in _workflows():
        assert _top_level_block(_read(path), "permissions") is not None, (
            f"{path.name}: no top-level `permissions:` block, so it inherits whatever "
            "the repository default is -- write, on a repo nobody narrowed. Declare "
            "`permissions:` / `  contents: read`; a job needing more opts in with its "
            "own block."
        )


def test_every_workflow_declares_concurrency():
    """No exemptions left, and the one that used to be here is worth recording.

    `dependabot-automerge.yml` was exempt because its merge job is driven by
    `workflow_run` completions and a shared group would drop one of two branches'
    completions as superseded. That reasoning was about the *key*, not about concurrency
    itself -- and when the file gained a `schedule:` for its retry sweep, the exemption
    would have silently swallowed the requirement that a scheduled run be allowed to
    finish. It keys on `github.event.workflow_run.head_branch || github.run_id` now, so
    branches stay independent and anything without a branch gets a group of its own.
    """
    for path in _workflows():
        assert _top_level_block(_read(path), "concurrency") is not None, (
            f"{path.name}: no top-level `concurrency:` block, so redundant runs pile "
            "up on the same ref."
        )


def test_a_scheduled_run_is_allowed_to_finish():
    checked = 0
    for path in _workflows():
        text = _read(path)
        if "schedule" not in _triggers(text):
            continue
        checked += 1
        body = _top_level_block(text, "concurrency") or []
        cancels = [ln.strip() for ln in body if ln.strip().startswith("cancel-in-progress:")]
        assert cancels, (
            f"{path.name} is scheduled but its concurrency group leaves "
            "`cancel-in-progress` unset, which defaults to false only by convention. "
            "State it."
        )
        value = cancels[0].split(":", 1)[1].strip()
        assert value == "false", (
            f"{path.name}: a scheduled run must be allowed to finish "
            f"(`cancel-in-progress: false`), not {value!r}. A manual dispatch during "
            "the scheduled window, or a slow run overrunning into the next one, would "
            "otherwise kill the only unattended signal the default branch gets."
        )
    assert checked, "no scheduled workflow was examined -- the trigger scan is inert"


USES = re.compile(r"uses:\s*([\w.-]+/[\w./-]+)@(\S+)")
IMMUTABLE_REF = re.compile(r"v?\d+(?:\.\d+)*|[0-9a-f]{40}")


def test_third_party_actions_are_pinned_to_an_immutable_ref():
    """A branch pin lets someone else's push change what this repo runs.

    Local composite actions (`uses: ./.github/actions/...`) carry no `@ref` and so are
    not matched -- they are this repo's own files, gated like anything else in it.
    """
    floating = [
        f"{path.name}: {action}@{ref}"
        for path in _workflows()
        for action, ref in USES.findall(_read(path))
        if not IMMUTABLE_REF.fullmatch(ref)
    ]
    assert not floating, f"actions pinned to a mutable ref: {floating}"


# Every `uses: ./some/path` in a workflow. Deliberately separate from `USES`, which
# matches the `owner/repo@ref` form -- a local action carries no `@ref` at all.
LOCAL_USES = re.compile(r"^\s*-?\s*uses:\s*(\./[^\s\"'#]+)", re.MULTILINE)


def _local_action_targets() -> list[tuple[Path, str]]:
    """`(workflow, path)` for every local action reference across the workflows."""
    return [(path, ref) for path in _workflows() for ref in LOCAL_USES.findall(_read(path))]


def test_every_local_action_a_workflow_uses_actually_exists():
    """A `uses: ./...` naming a directory this repo does not have fails the job at
    startup, before a single step runs.

    **This is the check that would have caught the un-vendoring.** When
    `.github/actions/setup-python-env/action.yml` moved from the MANIFEST back to
    `templates/`, `--pull` deleted it from every consumer that had not customised it and
    put nothing back -- `templates/` is a one-shot copy, consulted only when a project is
    generated. Two projects' gates died on the next push. A third's *nightly* was the
    only workflow using it, so nothing failed anywhere a human was looking, and it
    stayed broken for a week.

    Every previous test in this file asks whether a file exists. This one asks whether
    the files *agree with each other*, which is the failure a one-shot tier produces:
    nothing is missing from any single point of view.

    Portable by construction -- it reads this repo's own `.github/` and nothing else --
    which is why it belongs in the vendored tier rather than in any one project's suite.
    Had it been here, every consumer would have gone red in the same commit that broke
    it, instead of one CI run at a time.
    """
    missing = [
        f"{workflow.name}: uses {ref}, which is not in this repo"
        for workflow, ref in _local_action_targets()
        if not _resolves(ref)
    ]
    assert not missing, (
        "workflow references a local action that does not exist:\n  "
        + "\n  ".join(missing)
        + "\n\nA deleted composite action fails the job at startup. If devkit un-vendored "
        "it, the file is yours now: restore it from the project's history "
        "(`git log --diff-filter=D -- <path>`) or render it from devkit's "
        "`templates/core/dot-github/`."
    )


def _resolves(ref: str) -> bool:
    """Whether `./x` names an action this repo has.

    Both spellings count: a directory holding `action.yml`/`action.yaml`, which is the
    composite form, and a direct path to the file.
    """
    target = REPO_ROOT / ref[2:]
    return (
        (target / "action.yml").is_file() or (target / "action.yaml").is_file() or target.is_file()
    )


# --- the parsers above, on inputs whose answer is known ------------------------


def test_the_local_action_pattern_matches_the_shapes_a_workflow_uses():
    """Pinned because a regex that silently matched nothing would make the check above
    vacuous -- the same trap `test_the_workflow_scan_is_not_vacuous` guards for."""
    text = "steps:\n  - uses: ./.github/actions/setup-python-env\n  - uses: actions/checkout@v7\n"
    assert LOCAL_USES.findall(text) == ["./.github/actions/setup-python-env"]
    # A commented-out reference is not a reference.
    assert LOCAL_USES.findall("  # - uses: ./.github/actions/gone\n") == []


def test_a_local_action_resolves_by_directory_or_by_file(tmp_path, monkeypatch):
    """Both spellings GitHub accepts, against a tree whose answer is known.

    Built rather than borrowed from this repo: the vendored suite may assert nothing
    about which actions a particular project happens to have.
    """
    monkeypatch.setattr(sys.modules[__name__], "REPO_ROOT", tmp_path)
    composite = tmp_path / ".github" / "actions" / "setup"
    composite.mkdir(parents=True)
    assert not _resolves("./.github/actions/setup"), "a directory with no action.yml"

    (composite / "action.yml").write_text("runs:\n", encoding="utf-8")
    assert _resolves("./.github/actions/setup")

    (composite / "action.yml").rename(composite / "action.yaml")
    assert _resolves("./.github/actions/setup"), "the .yaml spelling counts too"

    assert not _resolves("./.github/actions/never-created")


def test_block_parser_reads_only_column_zero_keys():
    text = "on:\n  push:\n    branches: [main]\njobs:\n  build:\n    concurrency: x\n"
    assert _top_level_block(text, "on") == ["  push:", "    branches: [main]"]
    # That `concurrency:` is a job's, not the workflow's -- the distinction the
    # permissions and concurrency checks rest on.
    assert _top_level_block(text, "concurrency") is None


def test_block_parser_distinguishes_an_empty_body_from_an_absent_key():
    assert _top_level_block("permissions: {}\njobs:\n", "permissions") == []
    assert _top_level_block("jobs:\n", "permissions") is None


def test_block_parser_ignores_commented_out_keys():
    commented = "# schedule:\n#   - cron: '0 2 * * *'\non:\n  push:\n"
    assert _top_level_block(commented, "schedule") is None


def test_direct_keys_ignores_nested_mappings():
    body = ["  push:", "    branches: [main]", "  schedule:", "    - cron: '0 2 * * *'"]
    assert _direct_keys(body) == ["push", "schedule"]


def test_immutable_ref_accepts_tags_and_shas_and_rejects_branches():
    for good in ("v7", "v1.2.3", "3.12.1", "a" * 40):
        assert IMMUTABLE_REF.fullmatch(good), good
    for bad in ("main", "master", "v7-beta", "latest"):
        assert not IMMUTABLE_REF.fullmatch(bad), bad
