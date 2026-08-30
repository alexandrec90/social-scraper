#!/usr/bin/env python3
"""PreToolUse hook: blocks the few shell commands whose output scales with the tree.

This gate used to work the other way round, and the inversion is the whole content of
this file. It required every Bash call to *prove* it was bounded -- through the wrapper,
a `head`/`tail` cap, a redirect, or membership in a growing list of exempt shapes -- and
blocked everything it could not recognise. Recognising bounded shell is not a small
problem: it is most of a shell parser plus a model of what every command prints, and the
exempt tier grew to twenty-five regexes covering heredocs, brace groups, `case` arms,
loop keywords, env-assignment prefixes, `git` global options, and the difference between
a backtick inside single quotes and one inside double quotes.

It did not converge, and the transcripts say so rather than an opinion. Of the 305 blocks
recoverable from this workspace's sessions, **139 -- 46% -- are allowed by the very next
version of the gate**: they were not commands anyone needed to rewrite, they were this
file's own bugs. Fourteen of its eighteen commits are titled some variant of "stop the
gate blocking X". Each of those cost a turn plus the ~1 KB of remedy prose the block
message had to carry, and the remedy was often one the shape could not take -- a heredoc,
a loop and a brace group all fail to survive the wrapper's `cmd.exe`.

A fix here also reaches a consuming project only when someone runs `sync-devkit.py
--pull`, so a false positive keeps being *re*-reported for as long as the vendored copies
lag. The report that prompted this rewrite was a `sed -n '1,30p'` block that had already
been fixed upstream the day before.

So the policy is now stated in the direction that is decidable. **Everything is allowed
except a short list of commands whose output grows with the repository** -- `ls`, `cat`,
`find`, `tree`, `du`, `env`, `git status`, an uncounted `git log`, and a raw
`git diff`/`git show`. Those nine were 68 of the 166 blocks that were the old gate working
as designed, which is to say that nearly everything it was ever right about, it was right
about for one of nine reasons. The list is closed by policy: a command earns a place on it
by being observed to flood a session, not by being suspected of it.

What this gives up is real and worth naming: an unknown command that prints a megabyte is
now allowed through. Two things carry that case instead of a block.

  * `BASH_MAX_OUTPUT_LENGTH` in `.claude/settings.json` is a native, unconditional bound
    on every Bash result. It needs no grammar and cannot false-positive, because it
    truncates bytes that already exist rather than predicting bytes that might.
  * `invoke-capped.py` is still here and still the right tool for a test or lint run,
    where a head *and* a tail window beats truncation from one end. It is now a thing to
    reach for rather than a thing to be blocked into.

Failing **open** is therefore the design and not a gap in it. A blocklist that cannot
parse a statement allows it, and the backstop above bounds the cost; the allow-list this
replaced failed *closed* on everything it could not parse, which is precisely how a gate
arrives at a 46% false-positive rate.

Two pieces of the old parser are kept, because both prevent a block rather than cause one.
`split_top_level` still drops heredoc bodies and comments -- otherwise a commit message
mentioning `cat` is read as a command -- and it still holds a `{ ...; }` group together,
so the cap in `{ pwd; cat big; } | head -c N` is seen to bind the `cat` inside it.

Decision logic is exposed as pure functions (`decide`, `offenders`, `noisy_reason`,
`has_cap`, `statements`, `get_value`) so it can be unit-tested without spawning a
subprocess. See `scripts/hooks/tests/test_enforce_capped_bash.py`.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# scripts/hooks/ on path so the sibling, stdlib-only config helper imports before
# the venv (same pattern as stop.py's harness_config import).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_config

# Guarded: the ledger is diagnostics, and a consumer whose pull went sideways must not
# lose (or crash) the gate over it. `record` itself never raises by contract.
try:
    import harness_events
except ImportError:  # pragma: no cover - a partially vendored consumer
    harness_events = None  # type: ignore[assignment]

REPO_ROOT = (Path(__file__).parent / "../..").resolve()
CFG = harness_config.load(REPO_ROOT)

# Claude Code hook contract: 0 allows the call, 2 blocks it and feeds stderr back
# to the model. Every other non-zero code is reported as a non-blocking hook
# *error* and the tool call proceeds anyway -- so a blocking hook MUST use 2 and
# MUST write its reason to stderr.
EXIT_BLOCK = 2

# The vendored wrapper's path is fixed by the MANIFEST, so it is safe to match literally.
WRAPPER_RE = re.compile(r"scripts/hooks/invoke-capped\.py")

# What counts as a cap on a pipeline segment. Under an allow-list every missing spelling
# here was a false positive, and finding them all took four commits; under a blocklist a
# missing spelling only matters for the nine commands below, and `wc` joins the list for
# free -- it consumes a stream and answers with three integers.
CAP_RE = re.compile(
    # The count is optional because `head` and `tail` are bounded without one: both
    # default to ten lines, so a bare `| head` caps as truly as `| head -20` does, and
    # requiring the number blocked `git status --porcelain | head`. The lookahead is
    # what keeps that from admitting the spellings that are *not* bounded -- `tail -f`
    # follows forever and `tail -n +5` counts from the front -- so an unrecognised flag
    # disqualifies the segment while a plain operand (`head file`) does not.
    r"^(?:head|tail)(?:\s+(?:-c\s*\d+|-n\s*\d+|-\d+))?(?=\s+[^-\s]|\s*$)"
    r"|^wc(?:\s|$)"
    # A counting grep emits one number per input, so it bounds a pipeline the same
    # way `wc` does -- `git show <ref>:<file> | grep -c foo` was blocked for want of
    # this spelling. `-C 3` is context, not count, so the match stays case-sensitive.
    r"|^(?:e|f)?grep\s+(?:\S+\s+)*?(?:-[a-zA-Z]*c|--count)(?=\s|$)"
)

# Redirection of stdout to a file bounds a statement by sending its output somewhere that
# is not the agent's context at all. Matches only *stdout*: `2>&1` and `>&2` duplicate a
# descriptor and must not count, which is what the leading `(?:^|\s)` and the `(?![>&])`
# do. `1>` and `&>` are spelled out because both do redirect stdout.
REDIRECT_RE = re.compile(r"(?:^|\s)(?:1|&)?>>?\s*(?![>&])\S")

# Statement separators, longest first so `&&` is never read as a bare `&`.
STATEMENT_SEPARATORS = ("&&", "||", ";", "\n")

# Quoted spans, blanked before flags are read. Without this the *message* decides:
# `git commit -m "drop the --stat flag"` reads as carrying `--stat`.
QUOTED_SPAN_RE = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"", re.DOTALL)

# A heredoc's body is data, not commands. Without this the `\n` split turns every line of
# a commit message into a "statement", and a message that mentions `cat` blocks the commit.
#
# This pattern does NOT rule out `<<<` on its own: it fails at the first `<` of a
# here-string, the scan advances one character, and `<<'text'` then matches perfectly as a
# heredoc named `text` -- swallowing every statement after it. `split_top_level` consumes
# `<<<` whole before reaching here, which is the only reliable place for that distinction.
HEREDOC_RE = re.compile(r"<<-?\s*(?P<quote>['\"]?)(?P<word>[A-Za-z_][\w.-]*)(?P=quote)")

# Prefixes that emit nothing themselves and hand the terminal to the command behind them:
# loop and conditional keywords, a `!` inverting an exit code, an env-assignment prefix,
# and a `case` header with its pattern arm. Peeling them is what keeps `do cat big` and
# `FOO=1 ls -R /` blocked. Under the old allow-list a missed peel was a false positive;
# here it is a miss in the other direction, which the module docstring accepts by design.
CONTROL_PREFIX_RE = re.compile(r"^(?:do|then|else|elif|if|while|until|\{|\(|!)\s+")
ENV_ASSIGNMENT_PREFIX_RE = re.compile(r"^[A-Za-z_]\w*=(?:'[^']*'|\"(?:\\.|[^\"\\])*\"|\S*)\s+")
CASE_HEADER_RE = re.compile(r"^case\s+\S+\s+in\s+")

# A brace group or subshell that is the *whole* statement, so its members can be judged
# individually. Only reached when nothing binds the group -- a cap or a redirect written
# after the closer is `has_cap`'s business, and it looks first. Anchoring at both ends is
# what keeps `(a) b` and `${HOME}` out of it.
GROUP_BODY_RE = re.compile(r"^[({]\s*(?P<body>.*?)\s*[)}]\s*$", re.DOTALL)
CASE_ARM_RE = re.compile(r"""^[^()&|;'"]+(?:\|[^()&|;'"]+)*\)\s+""")

