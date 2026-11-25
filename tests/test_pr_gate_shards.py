"""The PR gate's application suite runs as shards, and every test file runs in exactly one.

`.github/workflows/pr-gate.yml` splits `tests/test_*.py` across a matrix with
`printf '%s\\n' tests/test_*.py | awk '<program>'`, one program per shard value. A file
no shard's program selects is a file the gate never runs, and nothing else would say so:
every shard stays green. These tests evaluate each shard's program over the real file
list, the way the runner's `bash` and `awk` would, and fail on a missed or doubled file.

The evaluator accepts only the small grammar the workflow uses -- `||` of `&&` of
`NR % k == n`, `$0 == "path"`, `$0 != "path"` and `"a" == "b"` (the shard name compared
with a literal, once `${{ matrix.shard }}` is substituted) -- and refuses anything else,
so a program reshaped beyond it fails here rather than being half-understood.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / ".github" / "workflows" / "pr-gate.yml"

# The step that hands a shard's files to the runner. The awk program is single-quoted.
_SHARD_RUN = re.compile(
    r"""printf\s+'%s\\n'\s+tests/test_\*\.py\s*\|\s*awk\s+'(?P<program>[^']+)'\s*"""
    r"""\|\s*xargs\s+uv\s+run\s+python\s+scripts/run-tests\.py\s+--only"""
)
_MATRIX = re.compile(r"^\s*shard:\s*\[(?P<values>[^\]]*)\]\s*$", re.MULTILINE)
_MATRIX_REF = "${{ matrix.shard }}"

_NR_MOD = re.compile(r"NR\s*%\s*(\d+)\s*==\s*(\d+)")
_PATH_EQ = re.compile(r'\$0\s*(==|!=)\s*"([^"]+)"')
_LITERAL_EQ = re.compile(r'"([^"]*)"\s*(==|!=)\s*"([^"]*)"')


class UnsupportedProgram(ValueError):
    """The awk program left the grammar this module can evaluate."""


def shard_values(text: str) -> list[str]:
    """The values of the `shard:` matrix axis, in order."""
    match = _MATRIX.search(text)
    if match is None:
        raise UnsupportedProgram("no `shard: [...]` matrix in the gate")
    return [v.strip().strip("'\"") for v in match.group("values").split(",") if v.strip()]


def shard_program(text: str) -> str:
    """The awk program the shard step runs, `${{ matrix.shard }}` still in it."""
    found = _SHARD_RUN.findall(text)
    if len(found) != 1:
        raise UnsupportedProgram(f"expected one shard step, found {len(found)}")
    return found[0]


def _term(term: str, path: str, nr: int) -> bool:
    term = term.strip()
    if match := _NR_MOD.fullmatch(term):
        return nr % int(match.group(1)) == int(match.group(2))
    if match := _PATH_EQ.fullmatch(term):
        return (path == match.group(2)) == (match.group(1) == "==")
    if match := _LITERAL_EQ.fullmatch(term):
        return (match.group(1) == match.group(3)) == (match.group(2) == "==")
    raise UnsupportedProgram(f"cannot evaluate awk term {term!r}")


def selects(program: str, path: str, nr: int) -> bool:
    """Whether awk `program` prints line `path`, record number `nr` (1-based)."""
    return any(
        all(_term(t, path, nr) for t in alternative.split("&&"))
        for alternative in program.split("||")
    )


def assignment(text: str, files: list[str]) -> dict[str, list[str]]:
    """Each test file mapped to every shard whose program selects it.

    `files` must be in the order bash expands `tests/test_*.py`, which is what `NR`
    counts over.
    """
    program = shard_program(text)
    if _MATRIX_REF not in program:
        raise UnsupportedProgram(f"the shard program never reads {_MATRIX_REF}")
    shards = shard_values(text)
    return {
        path: [s for s in shards if selects(program.replace(_MATRIX_REF, s), path, nr)]
        for nr, path in enumerate(files, start=1)
    }


def gate_test_files() -> list[str]:
    """`tests/test_*.py` in the order the runner's bash expands it.

    bash sorts a glob by the locale's collation; the runner's is C.UTF-8, which for
    these ASCII names is byte order.
    """
    return sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "tests").glob("test_*.py"))


# --- the evaluator ------------------------------------------------------------------


