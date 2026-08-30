"""Tests for `untested_symbols.py` — the ratchet that a public symbol has a test.

Two halves, and only the second is the same kind of test as the rest of this tree:

- **The scanner's units**, exercised against synthetic trees under `tmp_path`. A gate
  that decides what counts as covered has to be right about hyphens, substrings and
  which directories are sources, and none of that is checkable by running it on the
  repo it happens to be in.
- **The live gate**, run against this project: no public symbol outside the baseline,
  and no line in the baseline that is no longer true.

Vendored, so every value that varies per project is read from `hook.CFG`, and the only
paths named literally are vendored ones that exist in every consumer.

**The fixtures below use invented symbol names — `alpha`, `beta`, `Gamma` — on
purpose.** This file is part of the corpus the scanner reads, so a fixture quoting a
real symbol would have the gate certify coverage it invented; that is not hypothetical,
it is the defect the devkit-only ancestor of this gate shipped with and had to fix.
`test_the_gate_certifies_only_what_it_actually_tests` holds the line.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from conftest import REPO_ROOT, load_module

us = load_module("scripts/hooks/untested_symbols.py")
hc = us.harness_config

GATE = Path("scripts/hooks/untested_symbols.py")
CONFIG_MODULE = Path("scripts/hooks/harness_config.py")


def config(**kwargs) -> object:
    """A neutral `Config` with `[test_contract]` overrides, for synthetic trees."""
    contract = hc.TestContractConfig(
        sources=tuple(kwargs.pop("sources", ())),
        exclude=tuple(kwargs.pop("exclude", ())),
    )
    return dataclasses.replace(hc.Config(), test_contract=contract, **kwargs)


def write(root: Path, rel: str, text: str = "") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --- public_symbols -----------------------------------------------------------


def test_public_symbols_reads_top_level_definitions_in_file_order():
    source = "def alpha():\n    pass\n\n\nclass Gamma:\n    pass\n\n\nasync def beta():\n    pass\n"
    assert us.public_symbols(source) == ["alpha", "Gamma", "beta"]


def test_public_symbols_skips_the_private_ones():
    """A leading underscore is the author saying "not the API"; the public function
    calling it answers for its behaviour."""
    assert us.public_symbols("def _alpha():\n    pass\n") == []


def test_public_symbols_does_not_descend_into_bodies():
    """A nested helper is not separately callable, so demanding a test naming it would
    be demanding a test of an implementation detail."""
    source = "def alpha():\n    def beta():\n        pass\n"
    assert us.public_symbols(source) == ["alpha"]


def test_public_symbols_does_not_exempt_main():
    """argv handling and exit codes are what a harness depends on, and the part that
    breaks quietly — `main` is the last thing that should be waved through."""
    assert us.public_symbols("def main():\n    pass\n") == ["main"]


def test_public_symbols_ignores_module_level_assignments():
    """Constants are data. A name bound to a value has no behaviour to assert."""
    assert us.public_symbols("ALPHA = 1\nbeta = lambda: None\n") == []


# --- reference_pattern --------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "acme.alpha(1)",  # attribute access
        "assert acme.alpha == 2",  # attribute, not called
        "alpha(1)",  # bare call, after a from-import
        "from acme import alpha",
        "from acme import beta, alpha",
    ],
)
def test_a_reference_is_a_call_an_attribute_or_an_import(text):
    assert us.reference_pattern("alpha").search(text)


@pytest.mark.parametrize(
    "text",
    [
        "alphabet = 1",  # substring of a longer name
        "acme.alphabet()",  # substring after a dot
        '"alpha"',  # a bare mention in prose or a string
        "# alpha is untested",
    ],
)
def test_a_bare_substring_is_not_a_reference(text):
    """The failure this rules out: `cap` matching inside `capsys`, which would pass
    every module defining a `cap` on the strength of a pytest fixture name."""
    assert not us.reference_pattern("alpha").search(text)


# --- module_pattern / corpus_for ----------------------------------------------


@pytest.mark.parametrize("text", ["load_module('scripts/acme-tool.py')", "acme_tool.alpha()"])
def test_a_module_is_recognised_in_either_spelling(text):
    """A hyphenated script is loaded by path and imported under an underscored name; a
    test may use either, and one using neither is not the test covering it."""
    assert us.module_pattern(Path("scripts/acme-tool.py")).search(text)


def test_the_corpus_is_only_the_tests_that_name_the_module():
    """Scoped, because searching every test in the repo passes `main` and `run`
    everywhere: many modules define those names, so a hit says nothing about which."""
    texts = {
        Path("tests/test_acme.py"): "acme.alpha()",
        Path("tests/test_other.py"): "other.beta()",
    }
    corpus = us.corpus_for(Path("scripts/acme.py"), texts)
    assert "alpha" in corpus
    assert "beta" not in corpus


@pytest.mark.parametrize(
    "text",
    [
        "assert '--ignore=tests/acme_tool' in ADDOPTS",  # a path that merely contains it
        "# acme_tool is the one this does NOT cover",  # prose naming the module
        "acme_tool_helpers.alpha()",  # a longer name that starts with it
    ],
)
def test_a_module_named_only_in_passing_does_not_join_its_corpus(text):
    """The false vouch this rules out, and it is the one the opening docstring claims
    cannot happen: a bare stem match is a substring match with a word boundary painted
    on. `test_run_tests.py` asserting `'--ignore=tests/local_e2e'` pulled that whole file
    into `scripts/local-e2e.py`'s corpus, where an unrelated `rt.main(` satisfied
    `reference_pattern('main')` -- so `local-e2e.py::main` read as covered and the stale
    half of the ratchet then demanded its line be deleted from the baseline."""
    assert not us.module_pattern(Path("scripts/acme-tool.py")).search(text)


def test_a_symbol_is_not_vouched_for_by_a_file_that_merely_mentions_its_module():
    """The same defect at the level the gate actually reports: end to end, through
    `corpus_for`, the pairing that produced a wrong "now covered" verdict."""
    texts = {Path("tests/test_run_tests.py"): "assert '--ignore=tests/acme_tool' in A\nrt.alpha()"}
    assert "alpha" not in us.corpus_for(Path("scripts/acme-tool.py"), texts)


def test_the_corpus_admits_a_sibling_that_names_the_module():
    """The other half of the scoping: a helper covered through the caller that
    exercises it is covered, and a gate insisting on `test_<stem>.py` would say
    otherwise and be wrong."""
    texts = {Path("tests/test_caller.py"): "import acme\nacme.alpha()"}
    assert "alpha" in us.corpus_for(Path("scripts/acme.py"), texts)


# --- source_dirs / source_files / test_files ----------------------------------


def test_source_dirs_defaults_to_the_app_tree_and_scripts():
    """`scripts/` is in by default because it is the code no framework test touches:
    nothing renders it and nothing hits it over HTTP."""
    assert us.source_dirs(config(app_dir="src/")) == ("src", "scripts")


def test_source_dirs_does_not_repeat_a_tree_that_is_both():
    assert us.source_dirs(config(app_dir="scripts/")) == ("scripts",)


def test_source_dirs_honours_an_explicit_list():
    """A project keeping code somewhere else names it, rather than accepting a gate
    that quietly covers half of it."""
    assert us.source_dirs(config(sources=["lib", "tools"])) == ("lib", "tools")


def test_source_files_finds_modules_at_any_depth(tmp_path):
    write(tmp_path, "src/acme/deep/thing.py")
    assert us.source_files(tmp_path, config(sources=["src"])) == [
        Path("src/acme/deep/thing.py"),
    ]


@pytest.mark.parametrize(
    "rel",
    [
        "src/test_acme.py",  # a test, not a source
        "src/conftest.py",  # fixtures
        "src/_private.py",  # not the API
        "src/__init__.py",  # re-exports
        "src/tests/helper.py",  # inside a tests tree
        "src/__pycache__/acme.py",  # tooling
        "src/.venv/lib/acme.py",  # tooling
    ],
)
def test_source_files_skips_what_is_not_this_project_s_api(tmp_path, rel):
    write(tmp_path, rel)
    assert us.source_files(tmp_path, config(sources=["src"])) == []


def test_source_files_honours_the_configured_exclusions(tmp_path):
    write(tmp_path, "src/generated/client.py")
    write(tmp_path, "src/acme.py")
    cfg = config(sources=["src"], exclude=["generated"])
    assert us.source_files(tmp_path, cfg) == [Path("src/acme.py")]


def test_test_files_reads_the_tests_tree_and_the_source_trees(tmp_path):
    """Hook tests live beside the hooks rather than in `tests/`, so a corpus taken from
    the configured tests directory alone would miss the tier that tests itself."""
    write(tmp_path, "tests/test_acme.py")
    write(tmp_path, "src/hooks/tests/test_beta.py")
    cfg = config(sources=["src"], tests_dir="tests/")
    assert us.test_files(tmp_path, cfg) == [
        Path("src/hooks/tests/test_beta.py"),
        Path("tests/test_acme.py"),
    ]


def test_test_files_ignores_tests_outside_the_declared_trees(tmp_path):
    """The generator case: a template tree contains test files for the projects it
    emits, and those must never vouch for the generator's own code."""
    write(tmp_path, "templates/tests/test_acme.py")
    assert us.test_files(tmp_path, config(sources=["src"], tests_dir="tests/")) == []


