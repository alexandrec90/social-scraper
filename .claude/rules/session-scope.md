---
description: How a coding session ships (/land) and what it leaves to CI and the fix pass — no tests, no provisioning, no waiting on a gate — and the fixer sessions that are exempt
---

# Rule: Stop at the change

Deliberately **unscoped**, and vendored from devkit like `engineering.md`: change it
there.

Shipping is Claude Code's own business. Asked to ship, create a PR or merge, use the
`/land` skill: it commits, opens the PR and gets it merged, with no person in the loop.
An unattended session, or a task for later, goes as
[`.claude/ship-intent.md`](../ship-intent.md) says.

A session does **not**:

- run the whole test suite — `pytest tests`, or any run naming no test file, however
  few tests it holds — or wait on a CI gate;
- run any tests at all, or the linter by hand — not a single file;
- provision the tree to run them: no `uv sync`, no `.venv`, no `node_modules`, no
  `worktree.py provision`. A `claude --worktree` tree arrives bare and stays bare.

Write the tests the change needs, and stop there. Every check runs after you stop, in CI
and the scheduled jobs; a red one comes back to a fresh session with the failures named.
That fresh session is a fixer, and running the checks is its job, not yours.

**Fixer sessions are exempt:** one the fix pass dispatched, as its prompt's first
sentence says, follows that prompt and [`.claude/fixer.md`](../fixer.md), tests and
provisioning included.
