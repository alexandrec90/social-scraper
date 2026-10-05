"""Unit tests for `toolchain.py` -- what a fresh checkout lacks, and the named fix.

**This file is vendored into every consuming project**, so it builds throwaway
checkouts of each dependency model rather than asserting anything about the repo it
runs in. The ladder here is the one `session-start.sh` prints at session start and
`ship.py --preflight` prints at the top of `/ship`; both callers are covered in their
own test modules, and what is pinned here is the detection those two now share.
"""

from __future__ import annotations

import os
import shlex
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


PATH_SOURCE = (
    PYPROJECT + '\n[tool.uv.sources]\ndata-lake = { path = "../data-lake", editable = true }\n'
    'other = { git = "https://example.invalid/other" }\n'
)


def test_a_path_dependency_missing_from_a_worktree_is_named_with_where_the_checkout_has_it(
    tmp_path,
):
    """ibkr_trader's `../data-lake` resolves beside the checkout and nowhere else: from a
    `.claude/worktrees/` tree every `uv run` died on `Distribution not found`, which says
    nothing about why. The gap names the path, the checkout's copy, and comes first,
    because `uv sync` cannot succeed until it is there."""
    main = _checkout(tmp_path / "ibkr", {"uv.lock": "", "pyproject.toml": PATH_SOURCE})
    (main.parent / "data-lake").mkdir()
    tree = _checkout(main / ".claude/worktrees/x", {"uv.lock": "", "pyproject.toml": PATH_SOURCE})
    (tree / ".git").write_text(f"gitdir: {main / '.git' / 'worktrees' / 'x'}\n", "utf-8")
    first, venv = tc.missing_toolchain(tree)
    assert "../data-lake" in first.what and "Distribution not found" in first.what
    assert str((main.parent / "data-lake").resolve()) in first.fix
    assert venv.what.startswith("No .venv")
    assert tc.missing_path_sources(main) == ()


def test_a_path_dependency_nothing_has_is_still_named(tmp_path):
    root = _checkout(tmp_path / "solo", {"pyproject.toml": PATH_SOURCE, ".venv/pyvenv.cfg": ""})
    (gap,) = tc.missing_toolchain(root)
    assert "../data-lake" in gap.what and str((root.parent / "data-lake").resolve()) in gap.fix


def test_an_unreadable_pyproject_names_no_path_dependency(tmp_path):
    root = _checkout(tmp_path, {"pyproject.toml": "[tool.uv.sources\n", ".venv/pyvenv.cfg": ""})
    assert tc.path_sources(root) == () and tc.missing_path_sources(root) == ()
    sources = _checkout(tmp_path / "ok", {"pyproject.toml": PATH_SOURCE})
    assert tc.path_sources(sources) == ("../data-lake",)


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


# --- provisioning a runner's tree: `rerun_in_venv` -------------------------------------


class FakeRun:
    """Records each command. An install step (its output captured) creates the venv
    interpreter unless told otherwise; the re-run itself, whose streams are the caller's,
    exits 7 so its code is traceable."""

    def __init__(self, root: Path, returncode: int = 0, output: str = "", make_venv: bool = True):
        self.root = root
        self.returncode = returncode
        self.output = output
        self.make_venv = make_venv
        self.calls: list[tuple[object, dict]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        if not kwargs.get("capture_output"):
            return subprocess.CompletedProcess(cmd, 7, "", "")
        if self.returncode == 0 and self.make_venv:
            python = tc.venv_python(self.root)
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, self.returncode, self.output, "")


ABSENT = "definitely_not_an_installed_module"
LOCKED = {"uv.lock": "", "pyproject.toml": PYPROJECT}


def test_the_venv_interpreter_is_spelled_for_the_platform(tmp_path, monkeypatch):
    monkeypatch.setattr(tc.os, "name", "nt")
    assert tc.venv_python(tmp_path) == tmp_path / ".venv" / "Scripts" / "python.exe"
    monkeypatch.setattr(tc.os, "name", "posix")
    assert tc.venv_python(tmp_path) == tmp_path / ".venv" / "bin" / "python"


