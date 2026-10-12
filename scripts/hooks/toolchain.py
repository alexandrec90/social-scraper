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
file the same friction. A tree that *has* a `.venv` is re-synced on the same path once
its lock is newer than the venv's last install (`resync_if_stale`), since a merge that
adds a package is the same `No module named` one step later. Building the tree's own
`.venv` at that first run, instead of borrowing the checkout's, is deliberate: a project
installed editable points its venv at the checkout's `src/`, so a borrowed one would
test the checkout's code, not the branch's.

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
import re
import shlex
import subprocess
import sys
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_config
import sibling_pins
import worktree_tiers

# The Python toolchain, in the order the ladder reads them.
PYTHON_MARKERS = ("uv.lock", "requirements-dev.txt", "pyproject.toml")

# Set in the environment of a runner re-run under the tree's `.venv`: a venv that still
# lacks the module must fail there, not send the run round again.
RERUN_ENV = "DEVKIT_TOOLCHAIN_RERUN"

# Touched inside `.venv` by every clean install, so a lock that changed after it -- a
# merge that added a dependency, a checkout -- reads as newer than what the venv holds.
SYNC_STAMP = ".devkit-synced"

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


def dependency_files(root: Path) -> tuple[Path, ...]:
    """The files `python_steps` installs from: a change to one is a change to `.venv`'s due."""
    if (root / "uv.lock").is_file():
        return (root / "uv.lock",)
    if (root / "requirements-dev.txt").is_file():
        names = ("requirements.txt", "requirements-dev.txt")
        return tuple(root / name for name in names if (root / name).is_file())
    if (root / "pyproject.toml").is_file():
        return (root / "pyproject.toml",)
    return ()


def stale_dependency(root: Path) -> Path | None:
    """The dependency file changed since `.venv` was last installed from, or None.

    A tree that already has a `.venv` used to be trusted as-is, so a merge that added a
    package to `uv.lock` left every run in it failing on `No module named ...` until
    someone synced by hand. The venv is dated by `SYNC_STAMP`, else by its `pyvenv.cfg`,
    which a venv provisioned by something else still has; a directory carrying neither
    cannot be dated and is not judged. Nor is a borrowed one -- a link to the checkout's
    (`worktree.symlinkDirectories`): it is the checkout's to sync, against its own lock.
    """
    venv = root / ".venv"
    try:
        # `is_junction` is 3.12's; a consumer's runner may be started by an older one.
        if venv.is_symlink() or getattr(venv, "is_junction", lambda: False)():
            return None
        marks = [
            mark.stat().st_mtime
            for mark in (venv / SYNC_STAMP, venv / "pyvenv.cfg")
            if mark.is_file()
        ]
        if not marks:
            return None
        synced = max(marks)
        return next((f for f in dependency_files(root) if f.stat().st_mtime > synced), None)
    except OSError:
        return None


def creates_venv(step: Sequence[str]) -> bool:
    """Whether `step` is `venv_argv`'s -- the step a re-sync of an existing venv skips.

    `uv venv` over an existing `.venv` replaces it, and on Windows that fails outright
    while the runner asking for the sync is that venv's own interpreter.
    """
    return tuple(step[1:]) == ("-m", "venv", ".venv") or tuple(step[:2]) == ("uv", "venv")


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


# --- the sibling a path source names, held at the commit this tree's CI pins ----

WORKFLOWS = Path(".github") / "workflows"
# A step's start: a sequence item. Every key of one checkout step is read between two.
STEP_START = re.compile(r"^\s*-\s", re.M)
# `actions/checkout`'s two inputs that name another repo and where it stands.
CHECKOUT_KEY = re.compile(r"^\s*(repository|ref)\s*:\s*(.*?)\s*(?:\s#.*)?$")
# Variables git reads ahead of `-C`: a runner started by a pre-commit hook inherits
# `GIT_INDEX_FILE` and would aim every sibling command at this tree's repository.
GIT_REPO_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_PREFIX")