# Global options between `git` and its subcommand, so the git entries below survive
# `git -C <path> status` as well as `git status`. That spelling is the workspace's own:
# an ephemeral box is never the session's working directory.
_GIT_GLOBAL_OPTS = r"""(?:\s+(?:-[cC]\s+(?:"[^"]*"|'[^']*'|\S+)|--\S+))*"""

# The closed list. Each entry is a command observed to flood a session, paired with the
# remedy that actually applies to it -- a block message that names one command and one
# alternative is a fraction of the size of the general-purpose essay the allow-list had to
# print, because it no longer has to explain a policy the agent has not violated.
#
# `grep`, `rg`, `sed`, `awk` and `python -c` are deliberately absent although each can
# print without limit. Their output is bounded by an argument the caller chose rather than
# by the tree, they were 35 of the old gate's blocks with no flood among them, and every
# one of them has a legitimate quiet spelling the gate would have to learn. That is the
# allow-list's failure mode in miniature, and the native output cap covers the tail risk.
NOISY_COMMANDS = (
    (re.compile(r"ls(?:\s|$)"), "`ls` grows with the directory -- prefer the Glob tool"),
    (re.compile(r"cat\s"), "`cat` prints a whole file -- prefer the Read tool, which pages"),
    (re.compile(r"(?:find|tree)\s"), "`find`/`tree` walk the tree -- prefer the Glob tool"),
    (re.compile(r"du(?:\s|$)"), "`du` walks the tree"),
    (re.compile(r"(?:env|printenv)(?:\s|$)"), "`env` prints the whole environment"),
    (
        re.compile(r"git" + _GIT_GLOBAL_OPTS + r"\s+status(?:\s|$)"),
        "`git status` grows with the working tree",
    ),
)