def test_test_files_reports_each_path_once(tmp_path):
    """`tests/` under a source tree is reachable from both roots."""
    write(tmp_path, "src/tests/test_acme.py")
    cfg = config(sources=["src"], tests_dir="src/tests")
    assert us.test_files(tmp_path, cfg) == [Path("src/tests/test_acme.py")]


# --- gaps ---------------------------------------------------------------------


def test_entry_is_posix_so_the_baseline_is_the_same_file_on_any_os():
    assert us.entry(Path("src") / "acme.py", "alpha") == "src/acme.py::alpha"


def test_gaps_names_the_symbol_no_test_references(tmp_path):
    write(tmp_path, "src/acme.py", "def alpha():\n    pass\n\n\ndef beta():\n    pass\n")
    write(tmp_path, "tests/test_acme.py", "import acme\n\n\ndef test_it():\n    acme.alpha()\n")
    assert us.gaps(tmp_path, config(sources=["src"])) == ["src/acme.py::beta"]


def test_gaps_counts_a_module_no_test_mentions_at_all(tmp_path):
    """The case the whole gate exists for: a script that was never covered by anything,
    which every other contract test in a repo passes straight over."""
    write(tmp_path, "src/acme.py", "def alpha():\n    pass\n")
    write(tmp_path, "tests/test_other.py", "def test_it():\n    pass\n")
    assert us.gaps(tmp_path, config(sources=["src"])) == ["src/acme.py::alpha"]