def test_has_module_answers_without_importing():
    assert tc.has_module("json")
    assert not tc.has_module(ABSENT)
    # A dotted name whose parent is absent raises inside find_spec; that is a "no".
    assert not tc.has_module(f"{ABSENT}.child")


def test_provisioning_runs_the_named_command_in_the_tree(tmp_path, capsys):
    root = _checkout(tmp_path, LOCKED)
    run = FakeRun(root)
    assert tc.provision_python(root, run=run)
    [(cmd, kwargs)] = run.calls
    assert cmd == ["uv", "sync", "--all-extras", "--all-groups"]
    assert kwargs["cwd"] == root
    assert not kwargs.get("shell"), "argv, so no platform shell re-reads the quoting"
    assert "provisioning it: uv sync" in capsys.readouterr().err


def test_a_two_step_ladder_runs_in_order_and_stops_at_a_failure(tmp_path):
    root = _checkout(tmp_path, {"pyproject.toml": PYPROJECT})
    run = FakeRun(root)
    assert tc.provision_python(root, run=run)
    assert [cmd for cmd, _ in run.calls] == [
        [sys.executable, "-m", "venv", ".venv"],
        ["uv", "pip", "install", "-e", ".[dev]"],
    ]
    failing = FakeRun(root, returncode=1)
    assert not tc.provision_python(root, run=failing)
    assert len(failing.calls) == 1


def test_the_manifest_install_command_is_what_provisioning_runs(tmp_path):
    """carameli installs pip-tools locks through its own bootstrap, not `uv sync`; its
    `python` is this interpreter, not whatever lacks the module on `PATH`."""
    manifest = '[python]\ninstall_command = "python boot.py"\n'
    root = _checkout(tmp_path, {"requirements-dev.txt": "", ".devkit.toml": manifest})
    run = FakeRun(root)
    assert tc.provision_python(root, run=run)
    assert run.calls[0][0] == [sys.executable, "boot.py"]


def test_an_install_command_that_needs_a_shell_is_named_not_run(tmp_path, capsys):
    manifest = '[python]\ninstall_command = "make venv && make deps"\n'
    root = _checkout(tmp_path, {"pyproject.toml": PYPROJECT, ".devkit.toml": manifest})
    run = FakeRun(root)
    assert not tc.provision_python(root, run=run)
    assert run.calls == []
    assert "make venv && make deps needs a shell" in capsys.readouterr().err


def test_venv_argv_takes_the_pin_through_uv_and_the_default_through_python():
    assert tc.venv_argv("3.12") == ("uv", "venv", "--python", "3.12", ".venv")
    assert tc.venv_argv() == ("python", "-m", "venv", ".venv")


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        pytest.param("python boot.py", ((sys.executable, "boot.py"),), id="python-is-this-one"),
        pytest.param("uv sync --frozen", (("uv", "sync", "--frozen"),), id="plain-verbatim"),
        pytest.param("make a && make b", (), id="needs-a-shell"),
        pytest.param("pip install -r 'x y.txt'", (), id="quoted"),
        pytest.param("   ", (), id="blank"),
    ],
)
def test_install_argvs_runs_a_plain_install_command_and_refuses_shell_syntax(
    tmp_path, command, expected
):
    root = _checkout(tmp_path, LOCKED)
    assert tc.install_argvs(root, install_command=command) == expected


def test_install_argvs_without_a_command_is_the_ladder_on_this_interpreter(tmp_path):
    root = _checkout(tmp_path, {"pyproject.toml": PYPROJECT})
    assert tc.install_argvs(root) == (
        (sys.executable, "-m", "venv", ".venv"),
        ("uv", "pip", "install", "-e", ".[dev]"),
    )
    (tmp_path / "empty").mkdir()
    assert tc.install_argvs(_checkout(tmp_path / "empty", {})) == ()


