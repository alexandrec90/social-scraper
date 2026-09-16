#!/usr/bin/env python3
"""Which public callables no test names, as a debt list that may only shrink.

Every contract test in a project asserts something about code that someone remembered
to cover. Nothing asserts that the remembering happened, so "every new unit of logic
ships with its tests" -- `.claude/rules/engineering.md`, in those words -- is a
preference with a green suite behind it. In the repo this was written for, three
scripts had no test at all and the suite had been green for years.

Naming is a proxy for testing and a weak one: a test can name a function and assert
nothing about it. It is a proxy with **no false negatives**, which is the property that
makes it worth gating on -- a symbol that no relevant test mentions is certainly
untested, whatever else is true.

## What it looks at, and why the scoping is what it is

The corpus for a given module is **the test files that reference that module**, not
every test in the repo and not only the identically-named one.

- Searching the whole corpus passes `main`, `run`, `check` and `cap` everywhere, since
  many modules define those names; a hit says nothing about which one was exercised.
- Searching only a matching `test_<stem>.py` fails work that is correctly covered from
  a sibling -- routing logic tested through the guard that calls it, a helper covered
  by the endpoint test that exercises it.

A *reference* is a call, an attribute access, or an import. Never a bare substring,
which `cap` satisfies inside `capsys`.

## The baseline is debt, not configuration

`.devkit-untested.txt` sits beside `.devkit.toml`, is per-project, and is deliberately
**not vendored**: its content is a fact about one repo. Two rules make it a ratchet
rather than a config file, and `tests/test_untested_symbols.py` enforces both:

- A symbol not in it fails. That is the half that bites in review.
- A line in it that is no longer a gap **also** fails, so covering a symbol forces its
  line out and the file can only shrink. Without that the file is write-only, and the
  next person to delete a test finds the gate already looking the other way.

`sync-devkit.py --pull` seeds the file when a project first adopts the gate, so
adoption and recording the existing debt are one act. Seeding an *existing* file is
refused -- that would launder new untested code into the debt list, which is the one
way this check can be defeated without anyone deciding to defeat it.
"""

from __future__ import annotations

import argparse
import ast
import io
import re
import sys
import tokenize
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness_config

REPO_ROOT = (Path(__file__).parent / "../..").resolve()
CFG = harness_config.load(REPO_ROOT)

BASELINE_NAME = ".devkit-untested.txt"

# Directories that hold no first-party source in any project. Anything beyond this is
# project shape and comes from `[test_contract] exclude` in `.devkit.toml` -- generated
# migrations being the usual one.
TOOLING_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".worktrees",
    }
)

BASELINE_HEADER = """\
# Public callables that no test naming their module references.
#
# Debt, not configuration. This file may only SHRINK: a symbol missing from it fails
# the gate, and so does a line here that is no longer a gap -- so covering a symbol
# forces its line out. Never add a line to make a new symbol pass; write the test.
#
# Regenerate only when adopting the gate: python scripts/hooks/untested_symbols.py --seed
"""


def _excluded(rel: Path, extra: frozenset[str]) -> bool:
    return any(part in TOOLING_DIRS or part in extra for part in rel.parts)


def source_dirs(cfg: harness_config.Config) -> tuple[str, ...]:
    """The trees whose public API has to be covered, deduped and in a stable order.

    The application code and the project's own tooling. `scripts/` is included even
    where it is not the app dir because it is the part of a repo that no framework
    test touches: nothing renders it, nothing hits it over HTTP, and it is where a
    project keeps the code that moves its data around.
    """
    configured = cfg.test_contract.sources
    if configured:
        return configured
    ordered = [cfg.app_dir, "scripts/"]
    seen: list[str] = []
    for entry in ordered:
        normalised = entry.rstrip("/") or "."
        if normalised not in seen:
            seen.append(normalised)
    return tuple(seen)


