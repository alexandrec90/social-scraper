"""Tests for report-harness-defect.py -- the agent-report ledger CLI.

Project-agnostic: the ledger destination is a monkeypatched $DEVKIT_DIR, and the
project/version fields are asserted present rather than against devkit's values.
"""

from conftest import load_module

report = load_module("scripts/hooks/report-harness-defect.py")


def read_ledger(base):
    return (base / "logs" / "harness-events.log").read_text(encoding="utf-8")


class TestMain:
    def test_records_agent_report(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("DEVKIT_DIR", str(tmp_path))
        rc = report.main(["--message", "the gate blocked a plain grep", "--command", "grep -r x"])
        assert rc == 0
        line = read_ledger(tmp_path).splitlines()[0]
        assert "\tevent=agent-report\t" in line
        assert "\tcommand=grep -r x\t" in line
        assert line.endswith("\tmessage=the gate blocked a plain grep")
        assert "\tproject=" in line
        assert "\tversion=" in line
        assert "recorded to" in capsys.readouterr().out

    def test_command_is_optional(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEVKIT_DIR", str(tmp_path))
        assert report.main(["--message", "m"]) == 0
        assert "\tcommand=-\t" in read_ledger(tmp_path)

    def test_no_devkit_dir_still_exits_zero(self, tmp_path, monkeypatch, capsys):
        """Pinned to a *consuming* copy, which is the only one an unset var leaves with
        nowhere to file: `harness_events.ledger_path` falls back to the checkout itself
        when the copy is devkit. Unpinned, this would file a test report on the real
        ledger and assert the opposite of what devkit does."""
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "someproject"\n', encoding="utf-8"
        )
        monkeypatch.delenv("DEVKIT_DIR", raising=False)
        monkeypatch.setattr(report.harness_events, "REPO_ROOT", tmp_path)
        assert report.main(["--message", "m"]) == 0
        assert "no central ledger" in capsys.readouterr().out
