#!/usr/bin/env python3
"""Per-file structural measurements for Python and JavaScript/TypeScript, stdlib only.

The measuring half of `structure_check.py`, kept apart from the judging half so that
each language's scanner can be tested against a snippet rather than a repository. It
knows nothing about limits, baselines or `.devkit.toml`: it reads one file's text and
returns numbers.

## Two scanners, one shape

Python is read with `ast`, which is exact. JavaScript and TypeScript have no parser in
the standard library, so that scanner is a **masking pass plus regular expressions**:
`mask_literals` blanks every string, template literal, comment and regex literal to
spaces of the same length, and everything after it matches on text in which a `{`
inside a string cannot unbalance a function body and `TODO` in a comment cannot look
like code. It is a heuristic, and its failure mode is deliberately the quiet one -- a
construct it cannot follow yields a function it does not measure, never a spurious one.

Both return a `FileMetrics`: the file's size and fan-out, every function and class with
its measurements, the import specifiers the dependency graph is built from, and the
counters that are ratcheted rather than limited (suppressions, `any`, TODOs, skipped
tests, swallowed errors). The rule names those counters use are the keys of
`FileMetrics.counts`, and `structure_check.py` reads them by name.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from dataclasses import dataclass, field
from pathlib import PurePosixPath

PY_SUFFIXES = frozenset({".py"})
JS_SUFFIXES = frozenset({".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"})
REACT_SUFFIXES = frozenset({".jsx", ".tsx"})

# The ratcheted counters every scanner reports, so a language that has no notion of
# one (Python has no `any`) still reports 0 and the rule set is identical per file.
COUNTERS = (
    "suppressions",
    "any_types",
    "todos",
    "skipped_tests",
    "focused_tests",
    "swallowed_errors",
)


@dataclass(frozen=True)
class Symbol:
    """One function, method, class or React component, with what the limits read."""

    name: str
    kind: str  # "function" | "class" | "component"
    line: int
    lines: int
    params: int = 0
    complexity: int = 1
    depth: int = 0
    methods: int = 0


@dataclass(frozen=True)
class FileMetrics:
    lines: int
    imports: int
    definitions: int
    symbols: tuple[Symbol, ...] = ()
    # Import specifiers as written, for the dependency graph. Runtime imports only:
    # `import type` and `if TYPE_CHECKING:` create no cycle a program can fall into.
    modules: tuple[str, ...] = ()
    counts: dict[str, int] = field(default_factory=dict)
    react_state: int = 0
    react_effects: int = 0


def is_test_file(rel: str) -> bool:
    """Whether a repo-relative POSIX path is a test, by the conventions of both stacks."""
    path = PurePosixPath(rel)
    name = path.name
    if any(part in ("tests", "test", "__tests__", "__mocks__") for part in path.parts[:-1]):
        return True
    if name.startswith("test_") or name == "conftest.py":
        return True
    stem = name.split(".", 1)[0]
    return (
        name.endswith(("_test.py",))
        or bool(re.search(r"\.(test|spec|stories)\.[cm]?[jt]sx?$", name))
        or stem.endswith("_test")
    )


# --- Python ---------------------------------------------------------------------

_PY_SUPPRESSION = re.compile(
    r"#\s*(noqa|type:\s*ignore|nosec|pragma:\s*no\s*cover|pyright:\s*ignore|"
    r"ruff:\s*noqa|mypy:\s*ignore|fmt:\s*(off|skip))"
)
_TODO = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b")
_PY_SKIP = re.compile(
    r"\bpytest\.mark\.(skip|skipif|xfail)\b|\bpytest\.(skip|xfail)\s*\("
    r"|\bunittest\.(skip|skipIf|skipUnless|expectedFailure)\b"
)
_BROAD = frozenset({"Exception", "BaseException"})
_BRANCHES = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler, ast.IfExp, ast.Assert)
_NESTING = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try)
_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


def python_comments(text: str) -> list[str]:
    """Every comment in `text`, via the tokenizer, so a `#` inside a string is not one.

    Falls back to a line scan when the tokenizer cannot finish -- a file with a
    tokenizer error still has comments worth counting, and the linter will report the
    error itself.
    """
    try:
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
        return [tok.string for tok in tokens if tok.type == tokenize.COMMENT]
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return [line[line.index("#") :] for line in text.splitlines() if "#" in line]


def _iter_body(node: ast.AST):
    """Descendants of `node` that belong to its own body: nested defs are their own."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _DEFS) and not isinstance(child, ast.Lambda):
            continue
        yield child
        yield from _iter_body(child)


