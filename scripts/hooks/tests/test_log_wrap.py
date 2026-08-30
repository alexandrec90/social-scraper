"""Tests for scripts/log-wrap.py (the task failure-artifact wrapper).

Vendored, so nothing here may name a value that varies per project: every path is
built under `tmp_path`, and the wrapper itself resolves `logs/` from the cwd rather
than from `__file__` for the same reason.

The interesting half is what the artifact does when a run *passes*. A wrapper that
only writes on failure leaves the previous failure sitting there, and the next agent
reads it as current -- which is the specific failure `.claude/rules/engineering.md`
requires an empty-on-success artifact to prevent.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest
from conftest import REPO_ROOT, load_module

lw = load_module("scripts/log-wrap.py")


# --- argv ---------------------------------------------------------------------


def test_the_title_and_command_split_on_the_separator():
    assert lw.split_argv(["Ship: Sweep", "--", "python", "scripts/sweep.py"]) == (
        "Ship: Sweep",
        ["python", "scripts/sweep.py"],
    )


def test_an_unquoted_title_is_rejoined():
    """`notify-wrap.py` accepts a bare multi-word title; parsing it differently here
    would be one more thing to get right at a call site that names both."""
    title, command = lw.split_argv(["Ship:", "Sweep", "Workspace", "--", "python", "x.py"])
    assert title == "Ship: Sweep Workspace"
    assert command == ["python", "x.py"]


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["Ship: Sweep"],  # no separator
        ["--", "python", "x.py"],  # no title
        ["Ship: Sweep", "--"],  # no command
    ],
)
def test_a_malformed_invocation_is_refused_rather_than_guessed(argv):
    assert lw.split_argv(argv) is None


def test_a_malformed_invocation_exits_two_without_running_anything():
    def never(_command):
        raise AssertionError("ran a command it could not parse")

    assert lw.main(["Ship: Sweep"], run=never) == 2


# --- naming -------------------------------------------------------------------


def test_the_artifact_is_named_after_the_task():
    assert lw.slug("Ship: Sweep Workspace") == "ship-sweep-workspace"
    assert lw.slug("Test: Run pytest --changed") == "test-run-pytest-changed"


def test_a_title_with_nothing_nameable_still_gets_a_file():
    """An empty stem writes `logs/.log`, which is hidden on every platform that
    matters and would be reported as "the wrapper wrote nothing"."""
    assert lw.slug(":::") == "task"


# --- the body -----------------------------------------------------------------


def test_a_passing_run_writes_nothing_at_all():
    assert lw.artifact_body("Ship: Sweep", ["python", "x.py"], 0, "lots of output") == ""


def test_a_failing_run_carries_the_command_and_the_output():
    body = lw.artifact_body("Ship: Sweep", ["python", "scripts/sweep.py", "--check"], 1, "boom")
    assert "# task: Ship: Sweep" in body
    assert "# exit: 1" in body
    assert "python scripts/sweep.py --check" in body
    assert "boom" in body


def test_colour_is_stripped_from_the_file_but_not_from_the_run():
    """The child is told to keep its colour so the live terminal stays readable; the
    file is read by grep and by agents, and escape sequences are noise there."""
    body = lw.artifact_body("T", ["x"], 1, "\x1b[31mFAILED\x1b[0m tests/test_a.py")
    assert "FAILED tests/test_a.py" in body
    assert "\x1b[" not in body


def test_the_child_keeps_colour_unless_the_caller_decided():
    assert lw.child_env({})["FORCE_COLOR"] == "1"
    assert lw.child_env({"FORCE_COLOR": "0"})["FORCE_COLOR"] == "0"


# --- capping ------------------------------------------------------------------


def test_a_short_run_is_stored_whole():
    text = "\n".join(f"line {n}" for n in range(20))
    assert lw.cap(text) == text


def test_an_over_long_run_keeps_both_ends():
    """A head-only cap drops the summary every runner prints last -- the part an
    agent reads first."""
    text = "\n".join(f"line {n}" for n in range(5000))
    capped = lw.cap(text, head=3, tail=2)
    assert capped.splitlines()[:3] == ["line 0", "line 1", "line 2"]
    assert capped.splitlines()[-2:] == ["line 4998", "line 4999"]
    assert "4995 lines omitted" in capped


# --- writing ------------------------------------------------------------------


def test_the_artifact_lands_under_logs_in_the_cwd(tmp_path):
    path = lw.write_artifact(tmp_path, "ship-sweep", "boom\n")
    assert path == tmp_path / "logs" / "ship-sweep.log"
    assert path.read_text(encoding="utf-8") == "boom\n"


def test_a_pass_clears_what_the_last_failure_left(tmp_path):
    """The whole reason the artifact is written on success too.

    `_aged` because the retraction is now decided by comparing the file's mtime against
    this run's start, and a file written microseconds earlier is not reliably *older*:
    Windows dates a file from the tick-based system clock, which drifts either side of
    the precise one `time.time()` reads. An hour makes the intended case -- a failure
    left by an earlier run -- the one the test actually sets up.
    """
    _aged(lw.write_artifact(tmp_path, "ship-sweep", "an old failure\n"), 3600)
    assert lw.main(["Ship: Sweep", "--", "x"], run=lambda _c: (0, "fine"), root=tmp_path) == 0
    assert (tmp_path / "logs" / "ship-sweep.log").read_text(encoding="utf-8") == ""


# --- two instances of one task, one artifact ----------------------------------
#
# A task may now run several instances at once (`runOptions.instanceLimit`), so the
# single artifact has two writers and the losing order is not the obvious one: an empty
# body is a *retraction*, so a short pass finishing after a long run already failed
# would delete a failure nobody has read.


def _aged(path, seconds):
    """Backdate `path`'s mtime, so "written before this run started" is not a race."""
    os.utime(path, (path.stat().st_atime, path.stat().st_mtime - seconds))


