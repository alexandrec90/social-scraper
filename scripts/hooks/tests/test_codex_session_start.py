"""Tests for the cross-platform Codex SessionStart bridge."""

import subprocess

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