def pinned_ref(root: Path, name: str) -> str:
    """The ref `root`'s own CI checks its sibling repo `name` out at; "" if none pins it.

    The same reading as devkit's `worktree_env.pinned_ref`, which cuts the sibling and is
    installed alone, so it cannot import this; `tests/test_worktree_env.py` holds the two
    to one answer. `pr-gate*` is read first, as a nightly may check the same repo out at
    its default branch on purpose; a ref that is a workflow expression is no pin.
    """
    try:
        files = sorted(
            (root / WORKFLOWS).glob("*.y*ml"),
            key=lambda path: (not path.name.startswith("pr-gate"), path.name),
        )
    except OSError:
        return ""
    for workflow in files:
        try:
            text = workflow.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for step in STEP_START.split(text):
            keys: dict[str, str] = {}
            for line in step.splitlines():
                found = CHECKOUT_KEY.match(line)
                if found:
                    keys.setdefault(found.group(1), found.group(2).strip("'\""))
            repo = keys.get("repository", "").rstrip("/").rsplit("/", 1)[-1]
            ref = keys.get("ref", "")
            if repo.lower() == name.lower() and ref and "${{" not in ref:
                return ref
    return ""


def _git(where: Path, *args: str, timeout: float = 10) -> str:
    """`git -C where <args>`'s stripped stdout; "" when it fails or cannot run."""
    env = {k: v for k, v in os.environ.items() if k not in GIT_REPO_VARS}
    try:
        done = subprocess.run(
            ["git", "-C", str(where), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


# Spelled apart from any f-string: a doubled brace there reads as an unrendered template
# tag to the generated-project check, which scans every vendored file.
PEEL_COMMIT = "^{commit}"


def _commit_of(where: Path, pin: str) -> str:
    for ref in (f"origin/{pin}", pin):
        found = _git(where, "rev-parse", "--verify", "--quiet", ref + PEEL_COMMIT)
        if found:
            return found
    return ""


def _warning(target: Path) -> str:
    have = _git(target, "rev-parse", "HEAD")
    return f"do not `uv lock` here until it is: the lock would be resolved against {have[:7]}"


def align_sibling(root: Path, target: Path, wait: float = sibling_pins.WAIT) -> tuple[bool, str]:
    """Hold the sibling tree `target` at the commit `root`'s CI pins it to, for this run.

    `(moved, line)`: whether it moved, and what to say -- "" when there is nothing to.
    Only a linked worktree that is detached and clean is moved: that is the tree
    devkit's `worktree_env` cut for every worktree of the tier to share. A checkout of
    its own is a person's, and a branch checked out there is a task's. Once there, the
    run leases it (`sibling_pins.claim`), and a move waits for every other run leasing it
    at another commit -- it is the one tree for the whole tier, so moving it under a
    running one is what broke that run (d32ffcc2).
    """
    if not (target / ".git").is_file():
        return False, ""
    pin = pinned_ref(root, target.name)
    if not pin:
        return False, ""
    want = _commit_of(target, pin)
    if not want:
        _git(target, "fetch", "--quiet", "origin", timeout=60)
        want = _commit_of(target, pin)
    have = _git(target, "rev-parse", "HEAD")
    if not want:
        return False, f"CI pins {target.name} to {pin}, which {target} lacks; {_warning(target)}"
    if want != have and _git(target, "symbolic-ref", "-q", "HEAD"):
        return False, f"{target} is on a branch, not at CI's pin {pin}; {_warning(target)}"
    if want != have and _git(target, "status", "--porcelain", "--untracked-files=no"):
        return False, (
            f"{target} has local changes, so it is not at CI's pin {pin}; {_warning(target)}"
        )

    def move() -> bool:
        _git(target, "checkout", "--quiet", "--detach", want, timeout=60)
        return _git(target, "rev-parse", "HEAD") == want

    shared = sibling_pins.Sibling(target, want, lambda: _git(target, "rev-parse", "HEAD"), move)
    done = sibling_pins.claim(shared, root, sibling_pins.Pace(wait=wait))
    if done.blocked:
        holders = "; ".join(lease.line for lease in done.blocked)
        return False, f"{target} is held by {holders}, not at CI's pin {pin}; {_warning(target)}"
    if done.held is None:
        return False, f"could not move {target} to CI's pin {pin}; {_warning(target)}"
    if not done.moved:
        return False, ""
    return True, f"moved {target} from {have[:7]} to {want[:7]}, the commit this tree's CI pins"


def align_path_sources(root: Path, env: Mapping[str, str] | None = None) -> tuple[Path, ...]:
    """Hold every sibling a path source names at this tree's CI pin; the ones moved.

    ibkr_trader's `.claude/worktrees` all resolve `../data-lake` to one shared tree, and
    `worktree_env` moved it to a tree's pin only when that tree was cut -- so cutting a
    tree off an older branch moved it back under every other, and a later `uv run` there
    relocked `uv.lock` against the wrong data-lake and failed mypy that CI passed
    (0db0d931, 941b7470). Run before anything syncs, every run re-checks it, and holds
    a lease on it for as long as this process lives, so a run in another tree on another
    pin waits rather than moving it under this one (`sibling_pins`, d32ffcc2). What
    could not be put right is said on stderr, never raised. CI checks the pin out itself.
    """
    environ = os.environ if env is None else env
    if environ.get("CI"):
        return ()
    moved: list[Path] = []
    for rel in path_sources(root):
        target = Path(os.path.normpath(root / rel))
        did, line = align_sibling(root, target)
        if line:
            print(f"toolchain: {line}", file=sys.stderr)
        if did:
            moved.append(target)
    return tuple(moved)


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
    root: Path,
    cfg: harness_config.Config | None = None,
    run: Runner = subprocess.run,
    stale: Path | None = None,
) -> bool:
    """Install `root`'s Python toolchain with the command `missing_toolchain` names.

    True when every step ran clean and left an interpreter in `.venv`, which is then
    stamped (`SYNC_STAMP`). Says what it is doing on stderr, because a run that goes
    quiet for a minute reads as a hang. Not attempted, with the reason printed, when a
    path dependency the tree cannot see would fail `uv sync` with a message naming the
    path and not the reason, or when the manifest's `install_command` needs a shell
    (`install_argvs`). With `stale` -- the dependency file newer than an existing venv --
    it re-syncs that venv, skipping the step that would create it.
    """
    config = harness_config.load(root) if cfg is None else cfg
    blockers = missing_path_sources(root)
    for gap in blockers:
        print(f"toolchain: cannot provision .venv: {gap.line}", file=sys.stderr)
    fix = python_fix(root, config.python.install_command, config.python.version)
    steps = install_argvs(root, config.python.install_command, config.python.version)
    if fix and not steps:
        print(f"toolchain: {fix} needs a shell; run it yourself", file=sys.stderr)
    if stale is not None:
        steps = tuple(step for step in steps if not creates_venv(step))
    if blockers or not steps:
        return False
    if stale is None:
        print(f"toolchain: no .venv here; provisioning it: {fix}", file=sys.stderr)
    else:
        line = " && ".join(shlex.join(step) for step in steps)
        print(f"toolchain: .venv predates {stale.name}; re-syncing it: {line}", file=sys.stderr)
    if not all(_install_step(root, step, run) for step in steps):
        return False
    if not venv_python(root).is_file():
        return False
    (root / ".venv" / SYNC_STAMP).touch()
    return True


def _install_step(root: Path, step: Sequence[str], run: Runner) -> bool:
    """Run one install step in `root`; on a failure, print why and answer False."""
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
        print(f"toolchain: {shlex.join(step)} exited {done.returncode}:\n{tail}", file=sys.stderr)
        return False
    return True


def runs_tree_venv(root: Path, prefix: str | None = None) -> bool:
    """Whether this interpreter is `root`'s own `.venv` -- `.venv/Scripts/python.exe x.py`."""
    try:
        return (
            Path(sys.prefix if prefix is None else prefix).resolve() == (root / ".venv").resolve()
        )
    except OSError:
        return False


def resync_if_stale(root: Path, run: Runner = subprocess.run, moved: Sequence[Path] = ()) -> bool:
    """Re-sync `root`'s existing `.venv` when a dependency file changed since its install.

    `moved` -- siblings `align_path_sources` just moved -- counts as such a change: their
    metadata is part of what `.venv` was installed from. True when a sync ran clean. A
    failed one is printed and the runner carries on, to fail as it would have; the
    import caches are dropped so a package the sync just added is importable by the
    interpreter that asked for it.
    """
    stale = stale_dependency(root) or next((m / "pyproject.toml" for m in moved), None)
    if stale is None or not venv_python(root).is_file():
        return False
    synced = provision_python(root, run=run, stale=stale)
    importlib.invalidate_caches()
    return synced


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

    Whichever way the run goes, a tree `.venv` it is about to use -- this interpreter, or
    the re-run's -- is re-synced first when its lock moved on since (`stale_dependency`):
    `.venv/Scripts/python.exe scripts/run-tests.py` after a merge that added a package
    used to fail on the import until someone ran `uv sync` by hand. Before either, a
    shared sibling a path source names is put back at this tree's CI pin
    (`align_path_sources`), since the sync resolves against it.
    """
    environ = os.environ if env is None else env
    if environ.get(RERUN_ENV):
        return None
    moved = align_path_sources(root, environ)
    lacking = not has_module(module)
    if (lacking or runs_tree_venv(root)) and not environ.get("CI"):
        resync_if_stale(root, run, moved)
    if not lacking:
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
