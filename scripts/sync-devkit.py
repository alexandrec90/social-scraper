#!/usr/bin/env python3
"""Vendor the shared agent-harness scripts into this project, and drift-check them.

The hook/harness scripts are meant to be identical across projects (see
`.devkit.toml` for the per-project seam). Rather than fork them per repo,
one **shared harness repo is the source of truth** and each project commits a
**vendored copy** -- so cloning a single project still gets everything, with no
submodule. This script keeps that copy honest:

  - `--check` (default): fail (exit 1) if any vendored file drifts from the shared
    repo. Wired into the PR gate. **No-ops clean (exit 0) when the shared repo is
    not configured and this project has never pulled**, so CI is safe before a
    project adopts the shared repo; once `DEVKIT_VERSION` exists, an unresolvable
    source is itself a failure rather than a skip (see `main`).
  - `--pull`: copy the shared repo's version into this project (adopt upstream).
    Also remove exact paths retired by the shared repo; project-owned skill state
    is never included in that retirement list.
  - `--push`: copy this project's version into the shared repo (author a change /
    seed a fresh shared repo from the project that currently owns the code).
  - `--list`: print the manifest and resolved source, then exit.

The source resolves from `--src` or `$DEVKIT_DIR`; `.devkit.toml` stays project-owned.

`MANIFEST` is the reviewed, portable subset; extend it as more scripts are decoupled.
Pure helpers are unit-tested in `scripts/hooks/tests/test_sync_devkit.py`.

`BLOCK_MANIFEST` gates a *region* delimited by `<!-- devkit:begin <id> -->` /
`<!-- devkit:end <id> -->` byte-for-byte while
the rest of the file stays the project's own. It exists for `CLAUDE.md`, which can
never join `MANIFEST` -- see the tier's own comment block below for why the policy has
to be inline rather than behind a pointer. Every mode handles both tiers; a block that
cannot be located is reported, never skipped.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

# For `project_settings.py`, which the three functions below import **on use** rather
# than here. A project's very first pull runs the copy of this script the generator
# placed there, at a moment when nothing else in the MANIFEST exists yet -- so an import
# at module scope is a crash in exactly the bootstrap the pull exists for, and that is
# not a hypothesis: it is what `test_a_generated_project_satisfies_the_vendored_ci_contract`
# caught. The insert names *this* copy's directory, so a consumer's `workspace-status.py`
# loading this file by path cannot reach a sibling checkout's tier.
sys.path.insert(0, str(Path(__file__).resolve().parent))

REPO_ROOT = (Path(__file__).parent / "..").resolve()
SRC_ENV = "DEVKIT_DIR"
# Records which shared-repo commit this project's vendored copy corresponds to.
# Committed, per-project (like .devkit.toml), so NOT in MANIFEST.
VERSION_FILE = "DEVKIT_VERSION"
# Receipt of the exact files the last pull managed, with content hashes. Unlike the
# version stamp this is operational: it lets a later manifest retire paths safely
# without growing a permanent tombstone list. It is per-project and never vendored.
RECEIPT_FILE = "DEVKIT_FILES.json"
# The consumer-side pin `--pull` keeps in step with the files it copies. Both are
# per-project files, never vendored. The gate at commit time clones devkit at this
# `rev:` and byte-compares, so a pin that lags the stamp reports every file added
# upstream since as drift -- 38 of them, none of which is the actual problem.
PRECOMMIT_FILE = ".pre-commit-config.yaml"
DEVKIT_REPO = "https://github.com/alexandrec90/devkit"
# The second consumer-side pin, per RELEASING.md: the PR gate checks devkit out at
# this ref and runs `--check` against it. It must move with the pin above and the
# stamp -- bumping it alone turns a green gate red by design, and leaving it behind
# makes CI compare the vendored tree against a revision it was never pulled from.
PR_GATE_FILE = ".github/workflows/pr-gate.yml"
DEVKIT_SLUG = "alexandrec90/devkit"

# Windows only, and load-bearing whenever this script runs unattended.
#
# A process with no console of its own -- `pythonw.exe`, which is how every scheduled
# devkit job runs -- makes Windows allocate a **brand new console window** for each
# console child it spawns. The nightly upgrade pass spawns this script exactly that way,
# so an unflagged `git` here is a window flashing on the desktop for every command a pull
# makes, times every project in the pass. That is what it did: the flag was added to the
# jobs' own spawns and stopped at the process boundary, because a scheduled job's reach
# is longer than the script the scheduler names.
#
# A child that carries the flag gets a console with **no window**, and its own
# descendants inherit that console -- which is why flagging the outermost spawn in each
# script is enough, and why the flag has to be on every script the scheduler can reach.
# It is ignored for a GUI-subsystem child (`pythonw.exe` itself), so the flag on the
# spawn that started this script bought nothing here.
#
# `getattr` supplies zero off Windows, where the flag does not exist.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def console_python() -> str:
    """The console interpreter beside `sys.executable`, for spawning a Python child.

    `NO_WINDOW` is necessary and **not sufficient**. Windows ignores
    `CREATE_NO_WINDOW` for a GUI-subsystem child, so passing the flag alongside
    `pythonw.exe` -- which is what `sys.executable` is under a scheduled job -- leaves
    that child console-*less*, the exact condition that makes Windows open a fresh
    visible console for each of *its* children. Spawn a console interpreter with the
    flag instead and the child gets a hidden console that every descendant inherits.
    Pair the two; neither alone suppresses a window. Identity off Windows, and under
    any session that already has a console.
    """
    executable = Path(sys.executable)
    if executable.name.lower() != "pythonw.exe":
        return sys.executable
    console = executable.with_name("python.exe")
    # An embedded install could ship `pythonw.exe` with no console twin next to it.
    return str(console) if console.exists() else sys.executable


# Repo-relative paths of the shared harness files (source of truth = shared repo).
# Every entry ships with its test; keep both in the manifest so a vendored copy is
# verifiable in isolation. NB: `.devkit.toml` is intentionally absent -- it
# is per-project config, not shared code.
#
# The Codex compatibility scripts are near the bottom. `sync-codex-context.py` backs
# a shared workspace task (`devkit_project.ACTIONS["sync-codex"]`), so every consuming
# project is expected to have it at that exact path and the drift check keeps it there.
MANIFEST: tuple[str, ...] = (
    # Test plumbing: the load_module() loader every vendored test imports.
    "scripts/hooks/tests/conftest.py",
    # Repo-shape contract: the vendored scripts' unvendored dependencies exist, and
    # the manifest that selects them is spelled right. No script of its own -- it
    # asserts against whatever the consuming repo already has.
    "scripts/hooks/tests/test_repo_contract.py",
    # Execution contract: runs every hook the consuming repo actually wires, as a
    # subprocess over JSON stdin, and asserts the exit codes Claude Code acts on.
    # Also has no script of its own -- it reads that repo's `.claude/settings.json`.
    "scripts/hooks/tests/test_hook_execution_contract.py",
    # Config loader (the per-project seam) + the Stop dispatcher it drives.
    "scripts/hooks/harness_config.py",
    "scripts/hooks/tests/test_harness_config.py",
    # The untested-symbol ratchet: gates *this* project's public callables against
    # its own `.devkit-untested.txt`, which is deliberately not vendored -- the debt
    # list is a fact about one repo. Scope comes from `[test_contract]` in the
    # manifest, so the scanner never names a path. `--pull` seeds the baseline, which
    # is what keeps adoption from turning a consumer's PR gate red.
    "scripts/hooks/untested_symbols.py",
    "scripts/hooks/tests/test_untested_symbols.py",
    # The structural ratchet: size, complexity, fan-out, cycles, boundaries and
    # suppression counts, held to `.devkit-structure.txt` on the same terms as the
    # untested-symbol list above -- the file may only shrink, `--pull` seeds it, and
    # the vendored test is what runs it in a consumer's gate. Two modules because the
    # language scanners are testable against a snippet and the judging half is not.
    "scripts/hooks/structure_scan.py",
    "scripts/hooks/tests/test_structure_scan.py",
    "scripts/hooks/structure_check.py",
    "scripts/hooks/tests/test_structure_check.py",
    "scripts/hooks/stop.py",
    "scripts/hooks/stop_session.py",
    "scripts/hooks/tests/test_stop.py",
    "scripts/hooks/tests/test_stop_session.py",
    # Auto-fix-on-edit PostToolUse hook (repo-relative ruff path fix).
    "scripts/hooks/lint-fix.py",
    "scripts/hooks/tests/test_lint_fix.py",
    "scripts/hooks/worktree-guard-launch.py",
    "scripts/hooks/tests/test_worktree_guard_launch.py",
    # The settings tier `--pull` edits: the project's own `.claude/settings.json`, which
    # no MANIFEST entry covers. It ships beside the shim above because it is what wires
    # it -- the shim was vendored a release before anything ran it, and every consumer
    # spent that release holding an edit guard it never called.
    "scripts/project_settings.py",
    "scripts/hooks/tests/test_project_settings.py",
    # Bash output cap: the PreToolUse gate and the wrapper it demands. They ship
    # together because the gate's allow-list matches the wrapper's path -- vendoring
    # one without the other yields a hook that blocks every Bash call and names a
    # remedy the repo does not have. Cap size is `[bash]` in the manifest.
    "scripts/hooks/enforce-capped-bash.py",
    "scripts/hooks/tests/test_enforce_capped_bash.py",
    "scripts/hooks/invoke-capped.py",
    "scripts/hooks/tests/test_invoke_capped.py",
    # The harness-events ledger: the stdlib-only append helper every gate above uses
    # to leave a central record of a block on this machine's devkit checkout (via
    # $DEVKIT_DIR; silent no-op without one), and the CLI that gives an agent's own
    # defect report the same destination. Vendored together because a writer without
    # the helper is an ImportError inside a PreToolUse hook -- which exits non-2 and
    # silently disables the gate it lives in.
    "scripts/hooks/harness_events.py",
    "scripts/hooks/tests/test_harness_events.py",
    "scripts/hooks/report-harness-defect.py",
    "scripts/hooks/tests/test_report_harness_defect.py",
    # The task-layer failure artifact. Vendored rather than templated because nothing
    # in it varies: it resolves `logs/` from the cwd the task set, and the task label
    # it is given names the file. `notify-wrap.py` is its sibling and stays a template
    # only because it already was -- the two compose, one per concern.
    "scripts/log-wrap.py",
    "scripts/hooks/tests/test_log_wrap.py",
    # Branch lifecycle: default-branch auto-detected (detect_default_branch), so
    # these vendor unchanged. session-start.sh is the SessionStart entrypoint.
    "scripts/task_branch.py",
    "scripts/hooks/tests/test_task_branch.py",
    "scripts/ship.py",
    "scripts/hooks/tests/test_ship.py",
    "scripts/hooks/session-sync.py",
    "scripts/hooks/tests/test_session_sync.py",
    # `branch-per-task.py` and `branch-on-write.py` were here. They cut a task branch
    # *inside* the checkout the session was in, which is the one thing that made a
    # checkout outlive its task -- and every state `sweep.py` hunts for follows from
    # that. `worktree-guard.py` now routes the same edit into an ephemeral box instead,
    # so the branch is cut somewhere disposable. See `RETIRED_HOOKS` below: `--pull`
    # deletes these files, so it also has to drop the settings entries pointing at them.
    ".claude/hooks/session-start.sh",
    "scripts/hooks/tests/test_session_start.py",
    # The vendoring tool itself, so a project can drift-check / pull / push.
    "scripts/sync-devkit.py",
    "scripts/hooks/tests/test_sync_devkit.py",
    # --- The shared instruction tier -----------------------------------------
    # Same argument as the scripts, applied to the prose that steers the agent. These
    # paragraphs used to live inline in each repo's CLAUDE.md, get copied forward by
    # hand, and drift: devkit's own template had already lost a clause of the testing
    # mandate that carameli still had, and nothing could detect it. A project's
    # CLAUDE.md now points at these instead of restating them.
    #
    # Only genuinely portable files belong here. A rule or skill that names one
    # project's paths, services, or default branch is that project's own -- vendoring
    # it repeats the mistake that made every generated project fail 12 tests on its
    # first CI run.
    ".claude/rules/engineering.md",
    ".claude/rules/authoring.md",
    # The reference half of the policy above. Vendored because the pointers into it are:
    # an unvendored target would leave every consumer's engineering.md citing a path that
    # does not exist there. It sits *outside* `.claude/rules/` on purpose -- every `.md`
    # under that directory is loaded as a rule, and an unscoped one is re-sent on every
    # API call of every session, which is exactly what this file must not be.
    ".claude/engineering-evidence.md",
    # The one portable task workflow. Its script detects the remote default branch
    # and owns the mechanical checks; the skill supplies the semantic commit/PR text.
    ".claude/skills/ship/SKILL.md",
    # Codex reads CLAUDE.md through its project-document fallback. The remaining
    # compatibility layer mirrors repository skills and, when a project opts into
    # `.codex/`, translates Claude hook wiring.
    # NB: carameli's test_codex_hooks_contract.py is deliberately NOT vendored -- it
    # pins that repo's exact hook topology, which is the coupling this tier exists to
    # avoid. It belongs in a project's own non-vendored suite.
    "scripts/sync-codex-context.py",
    "scripts/hooks/tests/test_sync_codex_context.py",
    "scripts/sync-codex-hooks.py",
    "scripts/hooks/tests/test_sync_codex_hooks.py",
    # Generated commands route shared handlers through these Codex-native runtime
    # bridges. They must be vendored with their tests: otherwise conversion succeeds
    # while every emitted command points at a file the consumer never received.
    "scripts/hooks/codex-hook-adapter.py",
    "scripts/hooks/tests/test_codex_hook_adapter.py",
    # Codex's *own* published output contract, extracted from the binary by devkit's
    # `scripts/extract-codex-schema.py` (which is not vendored -- a consumer has no
    # reason to re-derive it, and most CI runners have no Codex to read). The adapter
    # loads this at runtime to decide which members of a hook's response Codex will
    # actually carry, so a consumer that received the adapter without it degrades every
    # translation check to "unknown-event" -- which passes, silently, forever.
    "scripts/hooks/codex-hook-schema.json",
    "scripts/hooks/codex-session-start.py",
    "scripts/hooks/tests/test_codex_session_start.py",
    # The gate on what those bridges produce, as opposed to on whether they were copied.
    # It has no script of its own -- it reads the *consumer's* `.claude/settings.json`
    # and the handlers it names -- which is exactly why it must be vendored: the wiring
    # it judges exists only in the consuming project.
    "scripts/hooks/tests/test_codex_translation.py",
    # --- The shared CI tier ---------------------------------------------------
    # Same argument again, applied to the two GitHub Actions files that carry no
    # project-specific content. Both used to ship only as `templates/` renders, which
    # is a one-shot copy: `--pull` never looked at them again, so every fix made here
    # after a project was generated stayed here. carameli's copy of the auto-merge
    # workflow is a case in point -- it is missing `issues: write`, the `--force` on
    # `gh label create`, and `GH_REPO`, three fixes written for failures it can still
    # hit, and nothing anywhere could report that.
    #
    # The gate itself (`.github/workflows/pr-gate.yml`) is deliberately NOT here and
    # stays a template: its jobs are the project's -- services, migrations, a frontend
    # tier -- and carameli's five-job gate is the proof that a shared one would either
    # delete real work or live permanently exempted.
    #
    # The auto-merge workflow qualifies because nothing in it varies: no `branches:`
    # filter, and the workflow it waits on is titled `PR Gate` in every project
    # including devkit.
    #
    # `.github/actions/setup-python-env/action.yml` was here in v0.7.0 and is NOT any
    # more. The argument for vendoring it was that its one variable -- the Python
    # version -- had moved to the caller, so nothing project-specific was left. Two
    # consumers disproved that on the first pull that reached them, and both failures
    # were silent-until-CI:
    #
    #   - apt-finder's copy opened with a step that clones its private sibling
    #     `data-lake` into `../data-lake`, because `[tool.uv.sources]` declares an
    #     editable path dependency there. The vendored copy deleted the step, and every
    #     job then died on `Distribution not found` before running a check.
    #   - carameli does not use `uv sync` at all. It installs pip-tools compiled locks
    #     with `uv pip install --system -r requirements.txt -r requirements-dev.txt`,
    #     pinning uv itself to the version in that lock, and takes an `extra-packages`
    #     input its weekly mutation job passes. `uv sync --all-extras --all-groups`
    #     cannot serve any of that -- there is no `uv.lock` to sync from.
    #
    # What varies is not the Python version. It is **how a project installs**, and that
    # is exactly the kind of thing `templates/` is for. It is rendered from
    # `templates/core/dot-github/actions/setup-python-env/action.yml`, which is a
    # byte-identical copy of devkit's own -- `test_setup_action_template_matches_devkits`
    # holds the two together the way `notify.py` is held to its template.
    ".github/workflows/dependabot-automerge.yml",
    # The scheduled-failure reporter qualifies on the same test: it watches a workflow
    # titled `Nightly` (required of every project by the contract test below, exactly as
    # `PR Gate` is) and reads its assignee from `github.repository_owner` at run time,
    # so there is no project value left in either the workflow or its script.
    #
    # It is vendored rather than added as a job to the nightly *because* the nightly is
    # a template. A one-shot copy would have delivered this to new projects only, and
    # the repos that most need it are the existing ones -- the whole failure mode it
    # addresses is a red scheduled run in a repo nobody has opened for a month.
    ".github/workflows/scheduled-failure-issue.yml",
    "scripts/report-workflow-failure.py",
    "scripts/hooks/tests/test_report_workflow_failure.py",
    # The auto-merge's retry half, and vendored for the same reason: an event handler that
    # fires once per gate completion strands its PR permanently on any transient failure,
    # and the repos where that goes unnoticed longest are the existing ones.
    "scripts/merge-dependabot-prs.py",
    "scripts/hooks/tests/test_merge_dependabot_prs.py",
    # The rest of the CI surface -- `dependabot.yml`, the gate, the nightly -- cannot
    # be vendored for the reason above, and `templates/` cannot keep them honest
    # either: a one-shot copy has no way to notice that a project never received a
    # file, or deleted one. This test is the half neither tier can do. It does not
    # supply a workflow; it refuses to let a project go without one, and it reads
    # nothing but that repo's own `.github/`, so it is portable by construction.
    "scripts/hooks/tests/test_ci_workflow_contract.py",
)

# Exact formerly-vendored files removed by `--pull`. Skill mirrors are included because
# leaving an old mirrored SKILL.md would keep
# the deleted command alive for Codex. Project-owned files such as state.json and
# known-fixes.md are deliberately absent: retiring shared prose must not erase local data.
_RETIRED_CLAUDE_PATHS: tuple[str, ...] = (
    ".claude/skills/audit-claude-md/SKILL.md",
    ".claude/skills/audit-dockerignore/SKILL.md",
    ".claude/skills/audit-gitignore/SKILL.md",
    ".claude/skills/fix-pre-commit/SKILL.md",
    ".claude/skills/plan-handoff/SKILL.md",
    ".claude/skills/refactor/SKILL.md",
    ".claude/skills/retro/SKILL.md",
    ".claude/skills/retro/extract.py",
    ".claude/skills/task/SKILL.md",
    ".claude/skills/test-skill/SKILL.md",
    ".claude/skills/test-skill/write-artifacts.py",
    ".claude/skills/state-tools/state-engine.py",
    ".claude/skills/state-tools/README.md",
)
_RETIRED_HARNESS_PATHS: tuple[str, ...] = (
    "scripts/hooks/branch-per-task.py",
    "scripts/hooks/branch-on-write.py",
    "scripts/hooks/tests/test_branch_on_write.py",
    "scripts/hooks/normalize-known-fixes.py",
    "scripts/hooks/tests/test_normalize_known_fixes.py",
    "scripts/hooks/finalize-state.py",
    "scripts/hooks/tests/test_finalize_state.py",
    "scripts/hooks/tests/test_state_engine.py",
    "scripts/hooks/archive-session.py",
    "scripts/hooks/tests/test_archive_session_outcomes.py",
    "scripts/hooks/tests/test_archive_session_stdin.py",
    "scripts/hooks/tests/test_archive_session_tokens.py",
)
RETIRED_PATHS: tuple[str, ...] = (
    _RETIRED_HARNESS_PATHS
    + _RETIRED_CLAUDE_PATHS
    + tuple(path.replace(".claude/", ".agents/", 1) for path in _RETIRED_CLAUDE_PATHS)
)


def resolve_src(arg: str | None, env: Mapping[str, str]) -> Path | None:
    """The shared-repo root from `--src` or `$DEVKIT_DIR`, or None when unset."""
    raw = arg or env.get(SRC_ENV)
    return Path(raw).expanduser().resolve() if raw else None


def read_version(root: Path) -> str | None:
    """The stamped harness commit this project vendored, or None if never pulled."""
    try:
        return (root / VERSION_FILE).read_text().strip() or None
    except OSError:
        return None


def git_head(path: Path) -> str | None:
    """Short HEAD SHA of the git repo at `path`, or None (not a repo / no git)."""
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


def _git_out(path: Path, *args: str) -> str | None:
    """stdout of `git -C path <args>`, or None when git fails or is unavailable."""
    try:
        result = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


def source_tag(path: Path) -> str | None:
    """The tag naming the source's HEAD exactly, or None when HEAD is untagged.

    The pin in a consumer's `.pre-commit-config.yaml` can only name a tag, so an
    untagged HEAD is a revision the consumer is structurally unable to pin. Pulling
    from one produces a project whose files come from a revision its own gate can
    never be pointed at -- which is not a warning, it is an unfixable state.
    """
    return _git_out(path, "describe", "--tags", "--exact-match", "HEAD")


def source_dirty(path: Path) -> bool:
    """True when the source has uncommitted changes.

    A dirty source is worse than an untagged one: the files copied out are not the
    files at HEAD, but the stamp records HEAD anyway. The result passes every
    consistency check that compares a project against *itself* and matches no
    upstream revision that exists. Refusing is the only honest option.
    """
    return bool(_git_out(path, "status", "--porcelain"))


def bump_pin(text: str, tag: str, repo: str = DEVKIT_REPO) -> tuple[str, str | None]:
    """Retarget the `rev:` under `repo:` in a `.pre-commit-config.yaml` to `tag`.

    Returns `(new_text, previous_rev)`; `previous_rev` is None when no pin for
    `repo` was found, which is a project that does not use the published hooks
    rather than an error.

    Deliberately a line-level rewrite rather than a YAML round-trip: the config is
    dense with comments explaining *why* each pin is what it is (carameli's runs to
    fifteen lines), and every YAML library in the stdlib-only budget available here
    would drop them.
    """
    lines = text.splitlines(keepends=True)
    in_repo = False
    previous: str | None = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("- repo:"):
            in_repo = stripped.split("- repo:", 1)[1].strip() == repo
            continue
        if not in_repo or not stripped.startswith("rev:"):
            continue
        # Keep the indentation and any trailing comment: the comment is usually the
        # rationale for pinning a tag at all.
        head, sep, tail = line.partition("rev:")
        value = tail.strip()
        previous = value.split("#", 1)[0].strip()
        comment = f" #{value.split('#', 1)[1]}" if "#" in value else ""
        newline = "\n" if line.endswith("\n") else ""
        lines[index] = f"{head}{sep} {tag}{comment.rstrip()}{newline}"
        break
    return "".join(lines), previous


def bump_gate_ref(text: str, tag: str, slug: str = DEVKIT_SLUG) -> tuple[str, str | None]:
    """Retarget the `ref:` of the workflow step that checks devkit out, to `tag`.

    Returns `(new_text, previous_ref)`. Matches on the `repository: <slug>` line and
    then the next `ref:` in that step, so a workflow that checks out several repos
    only has devkit's moved. Line-level for the same reason as `bump_pin`: the
    surrounding comments carry the ordering constraints the step depends on.
    """
    lines = text.splitlines(keepends=True)
    armed = False
    previous: str | None = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("repository:"):
            armed = stripped.split("repository:", 1)[1].strip() == slug
            continue
        if not armed or not stripped.startswith("ref:"):
            continue
        head, sep, tail = line.partition("ref:")
        value = tail.strip()
        previous = value.split("#", 1)[0].strip()
        comment = f" #{value.split('#', 1)[1]}" if "#" in value else ""
        newline = "\n" if line.endswith("\n") else ""
        lines[index] = f"{head}{sep} {tag}{comment.rstrip()}{newline}"
        break
    return "".join(lines), previous


def _retarget(root: Path, rel: str, tag: str, bump) -> None:
    """Apply a pin rewrite to `root/rel`, reporting what moved. Absent file is fine:
    not every project has a PR gate, and pre-adoption neither file exists."""
    path = root / rel
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    updated, previous = bump(text, tag)
    if previous is None:
        print(f"  (no devkit pin in {rel}; nothing to bump)")
    elif previous == tag:
        print(f"  {rel} already at {tag}")
    else:
        path.write_text(updated, encoding="utf-8", newline="\n")
        print(f"  bumped {rel}: {previous} -> {tag}")


def read_pin(text: str, repo: str = DEVKIT_REPO) -> str | None:
    """The `rev:` currently pinned for `repo`, or None when there is no such pin."""
    return bump_pin(text, "", repo)[1] or None


def stale_pin(root: Path) -> tuple[str, str] | None:
    """`(pinned, vendored_tag)` when the pin and the vendored release disagree.

    The single check that would have named carameli's failure immediately. Both
    inputs are per-project and committed, so this needs no network and no source
    checkout -- it is a comparison the project can make against itself.

    Compares the pin against the *receipt's* recorded tag, not against
    `DEVKIT_VERSION`: that file holds a SHA by contract, and a SHA never equals a
    tag, so comparing it would report every project as stale forever.

    Returns None when the tag was never recorded -- a pull from before this existed,
    or an `--allow-untagged` one. "Cannot tell" is not "stale", and a check that
    cried wolf on every un-upgraded project would be ignored within a week.
    """
    try:
        pinned = read_pin((root / PRECOMMIT_FILE).read_text(encoding="utf-8"))
    except OSError:
        return None
    vendored = read_receipt_tag(root)
    if not pinned or not vendored or pinned == vendored:
        return None
    return pinned, vendored


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _read_text(path: Path) -> str | None:
    """UTF-8 text of `path`, or None when absent/unreadable.

    `newline=""` disables universal-newline translation, so the host file's own line
    endings survive a round trip. Without it, splicing a block into a CRLF file would
    rewrite the other four hundred lines to LF and report the whole file as changed --
    on Windows, on every pull. Passed to `open` rather than `Path.read_text` because
    that keyword only reached `Path` in 3.13 and the floor here is 3.12.
    """
    with (
        contextlib.suppress(OSError, UnicodeDecodeError),
        path.open(encoding="utf-8", newline="") as handle,
    ):
        return handle.read()
    return None


def _write_text(path: Path, text: str) -> None:
    """Counterpart to `_read_text`; writes bytes through unchanged."""
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def _sha256(path: Path) -> str | None:
    data = _read(path)
    return hashlib.sha256(data).hexdigest() if data is not None else None


def read_receipt(root: Path) -> dict[str, str]:
    """Return {managed path: sha256} from the last pull; malformed receipts are empty."""
    try:
        raw = json.loads((root / RECEIPT_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    files = raw.get("files") if isinstance(raw, dict) else None
    if not isinstance(files, dict):
        return {}
    return {
        path: digest
        for path, digest in files.items()
        if isinstance(path, str) and isinstance(digest, str)
    }


def read_receipt_tag(root: Path) -> str:
    """The release tag the last pull vendored from; "" when unrecorded.

    Lives here rather than in `DEVKIT_VERSION` because that file has a contract:
    it records the upstream short **SHA**, and the vendored
    `test_harness_version_records_a_commit` asserts a 7-40 char hex value. Stamping
    a tag there broke that contract and had to be corrected by hand in a consumer.

    But the *pin* can only ever be a tag, so comparing the two needs the tag
    recorded somewhere. The receipt is already written by every pull, already
    per-project, and already never vendored -- so it is the honest home for it.
    """
    try:
        raw = json.loads((root / RECEIPT_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return ""
    tag = raw.get("devkit_tag") if isinstance(raw, dict) else None
    return tag if isinstance(tag, str) else ""


def write_receipt(root: Path, manifest: tuple[str, ...], tag: str = "") -> None:
    files = {rel: digest for rel in manifest if (digest := _sha256(root / rel)) is not None}
    payload: dict[str, object] = {"version": 1, "files": files}
    if tag:
        payload["devkit_tag"] = tag
    (root / RECEIPT_FILE).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def classify(
    src: Path, repo_root: Path, manifest: tuple[str, ...]
) -> tuple[list[str], list[str], list[str]]:
    """Partition the manifest into (drifted, missing_in_src, ok) by byte comparison.

    `missing_in_src` are files absent from the shared repo (it does not have them
    yet -- e.g. before a first `--push`); they are reported, never silently OK.
    """
    drifted: list[str] = []
    missing: list[str] = []
    ok: list[str] = []
    for rel in manifest:
        src_bytes = _read(src / rel)
        if src_bytes is None:
            missing.append(rel)
            continue
        if _read(repo_root / rel) == src_bytes:
            ok.append(rel)
        else:
            drifted.append(rel)
    return drifted, missing, ok


# --- the marker-block tier ----------------------------------------------------
# MANIFEST vendors whole files. That covers everything a project does not own, and
# cannot cover the one file the agents read first: `CLAUDE.md` is per-project by
# definition, so a whole-file compare would report each project's own prose as drift.
#
# Pointing at a vendored rule file instead -- what devkit did -- is a Claude-only
# convention. Codex has no `.claude/rules/`, reads straight past it, and so runs on a
# different text; an eval calibrated against one arrangement then says nothing about
# the other. Inlining the policy fixes that and brings back the original problem: an
# ungated copy inside a per-project file drifts freely while reading as authoritative,
# which is the exact failure the vendored instruction tier was built to end.
#
# So the vendored unit here is a *region*. Between the markers devkit owns the bytes
# and `--check` gates them exactly as it gates a MANIFEST file; outside them the
# project writes what it likes. Same guarantee, applied to a slice of a file.
#
# HTML comments, so the markers are invisible in rendered Markdown and inert as
# instructions -- an agent reading the file sees policy, not bookkeeping.
BLOCK_BEGIN = "<!-- devkit:begin {block_id} -->"
BLOCK_END = "<!-- devkit:end {block_id} -->"

# (host file, block id). Hosts are per-project and deliberately absent from MANIFEST.
# Empty until `.claude/rules/engineering.md` moves inline: shipping the mechanism
# first keeps that migration a single reviewable content change, and an empty tuple
# is inert in every mode. The helpers below are unit-tested regardless, so the tier
# is not first exercised in a consumer.
BLOCK_MANIFEST: tuple[tuple[str, str], ...] = ()


def block_markers(block_id: str) -> tuple[str, str]:
    """The literal begin/end markers delimiting `block_id`."""
    return BLOCK_BEGIN.format(block_id=block_id), BLOCK_END.format(block_id=block_id)


def find_block(text: str, block_id: str) -> tuple[tuple[int, int] | None, str]:
    """Offsets of the content between `block_id`'s markers: `(span, "")` on success.

    On failure returns `(None, reason)`. Every caller reports the reason rather than
    treating it as "nothing to do": a block that cannot be located is policy this file
    is not carrying, and the quietest way to lose a gate is to let that read as OK.
    """
    begin, end = block_markers(block_id)
    if text.count(begin) > 1 or text.count(end) > 1:
        return None, f"duplicate '{block_id}' markers"
    start, stop = text.find(begin), text.find(end)
    if start < 0 and stop < 0:
        return None, f"no '{block_id}' block"
    if start < 0:
        return None, f"'{block_id}' end marker with no begin"
    if stop < 0:
        return None, f"'{block_id}' begin marker with no end"
    if stop < start:
        return None, f"'{block_id}' markers are inverted"
    return (start + len(begin), stop), ""


def extract_block(text: str, block_id: str) -> str | None:
    """The region's exact characters, or None when the block is unusable."""
    span, _ = find_block(text, block_id)
    return None if span is None else text[span[0] : span[1]]


