#!/usr/bin/env python3
"""PostToolUse hook: auto-fix just-edited Python files and relay what remains.

Runs after Edit/Write/MultiEdit/apply_patch on `.py` files. It applies the cheap,
deterministic fixers in place (`ruff format`, then `ruff check --fix`), so
formatting drift never survives to a commit — the CI lint job no longer gates on
it. Any lint finding ruff *cannot* auto-fix (undefined name, real bug pattern) is
printed to stderr with exit code 2, which Claude Code feeds straight back into the
coding agent's turn so it fixes the issue before finishing — no CI round-trip.

Deliberately ruff-only and edit-scoped to stay fast: the slower,
cross-file checks (mypy / vulture / frontend) belong in the Stop backstop and CI,
not on every keystroke. Best-effort throughout: a missing ruff or an unreadable
payload exits 0 so a tooling gap never blocks the agent.

**A file is linted against the project it lives in, or not at all.** The hook's own
location is the wrong root for both of the cases that actually occur. An agent edits
inside an ephemeral box under `<workspace>/.worktrees/`, which is not under the static
checkout this hook is vendored into, and it also writes scratch files to a session temp
directory, which is not under any project at all. Both used to be linted anyway, with
an absolute path and the *hook's* repo as the working directory, so ruff resolved
`per-file-ignores` against a config that does not own the file — the ignores silently
did not apply and rules the project deliberately disables (`S603`, `S607`, `T201`,
`S101` in tests) fired as blocking false positives on a throwaway script.

So `project_root_for` walks up from the file for a project marker: the file is linted
with that directory as the working directory, and skipped entirely when there is no
marker above it. A scratchpad stops being linted; a box starts being linted correctly,
by its own config rather than the vendoring checkout's.

**A marker is a ruff config, not a repository.** `.git` was one of them, which made the
claim "this tree configured ruff" out of a fact that says nothing about ruff at all, and
so caught every checkout an agent can reach — including a reference checkout that ships
no harness and no Python config. There, this hook was the whole harness applying itself
to a repo deliberately exempted from the rest of it: an edit to a `.py` file was
reformatted in place under ruff's *defaults*, and any finding those defaults could not
auto-fix exited 2 and blocked the turn. Nobody chose those rules for that tree, which is
the exact failure the paragraph above is about, one directory further out. Requiring a
config file states the claim honestly and needs no list of which checkouts are exempt —
a tree that configures ruff opts in by carrying the config, and every other tree is left
alone. Boxes are unaffected: a worktree checks out tracked files, and the config is one.

Pure helpers (`parse_hook_input`, `extract_paths`, `is_lintable`, `find_ruff`,
`project_root_for`) are unit-tested in `scripts/hooks/tests/test_lint_fix.py`.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

# scripts/hooks/ on path for the sibling, stdlib-only ledger helper; guarded because
# this hook is best-effort throughout, and a consumer whose pull went sideways must
# not have every edit blocked by an ImportError in its diagnostics.
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import harness_config
    import harness_events
except ImportError:  # pragma: no cover - a partially vendored consumer
    # One statement, one suppression: the structural gate counts each `type: ignore` as
    # something somebody chose to write, and two lines here would be two of them saying
    # the identical thing about the identical fallback.
    harness_config = harness_events = None  # type: ignore[assignment]

ALLOWED_TOOLS = {"Edit", "Write", "MultiEdit", "apply_patch", "create_file"}
REPO_ROOT = (Path(__file__).parent / "../..").resolve()

# What makes a directory the root of a project ruff should be run from. Ordered by how
# precisely each answers "which config governs this file": a ruff config is the direct
# answer, `pyproject.toml` usually carries one, and `.git` is the backstop for a
# repository that configures ruff nowhere and should still be linted with its own tree
# as the root.
#
# `.git` is matched as a path that *exists* rather than a directory: in a linked
# worktree -- which is exactly what an ephemeral box is -- `.git` is a file pointing at
# the primary's git dir, and requiring a directory would put every box back outside
# every project.
PROJECT_MARKERS = ("ruff.toml", ".ruff.toml", "pyproject.toml")

# Rules whose verdict is a statement about a *finished* file, evaluated here against a
# half-written one. `F401` is the whole list: adding `import os` and then, in the next
# edit, the function that uses it is the normal order for an incremental change, and this
# hook fires between the two -- so `--fix` deleted the import every time, and the F821 it
# became surfaced an edit later, pointing at the second edit rather than at the deletion.
# It has cost two sessions in the harness-events ledger (2026-08-22, 2026-08-23).
#
# Neither half of that can stay. Left fixable it is silently deleted; merely made
# unfixable it would be *reported*, which for this hook means blocking the edit that
# added the import -- worse, because it arrives immediately and reads as a rule against
# writing imports. So it is dropped from both runs, and the claim it makes is left to the
# place that can make it honestly: `lint-all.py`, the pre-commit gate and CI all see the
# file whole. That is the same line this hook already draws around mypy and vulture.
MID_EDIT_RULES = ("F401",)
PATCH_PATH_RE = re.compile(
    r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+?)\s*$",
    re.MULTILINE,
)


def parse_hook_input(raw: str) -> dict | None:
    """Parse raw stdin into a dict, or None when absent/malformed."""
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def extract_paths(hook_input: dict | None) -> list[str]:
    """Return edited paths for an allowed tool, including freeform patches.

    Tolerates both snake_case and camelCase payload keys (tool_name/toolName,
    tool_input/toolInput, file_path/filePath). Codex sends apply_patch content
    under ``tool_input.input`` instead of a file_path.
    """
    if not hook_input:
        return []
    tool = hook_input.get("tool_name") or hook_input.get("toolName") or ""
    if tool and tool not in ALLOWED_TOOLS:
        return []
    tool_input = hook_input.get("tool_input") or hook_input.get("toolInput") or {}

    found: list[str] = []
    patch: object
    if isinstance(tool_input, str):
        patch = tool_input
    elif isinstance(tool_input, dict):
        path = tool_input.get("file_path") or tool_input.get("filePath")
        if isinstance(path, str) and path:
            found.append(path)
        patch = tool_input.get("input") or tool_input.get("patch") or tool_input.get("command")
    else:
        return []
    if isinstance(patch, str):
        found.extend(PATCH_PATH_RE.findall(patch))

    return list(dict.fromkeys(found))


def extract_path(hook_input: dict | None) -> str | None:
    """Backward-compatible first edited path, or None."""
    paths = extract_paths(hook_input)
    return paths[0] if paths else None


def is_lintable(path: str) -> bool:
    """True for Python source files ruff should format and check."""
    return path.endswith((".py", ".pyi"))


def ruff_arg(target: Path, repo_root: Path) -> str:
    """Path form to hand ruff: project-relative POSIX when `target` is inside
    `repo_root`, else the absolute path.

    ruff matches `per-file-ignores` globs (e.g. `scripts/**/*.py`) against the file
    path relativised to the config-file directory. Handing ruff an absolute path
    makes that relativisation depend on exact drive-case / separator normalisation
    between the arg and ruff's discovered project root; when they differ, ruff
    treats the file as outside the project and the ignores silently do not apply,
    so ignored rules (T201/S603/S607, and S101 in tests) fire as false positives
    that block the edit. A repo-relative POSIX path needs no relativisation and
    matches the globs directly. `target`/`repo_root` are resolved first so a
    lowercase-drive payload path still relativises against the resolved repo root.
    """
    try:
        return target.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return str(target)


def project_root_for(target: Path) -> Path | None:
    """The project directory governing `target`, or None when it belongs to none.

    Walks up from the file's own directory and returns the first ancestor carrying a
    `PROJECT_MARKERS` entry. None is the answer that matters: it means nothing here
    configured ruff for this file, so linting it can only apply someone else's rules,
    and the honest thing is to leave it alone. A repository marker (`.git`) would not
    support that claim — every checkout has one, configured for ruff or not — so the
    markers are ruff's own config files and nothing else.

    Nearest-first, so a file inside a box resolves to the box rather than to the
    workspace above it, and a nested sub-project wins over its parent -- the same order
    ruff itself discovers configuration in.
    """
    try:
        start = target.resolve().parent
    except OSError:
        return None
    for directory in [start, *start.parents]:
        if any((directory / marker).exists() for marker in PROJECT_MARKERS):
            return directory
    return None


def find_ruff(repo_root: Path) -> str | None:
    """Resolve the ruff executable, preferring the project venv over PATH."""
    for sub in ("Scripts", "bin"):
        for name in ("ruff.exe", "ruff"):
            cand = repo_root / ".venv" / sub / name
            if cand.exists():
                return str(cand)
    return shutil.which("ruff")


def _run(ruff: str, *args: str, cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [ruff, *args],
        cwd=cwd,
        capture_output=True,
        check=False,
        text=True,
        # UTF-8 rather than the platform codec: ruff's diagnostics carry box-drawing and
        # curly quotes, and `text=True` alone decodes them through cp1252 on Windows,
        # where an unmapped byte raises inside subprocess's reader thread. That would
        # fail this hook -- which runs after *every* edit -- on the tool's output rather
        # than on the file's contents. The full account is the codec note under
        # `VERIFY_IMPORT` in stop.py, which is where this crashed a session first.
        encoding="utf-8",
        errors="replace",
    )


def main() -> int:
    # `is not None` because this hook tolerates a partially vendored consumer whose
    # sibling import failed; there the switch is simply unreadable and the hook runs.
    if harness_config is not None and harness_config.hooks_off("lint-fix"):
        return 0

    hook_input = parse_hook_input(_read_stdin())
    paths = [path for path in extract_paths(hook_input) if is_lintable(path)]
    if not paths:
        return 0

    failures: list[str] = []
    for path in paths:
        target = Path(path)
        if not target.is_absolute():
            target = REPO_ROOT / path
        if not target.exists():
            continue

        # Which project owns this file decides both whether to lint it and what config
        # to lint it by. No owner means a scratch file outside every project, and the
        # only rules available to judge it by would be someone else's.
        root = project_root_for(target)
        if root is None:
            continue

        # The project's own ruff first, so a box is linted by the toolchain it was
        # provisioned with. Falling back to this checkout's rather than skipping: an
        # unprovisioned box would otherwise lose the hook silently, which is the
        # failure mode being fixed, not a second instance of it.
        ruff = find_ruff(root) or find_ruff(REPO_ROOT)
        if not ruff:
            continue

        # Hand ruff the project-relative POSIX path so `per-file-ignores` globs match
        # deterministically. Claude Code's hook payload sends `file_path` as a
        # lowercase-drive absolute path (`c:\...`) while this process runs with an
        # uppercase-drive cwd (`C:\...`); ruff relativises an absolute arg against
        # its resolved project root to match the globs, and that drive-case mismatch
        # makes it treat the file as outside the project -- silently dropping the
        # ignores so T201/S603/S607 (and S101 in tests) fire as false positives.
        file_arg = ruff_arg(target, root)
        # Deterministic auto-fixers first, silently: formatting and import sorting
        # should never reach the agent as "errors" — they just get applied.
        mid_edit = ",".join(MID_EDIT_RULES)
        _run(ruff, "format", file_arg, cwd=root)
        _run(ruff, "check", "--fix", "--unfixable", mid_edit, file_arg, cwd=root)

        # Whatever remains is a genuine finding ruff can't fix on its own.
        remaining = _run(
            ruff, "check", "--ignore", mid_edit, file_arg, "--output-format=concise", cwd=root
        )
        if remaining.returncode != 0:
            detail = (remaining.stdout + remaining.stderr).strip()
            failures.append(f"ruff found issues in {path} that need a manual fix:\n{detail}")

    if failures:
        record_block(paths, failures)
        print("\n".join(failures), file=sys.stderr)
        return 2
    return 0


def record_block(paths: list[str], failures: list[str]) -> None:
    """One harness-events ledger line per blocking run, so a false-positive finding --
    the S603/T201-on-a-scratch-file class this hook has already shipped -- is
    diagnosable from `logs/harness-events.log` in the devkit checkout rather than from
    the chat of whichever session it blocked. Best-effort: `harness_events.ledger_path`
    owns where that is, and `record` no-ops (never raises) when the answer is nowhere.
    """
    if harness_events is None:
        return
    harness_events.record(
        "lint-fix-block",
        (
            ("project", harness_events.project_name(REPO_ROOT)),
            ("version", harness_config.harness_version(REPO_ROOT)),
            ("files", ";".join(paths)),
            ("detail", failures[0]),
        ),
    )


def _read_stdin() -> str:
    """Best-effort read of the hook payload; '' when stdin is a tty or unreadable.

    Decoded as UTF-8 rather than through the platform codec, for the reason
    `worktree-guard.read_stdin` sets out: the harness writes UTF-8 JSON and Windows
    decodes stdin as cp1252. What this hook takes from the payload is a file path, so
    the cost is a non-ASCII path silently missing the formatter rather than a corrupted
    edit -- a quiet skip, which is the shape of failure this repo treats as worst.
    """
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return ""
        buffer = getattr(sys.stdin, "buffer", None)
        if buffer is not None:
            return buffer.read().decode("utf-8", errors="replace")
        return sys.stdin.read()
    except (OSError, ValueError):
        return ""


if __name__ == "__main__":
    sys.exit(main())
