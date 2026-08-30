#!/usr/bin/env python3
"""Regenerate .codex/hooks.json from .claude/settings.json's `hooks` block.

.claude/settings.json is the single source of truth for hook wiring. Codex reads
its hooks from .codex/hooks.json, so this generator keeps that file in lock-step
with Claude instead of hand-editing.

Hooks are the only thing a project owns here. Codex's own configuration -- model,
reasoning effort, approval policy, sandbox mode, shell environment policy -- lives
in ~/.codex/config.toml, because each of those is a property of whoever is driving
the session rather than of the repo, and a per-project copy silently outranks the
user's. Claude keys like `permissions.allow` are not copied across either.

Codex supports most, but not all, Claude hook events and uses structured JSON
instead of non-zero exit codes for blocking. This generator therefore:

- drops explicitly classified unsupported events, matchers, and redundant handlers,
- resolves shared scripts from the git root so subdirectory sessions work, and
- wraps command hooks with the Codex compatibility adapter, and
- adds Windows handler forms and adjusts known compatibility limits.

Invoked by scripts/sync-codex-context.py after the repository skill mirror.

`to_codex_hooks` is pure and unit-tested
(scripts/hooks/tests/test_sync_codex_hooks.py).
"""

import json
import sys
from pathlib import Path

CLAUDE_PROJECT_DIR_PREFIX = "${CLAUDE_PROJECT_DIR:-.}/"
CODEX_ROOT_EXPR = "__CODEX_PROJECT_ROOT__"
CODEX_LAUNCHER_CODE = (
    "import pathlib,runpy,sys;"
    "p=pathlib.Path.cwd();"
    "r=next(x for x in (p,*p.parents) if (x/'.git').exists());"
    "sys.argv=[str(r/'scripts/hooks/codex-hook-adapter.py'),"
    "*[a.replace('__CODEX_PROJECT_ROOT__',str(r)) for a in sys.argv[1:]]];"
    "runpy.run_path(sys.argv[0],run_name='__main__')"
)
CODEX_LAUNCHER = f'python3 -c "{CODEX_LAUNCHER_CODE}"'
CODEX_ADAPTER = CODEX_LAUNCHER
CLAUDE_SESSION_START = ".claude/hooks/session-start.sh"
CODEX_SESSION_START = f'python3 "{CODEX_ROOT_EXPR}/scripts/hooks/codex-session-start.py"'
CLAUDE_BASH_CAP = "scripts/hooks/enforce-capped-bash.py"
MIN_CODEX_SESSION_START_TIMEOUT = 60
SUPPORTED_EVENTS = frozenset(
    {
        "SessionStart",
        "SessionEnd",
        "UserPromptSubmit",
        "PreToolUse",
        "PermissionRequest",
        "PostToolUse",
        "PreCompact",
        "PostCompact",
        "SubagentStart",
        "SubagentStop",
        "Stop",
    }
)
UNSUPPORTED_EVENTS = frozenset({"PostToolUseFailure"})
UNSUPPORTED_MATCHERS = frozenset({("PostToolUse", "^Skill$")})
REDUNDANT_HANDLERS = frozenset({("PreToolUse", CLAUDE_BASH_CAP)})


# Command prefixes that identify a shared handler worth routing through the adapter.
# `python ` earns its place alongside `python3 `: the workstation-level hooks are
# written by hand with the interpreter that exists on Windows, and a prefix list that
# missed them let every user-level hook through unwrapped -- so Codex received the raw
# command and none of the exit-code translation the adapter exists to provide.
HANDLER_PREFIXES = ("python3 ", "python ", "bash ")


def root_expression(root: str = "", windows: bool = False) -> str:
    """The stable project root spelling for the target command shell."""
    del windows  # The inline Python launcher is identical on every supported OS.
    return root or CODEX_ROOT_EXPR


def adapter_command(root: str = "", windows: bool = False) -> str:
    """The Codex adapter invocation, resolved through `root` or the session's git root.

    `root` is for the **workstation-level** file (`~/.codex/hooks.json`), which has no
    repository to resolve against: a Codex session opened at the workspace root, or in
    any directory that is not a checkout, has no project root for the inline launcher
    to discover. Passing devkit's absolute path fixes those without changing the
    per-repo files, which must stay repo-relative so a clone works anywhere.
    """
    project_root = root_expression(root, windows)
    if not root:
        return CODEX_LAUNCHER
    return f'python3 "{project_root}/scripts/hooks/codex-hook-adapter.py"'


