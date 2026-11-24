---
description: What a coding session leaves to the fix pass — no commit, push or PR, no full test suite — the fixer sessions that are exempt, and why a checkout that cannot run its checks is fixed rather than reported
---

# Rule: Stop at the change

Deliberately **unscoped**, and vendored from devkit like `engineering.md`: change it
there.

A coding session makes the change and describes it. It does **not**:

- `git commit`, `git push`, or open a PR (`gh pr create`) — only the top-level session
  ships; a subagent never invokes `/ship` or writes `logs/ship-intent.md`;
- run the whole test suite — `pytest tests`, or any run naming no test file, however
  few tests it holds — or wait on a CI gate.

- run any tests at all, or the linter by hand — not a single file;
- provision the tree to run them: no `uv sync`, no `.venv`, no `node_modules`, no
  `worktree.py provision`. A `claude --worktree` tree arrives bare and stays bare.

Write the tests the change needs, and stop there. The commit hook formats and lints,
and the scheduled jobs commit, push, open the PR and run the full gate in CI; a red
gate comes back to a fresh session with the failures named. That fresh session is a
fixer, and running the checks is its job, not yours.

**Fixer sessions are exempt:** one the fix pass dispatched, as its prompt's first
sentence says, follows that prompt and [`.claude/fixer.md`](../fixer.md), tests and
provisioning included.
