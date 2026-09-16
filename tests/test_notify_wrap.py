"""`scripts/notify-wrap.py` -- the task wrapper's exit-code contract.

The wrapper exists so a VS Code task's result is readable after the terminal has
scrolled: it runs the wrapped command, then raises a toast saying how it went. That
makes its exit code the only thing downstream can act on, and the only thing worth
pinning here -- a wrapper that swallowed a failure would make every red task look green.

`notify` is stubbed throughout. The real one raises a Windows toast per call, which a
test suite has no business doing, and the assertion is the return value either way.

Written when devkit v0.11.18's untested-symbol ratchet flagged `main` as public and
uncovered. It is genuinely uncovered here: `notify-wrap.py` arrives from devkit's
`templates/core/scripts/`, which is a ONE-SHOT copy that ships the script without a
test, so every generated project starts with this gap.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load(rel: str):
    """Load a hyphenated script by path. Registered in `sys.modules` before exec so a
    dataclass inside it can resolve its own annotations."""
    path = REPO_ROOT / rel
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    # `spec_from_file_location` is typed `ModuleSpec | None` and a loaderless spec is
    # possible for a path importlib cannot handle. Asserting names the failure here
    # rather than as an AttributeError three lines down.
    assert spec is not None and spec.loader is not None, f"cannot load {rel}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("code", [0, 1, 2])
def test_the_wrapped_commands_exit_code_is_the_wrappers(monkeypatch, code):
    """0 clean, 1 needs action, 2 blocked -- the full range a checked task returns."""
    notify_wrap = _load("scripts/notify-wrap.py")
    monkeypatch.setattr(notify_wrap, "notify", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        sys,
        "argv",
        ["notify-wrap.py", "T", "--", sys.executable, "-c", f"raise SystemExit({code})"],
    )
    assert notify_wrap.main() == code


def test_a_command_that_cannot_start_is_not_reported_as_success(monkeypatch):
    """The failure mode that matters most: a wrapper that reports 0 when it never ran
    anything turns a broken task into a silent one."""
    notify_wrap = _load("scripts/notify-wrap.py")
    monkeypatch.setattr(notify_wrap, "notify", lambda *args, **kwargs: None)
    monkeypatch.setattr(sys, "argv", ["notify-wrap.py", "T", "--", "a-command-that-does-not-exist"])

    def boom(*args, **kwargs):
        raise FileNotFoundError("no such command")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(notify_wrap.subprocess, "run", boom)

    try:
        result = notify_wrap.main()
    except FileNotFoundError:
        return  # surfacing it is a valid answer; silently succeeding is not
    assert result != 0