# `git log` is bounded exactly when it is told how many commits to print, and a counted log
# asked for patches is not: one commit's diff has no bound, so `-p` revokes what the count
# earned.
GIT_LOG_RE = re.compile(r"git" + _GIT_GLOBAL_OPTS + r"\s+log(?:\s|$)")
GIT_LOG_COUNT_RE = re.compile(r"(?:^|\s)(?:-\d+|-n\s*\d+|--max-count(?:=|\s+)\d+)(?=\s|$)")
GIT_LOG_PATCH_RE = re.compile(r"(?:^|\s)(?:-p|-u|--patch)(?=\s|$)")

# A raw diff is the largest single thing a repository can hand back, and `git show <sha>:
# <path>` is `cat` wearing a git prefix. The summary flags are the spellings whose output
# is bounded by the *change* rather than by the tree, and they are exempt: `--name-only`
# heads most scripted uses here and blocking it would rebuild the false-positive tier one
# flag at a time.
GIT_DIFF_RE = re.compile(r"git" + _GIT_GLOBAL_OPTS + r"\s+(?:diff|show)(?:\s|$)")
GIT_DIFF_SUMMARY_RE = re.compile(
    r"(?:^|\s)--(?:stat|shortstat|numstat|name-only|name-status|quiet|exit-code)(?=[\s=]|$)"
)

