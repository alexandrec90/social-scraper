"""Tests for the cross-platform Codex SessionStart bridge."""

import subprocess
import sys
from pathlib import Path

from conftest import load_module

hook = load_module("scripts/hooks/codex-session-start.py")


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_branch_name_returns_empty_on_git_failure(monkeypatch):
    monkeypatch.setattr(hook, "run", lambda *args, **kwargs: _completed(1))
    assert hook.branch_name() == ""


def test_windows_master_does_not_run_sync(monkeypatch):
    calls = []
    monkeypatch.setattr(hook, "branch_name", lambda: "master")
    monkeypatch.setattr(hook, "run", lambda *args, **kwargs: calls.append(args) or _completed())

    assert hook.run_windows_local() == 0
    assert calls == []


def test_windows_dirty_branch_reports_skip_without_failing(monkeypatch, capsys):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[:2] == ["git", "branch"]:
            return _completed(0, stdout="feature/hooks\n")
        if args[:3] == ["git", "rev-parse", "--git-path"]:
            return _completed(0, stdout=".git/not-in-progress\n")
        return _completed(1)

    monkeypatch.setattr(hook, "run", fake_run)

    assert hook.run_windows_local() == 0
    assert "Branch sync skipped" in capsys.readouterr().out
    assert not any(args[:3] == ["git", "rebase", "--abort"] for args in calls)


def test_posix_main_delegates_to_shared_bash_script(monkeypatch):
    calls = []
    monkeypatch.setattr(hook.os, "name", "posix")
    monkeypatch.setattr(
        hook,
        "run",
        lambda args, **kwargs: calls.append(args) or _completed(7),
    )

    assert hook.main() == 7
    assert calls == [["bash", str(hook.SESSION_START_SH)]]


def test_run_spawns_from_the_repo_root_and_never_raises_on_a_bad_byte():
    """The one seam every other function here reaches git through, and the reason it is
    written out rather than calling `subprocess.run` inline.

    Two properties, both load-bearing and neither visible from the callers, which is why
    this test names `run` directly: it spawns with `cwd` at the repository root, so a
    SessionStart hook invoked from anywhere answers for the right repo; and it decodes
    as UTF-8 **with replacement**, so a byte the platform codec cannot read -- in a
    branch name, which is the string every caller here asks for -- yields a replacement
    character instead of a `UnicodeDecodeError`. A SessionStart hook that raises starts
    no session.

    It was uncovered until the `module_pattern` path spelling was narrowed: an unrelated
    `subprocess.run(` in a file that merely mentioned this script's path read as
    coverage of this `run`.
    """
    # Written with `bytes([255])` rather than an escape: this file is read back by the
    # very gate that made the test necessary, and a literal `\xff` in it is a byte no
    # encoding declaration covers.
    emit = "import os, sys; sys.stdout.buffer.write(os.getcwd().encode() + bytes([10, 255]))"
    result = hook.run([sys.executable, "-c", emit], capture_output=True)

    assert result.returncode == 0
    reported, _, tail = result.stdout.partition("\n")
    assert Path(reported) == hook.REPO_ROOT, "the child must run from the repository root"
    assert tail == "\ufffd", "an undecodable byte must be replaced, never raised"


def test_run_returns_the_childs_failure_rather_than_raising():
    """`check=False` is deliberate: every caller reads `returncode` and answers for
    itself, and a raising helper would take the session down for a git command that is
    allowed to fail (`branch_name` on a detached HEAD)."""
    result = hook.run([sys.executable, "-c", "raise SystemExit(3)"])
    assert result.returncode == 3
