"""Tests for `scripts/pytest-plugins/devkit_temproot.py`: a temp root per pytest run.

The plugin is loaded into the run executing these tests too, so each one drives the
hook against a stand-in `config` and a patched environment rather than the real run's.
"""

import os
import subprocess
import sys
from pathlib import Path

from conftest import REPO_ROOT, load_module

plugin = load_module("scripts/pytest-plugins/devkit_temproot.py")


class Config:
    """The two things the hook reads off pytest's `config`, and the one it calls."""

    def __init__(self, basetemp=None, worker=False):
        self._basetemp = basetemp
        self.cleanups = []
        if worker:
            self.workerinput = {}

    def getoption(self, name, default=None):
        return self._basetemp if name == "basetemp" else default

    def add_cleanup(self, func):
        self.cleanups.append(func)


def test_only_a_run_with_no_root_of_its_own_gets_one():
    assert plugin.wants_root({}, None, worker=False)
    assert not plugin.wants_root({plugin.ENV: "x"}, None, worker=False)
    assert not plugin.wants_root({}, "some/dir", worker=False)
    assert not plugin.wants_root({}, None, worker=True)
    assert plugin.wants_root({plugin.ENV: ""}, "", worker=False)


def test_each_run_root_is_fresh_and_under_the_shared_parent(tmp_path):
    first, second = plugin.run_root(str(tmp_path)), plugin.run_root(str(tmp_path))
    assert first != second
    assert first.parent == second.parent == tmp_path / plugin.PARENT
    assert list(first.iterdir()) == []


def test_configure_points_pytest_at_a_root_it_removes_afterwards(tmp_path, monkeypatch):
    monkeypatch.delenv(plugin.ENV, raising=False)
    monkeypatch.setattr(plugin.tempfile, "gettempdir", lambda: str(tmp_path))
    config = Config()
    plugin.pytest_configure(config)
    root = Path(os.environ[plugin.ENV])
    assert root.is_dir() and root.parent == tmp_path / plugin.PARENT
    (root / "pytest-of-x").mkdir()
    [release] = config.cleanups
    release()
    assert plugin.ENV not in os.environ
    assert not root.exists()


def test_configure_leaves_a_run_that_already_has_a_root_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setenv(plugin.ENV, "chosen")
    for config in (Config(), Config(basetemp="b"), Config(worker=True)):
        plugin.pytest_configure(config)
        assert config.cleanups == [] and os.environ[plugin.ENV] == "chosen"
    assert not (tmp_path / plugin.PARENT).exists()


def test_a_run_loading_it_by_p_keeps_its_tmp_path_out_of_the_shared_root(tmp_path):
    """The whole path, as a project's `addopts` spells it: `-p` finds the module on the
    ini `pythonpath`, `tmp_path` lands under the run's own root, and the run never makes
    the shared `pytest-of-<user>` whose teardown walk is what crashed (97d20f01)."""
    shared = tmp_path / "shared"
    shared.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        f'pythonpath = ["{(REPO_ROOT / "scripts" / "pytest-plugins").as_posix()}"]\n'
        'addopts = "-p devkit_temproot"\n',
        encoding="utf-8",
    )
    (project / "test_where.py").write_text(
        "import os\n"
        "def test_where(tmp_path):\n"
        "    root = os.environ['PYTEST_DEBUG_TEMPROOT']\n"
        "    assert str(tmp_path).startswith(root), (tmp_path, root)\n"
        "    assert 'pytest-runs' in root\n",
        encoding="utf-8",
    )
    env = {k: v for k, v in os.environ.items() if k not in (plugin.ENV, "PYTEST_ADDOPTS")}
    env.update({"TMP": str(shared), "TEMP": str(shared), "TMPDIR": str(shared)})
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(project)],
        cwd=project,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert [p.name for p in shared.iterdir()] == [plugin.PARENT], "the shared root is untouched"
    assert list((shared / plugin.PARENT).iterdir()) == [], "the run removed its own root"
