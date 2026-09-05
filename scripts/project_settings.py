#!/usr/bin/env python3
"""The project's own harness config: `.claude/settings.json`, which devkit never vendors.

Split out of `sync-devkit.py` along the seam its own comments already described. That
module copies files listed in a MANIFEST; this one *edits* a file the project owns, and
the difference is a contract, not a detail -- which is why the settings code there kept
needing a paragraph to explain why it was allowed to write at all. `sync-devkit.py` was
also past every structural limit it holds other files to, and this is the half that had
somewhere else to be.

The pull makes one pass over that file, in two directions:

- **Unwire what the pull deleted.** A retired hook script is *removed* by `--pull`, and
  a `hooks` entry still naming it is not inert: the harness spawns a missing file on
  every prompt and the interpreter fails. The pull created that state, so the pull
  cleans it up.
- **Wire what the pull delivered.** `scripts/hooks/worktree-guard-launch.py` is
  vendored, but a hook file is inert until the settings name it. For a release every
  consumer therefore held the shim and ran no guard at all, and an agent edit in those
  checkouts landed on the home branch with no task branch under it -- the exact backlog
  the guard exists to prevent. `templates/` wires it for projects generated after the
  shim existed; this wires the ones generated before.

Stdlib only, like everything else here that runs before a virtualenv exists.

Tested in `scripts/hooks/tests/test_project_settings.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

SETTINGS_FILE = ".claude/settings.json"

GUARD_HOOK = "scripts/hooks/worktree-guard-launch.py"
GUARD_EVENT = "PreToolUse"
# Kept in step with `templates/core/dot-claude/settings.json.tmpl` by a test: a project
# generated today and one back-filled by `--pull` must guard the same tools, or which
# calls reach the guard depends on the month the repo was created.
GUARD_MATCHER = "^(Edit|Write|MultiEdit|NotebookEdit|apply_patch|create_file|Bash|PowerShell)$"
GUARD_COMMAND = 'python3 "${CLAUDE_PROJECT_DIR:-.}/' + GUARD_HOOK + '"'
# Either spelling counts as already guarded: the vendored shim, or devkit's own settings
# naming the guard directly -- devkit holds the guard, so it needs no shim to reach it,
# and back-filling one there would run it twice.
GUARD_SCRIPT_NAMES = frozenset({GUARD_HOOK, "scripts/worktree-guard.py"})


def retired_hook_paths(retired: tuple[str, ...]) -> tuple[str, ...]:
    """The retired entries that could plausibly be wired as a hook command.

    A hook command runs a Python script under `scripts/`. A retired skill, rule, README
    or test file cannot be one, and including them is not merely wasteful -- it is how
    `README.md` came to be treated as a retired hook name. Narrowing the candidate set
    here means the matching in `prune_hook_commands` and in
    `workspace-status.retired_hooks_line` cannot go wrong the same way twice.
    """
    return tuple(rel for rel in retired if rel.startswith("scripts/") and rel.endswith(".py"))


def hook_entries(payload: object) -> list[dict]:
    """Every `{type, command}` entry in a settings tree, in file order.

    One walk, shared by the two questions asked of a settings file -- which retired
    scripts it still names, and whether anything runs the guard. They were separate
    walks that disagreed about which shapes to tolerate.
    """
    found: list[dict] = []
    hooks = payload.get("hooks") if isinstance(payload, dict) else None
    if not isinstance(hooks, dict):
        return found
    for groups in hooks.values():
        for group in groups if isinstance(groups, list) else []:
            entries = group.get("hooks") if isinstance(group, dict) else None
            for entry in entries if isinstance(entries, list) else []:
                if isinstance(entry, dict):
                    found.append(entry)
    return found


def names_in(command: object) -> str:
    """A hook command's path, slash-normalised; "" when it is not a string.

    Normalised because a settings file written on Windows spells the same hook
    `scripts\\hooks\\lint-fix.py`, and every path devkit compares against is POSIX.
    """
    return command.replace("\\", "/") if isinstance(command, str) else ""


def guard_wired(payload: object) -> bool:
    """Whether this settings tree runs the guard, under any event or matcher.

    Deliberately wider than what `wire_guard` writes. A project that wired the shim by
    hand -- a different matcher, an extra event -- has a guard, and a back-fill that
    only recognised its own spelling would append a second copy on every pull.
    """
    for entry in hook_entries(payload):
        probe = names_in(entry.get("command"))
        if any(name in probe for name in GUARD_SCRIPT_NAMES):
            return True
    return False


def wire_guard(payload: object) -> tuple[object, bool]:
    """`(settings, added)` -- the guard's hook group appended when none is present.

    Appended rather than merged into an existing `PreToolUse` group: the groups carry
    their own matchers, and folding this command into one written for a different
    matcher would change which tools *that* hook sees.
    """
    if not isinstance(payload, dict) or guard_wired(payload):
        return payload, False
    hooks = payload.get("hooks")
    hooks = dict(hooks) if isinstance(hooks, dict) else {}
    groups = hooks.get(GUARD_EVENT)
    groups = list(groups) if isinstance(groups, list) else []
    groups.append(
        {"matcher": GUARD_MATCHER, "hooks": [{"type": "command", "command": GUARD_COMMAND}]}
    )
    return {**payload, "hooks": {**hooks, GUARD_EVENT: groups}}, True


def _kept_group(group: object, names: tuple[str, ...], dropped: list[str]) -> object:
    """One hook group with its retired commands removed, or None when nothing is left.

    Appends each retired hook's basename to `dropped` as it goes -- the caller needs
    the names for its report, and threading them out of a comprehension is what kept
    the loop above nested four deep.
    """
    if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
        return group
    kept = []
    for entry in group["hooks"]:
        probe = names_in(entry.get("command", "") if isinstance(entry, dict) else "")
        hit = next((n for n in names if n in probe), "")
        if hit:
            dropped.append(hit.rsplit("/", 1)[-1])
        else:
            kept.append(entry)
    return {**group, "hooks": kept} if kept else None


def prune_hook_commands(payload: object, retired: tuple[str, ...]) -> tuple[object, list[str]]:
    """`(settings, dropped)` with every hook command naming a retired script removed.

    Structural, not textual: it walks the `hooks` tree and drops matching *commands*,
    then any hook group left with no commands, then any event left with no groups. A
    regex over the file would leave `{"hooks": []}` husks behind, and those are not
    harmless -- an event with an empty group list is a shape the harness has to parse,
    and the next reader cannot tell it from one that lost its hook by accident.

    Matches on the **repo-relative path**, not the basename. Basenames were tried first
    and are actively dangerous: `RETIRED_PATHS` holds non-script entries too, one of
    which is `.claude/skills/state-tools/README.md`, so `README.md` became a "retired
    hook" -- and carameli wires a markdownlint hook whose command lists `"README.md"`
    among its arguments. This function would have deleted that hook.
    """
    dropped: list[str] = []
    if not isinstance(payload, dict):
        return payload, dropped
    hooks = payload.get("hooks")
    if not isinstance(hooks, dict):
        return payload, dropped

    names = retired_hook_paths(retired)
    events: dict[str, object] = {}
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            events[event] = groups
            continue
        kept_groups = [g for g in (_kept_group(g, names, dropped) for g in groups) if g]
        if kept_groups:
            events[event] = kept_groups
    # Nothing retired means nothing rewritten, byte for byte. Returning the rebuilt tree
    # here would let this tidy up shapes it was not asked about -- an already empty hook
    # group, a matcher someone left behind -- and `--pull` would show a settings diff in
    # projects where no hook was retired at all.
    if not dropped:
        return payload, []
    return {**payload, "hooks": events}, dropped


def read(root: Path) -> object | None:
    """This project's settings tree, or None when it is absent or will not parse.

    Three-valued on purpose, and the third value is the point: a project with no
    settings file, or one that cannot be read, is not a project with a fault -- it is
    one this cannot speak about. Rewriting a file this could not read is how a pull
    would take a project's whole harness config with it, and reporting it as unguarded
    is how a status line earns being ignored.
    """
    try:
        return json.loads((root / SETTINGS_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def settings_guard(root: Path) -> bool | None:
    """Whether this project's settings run an edit guard; None when they cannot be read."""
    payload = read(root)
    return None if payload is None else guard_wired(payload)


