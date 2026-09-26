---
name: ship
description: 'Mark the task done: write the commit message as an intent file and stop. The fix pass commits, pushes and opens the PR.'
disable-model-invocation: false
---

# Ship the current task

Shipping is one file. You write it and stop. Nothing here runs a gate, commits, pushes
or opens a PR: the scheduled fix pass does all of that, reads what the gate says, and
sends a fixer if it is red. The only thing this session knows that nothing else can
recover is *why* the change was made, so that is the only thing it is asked for.

1. Read the change. Get the file list from `git status --short` and read the diff of
   each file with the Read tool. Do not run tests or the linter for this step: the
   commit-stage fixers run when the pass commits, and the PR gate judges the rest.
2. Write `logs/ship-intent.md` in this worktree with the Write tool:
   - the first line is the commit subject -- imperative, about the change, under
     seventy characters;
   - a blank line;
   - a body explaining *why*, in Markdown. Name identifiers in backticks. This body is
     the PR description and the only changelog a consumer of this repository gets, so
     write it for the reader who did not watch the work.
3. If the harness cost you turns -- a refusal, a missing tool, an instruction that sent
   you the wrong way -- write one line per thing to `logs/friction.md`. The pass files
   each on the harness-defect ledger, where the devkit session fixes it.
4. Report the subject and stop.

`logs/` is ignored in every project, so the file cannot be committed by accident. Once
shipped, the pass moves it to `logs/ship-intent.shipped.md`; to ship again, write a
fresh one, last.

## What happens next, and why none of it is your turn

The fix pass (`scripts/fix-pass.py`, scheduled every half hour, or the *Agent: Fix What
Is Red* task by hand) finds the intent, runs the commit-stage fixers, commits with your
message, pushes with the push gate skipped, and opens the PR. A PR from a tree the fix
pass cut for a fixer is labelled `automerge` and merges once green; yours is not, and
waits for a person. It records the outcome beside the intent in `logs/ship-state.json`.
CI runs the gate.
If it is red, the pass downloads the artifact and sends a fresh session at it with the
failing tests named; if the commit stage refused the change, the pass sends one at this
very worktree. A conflict gets a resolver that knows nothing about the gate. A vendored
failure shared across consumers gets one session in devkit rather than one per repo.

So: no `git commit`, no `git push`, no `gh pr create`, no `gh pr checks --watch`, no
autofix loop, and no "let me just run the tests once more". A session that pushes its
own branch is one the pass cannot tell from a session still working, and a session that
waits on a gate spends the tail of a long conversation on a verdict a fresh one reads in
a single call.

**Leave the worktree alone.** Do not reap a box, delete the branch or stop a stack.
`worktree.py reconcile` owns that and waits for the PR to actually merge.

**Never work around a refusal.** If something in the harness blocked a correct edit or
command, say so in your report with the exact command, put it in `logs/friction.md`,
and stop. The pass also reads every session's transcript for refusals and files them
itself (`scripts/session_friction.py` in devkit).