def _complexity(func: ast.AST) -> int:
    score = 1
    for node in _iter_body(func):
        if isinstance(node, _BRANCHES):
            score += 1
        elif isinstance(node, ast.BoolOp):
            score += len(node.values) - 1
        elif isinstance(node, ast.comprehension):
            score += 1 + len(node.ifs)
        elif type(node).__name__ == "match_case":
            score += 1
    return score


def _depth(node: ast.AST, current: int = 0) -> int:
    deepest = current
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _DEFS):
            continue
        nested = current + 1 if isinstance(child, _NESTING) else current
        deepest = max(deepest, _depth(child, nested))
    return deepest


def _params(func: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    args = func.args
    names = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    count = len(names) + (1 if args.vararg else 0) + (1 if args.kwarg else 0)
    if names and names[0].arg in ("self", "cls"):
        count -= 1
    return count


def _span(node: ast.AST) -> int:
    start = getattr(node, "lineno", 1)
    end = getattr(node, "end_lineno", None) or start
    return end - start + 1


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _runtime_imports(node: ast.AST) -> list[str]:
    """Import specifiers reachable at runtime: `a.b`, `.rel`, `..pkg.mod`.

    A `from x import y` yields both `x` and `x.y`, because `y` may be a submodule or a
    symbol and only the resolver, with the file tree in hand, can tell which.
    """
    found: list[str] = []
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.If) and _is_type_checking(child.test):
            found.extend(_runtime_imports_of(child.orelse))
            continue
        if isinstance(child, ast.Import):
            found.extend(alias.name for alias in child.names)
        elif isinstance(child, ast.ImportFrom):
            base = "." * child.level + (child.module or "")
            found.append(base)
            joiner = "" if base.endswith(".") or not base else "."
            found.extend(f"{base}{joiner}{alias.name}" for alias in child.names)
        else:
            found.extend(_runtime_imports(child))
    return found


def _runtime_imports_of(nodes: list[ast.stmt]) -> list[str]:
    holder = ast.Module(body=nodes, type_ignores=[])
    return _runtime_imports(holder)


def _swallows(handler: ast.ExceptHandler) -> bool:
    """A broad handler that never re-raises: the shape that hides every bug at once."""
    kind = handler.type
    names: list[str] = []
    if kind is None:
        broad = True
    else:
        parts = kind.elts if isinstance(kind, ast.Tuple) else [kind]
        names = [p.id for p in parts if isinstance(p, ast.Name)]
        broad = any(name in _BROAD for name in names)
    if not broad:
        return False
    return not any(isinstance(node, ast.Raise) for node in ast.walk(handler))


