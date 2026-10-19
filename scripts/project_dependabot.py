#!/usr/bin/env python3
"""The project's own `.github/dependabot.yml`, which devkit renders once and never vendors.

`templates/` is a one-shot copy, so the template's floor-keeping `versioning-strategy`
reached new projects only. The vendored `test_ci_workflow_contract.py` holds *every*
consumer to it, though, and a project generated before the template carried it went red
on the very pull that delivered the test -- the upgrade rehearsal caught exactly that,
adopting this tree into a project rendered at the release before. So the pull makes one
pass over this file, the way `project_settings.py` makes one over the settings.

It **adds `versioning-strategy: lockfile-only` to each `uv` or `pip` entry that names no
strategy, and rewrites `increase-if-necessary` -- the value devkit itself rendered until
Dependabot's uv updater was seen ignoring it -- to the same**, and does nothing else. Under
`increase-if-necessary` that updater still rewrote data-lake's `pyarrow>=17` as
`pyarrow>=26.0.0` and `boto3>=1.43.110` as `boto3>=1.43.114`, both releases its floor
already allowed, and ibkr_trader, whose mlflow caps `pyarrow<26`, stopped resolving. Any
other strategy the project wrote itself is its decision and is kept; if that value raises
floors, the contract test says so, with the fix. The edit is textual, one line per entry,
so the file's comments, order and line endings survive: a YAML round-trip would lose all
three, and there is no YAML parser in the standard library anyway.

Stdlib only, like everything else here that runs before a virtualenv exists.

Tested in `scripts/hooks/tests/test_project_dependabot.py`.
"""

from __future__ import annotations

import re
from pathlib import Path

DEPENDABOT_FILE = ".github/dependabot.yml"
# The ecosystems whose updater rewrites a `pyproject.toml` floor, and the strategy the
# template gives them. Kept in step with the contract test's own sets.
FLOOR_RAISING_ECOSYSTEMS = frozenset({"uv", "pip"})
FLOOR_KEEPING_STRATEGY = "lockfile-only"
# Strategies devkit once rendered itself that turned out to raise floors anyway, so a
# project holding one holds devkit's mistake rather than a decision of its own.
RETIRED_STRATEGIES = frozenset({"increase-if-necessary"})

_HEAD = re.compile(r"(\s*-\s*)package-ecosystem:\s*['\"]?([\w-]+)")
_STRATEGY = re.compile(r"versioning-strategy\s*:\s*['\"]?([\w-]+)")


def _is_code(line: str) -> bool:
    """Neither blank nor a comment -- the same lines the contract test reads."""
    return bool(line.strip()) and not line.lstrip().startswith("#")


def python_entries(lines: list[str]) -> list[tuple[int, str, int, int | None]]:
    """`(head line index, ecosystem, key column, strategy line index)` per Python entry.

    An entry is its `- package-ecosystem:` line and every code line indented past the
    dash; a sibling item or a top-level key ends it. The strategy counts only at the
    entry's own key column, so a same-named key nested under `groups:` is not taken
    for it; an entry naming none has `None` for its strategy line.
    """
    found: list[tuple[int, str, int, int | None]] = []
    entry: tuple[int, str, int] | None = None
    dash = 0
    strategy: int | None = None

    def close() -> None:
        if entry is not None and entry[1] in FLOOR_RAISING_ECOSYSTEMS:
            found.append((*entry, strategy))

    for index, line in enumerate(lines):
        if not _is_code(line):
            continue
        indent = len(line) - len(line.lstrip())
        if head := _HEAD.match(line):
            close()
            entry, dash, strategy = (index, head.group(2), head.end(1)), indent, None
        elif entry is not None and indent > dash:
            if strategy is None and indent == entry[2] and _STRATEGY.match(line.lstrip()):
                strategy = index
        else:
            close()
            entry = None
    close()
    return found


def unset_python_entries(lines: list[str]) -> list[tuple[int, str, int]]:
    """`(head line index, ecosystem, key column)` per Python entry naming no strategy."""
    return [entry[:3] for entry in python_entries(lines) if entry[3] is None]


def retired_strategy_lines(lines: list[str]) -> list[tuple[int, str]]:
    """`(strategy line index, ecosystem)` per Python entry on a `RETIRED_STRATEGIES` value."""
    retired: list[tuple[int, str]] = []
    for _head, ecosystem, _column, strategy in python_entries(lines):
        if strategy is None:
            continue
        match = _STRATEGY.match(lines[strategy].lstrip())
        if match and match.group(1) in RETIRED_STRATEGIES:
            retired.append((strategy, ecosystem))
    return retired


def with_floor_keeping(text: str) -> tuple[str, list[str]]:
    """`(text, ecosystems changed)`: the strategy added where missing, retired ones replaced.

    An added line goes directly under the entry's head, at the column of its other keys,
    with the head's own line ending; a replaced one keeps its indent, quoting and any
    trailing comment. A file with nothing to change comes back as the same string.
    """
    lines = text.splitlines(keepends=True)
    retired = retired_strategy_lines(lines)
    for index, _ecosystem in retired:
        old = lines[index]
        if value := _STRATEGY.search(old):  # always: retired_strategy_lines matched it
            lines[index] = old[: value.start(1)] + FLOOR_KEEPING_STRATEGY + old[value.end(1) :]
    missing = unset_python_entries(lines)
    for index, _ecosystem, column in reversed(missing):
        head = lines[index]
        ending = head[len(head.rstrip("\r\n")) :] or "\n"
        lines[index] = head.rstrip("\r\n") + ending
        lines.insert(
            index + 1, f"{' ' * column}versioning-strategy: {FLOOR_KEEPING_STRATEGY}{ending}"
        )
    if not missing and not retired:
        return text, []
    changed = sorted([*retired, *((index, ecosystem) for index, ecosystem, _ in missing)])
    return "".join(lines), [ecosystem for _index, ecosystem in changed]


def dependabot_pass(root: Path) -> list[str]:
    """The pull's pass over this project's Dependabot config; one note per change made.

    Best-effort, like the settings pass: a file that is absent or will not decode is
    left exactly as it is. Read and written as bytes so a CRLF file stays CRLF.
    """
    path = root / DEPENDABOT_FILE
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    updated, ecosystems = with_floor_keeping(text)
    if not ecosystems:
        return []
    try:
        path.write_bytes(updated.encode("utf-8"))
    except OSError:
        return []
    return [f"(floor-keeping versioning-strategy) {DEPENDABOT_FILE}: {', '.join(ecosystems)}"]