def test_gaps_skips_a_file_that_does_not_parse(tmp_path):
    """The interpreter and the linter both say so louder, and skipping keeps one broken
    file from masking the other two hundred modules."""
    write(tmp_path, "src/broken.py", "def alpha(:\n")
    write(tmp_path, "src/acme.py", "def beta():\n    pass\n")
    assert us.gaps(tmp_path, config(sources=["src"])) == ["src/acme.py::beta"]


def test_gaps_accepts_a_substituted_corpus(tmp_path):
    write(tmp_path, "src/acme.py", "def alpha():\n    pass\n")
    write(tmp_path, "tests/test_acme.py", "acme.alpha()")
    cfg = config(sources=["src"])
    assert us.gaps(tmp_path, cfg) == []
    assert us.gaps(tmp_path, cfg, texts={}) == ["src/acme.py::alpha"]


def test_read_tests_returns_the_text_of_every_corpus_file(tmp_path):
    write(tmp_path, "tests/test_acme.py", "acme.alpha()")
    assert us.read_tests(tmp_path, config(sources=["src"])) == {
        Path("tests/test_acme.py"): "acme.alpha()"
    }


# --- the baseline file --------------------------------------------------------


def test_read_baseline_drops_comments_and_blank_lines(tmp_path):
    path = write(tmp_path, "b.txt", "# header\n\nsrc/acme.py::alpha\n  src/acme.py::beta  \n")
    assert us.read_baseline(path) == ["src/acme.py::alpha", "src/acme.py::beta"]