@pytest.mark.parametrize(
    ("files", "count"),
    [
        pytest.param(LOCKED, 1, id="uv-lock"),
        pytest.param({"requirements-dev.txt": "", "requirements.txt": ""}, 2, id="locks"),
        pytest.param({"pyproject.toml": PYPROJECT}, 2, id="unlocked"),
    ],
)
def test_install_steps_are_the_displayed_fix(tmp_path, files, count):
    """One ladder: the line the reports print is the steps provisioning runs."""
    root = _checkout(tmp_path, files)
    steps = tc.python_steps(root, "3.12")
    assert len(steps) == count
    assert " && ".join(shlex.join(s) for s in steps) == tc.python_fix(root, python_version="3.12")


def test_a_failed_install_prints_the_command_and_its_tail(tmp_path, capsys):
    root = _checkout(tmp_path, LOCKED)
    noise = "\n".join(f"line {n}" for n in range(100))
    assert not tc.provision_python(root, run=FakeRun(root, returncode=2, output=noise))
    err = capsys.readouterr().err
    assert "exited 2" in err
    assert "line 99" in err
    assert "line 50" not in err, "only the tail, not the whole download log"


def test_an_install_that_leaves_no_interpreter_is_not_a_success(tmp_path):
    root = _checkout(tmp_path, LOCKED)
    assert not tc.provision_python(root, run=FakeRun(root, make_venv=False))


def test_an_installer_that_cannot_start_is_reported_not_raised(tmp_path, capsys):
    root = _checkout(tmp_path, LOCKED)

    def missing(*_args, **_kwargs):
        raise FileNotFoundError("uv")

    assert not tc.provision_python(root, run=missing)
    assert "FileNotFoundError" in capsys.readouterr().err


def test_nothing_to_install_from_runs_nothing(tmp_path):
    root = _checkout(tmp_path, {})
    run = FakeRun(root)
    assert not tc.provision_python(root, run=run)
    assert run.calls == []


def test_a_missing_path_dependency_is_named_instead_of_attempting_the_sync(tmp_path, capsys):
    pyproject = PYPROJECT + '[tool.uv.sources]\nlake = { path = "../lake", editable = true }\n'
    root = _checkout(tmp_path, {"uv.lock": "", "pyproject.toml": pyproject})
    run = FakeRun(root)
    assert not tc.provision_python(root, run=run)
    assert run.calls == []
    assert "../lake is not here" in capsys.readouterr().err


def test_an_interpreter_that_has_the_module_carries_on_untouched(tmp_path):
    root = _checkout(tmp_path, LOCKED)
    run = FakeRun(root)
    assert tc.rerun_target(root, "json", env={}, run=run) is None
    assert run.calls == []


def test_an_existing_tree_venv_is_used_without_installing(tmp_path):
    root = _checkout(tmp_path, LOCKED)
    python = tc.venv_python(root)
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    run = FakeRun(root)
    assert tc.rerun_target(root, ABSENT, env={}, run=run) == python
    assert run.calls == []


def test_a_tree_with_no_venv_is_provisioned_then_used(tmp_path):
    root = _checkout(tmp_path, LOCKED)
    run = FakeRun(root)
    assert tc.rerun_target(root, ABSENT, env={}, run=run) == tc.venv_python(root)
    assert len(run.calls) == 1


@pytest.mark.parametrize(
    "env", [{"CI": "true"}, {tc.RERUN_ENV: "1"}], ids=["ci-builds-its-own", "already-a-rerun"]
)
def test_ci_and_a_rerun_never_provision(tmp_path, env):
    root = _checkout(tmp_path, LOCKED)
    run = FakeRun(root)
    assert tc.rerun_target(root, ABSENT, env=env, run=run) is None
    assert run.calls == []


def test_a_failed_provision_leaves_the_runner_to_carry_on(tmp_path):
    root = _checkout(tmp_path, LOCKED)
    assert tc.rerun_target(root, ABSENT, env={}, run=FakeRun(root, returncode=1)) is None


def test_the_rerun_is_the_same_script_and_arguments_marked_as_a_rerun(tmp_path, capsys):
    root = _checkout(tmp_path, LOCKED)
    run = FakeRun(root)
    script = root / "scripts" / "run-tests.py"
    code = tc.rerun_in_venv(root, ABSENT, script, ["--all", "-k", "x"], env={"KEEP": "1"}, run=run)
    assert code == 7, "the re-run's own exit code is the caller's"
    cmd, kwargs = run.calls[-1]
    assert cmd == [str(tc.venv_python(root)), str(script), "--all", "-k", "x"]
    assert kwargs["cwd"] == root
    assert kwargs["env"] == {"KEEP": "1", tc.RERUN_ENV: "1"}
    assert f"no {ABSENT}; re-running under" in capsys.readouterr().err