def rewrite_command(command: str, root: str = "", windows: bool = False) -> str:
    """Resolve Claude's project-dir placeholder through the git root, or `root`."""
    return command.replace(CLAUDE_PROJECT_DIR_PREFIX, f"{root_expression(root, windows)}/")


def wrap_command(event: str, command: str, root: str = "", windows: bool = False) -> str:
    """Route a shared Python/bash handler through the Codex compatibility adapter."""
    project_root = root_expression(root, windows)
    rewritten = rewrite_command(command, root, windows)
    if event == "SessionStart" and CLAUDE_SESSION_START in rewritten:
        rewritten = f'python3 "{project_root}/scripts/hooks/codex-session-start.py"'
    if not rewritten.lstrip().startswith(HANDLER_PREFIXES):
        return rewritten
    return f"{adapter_command(root, windows)} --event {event} -- {rewritten}"


def port_handler(event: str, handler: dict, root: str = "") -> dict:
    """Add Codex's POSIX command and a Windows override to a command handler."""
    command = handler.get("command")
    if not isinstance(command, str):
        return dict(handler)

    result = {**handler, "command": wrap_command(event, command, root)}
    if event == "SessionStart" and isinstance(result.get("timeout"), (int, float)):
        # Codex adds its own compatibility process around imported hooks, and the
        # workstation status hook legitimately surveys several repositories plus
        # Task Scheduler. Preserve larger authored budgets, but never port the old
        # 20-second budget that is shorter than the work it encloses.
        result["timeout"] = max(result["timeout"], MIN_CODEX_SESSION_START_TIMEOUT)
    # A hand-authored Windows command is the better source when one exists. Only add
    # an override for handlers routed through the compatibility adapter; copying a
    # shell builtin such as `echo` would claim it was portable without making it so.
    windows_source = handler.get("commandWindows", command)
    if isinstance(windows_source, str):
        windows_rewritten = rewrite_command(windows_source, root, windows=True)
        if (
            event == "SessionStart" and CLAUDE_SESSION_START in windows_rewritten
        ) or windows_rewritten.lstrip().startswith(HANDLER_PREFIXES):
            result["commandWindows"] = wrap_command(event, windows_source, root, windows=True)
    return result


def is_redundant_handler(event: str, handler: dict) -> bool:
    """Whether Codex already provides the guarantee this Claude hook adds."""
    command = handler.get("command")
    if not isinstance(command, str):
        return False
    return any(
        event == redundant_event and handler_path in command
        for redundant_event, handler_path in REDUNDANT_HANDLERS
    )


def to_codex_hooks(claude_settings: dict, root: str = "") -> dict:
    """Build Codex's `{"hooks": ...}` payload from a Claude settings dict.

    Explicitly unsupported events, dead matchers, and handlers whose guarantee Codex
    already provides are omitted. An unclassified event raises so adding a Claude hook
    cannot silently skip Codex compatibility review. A missing `hooks` block yields an
    empty one.
    """
    hooks = claude_settings.get("hooks", {})
    unclassified_events = set(hooks) - SUPPORTED_EVENTS - UNSUPPORTED_EVENTS
    if unclassified_events:
        names = ", ".join(sorted(unclassified_events))
        raise ValueError(
            f"Unclassified Claude hook event(s): {names}. "
            "Add each event to SUPPORTED_EVENTS or UNSUPPORTED_EVENTS."
        )

    result: dict = {}
    for event, groups in hooks.items():
        if event in UNSUPPORTED_EVENTS:
            continue
        new_groups = []
        for group in groups:
            if (event, group.get("matcher", "")) in UNSUPPORTED_MATCHERS:
                continue
            handlers = [
                port_handler(event, handler, root) if isinstance(handler, dict) else handler
                for handler in group.get("hooks", [])
                if not (isinstance(handler, dict) and is_redundant_handler(event, handler))
            ]
            if not handlers:
                continue
            new_group = {**group, "hooks": handlers}
            new_groups.append(new_group)
        if new_groups:
            result[event] = new_groups
    return {"hooks": result}


def render(src: Path, root: str = "") -> str:
    """The exact text `sync` would write for `src`."""
    data = json.loads(src.read_text(encoding="utf-8"))
    return json.dumps(to_codex_hooks(data, root), indent=2) + "\n"