def source_files(root: Path, cfg: harness_config.Config) -> list[Path]:
    """Every module whose public API the gate covers, relative to `root`.

    Skipped, and each for a different reason: `test_*.py` and `conftest.py` are the
    tests themselves; `__init__.py` is usually re-exports, and where it is not, the
    thing it defines is covered where it is defined; a leading underscore is the
    author saying "not the API", and the public function calling it is on the hook for
    its behaviour; and anything under a `tests` directory belongs to the corpus, not to
    the sources -- without that, a vendored tier that keeps its tests inside the source
    tree audits its own test helpers.
    """
    extra = frozenset(cfg.test_contract.exclude)
    found: list[Path] = []
    for directory in source_dirs(cfg):
        base = root / directory
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            rel = path.relative_to(root)
            if _excluded(rel, extra) or "tests" in rel.parts:
                continue
            if path.name.startswith(("_", "test_")) or path.name == "conftest.py":
                continue
            found.append(rel)
    return found


def test_files(root: Path, cfg: harness_config.Config) -> list[Path]:
    """Every `test_*.py` that can vouch for a source module, relative to `root`.

    Scoped to the configured test directory plus the source trees, rather than to the
    whole repo: a generator's *templates* contain test files for the projects it emits,
    and those must never vouch for the generator's own code.
    """
    extra = frozenset(cfg.test_contract.exclude)
    roots = [cfg.tests_dir.rstrip("/") or ".", *source_dirs(cfg)]
    found: dict[Path, None] = {}
    for directory in roots:
        base = root / directory
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("test_*.py")):
            rel = path.relative_to(root)
            if not _excluded(rel, extra):
                found[rel] = None
    return sorted(found)


def public_symbols(source: str) -> list[str]:
    """Public top-level functions and classes defined in `source`, in file order.

    `main` is not exempt: argv handling and exit codes are what a harness depends on,
    and they are the part that breaks silently. Read with `ast` rather than by
    importing, because importing 50 modules to enumerate them runs 50 modules.
    """
    tree = ast.parse(source)
    definition = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    return [n.name for n in tree.body if isinstance(n, definition) and not n.name.startswith("_")]


def reference_pattern(symbol: str) -> re.Pattern[str]:
    """Matches a real reference to `symbol`: an attribute, a call, or an import.

    The definition of a reference. `referenced_names` is the same three shapes read the
    other way round -- every name a text references, in one pass -- and the scan uses
    that; `test_referenced_names_agrees_with_reference_pattern` holds the two together.
    """
    name = re.escape(symbol)
    return re.compile(
        rf"\.{name}\b"  # module.symbol
        rf"|(?<![\w.]){name}\s*\("  # symbol(...) after a from-import
        rf"|^\s*from\s+.*\bimport\b.*\b{name}\b",  # from mod import symbol
        re.MULTILINE,
    )


# The three shapes of `reference_pattern`, each capturing the name it would have been
# asked about. `\w+` is greedy, so the captured word is the maximal identifier -- which
# is exactly what the `\b` on the symbol side of `reference_pattern` demands.
_ATTRIBUTE_RE = re.compile(r"\.(\w+)")
_CALL_RE = re.compile(r"(?<![\w.])(\w+)\s*\(")
_FROM_IMPORT_RE = re.compile(r"^\s*from\s+.*?\bimport\b(.*)", re.MULTILINE)
_WORD_RE = re.compile(r"\w+")


def referenced_names(text: str) -> frozenset[str]:
    """Every symbol `reference_pattern` would find in `text`.

    Computed once per test file rather than once per (symbol, corpus) pair. The scan
    used to run `reference_pattern(symbol).search(corpus)` for every public symbol in
    the repo -- fifteen hundred regex passes over corpora that, for a module every test
    imports, are most of the test tree -- and took 25s per verdict on a 2.5 MB corpus,
    which the live gate then paid four times over. Reading each file once for the
    names it references and taking a set union per module answers the same question
    in well under a second.
    """
    names: set[str] = set(_ATTRIBUTE_RE.findall(text))
    names.update(_CALL_RE.findall(text))
    for rest_of_line in _FROM_IMPORT_RE.findall(text):
        names.update(_WORD_RE.findall(rest_of_line))
    return frozenset(names)


