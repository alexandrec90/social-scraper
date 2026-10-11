"""Tests for `code_text.py`: a file's code with its docstrings and comments blanked."""

from __future__ import annotations

import pytest
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


@pytest.mark.parametrize(
    "text, module, scopes",
    [
        ("", "", []),
        ("x = 1\n", "x = 1\n", []),
        ("def f():\n    pass\n", "", ["def f():\n    pass\n"]),
        # 7e14d6d3: a statement after a function is the module's, not the function's
        (
            "import a\ndef f():\n    pass\n\nup = a.load()\nclass C:\n    y = 1\n",
            "import a\nup = a.load()\n",
            ["def f():\n    pass\n\n", "class C:\n    y = 1\n"],
        ),
        # decorators stay with their definition; `async def` opens one too
        (
            "@d\n@e(1)\nasync def f():\n    pass\nz = 2\n",
            "z = 2\n",
            ["@d\n@e(1)\nasync def f():\n    pass\n"],
        ),
        # a multi-line signature's `):` and an indented body line at column 0 inside
        # brackets are continuations, not statements
        (
            "def f(\n    p,\n):\n    x = g(\n1)\nw = 3\n",
            "w = 3\n",
            ["def f(\n    p,\n):\n    x = g(\n1)\n"],
        ),
    ],
)
def test_top_level_scopes_gives_the_module_every_statement_that_is_not_a_definition(
    text, module, scopes
):
    assert ct.top_level_scopes(text) == (module, scopes)


def test_top_level_scopes_falls_back_to_splitting_at_definitions_when_text_is_not_python():
    broken = "x = 1\ndef f(:\n    pass\ny = (\n"
    assert ct.top_level_scopes(broken) == ("x = 1\n", ["def f(:\n    pass\ny = (\n"])
