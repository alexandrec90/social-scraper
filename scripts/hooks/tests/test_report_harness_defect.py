"""Tests for report-harness-defect.py -- the agent-report ledger CLI.

Project-agnostic: the ledger destination is a monkeypatched $DEVKIT_DIR, and the
project/version fields are asserted present rather than against devkit's values.
"""

from conftest import load_module

report = load_module("scripts/hooks/report-harness-defect.py")


def read_ledger(base):
    """Every shard's text -- the ledger is one file per machine."""
    return "".join(
        path.read_text(encoding="utf-8")
        for path in sorted((base / "logs").glob("harness-events*.log"))
        if path.is_file()
    )


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

    def test_records_the_directory_it_ran_from(self, tmp_path, monkeypatch):
        """The 2026-09-26 supervised run filed five reports citing
        `logs/fix-pass-supervise/iteration-1/...` from a worktree the row never named;
        each cited line cost the next sweep a search. `cwd=` anchors any relative path
        in the message or the command."""
        tree = tmp_path / "tree"
        tree.mkdir()
        monkeypatch.setenv("DEVKIT_DIR", str(tmp_path))
        monkeypatch.chdir(tree)
        report.main(["--message", "m", "--command", "logs/x.txt lines 1-2"])
        assert f"\tcwd={tree.resolve()}\t" in read_ledger(tmp_path)

    def test_evidence_is_resolved_to_absolute_paths(self, tmp_path, monkeypatch):
        tree = tmp_path / "tree"
        tree.mkdir()
        monkeypatch.setenv("DEVKIT_DIR", str(tmp_path))
        monkeypatch.chdir(tree)
        report.main(
            [
                "--message",
                "m",
                "--evidence",
                "logs/run/a.txt#L79-98",
                "--evidence",
                str(tmp_path / "b.txt"),
            ]
        )
        expected = f"{tree.resolve() / 'logs/run/a.txt'}#L79-98; {tmp_path / 'b.txt'}"
        assert f"\tevidence={expected}\t" in read_ledger(tmp_path)

    def test_evidence_is_optional(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEVKIT_DIR", str(tmp_path))
        report.main(["--message", "m"])
        assert "\tevidence=-\t" in read_ledger(tmp_path)

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


class TestAbsoluteEvidence:
    def test_relative_path_joins_the_base(self, tmp_path):
        assert report.absolute_evidence("a/b.txt", tmp_path) == str(tmp_path / "a" / "b.txt")

    def test_anchor_survives(self, tmp_path):
        got = report.absolute_evidence("t.jsonl#L12", tmp_path)
        assert got == f"{tmp_path / 't.jsonl'}#L12"

    def test_absolute_path_is_kept(self, tmp_path):
        target = str(tmp_path / "x.txt")
        assert report.absolute_evidence(target, tmp_path / "elsewhere") == target

    def test_blank_is_blank(self, tmp_path):
        assert report.absolute_evidence("  ", tmp_path) == ""
