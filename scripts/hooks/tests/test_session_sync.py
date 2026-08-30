"""Unit tests for scripts/hooks/session-sync.py (pure helpers)."""

from conftest import load_module

sync = load_module("scripts/hooks/session-sync.py")


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