def guard_unwired(root: Path) -> bool:
    """Whether `--check` should report this project as holding the shim and running it.

    Narrower than `settings_guard` alone by one condition: the shim has to be on disk.
    A project that has not vendored it yet is unguarded too, but `--check` already
    reports that file as drift, and one fault reported twice reads as two faults.
    """
    return (root / GUARD_HOOK).is_file() and settings_guard(root) is False


def settings_pass(root: Path, retired: tuple[str, ...]) -> list[str]:
    """The pull's pass over the settings file. Returns one note per change it made.

    Both directions in one read-modify-write, because they are one edit to one file and
    two passes would leave the file half-written if the second failed. Best-effort
    throughout: a settings file that cannot be read is left exactly as it is, and the
    caller reports nothing rather than a guess.

    The wiring will not name a script the project does not have on disk. A hook command
    pointing at a missing file is the precise failure the pruning half exists to undo,
    and a pull that skipped the shim -- `(absent)` in its own report -- is where that
    would otherwise happen.
    """
    payload = read(root)
    if payload is None:
        return []
    pruned, dropped = prune_hook_commands(payload, retired)
    wired, added = (pruned, False)
    if (root / GUARD_HOOK).is_file():
        wired, added = wire_guard(pruned)
    if not dropped and not added:
        return []
    try:
        (root / SETTINGS_FILE).write_text(
            json.dumps(wired, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
    except OSError:
        return []
    notes = [f"(unwired retired hook) {SETTINGS_FILE}: {name}" for name in dropped]
    if added:
        # Named loudly, because it is a behaviour change the pull made to a file the
        # project owns: from here on an agent edit aimed at a checkout's home branch is
        # routed into a box instead of landing on it.
        notes.append(f"(wired the cross-checkout edit guard) {SETTINGS_FILE}: {GUARD_HOOK}")
    return notes


def check_notes(root: Path, codex_file: str, codex_stale: bool) -> list[tuple[str, str]]:
    """`(summary label, stderr line)` for each fault `--check` finds outside the MANIFEST.

    Neither of these is drift: the files that differ are this project's own, which
    `--check` never compares. They are reported at the same volume anyway because an
    unwired guard is the one harness fault with no symptom at all -- the edits land on
    the home branch and nothing is red until a sweep reports the branch days later as
    though a human had left it there.

    Returned as a list rather than printed so `main` neither grows a branch per fault
    nor has to word the summary; both were what pushed that function past its limits.
    """
    notes: list[tuple[str, str]] = []
    if codex_stale:
        notes.append(
            (
                "the generated Codex hooks are stale",
                f"STALE   {codex_file} -- not what {SETTINGS_FILE} generates today; "
                f"Codex is running hook wiring this repo no longer describes. "
                f"Run `python scripts/sync-codex-context.py`",
            )
        )
    if guard_unwired(root):
        notes.append(
            (
                "no hook runs the cross-checkout edit guard",
                f"UNWIRED {SETTINGS_FILE} -- vendors {GUARD_HOOK} but no hook runs it, so "
                f"an agent edit lands on this checkout's home branch with no task branch "
                f"under it. Run `python scripts/sync-devkit.py --pull`",
            )
        )
    return notes


def check_summary(notes: list[tuple[str, str]]) -> str:
    """The clause naming those faults, for the line that says the MANIFEST is in sync.

    Both are named because either can hold a red gate alone, and a summary that
    mentions the Codex hooks when the guard is what is unwired sends the reader to the
    wrong file.
    """
    return " and ".join(label for label, _ in notes)