def test_read_baseline_of_a_file_that_does_not_exist_is_empty(tmp_path):
    assert us.read_baseline(tmp_path / "absent.txt") == []


def test_render_baseline_sorts_and_dedupes():
    """Sorted so a burn-down diff is readable, deduped so two branches adding the same
    line conflict in git instead of merging into a duplicate."""
    text = us.render_baseline(["b::two", "a::one", "b::two"])
    assert [line for line in text.splitlines() if not line.startswith("#")] == [
        "a::one",
        "b::two",
    ]


def test_render_baseline_says_the_file_may_only_shrink():
    """The header is the only instruction most people will read before editing it."""
    assert "SHRINK" in us.render_baseline([])


def test_the_baseline_is_named_beside_the_manifest_it_belongs_to():
    assert us.baseline_path(Path("/repo")) == Path("/repo") / us.BASELINE_NAME


# --- seed ---------------------------------------------------------------------


def test_seed_records_the_debt_a_project_already_has(tmp_path):
    write(tmp_path, "src/acme.py", "def alpha():\n    pass\n")
    count = us.seed(tmp_path, config(sources=["src"]))
    assert count == 1
    assert "src/acme.py::alpha" in us.read_baseline(us.baseline_path(tmp_path))


def test_seed_writes_lf_on_every_platform(tmp_path):
    """The file is committed, so a CRLF one fails the consumer's own line-ending hook
    on the very commit that adopts the gate — which reads as the gate being broken."""
    write(tmp_path, "src/acme.py", "def alpha():\n    pass\n")
    us.seed(tmp_path, config(sources=["src"]))
    assert b"\r\n" not in us.baseline_path(tmp_path).read_bytes()


def test_seed_refuses_to_overwrite_an_existing_baseline(tmp_path):
    """The one way this gate could be defeated without anyone deciding to: re-seeding
    launders every symbol someone just failed to test into the debt list, and the gate
    goes green having lost exactly the finding it exists to make."""
    write(tmp_path, "src/acme.py", "def alpha():\n    pass\n")
    write(tmp_path, us.BASELINE_NAME, "# empty\n")
    assert us.seed(tmp_path, config(sources=["src"])) is None
    assert us.read_baseline(us.baseline_path(tmp_path)) == []


def test_a_seeded_project_starts_clean(tmp_path):
    """Adoption must not be a red gate — that is what makes seeding part of `--pull`
    rather than a chore each project does after its build breaks."""
    write(tmp_path, "src/acme.py", "def alpha():\n    pass\n")
    cfg = config(sources=["src"])
    us.seed(tmp_path, cfg)
    assert us.verdict(tmp_path, cfg) == ([], [])


# --- verdict ------------------------------------------------------------------


def test_verdict_reports_a_symbol_that_is_not_in_the_baseline(tmp_path):
    write(tmp_path, "src/acme.py", "def alpha():\n    pass\n")
    write(tmp_path, us.BASELINE_NAME, "")
    assert us.verdict(tmp_path, config(sources=["src"])) == (["src/acme.py::alpha"], [])


def test_verdict_reports_a_baseline_line_that_is_no_longer_true(tmp_path):
    """Without this half the file is write-only, and the next person to delete a test
    finds the gate already looking the other way."""
    write(tmp_path, "src/acme.py", "def alpha():\n    pass\n")
    write(tmp_path, "tests/test_acme.py", "acme.alpha()")
    write(tmp_path, us.BASELINE_NAME, "src/acme.py::alpha\n")
    assert us.verdict(tmp_path, config(sources=["src"])) == ([], ["src/acme.py::alpha"])