# PowerShell equivalents, for the Codex override that passes `--shell powershell`. They
# stay separate rather than being folded into the Bash grammar: `Select-Object` has no
# output-bound meaning in Bash, and `head -c` is not a PowerShell pipeline stage.
POWERSHELL_CAP_RE = re.compile(
    r"^(?:Select-Object\s+-(?:First|Last)\s+\d+|Out-Null|Measure-Object)(?=\s|$)", re.IGNORECASE
)
POWERSHELL_NOISY_COMMANDS = (
    (
        re.compile(r"(?:Get-ChildItem|gci|dir)(?:\s|$)", re.IGNORECASE),
        "`Get-ChildItem` grows with the directory -- prefer the Glob tool",
    ),
    (
        re.compile(r"(?:Get-Content|gc|type)(?:\s|$)", re.IGNORECASE),
        "`Get-Content` prints a whole file -- prefer the Read tool, which pages",
    ),
    (
        re.compile(r"Get-ChildItem\b.*-Recurse", re.IGNORECASE),
        "`-Recurse` walks the tree -- prefer the Glob tool",
    ),
)
# `-TotalCount`/`-Tail` are `Get-Content`'s own caps and take it back off the list.
POWERSHELL_CONTENT_CAP_RE = re.compile(r"-(?:TotalCount|Tail)\s+\d+(?=\s|$)", re.IGNORECASE)


def block_message(
    reasons: list[str],
    max_bytes: int,
    command_shell: str = "bash",
    stamp: str = "",
) -> str:
    """The reason string fed back to the agent: what is unbounded, and the way out.

    `stamp` is `harness_config.provenance()` -- which copy of the harness decided this,
    appended as a footer. It is here rather than at the top because the actionable half
    has to lead; and it is on the *block* path only, because that is the message an
    agent goes on to report as a devkit defect when the copy that produced it was
    simply old. See `harness_config.harness_version` for what that costs today.
    """
    shell_flag = " --shell powershell" if command_shell == "powershell" else ""
    bullets = "\n".join(f"  - {reason}" for reason in dict.fromkeys(reasons))
    cap_forms = (
        "`| Select-Object -First N` or `> file`"
        if command_shell == "powershell"
        else "`| head -c N`, `| tail -c N`, `| wc -l` or `> file`"
    )
    return (
        "Blocked: this command's output grows with the repository, not with the "
        "command.\n"
        f"{bullets}\n"
        f"Cap it ({cap_forms}), or route it through the wrapper, which keeps a head "
        "*and* a tail window and preserves the exit code:\n"
        f"  python3 scripts/hooks/invoke-capped.py{shell_flag} "
        f'--command "<your command>" --max-bytes {max_bytes}\n'
        "Nothing else is gated. This is a short blocklist of tree-scaling commands, "
        "not a requirement to prove every call is bounded -- so do not wrap commands "
        "it did not name."
        + (f"\n{harness_events.REPORT_HINT}" if harness_events is not None else "")
        + (f"\n{stamp}" if stamp else "")
    )


def skip_heredoc_bodies(text: str, start: int, delimiters: list[str]) -> int:
    """Index just past the bodies of `delimiters`, beginning at `start`.

    Each body runs to a line whose stripped content is its terminator; an unterminated
    body swallows the rest of the text, which is what a shell does too. Multiple
    delimiters are consumed in order, for `cmd <<A <<B`.
    """
    index = start
    for delimiter in delimiters:
        while index < len(text):
            end = text.find("\n", index)
            line = text[index:] if end == -1 else text[index:end]
            index = len(text) if end == -1 else end + 1
            if line.strip() == delimiter:
                break
    return index


def is_brace_word(text: str, index: int) -> bool:
    """True when the brace at `text[index]` stands as its own word, as a group's does.

    This is the shell's own rule, and it is what keeps `${HOME}`, `awk '{print $1}'` and
    `find -exec ls {} \\;` out of the group grammar.
    """
    before = text[index - 1] if index else " "
    after = text[index + 1] if index + 1 < len(text) else " "
    return before in " \t\n;|&" and after in " \t\n;|&)"


