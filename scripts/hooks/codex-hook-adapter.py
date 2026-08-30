#!/usr/bin/env python3
"""Run a Claude-style command hook with Codex-compatible failure semantics.

Claude command hooks commonly use non-zero exit codes to block a tool or stop.
Codex supports exit code 2 for several lifecycle events, but not every shared hook
uses that exact code or event-specific output contract. Generated Codex wiring
therefore routes shared handlers through this adapter so any failure becomes the
structured JSON decision for its event.

The adapter also sets ``CLAUDE_PROJECT_DIR`` for shared hook scripts. Codex runs
hook commands from the session cwd, which may be below the repository root.

Failure is the easy direction, because an exit code says so. The **success** direction
is where a translation goes wrong quietly, and `translate_success` below carries that
half: a hook that exits 0 has its stdout passed through, and whether Codex acts on it is
decided by a schema this repo does not own. Every member is therefore checked against
Codex's *own* published contract -- extracted from the binary into `codex-hook-schema.json`
by `scripts/extract-codex-schema.py` -- rather than against a list anybody here wrote
from memory. The comment above `SCHEMA_FILE` says what happened when it was such a list.

Tested in `scripts/hooks/tests/test_codex_hook_adapter.py`, and replayed against the
project's own wired hooks by `scripts/hooks/tests/test_codex_translation.py`.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
# Duplicated as a literal in devkit's `scripts/worktree-guard.py`, which reads it. The
# two cannot share a constant: this file is vendored into every project and that one is
# devkit's alone, so an import either way would break in whichever tree lacks the other.
# devkit's `tests/test_worktree_guard.py` asserts the two spellings match, which is the
# only place both files exist.
ADAPTER_ENV = "DEVKIT_HOOK_ADAPTER"
MAX_REASON_BYTES = 4000
CONTINUATION_EVENTS = frozenset({"PostToolUse", "SubagentStop", "Stop", "UserPromptSubmit"})
COMMON_STOP_EVENTS = frozenset({"PreCompact", "PostCompact"})


def cap_reason(text: str, max_bytes: int = MAX_REASON_BYTES) -> str:
    """Return a UTF-8 byte-capped head/tail failure reason."""
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= max_bytes:
        return text
    marker = b"\n... hook output truncated ...\n"
    remaining = max(0, max_bytes - len(marker))
    head_size = remaining // 2
    tail_size = remaining - head_size
    clipped = raw[:head_size] + marker + raw[-tail_size:]
    return clipped.decode("utf-8", errors="replace")


def failure_output(event: str, reason: str) -> dict:
    """Build the Codex response for a failed underlying hook command."""
    if event == "PreToolUse":
        return {
            "hookSpecificOutput": {
                "hookEventName": event,
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    if event == "PermissionRequest":
        return {
            "hookSpecificOutput": {
                "hookEventName": event,
                "decision": {
                    "behavior": "deny",
                    "message": reason,
                },
            }
        }
    if event in CONTINUATION_EVENTS:
        return {"decision": "block", "reason": reason}
    if event in COMMON_STOP_EVENTS:
        return {"continue": False, "stopReason": reason, "systemMessage": reason}
    return {"systemMessage": reason}


# --- The success path is the one that translates silently ---------------------------
#
# Everything above concerns a hook that *failed*, where the exit code says so and this
# adapter rewrites it into the decision shape for its event. The dangerous direction is
# the other one: a hook that exits 0 has its stdout passed through verbatim, so whether
# its response means anything is decided entirely by Codex, silently, and a hook that
# grows a member Codex does not take does not start failing -- it stops taking effect.
#
# **Which members Codex takes is not something to guess at, and the first version of
# this section guessed wrong.** It carried a hand-built allowlist, flat across every
# event, and classified `hookSpecificOutput.updatedInput` as Claude-only -- on the
# strength of `worktree-guard.py` having been changed to stop emitting it under Codex.
# Codex 0.149.1 accepts it on PreToolUse. Refusing it would have converted the guard's
# re-aim into a hard deny on every Codex edit: the same class of silent-wrongness, in
# the other direction, shipped as the fix for it.
#
# The contract comes off Codex itself now. The binary embeds a draft-07 JSON Schema per
# hook event, `scripts/extract-codex-schema.py` reads them out, and the result is
# committed as `codex-hook-schema.json` beside this file -- vendored, so no Codex
# install is needed to run or test the adapter, and versioned, so a Codex upgrade shows
# up as a diff a human approves.
#
# Two facts from those schemas shape everything below, and neither was guessable:
#
# - **The accepted set is per event.** `hookSpecificOutput.permissionDecision` is
#   PreToolUse's; PermissionRequest takes `hookSpecificOutput.decision` instead; Stop
#   takes neither. Any flat allowlist is wrong for at least one event.
# - **Every schema is `additionalProperties: false`.** An unrecognised member is a
#   validation failure, not something quietly ignored -- so passing one through risks
#   the whole response being rejected, taking the hook's actual decision with it.
SCHEMA_FILE = Path(__file__).resolve().parent / "codex-hook-schema.json"

# The members that carry a decision, as opposed to decorating one. This list is about
# *Claude's* response contract, which devkit does control, and it is the one hand-held
# thing left here -- it decides what happens when Codex does not accept a member:
# refuse the call (a decision would be lost) or strip it (nothing would be).
DECISION_MEMBERS = {
    "decision": "it is the hook's block/approve verdict",
    "reason": "it is why the hook blocked, and the only thing the agent can act on",
    "continue": "it is the hook's instruction to stop the turn",
    "hookSpecificOutput.permissionDecision": "it is the allow/deny/ask verdict",
    "hookSpecificOutput.decision": "it is the permission verdict",
    "hookSpecificOutput.updatedInput": (
        "it replaces the arguments the tool is called with, so losing it runs the "
        "call the hook meant to rewrite"
    ),
    "hookSpecificOutput.updatedMCPToolOutput": "it replaces what the tool reported",
}

GAP_EVENT = "codex-translation-gap"


def accepted_members(event: str, schema_file: Path = SCHEMA_FILE) -> frozenset[str]:
    """What Codex's own schema accepts for `event`. Empty when it is not known.

    An unreadable or absent snapshot yields an empty set, and every caller below treats
    that as "no finding" rather than as "nothing is accepted". The failure has to be
    inert: this runs inside a live hook, and a vendored copy that predates the snapshot
    must not start refusing responses that were working.
    """
    try:
        parsed = json.loads(schema_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    members = (parsed.get("events") or {}).get(event) or []
    return frozenset(str(name) for name in members)


def classify_member(name: str, event: str, schema_file: Path = SCHEMA_FILE) -> str:
    """One response member as `portable`, `dropped`, `lost`, or `unknown-event`.

    - `portable` -- Codex's schema for this event accepts it.
    - `dropped` -- not accepted, but decorative: stripping it keeps the decision.
    - `lost` -- not accepted, and it *is* the decision. This is the one that must not
      be passed through quietly.
    - `unknown-event` -- no snapshot for this event, so nothing is claimed either way.
    """
    accepted = accepted_members(event, schema_file)
    if not accepted:
        return "unknown-event"
    if name in accepted:
        return "portable"
    return "lost" if name in DECISION_MEMBERS else "dropped"


def response_members(parsed: object) -> list[str]:
    """Every member of a hook response, `hookSpecificOutput` children dotted.

    One level deep on purpose: that is where the decision members live in every shape
    this adapter knows, and a recursive walk would classify the *contents* of a reason
    string as members.
    """
    if not isinstance(parsed, dict):
        return []
    names = []
    for key, value in parsed.items():
        names.append(str(key))
        if key == "hookSpecificOutput" and isinstance(value, dict):
            names.extend(f"hookSpecificOutput.{child}" for child in value)
    return names


def response_problems(stdout: str, event: str) -> tuple[list[str], list[str]]:
    """`(lost, dropped)` members in a successful hook's stdout, for this event.

    Non-JSON stdout has no members and is not a problem: a hook is free to print prose,
    and both runtimes treat that as context rather than as a decision.
    """
    try:
        parsed = json.loads(stdout)
    except (ValueError, TypeError):
        return [], []
    lost, dropped = [], []
    for name in response_members(parsed):
        verdict = classify_member(name, event)
        if verdict == "lost":
            lost.append(name)
        elif verdict == "dropped":
            dropped.append(name)
    return lost, dropped


def strip_members(stdout: str, members: list[str]) -> str:
    """`stdout` without `members`, so the rest of the decision still validates.

    Necessary because every Codex output schema is `additionalProperties: false`: left
    in, one decorative member Codex does not know can cost the whole response. Only
    ever called with members `classify_member` judged decorative, so nothing an agent
    can act on is removed here.
    """
    try:
        parsed = json.loads(stdout)
    except (ValueError, TypeError):
        return stdout
    if not isinstance(parsed, dict):
        return stdout
    for name in members:
        head, _, child = name.partition(".")
        if not child:
            parsed.pop(head, None)
        elif isinstance(parsed.get(head), dict):
            parsed[head].pop(child, None)
    return json.dumps(parsed)


def dropped_reason(event: str, members: list[str], stdout: str) -> str:
    """What to tell the agent when a response Codex cannot honour is refused instead.

    The hook's own words come last and are what the agent actually needs -- the guard's
    `additionalContext` is the paragraph naming the box to re-issue against. Refusing
    without it would trade a silent wrong outcome for a loud useless one.
    """
    losses = "; ".join(f"`{name}` -- {DECISION_MEMBERS[name]}" for name in members)
    said = ""
    with contextlib.suppress(ValueError, TypeError, AttributeError):
        parsed = json.loads(stdout)
        specific = parsed.get("hookSpecificOutput") or {}
        said = str(specific.get("additionalContext") or parsed.get("reason") or "").strip()
    head = (
        f"This {event} hook succeeded, but its response is one Codex cannot honour: "
        f"{losses}. Codex validates a hook response against a fixed schema for each "
        f"event, so passing this through would lose the hook's decision rather than "
        f"report it — devkit's hook adapter refuses the call instead of letting it run "
        f"as though the hook had allowed it."
    )
    return f"{head}\n\n{said}" if said else head


def _record_gap(event: str, members: list[str], repo_root: Path, kind: str = "dropped") -> None:
    """Put a member Codex will not take on the events ledger. Never raises, never blocks.

    Both kinds are recorded, because both are translation defects even when the
    immediate handling is right: a `dropped` member means a hook is emitting something
    this runtime never reads, and a `lost` one means an agent just took a deny it would
    not have taken under Claude. Neither is visible from a session transcript, which is
    what put this on the ledger rather than in a log line.
    """
    with contextlib.suppress(Exception):
        here = str(Path(__file__).resolve().parent)
        if here not in sys.path:
            sys.path.insert(0, here)
        import harness_events

        harness_events.record(
            GAP_EVENT,
            (
                ("project", harness_events.project_name(repo_root)),
                ("hook_event", event),
                ("detail", f"{kind} response member(s): " + " ".join(members)),
            ),
            root=None,
        )


def translate_success(
    event: str, stdout: str, *, repo_root: Path = REPO_ROOT
) -> tuple[str, list[str], list[str]]:
    """A successful hook's stdout as Codex should receive it.

    Three outcomes, and which one applies is decided by Codex's own schema rather than
    by this file's opinion:

    - Everything accepted: passed through untouched.
    - A decorative member Codex does not take: **stripped**, and recorded. Left in it
      would fail validation and take the accepted members down with it.
    - A decision member Codex does not take: **refused**, converted into a deny that
      carries the hook's own words. Passing it through is the silent failure this whole
      section exists to stop, and stripping it would be the same failure with extra
      steps.

    Returns the stdout to emit plus the two member lists, so a caller (and a test) can
    see what was found without re-parsing.
    """
    lost, dropped = response_problems(stdout, event)
    if lost:
        _record_gap(event, lost, repo_root, "lost")
        reason = cap_reason(dropped_reason(event, lost, stdout))
        return json.dumps(failure_output(event, reason)), lost, dropped
    if dropped:
        _record_gap(event, dropped, repo_root, "dropped")
        return strip_members(stdout, dropped), lost, dropped
    return stdout, lost, dropped


def run_hook(
    event: str,
    command: list[str],
    raw_stdin: str,
    *,
    repo_root: Path = REPO_ROOT,
) -> tuple[int, str, str]:
    """Run one hook and return adapter exit code, stdout, and stderr."""
    if not command:
        reason = "Codex hook adapter received no command."
        return 0, json.dumps(failure_output(event, reason)), ""

    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(repo_root)
    env["PYTHONUTF8"] = "1"
    # Announce the adapter, because `CLAUDE_PROJECT_DIR` above makes a hook look like it
    # is running under Claude Code when it is not. `worktree-guard.py` reads it to fall
    # back to a plain block rather than re-aim a call, and `harness_events.agent_name`
    # reads it so a row on the ledger says which runtime produced it. A hook that grows
    # a Claude-specific response has the same seam to read -- and `translate_success`
    # above is what happens when it does not.
    #
    # The guard's fallback is now *conservative* rather than *necessary*: Codex's
    # PreToolUse schema does accept `hookSpecificOutput.updatedInput`, so a re-aim may
    # well work there. Accepting a schema as proof of runtime behaviour is the mistake
    # that produced the wrong classification this file used to carry, so the guard keeps
    # blocking -- loudly and with the box named -- until a live Codex session is watched
    # honouring a re-aim. `tests/test_codex_hooks_live.py` is where that observation goes.
    env[ADAPTER_ENV] = "codex"
    try:
        result = subprocess.run(
            command,
            cwd=repo_root,
            env=env,
            input=raw_stdin,
            capture_output=True,
            encoding="utf-8",
            errors="surrogateescape",
            check=False,
        )
    except OSError as exc:
        reason = cap_reason(f"{command[0]} could not start: {exc}")
        return 0, json.dumps(failure_output(event, reason)), ""

    if result.returncode == 0:
        # Codex can mark an otherwise successful hook as failed when the command
        # emits diagnostics on stderr. Hook output belongs on stdout; discard
        # success-only diagnostics while retaining every failure detail below.
        # SessionStart's shared script only prints operational status; it is not
        # model context, and an empty successful response is the most portable
        # contract across Codex releases.
        if event == "SessionStart":
            return 0, "", ""
        stdout, _, _ = translate_success(event, result.stdout, repo_root=repo_root)
        return 0, stdout, ""

    detail = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
    reason = detail or f"{command[0]} failed with exit code {result.returncode}."
    return 0, json.dumps(failure_output(event, cap_reason(reason))), ""


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    return args


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args(sys.argv[1:] if argv is None else argv)
    raw_stdin = sys.stdin.read()
    exit_code, stdout, stderr = run_hook(args.event, args.command, raw_stdin)
    if stdout:
        sys.stdout.write(stdout)
        if not stdout.endswith("\n"):
            sys.stdout.write("\n")
    if stderr:
        sys.stderr.write(stderr)
        if not stderr.endswith("\n"):
            sys.stderr.write("\n")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
