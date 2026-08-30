#!/usr/bin/env python3
"""Append-only ledger of harness events, so a block is diagnosable without the chat.

Until now, most of what the harness *does to an agent* left its only record in that
agent's conversation: the capped-Bash gate blocked a command, the worktree guard routed
an edit into a box, a box spawn failed -- and the sole account of it was prose in a
session someone else has to be told to go read. The one exception proved the pattern:
`worktree.py`'s reap ledger exists because a box vanished and nothing on the machine
could say which pass took it. This module generalises that ledger to the rest of the
harness, so a devkit-scoped session diagnoses from a file instead of from hearsay.

Format follows the reap ledger, and for its reasons: one line per event, an ISO stamp
followed by tab-separated `key=value` pairs. The file is read by grepping for a project
or a session id months later, which rules out JSON (an interrupted write leaves an
unparseable document) and prose. Append-only, never rotated -- a record that the next
run overwrites is not a record.

Where the ledger lives decides who can write to it, and there are two callers:

- **Workspace scripts** (`worktree-guard.py`) know their devkit checkout by `__file__`
  and pass `root` explicitly.
- **Vendored copies** in consuming projects know nothing about this machine's layout.
  They reach the ledger through `$DEVKIT_DIR` -- the same machine-level seam
  `sync-devkit.py` reads -- with one fallback: unset, a copy that *is* devkit writes
  to its own checkout, because there the ledger and the copy are the same repo. Unset
  anywhere else (CI, a fresh clone, anyone else's machine) `record` is a silent no-op.
  A diagnostic ledger must never be a reason a hook fails, so unlike the drift gate
  there is no "stamped but unset" failure mode here: silence is always the right
  degradation.

Event names and their fields belong to the writers; this module only owns the format.
`record` never raises -- it is called from hooks on paths where they are already
reporting something else, and from blocking flows that must still block.

Tested in `scripts/hooks/tests/test_harness_events.py`.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import os
import sys
from collections.abc import Mapping
from pathlib import Path

LEDGER = Path("logs") / "harness-events.log"

# The repo this copy is vendored into -- devkit itself, when nobody vendored it. Read by
# `ledger_path` on the branch where `$DEVKIT_DIR` says nothing.
REPO_ROOT = Path(__file__).resolve().parents[2]

# A blocked command or a lint finding can be pages long; the ledger wants enough to
# recognise it, not to replay it. Grep the session transcript for the rest.
VALUE_LIMIT = 300

# Two fields are the exception, and the flat 300 cut exactly the half that mattered.
#
# `message` is an agent's *report*: a diagnosis followed by what it thinks the fix is.
# The proposal is at the end by the nature of the sentence, so a uniform cap deleted the
# actionable half of every report long enough to need one -- three of the four
# agent-reports open on this machine on 2026-08-24 ended mid-word, and one lost its
# entire recommendation ("... Slug the branc"). `detail` is a spawn exception, where
# git's own reason is likewise the tail: five `guard-spawn-failed` rows recorded the
# command that failed and truncated before saying why.
#
# The line format is one record per line with tabs between fields, so length costs
# nothing but disk, and `clean` still collapses newlines. These are ceilings against a
# pathological value reaching the file, not an editorial budget.
FIELD_LIMITS = {"message": 4000, "detail": 2000}


# The line every *block* message ends with: how to report the block itself as wrong.
#
# `report-harness-defect.py` has existed since this ledger did, and the instruction to
# use it lives in `.claude/rules/engineering.md`. That reaches exactly one runtime.
# **Codex reads every `CLAUDE.md` and reads straight past `.claude/rules/`**, no
# `CLAUDE.md` names the reporter, `sync-codex-context.py` mirrors only `.claude/skills/`
# to `.agents/`, and `codex-hook-adapter.py` discards SessionStart output outright -- so
# there was no path by which a Codex session could learn the channel exists. The ledger
# shows the result rather than implying it: on 2026-08-28, across 1176 rows, Codex had
# filed **zero** agent-reports while its sessions were hitting the guard the same day.
# Every report on the backlog is Claude's, and the Codex half of the harness -- the half
# with a whole translation tier that can only be judged from a session running under it
# -- was reporting nothing at all.
#
# So the pointer goes where neither runtime can miss it and no document has to be
# loaded: in the block itself, at the moment an agent has been stopped and is deciding
# between reporting the gate and routing around it. Block paths only. An allow says
# nothing an agent might dispute, and a route already tells it where to re-issue.
REPORT_HINT = (
    "If this block was wrong, report it rather than working around it:\n"
    '  python3 scripts/hooks/report-harness-defect.py --message "<what went wrong>" '
    '--command "<the exact command>"'
)


# `<project>--<slug>-<MMDD>` is `worktree.py`'s box-naming rule, and `project_of` there
# owns it. This is the stdlib-only half a hook can reach: hooks run before the venv
# exists, so importing that module is not available to them.
BOX_NAME_SEP = "--"


# Which agent runtime the hook ran under. Spelled here as a literal for the reason
# `codex-hook-adapter.py` spells it as one: that file is vendored and this one is too,
# but neither may import the other -- a hook loads this module by path from
# `worktree-guard.py`, where `scripts/hooks/` is not on `sys.path`.
ADAPTER_ENV = "DEVKIT_HOOK_ADAPTER"
NATIVE_AGENT = "claude"
UNKNOWN_AGENT = "unknown"


def agent_name(env: Mapping[str, str] | None = None) -> str:
    """The agent runtime this hook is running under: `claude`, `codex`, ...

    Every row on this ledger was written by a hook, and until now no row said **which
    agent's** hook. That is not bookkeeping: the same hook does not behave the same
    under both. A Claude session's PreToolUse response can re-aim a call, a Codex
    session's cannot; the capped-Bash gate is wired for one and deliberately unported
    to the other. So a block recorded under Codex is evidence about the *translation*,
    and a triage pass that reads it as evidence about Claude retires a defect nobody
    fixed -- and the reverse loses a Codex-only one behind a Claude fix.

    The answer is not inferred. `codex-hook-adapter.py` exports `DEVKIT_HOOK_ADAPTER`
    before it spawns a ported hook, precisely so a hook can tell; unset means nothing
    translated this call, which is Claude Code running the hook natively.
    """
    source = os.environ if env is None else env
    return (source.get(ADAPTER_ENV, "") or "").strip().lower() or NATIVE_AGENT


def project_name(root: Path) -> str:
    """The project a checkout belongs to -- the repo name, never a box directory's.

    Every writer used `root.name`, which for an agent session is usually an *ephemeral
    box*: `devkit--guard-quoted-redirect-0823`. Grouping by project months later is the
    ledger's whole purpose, and 28% of its rows named a project that does not exist and
    never will -- twenty pseudo-projects in three days, one per box, none of them
    greppable as the repo the work was actually in. The guard already recorded the real
    name because it resolves a project to decide anything at all; the three writers that
    had no such reason to know were the ones that got it wrong.
    """
    return root.name.split(BOX_NAME_SEP, 1)[0] or root.name


def limit_for(key: str) -> int:
    """How much of `key`'s value the ledger keeps. See `FIELD_LIMITS`."""
    return FIELD_LIMITS.get(key, VALUE_LIMIT)