def replace_block(text: str, block_id: str, content: str) -> tuple[str, str]:
    """`text` with `block_id`'s region replaced by `content`; `("", reason)` on failure.

    Splices verbatim -- no reindenting, no newline normalisation -- so a pulled block
    is character-identical to its source and `--check` is stable immediately after a
    `--pull`. A round trip that "tidied" the content would report drift it created.
    """
    span, problem = find_block(text, block_id)
    if span is None:
        return "", problem
    return text[: span[0]] + content + text[span[1] :], ""


def classify_blocks(
    src: Path, repo_root: Path, blocks: tuple[tuple[str, str], ...]
) -> tuple[list[str], list[str], list[str]]:
    """Partition `blocks` into (drifted, unusable, ok), mirroring `classify`.

    `unusable` collects every way a block fails to be comparable -- host file absent
    on either side, markers missing or malformed on either side. Reported, never
    counted OK, for the reason in `find_block`.
    """
    drifted: list[str] = []
    unusable: list[str] = []
    ok: list[str] = []
    for host, block_id in blocks:
        label = f"{host}#{block_id}"
        src_text, dest_text = _read_text(src / host), _read_text(repo_root / host)
        if src_text is None or dest_text is None:
            side = "shared repo" if src_text is None else "this project"
            unusable.append(f"{label} ({host} absent in {side})")
            continue
        src_content = extract_block(src_text, block_id)
        dest_content = extract_block(dest_text, block_id)
        if src_content is None:
            unusable.append(f"{label} (shared repo: {find_block(src_text, block_id)[1]})")
        elif dest_content is None:
            unusable.append(f"{label} (this project: {find_block(dest_text, block_id)[1]})")
        elif src_content == dest_content:
            ok.append(label)
        else:
            drifted.append(label)
    return drifted, unusable, ok


