"""Tests for scripts/project_settings.py -- the one file devkit edits and never vendors.

Two halves, and the second is why this module exists at all. Unwiring a retired hook is
loud when it goes wrong: the harness spawns a missing script on every prompt. Wiring the
edit guard is the opposite -- an unwired guard is silence, the edits land on the
checkout's home branch, and the only trace is a task branch someone finds stranded days
later. So the wiring half is covered here at the same depth as the pruning half.
"""

import json
from pathlib import Path

import pytest
from conftest import load_module

ps = load_module("scripts/project_settings.py")

RETIRED = ("scripts/hooks/branch-on-write.py", ".claude/skills/state-tools/README.md")


def _seed(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _project(root: Path, settings: object = "{}", shim: bool = True) -> Path:
    """A project with the shim on disk and `settings` written verbatim when a string."""
    if shim:
        _seed(root, ps.GUARD_HOOK, "# the shim\n")
    _seed(root, ps.SETTINGS_FILE, settings if isinstance(settings, str) else json.dumps(settings))
    return root


def _guard_hook(command: str, event: str = "PreToolUse") -> dict:
    return {"hooks": {event: [{"hooks": [{"type": "command", "command": command}]}]}}


def _commands(payload: dict) -> list[str]:
    return [entry["command"] for entry in ps.hook_entries(payload)]


# --- reading a file that may not be there ------------------------------------


def test_a_missing_or_unparseable_settings_file_answers_cannot_tell(tmp_path):
    """Three-valued on purpose. Rewriting a file this could not read is how a pull
    takes a project's whole harness config with it, and naming it as unguarded is how a
    status line earns being ignored."""
    assert ps.read(tmp_path) is None
    assert ps.settings_guard(tmp_path) is None
    _seed(tmp_path, ps.SETTINGS_FILE, "{not json,")
    assert ps.read(tmp_path) is None
    assert ps.settings_guard(tmp_path) is None


def test_a_readable_file_with_no_guard_is_a_definite_no(tmp_path):
    _project(tmp_path)
    assert ps.settings_guard(tmp_path) is False


def test_a_hook_command_is_read_with_forward_slashes_and_nothing_else_is_read_at_all():
    """A settings file written on Windows spells the same hook with backslashes, and
    every path devkit compares against is POSIX. A non-string command is not a path to
    normalise -- it is a shape this must not raise on."""
    assert ps.names_in("python3 scripts\\hooks\\lint-fix.py").endswith("scripts/hooks/lint-fix.py")
    assert ps.names_in(None) == ""
    assert ps.names_in(["python3", "x.py"]) == ""


def test_every_hook_entry_is_found_once_whatever_the_tree_looks_like():
    """One walk, shared by the two questions asked of a settings file. They used to be
    two walks that disagreed about which shapes to tolerate."""
    assert ps.hook_entries({"hooks": {"Stop": "nonsense", "PreToolUse": [{"hooks": "no"}]}}) == []
    tree = {"hooks": {"Stop": [{"hooks": [{"command": "a"}, "junk"]}, {"no": "hooks"}]}}
    assert ps.hook_entries(tree) == [{"command": "a"}]
    assert ps.hook_entries(None) == []


# --- the guard's wiring -------------------------------------------------------


def test_a_project_that_vendors_the_shim_and_runs_nothing_is_unwired(tmp_path):
    assert ps.guard_unwired(_project(tmp_path)) is True


def test_check_ignores_a_project_that_has_not_vendored_the_shim_yet(tmp_path):
    """It is unguarded, and `--check` stays quiet: the missing file is already reported
    as drift, and one fault reported twice reads as two faults."""
    _project(tmp_path, shim=False)
    assert ps.settings_guard(tmp_path) is False
    assert ps.guard_unwired(tmp_path) is False


def test_the_predicate_and_the_rewrite_are_separable(tmp_path):
    """Both answer about a settings *tree* without touching disk, which is what lets
    `workspace-status.py` ask the question about checkouts it must never write to."""
    empty: dict = {}
    assert ps.guard_wired(empty) is False
    wired, added = ps.wire_guard(empty)
    assert added is True
    assert empty == {}, "the caller's tree is not mutated"
    assert ps.guard_wired(wired) is True
    assert ps.wire_guard(wired) == (wired, False)


def test_wiring_writes_a_hook_that_names_the_shim(tmp_path):
    root = _project(tmp_path)
    assert ps.settings_pass(root, RETIRED) == [
        f"(wired the cross-checkout edit guard) {ps.SETTINGS_FILE}: {ps.GUARD_HOOK}"
    ]
    payload = json.loads((root / ps.SETTINGS_FILE).read_text(encoding="utf-8"))
    assert ps.GUARD_COMMAND in _commands(payload)
    assert payload["hooks"][ps.GUARD_EVENT][-1]["matcher"] == ps.GUARD_MATCHER
    assert ps.guard_unwired(root) is False


def test_a_second_pull_does_not_wire_it_twice(tmp_path):
    """`--pull` runs on every upgrade. A back-fill that could not recognise its own
    work would append a copy each time, and the guard would run once per copy."""
    root = _project(tmp_path)
    ps.settings_pass(root, RETIRED)
    before = (root / ps.SETTINGS_FILE).read_text(encoding="utf-8")
    assert ps.settings_pass(root, RETIRED) == []
    assert (root / ps.SETTINGS_FILE).read_text(encoding="utf-8") == before


@pytest.mark.parametrize(
    "command",
    [
        # The shim, wired by hand under a matcher of the project's own choosing.
        'python3 "${CLAUDE_PROJECT_DIR:-.}/scripts/hooks/worktree-guard-launch.py"',
        # The same, spelled by a settings file written on Windows.
        "python3 scripts\\hooks\\worktree-guard-launch.py",
        # devkit's own settings: it holds the guard, so it needs no shim to reach it.
        'python3 "${CLAUDE_PROJECT_DIR:-.}/scripts/worktree-guard.py"',
    ],
)
def test_an_existing_guard_is_recognised_however_it_is_spelled(tmp_path, command):
    root = _project(tmp_path, _guard_hook(command, event="PostToolUse"))
    assert ps.guard_unwired(root) is False
    assert ps.settings_pass(root, RETIRED) == []


def test_wiring_never_names_a_shim_the_project_does_not_have(tmp_path):
    """A hook command pointing at a missing script is not inert -- the harness spawns
    it on every edit and the interpreter fails. That is the precise state the pruning
    half exists to undo, so the wiring half must not create it."""
    root = _project(tmp_path, shim=False)
    assert ps.settings_pass(root, RETIRED) == []
    assert (root / ps.SETTINGS_FILE).read_text(encoding="utf-8") == "{}"


def test_wiring_leaves_the_projects_other_hooks_alone(tmp_path):
    """Appended as its own group rather than merged into an existing one: the groups
    carry their own matchers, and folding this command into a group written for a
    different matcher would change which tools *that* hook sees."""
    existing = {
        "env": {"KEEP": "1"},
        "hooks": {
            "PreToolUse": [
                {"matcher": "^Bash$", "hooks": [{"type": "command", "command": "python3 cap.py"}]}
            ]
        },
    }
    root = _project(tmp_path, existing)
    ps.settings_pass(root, RETIRED)
    payload = json.loads((root / ps.SETTINGS_FILE).read_text(encoding="utf-8"))
    assert payload["env"] == {"KEEP": "1"}
    assert payload["hooks"]["PreToolUse"][0] == existing["hooks"]["PreToolUse"][0]
    assert len(payload["hooks"]["PreToolUse"]) == 2


# --- the pruning half, moved here with the file it edits ----------------------


def test_a_retired_hook_is_unwired_and_named(tmp_path):
    root = _project(tmp_path, _guard_hook('python3 "x/scripts/hooks/branch-on-write.py"'))
    notes = ps.settings_pass(root, RETIRED)
    assert f"(unwired retired hook) {ps.SETTINGS_FILE}: branch-on-write.py" in notes
    payload = json.loads((root / ps.SETTINGS_FILE).read_text(encoding="utf-8"))
    assert "branch-on-write.py" not in json.dumps(payload)


def test_nothing_to_do_leaves_the_file_byte_for_byte(tmp_path):
    """A pull that retired nothing and had nothing to wire must show no settings diff."""
    original = '{\n   "hooks": {}\n}\n'
    root = _project(tmp_path, original, shim=False)
    assert ps.settings_pass(root, RETIRED) == []
    assert (root / ps.SETTINGS_FILE).read_text(encoding="utf-8") == original


# --- what `--check` says about all this ---------------------------------------


def test_an_unwired_guard_is_a_check_fault_with_its_own_summary(tmp_path):
    """The reversion check for the whole change: without this, the only report of an
    unwired guard is the stranded branch someone finds days later."""
    notes = ps.check_notes(_project(tmp_path), ".codex/hooks.json", codex_stale=False)
    label, message = notes[0]
    assert "UNWIRED" in message
    assert "--pull" in message
    assert ps.check_summary(notes) == label == "no hook runs the cross-checkout edit guard"


def test_both_faults_are_named_when_both_are_present(tmp_path):
    """Either can hold a red gate alone, and a summary that mentions the Codex hooks
    when the guard is what is unwired sends the reader to the wrong file."""
    notes = ps.check_notes(_project(tmp_path), ".codex/hooks.json", codex_stale=True)
    assert len(notes) == 2
    assert "Codex" in ps.check_summary(notes)
    assert "edit guard" in ps.check_summary(notes)


def test_a_wired_project_with_fresh_codex_hooks_has_no_faults(tmp_path):
    root = _project(tmp_path, _guard_hook(ps.GUARD_COMMAND))
    assert ps.check_notes(root, ".codex/hooks.json", codex_stale=False) == []
    assert ps.check_summary([]) == ""