# --- main ---------------------------------------------------------------------


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A synthetic project `main()` runs against, since it reads module-level state."""

    def install(**kwargs):
        cfg = config(**kwargs)
        monkeypatch.setattr(us, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(us, "CFG", cfg)
        return tmp_path

    return install


def test_main_passes_on_a_project_whose_debt_is_recorded(project, capsys):
    root = project(sources=["src"])
    write(root, "src/acme.py", "def alpha():\n    pass\n")
    us.seed(root, us.CFG)
    assert us.main([]) == 0
    assert "clean" in capsys.readouterr().out


def test_main_fails_and_names_the_symbol(project, capsys):
    root = project(sources=["src"])
    write(root, "src/acme.py", "def alpha():\n    pass\n")
    write(root, us.BASELINE_NAME, "")
    assert us.main([]) == 1
    assert "src/acme.py::alpha" in capsys.readouterr().out


def test_main_fails_on_a_baseline_line_that_is_now_covered(project, capsys):
    root = project(sources=["src"])
    write(root, "src/acme.py", "def alpha():\n    pass\n")
    write(root, "tests/test_acme.py", "acme.alpha()")
    write(root, us.BASELINE_NAME, "src/acme.py::alpha\n")
    assert us.main([]) == 1
    assert "now covered" in capsys.readouterr().out


def test_main_seed_writes_the_baseline_and_reports_the_count(project, capsys):
    root = project(sources=["src"])
    write(root, "src/acme.py", "def alpha():\n    pass\n")
    assert us.main(["--seed"]) == 0
    assert "1 untested symbol" in capsys.readouterr().out
    assert us.baseline_path(root).exists()


def test_main_seed_refuses_rather_than_overwriting(project, capsys):
    root = project(sources=["src"])
    write(root, us.BASELINE_NAME, "# empty\n")
    assert us.main(["--seed"]) == 1
    assert "refusing to overwrite" in capsys.readouterr().out


def test_main_list_prints_every_gap_regardless_of_the_baseline(project, capsys):
    """The burn-down view: what is untested, not what is unrecorded."""
    root = project(sources=["src"])
    write(root, "src/acme.py", "def alpha():\n    pass\n")
    write(root, us.BASELINE_NAME, "src/acme.py::alpha\n")
    assert us.main(["--list"]) == 0
    assert capsys.readouterr().out.strip() == "src/acme.py::alpha"


# --- the live gate ------------------------------------------------------------

BASELINE = us.baseline_path(REPO_ROOT)
adopted = pytest.mark.skipif(
    not BASELINE.exists(),
    reason=f"no {us.BASELINE_NAME}; adopt with `python scripts/hooks/untested_symbols.py --seed`",
)


@adopted
def test_every_public_symbol_is_named_by_a_test():
    uncovered, _ = us.verdict(REPO_ROOT, us.CFG)
    assert not uncovered, (
        "write a test naming these, do not add them to the baseline: " + ", ".join(uncovered)
    )


@adopted
def test_the_baseline_names_only_real_gaps():
    _, stale = us.verdict(REPO_ROOT, us.CFG)
    assert not stale, f"now covered — delete these lines from {us.BASELINE_NAME}: " + ", ".join(
        stale
    )


@adopted
def test_the_baseline_is_sorted_and_unique():
    """Mechanical, and the reason is merges: an unsorted file appends, and appending is
    how two branches both add a line and neither notices."""
    entries = us.read_baseline(BASELINE)
    assert entries == sorted(set(entries))


@adopted
def test_the_gate_certifies_only_what_it_actually_tests():
    """This file is in the corpus it scans, so a fixture quoting a real symbol would
    have the gate vouch for coverage it invented. Scanning without this file may only
    change the verdict for the two modules it genuinely exercises."""
    texts = us.read_tests(REPO_ROOT, us.CFG)
    mine = Path(__file__).resolve().relative_to(REPO_ROOT)
    without = {rel: text for rel, text in texts.items() if rel != mine}
    vouched = set(us.gaps(REPO_ROOT, us.CFG, texts=without)) - set(
        us.gaps(REPO_ROOT, us.CFG, texts=texts)
    )
    modules = {key.split("::")[0] for key in vouched}
    assert modules <= {GATE.as_posix(), CONFIG_MODULE.as_posix()}, (
        "fixtures here vouch for modules this file does not test: use invented symbol "
        f"names — {sorted(vouched)}"
    )