def module_pattern(module: Path) -> re.Pattern[str]:
    """Matches a test file's mention of `module`, in a spelling that names the module.

    A hyphenated script is loaded by path (`'scripts/sync-devkit.py'`) and imported
    under an underscored name (`sync_devkit`); a test may use either, and a test using
    neither is not the one covering it.

    The four accepted spellings are the four ways a test can actually reach a module:
    the file name, an `import`, an attribute off it, and a binding of it to a name. A
    **bare stem anywhere in the file** used to count, and that is a substring match with
    a word boundary painted on: `test_run_tests.py` asserting `'--ignore=tests/local_e2e'`
    pulled that whole file into `scripts/local-e2e.py`'s corpus, where an unrelated
    `rt.main(` then satisfied `reference_pattern('main')` and `local-e2e.py::main` read
    as covered. That is a **false negative** in the debt list -- the one failure mode
    this module's opening claims it does not have -- and the ratchet turns it into
    pressure to delete a real gap from the baseline as "now covered".

    The file-name spelling is still a plain substring, because that is how a test
    loading the module by path spells it. It is matched against the file's **code**,
    never its docstrings or comments: `read_tests` strips both through `code_only`
    before any pattern sees the text. A docstring explaining that a sibling script is
    *not* what a file tests used to put that file in the sibling's corpus, where an
    unrelated `.main()` then read as coverage of the sibling's `main` -- the same false
    "now covered" verdict as the bare-stem match above, reached through prose instead
    of a path, and it cost a reporter three diagnostic cycles because the prose moved
    with the tests when they were split into another file.
    """
    filename = re.escape(module.name)
    snake = re.escape(module.stem.replace("-", "_"))
    return re.compile(
        rf"\b{filename}\b"  # path spelling: 'scripts/sync-devkit.py'
        rf"|^\s*(?:import|from)\s+.*\b{snake}\b"  # import sync_devkit / from x import ...
        rf"|\b{snake}\s*\."  # sync_devkit.main
        rf"|\b{snake}\b\s*=",  # sync_devkit = load(...)
        re.MULTILINE,
    )


def corpus_files(module: Path, texts: dict[Path, str]) -> list[Path]:
    """The test files that mention `module`, in the order `texts` lists them.

    The substring test in front of the regex is a **necessary condition of every
    alternative** {@link module_pattern} accepts -- each one contains either the file
    name or the underscored stem literally -- so it can only skip a file the pattern
    would have rejected anyway, and `test_the_prefilter_cannot_skip_a_real_mention`
    pins that.

    It is here because this scan is quadratic and the regex is the expensive half:
    every module is searched against every test file, and `.*\\b<stem>\\b` under
    `re.MULTILINE` backtracks per line. On a repo with 235 modules and 166 test files
    -- 1.4 MB of corpus, which is nothing -- the whole scan took 60s and tripped the
    60s timeout on `test_every_public_symbol_is_named_by_a_test`; a `in` check that
    settles the same question for most pairs brings it to about 4s. A timing-out gate
    is not a slow gate, it is a red one, and the failure names a regex rather than the
    coverage it was asked about.
    """
    mentions = module_pattern(module)
    name = module.name
    snake = module.stem.replace("-", "_")
    return [
        rel
        for rel, text in texts.items()
        if (name in text or snake in text) and mentions.search(text)
    ]


def corpus_for(module: Path, texts: dict[Path, str]) -> str:
    """The text of every test file that mentions `module`, concatenated."""
    return "\n".join(texts[rel] for rel in corpus_files(module, texts))


def entry(module: Path, symbol: str) -> str:
    """The baseline key for one symbol. Posix, so the file is identical on any OS."""
    return f"{module.as_posix()}::{symbol}"


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


