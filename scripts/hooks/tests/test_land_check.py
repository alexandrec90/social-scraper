"""`scripts/land_check.py`: what no change may land with, and everything it must let by."""

from __future__ import annotations

import sys

import pytest
from conftest import load_module

land_check = load_module("scripts/land_check.py")

# Built, so this file never holds a conflict of its own.
MARKERS = "\n".join(("<" * 7 + " HEAD", "a", "=" * 7, "b", ">" * 7 + " other", ""))


def found(root, name: str, text: str, **kwargs) -> list[str]:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return land_check.problems(root, [name], **kwargs)


@pytest.mark.parametrize(
    ("name", "text", "says"),
    [
        ("a.py", "def broken(:\n", "does not parse"),
        (".devkit.toml", "[delivery\n", "not valid TOML"),
        ("package.json", "{,}", "not valid JSON"),
        ("notes.md", MARKERS, "unresolved merge conflict"),
        ("a.py", MARKERS, "unresolved merge conflict"),
    ],
)
def test_a_file_nothing_can_read_is_named(tmp_path, name, text, says):
    [problem] = found(tmp_path, name, text)
    assert problem.startswith(f"{name}: ") and says in problem


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("a.py", "x = 1\n"),
        ("a.py", "\N{BYTE ORDER MARK}x = 1\n"),
        ("a.py", "x = '\\d'\n"),  # a SyntaxWarning, which is the linter's
        ("pyproject.toml", "[project]\n"),
        ("data.json", "{}"),
        ("empty.json", ""),
        ("tsconfig.json", "{ // comments are this file's habit\n}"),
        (".vscode/settings.json", "{ // and this one's\n}"),
        ("notes.md", "Heading\n=======\nthen `<<<<<<< HEAD` in prose\n"),
        ("broken.txt", "def broken(:\n"),
    ],
)
def test_a_change_that_is_fine_is_never_in_the_way(tmp_path, name, text):
    assert found(tmp_path, name, text) == []


def test_a_deleted_a_binary_and_a_huge_file_are_passed_over(tmp_path):
    (tmp_path / "b.json").write_bytes(b"\xff\xfe\x00")
    (tmp_path / "c.json").write_text("{" + " " * land_check.MAX_BYTES, encoding="utf-8")
    assert land_check.problems(tmp_path, ["gone.py", "b.json", "c.json"]) == []
    assert land_check.read(tmp_path, "gone.py") is None


def test_a_file_already_broken_on_the_base_is_not_this_changes(tmp_path):
    """A fixture broken on purpose, edited."""
    assert found(tmp_path, "bad.json", "{,,}", base_text=lambda _p: "{,}") == []
    assert found(tmp_path, "bad.json", "{,,}", base_text=lambda _p: "{}")


def test_a_project_on_a_newer_python_is_not_judged_by_an_older_one(tmp_path):
    newer = f"{sys.version_info[0]}.{sys.version_info[1] + 1}"
    (tmp_path / ".devkit.toml").write_text(f'[python]\nversion = "{newer}"\n', encoding="utf-8")
    assert found(tmp_path, "a.py", "def broken(:\n") == []
    assert land_check.python_version(tmp_path) == (
        sys.version_info[0],
        sys.version_info[1] + 1,
    )
    (tmp_path / ".devkit.toml").write_text("[python]\n", encoding="utf-8")
    assert land_check.python_version(tmp_path) == (0, 0)


def test_each_reader_says_nothing_of_what_it_can_read():
    assert land_check.python_problem("x = 1") == ""
    assert land_check.python_problem("x = '\0'") == ""
    assert land_check.toml_problem("a = 1") == ""
    assert land_check.json_problem("[]") == ""
    assert land_check.problem_in("a.txt", "anything") == ""
