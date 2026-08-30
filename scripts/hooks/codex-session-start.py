#!/usr/bin/env python3
"""Run the shared SessionStart behavior from Codex on every platform.

The source hook is a Bash script because Claude's local and cloud runtimes use
Bash. On Windows, ``bash`` resolves to WSL and may be unavailable even though
Codex itself is running normally. This bridge preserves the Bash implementation
on POSIX and mirrors its small local-only branch-sync path on Windows.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SESSION_START_SH = REPO_ROOT / ".claude/hooks/session-start.sh"
SESSION_SYNC = REPO_ROOT / "scripts/hooks/session-sync.py"


def run(
    args: list[str],
    *,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run one child command from the repository root."""
    return subprocess.run(
        args,
        cwd=REPO_ROOT,
        capture_output=capture_output,
        check=False,
        text=True,
        # UTF-8 with replacement, never the platform codec -- see the codec note under
        # `VERIFY_IMPORT` in stop.py. A SessionStart hook that raises on a byte in a
        # branch name starts no session.
        encoding="utf-8",
        errors="replace",
    )


def branch_name() -> str:
    """Return the current branch, or an empty string for detached/error states."""
    result = run(["git", "branch", "--show-current"], capture_output=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def git_path(name: str) -> Path | None:
    """Resolve one Git administrative path without assuming .git is a directory."""
    result = run(["git", "rev-parse", "--git-path", name], capture_output=True)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    path = Path(result.stdout.strip())
    return path if path.is_absolute() else REPO_ROOT / path


def run_windows_local() -> int:
    """Mirror the local branch of session-start.sh without requiring Bash."""
    branch = branch_name()
    if not branch or branch == "master":
        return 0

    print(f"[session-start] Syncing '{branch}' onto origin/master (no-op if tree is dirty)...")
    result = run([sys.executable, str(SESSION_SYNC)])
    if result.returncode == 0:
        return 0

    rebase_paths = [git_path("rebase-merge"), git_path("rebase-apply")]
    if any(path is not None and path.is_dir() for path in rebase_paths):
        run(["git", "rebase", "--abort"])
        print(
            "[session-start] origin/master had conflicting changes - auto-sync aborted, "
            "branch left untouched. Sync manually to resolve."
        )
    else:
        print(
            "[session-start] Branch sync skipped (dirty tree or offline) - "
            "sync manually when ready."
        )
    return 0


def main() -> int:
    if os.name == "nt":
        if os.environ.get("CLAUDE_CODE_REMOTE") == "true":
            print(
                "[session-start] WARN: remote provisioning requires a POSIX runtime; "
                "skipping it on Windows."
            )
            return 0
        return run_windows_local()

    return run(["bash", str(SESSION_START_SH)]).returncode


if __name__ == "__main__":
    sys.exit(main())