def group_closer(text: str, index: int) -> str | None:
    """The delimiter that would close a group opening at `text[index]`, or None.

    `$(` opens one too, though it is a substitution rather than a subshell, and that is
    not a detail: its `)` has to pop the substitution instead of whatever encloses it.
    """
    char = text[index]
    if char == "(":
        return ")"
    if char == "{" and is_brace_word(text, index):
        return "}"
    return None


def closes_group(text: str, index: int, expected: str) -> bool:
    """True when `text[index]` closes an open group whose closer is `expected`."""
    char = text[index]
    if char != expected:
        return False
    return char == ")" or is_brace_word(text, index)


def split_top_level(text: str, separators: tuple[str, ...]) -> list[str]:
    """Split `text` on `separators` that are outside quotes. Never raises.

    Quote-awareness is the point: `invoke-capped.py --command "cd x; cat f"` is one
    statement, and a naive split would find a `cat` in the quoted argument. Not a shell
    parser -- it tracks single/double quotes, backslash escapes, comments, heredoc bodies
    and balanced groups, which is what the forms this gate sees actually use.

    Heredoc bodies are dropped rather than split; they are data, and the newline split
    otherwise reads each line of a commit message as its own statement. A balanced
    `{ ...; }` or `( ... )` comes back whole, because the cap in `{ a; b; } | head -c N`
    binds the group rather than the `b`.
    """
    out: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    pending_heredocs: list[str] = []
    group_stack: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if quote is not None:
            buf.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < len(text):
                buf.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < len(text):
            buf.append(ch)
            buf.append(text[i + 1])
            i += 2
            continue
        if ch == "#" and (i == 0 or text[i - 1].isspace()):
            # A comment is not a command, and it runs to end of line -- so it also
            # swallows any separator inside it, which is why it is dropped here rather
            # than per-statement afterwards. Only a `#` that starts a word is one;
            # `logs/x#2` and `curl http://h/p#frag` are ordinary arguments.
            newline = text.find("\n", i)
            i = len(text) if newline == -1 else newline
            continue
        if text.startswith("<<<", i):
            # A here-string feeds one word and has no body to skip. Consumed whole so the
            # scan cannot re-enter at the second `<` and read `<<'word'` as a heredoc.
            buf.append("<<<")
            i += 3
            continue
        # A heredoc operator only *declares* its delimiter here; the body starts after the
        # end of the current line, which may still carry a `| head -c N`.
        if ch == "<" and (heredoc := HEREDOC_RE.match(text, i)):
            pending_heredocs.append(heredoc.group("word"))
            buf.append(heredoc.group(0))
            i = heredoc.end()
            continue
        if ch == "\n" and pending_heredocs:
            i = skip_heredoc_bodies(text, i + 1, pending_heredocs)
            pending_heredocs = []
            if "\n" in separators:
                out.append("".join(buf))
                buf = []
            continue
        if group_stack and closes_group(text, i, group_stack[-1]):
            group_stack.pop()
            buf.append(ch)
            i += 1
            continue
        if (closer := group_closer(text, i)) is not None:
            group_stack.append(closer)
            buf.append(ch)
            i += 1
            continue
        # A closer with nothing open is ordinary text and is left alone: a `case` arm
        # reaches this function as `a) cat f`, and treating its `)` as a group's would
        # unbalance the scan for the rest of the command.
        hit = (
            next((sep for sep in separators if text.startswith(sep, i)), None)
            if not group_stack
            else None
        )
        if hit is not None:
            out.append("".join(buf))
            buf = []
            i += len(hit)
            continue
        buf.append(ch)
        i += 1
    out.append("".join(buf))
    return [part.strip() for part in out if part.strip()]


def statements(command: str) -> list[str]:
    """The command's top-level statements -- each judged on its own."""
    return split_top_level(command, STATEMENT_SEPARATORS)


