"""Tests for scripts/hooks/lint-fix.py."""

import json
import subprocess
from pathlib import Path

import pytest
from conftest import load_module

lint_fix = load_module("scripts/hooks/lint-fix.py")


def result(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


# ---- parse_hook_input ------------------------------------------------------


def test_parse_hook_input_empty_is_none():
    assert lint_fix.parse_hook_input("") is None


def test_parse_hook_input_malformed_is_none():
    assert lint_fix.parse_hook_input("{not json") is None


def test_parse_hook_input_non_dict_is_none():
    assert lint_fix.parse_hook_input("[1, 2]") is None


def test_parse_hook_input_valid_dict():
    assert lint_fix.parse_hook_input('{"tool_name": "Edit"}') == {"tool_name": "Edit"}


# ---- extract_path ----------------------------------------------------------


def test_extract_path_none_input():
    assert lint_fix.extract_path(None) is None


def test_extract_path_snake_case():
    payload = {"tool_name": "Edit", "tool_input": {"file_path": "app/main.py"}}
    assert lint_fix.extract_path(payload) == "app/main.py"


def test_extract_path_camel_case():
    payload = {"toolName": "Write", "toolInput": {"filePath": "app/x.py"}}
    assert lint_fix.extract_path(payload) == "app/x.py"


def test_extract_path_rejects_other_tools():
    payload = {"tool_name": "Bash", "tool_input": {"file_path": "app/main.py"}}
    assert lint_fix.extract_path(payload) is None


def test_extract_path_missing_path_is_none():
    assert lint_fix.extract_path({"tool_name": "Edit", "tool_input": {}}) is None


def test_extract_path_non_dict_tool_input():
    assert lint_fix.extract_path({"tool_name": "Edit", "tool_input": "nope"}) is None


def test_extract_paths_from_codex_apply_patch_payload():
    patch = """*** Begin Patch
*** Update File: scripts/a.py
@@
*** Add File: scripts/b.py
+x = 1
*** Delete File: README.md
*** End Patch
"""
    payload = {"tool_name": "apply_patch", "tool_input": {"input": patch}}
    assert lint_fix.extract_paths(payload) == [
        "scripts/a.py",
        "scripts/b.py",
        "README.md",
    ]


def test_extract_paths_from_codex_freeform_tool_input():
    patch = "*** Begin Patch\n*** Update File: scripts/a.py\n*** End Patch\n"
    payload = {"tool_name": "apply_patch", "tool_input": patch}
    assert lint_fix.extract_paths(payload) == ["scripts/a.py"]


def test_extract_paths_from_live_codex_command_payload():
    patch = "*** Begin Patch\n*** Update File: C:\\repo\\scripts\\a.py\n@@\n+x=1\n*** End Patch\n"
    payload = {"tool_name": "apply_patch", "tool_input": {"command": patch}}
    assert lint_fix.extract_paths(payload) == [r"C:\repo\scripts\a.py"]


def test_extract_paths_deduplicates_patch_targets():
    patch = "*** Update File: app/main.py\n*** Update File: app/main.py\n"
    payload = {"tool_name": "apply_patch", "tool_input": {"input": patch}}
    assert lint_fix.extract_paths(payload) == ["app/main.py"]


# ---- is_lintable -----------------------------------------------------------


def test_is_lintable_python():
    assert lint_fix.is_lintable("app/main.py")
    assert lint_fix.is_lintable("app/types.pyi")


def test_is_lintable_rejects_non_python():
    assert not lint_fix.is_lintable("README.md")
    assert not lint_fix.is_lintable("frontend/src/app.tsx")


# ---- find_ruff -------------------------------------------------------------


def test_find_ruff_prefers_venv(tmp_path):
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    ruff = venv_bin / "ruff"
    ruff.write_text("#!/bin/sh\n")
    assert lint_fix.find_ruff(tmp_path) == str(ruff)


def test_find_ruff_falls_back_to_path(tmp_path, monkeypatch):
    monkeypatch.setattr(lint_fix.shutil, "which", lambda name: "/usr/bin/ruff")
    assert lint_fix.find_ruff(tmp_path) == "/usr/bin/ruff"


# ---- main ------------------------------------------------------------------


def _payload(path: str) -> str:
    # json.dumps, not an f-string: a Windows tmp_path (C:\Users\...\Temp\...) embedded
    # raw makes \U/\A/\T invalid JSON escapes, so parse_hook_input returns None and
    # main() returns 0 before reaching the branch under test.
    return json.dumps({"tool_name": "Edit", "tool_input": {"file_path": path}})


def _patch_payload(*paths: str) -> str:
    patch = "\n".join(f"*** Update File: {path}" for path in paths)
    return json.dumps({"tool_name": "apply_patch", "tool_input": {"input": patch}})


def test_payload_survives_windows_paths():
    # Regression: the helper used to interpolate the path into JSON with an f-string,
    # so a Windows tmp_path silently failed to parse and every main() test below that
    # asserts `== 0` passed vacuously (main() returns 0 on an unparseable payload).
    win = r"C:\Users\Admin\AppData\Local\Temp\pytest-1\test_x0\x.py"
    assert lint_fix.extract_path(lint_fix.parse_hook_input(_payload(win))) == win


def test_main_skips_non_python(monkeypatch):
    monkeypatch.setattr(lint_fix, "_read_stdin", lambda: _payload("README.md"))
    called = []
    monkeypatch.setattr(lint_fix, "find_ruff", lambda root: called.append("ruff") or "ruff")
    assert lint_fix.main() == 0
    assert called == []  # returned before resolving ruff


def _project(tmp_path: Path) -> Path:
    """Make `tmp_path` look like a project, so its files are in scope to lint.

    A bare temp directory is deliberately *out* of scope now -- see the
    `project_root_for` tests below -- so a test about linting has to say which project
    the file belongs to, the same way a real edit does.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    return tmp_path


def test_main_noop_when_ruff_missing(monkeypatch, tmp_path):
    f = _project(tmp_path) / "x.py"
    f.write_text("x = 1\n")
    monkeypatch.setattr(lint_fix, "_read_stdin", lambda: _payload(str(f)))
    monkeypatch.setattr(lint_fix, "find_ruff", lambda root: None)
    assert lint_fix.main() == 0


def test_main_clean_file_returns_zero(monkeypatch, tmp_path):
    f = _project(tmp_path) / "x.py"
    f.write_text("x = 1\n")
    monkeypatch.setattr(lint_fix, "_read_stdin", lambda: _payload(str(f)))
    monkeypatch.setattr(lint_fix, "find_ruff", lambda root: "ruff")
    monkeypatch.setattr(lint_fix, "_run", lambda ruff, *args, **kw: result(returncode=0))
    assert lint_fix.main() == 0


def test_main_relays_remaining_errors(monkeypatch, tmp_path, capsys):
    f = _project(tmp_path) / "x.py"
    f.write_text("x = 1\n")
    monkeypatch.setattr(lint_fix, "_read_stdin", lambda: _payload(str(f)))
    monkeypatch.setattr(lint_fix, "find_ruff", lambda root: "ruff")

    calls = []

    def fake_run(ruff, *args, **kw):
        calls.append(args)
        # `format` and `check --fix` succeed silently; the final reporting
        # `check` (no --fix) surfaces the unfixable finding.
        if args[0] == "check" and "--fix" not in args:
            return result(returncode=1, stdout="x.py:1:1: F821 undefined name\n")
        return result(returncode=0)

    monkeypatch.setattr(lint_fix, "_run", fake_run)

    assert lint_fix.main() == 2
    err = capsys.readouterr().err
    assert "F821" in err
    # Fixers ran before the reporting check, against the project-relative path.
    assert ("format", "x.py") in calls
    assert ("check", "--fix", "--unfixable", "F401", "x.py") in calls


def _ruff_argv(monkeypatch, tmp_path, source: str) -> list[tuple[str, ...]]:
    """Every argv `main` hands ruff for a file holding `source`."""
    f = _project(tmp_path) / "x.py"
    f.write_text(source)
    monkeypatch.setattr(lint_fix, "_read_stdin", lambda: _payload(str(f)))
    monkeypatch.setattr(lint_fix, "find_ruff", lambda root: "ruff")
    calls: list[tuple[str, ...]] = []

    def record(ruff, *args, **kw):
        calls.append(args)
        return result(returncode=0)

    monkeypatch.setattr(lint_fix, "_run", record)
    lint_fix.main()
    return calls


def test_a_mid_edit_rule_is_neither_fixed_nor_reported(monkeypatch, tmp_path):
    """`F401` judges a *finished* file, and this hook runs on a half-written one.

    Adding `import os` and then, in the next edit, the function that uses it is the
    normal order for an incremental change. `--fix` deleted the import between the two,
    and the F821 it became surfaced an edit later pointing at the wrong edit. Reporting
    it instead would block the edit that added the import, which is worse -- so it is
    dropped from both runs and left to CI, which sees the file whole.

    Reversion check: drop either flag and one of these two assertions fails.
    """
    rules = ",".join(lint_fix.MID_EDIT_RULES)
    calls = _ruff_argv(monkeypatch, tmp_path, "x = 1\n")
    fix = next(a for a in calls if a[0] == "check" and "--fix" in a)
    report = next(a for a in calls if a[0] == "check" and "--fix" not in a)
    assert ("--unfixable", rules) == fix[fix.index("--unfixable") : fix.index("--unfixable") + 2]
    assert ("--ignore", rules) == report[report.index("--ignore") : report.index("--ignore") + 2]


def test_ruff_itself_keeps_an_import_the_next_edit_will_use(tmp_path):
    """The flag assertions above prove what was passed; this proves what ruff does with
    it. Run against the real binary because the whole defect was a behaviour, and a test
    that only inspects argv would pass against a flag ruff had renamed."""
    ruff = lint_fix.find_ruff(Path(lint_fix.REPO_ROOT))
    if not ruff:
        pytest.skip("no ruff on this machine; the argv assertions above still gate the flags")
    source = "import os\n"
    f = tmp_path / "x.py"
    f.write_text(source)
    rules = ",".join(lint_fix.MID_EDIT_RULES)
    subprocess.run(
        [ruff, "check", "--fix", "--unfixable", rules, "--isolated", "--select", "F", str(f)],
        capture_output=True,
        check=False,
    )
    assert f.read_text() == source


def test_main_missing_file_returns_zero(monkeypatch, tmp_path):
    missing = tmp_path / "gone.py"
    monkeypatch.setattr(lint_fix, "_read_stdin", lambda: _payload(str(missing)))
    monkeypatch.setattr(lint_fix, "find_ruff", lambda root: "ruff")
    assert lint_fix.main() == 0


def test_main_lints_every_python_file_in_codex_patch(monkeypatch, tmp_path):
    root = _project(tmp_path)
    first = root / "a.py"
    second = root / "b.py"
    first.write_text("x = 1\n")
    second.write_text("y = 2\n")
    monkeypatch.setattr(
        lint_fix,
        "_read_stdin",
        lambda: _patch_payload(str(first), str(second), "README.md"),
    )
    monkeypatch.setattr(lint_fix, "find_ruff", lambda root: "ruff")
    calls = []
    monkeypatch.setattr(
        lint_fix,
        "_run",
        lambda ruff, *args, **kw: calls.append(args) or result(returncode=0),
    )

    assert lint_fix.main() == 0
    assert ("format", "a.py") in calls
    assert ("format", "b.py") in calls
    assert not any("README.md" in call for args in calls for call in args)


# ---- project scope: lint a file by its own project, or not at all ----------


def test_a_file_outside_every_project_is_not_linted(monkeypatch, tmp_path):
    """The reported failure. A scratch script in a session temp directory was linted
    with the *hook's* repo as the working directory, so ruff resolved per-file-ignores
    against a config that does not own the file -- the ignores did not apply and rules
    the project deliberately disables fired as blocking false positives."""
    f = tmp_path / "probe.py"
    f.write_text("import subprocess\n")
    monkeypatch.setattr(lint_fix, "_read_stdin", lambda: _payload(str(f)))
    monkeypatch.setattr(lint_fix, "find_ruff", lambda root: "ruff")
    monkeypatch.setattr(
        lint_fix, "_run", lambda *a, **kw: pytest.fail("linted a file outside every project")
    )
    assert lint_fix.main() == 0


def test_a_file_in_another_project_is_linted_by_that_project(monkeypatch, tmp_path):
    """The case that must keep working, and the reason scoping to the hook's own repo
    would have been wrong: an agent edits inside an ephemeral worktree, which is not
    under the checkout this hook is vendored into."""
    elsewhere = _project(tmp_path / "box")
    f = elsewhere / "thing.py"
    f.write_text("x = 1\n")
    monkeypatch.setattr(lint_fix, "_read_stdin", lambda: _payload(str(f)))
    monkeypatch.setattr(lint_fix, "find_ruff", lambda root: "ruff")
    seen = []

    def fake_run(ruff, *args, **kw):
        seen.append(kw.get("cwd"))
        return result(returncode=0)

    monkeypatch.setattr(lint_fix, "_run", fake_run)

    assert lint_fix.main() == 0
    assert seen and Path(seen[0]).resolve() == elsewhere.resolve()


def test_the_nearest_project_wins(tmp_path):
    """A box resolves to the box, not to the workspace above it -- the same order ruff
    itself discovers configuration in."""
    outer = _project(tmp_path)
    inner = _project(outer / "nested")
    assert lint_fix.project_root_for(inner / "x.py").resolve() == inner.resolve()


def test_a_linked_worktree_counts_as_a_project(tmp_path):
    """An ephemeral box is a linked worktree, and its files must still be linted.

    The marker it is claimed by is the config, not the `.git` *file* a linked worktree
    carries in place of a directory: a worktree checks out tracked files, and the ruff
    config is one of them. Asserted with the `.git` file present so the box shape is the
    one under test rather than a plain directory that happens to hold a config.
    """
    box = tmp_path / "box"
    box.mkdir()
    (box / ".git").write_text("gitdir: ../devkit/.git/worktrees/box\n", encoding="utf-8")
    (box / "ruff.toml").write_text("line-length = 100\n", encoding="utf-8")
    assert lint_fix.project_root_for(box / "x.py").resolve() == box.resolve()


def test_a_checkout_that_configures_no_ruff_is_not_a_project(tmp_path):
    """A repository is not a claim that anyone configured ruff for it.

    `.git` used to be a marker, so every checkout an agent can reach was in scope --
    including a reference checkout that ships no harness and is exempt from the rest of
    it. A `.py` file edited there was reformatted in place under ruff's *defaults* and
    could exit 2 on a finding those defaults could not fix, which is this hook applying
    rules nobody chose for that tree. Requiring a config file is the whole exemption,
    and it needs no list of which checkouts are excused.
    """
    reference = tmp_path / "reference-checkout"
    (reference / "src").mkdir(parents=True)
    (reference / ".git").mkdir()
    assert lint_fix.project_root_for(reference / "src" / "x.py") is None


def test_no_marker_anywhere_above_is_none(tmp_path):
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    assert lint_fix.project_root_for(deep / "x.py") is None


# ---- ruff_arg: repo-relative POSIX so per-file-ignores globs match ----------


def test_ruff_arg_repo_relative_posix_inside_project():
    target = lint_fix.REPO_ROOT / "scripts" / "hooks" / "stop.py"
    assert lint_fix.ruff_arg(target, lint_fix.REPO_ROOT) == "scripts/hooks/stop.py"


def test_ruff_arg_relativises_despite_lowercase_drive():
    # Regression: Claude Code's payload sends a lowercase-drive absolute path
    # (`c:\...`) while the hook runs with an uppercase-drive cwd. Passing that
    # straight to ruff defeated per-file-ignores; ruff_arg must still relativise
    # it (resolve() canonicalises the drive case) to the same repo-relative path.
    root = lint_fix.REPO_ROOT
    lower_drive_root = Path(str(root)[:1].lower() + str(root)[1:])
    target = lower_drive_root / "scripts" / "hooks" / "stop.py"
    assert lint_fix.ruff_arg(target, root) == "scripts/hooks/stop.py"


def test_ruff_arg_absolute_when_outside_project(tmp_path):
    outside = tmp_path / "x.py"
    assert lint_fix.ruff_arg(outside, lint_fix.REPO_ROOT) == str(outside)


# --- record_block: the harness-events ledger line behind every blocking run ---


def ledger_lines(base: Path) -> list[str]:
    """Every ledger line, across every shard -- the ledger is one file per machine.

    Reading the single unsharded name found nothing once the writers moved to a shard,
    and the `return []` below turned every assertion built on this into one that passes
    against an empty list.
    """
    lines: list[str] = []
    for path in sorted((base / "logs").glob("harness-events*.log")):
        if path.is_file():
            lines.extend(path.read_text(encoding="utf-8").splitlines())
    return lines


def test_record_block_writes_files_and_first_detail(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVKIT_DIR", str(tmp_path))
    lint_fix.record_block(["a.py", "b.py"], ["F821 undefined name", "second"])
    (line,) = ledger_lines(tmp_path)
    assert "\tevent=lint-fix-block\t" in line
    assert "\tfiles=a.py;b.py\t" in line
    assert line.endswith("\tdetail=F821 undefined name")
    assert "\tproject=" in line
    assert "\tversion=" in line


def test_record_block_without_the_ledger_module_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVKIT_DIR", str(tmp_path))
    monkeypatch.setattr(lint_fix, "harness_events", None)
    lint_fix.record_block(["a.py"], ["boom"])
    assert ledger_lines(tmp_path) == []


def test_main_records_when_it_blocks(monkeypatch, tmp_path, capsys):
    ledger_root = tmp_path / "devkit"
    ledger_root.mkdir()
    monkeypatch.setenv("DEVKIT_DIR", str(ledger_root))
    f = _project(tmp_path / "proj") / "x.py"
    f.write_text("x = 1\n")
    monkeypatch.setattr(lint_fix, "_read_stdin", lambda: _payload(str(f)))
    monkeypatch.setattr(lint_fix, "find_ruff", lambda root: "ruff")

    def fake_run(ruff, *args, **kw):
        if args[0] == "check" and "--fix" not in args:
            return result(returncode=1, stdout="x.py:1:1: F821 undefined name\n")
        return result(returncode=0)

    monkeypatch.setattr(lint_fix, "_run", fake_run)
    assert lint_fix.main() == 2
    (line,) = ledger_lines(ledger_root)
    assert "\tevent=lint-fix-block\t" in line
    assert "F821" in line
    capsys.readouterr()
