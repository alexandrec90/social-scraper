---
description: What a coding session leaves to the fix pass — no commit, push or PR, no full test suite — the fixer sessions that are exempt, and why a checkout that cannot run its checks is fixed rather than reported
---

# Rule: Stop at the change

Deliberately **unscoped**, and vendored from devkit like `engineering.md`: change it
there, not here.

A coding session makes the change and describes it. It does **not**:

- `git commit`, `git push`, or open a PR (`gh pr create`) — finish with the `/ship`
  skill, which writes the commit message to a file and stops;
- run the whole test suite, or wait on a CI gate.

Running the tests for what you touched is optional. Where `scripts/run-tests.py --help`
says it picks them, run it bare: it builds the `.venv` a `claude --worktree` tree
arrives without, which `uv run pytest` builds lacking the dev extra.
The scheduled fix pass commits, pushes, opens the PR and runs the full gate in CI; a red
gate comes back to a fresh session with the failures named.

**Fixer sessions are exempt.** A fixer, a session the fix pass dispatched as its
prompt's first sentence says, follows that prompt and [`.claude/fixer.md`](../fixer.md),
including where they say to run the tests.

## An environment that cannot run the checks is part of the fix

Every session — fixer or not — that finds the linter or the tests unable to *start*
fixes that in the same session. A missing `.venv` or `node_modules`, or a runtime off the
project's pin, for example. It does not mention the problem in passing, write
`logs/fix-blocked.md` over it, or leave it: the next session, on the same machine, would
hit the same wall.

1. Run the project's provisioning command — the one its preflight names, or devkit's
   `python "$DEVKIT_DIR/scripts/worktree.py" provision .` from the tree (it installs;
   `--dry-run` only prints the plan).
2. If that command cannot close the gap and is the project's own script, extend it, with
   tests, in the same change. The pattern is `uv venv --python`: fetch the pinned
   version into a per-user cache, not the machine's.
3. Only a gap that needs something outside the repository is a blocker: admin rights,
   a credential, a paid service. Write that in `logs/fix-blocked.md` and lead the report
   with it.

**Then fix why it was open:** whatever cut this checkout unprovisioned — a worktree
tool, a dispatcher, a template — gets fixed in the same change, or the next tree it cuts
arrives just as empty. A tool outside the repo, or one vendored from devkit (a harness
defect under `engineering.md`), is reported instead. A fixer's "nothing else about the PR" scopes the *change under review*, not this.

Never "fix" it by hand-installing one binary, or by upgrading the machine's
system-wide runtime. That fixes this turn, not the next checkout.
