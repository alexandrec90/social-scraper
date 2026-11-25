#!/usr/bin/env python3
"""What no change may land with, checked in the seconds before it is pushed.

In the direct flow nothing gates a merge and every check runs after it, so a broken
change is fixed forward. Three breakages are worth stopping first, because each leaves a
file nothing can read and the session that wrote it is still here to fix it: Python that
does not parse, TOML or JSON that does not parse (`.devkit.toml` among them), and a
conflict marker left in a file.

**It must never be in the way of a change that is fine.** It installs nothing, runs
nothing, asks the network nothing, and reads only the files the change touches. Any doubt
passes: a file it cannot read, one that was already broken on the base, a syntax newer
than the interpreter running this, JSON that may carry comments.

`land.py` runs it for a session and `ship_intent.py` for an intent, so both roads into
the default branch hold the same thing. Vendored from devkit (`sync-devkit.py`'s
MANIFEST), stdlib only. Tested in `scripts/hooks/tests/test_land_check.py`.
"""

from __future__ import annotations

import ast
import json
import re
import sys
import tomllib
import warnings
from collections.abc import Callable, Iterable
from pathlib import Path

# A bigger file is data, not something a person resolved a conflict in or wrote by hand.
MAX_BYTES = 1_000_000

# All three of a conflict's lines, in order, each alone at the start of its line: prose
# and tests that mention one are not a conflict.
CONFLICT = re.compile(
    r"^<{7} [^\n]*\n(?:(?!^>{7} )[^\n]*\n)*?={7}\r?\n(?:(?!^<{7} )[^\n]*\n)*?^>{7} [^\n]*$", re.M
)

# JSON by name, comments by habit: each of these tools reads JSON with comments.
JSONC = re.compile(
    r"(^|/)(\.vscode|\.devcontainer)/|(^|/)[tj]sconfig[^/]*\.json$|\.code-workspace$"
)

BaseText = Callable[[str], "str | None"]


def python_version(root: Path) -> tuple[int, int]:
    """`[python] version` in `.devkit.toml`; `(0, 0)` when nothing readable says."""
    try:
        settings = tomllib.loads((root / ".devkit.toml").read_text(encoding="utf-8"))
        major, minor = str(settings["python"]["version"]).split(".")[:2]
        return int(major), int(minor)
    except (OSError, ValueError, KeyError, TypeError):  # TOMLDecodeError is a ValueError
        return 0, 0


def python_problem(text: str) -> str:
    if "\0" in text:
        return ""  # data, not source; a ValueError or a SyntaxError by the patch release
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # an invalid escape is the linter's
            ast.parse(text)
    except SyntaxError as broken:
        return f"does not parse: {broken.msg} (line {broken.lineno})"
    except (ValueError, RecursionError, MemoryError):
        return ""  # a file too deep to read: not this check's to judge
    return ""


def toml_problem(text: str) -> str:
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as broken:
        return f"is not valid TOML: {broken}"
    return ""


def json_problem(text: str) -> str:
    try:
        json.loads(text)
    except ValueError as broken:
        return f"is not valid JSON: {broken}"
    return ""


def problem_in(path: str, text: str, judges_python: bool = True) -> str:
    """What is wrong with `text`, the contents of `path`, or ""."""
    text = text.removeprefix("\N{BYTE ORDER MARK}")
    if CONFLICT.search(text):
        return "has an unresolved merge conflict"
    if path.endswith(".py"):
        return python_problem(text) if judges_python else ""
    if path.endswith(".toml"):
        return toml_problem(text)
    if path.endswith(".json") and not JSONC.search(path):
        return json_problem(text) if text.strip() else ""
    return ""


def read(root: Path, path: str) -> str | None:
    """`path` as text; None for one that is deleted, large, binary or not UTF-8."""
    file = root / path
    try:
        if file.is_symlink() or not file.is_file() or file.stat().st_size > MAX_BYTES:
            return None
        return file.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None


def _nothing(_path: str) -> str | None:
    return None


def problems(root: Path, paths: Iterable[str], base_text: BaseText = _nothing) -> list[str]:
    """`path: what is wrong` for each changed file that must not land.

    `base_text` reads a path as the base has it: a file broken the same way there is not
    this change's doing, and a fixture that is broken on purpose is exactly that.
    """
    judges_python = python_version(root) <= sys.version_info[:2]
    found = []
    for path in sorted(set(paths)):
        text = read(root, path)
        if text is None or not (why := problem_in(path, text, judges_python)):
            continue
        before = base_text(path)
        if before is not None and problem_in(path, before, judges_python):
            continue
        found.append(f"{path}: {why}")
    return found