def strip_prefixes(statement: str) -> str:
    """Peel prefixes that emit nothing, so `do cat big` is judged as `cat big`.

    The loop runs to a fixed point because the prefixes stack: `until ! test -f x` is two
    of them, and a `case` arm arrives as a header plus a pattern in front of a command.
    """
    while True:
        peeled = CASE_ARM_RE.sub("", CASE_HEADER_RE.sub("", statement, count=1), count=1)
        peeled = CONTROL_PREFIX_RE.sub("", peeled, count=1)
        peeled = ENV_ASSIGNMENT_PREFIX_RE.sub("", peeled, count=1)
        if peeled == statement:
            return statement
        statement = peeled


def strip_quoted(statement: str) -> str:
    """`statement` with quoted spans blanked out, so flags are read but prose is not."""
    return QUOTED_SPAN_RE.sub(" ", statement)


def has_cap(statement: str) -> bool:
    """True when this statement's output is bounded by a cap, a redirect, or the wrapper.

    A cap anywhere in the pipeline counts, not just at the end: everything downstream of
    it can only receive what it passed, so `cat big | head -c 100 | grep x` is genuinely
    bounded. Quoted spans collapse to a word character rather than to a space before the
    redirect check, because a redirect target is often quoted (`> "/tmp/run.log"`) and
    blanking it leaves a `>` with nothing after it to match.
    """
    if WRAPPER_RE.search(statement):
        return True
    if REDIRECT_RE.search(QUOTED_SPAN_RE.sub("q", statement)):
        return True
    return any(CAP_RE.match(segment.strip()) for segment in split_top_level(statement, ("|",)))


def has_powershell_cap(statement: str) -> bool:
    """The PowerShell spellings of the same three bounds."""
    if WRAPPER_RE.search(statement) or POWERSHELL_CONTENT_CAP_RE.search(statement):
        return True
    if REDIRECT_RE.search(QUOTED_SPAN_RE.sub("q", statement)):
        return True
    return any(
        POWERSHELL_CAP_RE.match(segment.strip()) for segment in split_top_level(statement, ("|",))
    )


def noisy_reason(statement: str, command_shell: str = "bash") -> str | None:
    """Why this statement's output scales with the tree, or None if it does not.

    Judged on the peeled statement and on the *first* command in the pipeline, since that
    is the one producing the bytes; a downstream stage can only ever bound them, which is
    `has_cap`'s question rather than this one's.

    An unbound group is unpacked and judged member by member, because `split_top_level`
    deliberately hands it over whole. Peeling its `{` like an ordinary control prefix
    would leave only the first member facing the table, and `{ pwd; cat big; }` would pass
    on the strength of the `pwd`. The recursion terminates because a group's body is
    strictly shorter than the statement holding it, and it is only ever reached with
    nothing binding the group -- `has_cap` has already looked at the tail.
    """
    text = statement.strip()
    if group := GROUP_BODY_RE.match(text):
        return next(
            (
                reason
                for member in statements(group.group("body"))
                if (reason := noisy_reason(member, command_shell=command_shell)) is not None
            ),
            None,
        )
    segments = split_top_level(strip_prefixes(text), ("|",))
    head = strip_prefixes(segments[0].strip()) if segments else ""
    if not head:
        return None
    table = POWERSHELL_NOISY_COMMANDS if command_shell == "powershell" else NOISY_COMMANDS
    for pattern, reason in table:
        if pattern.match(head):
            return reason
    if command_shell == "powershell":
        return None
    flags = strip_quoted(head)
    if GIT_LOG_RE.match(head):
        if not GIT_LOG_COUNT_RE.search(flags):
            return "`git log` grows with the history -- give it a commit count (`-5`)"
        if GIT_LOG_PATCH_RE.search(flags):
            return "`git log -p` prints every commit's full diff, however few commits"
        return None
    if GIT_DIFF_RE.match(head) and not GIT_DIFF_SUMMARY_RE.search(flags):
        return "a raw `git diff`/`git show` has no bound -- add `--stat` or `--name-only`"
    return None


def offenders(command: str, command_shell: str = "bash") -> list[str]:
    """Every reason this command is blocked; empty when it is allowed."""
    capped = has_powershell_cap if command_shell == "powershell" else has_cap
    return [
        reason
        for statement in statements(command)
        if not capped(statement)
        and (reason := noisy_reason(statement, command_shell=command_shell)) is not None
    ]


