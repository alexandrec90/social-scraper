"""Unit tests for `toolchain.py` -- what a fresh checkout lacks, and the named fix.

**This file is vendored into every consuming project**, so it builds throwaway
checkouts of each dependency model rather than asserting anything about the repo it
runs in. The ladder here is the one `session-start.sh` prints at session start and
`ship.py --preflight` prints at the top of `/ship`; both callers are covered in their
own test modules, and what is pinned here is the detection those two now share.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import REPO_ROOT, load_module

tc = load_module("scripts/hooks/toolchain.py")
hc = load_module("scripts/hooks/harness_config.py")

PYPROJECT = '[project]\nname = "probe"\nversion = "0.1.0"\n'


def _checkout(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "proj"
    for name, text in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    root.mkdir(exist_ok=True)
    return root


# --- the Python ladder ---------------------------------------------------------


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        pytest.param(
            {"uv.lock": "", "pyproject.toml": PYPROJECT},
            "uv sync --all-extras --all-groups",
            id="uv-lock",
        ),
        pytest.param(
            {"requirements.txt": "ruff==0.15.0\n", "requirements-dev.txt": "uv==0.4.0\n"},
            "python -m venv .venv && uv pip install -r requirements.txt -r requirements-dev.txt",
            id="requirements-locks",
        ),
        pytest.param(
            {"requirements-dev.txt": "uv==0.4.0\n"},
            "python -m venv .venv && uv pip install -r requirements-dev.txt",
            id="dev-lock-only",
        ),
        pytest.param(
            {"pyproject.toml": PYPROJECT},
            "python -m venv .venv && uv pip install -e '.[dev]'",
            id="unlocked-pyproject",
        ),
    ],
)
def test_the_named_command_matches_the_dependency_model(tmp_path, files, expected):
    """Same ladder as the installers, in the same order -- a command that does not fit
    the project is worse than none, because it is followed."""
    assert tc.python_fix(_checkout(tmp_path, files)) == expected


def test_the_lockfile_wins_over_the_pyproject_beside_it(tmp_path):
    """A project with both must not be installed twice; the lock is the pinned one."""
    root = _checkout(
        tmp_path,
        {"uv.lock": "", "requirements-dev.txt": "x\n", "pyproject.toml": PYPROJECT},
    )
    assert tc.python_fix(root).startswith("uv sync")


def test_the_manifest_install_command_wins_over_detection(tmp_path):
    root = _checkout(tmp_path, {"uv.lock": "", "pyproject.toml": PYPROJECT})
    assert tc.python_fix(root, install_command="make bootstrap") == "make bootstrap"


def test_a_project_with_no_dependency_file_names_no_command(tmp_path):
    assert tc.python_fix(_checkout(tmp_path, {"README.md": "# probe\n"})) == ""


def test_the_pinned_interpreter_reaches_the_venv_and_the_sync(tmp_path):
    """`requires-python` in a lock is a floor, so a project pinned to 3.12 resolves happily
    on 3.14 unless the pin is passed through -- and `python -m venv` can only ever copy
    the interpreter running it, which is why the pinned spelling is `uv venv`."""
    locked = _checkout(tmp_path / "a", {"uv.lock": "", "pyproject.toml": PYPROJECT})
    assert tc.python_fix(locked, python_version="3.12").endswith("--python 3.12")
    unlocked = _checkout(tmp_path / "b", {"pyproject.toml": PYPROJECT})
    assert tc.python_fix(unlocked, python_version="3.12").startswith(
        "uv venv --python 3.12 .venv && "
    )
    assert tc.venv_command() == "python -m venv .venv"


# --- the frontend half ---------------------------------------------------------


def test_a_committed_lock_is_installed_with_ci_not_install(tmp_path):
    """`npm install` rewrites the lockfile, which is a tracked change in a worktree that
    has edited nothing -- and the dirty tree `ship.py` then refuses to push."""
    root = _checkout(tmp_path, {"web/package.json": "{}\n", "web/package-lock.json": "{}\n"})
    assert tc.frontend_fix(root, "web") == "npm ci --prefix web"


def test_an_unlocked_frontend_is_installed_with_install(tmp_path):
    root = _checkout(tmp_path, {"web/package.json": "{}\n"})
    assert tc.frontend_fix(root, "web") == "npm install --prefix web"


# --- the report ----------------------------------------------------------------


def test_a_gap_renders_as_the_state_and_the_fix():
    """The `(fix: ...)` shape every session-start line already uses."""
    assert tc.Gap("No .venv here", "uv sync").line == "No .venv here (fix: uv sync)"


def test_a_fresh_worktree_is_told_both_halves(tmp_path):
    root = _checkout(
        tmp_path,
        {
            "uv.lock": "",
            "pyproject.toml": PYPROJECT,
            ".devkit.toml": '[frontend]\nenabled = true\ndir = "web"\n',
            "web/package.json": "{}\n",
            "web/package-lock.json": "{}\n",
        },
    )
    gaps = tc.missing_toolchain(root)
    assert [g.what for g in gaps] == [
        "No .venv here -- ruff/mypy/pytest are unavailable",
        "No web/node_modules -- the frontend linters are unavailable",
    ]
    assert [g.fix for g in gaps] == ["uv sync --all-extras --all-groups", "npm ci --prefix web"]
    assert gaps[0].line == (
        "No .venv here -- ruff/mypy/pytest are unavailable (fix: uv sync --all-extras --all-groups)"
    )


def test_a_provisioned_checkout_is_told_nothing(tmp_path):
    """The no-op half: every session and every ship on a working checkout pays this."""
    root = _checkout(
        tmp_path,
        {
            "uv.lock": "",
            "pyproject.toml": PYPROJECT,
            ".venv/pyvenv.cfg": "",
            ".devkit.toml": '[frontend]\nenabled = true\ndir = "web"\n',
            "web/package.json": "{}\n",
            "web/node_modules/.keep": "",
        },
    )
    assert tc.missing_toolchain(root) == ()


def test_a_frontend_tier_is_judged_only_when_switched_on_and_present(tmp_path):
    off = _checkout(tmp_path / "off", {"pyproject.toml": PYPROJECT, "web/package.json": "{}\n"})
    assert all("node_modules" not in g.what for g in tc.missing_toolchain(off))
    absent = _checkout(
        tmp_path / "absent",
        {"pyproject.toml": PYPROJECT, ".devkit.toml": '[frontend]\nenabled = true\ndir = "web"\n'},
    )
    assert all("node_modules" not in g.what for g in tc.missing_toolchain(absent))


def test_the_manifest_is_read_once_when_the_caller_already_holds_it(tmp_path):
    root = _checkout(tmp_path, {"uv.lock": "", "pyproject.toml": PYPROJECT})
    cfg = hc.from_dict({"python": {"install_command": "make bootstrap", "version": "3.12"}})
    (gap,) = tc.missing_toolchain(root, cfg)
    assert gap.fix == "make bootstrap"


def test_a_project_with_no_dependency_file_is_told_nothing(tmp_path):
    assert tc.missing_toolchain(_checkout(tmp_path, {"README.md": "# probe\n"})) == ()


# --- the CLI `session-start.sh` reads ------------------------------------------


def _cli(*argv: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts/hooks/toolchain.py"), *argv],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )


def test_the_cli_prints_one_gap_per_line_from_the_cwd(tmp_path):
    root = _checkout(tmp_path, {"uv.lock": "", "pyproject.toml": PYPROJECT})
    run = _cli(cwd=root)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == [
        "No .venv here -- ruff/mypy/pytest are unavailable (fix: uv sync --all-extras --all-groups)"
    ]


def test_the_cli_takes_a_root_and_prints_nothing_for_a_provisioned_one(tmp_path):
    root = _checkout(tmp_path, {"pyproject.toml": PYPROJECT, ".venv/pyvenv.cfg": ""})
    run = _cli("--root", str(root), cwd=tmp_path)
    assert run.returncode == 0, run.stderr
    assert run.stdout == ""


def test_the_cli_never_exits_non_zero(tmp_path):
    """The shell caller has no handler for a failure, and a hook must not die over a
    report. `main` is driven in-process too, so the guard is asserted rather than
    inferred from a subprocess that happened to succeed."""
    run = _cli("--root", str(tmp_path / "nowhere"), cwd=tmp_path)
    assert run.returncode == 0
    assert tc.main(["--root", str(tmp_path / "nowhere")]) == 0


def test_a_root_that_cannot_be_read_is_reported_to_stderr_not_raised(tmp_path, monkeypatch, capsys):
    def explode(*_args, **_kwargs):
        raise OSError("boom")

    monkeypatch.setattr(tc, "missing_toolchain", explode)
    assert tc.main(["--root", str(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "OSError" in captured.err
