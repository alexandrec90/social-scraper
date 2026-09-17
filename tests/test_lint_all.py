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
