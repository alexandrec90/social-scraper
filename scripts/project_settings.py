#!/usr/bin/env python3
"""The project's own harness config: `.claude/settings.json`, which devkit never vendors.

Split out of `sync-devkit.py` along the seam its own comments already described. That
module copies files listed in a MANIFEST; this one *edits* a file the project owns, and
the difference is a contract, not a detail -- which is why the settings code there kept
needing a paragraph to explain why it was allowed to write at all. `sync-devkit.py` was
also past every structural limit it holds other files to, and this is the half that had
somewhere else to be.

The pull makes one pass over that file. It **unwires every agent hook**: no hook is
wired anywhere -- not devkit's, not the template's, not one a project added -- so a
consumer's settings lose their whole `hooks` block on the pull that delivers this. The
hook scripts are still vendored, and inert while nothing names them. And it **adds the
agent-shell environment** (`AGENT_ENV`) wherever a key is missing, never overwriting a
value the project set itself. And it **links the checkout's dependency directories**
into the trees `claude --worktree` cuts (`dependency_dirs`), which no git hook reaches --
`.venv` only for a project that installs no package of its own (`borrows_venv`).

Stdlib only, like everything else here that runs before a virtualenv exists.

Tested in `scripts/hooks/tests/test_project_settings.py`.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

SETTINGS_FILE = ".claude/settings.json"

# What every agent shell runs with, so a captured result is text rather than terminal
# control codes. Claude Code starts its tools under `FORCE_COLOR=3`, so pytest coloured
# every line and a `rich` progress spinner (pip-audit's) redrew itself into 25 KB of one
# tool result. Each key is read by the Python tools only -- CPython's own `argparse` help
# and tracebacks (`PYTHON_COLORS`, from 3.13), pytest (`PY_COLORS`), rich
# (`TTY_COMPATIBLE`, `TTY_INTERACTIVE`), each checked before `FORCE_COLOR` -- because
# the settings `env` reaches Claude Code's own process too, and a `NO_COLOR` there is a
# bet on how its UI reads colour.
#
# `MSYS2_ARG_CONV_EXCL` is the same idea for Git Bash on Windows, which rewrites an
# argument it takes for a POSIX path list before a native program sees it:
# `git show origin/main:.github/x` reached git as `origin\main;.github\x`, an "ambiguous
# argument" that cost a fixer its turn. An argument opening with one of these prefixes is
# a remote-tracking or full ref, never a path worth converting; absolute paths (`/c/...`)
# still convert. Every other platform ignores the variable.
AGENT_ENV = {
    "PYTHON_COLORS": "0",
    "PY_COLORS": "0",
    "TTY_COMPATIBLE": "0",
    "TTY_INTERACTIVE": "0",
    "MSYS2_ARG_CONV_EXCL": "origin/;upstream/;refs/",
}


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


def with_agent_env(payload: object) -> tuple[object, list[str]]:
    """`(settings, keys added)`: `payload` with every missing `AGENT_ENV` key filled in.

    A key the project already sets is its decision and is kept, whatever its value. A
    tree that already carries them all comes back as the same object, so the pass can
    tell "nothing to write" by identity, as it does for the hooks. An `env` that is not
    an object is a shape this will not guess at.
    """
    if not isinstance(payload, dict):
        return payload, []
    env = payload.get("env", {})
    if not isinstance(env, dict):
        return payload, []
    missing = [key for key in AGENT_ENV if key not in env]
    if not missing:
        return payload, []
    return {**payload, "env": {**env, **{key: AGENT_ENV[key] for key in missing}}}, missing


# The dependency directories a tree cut by `claude --worktree` borrows from its checkout.
# Claude Code runs its own git with hooks off, so the global `post-checkout` that
# provisions every other tree (`worktree_env.py`) never fires for one of its trees, and
# `worktree.symlinkDirectories` is the only seam it offers that is not an agent hook. A
# roguelike session's tree came up with no `node_modules`, so neither `npm run dev` nor
# vitest started until it ran `npm ci` by hand (98fd1f9c). Each one is linked only
# where the file that says the project installs it is at the root or one level down.
#
# `.venv` only where the project installs no package of its own (`borrows_venv`). An
# editable install points the checkout's venv at the checkout's `src/`, so a borrowed one
# runs the checkout's code from inside the tree and its tests quietly test the wrong
# branch. Such a project's tree builds its own `.venv` on its first test or lint run
# instead, through `rerun_in_venv` in the vendored `scripts/hooks/toolchain.py`.
VENV_DIR = ".venv"
NODE_DIR = "node_modules"


def borrows_venv(root: Path) -> bool:
    """Whether `root`'s worktrees may link its `.venv`: it has one and installs no package.

    uv's own rule decides: `[tool.uv] package` when the project sets it, else whether a
    `[build-system]` is declared. A `pyproject.toml` that will not parse is not borrowed,
    since nothing can say what it installs.
    """
    try:
        pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return False
    tool = pyproject.get("tool")
    uv = tool.get("uv") if isinstance(tool, dict) else None
    package = uv.get("package") if isinstance(uv, dict) else None
    if isinstance(package, bool):
        return not package
    return "build-system" not in pyproject


def dependency_dirs(root: Path) -> list[str]:
    """The directories, relative to `root`, that its worktrees should link from it."""
    dirs = [VENV_DIR] if borrows_venv(root) else []
    for manifest in [root / "package.json", *sorted(root.glob("*/package.json"))]:
        where = manifest.parent.relative_to(root)
        if manifest.is_file() and not where.name.startswith((".", NODE_DIR)):
            dirs.append((where / NODE_DIR).as_posix())
    return dirs


def with_worktree_links(payload: object, root: Path) -> tuple[object, list[str]]:
    """`(settings, dirs added)`: `payload` with every `dependency_dirs` entry linked.

    Appended after what the project lists, never replacing it, and the same object back
    when nothing is missing, as `with_agent_env` does. A `worktree` or
    `symlinkDirectories` that is not the shape Claude Code reads is left alone.
    """
    if not isinstance(payload, dict):
        return payload, []
    worktree = payload.get("worktree", {})
    links = worktree.get("symlinkDirectories", []) if isinstance(worktree, dict) else None
    if not isinstance(links, list):
        return payload, []
    missing = [d for d in dependency_dirs(root) if d not in links]
    if not missing:
        return payload, []
    return {**payload, "worktree": {**worktree, "symlinkDirectories": [*links, *missing]}}, missing


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

    It unwires every agent hook, adds the missing `AGENT_ENV` keys and links the missing
    `dependency_dirs` into Claude Code's worktrees. `retired` is
    accepted and unused: a pull runs the *previous* `sync-devkit.py`, which still passes
    it, and a signature it cannot call would fail the one pull that delivers this
    version. Best-effort throughout: a settings file that cannot be read is left exactly
    as it is.
    """
    del retired
    payload = read(root)
    if payload is None:
        return []
    stripped, events = strip_hooks(payload)
    with_env, added = with_agent_env(stripped)
    updated, linked = with_worktree_links(with_env, root)
    if updated is payload:
        return []
    try:
        (root / SETTINGS_FILE).write_text(
            json.dumps(updated, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
    except OSError:
        return []
    notes = []
    if stripped is not payload:
        notes.append(f"(unwired agent hooks) {SETTINGS_FILE}: {', '.join(events) or 'hooks'}")
    if added:
        notes.append(f"(agent shell env) {SETTINGS_FILE}: {', '.join(added)}")
    if linked:
        notes.append(f"(worktree links) {SETTINGS_FILE}: {', '.join(linked)}")
    return notes


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
