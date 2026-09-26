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

Running the tests for what you touched is fine when it helps you, and never required.
The scheduled fix pass commits, pushes, opens the PR and runs the full gate in CI; a red
gate comes back to a fresh session with the failures named.

**Fixer sessions are exempt.** A session the fix pass dispatched — its opening prompt
names a failing PR, a refused commit or a merge conflict — follows that prompt, including
where it says to commit, push or run the tests: that is its job, and this rule does not
override it.

## An environment that cannot run the checks is part of the fix

Every session — fixer or not — that finds the linter or the tests unable to *start*
fixes that in the same session. A missing `.venv` or `node_modules`, or a runtime on the
wrong version for the project's pin, are examples. It does not mention the problem in
passing, write `logs/fix-blocked.md` over it, or leave it for the next session. The next
session is on the same machine and would hit the same wall.

1. Run the project's provisioning command — the one its preflight names, or devkit's
   `python "$DEVKIT_DIR/scripts/worktree.py" provision <this tree> --yes` (without
   `--yes` it only prints the plan).
2. If that command cannot close the gap, and it is the project's own script, extend it
   so it can. Put that in the same change, with tests, and say why in the intent. The
   pattern is `uv venv --python`: fetch the pinned version into a per-user cache rather
   than depending on what the machine has installed.
3. Only a gap that needs something outside the repository is a blocker: admin rights,
   a credential, a paid service. Write that in `logs/fix-blocked.md` and lead the report
   with it rather than burying it under the results.

Never "fix" it by hand-installing one binary, or by upgrading the machine's
system-wide runtime. That fixes this turn and leaves the next checkout just as broken.
A provisioning *script* that is vendored from devkit is still a harness defect under
`engineering.md` and is reported, not edited.