def sync_blocks(
    from_root: Path, to_root: Path, blocks: tuple[tuple[str, str], ...]
) -> tuple[list[str], list[str]]:
    """Splice each block from_root -> to_root. Returns (written, failed-with-reason).

    A failure leaves the host file untouched and is returned for reporting, never
    swallowed: a block that did not land is policy the destination is not carrying,
    and `--pull` claiming success would hide it until the next `--check`.
    """
    written: list[str] = []
    failed: list[str] = []
    for host, block_id in blocks:
        label = f"{host}#{block_id}"
        source, target = _read_text(from_root / host), _read_text(to_root / host)
        if source is None or target is None:
            side = "source" if source is None else "destination"
            failed.append(f"{label} ({host} absent in {side})")
            continue
        content = extract_block(source, block_id)
        if content is None:
            failed.append(f"{label} (source: {find_block(source, block_id)[1]})")
            continue
        spliced, problem = replace_block(target, block_id, content)
        if problem:
            failed.append(f"{label} (destination: {problem})")
            continue
        if spliced != target:
            _write_text(to_root / host, spliced)
        written.append(label)
    return written, failed


def _copy(rel: str, from_root: Path, to_root: Path) -> bool:
    """Copy one manifest file from_root -> to_root. False when the source is absent."""
    source = from_root / rel
    if not source.exists():
        return False
    dest = to_root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    return True


