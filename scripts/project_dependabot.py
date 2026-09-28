#!/usr/bin/env python3
"""The project's own `.github/dependabot.yml`, which devkit renders once and never vendors.

`templates/` is a one-shot copy, so the template's floor-keeping `versioning-strategy`
reached new projects only. The vendored `test_ci_workflow_contract.py` holds *every*
consumer to it, though, and a project generated before the template carried it went red
on the very pull that delivered the test -- the upgrade rehearsal caught exactly that,
adopting this tree into a project rendered at the release before. So the pull makes one
pass over this file, the way `project_settings.py` makes one over the settings.

It **adds `versioning-strategy: increase-if-necessary` to each `uv` or `pip` entry that
names no strategy**, and does nothing else. A strategy the project wrote itself is its
decision and is kept, whatever its value; if that value raises floors, the contract
test says so, with the fix. The edit is textual, one inserted line per entry, so the
file's comments, order and line endings survive: a YAML round-trip would lose all
three, and there is no YAML parser in the standard library anyway.

Stdlib only, like everything else here that runs before a virtualenv exists.

Tested in `scripts/hooks/tests/test_project_dependabot.py`.
"""

from __future__ import annotations

import re
from pathlib import Path

DEPENDABOT_FILE = ".github/dependabot.yml"
# The ecosystems whose default strategy rewrites a `pyproject.toml` floor, and the
# strategy the template gives them. Kept in step with the contract test's own sets.
FLOOR_RAISING_ECOSYSTEMS = frozenset({"uv", "pip"})
FLOOR_KEEPING_STRATEGY = "increase-if-necessary"

_HEAD = re.compile(r"(\s*-\s*)package-ecosystem:\s*['\"]?([\w-]+)")
_STRATEGY = re.compile(r"versioning-strategy\s*:")


def _is_code(line: str) -> bool:
    """Neither blank nor a comment -- the same lines the contract test reads."""
    return bool(line.strip()) and not line.lstrip().startswith("#")


def unset_python_entries(lines: list[str]) -> list[tuple[int, str, int]]:
    """`(head line index, ecosystem, key column)` per Python entry naming no strategy.

    An entry is its `- package-ecosystem:` line and every code line indented past the
    dash; a sibling item or a top-level key ends it. The strategy counts only at the
    entry's own key column, so a same-named key nested under `groups:` is not taken
    for it.
    """
    found: list[tuple[int, str, int]] = []
    entry: tuple[int, str, int] | None = None
    dash = 0
    has_strategy = False

    def close() -> None:
        if entry is not None and entry[1] in FLOOR_RAISING_ECOSYSTEMS and not has_strategy:
            found.append(entry)

    for index, line in enumerate(lines):
        if not _is_code(line):
            continue
        indent = len(line) - len(line.lstrip())
        if head := _HEAD.match(line):
            close()
            entry, dash, has_strategy = (index, head.group(2), head.end(1)), indent, False
        elif entry is not None and indent > dash:
            has_strategy |= indent == entry[2] and bool(_STRATEGY.match(line.lstrip()))
        else:
            close()
            entry = None
    close()
    return found


def with_floor_keeping(text: str) -> tuple[str, list[str]]:
    """`(text, ecosystems given a strategy)`: the strategy line added where it is missing.

    Inserted directly under the entry's head, at the column of its other keys, with the
    head's own line ending. A file with nothing to add comes back as the same string.
    """
    lines = text.splitlines(keepends=True)
    missing = unset_python_entries(lines)
    for index, _ecosystem, column in reversed(missing):
        head = lines[index]
        ending = head[len(head.rstrip("\r\n")) :] or "\n"
        lines[index] = head.rstrip("\r\n") + ending
        lines.insert(
            index + 1, f"{' ' * column}versioning-strategy: {FLOOR_KEEPING_STRATEGY}{ending}"
        )
    if not missing:
        return text, []
    return "".join(lines), [ecosystem for _index, ecosystem, _column in missing]


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
