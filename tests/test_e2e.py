"""Tests for `scripts/e2e.py`, the local end-to-end tier's runner.

No pytest child is spawned except where `main` is the subject, and there the child is a
fake: the junit report it would have written is written by the test.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "e2e.py"


def load_e2e() -> ModuleType:
    spec = importlib.util.spec_from_file_location("e2e", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["e2e"] = module
    spec.loader.exec_module(module)
    return module


e2e = load_e2e()

JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="4">
<testcase classname="tests.test_local_scrape" name="test_passes" time="1.0" />
<testcase classname="tests.test_local_scrape" name="test_fails" time="1.0">
<failure message="assert 0 &gt; 0">trace</failure></testcase>
<testcase classname="tests.test_local_scrape.TestGroup" name="test_case[a b]" time="0.1">
<error message="fixture broke">trace</error></testcase>
<testcase classname="tests.test_local_scrape" name="test_skipped" time="0.0">
<skipped type="pytest.skip" message="no chrome&#10;second line">why</skipped></testcase>
</testsuite></testsuites>
"""


def test_node_id_reads_the_module_and_classes() -> None:
    assert e2e.node_id("tests.test_x", "test_a") == "tests/test_x.py::test_a"
    assert e2e.node_id("tests.test_x.TestY", "test_a") == "tests/test_x.py::TestY::test_a"


def test_node_id_replaces_whitespace_so_the_id_is_one_word() -> None:
    assert e2e.node_id("tests.test_x", "test_a[1 2]") == "tests/test_x.py::test_a[1_2]"


def test_node_id_without_a_test_module_keeps_the_last_part_as_the_file() -> None:
    assert e2e.node_id("pkg.checks", "test_a") == "pkg/checks.py::test_a"
    assert e2e.node_id("", "test_a") == "?::test_a"