def get_value(obj, *paths):
    """Return the first present dotted-path value (as str) from a nested dict."""
    for path in paths:
        cur = obj
        ok = True
        for key in path.split("."):
            if not isinstance(cur, dict) or key not in cur:
                ok = False
                break
            cur = cur[key]
        if ok and cur is not None:
            return str(cur)
    return None


def decide(
    raw: str,
    max_bytes: int | None = None,
    command_shell: str = "bash",
    stamp: str = "",
) -> tuple[int, str]:
    """Pure decision: map raw stdin payload to (exit_code, message).

    exit_code 0 allows the call, EXIT_BLOCK blocks it. message may be empty.
    `max_bytes` defaults to the manifest value; injectable so a test does not
    depend on the repo it happens to run in, and `stamp` for the same reason --
    the provenance footer names a SHA, which no assertion should have to predict.
    """
    cap = CFG.bash.max_bytes if max_bytes is None else max_bytes

    if not raw.strip():
        return 0, ""

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return 0, "enforce-capped-bash: unable to parse hook payload; skipping enforcement."

    tool_name = get_value(payload, "tool_name", "toolName", "tool.name", "name")
    if tool_name != "Bash":
        return 0, ""

    command = get_value(
        payload, "tool_input.command", "toolInput.command", "input.command", "command"
    )
    if not command or not command.strip():
        # An empty command prints nothing, and the payload shape is the harness's problem
        # rather than the agent's. The allow-list blocked here because it could not prove
        # boundedness; a blocklist has nothing to name, and a block message that names no
        # command is one no agent can act on.
        return 0, ""

    reasons = offenders(command, command_shell=command_shell)
    if not reasons:
        return 0, ""

    return EXIT_BLOCK, block_message(reasons, cap, command_shell=command_shell, stamp=stamp)


def record_block(raw: str) -> None:
    """One harness-events ledger line per block, so a false positive is diagnosable
    from `logs/harness-events.log` in the devkit checkout rather than from the chat of
    whichever session it hit. Best-effort: `harness_events.ledger_path` owns where that
    is, and `record` no-ops (never raises) when the answer is nowhere.
    """
    if harness_events is None:
        return
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return
    harness_events.record(
        "capped-bash-block",
        (
            ("project", harness_events.project_name(REPO_ROOT)),
            ("session", get_value(payload, "session_id", "sessionId") or ""),
            ("version", harness_config.harness_version(REPO_ROOT)),
            (
                "command",
                get_value(
                    payload, "tool_input.command", "toolInput.command", "input.command", "command"
                )
                or "",
            ),
        ),
    )


def _read_stdin() -> str:
    """The hook payload, decoded as UTF-8 rather than through the platform codec.

    Same defect and same remedy as `worktree-guard.read_stdin`, which owns the account
    of it. Here the cost is smaller and still real: a command carrying an em dash or a
    quoted path with an accent arrives mangled, so `decide` judges a string the agent
    never wrote and `record_block` puts that string on the ledger as evidence.
    """
    try:
        buffer = getattr(sys.stdin, "buffer", None)
        if buffer is not None:
            return buffer.read().decode("utf-8", errors="replace")
        return sys.stdin.read() if sys.stdin is not None else ""
    except (OSError, ValueError):
        return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shell", choices=("bash", "powershell"), default="bash")
    args = parser.parse_args(argv)
    # UTF-8, never the platform codec -- see `worktree-guard.read_stdin`. A command
    # carrying an em dash decoded through cp1252 reaches `decide` mangled, and the
    # mangling is then what `record_block` puts on the ledger.
    raw = _read_stdin()
    exit_code, message = decide(
        raw,
        command_shell=args.shell,
        stamp=harness_config.provenance(REPO_ROOT),
    )
    if exit_code == EXIT_BLOCK:
        record_block(raw)
    if message:
        # stderr, not stdout: only stderr is surfaced for a blocking hook.
        print(message, file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
