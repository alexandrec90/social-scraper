#!/usr/bin/env python3
"""social-scraper's local end-to-end tier: the tests that need this workstation.

The contract is `.claude/rules/local-e2e.md`'s, which devkit's nightly
`devkit-local-e2e` job reads: exit 0 green, anything else red, each failing test printed
as pytest's `FAILED <id>` line, run from the checkout root. What it runs is every test
marked `local` -- a real Chrome against a live platform, end to end into the lake --
which `pyproject.toml`'s `addopts` deselects from every other run, CI's included. That
deselection, not a skip, is what keeps the tier off a hosted runner.

**A run that executed nothing is red**: a green from zero tests is a skip reported as a
pass. **Under the scheduler (`DEVKIT_E2E` set) a skip is a failure**, printed as a
`FAILED` line naming its reason: the checkout is the one the tests were written for, so
nothing has a reason left to skip. By hand, skips are listed and do not fail the run.

Test ids on the command line run those alone, still inside the tier.

pytest's own output is captured and re-printed, so the pass reading this process's
stdout sees pytest's `FAILED` lines even where the child would otherwise have written
to a hidden console. The junit report it parses stays at `logs/e2e-junit.xml`, the
parseable failure artifact, overwritten per run.

Stdlib only: the scheduler runs it with the checkout's `.venv` interpreter when there is
one and the machine's Python otherwise, and pytest is run from that `.venv` either way.

Tested in `tests/test_e2e.py`.
"""

from __future__ import annotations

import html
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_FLAG = "DEVKIT_E2E"
MARKER = "local"
JUNIT = Path("logs") / "e2e-junit.xml"

# pytest's junit report is its own flat shape: a `<testcase .../>` or a `<testcase ...>`
# whose body opens with `<failure>`, `<error>` or `<skipped>` when it did not pass. Read
# with patterns rather than an XML parser, as devkit's copy does: an entity or a DTD can
# change none of what is wanted here.
CASE = re.compile(r"<testcase\b(?P<attrs>[^>]*?)(?:/>|>(?P<body>.*?)</testcase>)", re.DOTALL)
ATTR = re.compile(r'(\w+)="([^"]*)"')
OUTCOME = re.compile(r"<(?P<tag>skipped|failure|error)\b(?P<attrs>[^>]*)")


@dataclass(frozen=True)
class Case:
    """One test case from the junit report: `tests/x.py::name`, and how it ended."""

    ident: str
    outcome: str  # "passed", "failed" or "skipped"
    message: str = ""


def scheduled(env: Mapping[str, str] = os.environ) -> bool:
    return bool(env.get(ENV_FLAG))


def node_id(classname: str, name: str) -> str:
    """pytest's node id from junit's `classname` and `name`.

    `tests.test_x.TestY` is the module `tests/test_x.py` and the class `TestY`: the module
    is the first dotted part named `test_*`. Whitespace in a parametrized name becomes
    `_`, since the fix pass reads an id up to its first whitespace.
    """
    parts = [p for p in classname.split(".") if p]
    module = next((i for i, p in enumerate(parts) if p.startswith("test_")), len(parts) - 1)
    path = "/".join(parts[: module + 1]) + ".py" if parts else "?"
    ident = "::".join([path, *parts[module + 1 :], name])
    return re.sub(r"\s+", "_", ident)


def cases(report: Path) -> list[Case]:
    """Every case in a pytest junit report. Unreadable is no cases, which `verdict`
    reads as a run that ran nothing."""
    try:
        text = report.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found: list[Case] = []
    for match in CASE.finditer(text):
        attrs = dict(ATTR.findall(match.group("attrs")))
        name = html.unescape(attrs.get("name", "?"))
        ident = node_id(html.unescape(attrs.get("classname", "")), name)
        ended = OUTCOME.search(match.group("body") or "")
        if ended is None:
            found.append(Case(ident, "passed"))
            continue
        message = html.unescape(dict(ATTR.findall(ended["attrs"])).get("message", ""))
        outcome = "skipped" if ended["tag"] == "skipped" else "failed"
        found.append(Case(ident, outcome, message))
    return found


def verdict(code: int, found: list[Case], strict: bool) -> tuple[int, list[str]]:
    """`(exit code, lines to print)` for pytest's `code` and the cases it reported.

    pytest already printed its own `FAILED` lines; these add the skips and the empty run.
    """
    ran = [case for case in found if case.outcome != "skipped"]
    skips = [case for case in found if case.outcome == "skipped"]
    lines: list[str] = []
    for case in skips:
        reason = case.message.strip().splitlines()[0] if case.message.strip() else "no reason"
        lines.append(f"{'FAILED' if strict else 'SKIPPED'} {case.ident} - skipped: {reason}")
    if not ran:
        lines.append(f"e2e: no {MARKER} test executed, which is not a pass")
        return (code or 1), lines
    if strict and skips:
        lines.append(f"e2e: {len(skips)} test(s) skipped under the scheduled run")
        return (code or 1), lines
    return code, lines


def venv_interpreter(root: Path, fallback: str) -> str:
    """The checkout's `.venv` Python, which has pytest and the project, else `fallback`."""
    for candidate in (root / ".venv" / "Scripts" / "python.exe", root / ".venv" / "bin" / "python"):
        if candidate.is_file():
            return str(candidate)
    return fallback


def pytest_argv(python: str, report: Path, named: Sequence[str] = ()) -> list[str]:
    return [
        python,
        "-m",
        "pytest",
        *(named or ["tests"]),
        "-m",
        MARKER,
        "-rfE",
        "-p",
        "no:cacheprovider",
        f"--junitxml={report}",
    ]


def main(argv: list[str] | None = None) -> int:
    named = sys.argv[1:] if argv is None else argv
    report = REPO_ROOT / JUNIT
    report.parent.mkdir(parents=True, exist_ok=True)
    report.unlink(missing_ok=True)
    python = venv_interpreter(REPO_ROOT, sys.executable)
    # No console window for the child on Windows: the scheduler runs this unattended.
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    done = subprocess.run(
        pytest_argv(python, report, named),
        cwd=REPO_ROOT,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        creationflags=flags,
    )
    print(done.stdout or "", end="", flush=True)
    print(done.stderr or "", end="", file=sys.stderr, flush=True)
    code, lines = verdict(done.returncode, cases(report), scheduled())
    for line in lines:
        print(line, flush=True)
    print(f"e2e: {'green' if code == 0 else 'red'} -- details in {JUNIT.as_posix()}", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