_BYTECODE_CACHE = "__pycache__"
_BYTECODE_SUFFIXES = frozenset({".pyc", ".pyo"})


def _prune_dir(parent: Path) -> None:
    """Remove `parent` once the last file devkit managed there is gone.

    This replaces a bare `rmdir()`, which silently failed whenever a `__pycache__/`
    survived -- the usual case, since every retired skill that shipped a `.py` had run
    at least once. Nothing catches the leftover: the cache is gitignored, so an empty
    husk is invisible to `git status`, to the drift gate, and to a reviewer. Carameli
    carried nine of them for months after the skills themselves were retired.

    The cache is deleted only when it is the *sole* remaining entry and holds nothing
    but bytecode. Beside a surviving source file it is live, and any other sibling --
    a `state.json`, a project-owned `check-specs.json` -- means the directory is still
    someone's, which retirement does not get to overrule.
    """
    with contextlib.suppress(OSError):
        if [item.name for item in parent.iterdir()] == [_BYTECODE_CACHE]:
            cache = parent / _BYTECODE_CACHE
            if all(item.suffix in _BYTECODE_SUFFIXES for item in cache.iterdir()):
                shutil.rmtree(cache)
        parent.rmdir()


def retired_present(root: Path) -> list[str]:
    return [rel for rel in RETIRED_PATHS if (root / rel).is_file()]


