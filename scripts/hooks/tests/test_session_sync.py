"""Unit tests for scripts/hooks/session-sync.py (pure helpers)."""

import subprocess

from conftest import load_module

sync = load_module("scripts/hooks/session-sync.py")


def fake_git(answers: dict[str, tuple[int, str]], calls: list[tuple[str, ...]]):
    """`run_git`, answering by subcommand; anything unlisted succeeds with no output."""

    def run_git(*args: str, capture_output: bool = False) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        code, out = answers.get(args[0], (0, ""))
        return subprocess.CompletedProcess(["git", *args], code, out, "")

    return run_git


class TestMain:
    def test_a_dirty_tree_is_refused_before_anything_is_fetched(self, monkeypatch):
        calls: list[tuple[str, ...]] = []
        monkeypatch.setattr(sync, "run_git", fake_git({"status": (0, " M a.py\n")}, calls))
        assert sync.main() == 1
        assert [c[0] for c in calls] == ["status"]

    def test_a_detached_head_is_refused(self, monkeypatch):
        calls: list[tuple[str, ...]] = []
        monkeypatch.setattr(sync, "run_git", fake_git({"branch": (0, "\n")}, calls))
        assert sync.main() == 1
        assert "fetch" not in [c[0] for c in calls]

    def test_a_clean_branch_is_rebased_onto_the_remote_default(self, monkeypatch):
        calls: list[tuple[str, ...]] = []
        answers = {
            "branch": (0, "agent/x\n"),
            "symbolic-ref": (0, "refs/remotes/origin/main\n"),
            "config": (1, ""),
            "rebase": (7, ""),
        }
        monkeypatch.setattr(sync, "run_git", fake_git(answers, calls))
        assert sync.main() == 7, "the rebase's own exit code"
        assert calls[-1] == ("rebase", "--no-autostash", "origin/main")


class TestGpgsignFlag:
    def test_true_adds_flag(self):
        assert sync.gpgsign_flag("true") == ["--gpg-sign"]

    def test_true_is_case_and_whitespace_insensitive(self):
        assert sync.gpgsign_flag(" TRUE\n") == ["--gpg-sign"]

    def test_false_adds_nothing(self):
        assert sync.gpgsign_flag("false") == []

    def test_empty_adds_nothing(self):
        # commit.gpgsign unset (git config --get returned nothing): no signing.
        assert sync.gpgsign_flag("") == []


class TestRebaseArgv:
    def test_signed_when_configured(self):
        assert sync.rebase_argv("true") == [
            "rebase",
            "--no-autostash",
            "--gpg-sign",
            "origin/master",
        ]

    def test_unsigned_when_not_configured(self):
        assert sync.rebase_argv("false") == ["rebase", "--no-autostash", "origin/master"]

    def test_respects_custom_upstream(self):
        assert sync.rebase_argv("", upstream="origin/main") == [
            "rebase",
            "--no-autostash",
            "origin/main",
        ]
