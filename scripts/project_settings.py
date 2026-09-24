#!/usr/bin/env python3
"""The project's own harness config: `.claude/settings.json`, which devkit never vendors.

Split out of `sync-devkit.py` along the seam its own comments already described. That
module copies files listed in a MANIFEST; this one *edits* a file the project owns, and
the difference is a contract, not a detail -- which is why the settings code there kept
needing a paragraph to explain why it was allowed to write at all. `sync-devkit.py` was
also past every structural limit it holds other files to, and this is the half that had
somewhere else to be.

The pull makes one pass over that file: it **unwires every agent hook**. No hook is
wired anywhere -- not devkit's, not the template's, not one a project added -- so a
consumer's settings lose their whole `hooks` block on the pull that delivers this. The
hook scripts are still vendored, and inert while nothing names them.

Stdlib only, like everything else here that runs before a virtualenv exists.

Tested in `scripts/hooks/tests/test_project_settings.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

SETTINGS_FILE = ".claude/settings.json"


def retired_hook_paths(retired: tuple[str, ...]) -> tuple[str, ...]:
    """The retired entries that could plausibly be wired as a hook command.

    A hook command runs a Python script under `scripts/`. A retired skill, rule, README
    or test file cannot be one, and including them is not merely wasteful -- it is how
    `README.md` came to be treated as a retired hook name, which
    `workspace-status.retired_hooks_line` would otherwise report.
    """
    return tuple(rel for rel in retired if rel.startswith("scripts/") and rel.endswith(".py"))


def hook_entries(payload: object) -> list[dict]:
    """Every `{type, command}` entry in a settings tree, in file order.

    One walk, tolerant of every malformed shape, for whatever asks which scripts a
    settings file still names.
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


def strip_hooks(payload: object) -> tuple[object, list[str]]:
    """`(settings, events)` with the whole `hooks` block removed, and the events it held.

    Whole, not per command: no agent hook is wired anywhere any more -- devkit's own,
    the template's, or one a project added itself -- so there is nothing to tell apart.
    A settings tree with no `hooks` key comes back untouched, byte for byte, so a pull
    over an already clean project shows no settings diff.
    """
    if not isinstance(payload, dict) or "hooks" not in payload:
        return payload, []
    hooks = payload["hooks"]
    events = sorted(hooks) if isinstance(hooks, dict) else []
    return {key: value for key, value in payload.items() if key != "hooks"}, events


def read(root: Path) -> object | None:
    """This project's settings tree, or None when it is absent or will not parse.

    Three-valued on purpose, and the third value is the point: a project with no
    settings file, or one that cannot be read, is not a project with a fault -- it is
    one this cannot speak about. Rewriting a file this could not read is how a pull
    would take a project's whole harness config with it.
    """
    try:
        return json.loads((root / SETTINGS_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def settings_pass(root: Path, retired: tuple[str, ...] = ()) -> list[str]:
    """The pull's pass over the settings file. Returns one note per change it made.

    It unwires every agent hook. `retired` is accepted and unused: a pull runs the
    *previous* `sync-devkit.py`, which still passes it, and a signature it cannot call
    would fail the one pull that delivers this version. Best-effort throughout: a
    settings file that cannot be read is left exactly as it is.
    """
    del retired
    payload = read(root)
    if payload is None:
        return []
    stripped, events = strip_hooks(payload)
    if stripped is payload:
        return []
    try:
        (root / SETTINGS_FILE).write_text(
            json.dumps(stripped, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
    except OSError:
        return []
    return [f"(unwired agent hooks) {SETTINGS_FILE}: {', '.join(events) or 'hooks'}"]


def check_notes(root: Path, codex_file: str, codex_stale: bool) -> list[tuple[str, str]]:
    """`(summary label, stderr line)` for each fault `--check` finds outside the MANIFEST.

    It is not drift: the file that differs is this project's own, which `--check` never
    compares. A stale Codex mirror is reported anyway because Codex reads that file, not
    the settings it was generated from.

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
    return notes


def check_summary(notes: list[tuple[str, str]]) -> str:
    """The clause naming those faults, for the line that says the MANIFEST is in sync.

    Named rather than counted, so the reader is sent to the file that is wrong.
    """
    return " and ".join(label for label, _ in notes)