def scan_python(text: str) -> FileMetrics | None:
    """Measure one Python module. `None` when it does not parse -- not this scanner's
    finding to make; the interpreter and the linter both report it louder."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    symbols: list[Symbol] = []
    imports = 0
    swallowed = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports += len(node.names)
        elif isinstance(node, ast.ImportFrom):
            imports += 1
        elif isinstance(node, _FUNCTIONS):
            symbols.append(
                Symbol(
                    name=node.name,
                    kind="function",
                    line=node.lineno,
                    lines=_span(node),
                    params=_params(node),
                    complexity=_complexity(node),
                    depth=_depth(node),
                )
            )
        elif isinstance(node, ast.ClassDef):
            methods = sum(isinstance(n, _FUNCTIONS) for n in node.body)
            symbols.append(
                Symbol(
                    name=node.name,
                    kind="class",
                    line=node.lineno,
                    lines=_span(node),
                    methods=methods,
                )
            )
        elif isinstance(node, ast.ExceptHandler) and _swallows(node):
            swallowed += 1
    comments = python_comments(text)
    counts = {
        "suppressions": sum(bool(_PY_SUPPRESSION.search(c)) for c in comments),
        "any_types": 0,
        "todos": sum(bool(_TODO.search(c)) for c in comments),
        "skipped_tests": len(_PY_SKIP.findall(_without_comments(text, comments))),
        "focused_tests": 0,
        "swallowed_errors": swallowed,
    }
    definitions = sum(isinstance(n, (*_FUNCTIONS, ast.ClassDef)) for n in tree.body)
    return FileMetrics(
        lines=len(text.splitlines()),
        imports=imports,
        definitions=definitions,
        symbols=tuple(sorted(symbols, key=lambda s: s.line)),
        modules=tuple(dict.fromkeys(_runtime_imports(tree))),
        counts=counts,
    )


def _without_comments(text: str, comments: list[str]) -> str:
    for comment in comments:
        text = text.replace(comment, "", 1)
    return text


# --- JavaScript / TypeScript ------------------------------------------------------

_JS_SUPPRESSION = re.compile(r"eslint-disable|@ts-ignore|@ts-expect-error|@ts-nocheck|biome-ignore")
_JS_ANY = re.compile(r":\s*any\b|\bas\s+any\b|<any>|\bany\[\]")
_JS_SKIP = re.compile(r"\b(?:it|test|describe)\.skip\s*\(|\bx(?:it|test|describe)\s*\(")
_JS_ONLY = re.compile(r"\b(?:it|test|describe)\.only\s*\(|\bf(?:it|describe)\s*\(")
_JS_EMPTY_CATCH = re.compile(r"\bcatch\b\s*(?:\([^)]*\))?\s*\{\s*\}")
_JS_IMPORT_STMT = re.compile(r"^\s*(?:import\b|export\s+(?:\*|\{)[^;]*?\bfrom\b)", re.M)
_JS_REQUIRE = re.compile(r"\brequire\s*\(")
_JS_SPECIFIER = re.compile(
    r"(?:\b(?:import|export)\s+(?P<type>type\s+)?(?:[^;'\"]*?\bfrom\s*)?"
    r"|\brequire\s*\(\s*|\bimport\s*\(\s*)(?P<q>['\"])(?P<spec>[^'\"]*)(?P=q)"
)
_JS_FUNCTION = re.compile(r"\bfunction\b\s*\*?\s*(?P<name>[A-Za-z_$][\w$]*)?\s*(?:<[^>()]*>)?\s*\(")
_JS_ARROW = re.compile(
    r"(?:\b(?:const|let|var)\s+)?(?P<name>[A-Za-z_$][\w$]*)\s*(?::\s*[^=;{}]+?)?\s*[=:]\s*"
    r"(?:async\s*)?(?:<[^>()]*>\s*)?(?P<params>\((?:[^()]|\([^()]*\))*\)|[A-Za-z_$][\w$]*)"
    r"\s*(?::\s*[^=;{}]+?)?\s*=>\s*(?P<open>[{(])"
)
# A method starts a line or follows a `{`, `}`, `;` or `,` -- the second half is what
# lets `class K { a() {} b() {} }` and an object literal on one line count.
_JS_METHOD = re.compile(
    r"(?:^|(?<=[{};,]))[ \t]*(?:(?:public|private|protected|static|async|readonly|override|get|set)\s+)*"
    r"(?P<name>[A-Za-z_$][\w$]*)\s*(?:<[^>()]*>)?\s*(?P<params>\((?:[^()]|\([^()]*\))*\))"
    r"\s*(?::\s*[^{;=]+?)?\s*\{",
    re.M,
)
_JS_CLASS = re.compile(r"\bclass\s+(?P<name>[A-Za-z_$][\w$]*)[^{;]*\{")
_JS_CONTROL = frozenset({"if", "for", "while", "switch", "catch", "with"})
_JS_BARE_CONTROL = frozenset({"else", "try", "finally", "do"})
_JS_NOT_A_METHOD = _JS_CONTROL | _JS_BARE_CONTROL | {"function", "return", "class", "new", "typeof"}
_JS_BRANCH = re.compile(r"\b(?:if|for|while|case|catch)\b|&&|\|\||\?\?|\?(?![.?])")
_REGEX_PRECEDERS = frozenset("(,=:[!&|?{};+-*%<>~^")
_REGEX_KEYWORDS = frozenset({"return", "typeof", "case", "do", "else", "in", "of", "throw"})


def mask_literals(text: str) -> tuple[str, list[str]]:
    """`text` with every string, template, regex and comment blanked, plus the comments.

    Delimiters stay so the result lines up with the original character for character;
    only the interiors become spaces, and newlines survive so line numbers hold.
    """
    out = list(text)
    comments: list[str] = []
    i, n = 0, len(text)
    template_depths: list[int] = []  # brace depth at which each open `${` began
    depth = 0

    def blank(start: int, end: int) -> None:
        for k in range(start, end):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            end = text.find("\n", i)
            end = n if end < 0 else end
            comments.append(text[i:end])
            blank(i, end)
            i = end
        elif ch == "/" and nxt == "*":
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
            comments.append(text[i:end])
            blank(i, end)
            i = end
        elif ch in "'\"":
            end = _string_end(text, i, ch)
            blank(i + 1, end - 1)
            i = end
        elif ch == "`":
            i = _template(text, i, blank, template_depths, depth)
        elif ch == "}" and template_depths and template_depths[-1] == depth:
            template_depths.pop()
            i = _template(text, i, blank, template_depths, depth)
        elif ch == "/" and _regex_follows(text, i):
            end = _regex_end(text, i)
            blank(i + 1, end - 1)
            i = end
        else:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            i += 1
    return "".join(out), comments


def _string_end(text: str, start: int, quote: str) -> int:
    i = start + 1
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == quote or ch == "\n":
            return i + 1
        i += 1
    return len(text)


def _template(text: str, start: int, blank, depths: list[int], depth: int) -> int:
    """Blank a template literal from `start` (a backtick or the `}` closing a `${`)
    until its end or the next `${`, whose expression is left as code."""
    i = start + 1
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "`":
            blank(start + 1, i)
            return i + 1
        if ch == "$" and text[i + 1 : i + 2] == "{":
            blank(start + 1, i)
            depths.append(depth)
            return i + 2
        i += 1
    blank(start + 1, len(text))
    return len(text)


def _regex_follows(text: str, i: int) -> bool:
    j = i - 1
    while j >= 0 and text[j] in " \t":
        j -= 1
    if j < 0 or text[j] == "\n":
        return True
    if text[j] in _REGEX_PRECEDERS:
        return True
    k = j
    while k >= 0 and (text[k].isalnum() or text[k] == "_"):
        k -= 1
    return text[k + 1 : j + 1] in _REGEX_KEYWORDS


def _regex_end(text: str, start: int) -> int:
    i, in_class = start + 1, False
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "\n":
            return i
        if ch == "[":
            in_class = True
        elif ch == "]":
            in_class = False
        elif ch == "/" and not in_class:
            return i + 1
        i += 1
    return len(text)


def _match_bracket(text: str, start: int) -> int:
    """Index just past the bracket matching the one at `start`, or -1 if unbalanced."""
    pairs = {"{": "}", "(": ")", "[": "]"}
    close = pairs[text[start]]
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch in pairs:
            depth += 1
        elif ch in ")}]":
            depth -= 1
            if depth == 0:
                return i + 1 if ch == close else -1
    return -1


def _depth_map(text: str) -> list[int]:
    """Brace depth at every index, so 'top level' is a lookup rather than a rescan."""
    depths = [0] * (len(text) + 1)
    depth = 0
    for i, ch in enumerate(text):
        depths[i] = depth
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
    depths[len(text)] = depth
    return depths


def _count_params(inner: str) -> int:
    if not inner.strip():
        return 0
    depth, count = 0, 1
    for ch in inner:
        if ch in "([{<":
            depth += 1
        elif ch in ")]}>":
            depth -= 1
        elif ch == "," and depth == 0:
            count += 1
    return count - (1 if inner.rstrip().endswith(",") else 0)


def _word_before(text: str, i: int) -> str:
    j = i - 1
    while j >= 0 and text[j] in " \t\n":
        j -= 1
    if j >= 0 and text[j] == ")":
        k = j
        depth = 0
        while k >= 0:
            if text[k] == ")":
                depth += 1
            elif text[k] == "(":
                depth -= 1
                if depth == 0:
                    break
            k -= 1
        j = k - 1
        while j >= 0 and text[j] in " \t\n":
            j -= 1
    k = j
    while k >= 0 and (text[k].isalnum() or text[k] in "_$"):
        k -= 1
    return text[k + 1 : j + 1]


def _control_depth(body: str) -> int:
    stack: list[bool] = []
    deepest = 0
    for i, ch in enumerate(body):
        if ch == "{":
            word = _word_before(body, i)
            control = word in _JS_CONTROL or word in _JS_BARE_CONTROL
            stack.append(control)
            deepest = max(deepest, sum(stack))
        elif ch == "}" and stack:
            stack.pop()
    return deepest


@dataclass(frozen=True)
class _Fn:
    name: str
    start: int
    body_start: int
    end: int
    params: int


def _js_functions(masked: str) -> list[_Fn]:
    found: dict[int, _Fn] = {}
    for m in _JS_FUNCTION.finditer(masked):
        paren = m.end() - 1
        close = _match_bracket(masked, paren)
        if close < 0:
            continue
        brace = masked.find("{", close)
        if brace < 0:
            continue
        end = _match_bracket(masked, brace)
        if end > 0:
            name = m.group("name") or "<anonymous>"
            found[brace] = _Fn(
                name, m.start(), brace, end, _count_params(masked[paren + 1 : close - 1])
            )
    for m in _JS_ARROW.finditer(masked):
        open_at = m.end() - 1
        end = _match_bracket(masked, open_at)
        if end < 0 or open_at in found:
            continue
        params = m.group("params")
        count = _count_params(params[1:-1]) if params.startswith("(") else 1
        found[open_at] = _Fn(m.group("name"), m.start(), open_at, end, count)
    for m in _JS_METHOD.finditer(masked):
        name = m.group("name")
        brace = m.end() - 1
        if name in _JS_NOT_A_METHOD or brace in found:
            continue
        end = _match_bracket(masked, brace)
        if end > 0:
            found[brace] = _Fn(name, m.start(), brace, end, _count_params(m.group("params")[1:-1]))
    return sorted(found.values(), key=lambda f: f.start)


def _own_body(fn: _Fn, functions: list[_Fn], masked: str) -> str:
    """The function's body with every nested function's body blanked out."""
    body = list(masked[fn.body_start : fn.end])
    for other in functions:
        if other is fn or other.body_start <= fn.body_start or other.end > fn.end:
            continue
        for k in range(other.body_start - fn.body_start, other.end - fn.body_start):
            if body[k] != "\n":
                body[k] = " "
    return "".join(body)


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def scan_js(text: str, react: bool = False) -> FileMetrics:
    """Measure one JavaScript or TypeScript module. `react` marks a `.jsx`/`.tsx`
    file, where a capitalised top-level function is a component."""
    masked, comments = mask_literals(text)
    depths = _depth_map(masked)
    functions = _js_functions(masked)
    symbols: list[Symbol] = []
    for fn in functions:
        body = _own_body(fn, functions, masked)
        lines = _line_of(masked, fn.end - 1) - _line_of(masked, fn.start) + 1
        is_component = react and fn.name[:1].isupper() and depths[fn.start] == 0
        symbols.append(
            Symbol(
                name=fn.name,
                kind="component" if is_component else "function",
                line=_line_of(masked, fn.start),
                lines=lines,
                params=fn.params,
                complexity=1 + len(_JS_BRANCH.findall(body)),
                depth=_control_depth(body[1:-1]),
            )
        )
    top_level = sum(1 for fn in functions if depths[fn.start] == 0 and fn.name != "<anonymous>")
    for m in _JS_CLASS.finditer(masked):
        brace = m.end() - 1
        end = _match_bracket(masked, brace)
        if end < 0:
            continue
        methods = sum(
            1
            for fn in functions
            if brace < fn.start < end and depths[fn.start] == depths[brace] + 1
        )
        symbols.append(
            Symbol(
                name=m.group("name"),
                kind="class",
                line=_line_of(masked, m.start()),
                lines=_line_of(masked, end - 1) - _line_of(masked, m.start()) + 1,
                methods=methods,
            )
        )
        if depths[m.start()] == 0:
            top_level += 1
    modules: list[str] = []
    for m in _JS_SPECIFIER.finditer(masked):
        if m.group("type"):
            continue
        spec = text[m.start("spec") : m.end("spec")]
        if spec:
            modules.append(spec)
    comment_text = "\n".join(comments)
    counts = {
        "suppressions": len(_JS_SUPPRESSION.findall(comment_text)),
        "any_types": len(_JS_ANY.findall(masked)),
        "todos": len(_TODO.findall(comment_text)),
        "skipped_tests": len(_JS_SKIP.findall(masked)),
        "focused_tests": len(_JS_ONLY.findall(masked)),
        "swallowed_errors": len(_JS_EMPTY_CATCH.findall(masked)),
    }
    return FileMetrics(
        lines=len(text.splitlines()),
        imports=len(_JS_IMPORT_STMT.findall(masked)) + len(_JS_REQUIRE.findall(masked)),
        definitions=top_level,
        symbols=tuple(sorted(symbols, key=lambda s: s.line)),
        modules=tuple(dict.fromkeys(modules)),
        counts=counts,
        react_state=len(re.findall(r"\buseState\s*[<(]", masked)) if react else 0,
        react_effects=len(re.findall(r"\buse(?:Layout)?Effect\s*\(", masked)) if react else 0,
    )


def scan(rel: str, text: str) -> FileMetrics | None:
    """Dispatch on suffix. `None` for a language the scanner does not read, or a
    Python file that does not parse."""
    suffix = PurePosixPath(rel).suffix
    if suffix in PY_SUFFIXES:
        return scan_python(text)
    if suffix in JS_SUFFIXES:
        return scan_js(text, react=suffix in REACT_SUFFIXES)
    return None
