"""Task notification wrapper.

Runs a command, times it, then sends a Windows toast with pass/fail status.
Exits with the same code as the wrapped command so VS Code shows the correct
task icon (green checkmark / red X).

Usage (tasks.json):
  python scripts/notify-wrap.py "Task Name" -- <command> [args...]

Example:
  python scripts/notify-wrap.py "Test: Run pytest" -- python scripts/run-tests.py
"""

import os
import subprocess
import sys
import time

# Ensure the sibling notify.py is importable whether this file is run as a script
# (script dir is on sys.path automatically) or loaded by path in tests (it isn't).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from notify import notify


def main() -> int:
    if "--" not in sys.argv:
        return 2

    sep = sys.argv.index("--")
    title = " ".join(sys.argv[1:sep])
    cmd = sys.argv[sep + 1 :]

    if not title or not cmd:
        return 2

    # A dismissed quick-pick is not an answer. VS Code aborts a task itself only for its
    # own `promptString`/`pickString` inputs; a `command` input -- every multi-select
    # picker -- returns nothing, no substitution is recorded, and the task launches with
    # the literal `${input:<id>}` still in the argument list. Run nothing, and toast
    # nothing: a cancel is the user's decision, not an outcome to notify them about.
    if any("${input:" in arg for arg in cmd):
        print("notify-wrap: cancelled -- nothing ran (a task prompt was dismissed)")
        return 0

    start = time.monotonic()
    try:
        # cmd comes from tasks.json (trusted), not user input.
        result = subprocess.run(cmd)
    except FileNotFoundError:
        # Windows can't CreateProcess batch launchers (npm, npx, vite) directly —
        # they're .cmd shims. Fall back to the shell so they resolve.
        result = subprocess.run(cmd, shell=True)  # noqa: S602
    elapsed = time.monotonic() - start

    minutes, seconds = divmod(int(elapsed), 60)
    elapsed_str = f"{minutes}m {seconds}s" if minutes else f"{seconds}s"

    if result.returncode == 0:
        notify(f"{title}", f"Passed in {elapsed_str}")
    else:
        notify(f"{title}", f"Failed ({elapsed_str})")

    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
