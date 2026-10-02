"""The `scripts/login.py` task entrypoint devkit's `scraper-login` action dispatches to."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_login():
    path = REPO_ROOT / "scripts" / "login.py"
    spec = importlib.util.spec_from_file_location("login_script", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_login_command_runs_the_cli_through_uv():
    login = _load_login()
    assert login.login_command("uv", "x") == ["uv", "run", "social-scraper", "login", "x"]


def test_main_runs_from_the_repo_root_so_the_profile_lands_under_data(monkeypatch):
    login = _load_login()
    calls = []

    def fake_run(cmd, cwd, check):
        calls.append((cmd, cwd))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(login.shutil, "which", lambda name: "/bin/uv")
    monkeypatch.setattr(login.subprocess, "run", fake_run)
    assert login.main(["x"]) == 0
    assert calls == [(["/bin/uv", "run", "social-scraper", "login", "x"], login.REPO_ROOT)]


def test_main_passes_the_cli_exit_code_through(monkeypatch):
    login = _load_login()
    monkeypatch.setattr(login.shutil, "which", lambda name: "/bin/uv")
    monkeypatch.setattr(
        login.subprocess, "run", lambda cmd, cwd, check: subprocess.CompletedProcess(cmd, 2)
    )
    assert login.main(["facebook"]) == 2


def test_main_reports_a_missing_uv_without_running_anything(monkeypatch, capsys):
    login = _load_login()
    monkeypatch.setattr(login.shutil, "which", lambda name: None)
    monkeypatch.setattr(login.subprocess, "run", lambda *a, **k: pytest.fail("ran without uv"))
    assert login.main(["x"]) == 2
    assert "uv is not on PATH" in capsys.readouterr().err


def test_main_requires_a_platform():
    login = _load_login()
    with pytest.raises(SystemExit):
        login.main([])
