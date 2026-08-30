"""Tests for `structure_scan.py` -- the per-file measurements behind the structural gate.

Pure functions over snippets: nothing here touches the repository, so a wrong number
is a wrong scanner and not a wrong fixture. The JS scanner is a masking pass plus
regular expressions, and the tests for it are mostly the things that pass would get
wrong if it were naive -- a brace in a string, a `TODO` in a comment, a regex literal
holding a quote.
"""

from __future__ import annotations

from conftest import load_module

ss = load_module("scripts/hooks/structure_scan.py")

PY = """\
import os
import sys
from pathlib import Path
from .sibling import thing
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from heavy import Heavy


def alpha(a, b, c, d, e, f, g):
    if a:  # noqa: E501
        for x in b:
            while c:
                with d:
                    if e and f or g:
                        pass
    try:
        pass
    except Exception:
        pass
    return 1


class Gamma:
    def one(self):
        pass

    def two(self, x):
        pass


# TODO: later
"""

JS = """\
import React, { useState, useEffect } from "react";
import type { Foo } from "./types";
import { helper } from "./lib/helper";
const api = require("../api");
// eslint-disable-next-line no-console
const x: any = "not a { brace";
const re = /"{/g;
export default function Widget({ a, b }) {
  const [s, setS] = useState(0);
  const [t, setT] = useState(1);
  useEffect(() => {}, []);
  try { risky(); } catch (e) {}
  return <div>{s}</div>;
}
export function tiny(a, b, c, d, e, f, g) { if (a && b) { return 1; } return 2; }
it.skip("later", () => {});
test.only("focus", () => {});
class Store { get() {} set() {} }
"""


def _symbol(metrics, name):
    return next(s for s in metrics.symbols if s.name == name)


# --- is_test_file ---------------------------------------------------------------


def test_is_test_file_knows_both_stacks_conventions():
    assert ss.is_test_file("tests/test_alpha.py")
    assert ss.is_test_file("src/conftest.py")
    assert ss.is_test_file("src/alpha_test.py")
    assert ss.is_test_file("src/__tests__/alpha.ts")
    assert ss.is_test_file("src/alpha.test.tsx")
    assert ss.is_test_file("src/alpha.spec.js")
    assert ss.is_test_file("src/Alpha.stories.tsx")
    assert not ss.is_test_file("src/alpha.py")
    assert not ss.is_test_file("src/testing_helpers.ts")


# --- Python -----------------------------------------------------------------------


def test_scan_python_measures_a_function():
    m = ss.scan_python(PY)
    alpha = _symbol(m, "alpha")
    assert alpha.kind == "function"
    assert alpha.params == 7
    assert alpha.depth == 5
    # 1 + if + for + while + inner if + (and, or) + except handler.
    assert alpha.complexity == 8
    assert alpha.lines == 12


def test_scan_python_counts_methods_on_a_class_and_drops_self():
    m = ss.scan_python(PY)
    gamma = _symbol(m, "Gamma")
    assert gamma.kind == "class"
    assert gamma.methods == 2
    assert _symbol(m, "two").params == 1


def test_scan_python_reports_size_and_fan_out():
    m = ss.scan_python(PY)
    assert m.lines == len(PY.splitlines())
    assert m.definitions == 2
    assert m.imports == 6


def test_scan_python_runtime_imports_skip_the_type_checking_block():
    m = ss.scan_python(PY)
    assert "os" in m.modules
    assert "pathlib" in m.modules
    assert "pathlib.Path" in m.modules
    assert ".sibling.thing" in m.modules
    assert not any(mod.startswith("heavy") for mod in m.modules)


def test_scan_python_counters():
    m = ss.scan_python(PY)
    assert m.counts["suppressions"] == 1
    assert m.counts["todos"] == 1
    assert m.counts["swallowed_errors"] == 1
    assert m.counts["skipped_tests"] == 0
    assert m.counts["any_types"] == 0
    assert set(m.counts) == set(ss.COUNTERS)


def test_scan_python_counts_skipped_and_xfailed_tests():
    src = (
        "import pytest\n\n"
        "@pytest.mark.skip(reason='x')\ndef test_a(): pass\n\n"
        "@pytest.mark.xfail\ndef test_b(): pass\n\n"
        "def test_c():\n    pytest.skip('later')\n"
    )
    assert ss.scan_python(src).counts["skipped_tests"] == 3


def test_scan_python_a_handler_that_reraises_is_not_a_swallow():
    src = "try:\n    pass\nexcept Exception:\n    raise\nexcept ValueError:\n    pass\n"
    assert ss.scan_python(src).counts["swallowed_errors"] == 0