def is_stale(src: Path, dest: Path, root: str = "") -> bool:
    """Whether `dest` differs from what this generator produces from `src` today.

    `.codex/hooks.json` is a **generated artifact that is committed**, and until this
    existed nothing compared the two. That gap is what let the Claude-only Bash cap go
    on blocking Codex long after it was fixed: `REDUNDANT_HANDLERS` dropped the handler
    the day it landed, and every project that had already generated its
    `.codex/hooks.json` kept running the old file, because `--pull` copies the
    *generator* and nothing anywhere read its output. A project found out only by being
    blocked, one command at a time -- and the recovery the block message suggests, wrap
    it in `invoke-capped.py`, is exactly the wrapping the omission exists to prevent, so
    the session pays for it on every command from there on.

    False when either file is absent: a project that has not opted into `.codex/` has
    nothing to keep in step, and a missing settings file is the no-op `sync` already
    treats as clean.
    """
    if not src.exists() or not dest.exists():
        return False
    # `sync` writes with newline='' so the file keeps LF on Windows, but a consumer
    # whose git checked it out with CRLF must not read as drift a regeneration would
    # not change.
    return dest.read_text(encoding="utf-8").replace("\r\n", "\n") != render(src, root)


def sync(src: Path, dest: Path, root: str = "") -> int:
    """Write `src` settings' hooks to `dest` in Codex form. No-op if src missing."""
    if not src.exists():
        return 0
    # newline='' keeps LF on Windows (the repo enforces eol=lf via .gitattributes).
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(render(src, root), encoding="utf-8", newline="")
    return 0


USER_CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"
USER_CODEX_HOOKS = Path.home() / ".codex" / "hooks.json"
BOXES_DIR_NAME = ".worktrees"


def stable_root() -> str:
    """devkit's **permanent** checkout, even when this file is running from a box.

    The workstation-level hooks name an absolute path and are read for as long as the
    machine exists, so they must never point into `<workspace>/.worktrees/<box>`: a box
    is destroyed the moment its PR merges, and every Codex hook on the machine would
    then invoke a deleted directory. Running `--user` from inside a box is not exotic
    -- it is the normal way this repo is worked on now.

    So a box resolves to its sibling `<workspace>/devkit` instead of to itself. Same
    reasoning, and the same `.worktrees` test, as `worktree.DEFAULT_WORKSPACE`.
    """
    here = Path(__file__).resolve().parent.parent
    if here.parent.name == BOXES_DIR_NAME:
        return (here.parent.parent / "devkit").as_posix()
    return here.as_posix()


def main(argv: list[str]) -> int:
    """`[src dest] | --user | --check`. Without arguments, syncs this repo's pair.

    `--check` reports rather than writes, and is what `sync-devkit.py --check` calls so
    a stale `.codex/hooks.json` fails a PR gate instead of being discovered by a Codex
    session hitting the hook it should no longer have.

    `--user` is the workstation pair, and it is what keeps Codex from silently falling
    behind Claude. The user-level `~/.claude/settings.json` wires the hooks that are
    NOT vendored into any repo -- the home-branch edit guard and the task-slug
    recorder -- so a Codex session had neither, and its edits landed on home branches
    with nothing catching them. Run from a SessionStart hook, so any change to the
    Claude file is ported at the next session of either agent rather than whenever
    someone remembers.
    """
    root = ""
    check = "--check" in argv
    positional = [arg for arg in argv if not arg.startswith("--")]
    if "--user" in argv:
        src, dest = USER_CLAUDE_SETTINGS, USER_CODEX_HOOKS
        # No repository to resolve against at this level -- see `adapter_command`.
        root = stable_root()
    elif len(positional) >= 2:
        src, dest = Path(positional[0]), Path(positional[1])
    else:
        repo_root = Path(__file__).resolve().parent.parent
        src = repo_root / ".claude/settings.json"
        dest = repo_root / ".codex/hooks.json"
    if check:
        if not is_stale(src, dest, root):
            return 0
        print(
            f"sync-codex-hooks: {dest} is not what {src} generates today. Codex is "
            f"running hook wiring this repo no longer describes -- regenerate it with "
            f"`python scripts/sync-codex-context.py`.",
            file=sys.stderr,
        )
        return 1
    return sync(src, dest, root)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