# The settings file is the project's own (never vendored — see CLAUDE.md), which is
# exactly why `--pull` has to touch it: the pull deletes a retired hook script and wires
# a newly delivered one, and both of those are edits to a file no MANIFEST covers. That
# tier lives in `project_settings.py` — a different contract from copying, and this
# module was past every structural limit it holds other files to. Spelled out here
# rather than read from there because this module must load before that file exists
# (see the sys.path comment at the top); `test_the_two_modules_name_the_same_settings
# _file` is what stops the two copies drifting.
SETTINGS_FILE = ".claude/settings.json"


def retired_hook_paths(retired: tuple[str, ...] | None = None) -> tuple[str, ...]:
    """`project_settings.retired_hook_paths`, over this module's retired list.

    Kept here, thin, because `retired=None` must read `RETIRED_PATHS` **at call time**:
    `workspace-status.py` loads this module by path and its test replaces that list on
    the loaded module to prove the names are read from here rather than copied. A
    default captured at def time silently ignores that, and the test would have been
    asserting nothing.

    Empty when the tier is not on disk yet, which is the bootstrap pull and nothing
    else: a copy that has vendored the MANIFEST and lost this file is drift, and
    `--check` reports it as drift rather than through this.
    """
    try:
        import project_settings
    except ModuleNotFoundError:
        return ()
    return project_settings.retired_hook_paths(RETIRED_PATHS if retired is None else retired)