def test_nr_modulo_selects_by_record_number():
    assert selects("NR % 3 == 1", "a", 4)
    assert not selects("NR % 3 == 1", "a", 3)


def test_path_terms_compare_the_whole_line():
    assert selects('$0 == "tests/test_x.py"', "tests/test_x.py", 1)
    assert not selects('$0 != "tests/test_x.py"', "tests/test_x.py", 1)
    assert selects('$0 != "tests/test_x.py"', "tests/test_y.py", 1)


def test_literal_terms_compare_the_substituted_shard_name():
    assert selects('"rest" == "rest"', "anything", 1)
    assert not selects('"cli" == "rest"', "anything", 1)
    assert selects('"cli" != "rest"', "anything", 1)


def test_and_binds_tighter_than_or():
    program = '$0 == "a" || $0 != "a" && NR % 2 == 0'
    assert selects(program, "a", 1)
    assert selects(program, "b", 2)
    assert not selects(program, "b", 1)


def test_an_unknown_term_is_refused_not_guessed():
    with pytest.raises(UnsupportedProgram, match="cannot evaluate"):
        selects("/slow/", "tests/test_slow.py", 1)


def test_a_gate_without_a_shard_step_is_refused():
    with pytest.raises(UnsupportedProgram, match="found 0"):
        shard_program("run: uv run python scripts/run-tests.py\n")


def test_a_program_that_ignores_the_matrix_is_refused():
    text = (
        "        shard: [1, 2]\n"
        "        run: printf '%s\\n' tests/test_*.py | awk 'NR % 2 == 1' "
        "| xargs uv run python scripts/run-tests.py --only\n"
    )
    with pytest.raises(UnsupportedProgram, match="never reads"):
        assignment(text, ["tests/test_a.py"])


def test_a_missed_file_shows_up_with_no_shard():
    text = (
        "        shard: [0, 1]\n"
        "        run: printf '%s\\n' tests/test_*.py | awk 'NR % 3 == ${{ matrix.shard }}' "
        "| xargs uv run python scripts/run-tests.py --only\n"
    )
    got = assignment(text, ["tests/test_a.py", "tests/test_b.py", "tests/test_c.py"])
    assert got == {"tests/test_a.py": ["1"], "tests/test_b.py": [], "tests/test_c.py": ["0"]}


# --- the real gate ------------------------------------------------------------------


def test_the_gate_runs_three_shards():
    assert len(shard_values(GATE.read_text(encoding="utf-8"))) == 3


def test_every_test_file_runs_in_exactly_one_shard():
    files = gate_test_files()
    assert len(files) > 10, "tests/test_*.py matched almost nothing -- the layout changed"
    got = assignment(GATE.read_text(encoding="utf-8"), files)
    missed = [path for path, shards in got.items() if not shards]
    doubled = {path: shards for path, shards in got.items() if len(shards) > 1}
    assert not missed, f"no PR gate shard runs {missed}"
    assert not doubled, f"more than one PR gate shard runs {doubled}"


def test_every_shard_runs_something():
    text = GATE.read_text(encoding="utf-8")
    got = assignment(text, gate_test_files())
    used = {shard for shards in got.values() for shard in shards}
    assert used == set(shard_values(text))


def test_every_named_shard_names_a_real_file():
    # `$0 == "tests/test_<shard>.py"` picks a heavy file by the shard's name; a shard
    # whose file was renamed would run nothing, and `rest` would silently take it.
    text = GATE.read_text(encoding="utf-8")
    named = [s for s in shard_values(text) if s != "rest"]
    missing = [s for s in named if not (ROOT / "tests" / f"test_{s}.py").is_file()]
    assert not missing, f"shards {missing} name no tests/test_<shard>.py"


def test_tests_sit_flat_so_the_glob_reaches_them_all():
    # The shard glob is `tests/test_*.py`; a test in a subfolder would never run.
    nested = [
        p.relative_to(ROOT).as_posix()
        for p in (ROOT / "tests").rglob("test_*.py")
        if p.parent != ROOT / "tests"
    ]
    assert not nested, f"the PR gate's shard glob does not reach {nested}"


def test_each_shard_uploads_its_own_artifact():
    text = GATE.read_text(encoding="utf-8")
    assert "name: test-failures-${{ matrix.shard }}" in text
    assert re.search(r"^\s*name: test-failures\s*$", text, re.MULTILINE) is None