def test_no_rerun_returns_none_and_runs_nothing(tmp_path):
    root = _checkout(tmp_path, LOCKED)
    run = FakeRun(root)
    assert tc.rerun_in_venv(root, "json", root / "x.py", [], env={}, run=run) is None
    assert run.calls == []


# --- a tree `.venv` older than its lock: `resync_if_stale` -----------------------------


def _venv(root: Path, age: float) -> None:
    """An existing `.venv`, its `pyvenv.cfg` dated `age` seconds before the lock files."""
    python = tc.venv_python(root)
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("", encoding="utf-8")
    cfg = root / ".venv" / "pyvenv.cfg"
    cfg.write_text("home = x\n", encoding="utf-8")
    newest = max(f.stat().st_mtime for f in root.iterdir() if f.is_file())
    os.utime(cfg, (newest - age, newest - age))


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        pytest.param(LOCKED, ["uv.lock"], id="uv-lock-not-pyproject"),
        pytest.param(
            {"requirements-dev.txt": "", "requirements.txt": ""},
            ["requirements.txt", "requirements-dev.txt"],
            id="locks",
        ),
        pytest.param({"pyproject.toml": PYPROJECT}, ["pyproject.toml"], id="unlocked"),
        pytest.param({"requirements.txt": ""}, [], id="nothing-the-ladder-reads"),
    ],
)
def test_dependency_files_are_the_ones_the_ladder_installs_from(tmp_path, files, expected):
    root = _checkout(tmp_path, files)
    assert [f.name for f in tc.dependency_files(root)] == expected


def test_a_venv_is_stale_once_its_lock_is_newer_than_its_last_install(tmp_path):
    root = _checkout(tmp_path, LOCKED)
    assert tc.stale_dependency(root) is None, "no venv: nothing to date"
    _venv(root, age=60)
    assert tc.stale_dependency(root) == root / "uv.lock"
    (root / ".venv" / tc.SYNC_STAMP).touch()
    os.utime(root / "uv.lock", (0, 0))
    assert tc.stale_dependency(root) is None, "the stamp dates a sync after the lock"


def test_a_venv_with_neither_mark_is_not_judged(tmp_path):
    root = _checkout(tmp_path, LOCKED)
    (root / ".venv").mkdir()
    assert tc.stale_dependency(root) is None


def test_a_borrowed_venv_is_the_checkouts_to_sync_not_this_trees(tmp_path):
    checkout = _checkout(tmp_path / "checkout", LOCKED)
    _venv(checkout, age=60)
    tree = _checkout(tmp_path / "tree", LOCKED)
    if sys.platform == "win32":  # spelled so mypy narrows `_winapi` off Windows
        # A junction, which Windows creates unprivileged, where a symlink needs developer mode.
        import _winapi

        _winapi.CreateJunction(str(checkout / ".venv"), str(tree / ".venv"))
    else:
        (tree / ".venv").symlink_to(checkout / ".venv", target_is_directory=True)
    assert tc.stale_dependency(checkout) == checkout / "uv.lock"
    assert tc.stale_dependency(tree) is None


def test_resync_if_stale_syncs_only_a_stale_venv_and_says_whether_it_did(tmp_path):
    root = _checkout(tmp_path, LOCKED)
    run = FakeRun(root)
    assert not tc.resync_if_stale(root, run=run), "no venv: provisioning is not a re-sync"
    _venv(root, age=60)
    assert tc.resync_if_stale(root, run=run)
    assert not tc.resync_if_stale(root, run=run), "stamped: nothing left to sync"
    assert len(run.calls) == 1


