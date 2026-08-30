#!/usr/bin/env python3
"""CLI for the judgment half of the harness-events ledger: an agent flagging a defect.

The mechanical half of the ledger writes itself -- a gate that blocks knows it blocked,
and records the fact (`harness_events.py`). What no process boundary can capture is the
*agent's* half: "this block was a false positive", "this instruction sent me into a
dead end". The feedback-loop guardrail in `.claude/rules/engineering.md` already
requires that judgment to be reported; until now its only destination was the session
transcript, which a devkit-scoped session has to be handed by a human. This verb gives
the same report a durable, greppable destination on the machine's central ledger.

It complements the reply rather than replacing it: the user still needs to see the
flag in the report they are actually reading. And it is best-effort like every ledger
writer -- on a machine with no `$DEVKIT_DIR` it says so and exits 0, because a defect
report that itself errors is one nobody files twice.

Tested in `scripts/hooks/tests/test_report_harness_defect.py`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# scripts/hooks/ on path so the sibling, stdlib-only helpers import before the venv
# (same pattern as enforce-capped-bash.py).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_config
import harness_events

REPO_ROOT = (Path(__file__).parent / "../..").resolve()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Record a harness-defect report on this machine's central ledger."
    )
    parser.add_argument(
        "--message",
        required=True,
        help="what went wrong, in one sentence -- the same flag the reply carries",
    )
    parser.add_argument(
        "--command",
        default="",
        help="the exact command or tool call that triggered it, when one did",
    )
    args = parser.parse_args(argv)
    path = harness_events.record(
        "agent-report",
        (
            ("project", harness_events.project_name(REPO_ROOT)),
            ("version", harness_config.harness_version(REPO_ROOT)),
            ("command", args.command),
            ("message", args.message),
        ),
    )
    if path is None:
        print(
            "no central ledger on this machine ($DEVKIT_DIR unset) -- "
            "keep the report in your reply only."
        )
    else:
        print(f"recorded to {path} -- still include the report in your reply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
