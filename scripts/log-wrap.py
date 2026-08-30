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

Colour survives the wrapping. A captured child is talking to a pipe rather than a
terminal, and most tools drop their colour the moment they notice -- so `FORCE_COLOR`
and `PY_COLORS` are set (when the caller has not) to keep the live view readable, and
the escape sequences are stripped back out of the file, which wants to be greppable.

Pure and stdlib-only; every decision is an importable function tested in
`scripts/hooks/tests/test_log_wrap.py`.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
import subprocess
import sys
import time
from pathlib import Path

LOGS_DIR = "logs"

# The head and tail kept when a run is too long to store whole. Both ends matter and
# the middle rarely does: the head carries what was run and the first thing to go
# wrong, the tail carries the summary every test runner and linter prints last. A
# single head-only cap drops exactly the part an agent reads first.
HEAD_LINES = 80
TAIL_LINES = 240

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

# Set on the child only when the caller has not, so `FORCE_COLOR=0` still wins.
COLOR_ENV = {"FORCE_COLOR": "1", "PY_COLORS": "1"}

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
    title: str, command: list[str], code: int, output: str, always: bool = False
) -> str:
    """The file's full text -- **empty when the command succeeded**, unless `always`.

    Empty rather than absent: `logs/` keeps one file per task that has failed since it
    last passed, and a run that passes has to retract its own previous report.

    The header carries the command verbatim, because the reader of this file is
    usually not the person who clicked the task, and "re-run the task" is not something
    an agent can do. Under `always` it also carries the timestamp, which is the whole
    question for an unattended job: a passing report with no clock on it cannot be told
    apart from the same report written a fortnight ago.
    """
    if code == 0 and not always:
        return ""
    stamped = f"# when: {_now()}\n" if always else ""
    verdict = (
        f"# fix: re-run `{' '.join(command)}` -- this file is overwritten per run\n"
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


def child_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """The child's environment, with colour forced unless the caller decided."""
    env = dict(os.environ if base is None else base)
    for key, value in COLOR_ENV.items():
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


def stream(command: list[str]) -> tuple[int, str]:
    """Run `command`, echoing output live while keeping a copy. `(exit code, output)`.

    stderr is merged into stdout on purpose. Two pipes would need two readers to avoid
    deadlocking on a full buffer, and the artifact wants the interleaving the operator
    saw far more than it wants to know which stream a line came from.

    Read with `iter(readline, "")` rather than `for line in pipe`, because iterating a
    pipe object buffers ahead -- which is invisible in a test and turns a long task's
    terminal into a stall followed by a flood.
    """
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=child_env(),
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
            env=child_env(),
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


def main(argv: list[str] | None = None, run=stream, root: Path | None = None) -> int:
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
    return code


if __name__ == "__main__":
    sys.exit(main())
