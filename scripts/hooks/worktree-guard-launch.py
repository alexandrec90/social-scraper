#!/usr/bin/env python3
"""PreToolUse shim: run devkit's worktree guard from a project that does not hold it.

**The guard was wired in exactly one repo, and it was not the one doing the work.**
`worktree-guard.py` needs the multi-root registry, the box lease table and the reap
ledger, so it lives in devkit and is not vendored. That left every consuming project --
five of them here -- with no guard at all: a session opened in one edited its `main`
directly, all day, and nothing said a word. The ledger proves it, because it has no
`guard-route` event on this machine at all while three checkouts sat holding uncommitted
agent work on their home branches.

This is the one line each project needs. It resolves devkit, forwards the hook payload
unchanged, and hands back the guard's own verdict.

**Shipping the file was not shipping the hook.** A settings file is the project's own
and is never vendored, so for a release every consumer held this shim and ran nothing --
the same silence the paragraph above describes, now with the fix sitting unused on disk.
`sync-devkit.py --pull` back-fills the `PreToolUse` entry (`wire_settings`), `--check`
reports a project that vendors this file and runs no hook, and
`workspace-status.unguarded_line` names one at session start.

**Every failure path here exits 0.** That is the whole contract and it is not laziness:
a PreToolUse hook exiting 2 is a *block*, so a shim that cannot find devkit and said so
loudly would refuse every tool call in the project -- turning "this repo has no guard"
into "this repo cannot be worked in". No devkit, no interpreter, a guard that crashes:
each is a reason to allow, because the alternative to an unguarded edit is not a safer
edit, it is no session.

Resolution order, first hit wins:

1. `$DEVKIT_DIR` -- the name the rest of the harness already uses (`sync-devkit.py`,
   `report-harness-defect.py`), so a machine that has set it for those has set it here;
2. `.devkit.toml`'s `[devkit] dir`, for a machine that would rather commit the path than
   set an environment variable;
3. a sibling `devkit/` beside this checkout, which is the workspace layout and therefore
   the case that needs no configuration at all.

`DEVKIT_HOOKS_OFF=branch-tier` stands this down before it resolves anything, and that is
the one exit-0 path here that is a *decision* rather than a fallback. See the kill-switch
section of `harness_config.py` for why the branch tier is switchable at all.

Stdlib only, and no import of anything under `scripts/` -- this runs before a virtualenv
exists, in a repo devkit does not control.

Tested in `scripts/hooks/tests/test_worktree_guard_launch.py`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# The guard's path inside a devkit checkout. A shim that pointed at a name devkit does
# not have would resolve a directory and then exit 0 forever, which looks exactly like a
# machine with no devkit -- so the existence of this file is what "found devkit" means.
GUARD_RELATIVE = Path("scripts") / "worktree-guard.py"

ENV_VAR = "DEVKIT_DIR"

# Claude Code reads exit 2 as "block". Nothing here ever chooses it; only the guard does,
# by exiting 2 itself, and that code is passed straight through.
EXIT_ALLOW = 0


def devkit_root(start: Path, env: dict[str, str] | None = None) -> Path | None:
    """Where devkit is, or None. See the resolution order in the module docstring."""
    environment = os.environ if env is None else env
    named = (environment.get(ENV_VAR) or "").strip()
    candidates = []
    if named:
        candidates.append(Path(named))
    candidates.append(start / ".." / "devkit")
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except (OSError, ValueError, RuntimeError):
            continue
        if (resolved / GUARD_RELATIVE).is_file():
            return resolved
    return None


def configured_dir(root: Path) -> str:
    """`[devkit] dir` out of `.devkit.toml`, or "".

    Read with a line scan rather than `tomllib`, for the same reason the rest of this
    tier avoids imports: this file has to work on an interpreter old enough to predate
    it, and one key does not justify the dependency.
    """
    manifest = root / ".devkit.toml"
    try:
        text = manifest.read_text(encoding="utf-8")
    except OSError:
        return ""
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if section != "devkit" or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == "dir":
            return value.strip().strip("'\"")
    return ""


def branch_tier_off() -> bool:
    """Whether `DEVKIT_HOOKS_OFF` names the branch tier.

    The import is local rather than module-level for `lint-fix.py`'s reason -- a consumer
    whose pull went sideways must not have every tool call routed through an ImportError,
    and this shim's whole contract is that it allows when it cannot decide. Written as a
    function rather than a module-level `try` with a `None` fallback because that shape
    costs two suppressions (`pragma: no cover` and `type: ignore`) in a vendored file
    where `structure_check.py` counts every one of them, and this needs neither.
    """
    try:
        import harness_config
    except ImportError:
        return False
    return bool(harness_config.hooks_off("branch-tier"))


def main(argv: list[str] | None = None) -> int:
    # Before the stdin read and before devkit is resolved, so a stood-down tier costs a
    # dict lookup rather than a subprocess. `branch-tier` covers this shim and, in
    # devkit, the guard and the slug hook behind it: an operator who has switched the
    # tier off is cutting branches through the workspace tasks instead.
    if branch_tier_off():
        return EXIT_ALLOW

    # UTF-8 in both directions, explicitly. `text=True` and a bare `sys.stdin` decode
    # through `locale.getencoding()` -- cp1252 on a Windows workstation -- and this shim
    # carries the guard's payload in and its `updatedInput` back out. A mangled byte on
    # the way in makes an `old_string` stop matching a byte-identical file; on the way out
    # it rewrites the text the agent typed. Neither raises, which is why it is spelled out
    # rather than left to the default.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    here = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    if not env.get(ENV_VAR):
        # Only consulted when the environment is silent, so a machine that has set the
        # variable keeps one answer rather than two that can disagree.
        configured = configured_dir(here)
        if configured:
            env[ENV_VAR] = configured

    root = devkit_root(here, env)
    if root is None:
        return EXIT_ALLOW

    # Through `.buffer` and decoded once, rather than letting the platform codec do it:
    # the payload carries the text of an edit, and cp1252 on a Windows workstation
    # silently rewrites an em dash rather than raising. The same read made the guard
    # refuse an `Edit` against a byte-identical file, because the box's copy was read as
    # UTF-8 and the `old_string` had come through cp1252.
    payload = "" if sys.stdin.isatty() else sys.stdin.buffer.read().decode("utf-8", "replace")
    try:
        result = subprocess.run(
            [sys.executable, str(root / GUARD_RELATIVE), *(argv or sys.argv[1:])],
            input=payload,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return EXIT_ALLOW

    # Forwarded verbatim. The guard's stdout carries the `updatedInput` that re-aims an
    # edit and its stderr carries the block message; rewriting either here would make the
    # shim a second author of a contract it does not own.
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
