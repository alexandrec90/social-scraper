"""Behavioral coverage for the project-owned lint runner."""

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_lint_all():
    path = REPO_ROOT / "scripts" / "lint-all.py"
    spec = importlib.util.spec_from_file_location("lint_all", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_main_clears_the_artifact_when_explicit_paths_need_no_linter(tmp_path, monkeypatch):
    lint_all = _load_lint_all()
    artifact = tmp_path / "logs" / "lint-errors.log"
    documentation = tmp_path / "README.md"
    documentation.write_text("documentation only\n", encoding="utf-8")
    artifact.parent.mkdir()
    artifact.write_text("stale failure\n", encoding="utf-8")
    monkeypatch.setattr(lint_all, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(lint_all, "ARTIFACT", artifact)

    assert lint_all.main(["--paths", "README.md"]) == 0
    assert artifact.read_text(encoding="utf-8") == ""


def _rooted_with_the_hook(lint_all, monkeypatch, tmp_path, verdict=""):
    """`lint_all` rooted at `tmp_path`, whose commit stage declares the secrets hook; every
    tool answers `verdict` and is recorded in the list this returns."""
    (tmp_path / ".pre-commit-config.yaml").write_text("- id: detect-secrets\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("words\n", encoding="utf-8")
    monkeypatch.setattr(lint_all, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(lint_all, "ARTIFACT", tmp_path / "logs" / "lint-errors.log")
    calls = []

    def record(name, cmd, _hint):
        calls.append((name, cmd))
        return verdict

    monkeypatch.setattr(lint_all, "run_tool", record)
    return calls


def test_secrets_targets_are_every_file_only_where_the_hook_is_declared(tmp_path, monkeypatch):
    lint_all = _load_lint_all()
    monkeypatch.setattr(lint_all, "REPO_ROOT", tmp_path)
    assert lint_all.secrets_targets(["a.md"], skip=False) == [], "no config, nothing to mirror"
    config = tmp_path / ".pre-commit-config.yaml"
    config.write_text("- id: ruff\n", encoding="utf-8")
    assert lint_all.secrets_targets(["a.md"], skip=False) == []
    config.write_text("      - id: detect-secrets\n        exclude: x\n", encoding="utf-8")
    assert lint_all.secrets_targets(["a.md", "b.py"], skip=False) == ["a.md", "b.py"]
    assert lint_all.secrets_targets(["a.md"], skip=True) == []
    assert lint_all.secrets_targets([], skip=False) == []


def test_a_narrowed_run_scans_a_file_no_other_pass_reads(tmp_path, monkeypatch):
    """This runner passed what the commit then refused, a whole dispatch later: the only
    check before `/ship` that reads a non-Python file is the one that refused it."""
    lint_all = _load_lint_all()
    calls = _rooted_with_the_hook(lint_all, monkeypatch, tmp_path)

    assert lint_all.main(["--paths", "notes.md"]) == 0
    assert calls == [
        (
            "detect-secrets",
            [sys.executable, "-m", "pre_commit", "run", "detect-secrets", "--files", "notes.md"],
        )
    ]


def test_a_refused_scan_fails_the_run(tmp_path, monkeypatch):
    lint_all = _load_lint_all()
    _rooted_with_the_hook(lint_all, monkeypatch, tmp_path, verdict="# detect-secrets\nfound\n")

    assert lint_all.main(["--paths", "notes.md"]) == 1
    assert "# detect-secrets" in lint_all.ARTIFACT.read_text(encoding="utf-8")


def test_no_secrets_skips_the_scan(tmp_path, monkeypatch):
    lint_all = _load_lint_all()
    calls = _rooted_with_the_hook(lint_all, monkeypatch, tmp_path)

    assert lint_all.main(["--paths", "notes.md", "--no-secrets"]) == 0
    assert calls == []


def test_secrets_section_has_nothing_to_say_about_no_files():
    assert _load_lint_all().secrets_section([]) == ""


def test_main_hands_the_run_to_the_tree_venv_when_this_interpreter_has_no_ruff(monkeypatch):
    """Run with the system Python, it said "ruff: not installed", "mypy: not installed"
    and then "lint-all: clean" from a tree whose `.venv` held both."""
    lint_all = _load_lint_all()
    # By name, as main() imports it: from `scripts/hooks`, which is not on mypy's path.
    monkeypatch.syspath_prepend(str(REPO_ROOT / "scripts" / "hooks"))
    toolchain = importlib.import_module("toolchain")

    calls = []

    def rerun(root, module, script, argv):
        calls.append((root, module, script, argv))
        return 7

    monkeypatch.setattr(toolchain, "rerun_in_venv", rerun)
    monkeypatch.setattr(
        lint_all, "python_sections", lambda *_a: (_ for _ in ()).throw(AssertionError)
    )

    assert lint_all.main(["--changed"]) == 7
    assert calls == [(REPO_ROOT, "ruff", REPO_ROOT / "scripts" / "lint-all.py", ["--changed"])]


def test_selection_narrows_to_the_named_paths_or_the_diff_and_otherwise_not_at_all(monkeypatch):
    lint_all = _load_lint_all()
    monkeypatch.setattr(lint_all, "explicit_paths", lambda paths: [f"named:{p}" for p in paths])
    monkeypatch.setattr(lint_all, "changed_paths", lambda: ["diff.py"])
    parser = lint_all.build_parser()

    assert lint_all.selection(parser.parse_args(["--paths", "a.py"])) == (True, ["named:a.py"])
    assert lint_all.selection(parser.parse_args(["--changed"])) == (True, ["diff.py"])
    assert lint_all.selection(parser.parse_args([])) == (False, [])


def test_rerun_in_tree_venv_carries_on_here_without_the_vendored_toolchain(tmp_path, monkeypatch):
    lint_all = _load_lint_all()
    monkeypatch.setattr(lint_all, "REPO_ROOT", tmp_path)

    assert lint_all.rerun_in_tree_venv("ruff", []) is None


def test_mypy_targets_keep_only_files_inside_the_mypy_scope():
    lint_all = _load_lint_all()

    assert lint_all.mypy_targets(
        [
            "social_scraper/cli.py",
            "tests/test_cli.py",
            "scripts/hooks/tests/test_toolchain.py",
            "scripts/lint-all.py",
        ]
    ) == ["social_scraper/cli.py", "tests/test_cli.py"]


def test_mypy_targets_is_empty_when_nothing_changed_is_in_scope():
    lint_all = _load_lint_all()

    assert lint_all.mypy_targets(["scripts/hooks/toolchain.py"]) == []


def test_scoped_run_never_hands_mypy_the_vendored_harness(tmp_path, monkeypatch):
    # A merge that brought in a devkit upgrade put `scripts/hooks/tests/test_toolchain.py`
    # on the same mypy command line as `tests/`, and the two `conftest` modules collided.
    lint_all = _load_lint_all()
    for name in ("tests/test_cli.py", "scripts/hooks/tests/test_toolchain.py"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("", encoding="utf-8")
    monkeypatch.setattr(lint_all, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(lint_all, "ARTIFACT", tmp_path / "logs" / "lint-errors.log")
    monkeypatch.setattr(lint_all.subprocess, "run", lambda *a, **k: None)
    calls: dict[str, list[str]] = {}
    monkeypatch.setattr(
        lint_all, "run_tool", lambda name, cmd, hint: calls.setdefault(name, cmd) and ""
    )

    lint_all.main(["--paths", "tests/test_cli.py", "scripts/hooks/tests/test_toolchain.py"])

    for platform in lint_all.MYPY_PLATFORMS:
        assert "tests/test_cli.py" in calls[f"mypy ({platform})"]
        assert "scripts/hooks/tests/test_toolchain.py" not in calls[f"mypy ({platform})"]
    assert "scripts/hooks/tests/test_toolchain.py" in calls["ruff"]


def test_build_parser_accepts_the_ship_and_stop_hook_flags():
    lint_all = _load_lint_all()

    args = lint_all.build_parser().parse_args(["--paths", "a.py", "b.py", "--no-secrets"])

    assert args.paths == ["a.py", "b.py"]
    assert args.no_secrets is True
    assert args.changed is False


def test_python_sections_reports_ruff_and_mypy_failures(monkeypatch):
    lint_all = _load_lint_all()
    fixes: list[list[str]] = []
    monkeypatch.setattr(lint_all.subprocess, "run", lambda cmd, **k: fixes.append(cmd))
    monkeypatch.setattr(lint_all, "run_tool", lambda name, cmd, hint: f"# {name}\n")

    sections = lint_all.python_sections(["tests/test_cli.py"], ["tests/test_cli.py"])

    assert sections == "# ruff\n# mypy (linux)\n# mypy (win32)\n"
    # Both fix passes leave the vendored harness alone.
    assert all("scripts/hooks" in cmd for cmd in fixes) and len(fixes) == 2


def test_mypy_checks_every_platform_whatever_the_host(monkeypatch):
    # `scratch.py` called `ctypes.WinDLL` in a helper only reached on Windows. mypy on
    # the Windows desktop passed it and mypy on the Linux gate did not, so main went red
    # with a local lint that said clean. Each pass names its platform and its own cache:
    # one shared cache would be rebuilt from scratch every time the platform flips.
    lint_all = _load_lint_all()
    monkeypatch.setattr(lint_all.subprocess, "run", lambda *a, **k: None)
    calls: dict[str, list[str]] = {}
    monkeypatch.setattr(
        lint_all, "run_tool", lambda name, cmd, hint: calls.setdefault(name, cmd) and ""
    )

    lint_all.python_sections(["social_scraper/scratch.py"], ["social_scraper/scratch.py"])

    assert {"linux", "win32"} <= set(lint_all.MYPY_PLATFORMS)
    caches = set()
    for platform in lint_all.MYPY_PLATFORMS:
        cmd = calls[f"mypy ({platform})"]
        assert cmd[cmd.index("--platform") + 1] == platform
        caches.add(cmd[cmd.index("--cache-dir") + 1])
    assert len(caches) == len(lint_all.MYPY_PLATFORMS)


def test_python_sections_skips_mypy_with_nothing_typed(monkeypatch):
    lint_all = _load_lint_all()
    monkeypatch.setattr(lint_all.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(lint_all, "run_tool", lambda name, cmd, hint: f"# {name}\n")

    assert lint_all.python_sections(["scripts/hooks/toolchain.py"], []) == "# ruff\n"


def test_scoped_run_skips_mypy_when_only_the_harness_changed(tmp_path, monkeypatch):
    lint_all = _load_lint_all()
    harness = tmp_path / "scripts" / "hooks" / "toolchain.py"
    harness.parent.mkdir(parents=True)
    harness.write_text("", encoding="utf-8")
    monkeypatch.setattr(lint_all, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(lint_all, "ARTIFACT", tmp_path / "logs" / "lint-errors.log")
    monkeypatch.setattr(lint_all.subprocess, "run", lambda *a, **k: None)
    calls: dict[str, list[str]] = {}
    monkeypatch.setattr(
        lint_all, "run_tool", lambda name, cmd, hint: calls.setdefault(name, cmd) and ""
    )

    lint_all.main(["--paths", "scripts/hooks/toolchain.py"])

    assert "ruff" in calls
    assert not any(name.startswith("mypy") for name in calls)
