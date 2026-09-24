"""Tests for scripts/project_settings.py -- the one file devkit edits and never vendors.

No agent hook is wired anywhere, so the pull's whole job on this file is to take the
`hooks` block out and leave everything else exactly as the project wrote it.
"""

import json
from pathlib import Path

from conftest import load_module

ps = load_module("scripts/project_settings.py")

RETIRED = ("scripts/hooks/branch-on-write.py", ".claude/skills/state-tools/README.md")


def _seed(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _project(root: Path, settings: object = "{}") -> Path:
    """A project with `settings` written verbatim when a string, as JSON otherwise."""
    _seed(root, ps.SETTINGS_FILE, settings if isinstance(settings, str) else json.dumps(settings))
    return root


def _hook(command: str, event: str = "PreToolUse") -> dict:
    return {"hooks": {event: [{"hooks": [{"type": "command", "command": command}]}]}}


def _settings(root: Path) -> dict:
    return json.loads((root / ps.SETTINGS_FILE).read_text(encoding="utf-8"))


# --- reading a file that may not be there ------------------------------------


def test_a_missing_or_unparseable_settings_file_is_left_alone(tmp_path):
    """Rewriting a file this could not read is how a pull takes a project's whole
    harness config with it."""
    assert ps.read(tmp_path) is None
    assert ps.settings_pass(tmp_path) == []
    _seed(tmp_path, ps.SETTINGS_FILE, "{not json,")
    assert ps.read(tmp_path) is None
    assert ps.settings_pass(tmp_path) == []
    assert (tmp_path / ps.SETTINGS_FILE).read_text(encoding="utf-8") == "{not json,"


def test_a_hook_command_is_read_with_forward_slashes_and_nothing_else_is_read_at_all():
    """A settings file written on Windows spells the same hook with backslashes, and
    every path devkit compares against is POSIX. A non-string command is not a path to
    normalise -- it is a shape this must not raise on."""
    assert ps.names_in("python3 scripts\\hooks\\lint-fix.py").endswith("scripts/hooks/lint-fix.py")
    assert ps.names_in(None) == ""
    assert ps.names_in(["python3", "x.py"]) == ""


def test_every_hook_entry_is_found_once_whatever_the_tree_looks_like():
    assert ps.hook_entries({"hooks": {"Stop": "nonsense", "PreToolUse": [{"hooks": "no"}]}}) == []
    tree = {"hooks": {"Stop": [{"hooks": [{"command": "a"}, "junk"]}, {"no": "hooks"}]}}
    assert ps.hook_entries(tree) == [{"command": "a"}]
    assert ps.hook_entries(None) == []


# --- unwiring ------------------------------------------------------------------


def test_the_pull_unwires_every_agent_hook_and_names_the_events(tmp_path):
    """devkit's own, the guard, and one the project added itself all go: no agent hook
    is wired anywhere, so there is nothing to tell apart."""
    settings = {
        "env": {"KEEP": "1"},
        "hooks": {
            **_hook('python3 "x/scripts/hooks/worktree-guard-launch.py"')["hooks"],
            **_hook("npx markdownlint README.md", event="Stop")["hooks"],
        },
    }
    root = _project(tmp_path, settings)

    assert ps.settings_pass(root, RETIRED) == [
        f"(unwired agent hooks) {ps.SETTINGS_FILE}: PreToolUse, Stop"
    ]
    assert _settings(root) == {"env": {"KEEP": "1"}}


def test_the_strip_does_not_mutate_the_callers_tree():
    tree = _hook("python3 cap.py")
    stripped, events = ps.strip_hooks(tree)
    assert stripped == {} and events == ["PreToolUse"]
    assert "hooks" in tree


def test_an_empty_or_malformed_hooks_block_is_removed_too(tmp_path):
    """`{"hooks": {}}` is a husk the next reader cannot tell from a hook lost by
    accident, and a non-object one is not a shape anything should keep."""
    root = _project(tmp_path, '{"hooks": {}, "env": {}}')
    assert ps.settings_pass(root) == [f"(unwired agent hooks) {ps.SETTINGS_FILE}: hooks"]
    assert _settings(root) == {"env": {}}
    assert ps.strip_hooks({"hooks": "nonsense"}) == ({}, [])


def test_nothing_to_unwire_leaves_the_file_byte_for_byte(tmp_path):
    """A pull over an already clean project must show no settings diff."""
    original = '{\n   "env": {"KEEP": "1"}\n}\n'
    root = _project(tmp_path, original)
    assert ps.settings_pass(root, RETIRED) == []
    assert (root / ps.SETTINGS_FILE).read_text(encoding="utf-8") == original


def test_a_second_pull_has_nothing_left_to_do(tmp_path):
    root = _project(tmp_path, _hook("python3 cap.py"))
    ps.settings_pass(root)
    before = (root / ps.SETTINGS_FILE).read_text(encoding="utf-8")
    assert ps.settings_pass(root) == []
    assert (root / ps.SETTINGS_FILE).read_text(encoding="utf-8") == before


def test_the_previous_sync_devkit_can_still_call_the_pass(tmp_path):
    """A pull runs the *previous* `sync-devkit.py`, which passes the retired list
    positionally. A signature it could not call would fail the pull that delivers this."""
    root = _project(tmp_path, _hook("python3 cap.py"))
    assert ps.settings_pass(root, RETIRED)


# --- what `--check` says -------------------------------------------------------


def test_an_unguarded_project_is_not_a_check_fault(tmp_path):
    """There is no guard to expect. This used to report UNWIRED for a project holding
    the shim with no hook running it, and hold every consumer's gate red over it."""
    root = _project(tmp_path)
    _seed(root, "scripts/hooks/worktree-guard-launch.py", "# the shim\n")
    assert ps.check_notes(root, ".codex/hooks.json", codex_stale=False) == []
    assert ps.check_summary([]) == ""


def test_a_stale_codex_mirror_is_still_a_check_fault(tmp_path):
    notes = ps.check_notes(_project(tmp_path), ".codex/hooks.json", codex_stale=True)
    ((label, message),) = notes
    assert "STALE" in message
    assert ps.check_summary(notes) == label
