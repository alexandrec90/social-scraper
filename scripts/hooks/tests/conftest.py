"""Shared loader for hook scripts whose filenames contain hyphens.

Hook scripts are not importable as normal modules (hyphenated names, and one
lives outside this tree), so tests load them by path. Each script guards its
side effects behind `if __name__ == '__main__'`, so importing only binds the
pure functions.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _ledger_off_the_machine(tmp_path_factory, monkeypatch):
    """Point `$DEVKIT_DIR` at a scratch directory for every test in this tree.

    `harness_events.record` resolves its destination from `$DEVKIT_DIR`, so any hook a
    test drives end to end appends to **this machine's real** harness-events ledger --
    the one `harness_triage.py` reads as a backlog of things an agent hit. Every test
    that knew it wrote there already monkeypatched the variable itself; the defect is
    that knowing was required. One that did not, `test_codex_translation.py`'s
    end-to-end refusal case, filed 18 `codex-translation-gap` items against a project
    called `test_a_lost_decision_is_refuse0` -- a pytest `tmp_path` basename -- and they
    sat on the open list as a recurring harness failure nobody could reproduce, because
    the only thing reproducing them was the suite.

    Autouse rather than a fixture to remember, for the reason the ledger exists at all:
    a diagnostic that a test can quietly poison is a diagnostic nobody can trust. Tests
    that set or delete the variable themselves still win -- `monkeypatch` applies in
    order and theirs runs second -- so nothing that asserts on the unset behaviour
    changes, and `record(root=...)`, which names its destination outright, never
    consulted the environment in the first place.
    """
    monkeypatch.setenv("DEVKIT_DIR", str(tmp_path_factory.mktemp("ledger")))


# Put scripts/ on the path so modules under it (e.g. `diagnostics`) import the
# same way they do at runtime, where the script's own dir is sys.path[0].
sys.path.insert(0, str(REPO_ROOT / "scripts"))


def load_module(relpath: str):
    """Load a hook script (path relative to repo root) as a module object."""
    path = REPO_ROOT / relpath
    mod_name = path.stem.replace("-", "_")
    # Cache like a real import: re-registering an already-loaded name would
    # replace the module object other modules bound at their import time and
    # break cross-module identity (e.g. prune.restart_engine is
    # docker_win.restart_engine).
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        # A missing/unreadable script, or one importlib cannot infer a loader for.
        # Raising here names the path; falling through hands module_from_spec a None
        # and the AttributeError points at importlib internals instead.
        raise ImportError(f"cannot load {relpath} from {path}")
    module = importlib.util.module_from_spec(spec)
    # Register before exec (importlib docs): stdlib introspection such as
    # dataclasses' string-annotation resolution looks the module up by name.
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[mod_name]
        raise
    return module
