"""The path lists `sync-devkit.py` vendors, retires and gates -- data, and nothing else.

Cut out of `sync-devkit.py`, whose `file_lines` baseline had been raised nine times for
the same reason: a vendored file is a MANIFEST entry, a MANIFEST entry could live nowhere
but the MANIFEST, and the MANIFEST lived in the tool. Here the list grows without growing
the tool, and the tool stays the size of its logic.

Two readers, and both constrain the spelling:

- `sync-devkit.py` imports it by name, and re-exports every name below, so
  `sync_devkit.MANIFEST` and friends are what they always were to every caller and test.
- `structure_check.vendored_paths` reads `MANIFEST` and `GATED_MANIFEST` off this file's
  *source* with `ast.literal_eval`, in every consumer. Keep both pure literals.

Stdlib only by construction -- it imports nothing. A consumer's first pull after this
file existed runs its *old* `sync-devkit.py`, whose inline list does not name it, so the
new tool can arrive alone; `sync-devkit.py` then loads this file from the source it was
given (`--src` / `$DEVKIT_DIR`) and refuses to run without it rather than on a partial
list, which would read every file in the receipt as retired and delete it.

Tested through `scripts/hooks/tests/test_sync_devkit.py`, where the MANIFEST always was.
"""

from __future__ import annotations


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
    # The untested-symbol ratchet, against the project's own (unvendored) debt list;
    # `--pull` seeds it so adoption cannot redden a gate. It imports `code_text.py`, so
    # the two arrive in one `--pull` or the gate dies of an `ImportError`.
    "scripts/hooks/code_text.py",
    "scripts/hooks/tests/test_code_text.py",
    "scripts/hooks/untested_symbols.py",
    "scripts/hooks/tests/test_untested_symbols.py",
    # The structural ratchet: size, complexity, fan-out, cycles, boundaries and
    # suppression counts, held to `.devkit-structure.txt` on the same terms as the
    # untested-symbol list above -- the file may only shrink, `--pull` seeds it once and
    # tightens it on every pull after, and the vendored test is what runs it in a
    # consumer's gate. Three modules because the
    # language scanners are testable against a snippet and the judging half is not.
    "scripts/hooks/structure_scan.py",
    "scripts/hooks/tests/test_structure_scan.py",
    # The baseline FILE is the third, cut out of the judge, which had recorded a
    # `file_lines` raise on three branches. All three arrive in one `--pull`: a consumer
    # holding the judge without this gets an `ImportError` inside a hook, which exits
    # non-2 and silently disables the gate it lives in.
    "scripts/hooks/structure_baseline.py",
    "scripts/hooks/tests/test_structure_baseline.py",
    "scripts/hooks/structure_check.py",
    "scripts/hooks/tests/test_structure_check.py",
    # Where each agent CLI cuts a `--worktree` checkout, and which repo one belongs to.
    # Ships ahead of its two importers, which import it plainly and fail closed without it.
    "scripts/hooks/worktree_tiers.py",
    "scripts/hooks/tests/test_worktree_tiers.py",
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
    # `branch-per-task.py` and `branch-on-write.py` were here: a branch cut *inside* the
    # session's checkout is what made a checkout outlive its task. `worktree-guard.py` cuts
    # it in a disposable box now; `RETIRED_HOOKS` below deletes both and their settings.
    ".claude/hooks/session-start.sh",
    "scripts/hooks/tests/test_session_start.py",
    # What a fresh worktree lacks and the command that installs it: the one ladder
    # `session-start.sh` reports from and `ship.py --preflight` prints, so a session in a
    # worktree with no `.venv` is told before its commit-time gate refuses the commit.
    "scripts/hooks/toolchain.py",
    "scripts/hooks/tests/test_toolchain.py",
    # The vendoring tool itself, so a project can drift-check / pull / push, and the
    # path lists it reads -- this file. Cut out of the tool because every entry here
    # was a raise of its `file_lines` baseline; the tool cannot run without it, so a
    # consumer that holds one and not the other is refused by the tool, not guessed at.
    "scripts/sync-devkit.py",
    "scripts/devkit_manifest.py",
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
    # What a coding session leaves to the fix pass (commit, push, PR, the full suite),
    # and the fixer sessions exempt from that.
    ".claude/rules/session-scope.md",
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


# --- the gated tier -----------------------------------------------------------
# MANIFEST is unconditional: every consumer holds every path, and `--check` reports a
# missing one. Right for the harness, wrong for a file that only means anything in a
# project with a particular tier -- the dev server's port derivation is nonsense in
# devkit itself and in every stackless repo. It used to live in `templates/features/`,
# the one-shot tier, where every later fix stayed behind and nothing reported the gap.
#
# An entry here is keyed on a `.devkit.toml` section and named RELATIVE to that
# section's source prefix. `manifest_for` resolves it per consumer: present where
# `[frontend]` is enabled, at that project's `[frontend] src`; absent where it is not,
# which is the difference between a gate and a MISSING line. devkit's own copy lives at
# the default prefix, and `source_map` is the translation between the two layouts --
# every path outside this dict is spelled identically on both sides, so the map is empty
# for most consumers and absent from every MANIFEST entry.
FRONTEND_GATE = "frontend"
DEFAULT_FRONTEND_SRC = "frontend/src/"
# A LITERAL dict, keys spelled as strings: `structure_check.vendored_paths` reads this
# off the source with `ast.literal_eval` in every consumer, and a `Name` key would make
# the whole literal unreadable there -- silently, as an empty exemption.
GATED_MANIFEST: dict[str, tuple[str, ...]] = {
    # The host-Vite dev server's port derivation and its vitest file. Dependency-free by
    # contract (`tests/test_worktree_port.py`), so it drops into any Vite project, and
    # held equal to `scripts/hooks/worktree_tiers.py` by the same test.
    "frontend": ("worktreePort.ts", "worktreePort.test.ts"),
}
