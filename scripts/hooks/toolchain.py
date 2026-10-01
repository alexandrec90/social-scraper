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

**The reports never install.** SessionStart is synchronous and a cold install is
minutes; `ship.py` is a mechanical check. The command is printed for whoever is in a
position to spend the time.

**`rerun_in_venv` is the one caller that does**, because its caller is the one about
to fail without it: a test or lint runner started by an interpreter that lacks pytest
or ruff. A `claude --worktree` tree arrives with no `.venv`, and nothing can provision
it at creation -- Claude Code runs its git with hooks off, and no agent hook is wired --
so every session in one used to hit `No module named pytest`, provision by hand, and
file the same friction. Building the tree's own `.venv` at that first run, instead of
borrowing the checkout's, is deliberate: a project installed editable points its venv
at the checkout's `src/`, so a borrowed one would test the checkout's code, not the
branch's.

Detection, not configuration: the manifest's `[python] install_command` wins, then the
lockfile on disk decides, in the order `session-start.sh` and `worktree.provision_steps`
already use -- a project with both `uv.lock` and a `pyproject.toml` must not be installed
twice, and the lockfile is the pinned one. `[python] version` reaches the venv step and
`uv sync`, because `requires-python` in a lock is a floor, not a pin.

Stdlib only: this runs where nothing is installed yet, by construction.
"""

from __future__ import annotations

import importlib.util
import os
import shlex
import subprocess
import sys
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_config
import worktree_tiers

# The Python toolchain, in the order the ladder reads them.
PYTHON_MARKERS = ("uv.lock", "requirements-dev.txt", "pyproject.toml")

# Set in the environment of a runner re-run under the tree's `.venv`: a venv that still
# lacks the module must fail there, not send the run round again.
RERUN_ENV = "DEVKIT_TOOLCHAIN_RERUN"

# Lines of a failed install's output shown with the command, enough for uv's resolver
# error and not the whole download log above it.
INSTALL_TAIL_LINES = 20

# A manifest `install_command` holding any of these needs a shell, which provisioning
# does not start; `worktree_env.SHELL_SYNTAX` draws the same line for the git hook.
SHELL_SYNTAX = frozenset("&|;<>$`'\"*?()\\%^\n")
# Spellings of "the interpreter" in an install step, run as this one instead.
PYTHON_NAMES = frozenset({"python", "python3"})

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True)
class Gap:
    """One thing the checkout lacks: what is unavailable because of it, and the fix."""

    what: str
    fix: str

    @property
    def line(self) -> str:
        """The one-line report both callers print, under their own prefix."""
        return f"{self.what} (fix: {self.fix})"


def venv_argv(python_version: str = "") -> tuple[str, ...]:
    """How to create `.venv` -- on the pinned interpreter when the manifest names one.

    `python -m venv` can only copy the interpreter running it, which is the workstation
    default rather than the version the project pins; `uv venv --python` picks the pin
    and fetches it when the machine has none.
    """
    if python_version:
        return ("uv", "venv", "--python", python_version, ".venv")
    return ("python", "-m", "venv", ".venv")


def venv_command(python_version: str = "") -> str:
    """`venv_argv` as the line a person types."""
    return shlex.join(venv_argv(python_version))


def python_steps(root: Path, python_version: str = "") -> tuple[tuple[str, ...], ...]:
    """The ladder's install, one argv per step; () when no dependency file says how.

    Argv rather than a shell line because `provision_python` runs them, and a shell in
    between differs by platform: `cmd.exe` hands a single-quoted `'.[dev]'` to uv as
    part of the requirement.
    """
    if (root / "uv.lock").is_file():
        pin = ("--python", python_version) if python_version else ()
        return (("uv", "sync", "--all-extras", "--all-groups", *pin),)
    if (root / "requirements-dev.txt").is_file():
        locks = ["-r", "requirements-dev.txt"]
        if (root / "requirements.txt").is_file():
            locks = ["-r", "requirements.txt", *locks]
        return (venv_argv(python_version), ("uv", "pip", "install", *locks))
    if (root / "pyproject.toml").is_file():
        return (venv_argv(python_version), ("uv", "pip", "install", "-e", ".[dev]"))
    return ()


def python_fix(root: Path, install_command: str = "", python_version: str = "") -> str:
    """The command that provisions the Python toolchain here, or "" when nothing says how."""
    if install_command:
        return install_command
    return " && ".join(shlex.join(step) for step in python_steps(root, python_version))


def install_argvs(
    root: Path, install_command: str = "", python_version: str = ""
) -> tuple[tuple[str, ...], ...]:
    """`python_fix` as steps this interpreter can run with no shell; () when it cannot.

    A manifest `install_command` is a shell string by contract, and one that needs a
    shell -- `&&`, a redirect, a quote -- is left to the person the reports name it to.
    `python` is this interpreter, as `worktree_env.plain_argv` has it: whatever the name
    resolves to on `PATH` is the interpreter that was just found to be lacking.
    """
    if install_command:
        if SHELL_SYNTAX & set(install_command) or not install_command.split():
            return ()
        steps: tuple[tuple[str, ...], ...] = (tuple(install_command.split()),)
    else:
        steps = python_steps(root, python_version)
    return tuple(
        (sys.executable, *step[1:]) if step[0].lower() in PYTHON_NAMES else step for step in steps
    )


def frontend_fix(root: Path, frontend_dir: str) -> str:
    """`npm ci` when the lock is committed, else `npm install`.

    Not about speed: `npm install` rewrites `package-lock.json`, so running it in a
    worktree leaves a tracked file modified before anything has been edited -- which
    `ship.py` then refuses as a dirty tree. `ci` installs the lock exactly.
    """
    verb = "ci" if (root / frontend_dir / "package-lock.json").is_file() else "install"
    return f"npm {verb} --prefix {frontend_dir}"


def path_sources(root: Path) -> tuple[str, ...]:
    """The relative `path` entries of `[tool.uv.sources]`, as written; () when unreadable."""
    try:
        data: object = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    for key in ("tool", "uv", "sources"):
        data = data.get(key) if isinstance(data, dict) else None
    specs = data.values() if isinstance(data, dict) else ()
    paths = [spec.get("path") for spec in specs if isinstance(spec, dict)]
    return tuple(p for p in paths if isinstance(p, str) and not Path(p).is_absolute())


def missing_path_sources(root: Path) -> tuple[Gap, ...]:
    """A relative path dependency this checkout cannot see, with where its checkout does.

    `../data-lake` resolves beside the checkout and nowhere else, so from a worktree every
    `uv run` in ibkr_trader died on `Distribution not found` -- which names the path and
    not the reason. The target is read off the tree's `.git` pointer, so it is the copy
    the main checkout builds against, not a guess.
    """
    checkout = worktree_tiers.git_checkout(root) or root
    return tuple(
        Gap(
            f"{rel} is not here -- a [tool.uv.sources] path dependency, so `uv sync` "
            "fails with 'Distribution not found'",
            f"link {rel} to a checkout of it; the main checkout's is {(checkout / rel).resolve()}",
        )
        for rel in path_sources(root)
        if not (root / rel).exists()
    )


def missing_toolchain(root: Path, cfg: harness_config.Config | None = None) -> tuple[Gap, ...]:
    """Every gap in this checkout's toolchain. Empty when it is provisioned.

    A project with no dependency file at all has nothing to install and is told nothing;
    a frontend tier is judged only when the manifest switches it on and the directory
    exists, since a manifest half-filled for a tier the repo does not have is the
    `devkit-manifest` hook's finding rather than this one.
    """
    config = harness_config.load(root) if cfg is None else cfg
    # First: the install below cannot succeed until these are in place.
    gaps: list[Gap] = list(missing_path_sources(root))
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


def venv_python(root: Path) -> Path:
    """The interpreter inside `root`'s `.venv`, spelled for this platform."""
    if os.name == "nt":
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python"


