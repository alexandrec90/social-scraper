"""Tests for `structure_baseline.py` -- the `.devkit-structure.txt` file itself.

Pure text and one file read, so everything here is `tmp_path` or a string. What the
baseline *means* -- which findings are worse, what may be recorded, what a tighten
drops -- is `test_structure_check.py`'s; this half is only that the reader and the
writer agree about the format.
"""

from __future__ import annotations

from conftest import load_module

sb = load_module("scripts/hooks/structure_baseline.py")


# --- reading -------------------------------------------------------------------------


def test_a_missing_baseline_reads_as_no_findings(tmp_path):
    """A project that has not adopted the gate is not a project with a broken one."""
    assert sb.read_baseline(tmp_path / "nothing.txt") == {}
    assert sb.existing_notes(tmp_path / "nothing.txt") == []


def test_only_key_equals_int_lines_are_findings(tmp_path):
    """Comments, blanks and anything malformed are dropped rather than raising: the
    `--record` log lives in this same file, so most of its lines are prose by design."""
    path = tmp_path / sb.BASELINE_NAME
    path.write_text(
        "# a comment\n"
        "\n"
        "file_lines::src/a.py = 700\n"
        "complexity::src/a.py::f = 21\n"
        "not a finding at all\n"
        "file_lines::src/b.py = not-a-number\n",
        encoding="utf-8",
    )
    assert sb.read_baseline(path) == {"file_lines::src/a.py": 700, "complexity::src/a.py::f": 21}


def test_baseline_path_hangs_the_file_off_the_repo_root(tmp_path):
    assert sb.baseline_path(tmp_path) == tmp_path / sb.BASELINE_NAME


# --- writing -------------------------------------------------------------------------


def test_render_sorts_the_findings_and_omits_an_empty_log():
    """Sorted so the diff of a raised value is one line, and the log is absent rather
    than an empty marker when nothing has been recorded."""
    body = sb.render_baseline({"b::x": 2, "a::x": 1})
    lines = [line for line in body.splitlines() if line and not line.startswith("#")]
    assert lines == ["a::x = 1", "b::x = 2"]
    assert sb.RECORD_MARKER.strip() not in body


def test_the_log_goes_after_the_findings_so_the_sorted_block_stays_a_clean_diff():
    body = sb.render_baseline({"a::x": 1}, ["# one", "# two"])
    assert body.index("a::x = 1") < body.index(sb.RECORD_MARKER.strip())
    assert body.index("# one") < body.index("# two")


def test_a_rendered_baseline_reads_back_as_exactly_its_numbers(tmp_path):
    """The round trip, which is the whole contract between the two halves here: the log
    is comments, so `read_baseline` has to be blind to it."""
    entries = {"file_lines::src/a.py": 700, "orphan::src/b.py": 1}
    path = tmp_path / sb.BASELINE_NAME
    path.write_text(sb.render_baseline(entries, ["# 2026-09-19  recorded: why"]), encoding="utf-8")
    assert sb.read_baseline(path) == entries


def test_existing_notes_reads_back_the_log_and_nothing_above_it(tmp_path):
    path = tmp_path / sb.BASELINE_NAME
    path.write_text(sb.render_baseline({"a::x": 1}), encoding="utf-8")
    assert sb.existing_notes(path) == []

    path.write_text(sb.render_baseline({"a::x": 1}, ["# one", "# two"]), encoding="utf-8")
    assert sb.existing_notes(path) == ["# one", "# two"]


# --- the reason -----------------------------------------------------------------------


def test_reason_lines_dates_the_head_and_comments_every_line_after_it():
    """The regression. Only the head used to be prefixed, so the tail of a multi-line
    reason landed in the baseline as bare English -- a write that succeeded, printed
    `structure-check: clean`, and surfaced later as the sorted-and-unique test failing
    with a sentence of prose at index 0."""
    lines = sb.reason_lines("first\nsecond\n\nthird", "2026-09-19")
    assert lines == ["# 2026-09-19  recorded: first", "#   second", "#", "#   third"]
    assert all(line.startswith("#") for line in lines)


def test_a_blank_line_in_the_reason_stays_one_note(tmp_path):
    """`existing_notes` partitions the log on blank lines, so an empty line inside a
    reason would split one note in two and the second half would not read back."""
    note = "\n".join(sb.reason_lines("head\n\ntail", "2026-09-19"))
    path = tmp_path / sb.BASELINE_NAME
    path.write_text(sb.render_baseline({"a::x": 1}, [note]), encoding="utf-8")
    assert sb.existing_notes(path) == [note]


def test_a_single_line_reason_is_unchanged_by_the_fix():
    """The common case, pinned: the fix must not have added an indented empty line or
    moved the stamp."""
    assert sb.reason_lines("the split is its own PR", "2026-09-19") == [
        "# 2026-09-19  recorded: the split is its own PR"
    ]
