"""Tests for scripts/project_dependabot.py -- the pull's pass over `.github/dependabot.yml`.

The file is rendered once from a template and never vendored, while the vendored contract
test holds every consumer to a floor-keeping strategy. The pass is what lets a project
generated before the template carried one survive the pull that delivers the test.
"""

from pathlib import Path

from conftest import load_module
from test_ci_workflow_contract import FLOOR_KEEPING, FLOOR_RAISING_ECOSYSTEMS, _floor_raisers

pd = load_module("scripts/project_dependabot.py")

# What the template rendered before it carried a strategy: the v0.11.31 shape the upgrade
# rehearsal adopts this tree into.
RENDERED_BEFORE = """\
# Dependency updates, one ecosystem per manifest this project actually has.
version: 2
updates:
  - package-ecosystem: uv
    directory: /
    schedule:
      interval: weekly
    assignees:
      - owner
    groups:
      minor-and-patch:
        update-types:
          - "minor"
          - "patch"

  # The actions the workflows pin.
  - package-ecosystem: github-actions
    directory: /
    schedule:
      interval: weekly
"""


def _project(root: Path, text: str) -> Path:
    path = root / pd.DEPENDABOT_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return root


def _read(root: Path) -> str:
    return (root / pd.DEPENDABOT_FILE).read_bytes().decode("utf-8")


def test_the_pass_agrees_with_the_contract_it_exists_to_satisfy():
    """Two copies of one policy, one per vendored file; this is what holds them together."""
    assert pd.FLOOR_RAISING_ECOSYSTEMS == FLOOR_RAISING_ECOSYSTEMS
    assert pd.FLOOR_KEEPING_STRATEGY in FLOOR_KEEPING


def test_a_project_rendered_before_the_strategy_passes_the_contract_after_the_pull(tmp_path):
    """The upgrade rehearsal's failure: adopting the contract test from v0.11.31."""
    root = _project(tmp_path, RENDERED_BEFORE)
    assert _floor_raisers(_read(root)) == ["uv (no versioning-strategy)"]

    notes = pd.dependabot_pass(root)

    assert notes == [f"(floor-keeping versioning-strategy) {pd.DEPENDABOT_FILE}: uv"]
    assert _floor_raisers(_read(root)) == []


def test_the_line_lands_under_the_head_at_the_entrys_key_column():
    text, added = pd.with_floor_keeping(RENDERED_BEFORE)
    assert added == ["uv"]
    assert "  - package-ecosystem: uv\n    versioning-strategy: increase-if-necessary\n" in text
    # Everything else is the project's own and comes back untouched, comments included.
    assert text.replace("    versioning-strategy: increase-if-necessary\n", "") == RENDERED_BEFORE


def test_a_second_pass_changes_nothing(tmp_path):
    root = _project(tmp_path, RENDERED_BEFORE)
    pd.dependabot_pass(root)
    once = _read(root)
    assert pd.dependabot_pass(root) == []
    assert _read(root) == once


def test_a_strategy_the_project_chose_is_kept_whatever_it_is():
    """Its decision, as with a settings key: the contract test names a bad one."""
    for strategy in ("lockfile-only", "increase"):
        text = RENDERED_BEFORE.replace(
            "    directory: /\n", f"    versioning-strategy: {strategy}\n    directory: /\n", 1
        )
        assert pd.with_floor_keeping(text) == (text, [])


def test_every_python_entry_is_given_one_and_nothing_else_is():
    text = (
        "version: 2\nupdates:\n"
        "  - package-ecosystem: github-actions\n    directory: /\n"
        "  - package-ecosystem: 'pip'\n    directory: /docs\n"
        "  - package-ecosystem: uv\n    directory: /\n"
        "  - package-ecosystem: npm\n    directory: /frontend\n"
    )
    updated, added = pd.with_floor_keeping(text)
    assert added == ["pip", "uv"]
    assert updated.count("versioning-strategy: increase-if-necessary") == 2
    assert _floor_raisers(updated) == []


def test_a_strategy_on_a_sibling_or_nested_under_groups_does_not_count():
    """Read per entry, at the entry's own key column, like the contract test."""
    text = (
        "updates:\n"
        "  - package-ecosystem: github-actions\n    versioning-strategy: increase-if-necessary\n"
        "  - package-ecosystem: uv\n    groups:\n      g:\n        versioning-strategy: widen\n"
    )
    assert pd.unset_python_entries(text.splitlines(keepends=True)) == [(3, "uv", 4)]


def test_a_commented_out_strategy_does_not_count():
    text = "updates:\n  - package-ecosystem: uv\n    # versioning-strategy: widen\n"
    assert pd.with_floor_keeping(text)[1] == ["uv"]


def test_line_endings_survive(tmp_path):
    root = _project(tmp_path, RENDERED_BEFORE.replace("\n", "\r\n"))
    pd.dependabot_pass(root)
    text = _read(root)
    assert "\n" not in text.replace("\r\n", "")
    assert "    versioning-strategy: increase-if-necessary\r\n" in text


def test_an_entry_on_the_files_last_line_without_a_newline_still_gets_one():
    text, added = pd.with_floor_keeping("updates:\n  - package-ecosystem: uv")
    assert added == ["uv"]
    assert (
        text
        == "updates:\n  - package-ecosystem: uv\n    versioning-strategy: increase-if-necessary\n"
    )


def test_an_absent_or_undecodable_file_is_left_alone(tmp_path):
    assert pd.dependabot_pass(tmp_path) == []
    path = tmp_path / pd.DEPENDABOT_FILE
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe not utf-8 package-ecosystem: uv")
    assert pd.dependabot_pass(tmp_path) == []
    assert path.read_bytes() == b"\xff\xfe not utf-8 package-ecosystem: uv"
