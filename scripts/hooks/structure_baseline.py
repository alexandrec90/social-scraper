"""The `.devkit-structure.txt` file itself: its text, and how to read one back.

Cut out of `structure_check.py`, which had recorded a `file_lines` raise on three
separate branches -- 1042, 1050, 1078 -- each one saying in its own note that splitting
the module was its own change. `.claude/rules/engineering.md` calls the third of those a
defect report rather than a raise, so the fourth one is this file instead. What decided
the seam is that everything here answers a question about **the file on disk**, and
nothing here needs `findings()`: the scanner, the rules and the ratchet's policy all sit
on one side of that line and the serialisation sits on the other, which is why the
import goes one way and the ratchet keeps the CLI.

The format, in one place so the reader and the writer cannot disagree about it:

    <header comment block>
    <rule>::<path>[::<symbol>] = <int>      one line each, sorted
    <blank>
    # --- recorded growth ---                `RECORD_MARKER`
    <blank>
    # <date>  recorded: <reason>             one note per `--record`, blank-separated
    #   <key> -> <value>

Every line that is not `key = int` is a comment, so a reader that only wants the numbers
gets them by dropping `#` lines, and the `--record` log rides in the same file rather
than in a second one that could disagree with the baseline it annotates.

Stdlib only and pure apart from reading the file it is handed -- this is imported by
`structure_check.py`, which runs in a pre-commit hook before any virtualenv exists.
Vendored: it is in `sync-devkit.py`'s `MANIFEST` beside the module it was cut from, and
the two arrive in one `--pull` because a consumer with only the older one gets an
`ImportError` inside a hook. Tested in `scripts/hooks/tests/test_structure_baseline.py`.
"""

from __future__ import annotations

from pathlib import Path

BASELINE_NAME = ".devkit-structure.txt"

BASELINE_HEADER = """\
# Structural findings the code already had when it adopted the gate.
#
# Debt, not configuration. This file SHRINKS on its own: a new key fails the gate, a
# value that grew fails the gate, and a line the code no longer earns fails it too,
# so fixing a finding forces its line out (`structure_check.py --tighten` does that).
# Never add or raise a line to make new code pass; fix the code. `dependency::` is one
# exception -- adding a package is a line here, so the PR diff shows it.
#
# `--record --reason "..."` is the other, and it is the deliberate hole. A function far
# past its limit cannot gain a line, so a bug whose fix belongs in one has nowhere to
# go, and the refactor the finding asks for is a separate body of work. Recording says
# so out loud: the reason is mandatory, counter rules (`suppressions`, `todos`,
# `skipped_tests`, ...) are refused because those are always somebody giving up rather
# than a unit's size, and every move is logged below with what it moved and why.
# Module size -- `file_lines`, `imports`, `definitions` -- is advisory and never here.
#
# Regenerate only when adopting the gate: python scripts/hooks/structure_check.py --seed
"""

# What `existing_notes` reads back, and what separates the machine-readable lines above
# it from the `--record` log below. Both halves are comments to `read_baseline`.
RECORD_MARKER = "# --- recorded growth ------------------------------------------------\n"

# Printed under a size-limit finding. A fixer spent 7 calls and deleted docstring prose to
# land a fix at net zero, because the report said only "split it" (that was a module-size
# finding, advisory since); and a split it did make then failed `untested_symbols`. Both
# routes, and the one ceiling on the second: `.claude/rules/engineering.md` calls a limit
# raised on three branches in a row a defect report, and the fourth is the refactor.
SIZE_HINT = (
    "  A size limit (not a counter) may instead be recorded when the refactor is its own "
    'body of work: --record --reason "<why this growth is honest>" -- unless this limit '
    "was already raised on the last three branches, when the refactor is due now. Any "
    "helper a refactor creates needs a test that names it (untested_symbols)."
)


def baseline_path(root: Path) -> Path:
    return root / BASELINE_NAME


def read_baseline(path: Path) -> dict[str, int]:
    """`key -> value`; comments, blanks and malformed lines dropped."""
    if not path.exists():
        return {}
    out: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.rpartition(" = ")
        if sep and value.strip().isdigit():
            out[key.strip()] = int(value)
    return out


def render_baseline(
    entries: dict[str, int], notes: list[str] | None = None, header: str | None = None
) -> str:
    """The whole file: header, the sorted findings, then the `--record` log.

    The log goes *after* the entries so the sorted block stays a clean diff -- a note
    inserted between two lines would move every line under it. `header` is the file's
    own (`existing_header`); `None` means a new file, which gets `BASELINE_HEADER`.
    """
    top = BASELINE_HEADER if header is None else header
    body = top + "".join(f"{k} = {entries[k]}\n" for k in sorted(entries))
    if not notes:
        return body
    return body + "\n" + RECORD_MARKER + "\n" + "\n\n".join(notes) + "\n"


def existing_header(path: Path) -> str | None:
    """The comment block the file opens with, verbatim; `None` when there is no file.

    `--tighten` and `--record` rewrite the file, and used to write `BASELINE_HEADER` over
    whatever header it had. The header is the project's copy of the text it was seeded
    with, so once devkit reworded it, a tighten that only dropped a stale `dependency::`
    line in a consumer also carried a paragraph of unrelated prose in its diff. A rewrite
    changes the numbers and the log; the prose above them is left as it was found.
    """
    if not path.exists():
        return None
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    head: list[str] = []
    for line in lines:
        if not line.startswith("#") or line.startswith(RECORD_MARKER.strip()):
            break
        head.append(line if line.endswith("\n") else line + "\n")
    return "".join(head)


def existing_notes(path: Path) -> list[str]:
    """The `--record` notes already in the file, so a later one appends rather than wins.

    Read back out of the file rather than kept beside it: a second state file would be
    one more thing that can disagree with the baseline it annotates, and the whole point
    of this tier is that one file is the record.
    """
    if not path.exists():
        return []
    body = path.read_text(encoding="utf-8")
    _, marker, tail = body.partition(RECORD_MARKER)
    if not marker:
        return []
    blocks = [b.strip("\n") for b in tail.split("\n\n") if b.strip()]
    return [b for b in blocks if b.startswith("#")]


def reason_lines(reason: str, stamp: str) -> list[str]:
    """`reason`, dated and turned into comment lines -- **every** line of it.

    A multi-line reason is the natural thing to pass to `--record`, because the clause
    that escape hatch satisfies (`.claude/rules/engineering.md`: a ceiling raised three
    times is a defect report) asks for a measured account of what is filling the file.
    Only the first line used to be prefixed, so the rest landed in the baseline as bare
    English. `read_baseline` drops what it cannot parse, so the write succeeded and
    `--record` printed `structure-check: clean`; the damage surfaced later and
    elsewhere, as `test_the_baseline_is_sorted_and_unique` failing with a sentence of
    prose at index 0, and recovery was `git checkout --` the baseline and a re-record
    with the reason collapsed onto one line.

    Prefixed rather than refused: the reason is the artifact this hole trades for, and a
    tool that rejects the more considered of two answers teaches everyone to give the
    shorter one.

    A blank line inside the reason becomes `#`, not an empty line, on purpose --
    `existing_notes` partitions the log on blank lines, so a truly empty one would split
    one note into two and the second would not read back.
    """
    head, *rest = reason.strip().splitlines()
    return [f"# {stamp}  recorded: {head}"] + [f"#   {line.strip()}".rstrip() for line in rest]