def test_a_pass_does_not_retract_a_failure_written_after_it_started(tmp_path):
    started = time.time() - 60
    path = lw.write_artifact(tmp_path, "preview", "the other instance failed\n")
    assert lw.write_artifact(tmp_path, "preview", "", since=started) == path
    assert path.read_text(encoding="utf-8") == "the other instance failed\n"


def test_a_pass_still_clears_a_report_older_than_this_run(tmp_path):
    """The single-instance behaviour is the common case and must not regress: a task
    that passes retracts the failure it left last time."""
    path = lw.write_artifact(tmp_path, "preview", "yesterday's failure\n")
    _aged(path, 3600)
    assert lw.write_artifact(tmp_path, "preview", "", since=time.time()) == path
    assert path.read_text(encoding="utf-8") == ""


def test_a_failure_is_never_held_back_by_a_newer_file(tmp_path):
    """Only the retraction is skipped. The last failure to finish is the one worth
    keeping, and the other instance's terminal still holds its own."""
    path = lw.write_artifact(tmp_path, "preview", "the other instance failed\n")
    assert lw.write_artifact(tmp_path, "preview", "this one failed too\n", since=time.time() - 60)
    assert path.read_text(encoding="utf-8") == "this one failed too\n"


def test_without_a_start_time_the_artifact_is_always_overwritten(tmp_path):
    """`since` defaults to 0 -- an unknown start time cannot rule anything out, so the
    caller that does not pass one gets exactly the old behaviour."""
    path = lw.write_artifact(tmp_path, "preview", "a failure\n")
    assert lw.write_artifact(tmp_path, "preview", "") == path
    assert path.read_text(encoding="utf-8") == ""


def test_a_wrapped_pass_dates_itself_from_before_the_command_ran(tmp_path):
    """`main` has to take the timestamp *before* the command, not after: a run that
    fails while this one is working is the case being protected, and a stamp taken at
    the end is after it."""
    path = tmp_path / "logs" / "t.log"

    def run(_command):
        path.parent.mkdir(parents=True, exist_ok=True)
        # A file's mtime is taken from the system clock, which on Windows advances on a
        # ~15ms tick, while `time.time()` does not -- so a file written in the same tick
        # as the stamp can be dated *before* it. Spending a few ticks here is what makes
        # "written after main started" true of the timestamps and not just of the order.
        time.sleep(0.05)
        path.write_text("a concurrent failure\n", encoding="utf-8")
        return 0, "fine"

    assert lw.main(["T", "--", "x"], run=run, root=tmp_path) == 0
    assert path.read_text(encoding="utf-8") == "a concurrent failure\n"


def test_an_unwritable_logs_directory_does_not_change_the_exit_code(tmp_path):
    """The artifact reports on the task; it never overrules it."""
    (tmp_path / "logs").write_text("not a directory", encoding="utf-8")
    assert lw.write_artifact(tmp_path, "x", "body") is None
    assert lw.main(["T", "--", "x"], run=lambda _c: (3, "boom"), root=tmp_path) == 3


# --- end to end ---------------------------------------------------------------


def test_the_wrapped_commands_exit_code_is_the_wrappers(tmp_path):
    for code in (0, 1, 2, 7):
        assert lw.main(["T", "--", "x"], run=lambda _c, c=code: (c, "out"), root=tmp_path) == code


