"""Task failure-artifact wrapper.

Runs a command, streams its output to the terminal as it arrives, and persists that
output to a parseable file under `logs/` when the command fails. Exits with the
wrapped command's code, so VS Code still shows the right task icon.

Usage (tasks.json):
  python scripts/log-wrap.py [--always] "Task Name" -- <command> [args...]

Example:
  python scripts/notify-wrap.py "Devkit: Upgrade Projects" --
    python scripts/log-wrap.py "Devkit: Upgrade Projects" --
      python scripts/upgrade-project.py --all

**Why a second wrapper rather than a flag on `notify-wrap.py`.** They compose in that
order and each does one thing: the toast needs only an exit code, this needs the
output. Keeping them apart is also what lets a task opt out of either -- a script that
already writes its own artifact (`run-tests.py`, `lint-all.py`) does not need this one,
and wrapping it anyway would produce a second, redundant file.

**The artifact is the point, per `.claude/rules/engineering.md`:** a task's failure has
to survive the terminal it scrolled past. `logs/<slug>.log` is overwritten per run and
**emptied on success**, so a stale report can never outlive the failure it describes.

**`--always` keeps the passing run too, and is for the unattended caller.** For a task
someone clicked, an empty file is unambiguous -- they watched it pass. Nobody watches a
scheduled job, and there "empty" covers three different states: it passed, it ran and
declined to do anything, or it has not run since the last time it passed. A prune that
skips every night because containers are up is *working as designed* and looks
identical to one that stopped being scheduled at all, which is the failure
`scripts/schedule_health.py` was written after. So a job on a schedule records its
passes as well; the file is still overwritten per run and still capped, so the cost is
one bounded file rather than a growing one.

**Every failure is also recorded on the harness-events ledger**, which is the half the
artifact cannot cover. The artifact is overwritten per run, so a job that fails every
night keeps only its most recent evidence and nothing anywhere says it has been failing
since Tuesday. That is not hypothetical: the nightly release failed three nights
running, each run erasing the previous one's reason, and the first anyone knew of it
was a person trying to cut a release by hand and finding a stale branch. A
`scheduled-job-failed` event is append-only and survives the next run, so the fix pass
sends it to the devkit session. **A clicked task records too**, its message saying it
was run by hand: it once did not, on the theory that the person had watched it fail,
but watching is not fixing. Failures share causes, so every routine one goes to the one
place they are aggregated, triaged and fixed together.

**And the event needs evidence still on disk when it is triaged**, which is the half
that was missing. The ledger entry survived; the file it pointed at did not. `Scheduled:
Devkit Release` failed on 2026-09-18 with exit 2, the next night's run passed and
overwrote `logs/scheduled-devkit-release.log` with its own success, and the sweep that
reached the event a day later could say only that it no longer reproduced -- no PR was
opened, so the failure was somewhere in `release.prepare`, and which branch of it is now
unknowable. So a failure is kept a second time, at `logs/<slug>.failed.log`, and
**that** is the path the event names -- a click retried until green empties the per-run
file just as a passing night does. It is written only on a failure, so a pass never
clears it and the next failure is the only thing that replaces it; the header says so,
because a file whose mtime is a week old is evidence for the event a week old, not for
this morning's run. One extra bounded file per task that has ever failed. **The event
names a third copy**, that failure's own under `logs/failed/` (`archive_artifact`): the
per-job one is the next failure's by the time a sweep reaches an older group of the job.

Colour survives the wrapping. A captured child is talking to a pipe rather than a
terminal, and most tools drop their colour the moment they notice -- so `FORCE_COLOR`
and `PY_COLORS` are set (when the caller has not) to keep the live view readable, and
the escape sequences are stripped back out of the file, which wants to be greppable.

Pure and stdlib-only; every decision is an importable function tested in
`scripts/hooks/tests/test_log_wrap.py`.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

LOGS_DIR = "logs"

# The ledger event an unattended failure leaves behind. Read by `harness_triage.py`,
# which lists it in `TRIAGE_EVENTS`.
FAILED_EVENT = "scheduled-job-failed"

# Suffix of the kept copy an unattended failure leaves beside its per-run artifact, and
# the path the ledger event names. See the docstring: the event outlives the artifact,
# and a triage sweep that reaches it a day later needs the reason, not the next run's.
FAILED_SUFFIX = ".failed"

# Each failure's own copy, under `logs/failed/`, which is what its ledger row names. The
# `.failed.log` above is per job, so on 2026-10-08 five groups of one job, each a
# different cause, were all handed the newest failure's copy as their own evidence
# (96d2638e, e711daa0). `ARCHIVE_KEEP` newest per job are kept, so the directory is
# bounded; a row whose copy aged out names a file that is not there, which the fix pass
# reports as absent rather than as someone else's.
ARCHIVE_DIR = "failed"
ARCHIVE_KEEP = 20
ARCHIVE_STAMP = "%Y%m%d-%H%M%S"

# The head and tail kept when a run is too long to store whole. Both ends matter and
# the middle rarely does: the head carries what was run and the first thing to go
# wrong, the tail carries the summary every test runner and linter prints last. A
# single head-only cap drops exactly the part an agent reads first.
HEAD_LINES = 80
TAIL_LINES = 240

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

# The line an unattended failure's `cause=` is read from, most telling first: the
# exception a traceback ends on, pytest's first failed test, a tool's `error:` line.
# Without it every failure of one job was one ledger group, so a new cause read as
# `RECURRED` and quoted an unrelated earlier fix as what not to repeat (950c4a96).
# A `logging` record at ERROR or above, then at WARNING, is read from its level on, so
# its timestamp is not part of the cause. A line pointing at an artifact ("FAILED --
# details in logs\scrape-run.json") names no cause, so it is never a match: as one, every
# failure of the social-scraper collector was one group, and a WinError 32 read as
# `RECURRED` over an unrelated fix (6ba2230e). It is still the fallback when nothing
# else was said, since it says where to look.
CAUSE_LINES = (
    (re.compile(r"^[A-Za-z_][\w.]*(?:Error|Exception|Interrupt|Exit)\b(?::.*)?$"), "last"),
    (re.compile(r"^(?:FAILED|ERROR)\s"), "first"),
    (re.compile(r"^(?:error|fatal)\b", re.I), "last"),
    (re.compile(r"\b(?:CRITICAL|ERROR)[\s:]+[\w.]+:"), "last"),
    (re.compile(r"\bWARNING[\s:]+[\w.]+:"), "last"),
)
POINTER = re.compile(r"\bdetails in\b", re.I)
CAUSE_WIDTH = 120
# The same line unfolded rides beside it as `said=`, outside the signature: folding read
# a transient `unable to access 'https://github.com/<owner>/<repo>.git/' ... error: 403`
# as `'https:/<repo>.git/' ... error: N`, and the status that said
# what happened cost a sweep a turn in the kept log to find (718f71c4).
SAID_WIDTH = 300
# What differs between two runs failing for one reason: where, which commit, how long.
ABSOLUTE_PATH = re.compile(r"(?:[A-Za-z]:)?(?:[\\/][^\s'\"\\/:]+)+[\\/]([^\s'\"\\/:]+)")
HEX_ID = re.compile(r"\b[0-9a-f]{7,40}\b")
NUMBER = re.compile(r"\d+(?:\.\d+)?")
# A status line plus a path -- the failure-artifact rule's own shape, `<tool>: FAILED --
# details in <file>` -- names where the cause is, not what it is: filed as the cause, every
# failure of the job was one group (6d11553e). Such a line is followed into the file.
DEFERS_TO = re.compile(r"\bdetails in\s+(\S+?)\.?$", re.I)
STATUS_ONLY = re.compile(r"^(?:[\w.-]+:)?\W*(?:FAILED|ERROR)?\W*(?:\(exit \d+\))?\W*$", re.I)
ARTIFACT_BYTES = 1 << 20
# Where a JSON artifact keeps what went wrong; the first such string, in document order.
ERROR_KEYS = re.compile(r"^(?:errors?|failures?|cause|reason|exception)$", re.I)

# Set on the child only when the caller has not, so `FORCE_COLOR=0` still wins.
# `PYTHONIOENCODING` matches what `stream` decodes: a Python child writing to a pipe
# otherwise encodes with the locale's code page, cp1252 on Windows, and dies with
# `UnicodeEncodeError` on the first character outside it. `PYTHONUNBUFFERED` keeps the
# merged output in the order the child wrote it: piped, its stdout is block-buffered and
# flushed at exit while stderr goes out at once, so a stderr warning landed *before* the
# stdout lines printed ahead of it, and the last line `cause_said` falls back to was a
# bystander's -- "social-scraper is already on devkit vN.N" for an owed release (583d8e80).
COLOR_ENV = {
    "FORCE_COLOR": "1",
    "PY_COLORS": "1",
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUNBUFFERED": "1",
}

# Set, the same way, on an unattended (`--always`) child only: nobody is there to answer
# a prompt, so a tool that would ask must fail instead, and a transfer that stalls must
# end. fb1f5465: every Worktree Reconcile run from 01:15 to 05:00 on 2026-10-08 was held
# to the scheduler's one-hour kill, writing nothing, until a fire was skipped as an
# overlap. A pass with no boxes spawns only `git fetch` and `gh`, unbounded, and git asks
# Git Credential Manager, which can wait on a sign-in window. Git aborts a transfer slower
# than `GIT_HTTP_LOW_SPEED_LIMIT` bytes/s for `GIT_HTTP_LOW_SPEED_TIME` seconds.
UNATTENDED_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GCM_INTERACTIVE": "never",
    "GH_PROMPT_DISABLED": "1",
    "GIT_HTTP_LOW_SPEED_LIMIT": "1000",
    "GIT_HTTP_LOW_SPEED_TIME": "120",
}

# Windows only. This wrapper has two kinds of caller and the flag is for the unattended
# one: a scheduled task runs it under `pythonw.exe`, which has no console, and Windows
# answers that by allocating a brand new console **window** for every console child. The
# wrapped command is a console program by definition, so without this the job it wraps
# announces itself on the desktop -- which is the opposite of unattended.
#
# It costs the clicked caller nothing. `stream` pipes stdout and stderr either way, so
# the child was never writing to a console of its own; what changes is only that Windows
# stops creating one. The window-less console is inherited by the child's own children,
# so a wrapped script does not have to know about any of this.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def split_argv(argv: list[str]) -> tuple[str, list[str]] | None:
    """`(title, command)` from `<title...> -- <command...>`, or None when malformed.

    Deliberately the same shape `notify-wrap.py` parses, because the two are written
    next to each other in a task and a second spelling would be one more thing to get
    right at the call site.
    """
    if "--" not in argv:
        return None
    separator = argv.index("--")
    title = " ".join(argv[:separator]).strip()
    command = argv[separator + 1 :]
    if not title or not command:
        return None
    return title, command


ALWAYS_FLAG = "--always"


def _now() -> str:
    """Local wall-clock, to the second. Local rather than UTC because the reader is
    comparing it against "did this run last night", not against another machine."""
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_argv(argv: list[str]) -> tuple[str, list[str], bool] | None:
    """`(title, command, always)`, or None when malformed.

    The flag is consumed **only from the front**, ahead of the title. Everything after
    the `--` belongs to the wrapped command, and a title is free-form text that could
    itself contain the word; scanning the whole argv would let a task called
    `Deploy --always` change this wrapper's behaviour by accident.
    """
    rest = argv[1:] if argv[:1] == [ALWAYS_FLAG] else argv
    parsed = split_argv(rest)
    if parsed is None:
        return None
    title, command = parsed
    return title, command, rest is not argv


def slug(title: str) -> str:
    """`"Devkit: Upgrade Projects"` -> `"devkit-upgrade-projects"`, the artifact's stem.

    Derived from the task label rather than from the command, so the file is named
    after the thing the operator clicked. Falls back to `task` for a title that is
    entirely punctuation -- an empty stem would silently write `logs/.log`.
    """
    cleaned = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return cleaned or "task"


def strip_ansi(text: str) -> str:
    """Drop the colour the child was asked to emit. The terminal wanted it; a log
    file being read by `grep`, or by an agent, does not."""
    return ANSI.sub("", text)


def _named(line: str) -> str:
    """`line` up to a "details in <file>" pointer, or `""` when nothing comes before the
    pointer but a status word (`STATUS_ONLY`).

    A status line that leads with its error -- social-scraper's `FAILED -- export posts:
    StoreError: ... -- details in <file>` -- names the cause; skipped whole as a pointer,
    it lost to the last logged warning, and ecf22e00 filed an export deferred for want of
    time, the consequence, in place of the store refusing every write."""
    pointer = POINTER.search(line)
    if pointer is None:
        return line
    head = line[: pointer.start()]
    return "" if STATUS_ONLY.match(head) else head.rstrip(" -;,(")


def cause_said(output: str) -> str:
    """The line of a failed run's output that names why, as the run said it: the
    exception, the first failed test, the `error:` line or a logged error or warning,
    else the last line; `""` for no output. A line's pointer to a file is no part of
    it (`_named`). Colour stripped, whitespace folded, bounded by `SAID_WIDTH`."""
    lines = [line.strip() for line in strip_ansi(output).splitlines() if line.strip()]
    found = lines[-1] if lines else ""
    for pattern, which in CAUSE_LINES:
        hits = [
            named[match.start() :]
            for line in lines
            if (named := _named(line)) and (match := pattern.search(named))
        ]
        if hits:
            found = hits[0] if which == "first" else hits[-1]
            break
    return " ".join(found.split())[:SAID_WIDTH]


def _first_error(node: object, under_error_key: bool = False) -> str:
    """The first string a JSON document keeps under an `ERROR_KEYS` key, else `""`."""
    if isinstance(node, str):
        return node if under_error_key else ""
    if isinstance(node, list):
        children = [(item, under_error_key) for item in node]
    elif isinstance(node, dict):
        children = [(v, under_error_key or bool(ERROR_KEYS.match(str(k)))) for k, v in node.items()]
    else:
        return ""
    for child, flagged in children:
        found = _first_error(child, flagged)
        if found.strip():
            return found
    return ""


def cause_source(output: str, root: Path) -> str:
    """The text a failure's cause is read from: `output`, unless the line `cause_said`
    picks only points at a file (`DEFERS_TO`, `STATUS_ONLY`) -- then that file, relative to
    `root`, read as JSON (`_first_error`) or as text. `output` whenever the file is not
    there or names nothing, since the status line still beats an empty cause."""
    said = cause_said(output)
    pointer = DEFERS_TO.search(said)
    if pointer is None or not STATUS_ONLY.match(said[: pointer.start()]):
        return output
    try:
        with (root / pointer.group(1).replace("\\", "/")).open(
            encoding="utf-8", errors="replace"
        ) as f:
            text = f.read(ARTIFACT_BYTES)
    except OSError:
        return output
    try:
        text = _first_error(json.loads(text))
    except ValueError:
        pass
    return text if cause_said(text) else output


def failure_cause(output: str) -> str:
    """`cause_said` with what varies run to run (paths, shas, numbers) folded out -- so
    the same cause on two nights is one ledger group and a different cause is another."""
    found = ABSOLUTE_PATH.sub(r"\1", cause_said(output))
    found = NUMBER.sub("N", HEX_ID.sub("<sha>", found))
    return " ".join(found.split())[:CAUSE_WIDTH]


def cap(text: str, head: int = HEAD_LINES, tail: int = TAIL_LINES) -> str:
    """Keep both ends of an over-long run, saying how much went.

    Pure, so the truncation is tested without running anything that produces 10,000
    lines of output.
    """
    lines = text.splitlines()
    if len(lines) <= head + tail:
        return text.strip("\n")
    dropped = len(lines) - head - tail
    kept = [
        *lines[:head],
        f"... ({dropped} lines omitted; {len(lines)} in total) ...",
        *lines[-tail:],
    ]
    return "\n".join(kept)


def artifact_body(
    title: str, command: list[str], code: int, output: str, always: bool = False, kept: bool = False
) -> str:
    """The file's full text -- **empty when the command succeeded**, unless `always`.

    Empty rather than absent: `logs/` keeps one file per task that has failed since it
    last passed, and a run that passes has to retract its own previous report.

    The header carries the command verbatim, because the reader of this file is
    usually not the person who clicked the task, and "re-run the task" is not something
    an agent can do. Under `always` it also carries the timestamp, which is the whole
    question for an unattended job: a passing report with no clock on it cannot be told
    apart from the same report written a fortnight ago.

    `kept` writes the `<slug>.failed.log` copy instead of the per-run one, and the only
    thing that changes is the `# fix:` line -- which has to stop saying "overwritten per
    run", because that is exactly what this copy is not. A reader who believes it is
    looking at this morning's run when the file is from Tuesday is worse off than one
    with no file at all.
    """
    if code == 0 and not always:
        return ""
    stamped = f"# when: {_now()}\n" if always or kept else ""
    freshness = (
        "kept until the NEXT failure -- a pass does not clear it, so read `# when:` above"
        if kept
        else "this file is overwritten per run"
    )
    verdict = (
        f"# fix: re-run `{' '.join(command)}` -- {freshness}\n"
        if code
        else "# result: passed -- kept because this task runs unattended\n"
    )
    return (
        "# source: devkit scripts/log-wrap.py\n"
        f"# task: {title}\n"
        f"{stamped}"
        f"# exit: {code}\n"
        f"{verdict}" + cap(strip_ansi(output)) + "\n"
    )


def write_artifact(root: Path, name: str, body: str, since: float = 0.0) -> Path | None:
    """Persist under `root/logs/<name>.log`. Best-effort: a `logs/` that cannot be
    written is not a reason to change what the task itself reported.

    `since` is when THIS run started, and it exists because a task can now run more than
    one instance at a time (`runOptions.instanceLimit` in the workspace file, added so
    two previews can be brought up at once). One artifact per task then has two writers,
    and the losing order is not the obvious one: an *empty* body is a passing run
    retracting its own earlier report, so a short pass that finishes after a long run
    already failed would delete a failure nobody has read yet.

    The retraction is therefore skipped when the file on disk was written *after* this
    run began -- that content cannot be the report this run is retracting. A failure
    body is never held back: the last failure to finish is the one worth keeping, and
    the terminal still holds the other one.
    """
    path = root / LOGS_DIR / f"{name}.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not body and since and path.is_file() and path.stat().st_mtime > since:
            return path
        path.write_text(body, encoding="utf-8")
    except OSError:
        return None
    return path


def archive_artifact(
    root: Path, name: str, body: str, now: _dt.datetime | None = None
) -> str | None:
    """Keep `body` as this failure's own copy, `logs/failed/<name>-<stamp>.log`, and prune
    `name`'s older copies past `ARCHIVE_KEEP`; the path relative to `root`, or None when it
    could not be written. Best-effort, like `write_artifact`: the copy is evidence, never a
    reason to change what the job reported."""
    folder = root / LOGS_DIR / ARCHIVE_DIR
    stem = f"{name}-{(now or _dt.datetime.now()).strftime(ARCHIVE_STAMP)}"
    mine = re.compile(rf"^{re.escape(name)}-\d{{8}}-\d{{6}}(?:-\d+)?\.log$")
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{stem}.log"
        n = 1
        while path.exists():
            n += 1
            path = folder / f"{stem}-{n}.log"
        path.write_text(body, encoding="utf-8")
        copies = sorted(
            (p for p in folder.iterdir() if mine.match(p.name)), key=lambda p: p.stat().st_mtime
        )
        for old in copies[:-ARCHIVE_KEEP]:
            old.unlink(missing_ok=True)
    except OSError:
        return None
    return f"{LOGS_DIR}/{ARCHIVE_DIR}/{path.name}"


def child_env(base: dict[str, str] | None = None, unattended: bool = False) -> dict[str, str]:
    """The child's environment, with colour, UTF-8 and unbuffered output forced unless
    the caller decided -- and, for an `unattended` child, no prompts (`UNATTENDED_ENV`)."""
    env = dict(os.environ if base is None else base)
    for key, value in (COLOR_ENV | (UNATTENDED_ENV if unattended else {})).items():
        env.setdefault(key, value)
    return env


def echo(line: str, out=None) -> None:
    """Mirror one captured line to the terminal, when there is one to mirror to.

    `sys.stdout` is **None**, not a discarded stream, under `pythonw.exe` -- which is
    how every unattended devkit job runs, precisely so no console window flashes up on
    each fire. `sys.stdout.write` then raises `AttributeError` on the first line the
    child prints, killing the wrapper whose entire job is to keep that output. The
    scheduled run would exit non-zero having written no artifact, which is the exact
    failure this file exists to prevent, in the one context where nobody is watching.

    A closed or detached stream (`ValueError`) is the same case arriving later, so it
    is swallowed here too: a task's output is never a reason to fail the task.
    """
    target = sys.stdout if out is None else out
    if target is None:
        return
    try:
        target.write(line)
        target.flush()
    except (ValueError, OSError):
        return


def stream(command: list[str], env: dict[str, str] | None = None) -> tuple[int, str]:
    """Run `command`, echoing output live while keeping a copy. `(exit code, output)`.
    `env` is the child's whole environment, `child_env()` when None.

    stderr is merged into stdout on purpose. Two pipes would need two readers to avoid
    deadlocking on a full buffer, and the artifact wants the interleaving the operator
    saw far more than it wants to know which stream a line came from.

    Read with `iter(readline, "")` rather than `for line in pipe`, because iterating a
    pipe object buffers ahead -- which is invisible in a test and turns a long task's
    terminal into a stall followed by a flood.
    """
    env = child_env() if env is None else env
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            creationflags=NO_WINDOW,
        )
    except FileNotFoundError:
        # Windows cannot CreateProcess a batch launcher (npm, npx, vite) directly --
        # they are .cmd shims. Same fallback `notify-wrap.py` carries, for the same
        # reason and with the same trust assumption.
        process = subprocess.Popen(  # noqa: S602 - see above; tasks.json is trusted
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            shell=True,
            creationflags=NO_WINDOW,
        )
    captured: list[str] = []
    assert process.stdout is not None  # noqa: S101 - PIPE above guarantees it
    for line in iter(process.stdout.readline, ""):
        captured.append(line)
        echo(line)
    process.stdout.close()
    return process.wait(), "".join(captured)


def main(argv: list[str] | None = None, run=None, root: Path | None = None) -> int:
    """`run(command)` is `stream` when None, in the environment `--always` asks for."""
    parsed = parse_argv(sys.argv[1:] if argv is None else argv)
    if parsed is None:
        print(
            'log-wrap: usage: python scripts/log-wrap.py [--always] "Task Name" '
            "-- <command> [args...]",
            file=sys.stderr,
        )
        return 2
    title, command, always = parsed

    started = time.time()
    if run is None:
        code, output = stream(command, child_env(unattended=always))
    else:
        code, output = run(command)

    name = slug(title)
    path = write_artifact(
        root or Path.cwd(),
        name,
        artifact_body(title, command, code, output, always),
        since=started,
    )
    if code != 0 and path is not None:
        # One line, and only the path -- the failure text is already above, and
        # repeating it here is what buries the pointer to the file that keeps it.
        print(
            f"\nlog-wrap: FAILED (exit {code}) -- details in {LOGS_DIR}/{name}.log", file=sys.stderr
        )
    if code != 0:
        # The kept copy first, so the event never names a path that is not there yet.
        # `since` is not passed: this file is only ever written by a failure, so it has
        # no retraction to hold back and the concurrency case `write_artifact` guards
        # cannot arise.
        body = artifact_body(title, command, code, output, always, kept=True)
        kept = write_artifact(root or Path.cwd(), name + FAILED_SUFFIX, body)
        archived = archive_artifact(root or Path.cwd(), name, body)
        artifact = archived or artifact_ref(name, kept=kept is not None)
        source = cause_source(output, root or Path.cwd())
        failure = Failure(failure_message(title, always), command, code, artifact, source, started)
        record_failure(failure, root)
    return code


def failure_message(title: str, unattended: bool) -> str:
    """The ledger row's message: which task, and whether a schedule or a person ran it --
    two groups, since a job failing nightly and a click failing once rarely share why."""
    return f"{'unattended ' if unattended else ''}task {title!r} failed"


def artifact_ref(name: str, kept: bool = True) -> str:
    """The artifact a failure's ledger row names: the **kept** copy, not the per-run one,
    because the two have different lifetimes and only one of them outlives the event: a
    sweep reaching this row tomorrow finds the reason there and finds the next run's
    output in the other. `kept=False` falls back to the per-run path -- a `logs/` that
    could not be written is not a reason to file no event, and a pointer to the ordinary
    artifact is still better than none."""
    return f"{LOGS_DIR}/{name}{FAILED_SUFFIX if kept else ''}.log"


@dataclass(frozen=True)
class Failure:
    """One failed run, as its ledger row files it.

    `started` (epoch seconds) is when the run began, filed as `started=` beside the row's
    own stamp, which is when it ended: the code a job ran is the code on disk when it
    started, so a run that began before a fix merged and failed after it is the defect
    the fix was waiting on, not a recurrence (`fix_verify.covered`). 1fad5675: a 04:00Z
    scrape failed at 04:13Z on code from before social-scraper #68 merged at 04:04Z.
    """

    message: str
    command: list[str]
    code: int
    artifact: str
    output: str = ""
    started: float | None = None


def record_failure(failure: Failure, root: Path | None = None) -> None:
    """Leave `failure` on the harness-events ledger under its `message`
    (`failure_message`), naming its `artifact` (`artifact_ref`) as the file that holds
    its `output`.

    Best-effort twice over. `harness_events` swallows its own errors by contract, and
    the import is guarded because this module is vendored into projects that may hold a
    copy of `log-wrap.py` newer than their `scripts/hooks/` tier -- a wrapper that
    crashed on a missing ledger would take the job's exit code with it, turning a
    reporting gap into a broken job.

    The message is deliberately stable across runs: `Item.signature` groups by its first
    `SIGNATURE_WIDTH` characters, so a job failing nightly reads as one defect recurring
    rather than as a new one every morning. The exit code and artifact path ride in
    their own fields, where they do not disturb that grouping. `cause` (`failure_cause`)
    does, deliberately: `harness_triage.Item.signature` keys on it, so one cause failing
    nightly is one group and a new cause is a new one rather than a false recurrence.
    `said` (`cause_said`) is that line unfolded, filed only where folding changed it.
    """
    cause, said = failure_cause(failure.output), cause_said(failure.output)
    started = failure.started
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))
        import harness_events
    except ImportError:
        # Specifically the module not being there. Anything else -- a `harness_events`
        # that imports but is broken -- is a defect worth a traceback rather than a
        # scheduled job that reports nothing and looks fine.
        return
    harness_events.record(
        FAILED_EVENT,
        (
            ("project", harness_events.project_name(root or Path.cwd())),
            ("command", " ".join(failure.command)),
            ("artifact", failure.artifact),
            ("exit", failure.code),
            ("message", failure.message),
            ("cause", cause or "-"),
            *((("said", said),) if said and said != cause else ()),
            *((("started", started_stamp(started)),) if started is not None else ()),
        ),
        root=root,
    )


def started_stamp(started: float) -> str:
    """`started` as the ledger spells a moment: UTC ISO 8601 to the second."""
    return _dt.datetime.fromtimestamp(started, _dt.UTC).isoformat(timespec="seconds")


if __name__ == "__main__":
    sys.exit(main())