def read_tests(root: Path, cfg: harness_config.Config) -> dict[Path, str]:
    """The corpus, as `{relative path: text}`, read once for the whole scan.

    Each file is reduced to its code by `code_only` here, at the one place the corpus
    is read, so every pattern downstream -- `module_pattern`, `reference_pattern` --
    sees the same text and none can be reached through a docstring.
    """
    return {
        rel: code_only((root / rel).read_text(encoding="utf-8")) for rel in test_files(root, cfg)
    }


def gaps(root: Path, cfg: harness_config.Config, texts: dict[Path, str] | None = None) -> list[str]:
    """Baseline keys for every public symbol no test naming its module references.

    `texts` overrides the corpus, which is how the gate's own test checks that this
    module's fixtures vouch for nothing: scan once with them and once without, and the
    answer has to be the same.
    """
    if texts is None:
        texts = read_tests(root, cfg)
    referenced = {rel: referenced_names(text) for rel, text in texts.items()}
    found: list[str] = []
    for module in source_files(root, cfg):
        try:
            symbols = public_symbols((root / module).read_text(encoding="utf-8"))
        except SyntaxError:
            # Not this gate's job to report: the linter and the interpreter both say so
            # louder. Skipping keeps a broken file from masking every other module.
            continue
        names: set[str] = set()
        for rel in corpus_files(module, texts):
            names |= referenced[rel]
        found.extend(entry(module, symbol) for symbol in symbols if symbol not in names)
    return sorted(found)


def baseline_path(root: Path) -> Path:
    return root / BASELINE_NAME


def read_baseline(path: Path) -> list[str]:
    """The recorded gaps: comments and blank lines dropped, order preserved."""
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]


def render_baseline(entries: list[str]) -> str:
    """Sorted and deduped, so two branches adding a line conflict in git instead of
    merging into a duplicate, and a burn-down diff is readable."""
    return BASELINE_HEADER + "\n".join(sorted(set(entries))) + "\n"


def seed(root: Path, cfg: harness_config.Config | None = None) -> int | None:
    """Write the baseline for a project adopting the gate. `None` if one already exists.

    Refusing to overwrite is the whole safety property. Re-seeding an existing file
    would launder every symbol someone had just failed to test into the debt list, and
    the gate would go green having lost exactly the finding it exists to make.
    """
    path = baseline_path(root)
    if path.exists():
        return None
    entries = gaps(root, cfg or harness_config.load(root))
    # `newline="\n"`, because the default translates on Windows and the file is
    # committed: a CRLF baseline fails the consumer's own mixed-line-ending hook on the
    # very commit that adopts the gate, which reads as the gate being broken.
    path.write_text(render_baseline(entries), encoding="utf-8", newline="\n")
    return len(entries)


def verdict(root: Path, cfg: harness_config.Config) -> tuple[list[str], list[str]]:
    """`(uncovered, stale)` -- symbols missing from the baseline, and lines no longer
    true. Both empty is the passing state; either one non-empty fails the gate."""
    current = set(gaps(root, cfg))
    recorded = set(read_baseline(baseline_path(root)))
    return sorted(current - recorded), sorted(recorded - current)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seed",
        action="store_true",
        help=f"write {BASELINE_NAME} when adopting the gate; refuses to overwrite",
    )
    parser.add_argument("--list", action="store_true", help="print every gap, covered or not")
    args = parser.parse_args(argv)

    if args.seed:
        count = seed(REPO_ROOT, CFG)
        if count is None:
            print(f"untested-symbols: {BASELINE_NAME} already exists; refusing to overwrite.")
            return 1
        print(f"untested-symbols: recorded {count} untested symbol(s) in {BASELINE_NAME}.")
        return 0

    if args.list:
        for key in gaps(REPO_ROOT, CFG):
            print(key)
        return 0

    uncovered, stale = verdict(REPO_ROOT, CFG)
    for key in uncovered:
        print(f"untested: {key}")
    for key in stale:
        print(f"now covered, so {BASELINE_NAME} must drop the line: {key}")
    if uncovered or stale:
        return 1
    print(f"untested-symbols: clean ({len(read_baseline(baseline_path(REPO_ROOT)))} known gap(s)).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
