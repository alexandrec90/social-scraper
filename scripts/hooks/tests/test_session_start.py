"""Integration tests for the SessionStart provisioning hook.

`.claude/hooks/session-start.sh` had **no tests**, and that is precisely how it
shipped a bug that broke every project except the one it was written for: it read
`requirements-dev.txt` unconditionally and expanded `${uv_version:?...}`. `:?` on
an empty value *exits* a non-interactive shell, and the trailing `|| echo` cannot
catch a parameter-expansion failure — so in any project without pip-tools locks,
provisioning died mid-script, before the PATH export, leaving an empty venv and no
ruff/mypy/pytest. Only remote sandboxes run that code path, so nothing local ever
went red.

**This file is vendored into every consuming project**, so it may not assert any
one project's dependency model. It builds throwaway projects of each shape and
checks the script dispatches correctly in all of them.

The script is driven for real (`CLAUDE_CODE_REMOTE=true`) with stub executables on
PATH, so nothing is installed and no network is touched. Every stub appends its
argv to a log, which is what the assertions read.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import REPO_ROOT

SCRIPT = REPO_ROOT / ".claude" / "hooks" / "session-start.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(
    BASH is None or not SCRIPT.exists(), reason="needs bash and .claude/hooks/session-start.sh"
)

# The line whose absence *is* the bug: provisioning must reach the end of the
# script and persist the venv on PATH, whatever the project's dependency model.
PATH_EXPORT = "export PATH="


def _write_exec(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _make_project(tmp_path: Path, files: dict[str, str]) -> tuple[Path, Path, Path]:
    """A throwaway project plus a stub bin dir. Returns (project, log, env_file)."""
    project = tmp_path / "proj"
    project.mkdir()
    for name, text in files.items():
        target = project / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    # The real config loader, so the manifest seam is exercised rather than faked.
    (project / "scripts" / "hooks").mkdir(parents=True, exist_ok=True)
    shutil.copy(
        REPO_ROOT / "scripts" / "hooks" / "harness_config.py",
        project / "scripts" / "hooks" / "harness_config.py",
    )

    log = tmp_path / "calls.log"
    stub_bin = tmp_path / "bin"

    # `python3` must stay real for `harness_config.py` (that call is the seam under
    # test); anything else is logged and skipped.
    _write_exec(
        stub_bin / "python3",
        f'#!/bin/sh\necho "python3 $*" >> "{log}"\n'
        f'case "$*" in *harness_config.py*) exec "{sys.executable}" "$@";; esac\nexit 0\n',
    )
    for tool in ("npm", "curl", "node", "pre-commit"):
        _write_exec(stub_bin / tool, f'#!/bin/sh\necho "{tool} $*" >> "{log}"\nexit 0\n')
    # Pre-created so the script's `[ -d .venv ]` short-circuits venv creation.
    _write_exec(
        project / ".venv" / "bin" / "python",
        f'#!/bin/sh\necho "venv-python $*" >> "{log}"\nexit 0\n',
    )

    return project, log, tmp_path / "env_file"


def _run(
    project: Path,
    log: Path,
    env_file: Path,
    *,
    inherit_path: bool = True,
    remote: bool = True,
    extra_env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """Drive the script with the stub bin dir first on PATH.

    `inherit_path=False` drops the real PATH entirely, keeping only the stubs plus the
    directories bash itself needs. That is the only way to test a *missing* tool: deleting
    a stub is not enough when the real binary is installed in the environment running the
    tests, which is exactly the case in CI (`uv run pre-commit` puts it on PATH) and made
    the pre-commit-absent test pass alone and fail in a full run.

    `remote=False` drives the LOCAL branch — the one every test here used to skip, which
    is how the pre-commit wiring came to be reachable only from a sandbox. The project is
    not a git repository, so the auto-rebase half finds no branch and stands down; the
    branch-protection tests below build a real one when they need it.
    """
    # `pytestmark` already skips this module when bash is absent, but that is a runtime
    # guard a type-checker cannot see — assert so `BASH` narrows from `str | None`.
    assert BASH is not None
    env = dict(os.environ)
    stub_bin = project.parent / "bin"
    if inherit_path:
        path = f"{stub_bin}{os.pathsep}{env.get('PATH', '')}"
    else:
        # /usr/bin and /bin only: bash needs its own coreutils (sed, mkdir, command),
        # and none of those are what any of these tests stub out.
        path = os.pathsep.join([str(stub_bin), "/usr/bin", "/bin"])
    env.update(
        CLAUDE_CODE_REMOTE="true" if remote else "false",
        CLAUDE_PROJECT_DIR=str(project),
        CLAUDE_ENV_FILE=str(env_file),
        # Do not inherit the developer machine's global core.hooksPath. Individual
        # tests populate this isolated file when they need the installed state.
        GIT_CONFIG_GLOBAL=str(project.parent / "gitconfig"),
        PATH=path,
    )
    env.update(extra_env or {})
    proc = subprocess.run(
        [BASH, str(SCRIPT)], cwd=project, env=env, capture_output=True, text=True, timeout=120
    )
    calls = log.read_text(encoding="utf-8") if log.exists() else ""
    written = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
    return proc.returncode, calls + proc.stdout, written


PYPROJECT = '[project]\nname = "probe"\nversion = "0.1.0"\n'


@pytest.mark.parametrize(
    ("files", "expect_in_calls"),
    [
        # uv-native: one committed lockfile, uv owns the venv.
        pytest.param({"uv.lock": "", "pyproject.toml": PYPROJECT}, "uv sync", id="uv-lock"),
        # pip-tools: fully-pinned compiled locks.
        pytest.param(
            {"requirements.txt": "ruff==0.15.0\n", "requirements-dev.txt": "uv==0.4.0\n"},
            "-r requirements.txt",
            id="requirements-locks",
        ),
        # Unlocked pyproject — the shape every generated project starts in.
        pytest.param({"pyproject.toml": PYPROJECT}, "-e .[dev]", id="unlocked-pyproject"),
    ],
)
def test_each_dependency_model_installs_and_still_exports_path(tmp_path, files, expect_in_calls):
    project, log, env_file = _make_project(tmp_path, files)
    rc, output, written = _run(project, log, env_file)
    assert rc == 0, output
    assert expect_in_calls in output, output
    # The regression assertion. Before the fix this file was empty for every model
    # except `requirements-locks`, because the shell had already exited.
    assert PATH_EXPORT in written, f"provisioning died before the PATH export\n{output}"


# The local branch's last act is `report_missing_toolchain`, which asks the manifest
# about the frontend tier. Its call is therefore the marker for "the script ran"; the
# `[session-start]` banner is not, because a project with no pre-commit config, a `.venv`
# already present and no frontend correctly prints nothing at all.
LOCAL_BRANCH_RAN = "frontend.enabled"


@pytest.mark.parametrize("value", ["1", "all", "stop,session-start", "SESSION-START"])
def test_the_kill_switch_stands_a_local_session_start_down(tmp_path, value):
    """`DEVKIT_HOOKS_OFF` is the one-line way to quieten the harness, and this script is
    part of what it quietens: on a local session it prints a banner, wires the
    commit-time gate and rebases the branch onto the default, none of which an operator
    who has switched the harness off is asking for."""
    project, log, env_file = _make_project(tmp_path, {"pyproject.toml": PYPROJECT})
    rc, output, _written = _run(
        project, log, env_file, remote=False, extra_env={"DEVKIT_HOOKS_OFF": value}
    )
    assert rc == 0, output
    assert LOCAL_BRANCH_RAN not in output, output


@pytest.mark.parametrize("value", ["stop", "0", ""])
def test_a_switch_that_does_not_name_this_script_leaves_it_running(tmp_path, value):
    """Selective is how the harness comes back, one hook at a time -- and `0` must never
    read as "off", per the asymmetry `hooks_off` documents."""
    project, log, env_file = _make_project(tmp_path, {"pyproject.toml": PYPROJECT})
    rc, output, _written = _run(
        project, log, env_file, remote=False, extra_env={"DEVKIT_HOOKS_OFF": value}
    )
    assert rc == 0, output
    assert LOCAL_BRANCH_RAN in output, output


def test_the_kill_switch_never_strips_provisioning_from_a_remote_sandbox(tmp_path):
    """Below the local branch this script is not a check at all -- it is the only thing
    that installs a toolchain into a Claude Code on the web sandbox, which starts empty
    every time. Honouring the switch there would answer "stop gating my sessions" with a
    cloud session that has no ruff, mypy or pytest, and that reads as a broken sandbox
    rather than as a setting someone chose."""
    project, log, env_file = _make_project(tmp_path, {"pyproject.toml": PYPROJECT})
    rc, output, written = _run(project, log, env_file, extra_env={"DEVKIT_HOOKS_OFF": "1"})
    assert rc == 0, output
    assert PATH_EXPORT in written, output


def test_project_with_no_dependency_file_warns_and_continues(tmp_path):
    """A project with no dependency file must warn and continue, not die."""
    project, log, env_file = _make_project(tmp_path, {"README.md": "# probe\n"})
    rc, output, written = _run(project, log, env_file)
    assert rc == 0, output
    assert "skipping Python install" in output
    assert PATH_EXPORT in written


def test_install_command_overrides_detection(tmp_path):
    """`[python] install_command` wins even when a lockfile is present."""
    marker = tmp_path / "override-ran"
    project, log, env_file = _make_project(
        tmp_path,
        {
            "uv.lock": "",
            "pyproject.toml": PYPROJECT,
            ".devkit.toml": f'[python]\ninstall_command = "touch {marker.as_posix()}"\n',
        },
    )
    rc, output, _ = _run(project, log, env_file)
    assert rc == 0, output
    assert marker.exists(), f"install_command did not run\n{output}"
    assert "uv sync" not in output, "detection ran despite an explicit install_command"


def test_uv_pin_is_honoured_when_a_dev_lock_provides_one(tmp_path):
    project, log, env_file = _make_project(
        tmp_path, {"requirements.txt": "", "requirements-dev.txt": "uv==0.4.18\n"}
    )
    rc, output, _ = _run(project, log, env_file)
    assert rc == 0, output
    assert "uv==0.4.18" in output, output


def test_the_pinned_interpreter_reaches_both_the_venv_and_the_install(tmp_path):
    """`python -m venv` can only ever copy the interpreter running it, so a project
    pinned to 3.12 got a venv on whatever the sandbox default is and was reported as
    provisioned -- the mismatch surfacing later as a resolution or type-check failure
    that reads as broken code. `worktree.venv_step` fixed this for boxes; this file,
    which is vendored into every project, never got the matching change. The pin has to
    reach the install too: `requires-python` in a lock is a floor, not a pin."""
    project, log, env_file = _make_project(
        tmp_path,
        {
            "uv.lock": "",
            "pyproject.toml": PYPROJECT,
            ".devkit.toml": '[python]\nversion = "3.12"\n',
        },
    )
    rc, output, written = _run(project, log, env_file)
    assert rc == 0, output
    assert "uv venv --python 3.12 .venv" in output, output
    assert "sync --all-extras --all-groups --python 3.12" in output, output
    assert PATH_EXPORT in written


def test_an_unpinned_project_keeps_the_plain_venv_and_the_plain_sync(tmp_path):
    """The pin is an override, not a requirement: a project that declares none must not
    start paying for an interpreter fetch it never asked for."""
    project, log, env_file = _make_project(tmp_path, {"uv.lock": "", "pyproject.toml": PYPROJECT})
    rc, output, _ = _run(project, log, env_file)
    assert rc == 0, output
    assert "uv venv" not in output, output
    assert "--python" not in output.split("sync --all-extras --all-groups")[-1].splitlines()[0]


def test_a_venv_already_on_the_pinned_version_is_not_rebuilt(tmp_path):
    """The rebuild is the expensive branch and it is idempotent-by-check, not by luck:
    every session start would otherwise throw away a correct venv and re-fetch it."""
    project, log, env_file = _make_project(
        tmp_path,
        {
            "uv.lock": "",
            "pyproject.toml": PYPROJECT,
            ".devkit.toml": '[python]\nversion = "3.12"\n',
        },
    )
    (project / ".venv" / "pyvenv.cfg").write_text("version = 3.12.7\n", encoding="utf-8")
    rc, output, _ = _run(project, log, env_file)
    assert rc == 0, output
    assert "uv venv" not in output, output
    assert "sync --all-extras --all-groups --python 3.12" in output, output


def test_missing_uv_pin_falls_back_to_unpinned_uv(tmp_path):
    """The `:?`-to-`:+` fix: no pin must mean plain `uv`, not a dead shell."""
    project, log, env_file = _make_project(tmp_path, {"pyproject.toml": PYPROJECT})
    rc, output, written = _run(project, log, env_file)
    assert rc == 0, output
    assert "pip install --quiet --disable-pip-version-check uv" in output.replace("\n", " "), output
    assert PATH_EXPORT in written


def test_frontend_install_is_skipped_without_a_frontend_tier(tmp_path):
    project, log, env_file = _make_project(tmp_path, {"pyproject.toml": PYPROJECT})
    rc, output, _ = _run(project, log, env_file)
    assert rc == 0, output
    assert "skipping npm install" in output
    assert "npm install" not in output.replace("skipping npm install", "")


def test_frontend_install_runs_when_the_manifest_declares_one(tmp_path):
    project, log, env_file = _make_project(
        tmp_path,
        {
            "pyproject.toml": PYPROJECT,
            ".devkit.toml": '[frontend]\nenabled = true\ndir = "web"\n',
            "web/package.json": "{}\n",
        },
    )
    rc, output, _ = _run(project, log, env_file)
    assert rc == 0, output
    assert "npm --prefix web" in output or "npm install --prefix web" in output, output


# --- the pre-commit gate ------------------------------------------------------
# `.git/hooks/` is not committed, so a fresh clone (and every fresh sandbox) has the config
# file and none of the hooks it describes — the gate silently does not exist until someone
# remembers to run `pre-commit install`. Detection is on the config file, so a project
# without one is unaffected.


def test_pre_commit_hook_is_installed_when_a_config_is_present(tmp_path):
    project, log, env_file = _make_project(
        tmp_path,
        {
            "pyproject.toml": PYPROJECT,
            ".pre-commit-config.yaml": "repos: []\n",
        },
    )
    rc, output, _ = _run(project, log, env_file)
    assert rc == 0, output
    assert "pre-commit install" in output, output
    # A cold sandbox builds the hook environments while it is provisioning anyway; the
    # local branch deliberately does not (see the test below).
    assert "--install-hooks" in output, output


def test_the_pre_commit_gate_is_wired_on_a_local_session_too(tmp_path):
    """The regression this whole block exists for.

    `.git/hooks/` is not committed on *any* machine, so a fresh clone has
    `.pre-commit-config.yaml` and none of the hooks it describes. The wiring used to sit
    below the local `exit 0`, so the only shape it ever reached was a remote sandbox --
    and a consuming project cloned onto a second machine committed ungated, with nothing
    to report it: the config file is committed and looks like the gate, and CI runs the
    same checks in a job, so both ends read healthy while the commit-time half is absent.
    """
    project, log, env_file = _make_project(
        tmp_path,
        {
            "pyproject.toml": PYPROJECT,
            ".pre-commit-config.yaml": "repos: []\n",
        },
    )
    rc, output, _ = _run(project, log, env_file, remote=False)
    assert rc == 0, output
    assert "pre-commit install" in output, output
    # The cheap form: it writes `.git/hooks/pre-commit` and nothing else. Building the
    # hook environments here would put a multi-minute install in front of a session that
    # already has its toolchain.
    assert "--install-hooks" not in output, output
    # And nothing else from the remote provisioning path ran.
    assert "-e .[dev]" not in output, output


def test_a_local_session_defers_to_the_global_devkit_dispatcher(tmp_path):
    """The dispatcher check has to hold on the branch that actually meets it.

    A machine with the global branch policy installed is the *local* case by definition,
    so this is where overwriting `core.hooksPath` -- disabling the policy for every
    repository on the machine -- would really happen.
    """
    project, log, env_file = _make_project(
        tmp_path,
        {
            "pyproject.toml": PYPROJECT,
            ".pre-commit-config.yaml": "repos: []\n",
        },
    )
    global_hooks = tmp_path / "global-hooks"
    global_hooks.mkdir()
    (global_hooks / "devkit_git_policy.py").write_text("# installed\n", encoding="utf-8")
    (tmp_path / "gitconfig").write_text(
        f"[core]\n\thooksPath = {global_hooks.as_posix()}\n",
        encoding="utf-8",
    )

    rc, output, _ = _run(project, log, env_file, remote=False)
    assert rc == 0, output
    assert "global Devkit dispatcher" in output, output
    assert "pre-commit install" not in output, output


def test_a_local_session_without_a_config_wires_nothing(tmp_path):
    project, log, env_file = _make_project(tmp_path, {"pyproject.toml": PYPROJECT})
    rc, output, _ = _run(project, log, env_file, remote=False)
    assert rc == 0, output
    assert "pre-commit install" not in output, output


def test_pre_commit_install_defers_to_the_global_devkit_dispatcher(tmp_path):
    project, log, env_file = _make_project(
        tmp_path,
        {
            "pyproject.toml": PYPROJECT,
            ".pre-commit-config.yaml": "repos: []\n",
        },
    )
    global_hooks = tmp_path / "global-hooks"
    global_hooks.mkdir()
    (global_hooks / "devkit_git_policy.py").write_text("# installed\n", encoding="utf-8")
    (tmp_path / "gitconfig").write_text(
        f"[core]\n\thooksPath = {global_hooks.as_posix()}\n",
        encoding="utf-8",
    )

    rc, output, _ = _run(project, log, env_file)
    assert rc == 0, output
    assert "global Devkit dispatcher" in output
    assert "pre-commit install" not in output


def test_pre_commit_install_is_skipped_without_a_config(tmp_path):
    project, log, env_file = _make_project(tmp_path, {"pyproject.toml": PYPROJECT})
    rc, output, _ = _run(project, log, env_file)
    assert rc == 0, output
    assert "pre-commit install" not in output, output


def test_pre_commit_absence_is_a_warning_not_a_failure(tmp_path):
    """Provisioning must not fail over a missing optional tool."""
    project, log, env_file = _make_project(
        tmp_path,
        {
            "pyproject.toml": PYPROJECT,
            ".pre-commit-config.yaml": "repos: []\n",
        },
    )
    (tmp_path / "bin" / "pre-commit").unlink()
    # Real PATH dropped: with the tests' own pre-commit installed (CI runs them under
    # `uv run`), deleting the stub would just fall through to the real binary.
    rc, output, _ = _run(project, log, env_file, inherit_path=False)
    assert rc == 0, output
    assert "skipping git hook wiring" in output, output
    # The PATH export is the line whose absence *is* the bug this file was written for:
    # provisioning must still reach the end of the script.
    assert PATH_EXPORT in (env_file.read_text(encoding="utf-8") if env_file.exists() else "")


# --- what a LOCAL session is missing ------------------------------------------
# The local branch used to assert in a comment that "local machines already have the venv
# + node_modules". True of a checkout set up months ago; false for a fresh clone, which is
# the one machine that has neither — and it then discovers that one failed ruff/pytest
# call at a time, each looking like a broken tool rather than an empty checkout.
#
# The hook reports and does not install. These tests pin BOTH halves, because the
# tempting fix (run the ladder under a `[ -d .venv ]` guard) puts a multi-minute
# synchronous install in front of every fresh-clone session and duplicates a provisioner
# `worktree.py` already owns.


def _unprovisioned(tmp_path: Path, files: dict[str, str]) -> tuple[Path, Path, Path]:
    """`_make_project` with the pre-created `.venv` removed: a fresh clone."""
    project, log, env_file = _make_project(tmp_path, files)
    shutil.rmtree(project / ".venv")
    return project, log, env_file


def test_a_fresh_clone_is_told_which_install_command_to_run(tmp_path):
    project, log, env_file = _unprovisioned(tmp_path, {"uv.lock": "", "pyproject.toml": PYPROJECT})
    rc, output, written = _run(project, log, env_file, remote=False)
    assert rc == 0, output
    assert "No .venv here" in output, output
    assert "uv sync --all-extras --all-groups" in output, output
    # Reported, not run: none of the remote provisioning path may have executed.
    assert "Installing Python toolchain" not in output, output
    assert written == "", "a local session must not rewrite PATH for a venv that does not exist"


def test_a_provisioned_checkout_is_told_nothing(tmp_path):
    """The no-op half. Every session on a working checkout pays this and must see nothing."""
    project, log, env_file = _make_project(tmp_path, {"uv.lock": "", "pyproject.toml": PYPROJECT})
    rc, output, _ = _run(project, log, env_file, remote=False)
    assert rc == 0, output
    assert "No .venv here" not in output, output


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        pytest.param({"uv.lock": "", "pyproject.toml": PYPROJECT}, "uv sync", id="uv-lock"),
        pytest.param(
            {"requirements.txt": "ruff==0.15.0\n", "requirements-dev.txt": "uv==0.4.0\n"},
            "-r requirements.txt -r requirements-dev.txt",
            id="requirements-locks",
        ),
        pytest.param({"pyproject.toml": PYPROJECT}, "-e '.[dev]'", id="unlocked-pyproject"),
    ],
)
def test_the_named_command_matches_the_project_dependency_model(tmp_path, files, expected):
    """Same detection ladder as the installer, in the same order — a command that does not
    fit the project is worse than no command, because it is followed."""
    project, log, env_file = _unprovisioned(tmp_path, files)
    rc, output, _ = _run(project, log, env_file, remote=False)
    assert rc == 0, output
    assert expected in output, output


def test_a_project_with_no_dependency_file_is_told_nothing(tmp_path):
    """There is nothing to install, so there is no command to name."""
    project, log, env_file = _unprovisioned(tmp_path, {"README.md": "# probe\n"})
    rc, output, _ = _run(project, log, env_file, remote=False)
    assert rc == 0, output
    assert "No .venv here" not in output, output


def test_the_manifest_install_command_wins_over_detection(tmp_path):
    project, log, env_file = _unprovisioned(
        tmp_path,
        {
            "uv.lock": "",
            "pyproject.toml": PYPROJECT,
            ".devkit.toml": '[python]\ninstall_command = "make bootstrap"\n',
        },
    )
    rc, output, _ = _run(project, log, env_file, remote=False)
    assert rc == 0, output
    assert "fix: make bootstrap" in output, output
    assert "uv sync" not in output, "detection ran despite an explicit install_command"


def test_a_frontend_tier_missing_node_modules_is_reported_too(tmp_path):
    """`node_modules` is the other half of the claim this replaced."""
    project, log, env_file = _make_project(
        tmp_path,
        {
            "pyproject.toml": PYPROJECT,
            ".devkit.toml": '[frontend]\nenabled = true\ndir = "web"\n',
            "web/package.json": "{}\n",
        },
    )
    rc, output, _ = _run(project, log, env_file, remote=False)
    assert rc == 0, output
    assert "No web/node_modules" in output, output
    assert "npm install --prefix web" in output, output
    # Named, not run — the stub would have logged an invocation.
    assert "npm install --prefix web --no-audit" not in output, output


def test_a_frontend_tier_with_node_modules_is_not_reported(tmp_path):
    project, log, env_file = _make_project(
        tmp_path,
        {
            "pyproject.toml": PYPROJECT,
            ".devkit.toml": '[frontend]\nenabled = true\ndir = "web"\n',
            "web/package.json": "{}\n",
            "web/node_modules/.keep": "",
        },
    )
    rc, output, _ = _run(project, log, env_file, remote=False)
    assert rc == 0, output
    assert "node_modules" not in output, output


# --- the local auto-rebase must not rewrite published history ------------------
# A rebase rewrites commits. On a branch with an open PR that marks every review comment
# outdated and forces the next push -- an outward-facing consequence no prompt asked for,
# applied silently at session start. The clean-tree guard never caught it: a pushed,
# reviewed branch is clean.

GIT = shutil.which("git")

needs_git = pytest.mark.skipif(GIT is None, reason="needs git")


def _git_env(tmp_path: Path) -> dict[str, str]:
    """Environment that pins git to `tmp_path` and to nothing it inherited.

    `GIT_DIR`/`GIT_WORK_TREE`/`GIT_INDEX_FILE` are cleared explicitly: if the process
    running the tests has any of them set -- and a git hook or a harness perfectly well
    might -- every git call below would silently operate on *that* repository instead of
    the throwaway one, and the script under test would report on the wrong branch.
    """
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
        "GIT_CONFIG_SYSTEM": str(tmp_path / "gitconfig-system"),
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@e",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@e",
    }
    for leaked in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CEILING_DIRECTORIES"):
        env.pop(leaked, None)
    return env


def _local_repo(tmp_path: Path, *, publish: bool) -> Path:
    """A real repo on a feature branch, optionally pushed to a local bare `origin`."""
    assert GIT is not None
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    env = _git_env(tmp_path)

    def git(*args: str, cwd: Path = work) -> None:
        subprocess.run([GIT, *args], cwd=cwd, env=env, check=True, capture_output=True)

    subprocess.run(
        [GIT, "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True
    )
    work.mkdir()
    git("init", "-b", "main")
    (work / "f.txt").write_text("one\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-m", "base")
    git("remote", "add", "origin", str(origin))
    git("push", "-u", "origin", "main")
    git("checkout", "-b", "feature/x")
    (work / "f.txt").write_text("two\n", encoding="utf-8")
    git("commit", "-am", "work")
    if publish:
        git("push", "-u", "origin", "feature/x")
    return work


def _gh_free_path(stub_bin: Path) -> str:
    """PATH for the "no `gh` installed at all" case, with no ambient directory on it.

    Dropping `/usr/bin` is not optional here: GitHub's runners ship `gh` at `/usr/bin/gh`,
    so a PATH that keeps `/usr/bin` in order to reach `git` reaches a real `gh` too, and
    the no-gh branch under test is never taken. That is exactly how these tests passed on
    a developer machine -- where gh lives outside `/usr/bin` -- and failed in CI.

    `git` is the only external the local block needs, so it comes in as an explicit shim
    to the real binary rather than by way of a directory that may hold anything else.
    """
    assert GIT is not None
    _write_exec(stub_bin / "git", f'#!/bin/sh\nexec "{Path(GIT).as_posix()}" "$@"\n')
    return str(stub_bin)


def _run_local(project: Path, tmp_path: Path, *, gh_state: str | None, gh_exit: int = 0) -> str:
    """Drive the script's LOCAL branch (CLAUDE_CODE_REMOTE unset). Returns its output.

    `gh_state` None means no `gh` on PATH at all -- the absent-tool fallback.
    `gh_exit` non-zero makes the stub fail every call, standing in for a `gh` that is
    installed but cannot answer: not authenticated, no GitHub remote, or offline.
    """
    assert BASH is not None and GIT is not None
    stub_bin = tmp_path / "localbin"
    log = tmp_path / "local-calls.log"
    # python3 is what session-sync.py would be invoked as; stubbing it proves whether the
    # rebase was attempted without actually rewriting anything.
    _write_exec(stub_bin / "python3", f'#!/bin/sh\necho "python3 $*" >> "{log}"\nexit 0\n')
    if gh_state is None:
        path = _gh_free_path(stub_bin)
    else:
        _write_exec(stub_bin / "gh", f'#!/bin/sh\necho "{gh_state}"\nexit {gh_exit}\n')
        # git's own directory, explicitly. `/usr/bin` and `/bin` alone leave `git`
        # resolving off whatever the ambient PATH happened to contain -- these tests
        # passed standalone and failed under the Stop hook for exactly that reason, and
        # a git the script cannot run makes the whole local block a silent no-op.
        path = os.pathsep.join([str(stub_bin), str(Path(GIT).parent), "/usr/bin", "/bin"])

    env = _git_env(tmp_path)
    env.pop("CLAUDE_CODE_REMOTE", None)
    env.update(
        # POSIX form: this value reaches bash's `cd`, and a Windows path with backslashes
        # is read there as escape sequences rather than separators.
        CLAUDE_PROJECT_DIR=project.as_posix(),
        PATH=path,
    )
    proc = subprocess.run(
        [BASH, str(SCRIPT)], cwd=project, env=env, capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    calls = log.read_text(encoding="utf-8") if log.exists() else ""
    output = proc.stdout + proc.stderr + calls
    # No output at all means the local block never ran -- `cd` failed, or git was not
    # reachable. Every assertion below would then be judging a script that did nothing,
    # and the "not in output" ones would pass vacuously.
    assert output.strip(), (
        "session-start.sh produced no output: the local sync block did not run. "
        "Check that CLAUDE_PROJECT_DIR is a path bash can cd into and that git is on PATH."
    )
    return output


@needs_git
def test_auto_sync_is_skipped_on_a_branch_with_an_open_pr(tmp_path):
    project = _local_repo(tmp_path, publish=True)
    output = _run_local(project, tmp_path, gh_state="OPEN")
    assert "Skipping auto-sync" in output, output
    assert "open PR" in output
    assert "session-sync.py" not in output, "the rebase must not even be attempted"


@needs_git
def test_auto_sync_still_runs_when_the_pr_is_merged_or_absent(tmp_path):
    """The feature has to keep working -- this guard is a narrowing, not a disabling."""
    project = _local_repo(tmp_path, publish=False)
    output = _run_local(project, tmp_path, gh_state="MERGED")
    assert "Skipping auto-sync" not in output, output
    assert "session-sync.py" in output, output


@needs_git
def test_without_gh_a_published_branch_is_left_alone(tmp_path):
    """No `gh` means the PR question cannot be asked, so fall back to the safe proxy.

    A branch that exists on origin is one where a rewrite means a force-push. Guessing
    "probably no PR" is the one answer with a blast radius.
    """
    project = _local_repo(tmp_path, publish=True)
    output = _run_local(project, tmp_path, gh_state=None)
    assert "Skipping auto-sync" in output, output
    assert "published branch" in output


@needs_git
def test_without_gh_an_unpublished_branch_still_syncs(tmp_path):
    project = _local_repo(tmp_path, publish=False)
    output = _run_local(project, tmp_path, gh_state=None)
    assert "Skipping auto-sync" not in output, output
    assert "session-sync.py" in output, output


@needs_git
def test_a_gh_that_cannot_answer_is_not_read_as_there_being_no_pr(tmp_path):
    """Installed is not the same as able to answer, and the difference is a force-push.

    `gh pr view` exits non-zero for "this branch has no PR" *and* for "gh cannot reach
    GitHub" -- not authenticated, no GitHub remote, offline. Treating the second as the
    first rebases published history on any machine where gh was installed but never
    logged in, so an unusable gh has to fall back to the same proxy as a missing one.
    """
    project = _local_repo(tmp_path, publish=True)
    output = _run_local(project, tmp_path, gh_state="gh: not logged in", gh_exit=1)
    assert "Skipping auto-sync" in output, output
    assert "published branch" in output
    assert "session-sync.py" not in output, "the rebase must not even be attempted"


@needs_git
def test_a_gh_that_cannot_answer_still_syncs_an_unpublished_branch(tmp_path):
    """Falling back is a narrowing: nothing is published, so no rewrite can be forced."""
    project = _local_repo(tmp_path, publish=False)
    output = _run_local(project, tmp_path, gh_state="gh: not logged in", gh_exit=1)
    assert "Skipping auto-sync" not in output, output
    assert "session-sync.py" in output, output


@needs_git
def test_a_working_gh_overrides_the_published_branch_proxy(tmp_path):
    """When gh *can* answer, its answer wins -- that is the point of preferring it.

    A pushed branch with no open PR is the parallel-worktree case this auto-sync exists
    for, so the proxy must not veto it once gh has confirmed there is no PR to disturb.
    """
    project = _local_repo(tmp_path, publish=True)
    output = _run_local(project, tmp_path, gh_state="", gh_exit=0)
    assert "Skipping auto-sync" not in output, output
    assert "session-sync.py" in output, output


def test_script_contains_no_die_on_unset_expansions():
    """Guards the bug class, not just the one instance.

    `${var:?msg}` aborts a non-interactive shell and cannot be caught by `||`, so
    it must never appear in a hook that has to degrade gracefully across projects.
    """
    code = [
        line.strip()
        for line in SCRIPT.read_text(encoding="utf-8").splitlines()
        # Comments are exempt: the script explains this very bug in prose.
        if not line.lstrip().startswith("#")
    ]
    offenders = [line for line in code if ":?" in line and "${" in line]
    assert not offenders, f"`${{var:?...}}` exits the shell; use `:-` or `:+`: {offenders}"
