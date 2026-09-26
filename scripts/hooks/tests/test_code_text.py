"""Tests for `code_text.py`: a file's code with its docstrings and comments blanked."""

from __future__ import annotations

from conftest import load_module

ct = load_module("scripts/hooks/code_text.py")


def test_code_only_blanks_docstrings_and_comments_and_keeps_layout():
    """Blanked to spaces, not cut: every remaining token keeps its line and column, and
    the string a loader is *called with* -- an argument, not a docstring -- survives."""
    text = (
        '"""Module prose naming src/acme-tool.py."""\n'
        "\n"
        "def test_it():\n"
        '    """Function prose: acme_tool."""  # trailing acme_tool\n'
        "    acme = load('src/acme-tool.py')\n"
        "    acme.alpha()\n"
    )
    code = ct.code_only(text)
    assert len(code) == len(text)
    assert code.splitlines() == [
        " " * len('"""Module prose naming src/acme-tool.py."""'),
        "",
        "def test_it():",
        "    "
        + " " * len('"""Function prose: acme_tool."""')
        + "  "
        + " " * len("# trailing acme_tool"),
        "    acme = load('src/acme-tool.py')",
        "    acme.alpha()",
    ]


def test_code_only_returns_text_that_is_not_python_unchanged():
    """A broken test file is the linter's and the interpreter's to report; this gate
    scanning it as written is the same answer it gave before `code_only` existed."""
    broken = "def test_(:\n    acme.alpha("
    assert ct.code_only(broken) == broken
    assert ct.code_only("") == ""
