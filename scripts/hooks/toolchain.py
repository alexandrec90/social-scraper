#!/usr/bin/env python3
"""What a checkout is missing before its gates can run, and the command that installs it.

A linked worktree -- `claude --worktree`, `git worktree add`, a devkit box before it is
provisioned -- checks out **tracked files only**. So it has no `.venv` and no
`node_modules`, and everything that needs one fails in the order it happens to be
reached: `ruff` at the first edit, the frontend linters at the first lint, and the
commit-time pre-commit gate at `git commit`, where a `language: system` hook resolves
its entry point against a `PATH` that has no venv on it. That last one is how a session
in a fresh carameli worktree got `Executable 'detect-secrets-hook' not found` from the
one hook its config marks as the one that must stay local -- and answered by installing
that single tool by hand and prepending its `Scripts/` to `PATH` for the commit. It
passed, and left every other check in the worktree to fail the same way.

Two callers used to walk this detection ladder separately -- `session-start.sh` in shell,
for its start-of-session report, and `worktree.py provision` in Python, for boxes -- and
`session-start.sh` said in a comment that a third copy was how the two would drift. This
module is the one copy the vendored tier reads: the SessionStart report calls the CLI
below, and `ship.py --preflight` calls `missing_toolchain` so the state is named at the
top of `/ship` rather than discovered at its commit. `worktree.py provision` keeps its
own ladder, which does more (a venv on the pinned interpreter that `uv` fetches, an
`npm ci` into a leased box); the *commands* named here are the ones it would run.

**Reports, never installs.** SessionStart is synchronous and a cold install is minutes;
`ship.py` is a mechanical check. The command is printed for whoever is in a position
to spend the time.

Detection, not configuration: the manifest's `[python] install_command` wins, then the
lockfile on disk decides, in the order `session-start.sh` and `worktree.provision_steps`
already use -- a project with both `uv.lock` and a `pyproject.toml` must not be installed
twice, and the lockfile is the pinned one. `[python] version` reaches the venv step and
`uv sync`, because `requires-python` in a lock is a floor, not a pin.

Stdlib only: this runs where nothing is installed yet, by construction.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_config

# The Python toolchain, in the order the ladder reads them.
PYTHON_MARKERS = ("uv.lock", "requirements-dev.txt", "pyproject.toml")


@dataclass(frozen=True)
class Gap:
    """One thing the checkout lacks: what is unavailable because of it, and the fix."""

    what: str
    fix: str

    @property
    def line(self) -> str:
        """The one-line report both callers print, under their own prefix."""
        return f"{self.what} (fix: {self.fix})"


def venv_command(python_version: str = "") -> str:
    """How to create `.venv` -- on the pinned interpreter when the manifest names one.

    `python -m venv` can only copy the interpreter running it, which is the workstation
    default rather than the version the project pins; `uv venv --python` picks the pin
    and fetches it when the machine has none.
    """
    if python_version:
        return f"uv venv --python {python_version} .venv"
    return "python -m venv .venv"


def python_fix(root: Path, install_command: str = "", python_version: str = "") -> str:
    """The command that provisions the Python toolchain here, or "" when nothing says how."""
    if install_command:
        return install_command
    if (root / "uv.lock").is_file():
        pin = f" --python {python_version}" if python_version else ""
        return f"uv sync --all-extras --all-groups{pin}"
    if (root / "requirements-dev.txt").is_file():
        locks = "-r requirements-dev.txt"
        if (root / "requirements.txt").is_file():
            locks = f"-r requirements.txt {locks}"
        return f"{venv_command(python_version)} && uv pip install {locks}"
    if (root / "pyproject.toml").is_file():
        return f"{venv_command(python_version)} && uv pip install -e '.[dev]'"
    return ""


def frontend_fix(root: Path, frontend_dir: str) -> str:
    """`npm ci` when the lock is committed, else `npm install`.

    Not about speed: `npm install` rewrites `package-lock.json`, so running it in a
    worktree leaves a tracked file modified before anything has been edited -- which
    `ship.py` then refuses as a dirty tree. `ci` installs the lock exactly.
    """
    verb = "ci" if (root / frontend_dir / "package-lock.json").is_file() else "install"
    return f"npm {verb} --prefix {frontend_dir}"


def missing_toolchain(root: Path, cfg: harness_config.Config | None = None) -> tuple[Gap, ...]:
    """Every gap in this checkout's toolchain. Empty when it is provisioned.

    A project with no dependency file at all has nothing to install and is told nothing;
    a frontend tier is judged only when the manifest switches it on and the directory
    exists, since a manifest half-filled for a tier the repo does not have is the
    `devkit-manifest` hook's finding rather than this one.
    """
    config = harness_config.load(root) if cfg is None else cfg
    gaps: list[Gap] = []
    if not (root / ".venv").is_dir():
        fix = python_fix(root, config.python.install_command, config.python.version)
        if fix:
            gaps.append(Gap("No .venv here -- ruff/mypy/pytest are unavailable", fix))
    frontend = config.frontend
    if frontend.enabled and (root / frontend.dir).is_dir():
        if not (root / frontend.dir / "node_modules").is_dir():
            gaps.append(
                Gap(
                    f"No {frontend.dir}/node_modules -- the frontend linters are unavailable",
                    frontend_fix(root, frontend.dir),
                )
            )
    return tuple(gaps)


def main(argv: list[str] | None = None) -> int:
    """One gap per stdout line, for `session-start.sh`. Always exits 0.

    A hook must not die over a report, and the shell caller has no handler for a
    failure -- so an unreadable root prints nothing rather than a traceback. The root is
    the cwd, like `harness_config.py`'s own CLI, unless `--root <dir>` says otherwise.
    """
    args = sys.argv[1:] if argv is None else argv
    root = Path(args[1]) if args[:1] == ["--root"] and len(args) > 1 else Path.cwd()
    try:
        for gap in missing_toolchain(root):
            print(gap.line)
    except OSError as exc:
        # The only thing left that can raise: `harness_config.load` degrades to defaults
        # on its own, and the probes are `is_dir`/`is_file`, which propagate only the
        # OSErrors they do not classify (a permission refusal, an unreachable share).
        print(f"toolchain: could not read {root} ({type(exc).__name__})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
