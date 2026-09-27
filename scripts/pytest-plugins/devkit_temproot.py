"""Give every pytest run a temp root of its own, so no other run can fail it at its exit.

Left to itself pytest keeps every run's `tmp_path` under one directory per *user* --
`%TEMP%/pytest-of-<user>` -- repoints a `pytest-current` link there at the start of each
run, and resolves every link in that directory at the end. On a machine running several
sessions at once, one run replacing that link while another resolves it, or an orphaned
interpreter from a cut-off tool call still holding it, leaves it delete-pending: nothing
can stat it, and every run by that user, in any checkout, raises
`PermissionError: [WinError 5]` in its own teardown and exits 1 after a green suite
(97d20f01, 237ed2b1).

`run-tests.py` and the push gate already hand pytest a private `--basetemp`. This plugin
is the same for the run nothing wraps -- an agent's `python -m pytest tests/test_x.py`:
it points `PYTEST_DEBUG_TEMPROOT`, pytest's own switch, at a fresh directory under
`<tempdir>/pytest-runs/` and removes it once pytest is done with it. A root per run, not
per checkout, because two runs in one tree share a link just as two trees do.

It stands aside when the run already has a root -- `--basetemp`, the variable set by
whoever launched it, or an xdist worker, which the controller hands a base temp under
its own. A run killed before its cleanup leaves its directory behind; `reclaim.py`
sweeps `pytest-runs` with the other scratch trees.

Vendored, and loaded from `pyproject.toml` rather than as a `conftest.py`, which a
project owns: `pythonpath` names this directory and `addopts` carries
`-p devkit_temproot`. `scripts/hooks/tests/test_repo_contract.py` holds every project
that configures pytest there to both. Stdlib only, like the rest of the vendored tier:
the hook takes pytest's `config` without importing pytest.

Tested in `scripts/hooks/tests/test_devkit_temproot.py`.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

ENV = "PYTEST_DEBUG_TEMPROOT"
PARENT = "pytest-runs"


def wants_root(environ: Mapping[str, str], basetemp: object, worker: bool) -> bool:
    """Whether this run needs a root of its own: nothing else has given it one."""
    return not environ.get(ENV) and not basetemp and not worker


def run_root(base: str | None = None) -> Path:
    """A fresh, empty directory for one run, under `<base>/pytest-runs/`."""
    parent = Path(base or tempfile.gettempdir()) / PARENT
    parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="run-", dir=parent))


def pytest_configure(config: Any) -> None:
    """Point pytest at this run's own root before anything asks for a temp directory.

    pytest computes its base temp directory lazily -- at the first `tmp_path`, or when
    xdist starts its workers -- both after every plugin is configured.
    """
    basetemp = config.getoption("basetemp", None)
    if not wants_root(os.environ, basetemp, hasattr(config, "workerinput")):
        return
    root = run_root()
    os.environ[ENV] = str(root)

    def release() -> None:
        os.environ.pop(ENV, None)
        shutil.rmtree(root, ignore_errors=True)

    config.add_cleanup(release)