def settings_pass(root: Path, retired: tuple[str, ...] | None = None) -> list[str]:
    """The pull's pass over this project's settings file; one note per change made.

    Unwires the hooks this pull's deletions left dangling, and wires the cross-checkout
    edit guard when nothing runs it. `project_settings.settings_pass` owns both, and
    the notes it returns are already worded for the report below.
    """
    try:
        import project_settings
    except ModuleNotFoundError:
        return []
    return project_settings.settings_pass(root, retired_hook_paths(retired))


def local_faults(root: Path) -> tuple[list[tuple[str, str]], str]:
    """`(faults, summary)` for what `--check` finds outside the MANIFEST.

    Neither the settings file nor the generated Codex mirror is ever compared against
    upstream, so neither is drift -- but an unwired edit guard is the one harness fault
    with no symptom at all, and a check that stayed quiet about it is how five
    checkouts spent a release routing agent edits onto their home branches.

    Returns the summary already worded, so `main` neither grows a branch per fault nor
    words it there; both are what pushed that function past its limits.
    """
    try:
        import project_settings
    except ModuleNotFoundError:
        return [], ""
    faults = project_settings.check_notes(root, CODEX_HOOKS_FILE, codex_hooks_stale(root))
    return faults, project_settings.check_summary(faults)


CODEX_HOOKS_FILE = ".codex/hooks.json"
CODEX_GENERATOR = "scripts/sync-codex-hooks.py"


def _codex_generator(root: Path, *flags: str) -> list[str]:
    return [
        console_python(),
        str(root / CODEX_GENERATOR),
        *flags,
        str(root / SETTINGS_FILE),
        str(root / CODEX_HOOKS_FILE),
    ]


def codex_hooks_stale(root: Path) -> bool:
    """Whether this project's committed `.codex/hooks.json` is what it would generate.

    The generator is vendored; **its output is not**, and that asymmetry is the whole
    reason this exists. `--pull` copies `sync-codex-hooks.py`, so a fix to what Codex
    should be running lands in every consumer — and changes nothing, because the file
    Codex actually reads was written by the *previous* generator and no gate on either
    side ever looked at it. `regenerate_codex_hooks` closes it for a project that pulls;
    this closes it for one whose file went stale some other way, so the PR gate says so
    instead of a Codex session discovering it a blocked command at a time.

    Runs the generator out-of-process rather than importing it: these are sibling
    scripts at fixed vendored paths, and `sync-codex-context.py` already invokes it the
    same way. Best-effort — a generator that is absent or that raises leaves this
    reporting nothing, because a project with no `.codex/` has nothing to be stale.
    """
    if not (root / CODEX_HOOKS_FILE).is_file() or not (root / CODEX_GENERATOR).is_file():
        return False
    try:
        result = subprocess.run(
            _codex_generator(root, "--check"),
            capture_output=True,
            text=True,
            creationflags=NO_WINDOW,
        )
    except OSError:
        return False
    return result.returncode == 1


def regenerate_codex_hooks(root: Path) -> bool:
    """Rewrite `.codex/hooks.json` from the just-pulled generator. True if it changed.

    Only for a project that already has the file: generating one for a project that
    never opted into `.codex/` would wire Codex hooks nobody asked for.
    """
    if not codex_hooks_stale(root):
        return False
    try:
        # Captured and re-emitted rather than left to inherit. `NO_WINDOW` binds a child
        # that captures nothing to the console it was just given, so the flag alone
        # silently takes whatever the generator says out of a `--pull` a human is
        # reading. Both streams, and without asking what today's generator prints: that
        # is the generator's business to change, and this is the only place it could be
        # lost.
        result = subprocess.run(
            _codex_generator(root), capture_output=True, text=True, creationflags=NO_WINDOW
        )
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        return result.returncode == 0
    except OSError:
        return False


# Not in MANIFEST, and it never will be: the file's content is a fact about one repo.
# The name is shared so a pull can tell "already adopted" from "adopting now".
UNTESTED_BASELINE_FILE = ".devkit-untested.txt"


