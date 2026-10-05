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

    assert "tests/test_cli.py" in calls["mypy"]
    assert "scripts/hooks/tests/test_toolchain.py" not in calls["mypy"]
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

    assert sections == "# ruff\n# mypy\n"
    # Both fix passes leave the vendored harness alone.
    assert all("scripts/hooks" in cmd for cmd in fixes) and len(fixes) == 2


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
    assert "mypy" not in calls