def test_cases_reads_every_outcome(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    report.write_text(JUNIT, encoding="utf-8")
    found = e2e.cases(report)
    assert [(c.ident, c.outcome) for c in found] == [
        ("tests/test_local_scrape.py::test_passes", "passed"),
        ("tests/test_local_scrape.py::test_fails", "failed"),
        ("tests/test_local_scrape.py::TestGroup::test_case[a_b]", "failed"),
        ("tests/test_local_scrape.py::test_skipped", "skipped"),
    ]
    assert found[1].message == "assert 0 > 0"
    assert found[3].message == "no chrome\nsecond line"


def test_cases_of_a_missing_report_is_empty(tmp_path: Path) -> None:
    assert e2e.cases(tmp_path / "absent.xml") == []


def test_verdict_green_run_passes_through() -> None:
    found = [e2e.Case("tests/test_a.py::test_a", "passed")]
    assert e2e.verdict(0, found, strict=True) == (0, [])


def test_verdict_keeps_pytest_red() -> None:
    found = [e2e.Case("tests/test_a.py::test_a", "failed", "boom")]
    assert e2e.verdict(1, found, strict=False) == (1, [])


def test_verdict_nothing_executed_is_red() -> None:
    code, lines = e2e.verdict(5, [], strict=False)
    assert code == 5
    assert lines == ["e2e: no local test executed, which is not a pass"]
    # pytest exiting 0 with only skips is still red.
    skipped = [e2e.Case("tests/test_a.py::test_a", "skipped", "")]
    code, lines = e2e.verdict(0, skipped, strict=False)
    assert code == 1
    assert lines[0] == "SKIPPED tests/test_a.py::test_a - skipped: no reason"


def test_verdict_a_skip_by_hand_is_listed_not_failed() -> None:
    found = [
        e2e.Case("tests/test_a.py::test_a", "passed"),
        e2e.Case("tests/test_a.py::test_b", "skipped", "no chrome\nmore"),
    ]
    assert e2e.verdict(0, found, strict=False) == (
        0,
        ["SKIPPED tests/test_a.py::test_b - skipped: no chrome"],
    )


def test_verdict_a_skip_under_the_scheduler_is_a_failure() -> None:
    found = [
        e2e.Case("tests/test_a.py::test_a", "passed"),
        e2e.Case("tests/test_a.py::test_b", "skipped", "no chrome"),
    ]
    code, lines = e2e.verdict(0, found, strict=True)
    assert code == 1
    assert lines == [
        "FAILED tests/test_a.py::test_b - skipped: no chrome",
        "e2e: 1 test(s) skipped under the scheduled run",
    ]


def test_scheduled_reads_the_flag() -> None:
    assert e2e.scheduled({"DEVKIT_E2E": "1"})
    assert not e2e.scheduled({"DEVKIT_E2E": ""})
    assert not e2e.scheduled({})


def test_venv_interpreter_prefers_the_checkout_venv(tmp_path: Path) -> None:
    assert e2e.venv_interpreter(tmp_path, "fallback") == "fallback"
    python = tmp_path / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    assert e2e.venv_interpreter(tmp_path, "fallback") == str(python)
    windows = tmp_path / ".venv" / "Scripts" / "python.exe"
    windows.parent.mkdir(parents=True)
    windows.write_text("", encoding="utf-8")
    assert e2e.venv_interpreter(tmp_path, "fallback") == str(windows)


def test_pytest_argv_selects_the_tier(tmp_path: Path) -> None:
    report = tmp_path / "r.xml"
    argv = e2e.pytest_argv("py", report)
    assert argv[:4] == ["py", "-m", "pytest", "tests"]
    assert argv[argv.index("-m", 3) + 1] == "local"
    assert f"--junitxml={report}" in argv
    named = e2e.pytest_argv("py", report, ["tests/test_x.py::test_a"])
    assert named[3] == "tests/test_x.py::test_a"
    assert "tests" not in named


def test_the_default_run_deselects_the_tier() -> None:
    """`addopts` keeps `local` out of every run that does not ask for it, CI's included."""
    options = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pytest_options = options["tool"]["pytest"]["ini_options"]
    assert "-m 'not local'" in pytest_options["addopts"]
    assert any(m.startswith("local:") for m in pytest_options["markers"])


def test_the_tier_holds_a_real_end_to_end_test() -> None:
    """At least one `local` test exists for the script to run (an empty tier is red)."""
    mark = "@pytest.mark." + "local"  # split, so this file does not count itself
    tests = (p for p in (ROOT / "tests").glob("test_*.py") if p.name != Path(__file__).name)
    assert [p for p in tests if mark in p.read_text(encoding="utf-8")]


def fake_run(report_text: str | None, code: int, seen: list[list[str]]):
    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        report = Path(next(a for a in argv if a.startswith("--junitxml=")).split("=", 1)[1])
        if report_text is not None:
            report.write_text(report_text, encoding="utf-8")
        return subprocess.CompletedProcess(argv, code, stdout="pytest out\n", stderr="")

    return run


def test_main_prints_pytest_output_and_the_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(e2e, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("DEVKIT_E2E", "1")
    seen: list[list[str]] = []
    monkeypatch.setattr(e2e.subprocess, "run", fake_run(JUNIT, 1, seen))
    assert e2e.main(["tests/test_local_scrape.py"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("pytest out\n")
    assert "FAILED tests/test_local_scrape.py::test_skipped - skipped: no chrome" in out
    assert out.rstrip().endswith("e2e: red -- details in logs/e2e-junit.xml")
    assert seen[0][3] == "tests/test_local_scrape.py"


def test_main_with_no_report_is_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(e2e, "REPO_ROOT", tmp_path)
    stale = tmp_path / "logs" / "e2e-junit.xml"
    stale.parent.mkdir()
    stale.write_text(JUNIT, encoding="utf-8")  # a previous run's, which must not count
    monkeypatch.setattr(e2e.subprocess, "run", fake_run(None, 0, []))
    assert e2e.main([]) == 1
    assert "no local test executed" in capsys.readouterr().out