def test_scan_python_returns_none_for_what_does_not_parse():
    assert ss.scan_python("def (:\n") is None


def test_python_comments_lists_every_comment():
    comments = ss.python_comments("x = 1  # one\n# two\ns = '# not'\n")
    assert [c.strip() for c in comments] == ["# one", "# two"]


# --- JavaScript / TypeScript ----------------------------------------------------


def test_mask_literals_blanks_strings_comments_and_regexes_but_keeps_the_shape():
    text = 'const s = "a { b"; const r = /"{/g; // c {\n'
    masked, comments = ss.mask_literals(text)
    assert len(masked) == len(text)
    assert "{" not in masked
    assert masked.count("\n") == text.count("\n")
    assert any("c {" in c for c in comments)


def test_mask_literals_handles_a_template_with_a_nested_expression():
    text = "const t = `a ${ f({ x: 1 }) } b`; const after = 1;"
    masked, _ = ss.mask_literals(text)
    assert "after" in masked
    assert "b`" not in masked


def test_scan_js_measures_a_component_and_its_hooks():
    m = ss.scan_js(JS, react=True)
    widget = _symbol(m, "Widget")
    assert widget.kind == "component"
    assert m.react_state == 2
    assert m.react_effects == 1


def test_scan_js_measures_a_function():
    m = ss.scan_js(JS)
    tiny = _symbol(m, "tiny")
    assert tiny.kind == "function"
    assert tiny.params == 7
    assert tiny.complexity == 3
    assert tiny.lines == 1


def test_scan_js_counts_class_methods():
    store = _symbol(ss.scan_js(JS), "Store")
    assert store.kind == "class"
    assert store.methods == 2


def test_scan_js_runtime_specifiers_skip_type_only_imports():
    m = ss.scan_js(JS)
    assert "react" in m.modules
    assert "./lib/helper" in m.modules
    assert "../api" in m.modules
    assert "./types" not in m.modules


def test_scan_js_counters():
    m = ss.scan_js(JS)
    assert m.counts["suppressions"] == 1
    assert m.counts["any_types"] == 1
    assert m.counts["skipped_tests"] == 1
    assert m.counts["focused_tests"] == 1
    assert m.counts["swallowed_errors"] == 1
    assert m.counts["todos"] == 0


def test_scan_js_a_brace_in_a_string_does_not_end_a_function():
    src = 'function alpha() {\n  const s = "}";\n  if (s) {\n    return 1;\n  }\n}\n'
    assert _symbol(ss.scan_js(src), "alpha").lines == 6


def test_scan_js_a_todo_in_code_is_not_a_todo():
    src = "const TODO = 1; // TODO: rename\n"
    assert ss.scan_js(src).counts["todos"] == 1


def test_scan_js_a_nested_function_is_measured_apart_from_its_parent():
    src = "function outer() {\n  const inner = (a, b) => {\n    if (a) { return b; }\n  };\n  return inner;\n}\n"
    m = ss.scan_js(src)
    assert _symbol(m, "inner").params == 2
    assert _symbol(m, "outer").complexity == 1


def test_scan_js_arrow_and_method_forms():
    src = "const alpha = async (a) => { return a; };\nconst obj = { beta(a, b) { return a; } };\n"
    m = ss.scan_js(src)
    assert _symbol(m, "alpha").params == 1
    assert _symbol(m, "beta").params == 2


def test_scan_js_an_expression_bodied_arrow_is_not_measured():
    """The quiet failure mode, on purpose: no body to measure, and never a spurious
    symbol from a `=>` in a type annotation."""
    assert [s.name for s in ss.scan_js("const alpha = (a) => a;\n").symbols] == []


def test_scan_js_a_lowercase_function_in_a_tsx_file_is_not_a_component():
    m = ss.scan_js("function helper() { return 1; }\n", react=True)
    assert _symbol(m, "helper").kind == "function"


# --- dispatch -----------------------------------------------------------------------


def test_scan_dispatches_on_suffix():
    assert ss.scan("a.py", "x = 1\n").lines == 1
    assert ss.scan("a.ts", "const x = 1;\n").lines == 1
    assert ss.scan("a.md", "# nope\n") is None


def test_scan_a_tsx_file_gets_the_react_scanner():
    m = ss.scan("A.tsx", "export function Alpha() { return null; }\n")
    assert _symbol(m, "Alpha").kind == "component"


def test_symbol_and_file_metrics_are_frozen_records():
    sym = ss.Symbol(name="alpha", kind="function", line=1, lines=1)
    metrics = ss.FileMetrics(lines=1, imports=0, definitions=1, symbols=(sym,))
    assert metrics.symbols[0].complexity == 1
    assert metrics.counts == {}