@pytest.mark.parametrize(
    ("step", "creates"),
    [
        pytest.param((sys.executable, "-m", "venv", ".venv"), True, id="python-m-venv"),
        pytest.param(tc.venv_argv("3.12"), True, id="uv-venv"),
        pytest.param(("uv", "sync", "--all-extras"), False, id="sync"),
        pytest.param(("uv", "pip", "install", "-e", ".[dev]"), False, id="install"),
    ],
)
def test_creates_venv_names_the_step_a_resync_skips(step, creates):
    assert tc.creates_venv(step) is creates


def test_runs_tree_venv_compares_the_interpreter_prefix(tmp_path):
    root = _checkout(tmp_path, LOCKED)
    (root / ".venv").mkdir()
    assert tc.runs_tree_venv(root, prefix=str(root / ".venv"))
    assert not tc.runs_tree_venv(root, prefix=str(tmp_path))


def test_a_fresh_provision_stamps_the_venv(tmp_path):
    root = _checkout(tmp_path, LOCKED)
    assert tc.provision_python(root, run=FakeRun(root))
    assert (root / ".venv" / tc.SYNC_STAMP).is_file()


def test_the_tree_venv_running_behind_its_lock_is_resynced_then_carries_on(
    tmp_path, monkeypatch, capsys
):
    """The filed friction: `.venv/Scripts/python.exe scripts/run-tests.py` after a merge
    that added pytz to `uv.lock` -- pytest present, so the venv was used as-is and the
    run died on `No module named 'pytz'`."""
    root = _checkout(tmp_path, LOCKED)
    _venv(root, age=60)
    monkeypatch.setattr(tc, "runs_tree_venv", lambda r: r == root)
    run = FakeRun(root)
    assert tc.rerun_target(root, "json", env={}, run=run) is None
    assert [cmd for cmd, _ in run.calls] == [["uv", "sync", "--all-extras", "--all-groups"]]
    assert ".venv predates uv.lock; re-syncing it" in capsys.readouterr().err
    assert (root / ".venv" / tc.SYNC_STAMP).is_file()
    assert tc.stale_dependency(root) is None
    again = FakeRun(root)
    assert tc.rerun_target(root, "json", env={}, run=again) is None
    assert again.calls == [], "synced once, not on every run"


def test_another_interpreter_that_has_the_module_leaves_a_stale_venv_alone(tmp_path):
    root = _checkout(tmp_path, LOCKED)
    _venv(root, age=60)
    run = FakeRun(root)
    assert tc.rerun_target(root, "json", env={}, run=run) is None
    assert run.calls == [], "the tree venv is not what runs, so it is not this run's to sync"


def test_a_stale_venv_is_resynced_before_the_rerun_and_never_recreated(tmp_path):
    root = _checkout(tmp_path, {"pyproject.toml": PYPROJECT})
    _venv(root, age=60)
    run = FakeRun(root)
    assert tc.rerun_target(root, ABSENT, env={}, run=run) == tc.venv_python(root)
    assert [cmd for cmd, _ in run.calls] == [["uv", "pip", "install", "-e", ".[dev]"]]


@pytest.mark.parametrize(
    "env", [{"CI": "true"}, {tc.RERUN_ENV: "1"}], ids=["ci-builds-its-own", "already-a-rerun"]
)
def test_ci_and_a_rerun_never_resync(tmp_path, monkeypatch, env):
    root = _checkout(tmp_path, LOCKED)
    _venv(root, age=60)
    monkeypatch.setattr(tc, "runs_tree_venv", lambda r: True)
    run = FakeRun(root)
    assert tc.rerun_target(root, "json", env=env, run=run) is None
    assert run.calls == []


def test_a_failed_resync_is_reported_and_the_run_carries_on_unstamped(tmp_path, capsys):
    root = _checkout(tmp_path, LOCKED)
    _venv(root, age=60)
    run = FakeRun(root, returncode=1)
    assert tc.rerun_target(root, ABSENT, env={}, run=run) == tc.venv_python(root)
    assert "exited 1" in capsys.readouterr().err
    assert not (root / ".venv" / tc.SYNC_STAMP).exists()
    assert tc.stale_dependency(root) == root / "uv.lock", "still stale, so the next run retries"
