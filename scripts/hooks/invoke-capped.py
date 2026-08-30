#!/usr/bin/env python3
"""Runs a shell command and caps combined stdout+stderr to stay within context limits.

The companion to `enforce-capped-bash.py`: that hook blocks an uncapped Bash call,
this is what the agent routes the call through instead. Both windows of the cap are
kept -- head and tail -- because the two useful parts of a long command's output are
the first lines (what it was doing) and the last (how it failed).

By default the shell is the platform's, not the caller's: `subprocess.run(shell=True)`
uses `/bin/sh` on POSIX and `cmd.exe` on Windows. `--shell powershell` is the Codex
Windows port. It invokes PowerShell 7 explicitly, so the wrapper and the shell tool
interpret quoting, pipelines, and cmdlets the same way.

Defaults come from `[bash]` in `.devkit.toml` (see `harness_config.py`) so a
project can widen or tighten the cap without forking this file. Explicit CLI flags
still win.

`cap_output` is pure and unit-tested in `scripts/hooks/tests/test_invoke_capped.py`.

Usage: python3 scripts/hooks/invoke-capped.py --command "cmd" [--shell platform|powershell]
       [--max-bytes N] [--head-bytes N]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

# scripts/hooks/ on path so the sibling, stdlib-only config helper imports before
# the venv (same pattern as stop.py's harness_config import).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_config

REPO_ROOT = (Path(__file__).parent / "../..").resolve()
CFG = harness_config.load(REPO_ROOT)

# Re-exported so `hook.MIN_MAX_BYTES` still resolves; defined in harness_config so the
# block message in `enforce-capped-bash.py` quotes this exact number.
MIN_MAX_BYTES = harness_config.MIN_MAX_BYTES


def cap_output(data: bytes, max_bytes: int, head_bytes: int) -> bytes:
    """Cap `data` to `max_bytes`, keeping a head window and a tail window."""
    if len(data) <= max_bytes:
        return data
    head_bytes = min(head_bytes, max_bytes)
    tail_bytes = max_bytes - head_bytes
    head = data[:head_bytes]
    tail = data[max(0, len(data) - tail_bytes) :] if tail_bytes > 0 else b""
    skipped = len(data) - max_bytes
    marker = f"\n... [truncated bytes={skipped}] ...\n".encode()
    return head + marker + tail


def run_capped(
    command: str,
    max_bytes: int,
    head_bytes: int,
    command_shell: str = "platform",
) -> tuple[int, bytes]:
    """Run `command` in a shell and return (exit_code, capped combined output)."""
    # The command is agent-supplied tooling, not external input. The default preserves
    # the original platform-shell behavior for Claude. Codex selects PowerShell
    # explicitly because shell=True would silently switch its command to cmd.exe.
    if command_shell == "powershell":
        result = subprocess.run(
            ["pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
        )
    else:
        result = subprocess.run(command, shell=True, capture_output=True)  # noqa: S602
    combined = result.stdout + result.stderr
    return result.returncode, cap_output(combined, max_bytes, head_bytes)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--command", required=True)
    parser.add_argument("--shell", choices=("platform", "powershell"), default="platform")
    parser.add_argument("--max-bytes", type=int, default=CFG.bash.max_bytes)
    parser.add_argument("--head-bytes", type=int, default=CFG.bash.head_bytes)
    args = parser.parse_args(argv)

    if args.max_bytes < MIN_MAX_BYTES:
        print(f"--max-bytes must be >= {MIN_MAX_BYTES}", file=sys.stderr)
        return 1

    exit_code, capped = run_capped(
        args.command,
        args.max_bytes,
        args.head_bytes,
        command_shell=args.shell,
    )

    sys.stdout.buffer.write(capped)
    if capped and not capped.endswith(b"\n"):
        sys.stdout.buffer.write(b"\n")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
