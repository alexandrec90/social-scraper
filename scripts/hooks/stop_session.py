"""Whose work a Stop is about, and in which tree — the two questions `stop.py` asks first.

Split out of that module for the reason `guard_probes.py` was split out of the guard: it
is at its structural ceiling, and both tiers here are leaves. They read a lease file and
a transcript and answer one question each; neither knows anything about the gate's checks
or its verdict.

Stdlib only. This runs inside a Stop hook, which fires whether or not a virtualenv
exists — and, for the lease half, from a checkout that has no workspace scripts at all.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = (Path(__file__).parent / "../..").resolve()

# --- Which tree this stop verifies -----------------------------------------
#
# `REPO_ROOT` is where *this file* lives, and for a whole class of session that is not
# where the work is. Claude Code resolves the hook command through `CLAUDE_PROJECT_DIR`,
# which is the session's static checkout, so an agent whose every edit was routed into an
# ephemeral box got a Stop gate pointed at a checkout it never touched: it verified
# whatever branch that checkout happened to be parked on, and blocked on failures
# belonging to somebody else's work. That is not a hypothetical -- a session whose PR gate
# was fully green was blocked twice by two failures on the checkout's `release/…` branch,
# with no edit of its own anywhere in the tree being checked.
#
# The box tier is a *workspace* facility rather than a project one, so the lookup below is
# the stdlib-only half of it: read the lease file, match on the session id, take the
# directory. `worktree.py` owns these names, and a hook cannot import it -- hooks run
# before a venv exists and from a checkout that has no workspace scripts at all. The
# duplication is the same trade `harness_events.BOX_NAME_SEP` makes, for the same reason.
#
# **Absence is the ordinary case, not an error.** Every consuming project has no
# `.worktrees/` at all; so does a CI runner, a fresh clone, and any session working
# directly in its checkout. All of them fall through to `REPO_ROOT` and behave exactly as
# they did before this existed.
BOXES_DIR_NAME = ".worktrees"
LEASE_FILE_NAME = "leases.json"
# `worktree.SESSION_PREFIX_MIN`: a box cut by hand carries `--session <first 8 hex>`, and
# the abbreviation has to keep naming the session that abbreviated it.
SESSION_PREFIX_MIN = 8
# Only a task box holds a session's work. A `preview` box is a throwaway copy of somebody
# else's branch, brought up to be looked at in a browser -- verifying it would block this
# session on a tree it did not write and cannot fix.
BOX_KIND_TASK = "task"

# The tools that put a change in a file. `Bash` is deliberately absent: a session that
# ran a formatter or a generator changed the tree without any of these, so their absence
# cannot be read as "this session changed nothing" -- see `session_wrote_nothing`.
WRITE_TOOLS = frozenset({"Edit", "MultiEdit", "Write", "NotebookEdit"})


def payload(raw_stdin: str) -> dict:
    """The hook payload as a dict; `{}` when there is nothing readable to parse."""
    try:
        loaded = json.loads(raw_stdin)
    except (json.JSONDecodeError, TypeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def session_id(raw_stdin: str) -> str:
    """The session id from the hook payload, or '' when there is none to read."""
    value = payload(raw_stdin).get("session_id")
    return value.strip() if isinstance(value, str) else ""


def sessions_match(recorded: str, session: str) -> bool:
    """Does a lease's session id name this session?

    Exact, or either id a prefix of the other with the shorter side at least
    `SESSION_PREFIX_MIN` characters. Mirrors `worktree.sessions_match` -- see the note
    above for why this is a copy rather than an import.
    """
    if not recorded or not session:
        return False
    if recorded == session:
        return True
    short, long = sorted((recorded, session), key=len)
    return len(short) >= SESSION_PREFIX_MIN and long.startswith(short)


def session_box(session: str, repo_root: Path = REPO_ROOT) -> Path | None:
    """The ephemeral box holding this session's work for `repo_root`, or None.

    Best-effort by contract, in every direction: no lease file, unreadable JSON, an entry
    whose directory has since been reaped, no session id on the payload -- each returns
    None, and the caller verifies the checkout as before. A Stop gate must never fail
    *because* of this lookup; the worst it may do is decline to improve on the default.
    """
    if not session:
        return None
    project = repo_root.name
    try:
        raw = (repo_root.parent / BOXES_DIR_NAME / LEASE_FILE_NAME).read_text(encoding="utf-8")
        boxes = json.loads(raw).get("boxes", {})
        if not isinstance(boxes, dict):
            return None
        for name, lease in boxes.items():
            if not isinstance(lease, dict):
                continue
            if lease.get("project") != project:
                continue
            if lease.get("kind", BOX_KIND_TASK) != BOX_KIND_TASK:
                continue
            if not sessions_match(str(lease.get("session", "")), session):
                continue
            path = repo_root.parent / BOXES_DIR_NAME / name
            # A husk -- a box whose `git worktree remove` died partway -- is a directory
            # with no `.git`, and every check would run against a tree git has stopped
            # tracking. Fall back to the checkout instead.
            if (path / ".git").exists():
                return path
    except (OSError, ValueError, AttributeError):
        return None
    return None


def verify_root(raw_stdin: str, repo_root: Path = REPO_ROOT) -> Path:
    """The tree this stop should verify: the session's box when it has one."""
    return session_box(session_id(raw_stdin), repo_root) or repo_root


def transcript_path(raw_stdin: str) -> Path | None:
    """The session transcript named by the hook payload, or None."""
    value = payload(raw_stdin).get("transcript_path")
    if not isinstance(value, str) or not value.strip():
        return None
    return Path(value.strip())


def uses_a_write_tool(record: object) -> bool:
    """Does this transcript record contain a `tool_use` block naming a write tool?

    Walks rather than indexes: the transcript's envelope is the harness's to change, and
    a shape assumption that silently stopped matching would turn `session_wrote_nothing`
    into "no session ever writes anything" -- which is the failure that matters, because
    it would stand the gate down for every session at once.
    """
    if isinstance(record, dict):
        if record.get("type") == "tool_use" and record.get("name") in WRITE_TOOLS:
            return True
        return any(uses_a_write_tool(value) for value in record.values())
    if isinstance(record, list):
        return any(uses_a_write_tool(item) for item in record)
    return False


def session_wrote_nothing(raw_stdin: str) -> bool:
    """True only when this session's transcript proves it used no file-writing tool.

    The box tier above covers a session that has a box. A *static checkout shared by two
    sessions* is the other half of the same problem, and two reports caught it from
    opposite ends: one session made zero edits while another moved HEAD and committed a
    partial rename under it, and the gate re-fired four times demanding the read-only
    session fix five failures whose only correct resolution was the other session
    finishing. The second report caught the tree changing *between* two of its own stop
    runs -- a different set of failures each time, neither caused by the session being
    blocked. The only escape was the skip variable, which reads as skipping a real check
    and, being all-or-nothing, is the wrong thing to teach.

    Deliberately narrow. It does not scope the checks to the session's own paths: a
    `Bash` call can change a file with no write-tool use, so a *partial* edit set would
    silently shrink the gate. An **empty** one is the only claim the transcript supports
    on its own, and it is exactly the reported case.

    Fails to False -- no transcript, an unreadable one, a malformed line -- because the
    ordinary behaviour of the gate is to verify, and a hook that cannot read its own
    payload must not be what turns it off.
    """
    path = transcript_path(raw_stdin)
    if path is None:
        return False
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not any(tool in line for tool in WRITE_TOOLS):
                    continue  # cheap reject: no write-tool name anywhere in the record
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    return False
                if uses_a_write_tool(record):
                    return False
    except OSError:
        return False
    return True
