#!/usr/bin/env python3
"""A Python file's code with its prose blanked, so only code can be read out of it.

Cut out of `untested_symbols.py`, which was at its `definitions` and `file_lines`
ceilings: this is the one half of that scan that knows about tokens, and it answers a
question about **a file's text** without knowing what a corpus, a symbol or a baseline
is. The import goes one way -- `untested_symbols.read_tests` calls `code_only` on every
test file before any pattern sees it.

Stdlib only: imported by a vendored gate that runs before any virtualenv exists.
Vendored beside `untested_symbols.py` in `sync-devkit.py`'s `MANIFEST`, and the two arrive
in one `--pull`, because a consumer holding only the importer gets an `ImportError` inside
its gate. Tested in `scripts/hooks/tests/test_code_text.py`.
"""

from __future__ import annotations

import io
import re
import tokenize


# A string token is a docstring -- or a bare string statement, which is prose too -- when
# it opens a statement and nothing follows it on that statement.
_STATEMENT_START = frozenset({tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT})
_STATEMENT_END = frozenset({tokenize.NEWLINE, tokenize.ENDMARKER})
_NOT_CODE = frozenset({tokenize.COMMENT, tokenize.NL})


def code_only(text: str) -> str:
    """`text` with every docstring and comment blanked, so only code can name a module.

    Blanked rather than removed -- each is replaced by spaces of the same width, with
    its newlines kept -- so nothing else in the file moves. String literals that are
    not docstrings survive: the path a test passes to a loader is an argument, and it
    is exactly the spelling the corpus is scoped by. Read with the tokenizer rather than
    `ast`, because a docstring is a token-level fact (a string that opens a statement
    and ends it) and the tokenizer is what `structure_scan` already trusts for
    comments. Text it cannot finish is returned unchanged: a gate that raises on a
    broken test file reports the wrong thing, and the linter reports the right one.
    """
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, SyntaxError):
        return text
    lines = io.StringIO(text).readlines()  # split exactly as the tokenizer read it

    def blank(token: tokenize.TokenInfo) -> None:
        (start_line, start_col), (end_line, end_col) = token.start, token.end
        for number in range(start_line, end_line + 1):
            line = lines[number - 1]
            lo = start_col if number == start_line else 0
            hi = end_col if number == end_line else len(line.rstrip("\r\n"))
            lines[number - 1] = line[:lo] + " " * (hi - lo) + line[hi:]

    for token in tokens:
        if token.type == tokenize.COMMENT:
            blank(token)
    code = [token for token in tokens if token.type not in _NOT_CODE]
    previous = tokenize.NEWLINE
    for index, token in enumerate(code):
        following = code[index + 1].type if index + 1 < len(code) else tokenize.ENDMARKER
        if (
            token.type == tokenize.STRING
            and previous in _STATEMENT_START
            and following in _STATEMENT_END
        ):
            blank(token)
        previous = token.type
    return "".join(lines)


# Only the fallback for text the tokenizer cannot finish: a top-level `def`, `class` or
# decorator opens a scope, and a statement after a function joins it.
_SCOPE_RE = re.compile(r"^(?=@|(?:async[ \t]+)?(?:def|class)\b)", re.MULTILINE)
_OPENS_SCOPE = frozenset({"@", "def", "class", "async"})
_DEPTH = {tokenize.INDENT: 1, tokenize.DEDENT: -1}
_NOT_A_STATEMENT = _NOT_CODE | _DEPTH.keys() | {tokenize.NEWLINE, tokenize.ENDMARKER}


def _statement_starts(tokens: list[tokenize.TokenInfo]) -> list[tuple[int, bool]]:
    """The line index of each statement that opens at indentation zero, and whether it
    opens a definition; a decorator and the definition under it are one start."""
    starts = [(0, False)]
    depth, at_start, decorated = 0, True, False
    for token in tokens:
        depth += _DEPTH.get(token.type, 0)
        at_start = at_start or token.type == tokenize.NEWLINE
        if not at_start or token.type in _NOT_A_STATEMENT:
            continue
        at_start = False
        opens = token.string in _OPENS_SCOPE
        if depth == 0 and not (decorated and opens):
            starts.append((token.start[0] - 1, opens))
        decorated = depth == 0 and token.string == "@"
    return starts


def top_level_scopes(text: str) -> tuple[str, list[str]]:
    """`text` split into its module-level code and each top-level `def` or `class`,
    decorators included, as exact slices of its lines.

    7e14d6d3: splitting at every top-level `def` put `up = load_script("db-upgrade.py")`,
    written below `def load_script`, inside that function, so the module-level binding
    of `up` was never seen and every `up.x` vouched for every module the file named. Read
    with the tokenizer, every statement that opens at indentation zero and is not a
    definition is the module's, wherever it sits; a multi-line signature's `):` is a
    continuation, never a statement. Text the tokenizer cannot finish falls back to the
    old split, which can only leave a name unbound.
    """
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, SyntaxError):
        head, *tail = _SCOPE_RE.split(text)
        return head, tail
    lines = io.StringIO(text).readlines()
    starts = _statement_starts(tokens)
    ends = [line for line, _ in starts[1:]] + [len(lines)]
    blocks = [
        (opens, "".join(lines[line:end])) for (line, opens), end in zip(starts, ends, strict=True)
    ]
    return "".join(b for opens, b in blocks if not opens), [b for opens, b in blocks if opens]