def clean(value: object, limit: int = VALUE_LIMIT) -> str:
    """One field value, made safe for the line format: whitespace collapsed (tabs and
    newlines are the field and record separators), truncated, never empty."""
    text = " ".join(str(value).split()) or "-"
    return text[:limit]


def event_line(stamp: str, event: str, fields: tuple[tuple[str, object], ...]) -> str:
    """One ledger record. Pure, so the format is testable."""
    pairs = (("event", event), *fields)
    return stamp + "\t" + "\t".join(f"{key}={clean(value, limit_for(key))}" for key, value in pairs)


def _main_checkout(root: Path) -> Path:
    """`root`, or -- when it is a git worktree -- the checkout it was cut from.

    A worktree's `.git` is a *file* holding `gitdir: <main>/.git/worktrees/<name>`, so
    this is one read rather than a `git` subprocess in a hook. It matters because the
    fallback below fires inside ephemeral boxes too, and a ledger written into a box is
    destroyed with it: `worktree.py reconcile` reaps the box, and the report an agent
    filed from it was never on the machine's ledger at all.
    """
    with contextlib.suppress(OSError, ValueError, IndexError):
        pointer = root / ".git"
        if pointer.is_file():
            line = pointer.read_text(encoding="utf-8").strip()
            if line.startswith("gitdir:"):
                gitdir = Path(line.split(":", 1)[1].strip())
                for parent in gitdir.parents:
                    if parent.name == ".git":
                        return parent.parent
    return root


def _own_checkout_is_devkit() -> bool:
    """Whether the copy this module belongs to is devkit itself.

    Delegated to `harness_config` rather than re-implemented, and imported here rather
    than at module scope: this module is loaded by path from `worktree-guard.py`, where
    `scripts/hooks/` is not on `sys.path`, and a ledger helper must not make an import
    error out of a hook that was already reporting something else.
    """
    try:
        here = str(Path(__file__).resolve().parent)
        if here not in sys.path:
            sys.path.insert(0, here)
        import harness_config
    except Exception:  # pragma: no cover - a checkout missing the sibling module
        return False
    return harness_config.is_devkit_source(REPO_ROOT)


def ledger_path(root: Path | None = None) -> Path | None:
    """Where this machine's ledger is, or None when it has none.

    `root` is for callers that already know the devkit checkout; without it the answer
    comes from `$DEVKIT_DIR`.

    Unset, it used to mean "this machine keeps no central ledger" -- true of a consumer
    and of CI, and false in the one place it cost a report: **devkit itself**, where the
    ledger is this very checkout's `logs/`. `$DEVKIT_DIR` is a Claude-settings variable
    on the machine this was written for, so a Codex session filing a defect report with
    `report-harness-defect.py` was told there was nowhere to file it, standing in the
    directory the ledger lives in. So when the env says nothing, this copy answers for
    itself if it *is* devkit, and keeps quiet if it is not.
    """
    if root is not None:
        return root / LEDGER
    devkit = os.environ.get("DEVKIT_DIR", "").strip()
    if not devkit:
        return _main_checkout(REPO_ROOT) / LEDGER if _own_checkout_is_devkit() else None
    base = Path(devkit)
    if not base.is_dir():
        return None
    return base / LEDGER


def record(
    event: str, fields: tuple[tuple[str, object], ...], root: Path | None = None
) -> Path | None:
    """Append one event. Returns the ledger path, or None when nothing was written.

    Best-effort by contract: every caller is a hook mid-block or a CLI mid-report, and
    failing *their* work over bookkeeping would invert the priorities this file exists
    to serve.

    `agent=` is stamped **here** rather than passed by each writer, for the reason the
    workspace states as a rule: a remedy that depends on every caller remembering is the
    same defect again. Several writers reach this ledger and most of them have no reason
    to know what a hook adapter is. A caller recording an event on some *other* runtime's
    behalf passes its own `agent` field, and that one is kept.
    """
    try:
        path = ledger_path(root)
        if path is None:
            return None
        if not any(key == "agent" for key, _ in fields):
            fields = (("agent", agent_name()), *fields)
        stamp = _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(event_line(stamp, event, fields) + "\n")
    except Exception:
        return None
    return path
