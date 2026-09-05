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

That checkout holds **one shard per machine** rather than one file: `record` appends to
`harness-events-<host>.log` and the read half unions every `harness-events*.log` beside
it. `HOST_ENV` above carries why, and why the host is not part of a defect's identity.

Event names and their fields belong to the writers; this module only owns the format.
`record` never raises -- it is called from hooks on paths where they are already
reporting something else, and from blocking flows that must still block.

Tested in `scripts/hooks/tests/test_harness_events.py`.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import os
import platform
import sys
from collections.abc import Mapping
from pathlib import Path

LEDGER_DIR = Path("logs")
LEDGER_STEM = "harness-events"

# Every shard the read half unions. Matches the legacy name below as well as any
# `harness-events-<host>.log`, so a machine that syncs its `logs/` in beside another's
# is read without either of them being told about the other.
LEDGER_GLOB = f"{LEDGER_STEM}*.log"

# The unsharded name every row was written to before a machine had one of its own. Kept
# readable forever -- the ledger is append-only, and that history is most of it.
LEDGER = LEDGER_DIR / f"{LEDGER_STEM}.log"

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


# Which machine wrote a row, and which file it wrote it to.
#
# One ledger per machine was correct while there was one machine. Working the same
# projects from two of them makes it wrong in both directions: a defect filed on the
# laptop is invisible to a triage pass on the desktop, and -- worse, because it is
# silent -- a resolution is itself an event, so a group fixed and retired on one machine
# stays open on the other forever. The second machine re-verifies, and may re-fix, work
# that already shipped.
#
# Pooling wants a single directory that both machines write into, which a single file
# cannot survive: two hosts appending to one synced `harness-events.log` is the case
# every file-sync tool resolves by making conflict copies. So the *filename* carries the
# host and each machine only ever appends to its own, which makes the pooled directory
# conflict-free for any transport -- a synced folder, a private repo, an rsync.
#
# `host=` on the row is the other half, and it is deliberately **not** in
# `harness_triage.Item.signature`: one defect hit on both machines is one defect, and a
# single `--resolve-like` has to retire both machines' copies of it. That is the whole
# point of pooling, and putting the host in the signature would undo it. Which hosts a
# group was seen on is triage evidence -- `render` shows it -- not identity.
HOST_ENV = "DEVKIT_HOST"
UNKNOWN_HOST = "unknown-host"

# The host goes in a filename, so it is reduced to what is portable in one: ASCII
# alphanumerics and dashes. The cap is against a pathological name, not an opinion --
# a corporate hostname can be long, and the shard still has to be a legal path.
HOST_LIMIT = 32


def host_slug(raw: object) -> str:
    """`raw` reduced to a filename-safe token, or `""` when nothing survives.

    ASCII-only on purpose: `str.isalnum` is true for accented and CJK characters, which
    are legal in a filename and a poor thing to have decide whether two machines'
    shards collide after a round trip through a sync tool that normalises them.
    """
    kept = "".join(c if (c.isascii() and c.isalnum()) else "-" for c in str(raw).strip().lower())
    while "--" in kept:
        kept = kept.replace("--", "-")
    return kept.strip("-")[:HOST_LIMIT].strip("-")


def host_name(env: Mapping[str, str] | None = None) -> str:
    """This machine's name for the ledger: `$DEVKIT_HOST`, else the hostname.

    The override exists because the hostname is not always the name a person thinks of
    the machine by, and because a shard filename is durable -- renaming a machine should
    not silently start a second shard that reads as a third machine.
    """
    source = os.environ if env is None else env
    raw = (source.get(HOST_ENV, "") or "").strip()
    if not raw:
        with contextlib.suppress(Exception):  # pragma: no branch - platform-dependent
            raw = platform.node()
    return host_slug(raw) or UNKNOWN_HOST


def ledger_name(env: Mapping[str, str] | None = None) -> str:
    """The shard filename this machine appends to."""
    return f"{LEDGER_STEM}-{host_name(env)}.log"


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


def ledger_root(root: Path | None = None) -> Path | None:
    """The checkout whose `logs/` holds the ledger, or None when there is none.

    Split out of `ledger_path` when the ledger became one file per machine: resolving
    *which checkout* is unchanged and is what every caller shared, while the filename
    below is now the part that varies.

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
        return root
    devkit = os.environ.get("DEVKIT_DIR", "").strip()
    if not devkit:
        return _main_checkout(REPO_ROOT) if _own_checkout_is_devkit() else None
    base = Path(devkit)
    if not base.is_dir():
        return None
    return base


def ledger_path(root: Path | None = None) -> Path | None:
    """The shard this machine appends to, or None when it has no ledger at all."""
    base = ledger_root(root)
    return None if base is None else base / LEDGER_DIR / ledger_name()


def ledger_paths(root: Path | None = None) -> list[Path]:
    """Every shard the read half should union, this machine's included.

    The write target is always in the list even before it exists, so a caller can report
    "nothing open" against a real path rather than against nothing. Everything else in
    the list is whatever is present: the legacy unsharded file, and any other machine's
    shard that a sync has dropped in beside it.

    Never raises, for `record`'s reasons -- an unreadable `logs/` degrades to this
    machine's own shard rather than taking a hook down with it.
    """
    target = ledger_path(root)
    if target is None:
        return []
    found = {target}
    with contextlib.suppress(OSError):
        found.update(p for p in target.parent.glob(LEDGER_GLOB) if p.is_file())
    return sorted(found)


def record(
    event: str, fields: tuple[tuple[str, object], ...], root: Path | None = None
) -> Path | None:
    """Append one event. Returns the ledger path, or None when nothing was written.

    Best-effort by contract: every caller is a hook mid-block or a CLI mid-report, and
    failing *their* work over bookkeeping would invert the priorities this file exists
    to serve.

    `agent=` and `host=` are stamped **here** rather than passed by each writer, for the
    reason the workspace states as a rule: a remedy that depends on every caller
    remembering is the same defect again. Several writers reach this ledger and most of
    them have no reason to know what a hook adapter is, or that the machine they are on
    is one of several. A caller recording an event on some *other* runtime's or machine's
    behalf passes its own `agent` or `host` field, and that one is kept.
    """
    try:
        path = ledger_path(root)
        if path is None:
            return None
        if not any(key == "host" for key, _ in fields):
            fields = (("host", host_name()), *fields)
        if not any(key == "agent" for key, _ in fields):
            fields = (("agent", agent_name()), *fields)
        stamp = _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(event_line(stamp, event, fields) + "\n")
    except Exception:
        return None
    return path