def has_module(module: str) -> bool:
    """Whether this interpreter can import `module`, without importing it."""
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def provision_python(
    root: Path, cfg: harness_config.Config | None = None, run: Runner = subprocess.run
) -> bool:
    """Install `root`'s Python toolchain with the command `missing_toolchain` names.

    True when every step ran clean and left an interpreter in `.venv`. Says what it is
    doing on stderr, because a run that goes quiet for a minute reads as a hang. Not
    attempted, with the reason printed, when a path dependency the tree cannot see would
    fail `uv sync` with a message naming the path and not the reason, or when the
    manifest's `install_command` needs a shell (`install_argvs`).
    """
    config = harness_config.load(root) if cfg is None else cfg
    blockers = missing_path_sources(root)
    for gap in blockers:
        print(f"toolchain: cannot provision .venv: {gap.line}", file=sys.stderr)
    fix = python_fix(root, config.python.install_command, config.python.version)
    steps = install_argvs(root, config.python.install_command, config.python.version)
    if fix and not steps:
        print(f"toolchain: {fix} needs a shell; run it yourself", file=sys.stderr)
    if blockers or not steps:
        return False
    print(f"toolchain: no .venv here; provisioning it: {fix}", file=sys.stderr)
    for step in steps:
        try:
            done = run(
                list(step),
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except OSError as exc:
            print(f"toolchain: could not run {step[0]} ({type(exc).__name__})", file=sys.stderr)
            return False
        if done.returncode != 0:
            tail = "\n".join(
                ((done.stdout or "") + (done.stderr or "")).splitlines()[-INSTALL_TAIL_LINES:]
            )
            print(
                f"toolchain: {shlex.join(step)} exited {done.returncode}:\n{tail}", file=sys.stderr
            )
            return False
    return venv_python(root).is_file()


def rerun_target(
    root: Path,
    module: str,
    env: Mapping[str, str] | None = None,
    run: Runner = subprocess.run,
) -> Path | None:
    """The tree's `.venv` interpreter to re-run a runner under, or None to carry on here.

    None while this interpreter has `module` -- the runner already works, and this
    changes nothing about it -- and inside a re-run. Otherwise the tree's `.venv`,
    provisioned first when it is missing. CI never provisions: its environment is the
    workflow's to build, and an install there would hide a broken setup step.
    """
    environ = os.environ if env is None else env
    if environ.get(RERUN_ENV) or has_module(module):
        return None
    target = venv_python(root)
    if not target.is_file() and (environ.get("CI") or not provision_python(root, run=run)):
        return None
    return target


def rerun_in_venv(
    root: Path,
    module: str,
    script: Path,
    argv: Sequence[str],
    env: Mapping[str, str] | None = None,
    run: Runner = subprocess.run,
) -> int | None:
    """Run `script` again under the tree's `.venv` when this interpreter lacks `module`.

    The exit code of that run, or None when the caller should carry on itself -- see
    `rerun_target`. The re-run's streams are the caller's own, so its status line and
    artifact path reach the terminal exactly as a direct run's would.
    """
    environ = dict(os.environ if env is None else env)
    target = rerun_target(root, module, environ, run)
    if target is None:
        return None
    print(
        f"toolchain: this interpreter has no {module}; re-running under {target}", file=sys.stderr
    )
    environ[RERUN_ENV] = "1"
    done = run([str(target), str(script), *argv], cwd=root, env=environ, check=False)
    return done.returncode


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
