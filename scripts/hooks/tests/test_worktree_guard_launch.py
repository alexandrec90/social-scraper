"""`worktree-guard-launch.py` — the shim that gives a consuming project the guard.

Project-agnostic, like everything in this tree: no path here names devkit's own layout
beyond the one file the shim looks for.

**Every test about failure asserts exit 0.** A PreToolUse hook exiting 2 is a block, so a
shim that failed loudly would refuse every tool call in the project — turning "no guard
here" into "this repo cannot be worked in". That inversion is the only way this file can
do real damage, so it is what most of these cover.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import load_module

launch = load_module("scripts/hooks/worktree-guard-launch.py")


@pytest.fixture
def base_env():
    """The real environment with `$DEVKIT_DIR` removed.

    Inherited rather than empty because a bare `{}` gives the child no `SYSTEMROOT`, and
    a Python that cannot start is a spawn failure this shim reports as an allow -- so
    every test would pass for the wrong reason.
    """
    env = dict(os.environ)
    env.pop("DEVKIT_DIR", None)
    return env


PAYLOAD = '{"tool_name": "Edit", "tool_input": {"file_path": "a.py"}}'


def make_devkit(root: Path, body: str = "import sys; sys.exit(0)") -> Path:
    """A directory that looks like a devkit checkout, holding a stand-in guard."""
    guard = root / "scripts" / "worktree-guard.py"
    guard.parent.mkdir(parents=True, exist_ok=True)
    guard.write_text(body, encoding="utf-8")
    return root


def make_project(root: Path) -> Path:
    """A consuming project laid out as the shim expects to sit inside one."""
    (root / "scripts" / "hooks").mkdir(parents=True)
    return root


# --- finding devkit ---------------------------------------------------------


def test_the_branch_tier_switch_is_read_and_defaults_to_running(monkeypatch):
    """`DEVKIT_HOOKS_OFF=branch-tier` is the one exit-0 path here that is a decision
    rather than a fallback, and the default has to be "the guard runs" for a variable
    nobody has ever heard of."""
    assert launch.branch_tier_off() is False
    monkeypatch.setenv("DEVKIT_HOOKS_OFF", "branch-tier")
    assert launch.branch_tier_off() is True
    monkeypatch.setenv("DEVKIT_HOOKS_OFF", "stop")
    assert launch.branch_tier_off() is False


def test_the_switch_reads_as_running_when_the_sibling_is_not_vendored(monkeypatch):
    """A consumer whose pull went sideways must not have every tool call routed through
    an ImportError -- and "cannot decide" has to mean "let the guard run", not "skip it"."""
    monkeypatch.setitem(sys.modules, "harness_config", None)
    monkeypatch.setenv("DEVKIT_HOOKS_OFF", "branch-tier")
    assert launch.branch_tier_off() is False


def test_the_environment_variable_is_consulted_first(tmp_path):
    """`$DEVKIT_DIR` is the name `sync-devkit.py` and `report-harness-defect.py` already
    use, so a machine configured for those is configured for this."""
    devkit = make_devkit(tmp_path / "elsewhere")
    project = make_project(tmp_path / "proj")
    assert launch.devkit_root(project, {"DEVKIT_DIR": str(devkit)}) == devkit.resolve()


def test_a_sibling_devkit_needs_no_configuration(tmp_path):
    """The workspace layout: every checkout sits beside `devkit/`. This is the case that
    has to work with nothing set, because it is the common one."""
    make_devkit(tmp_path / "devkit")
    project = make_project(tmp_path / "proj")
    assert launch.devkit_root(project, {}) == (tmp_path / "devkit").resolve()


def test_a_directory_without_the_guard_is_not_devkit(tmp_path):
    """Resolving a path that exists but holds no guard would exit 0 forever while looking
    configured — indistinguishable from a machine with no devkit at all."""
    (tmp_path / "not-devkit").mkdir()
    project = make_project(tmp_path / "proj")
    assert launch.devkit_root(project, {"DEVKIT_DIR": str(tmp_path / "not-devkit")}) is None


def test_a_machine_with_no_devkit_anywhere_resolves_to_none(tmp_path):
    assert launch.devkit_root(make_project(tmp_path / "proj"), {}) is None


def test_a_nonsense_path_in_the_variable_does_not_raise(tmp_path):
    """This runs on every tool call. An exception here is a non-zero exit, and a non-zero
    exit from a PreToolUse hook is a blocked call."""
    project = make_project(tmp_path / "proj")
    assert launch.devkit_root(project, {"DEVKIT_DIR": "\0not a path"}) is None


# --- the .devkit.toml fallback ----------------------------------------------


def test_the_manifest_can_name_the_directory(tmp_path):
    project = make_project(tmp_path / "proj")
    (project / ".devkit.toml").write_text('[devkit]\ndir = "C:/ws/devkit"\n', encoding="utf-8")
    assert launch.configured_dir(project) == "C:/ws/devkit"


@pytest.mark.parametrize(
    "text",
    [
        '[other]\ndir = "C:/ws/devkit"\n',  # the key, but in the wrong section
        "[devkit]\nname = 'x'\n",  # the section, but no key
        "",
    ],
)
def test_a_manifest_without_the_key_reports_nothing(tmp_path, text):
    project = make_project(tmp_path / "proj")
    (project / ".devkit.toml").write_text(text, encoding="utf-8")
    assert launch.configured_dir(project) == ""


def test_a_missing_manifest_reports_nothing(tmp_path):
    assert launch.configured_dir(make_project(tmp_path / "proj")) == ""


# --- running it end to end --------------------------------------------------


def run_shim(project: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(project / "scripts" / "hooks" / "worktree-guard-launch.py")],
        input=PAYLOAD,
        capture_output=True,
        text=True,
        env=env,
    )


def install(project: Path) -> Path:
    """Copy the real shim into a project fixture, the way `sync-devkit.py` would."""
    source = Path(launch.__file__)
    target = project / "scripts" / "hooks" / "worktree-guard-launch.py"
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return target


def test_with_no_devkit_it_allows_silently(tmp_path, base_env):
    """The fresh-clone and CI case, and the one that must never become a block."""
    project = make_project(tmp_path / "proj")
    install(project)
    result = run_shim(project, base_env)
    assert result.returncode == 0
    assert result.stdout == ""


def test_a_guard_that_blocks_has_its_verdict_passed_through(tmp_path, base_env):
    """Exit code and stderr both, because the block message is the whole of what the
    agent gets — a shim that swallowed it would block with no explanation."""
    devkit = make_devkit(
        tmp_path / "devkit",
        "import sys; sys.stderr.write('routed to a box'); sys.exit(2)",
    )
    project = make_project(tmp_path / "proj")
    install(project)
    result = run_shim(project, {**base_env, "DEVKIT_DIR": str(devkit)})
    assert result.returncode == 2
    assert "routed to a box" in result.stderr


def test_the_payload_reaches_the_guard_unchanged(tmp_path, base_env):
    """The guard decides on the hook payload. A shim that dropped stdin would make every
    call look like one with no tool input, which the guard allows."""
    devkit = make_devkit(
        tmp_path / "devkit",
        "import sys; sys.stdout.write(sys.stdin.read())",
    )
    project = make_project(tmp_path / "proj")
    install(project)
    result = run_shim(project, {**base_env, "DEVKIT_DIR": str(devkit)})
    assert PAYLOAD in result.stdout


def test_a_guard_that_crashes_still_allows(tmp_path, base_env):
    """A guard raising on import must not take the session with it. Its own non-zero exit
    is forwarded, but a spawn that cannot happen at all is an allow."""
    project = make_project(tmp_path / "proj")
    install(project)
    result = run_shim(project, {**base_env, "DEVKIT_DIR": str(tmp_path / "gone")})
    assert result.returncode == 0


def test_main_allows_when_there_is_no_devkit_to_reach(tmp_path, monkeypatch):
    """`main` named directly, not only through the subprocess tests above.

    The untested-symbols ratchet asks for it, and it is right to: every end-to-end test
    here spawns a copy of this file, so a `main` that had been renamed or gutted would
    still be exercised only through the copy's own definition.
    """
    monkeypatch.setattr(launch.sys, "argv", ["worktree-guard-launch.py"])
    monkeypatch.setattr(launch.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(launch, "devkit_root", lambda *_a, **_kw: None)
    assert launch.main([]) == launch.EXIT_ALLOW