def seed_untested_baseline(root: Path) -> int | None:
    """Record this project's existing untested symbols on adoption. `None` if not.

    The ratchet's baseline is the debt a repo already has, and until it exists every
    public callable in the project reads as new debt. Seeding it here is what makes
    adopting the gate a no-op instead of a red PR gate on the pull that delivers it —
    and it has to be *here* rather than a step someone runs afterwards, because the
    someone is the thing that does not happen.

    The scanner refuses to overwrite, so this is safe to call on every pull: a project
    that has already adopted keeps its list, and new untested code cannot be laundered
    into it. Out-of-process for the same reason `regenerate_codex_hooks` is: the file
    was copied in a moment ago, and running it as a subprocess means a broken one fails
    visibly here rather than at this script's import.
    """
    script = root / "scripts/hooks/untested_symbols.py"
    if not script.is_file() or (root / UNTESTED_BASELINE_FILE).exists():
        return None
    try:
        result = subprocess.run(
            # `console_python()`, not `sys.executable`: an upgrade sweep runs this from
            # a scheduled job under `pythonw.exe`, where a GUI-subsystem child ignores
            # CREATE_NO_WINDOW and Windows gives its children visible consoles instead.
            [console_python(), str(script), "--seed"],
            cwd=str(root),
            capture_output=True,
            text=True,
            creationflags=NO_WINDOW,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return len(read_untested_baseline(root))


# The structural ratchet's twin of the file above, on the same terms: never vendored,
# shared name so a pull can tell adoption from a repeat.
STRUCTURE_BASELINE_FILE = ".devkit-structure.txt"


def seed_structure_baseline(root: Path) -> int | None:
    """Record this project's existing structural debt on adoption. `None` if not.

    Same argument as `seed_untested_baseline`: until the baseline exists every
    oversized function and every `# noqa` reads as new debt, so the pull that delivers
    the gate would redden the gate. The scanner refuses to overwrite, so calling this
    on every pull is safe, and it runs out of process for the same reason.
    """
    script = root / "scripts/hooks/structure_check.py"
    if not script.is_file() or (root / STRUCTURE_BASELINE_FILE).exists():
        return None
    try:
        result = subprocess.run(
            [console_python(), str(script), "--seed"],
            cwd=str(root),
            capture_output=True,
            text=True,
            creationflags=NO_WINDOW,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return len(read_structure_baseline(root))


def read_structure_baseline(root: Path) -> list[str]:
    """The recorded findings, so a pull can report how much debt it just wrote down."""
    path = root / STRUCTURE_BASELINE_FILE
    if not path.is_file():
        return []
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def read_untested_baseline(root: Path) -> list[str]:
    """The recorded gaps, so a pull can report how much debt it just wrote down."""
    path = root / UNTESTED_BASELINE_FILE
    if not path.is_file():
        return []
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def remove_retired(root: Path) -> list[str]:
    """Delete only reviewed retired files, never project-owned sibling state."""
    removed = retired_present(root)
    for rel in removed:
        (root / rel).unlink()
    # Sweep every retired path's directory, not only those that still held a file.
    # A path retired several releases ago -- or whose file went by hand, as carameli's
    # did in a sweep commit -- leaves the same husk, and `retired_present` cannot see
    # it because there is no longer a file to match. Pruning the whole list makes the
    # cleanup idempotent rather than one-shot, so a project that missed the release
    # that retired something still gets it tidied on its next pull.
    for rel in RETIRED_PATHS:
        _prune_dir((root / rel).parent)
    return removed


def receipt_retired_present(root: Path, manifest: tuple[str, ...]) -> list[str]:
    current = set(manifest)
    return sorted(
        rel for rel in read_receipt(root) if rel not in current and (root / rel).is_file()
    )


def template_outputs(src: Path | None) -> set[str]:
    """Every project-relative path the source's `templates/` tier can produce.

    The `dot-` prefix on a path component is the generator's spelling of a leading dot
    (`dot-github/workflows/pr-gate.yml.tmpl` -> `.github/workflows/pr-gate.yml`), and a
    trailing `.tmpl` marks a file that is rendered rather than copied. Both are stripped
    here so the result is directly comparable with a MANIFEST path.

    Best effort by design: a source with no `templates/` (an older devkit, or a consumer
    pushing back) yields the empty set, and every caller treats that as "cannot tell".
    """
    if src is None:
        return set()
    root = src / "templates"
    if not root.is_dir():
        return set()
    outputs: set[str] = set()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        # `templates/<preset>/...` -- the preset directory is not part of the output.
        parts = path.relative_to(root).parts[1:]
        if not parts:
            continue
        renamed = [
            part[4:] and f".{part[4:]}" if part.startswith("dot-") else part for part in parts
        ]
        renamed[-1] = renamed[-1].removesuffix(".tmpl")
        outputs.add("/".join(renamed))
    return outputs


def remove_receipt_retired(
    root: Path, manifest: tuple[str, ...], src: Path | None = None
) -> tuple[list[str], list[str], list[str]]:
    """Remove no-longer-managed files. `(removed, preserved, unvendored)`.

    Three ways a path in the receipt can be absent from the MANIFEST, and only one of
    them is a deletion:

    - **Locally edited.** Preserved, as before: the sha no longer matches what the last
      pull wrote, so someone owns it now.
    - **Un-vendored** -- it left the MANIFEST because it became a `templates/` file.
      **Preserved**, and this is the fix. Deleting it was silent data loss: `templates/`
      is a one-shot copy consulted only by `new-project.py`, so nothing put the file
      back and nothing on either side reported the gap. When
      `.github/actions/setup-python-env/action.yml` was un-vendored, the pull deleted it
      from every consumer that had *not* customised it -- and customising it was the
      only thing that saved the two that survived, by accident of the sha check above.
      Two projects' PR gates then died on an unresolvable local action, and a third's
      nightly failed silently for a week because only its nightly used the action.
    - **Retired.** Genuinely obsolete, nothing should have it. Deleted, as before.

    The distinction needs the source, so `src=None` (a caller that cannot supply one)
    falls back to the old behaviour rather than preserving everything -- a receipt entry
    that is neither in the MANIFEST nor in `templates/` really is retired, and never
    tidying those would leave a consumer accumulating dead files forever.
    """
    receipt = read_receipt(root)
    current = set(manifest)
    templates = template_outputs(src)
    removed: list[str] = []
    preserved: list[str] = []
    unvendored: list[str] = []
    for rel, expected in sorted(receipt.items()):
        path = root / rel
        if rel in current or not path.is_file():
            continue
        if rel in templates:
            unvendored.append(rel)
            continue
        if _sha256(path) != expected:
            preserved.append(rel)
            continue
        path.unlink()
        removed.append(rel)
        _prune_dir(path.parent)
    return removed, preserved, unvendored


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="fail on drift (default)")
    mode.add_argument("--pull", action="store_true", help="copy shared repo -> this project")
    mode.add_argument("--push", action="store_true", help="copy this project -> shared repo")
    mode.add_argument("--list", action="store_true", help="print manifest + source, then exit")
    parser.add_argument("--src", help=f"shared harness repo root (else ${SRC_ENV})")
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help=(
            "--pull from a source with uncommitted changes, stamping the version "
            "'<rev>-dirty'. For iterating on devkit locally; never for an upgrade"
        ),
    )
    parser.add_argument(
        "--allow-untagged",
        action="store_true",
        help=(
            "--pull from an untagged source. Leaves the .pre-commit-config.yaml pin "
            "stale, because there is no tag to move it to"
        ),
    )
    args = parser.parse_args(argv)

    src = resolve_src(args.src, os.environ)

    if args.list:
        print(f"source: {src or '(unset)'}")
        print(f"vendored version: {read_version(REPO_ROOT) or '(never pulled)'}")
        for rel in MANIFEST:
            print(f"  {rel}")
        if BLOCK_MANIFEST:
            print("vendored blocks (regions of per-project files):")
            for host, block_id in BLOCK_MANIFEST:
                print(f"  {host}#{block_id}")
        if RETIRED_PATHS:
            print("retired on pull:")
            for rel in RETIRED_PATHS:
                print(f"  {rel}")
        return 0

    if src is None:
        # Unconfigured. Before adoption that is correct and every mode no-ops clean, so a
        # project generated an hour ago -- no vendored files, and nothing to compare them
        # against -- passes its own PR gate.
        #
        # After adoption the same silence is a lie. A stamped project HAS vendored files,
        # `--check` is the gate over them, and exit 0 reports a comparison that never ran.
        #
        # The stamp is the one signal that separates the two, and the reason it works is
        # that it is committed. `$DEVKIT_DIR` is a property of the *machine* -- a second
        # workstation, a fresh clone, a CI job whose `env:` block was dropped -- and every
        # one of those is a place where the gate goes quiet exactly when the project has
        # the most vendored code to compare. `DEVKIT_VERSION` travels with the repo, so it
        # can tell "not adopted yet" from "adopted, and this machine cannot check it".
        stamped = read_version(REPO_ROOT)
        if stamped is None:
            print(f"sync-harness: ${SRC_ENV} unset and no --src; nothing to do (skipping).")
            return 0
        print(
            f"sync-harness: this project vendors devkit ({VERSION_FILE} = {stamped}), but "
            f"${SRC_ENV} is unset and no --src was given. There is nothing to compare "
            f"against, so NOTHING WAS CHECKED.\n"
            f"  point it at a devkit checkout:  {SRC_ENV}=<path> python scripts/sync-devkit.py\n"
            f"  or drift-check without a clone: pre-commit run devkit-drift --all-files\n"
            f"(that hook compares against the devkit rev pinned in {PRECOMMIT_FILE}, so it "
            f"needs no ${SRC_ENV} and no local devkit at all)"
        )
        return 1

    if args.pull:
        # Both guards run *before* anything is copied: a refusal must leave the
        # project exactly as it was, not half-upgraded with a stamp to match.
        if source_dirty(src) and not args.allow_dirty:
            print(
                f"sync-harness: {src} has uncommitted changes. Pulling from it copies "
                f"files that are at no upstream revision, while the stamp claims HEAD "
                f"-- the state that made a consumer's drift gate unfixable. Commit "
                f"there first, or pass --allow-dirty to stamp it as provisional.",
                file=sys.stderr,
            )
            return 2
        tag = source_tag(src)
        if tag is None and not args.allow_untagged:
            print(
                f"sync-harness: {src} HEAD is not tagged. A consumer pins devkit by "
                f"tag, so an untagged pull can never be pinned to what it vendored. "
                f"Tag the release first, or pass --allow-untagged to pull anyway "
                f"(leaving {PRECOMMIT_FILE} stale -- and the drift gate red).",
                file=sys.stderr,
            )
            return 2

    if args.pull or args.push:
        from_root, to_root = (src, REPO_ROOT) if args.pull else (REPO_ROOT, src)
        managed_removed, preserved, unvendored = (
            remove_receipt_retired(REPO_ROOT, MANIFEST, src) if args.pull else ([], [], [])
        )
        copied = [rel for rel in MANIFEST if _copy(rel, from_root, to_root)]
        skipped = [rel for rel in MANIFEST if rel not in copied]
        removed = (remove_retired(REPO_ROOT) + managed_removed) if args.pull else []
        # After the deletions, never before: pruning a hook whose script survived the
        # pull would disable a live hook.
        unwired = settings_pass(REPO_ROOT) if args.pull else []
        # After the settings pass, never before: the Codex file is generated *from*
        # those settings, so regenerating first would bake back in whatever the prune
        # is about to remove.
        codex_regenerated = regenerate_codex_hooks(REPO_ROOT) if args.pull else False
        # After the copy, because it runs the scanner this pull just delivered.
        seeded = seed_untested_baseline(REPO_ROOT) if args.pull else None
        seeded_structure = seed_structure_baseline(REPO_ROOT) if args.pull else None
        blocks_written, blocks_failed = sync_blocks(from_root, to_root, BLOCK_MANIFEST)
        verb = "pulled" if args.pull else "pushed"
        print(
            f"sync-harness: {verb} {len(copied)} file(s) and {len(blocks_written)} "
            f"block(s); removed {len(removed)} retired; skipped {len(skipped)} absent."
        )
        for rel in skipped:
            print(f"  (absent) {rel}")
        for rel in preserved:
            print(f"  (preserved local edit) {rel}")
        for rel in unvendored:
            # Named on every pull, not just the one that moved it. The file is this
            # project's own from here on -- devkit will not update it again, and the
            # only thing that could tell you so is this line.
            print(f"  (now yours -- un-vendored, devkit no longer updates it) {rel}")
        for note in unwired:
            print(f"  {note}")
        if codex_regenerated:
            # Named, because it is the one file the pull rewrote that was never copied
            # from the source: it is generated here, from this project's own settings.
            print(f"  (regenerated from {SETTINGS_FILE}) {CODEX_HOOKS_FILE}")
        if seeded is not None:
            # Only on the pull that adopts the gate. Named because it is a claim about
            # this repo that nobody wrote by hand, and because the number is the debt
            # the project is now expected to burn down rather than add to.
            print(
                f"  (adopted the untested-symbol ratchet) {UNTESTED_BASELINE_FILE}: "
                f"{seeded} symbol(s) with no test naming them"
            )
        if seeded_structure is not None:
            print(
                f"  (adopted the structure ratchet) {STRUCTURE_BASELINE_FILE}: "
                f"{seeded_structure} finding(s) grandfathered"
            )
        if args.pull:
            # The SHA, always: DEVKIT_VERSION records the upstream *commit*, and
            # the vendored `test_harness_version_records_a_commit` asserts exactly
            # that. The tag goes in the receipt instead, where `stale_pin` reads it.
            (REPO_ROOT / VERSION_FILE).write_text(
                f"{git_head(src) or 'unknown'}\n", encoding="utf-8", newline="\n"
            )
            # No tag recorded for an untagged or dirty pull: there is no release
            # those files correspond to, and `stale_pin` reporting "cannot tell"
            # beats it asserting something untrue.
            provisional = source_dirty(src)
            write_receipt(REPO_ROOT, MANIFEST, tag="" if provisional else (tag or ""))
            # The third moving part. Files, stamp and pin land together or the pull
            # is a half-upgrade whose gate fails later pointing at the wrong cause.
            if tag:
                _retarget(REPO_ROOT, PRECOMMIT_FILE, tag, bump_pin)
                _retarget(REPO_ROOT, PR_GATE_FILE, tag, bump_gate_ref)
        if blocks_failed:
            # Reported after the stamp is written, not instead of it: the files that
            # did land are on disk either way, and a receipt that omits them would
            # misdescribe the tree. The exit code is what must not claim success --
            # a configured block that could not be spliced leaves the destination
            # carrying no policy at all, which `--check` alone would find far later.
            for entry in blocks_failed:
                print(f"BLOCK   {entry}", file=sys.stderr)
            print(
                f"sync-harness: {len(blocks_failed)} block(s) did not land. Add the "
                f"missing `{BLOCK_BEGIN.format(block_id='<id>')}` / "
                f"`{BLOCK_END.format(block_id='<id>')}` markers to the host file.",
                file=sys.stderr,
            )
            return 1
        return 0

    # Default: --check
    vendored, available = read_version(REPO_ROOT), git_head(src)
    if available and vendored and vendored != available:
        # Informational only -- drift is decided by content below, not version.
        print(f"sync-harness: vendored {vendored}, shared repo at {available} (newer available).")
    drifted, missing, _ = classify(src, REPO_ROOT, MANIFEST)
    block_drifted, block_unusable, block_ok = classify_blocks(src, REPO_ROOT, BLOCK_MANIFEST)
    retired = retired_present(REPO_ROOT)
    receipt_retired = receipt_retired_present(REPO_ROOT, MANIFEST)
    # Faults in this project's own files: not drift, since `--check` never compares
    # them, but red for the same reason -- an unwired edit guard has no other symptom.
    local, local_summary = local_faults(REPO_ROOT)
    if not (
        drifted or missing or retired or receipt_retired or block_drifted or block_unusable or local
    ):
        blocks = f" and {len(block_ok)} block(s)" if BLOCK_MANIFEST else ""
        print(f"sync-harness: all {len(MANIFEST)} vendored files{blocks} in sync with {src}.")
        return 0
    # Named before the file list, because it changes what the file list *means*: a
    # stale pin makes every file added upstream since the pin look like drift, and
    # re-pulling -- the fix the listing implies -- cannot resolve any of it.
    stale = stale_pin(REPO_ROOT)
    if stale:
        pinned, vendored = stale
        print(
            f"sync-harness: {PRECOMMIT_FILE} pins {pinned} but these files were "
            f"vendored from {vendored}. The pin is stale, so the differences below "
            f"are measured against the wrong revision -- bump the pin to {vendored}; "
            f"re-pulling will not fix them.",
            file=sys.stderr,
        )
    for rel in drifted:
        print(f"DRIFT   {rel}", file=sys.stderr)
    for rel in missing:
        print(f"MISSING {rel} (not in shared repo)", file=sys.stderr)
    for rel in retired:
        print(f"RETIRED {rel} (run --pull to remove)", file=sys.stderr)
    for rel in receipt_retired:
        print(f"RETIRED {rel} (recorded by {RECEIPT_FILE})", file=sys.stderr)
    for label in block_drifted:
        print(f"DRIFT   {label} (vendored block)", file=sys.stderr)
    for label in block_unusable:
        # Distinct from DRIFT on purpose: `--pull` fixes drift, and cannot fix a
        # missing marker pair. Saying so here saves the pull that would not help.
        print(f"BLOCK   {label} -- not comparable; --pull cannot fix this", file=sys.stderr)
    for _, message in local:
        # Never DRIFT: `--pull` fixes these, but nothing upstream differs, so the file
        # list and the advice that goes with it would both point at the wrong thing.
        print(message, file=sys.stderr)
    if drifted or missing or retired or receipt_retired or block_drifted or block_unusable:
        print(
            "sync-harness: vendored harness drifted from the shared repo. "
            "Run `python scripts/sync-devkit.py --pull` to adopt upstream, "
            "or `--push` if this project authored the change.",
            file=sys.stderr,
        )
    else:
        # A `--pull` would resolve these too, but only incidentally, and telling
        # someone to adopt upstream when nothing upstream differs is the kind of advice
        # that gets a red gate reclassified as noise.
        print(
            f"sync-harness: every vendored file is in sync; {local_summary}.",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