def test_a_real_child_is_streamed_and_captured(tmp_path, capsys):
    """`stream` is the one part that cannot be proven with a stub: it has to actually
    read a pipe, keep a copy, and report the child's code."""
    script = "import sys; print('to stdout'); print('to stderr', file=sys.stderr); sys.exit(4)"
    code, output = lw.stream([sys.executable, "-c", script])
    assert code == 4
    assert "to stdout" in output
    assert "to stderr" in output  # merged on purpose -- one pipe, no deadlock
    assert "to stdout" in capsys.readouterr().out  # and it reached the terminal


def test_a_real_failure_lands_in_the_file_the_terminal_points_at(tmp_path, capsys):
    script = "import sys; print('boom'); sys.exit(1)"
    code = lw.main(["Ship: Sweep", "--", sys.executable, "-c", script], root=tmp_path)
    assert code == 1
    written = (tmp_path / "logs" / "ship-sweep.log").read_text(encoding="utf-8")
    assert "boom" in written
    assert "logs/ship-sweep.log" in capsys.readouterr().err


# --- the unattended caller ----------------------------------------------------
#
# Everything above assumes someone is watching the terminal. A scheduled job has no
# terminal at all: on Windows it runs under `pythonw.exe`, where `sys.stdout` is None
# rather than a stream that discards. These are the two things that breaks.


def test_the_leading_always_flag_is_consumed_not_treated_as_the_title():
    assert lw.parse_argv(["--always", "Nightly", "--", "x"]) == ("Nightly", ["x"], True)
    assert lw.parse_argv(["Nightly", "--", "x"]) == ("Nightly", ["x"], False)


def test_always_is_only_read_from_the_front():
    """A title is free-form text and the wrapped command has its own flags; scanning
    the whole argv would let either turn this mode on by accident."""
    title, command, always = lw.parse_argv(["Deploy --always", "--", "x", "--always"])
    assert (title, always) == ("Deploy --always", False)
    assert command == ["x", "--always"]


def test_a_malformed_argv_is_still_rejected_with_the_flag():
    assert lw.parse_argv(["--always"]) is None
    assert lw.parse_argv(["--always", "--", "x"]) is None  # no title


def test_a_passing_unattended_run_records_that_it_passed(tmp_path):
    """For a job nobody watches, an empty file cannot be told apart from a job that
    stopped being scheduled -- which is the failure this whole tier exists to catch."""
    assert (
        lw.main(["--always", "Nightly", "--", "x"], run=lambda _c: (0, "skipped"), root=tmp_path)
        == 0
    )
    written = (tmp_path / "logs" / "nightly.log").read_text(encoding="utf-8")
    assert "# exit: 0" in written
    assert "skipped" in written
    assert "# when:" in written  # a passing report with no clock on it proves nothing


def test_an_unattended_failure_still_reads_like_every_other_failure(tmp_path):
    body = lw.artifact_body("Nightly", ["x"], 1, "boom", always=True)
    assert "# exit: 1" in body
    assert "# fix: re-run" in body
    assert "boom" in body


def test_a_watched_run_is_unchanged_by_the_new_parameter(tmp_path):
    assert lw.artifact_body("T", ["x"], 0, "out") == ""


def test_echo_survives_an_interpreter_with_no_stdout():
    """`pythonw.exe` sets `sys.stdout` to None. Writing to it raised AttributeError on
    the child's first line of output, killing the wrapper mid-run -- so the scheduled
    job exited non-zero and wrote no artifact, which is the one context where nobody
    would see either."""
    lw.echo("a line", out=None)  # must not raise


def test_echo_survives_a_stream_closed_underneath_it():
    class Closed:
        def write(self, _text):
            raise ValueError("I/O operation on closed file")

        def flush(self):
            raise ValueError("I/O operation on closed file")

    lw.echo("a line", out=Closed())  # must not raise


def test_a_child_is_captured_even_with_no_terminal_to_mirror_to(tmp_path, monkeypatch):
    """The end-to-end shape of the above: output still reaches the artifact when there
    is nowhere to echo it."""
    monkeypatch.setattr(sys, "stdout", None)
    code, output = lw.stream([sys.executable, "-c", "print('captured anyway')"])
    assert code == 0
    assert "captured anyway" in output


def test_the_script_runs_as_a_script(tmp_path):
    """It is invoked by `python scripts/log-wrap.py ...` from a task, never imported,
    so the `__main__` path has to work with no package context."""
    script = REPO_ROOT / "scripts" / "log-wrap.py"
    result = subprocess.run(
        [sys.executable, str(script), "T", "--", sys.executable, "-c", "print('hi')"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "hi" in result.stdout
    assert (tmp_path / "logs" / "t.log").read_text(encoding="utf-8") == ""
