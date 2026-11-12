---
name: ship
description: 'Mark the task done: write the commit message as an intent file and stop. The fix pass or the ship task commits, pushes and opens the PR. `/ship auto` opens it to merge itself once green; `/ship hold` keeps it for a person.'
argument-hint: '[auto|hold]'
disable-model-invocation: false
---

# Ship the current task

Arguments: `$ARGUMENTS`

Shipping is one file. You write it and stop. Nothing here runs a gate, commits, pushes
or opens a PR: the scheduled jobs do all of that, read what the gate says, and send a
fixer if it is red. The only thing this session knows that nothing else can recover is
*why* the change was made, so that is the only thing it is asked for.

1. Read the change. Get the file list from `git status --short` and read the diff of
   each file with the Read tool. Do not run tests or the linter for this step: every
   check runs after you stop.
2. Write `logs/ship-intent.md` in this worktree with the Write tool:
   - the first line is the commit subject -- imperative, about the change, under
     seventy characters;
   - a blank line;
   - a body explaining *why*, in Markdown. Name identifiers in backticks. This body is
     the PR description and the only changelog a consumer of this repository gets, so
     write it for the reader who did not watch the work;
   - only when the arguments above are `auto`: a blank line, then `Automerge: yes` as
     the file's last line. The pass labels that PR `automerge`, and it merges once the
     gate is green with nobody reviewing it. That is the person's call, made by typing
     `/ship auto`: never add the line on your own judgement, nor because a change looks
     trivial to you;
   - only when the arguments above are `hold`: `Hold: yes`, standing alone on a line of
     its own, with nothing else on that line. The PR gets no merge label and waits for
     the person, whichever flow the project is in. The same rule: never add the line on
     your own judgement.
3. If the harness cost you turns -- a refusal, a missing tool, an instruction that sent
   you the wrong way -- add one line per thing to `logs/friction.md`, keeping the lines
   already there: another session in this tree may have written them. Write each
   quoting the harness words you saw, not your paraphrase of them: the line is filed as
   written, and the session sent at it has nothing else. The pass files
   each on the harness-defect ledger, where the devkit session fixes it. End a line whose
   cause you fixed in this tree with `fixed on this branch`: the pass files it as settled
   by this branch, and reopens it only if the branch never merges.
4. Report the subject and stop.

`logs/` is ignored in every project, so the file cannot be committed by accident. Once
shipped, it moves to `logs/ship-intent.shipped.md`; to ship again, write a
fresh one, last. A tree with nothing changed -- a fix that was all ledger resolutions, or
a failure already fixed where the tree was cut -- ships the same way, the body saying
what fixed it: the pass sets the intent aside and opens no PR. A session that ends with
no intent and no `logs/fix-blocked.md` reads to the pass as one that died.

## What happens next, and why none of it is your turn

A project is in one of two flows, `[delivery] flow` in its `.devkit.toml`.

**Gated, the default.** The fix pass (`scripts/fix-pass.py`, scheduled every half hour,
or the *Agent: Fix What Is Red* task by hand) finds the intent, runs the commit-stage
fixers, commits with your message, pushes with the push gate skipped, and opens the PR.
On a PC whose `devkit.fixPass` is `off`, the ship task does the same. A PR from a tree
the fix pass cut for a fixer is labelled `automerge` and merges once green; yours is not,
and waits for a person -- unless the intent ends `Automerge: yes`.

**Direct.** The ship task (`scripts/ship-pass.py`, every two minutes) commits with your
message, with no fixers and no pre-commit framework, pushes, and opens the PR labelled
`merge-now`, which merges with no gate consulted -- unless the intent holds that
`Hold: yes` line, and then it waits for a person, a resolver's commits included. Formatting and every check
run after the merge. A default branch that stays red gets its commits reverted, each
with a task queued to land that work again.

Either way the outcome is recorded beside the intent in `logs/ship-state.json`, and CI
runs the gate. If it is red, the pass downloads the artifact and sends a fresh session at
it with the failing tests named; if the commit stage refused the change, the pass sends
one at this very worktree. A conflict gets a resolver that knows nothing about the gate.
A vendored failure shared across consumers gets one session in devkit rather than one per
repo.

So: no `git commit`, no `git push`, no `gh pr create`, no `gh pr checks --watch`, no
autofix loop, and no "let me just run the tests once more". A session that pushes its
own branch is one the pass cannot tell from a session still working, and a session that
waits on a gate spends the tail of a long conversation on a verdict a fresh one reads in
a single call.

**Leave the worktree alone.** Do not reap a box, delete the branch or stop a stack.
`worktree.py reconcile` and `session_trees.py` own that and wait for the PR to actually
merge. A stack the project should keep running is not this tree's to host: it stays up,
its tree is kept for it, and your report names it to be moved to the checkout.

**Never work around a refusal.** If something in the harness blocked a correct edit or
command, say so in your report with the exact command, put it in `logs/friction.md`,
and stop. The pass also reads every session's transcript for refusals and files them
itself (`scripts/session_friction.py` in devkit).

## Never end by telling the person to do something

Nothing may depend on the person remembering. They forget what is left waiting on them,
so a session NEVER ends with "you need to", "after this merges, run" or "on the other
PC". **Anything that must happen later goes in a task file, in the same change**, and
every PC carries it out on its own:

```toml
# .devkit-queue/<name>.toml
when = "green"     # or merged, released, pr:<repo>#<n>, task:<name>, task:<repo>/<name>
on = "any"         # or every-machine, docker, or a hostname
run = "<command>"  # or agent = "<prompt>", or elevated = "<verb>": exactly one of the three
```

`when` and `on` are optional, and these are their defaults. `run` executes in that
project's checkout, and a failure goes to an agent on that machine with the output.
`agent` starts a fresh session. `elevated` takes `restart-wsl-service` and nothing else.
A task is its path and the commit that added it: a revert removes it, an edit never runs
a finished one again, so **to run something again, add a new file**. A malformed file
becomes an agent task saying so.

**Anything that does not fit, a case nobody thought of, is a plain `agent` task**, written
for a session with no memory of this one: what to do, in which files, and what finished
looks like. **A session with no code change still commits that one file:** write it, then
ship as above.

Only what truly needs the human, a payment or a login, reaches them, and only through a
task the agent attempts first. Your report may say what you queued; it may not leave
anyone a job.
