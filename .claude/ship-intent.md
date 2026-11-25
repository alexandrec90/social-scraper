# Finishing a session nobody can type `/ship` for

A person ships with `/land`, which merges the PR and syncs their checkout (Claude Code's
built-in `/ship` opens one the merge queue takes too). A person starts either, so a
session with nobody at the keyboard -- a fixer the fix pass sent, a `/go-nuts` or a
`/supervise-fix-pass` run -- finishes here instead, by writing one file and stopping.

Vendored from devkit, and deliberately **outside `.claude/rules/`**: every file there is
loaded into every session, and only an unattended one needs this.

## The intent file

1. Read the change. Get the file list from `git status --short` and read the diff of
   each file with the Read tool.
2. Write `logs/ship-intent.md` in this worktree with the Write tool:
   - the first line is the commit subject -- imperative, about the change, under
     seventy characters;
   - a blank line;
   - a body explaining *why*, in Markdown. Name identifiers in backticks. This body is
     the PR description and the only changelog a consumer of this repository gets, so
     write it for the reader who did not watch the work;
   - only when the person who started the run said this one waits for them:
     `Hold: yes`, standing alone on a line of its own, with nothing else on that line.
     Never add the line on your own judgement, nor because a change looks risky to you.
     Without it the PR merges itself.
3. If the harness cost you turns -- a refusal, a missing tool, an instruction that sent
   you the wrong way -- add one line per thing to `logs/friction.md`, keeping the lines
   already there: another session in this tree may have written them. Write each
   quoting the harness words you saw, not your paraphrase of them: the line is filed as
   written.
   End a line whose cause you fixed in this tree with `fixed on this branch`.
4. Report the subject and stop.

`logs/` is ignored in every project, so the file cannot be committed by accident. Once
shipped it moves to `logs/ship-intent.shipped.md`; to ship again, write a fresh one,
last. A tree with nothing changed -- a fix that was all ledger resolutions, or a failure
already fixed where the tree was cut -- ships the same way, the body saying what fixed
it: the pass sets the intent aside and opens no PR. A session that ends with no intent and no
`logs/fix-blocked.md` reads to the pass as one that died.

## What happens next

The ship task (`scripts/ship-pass.py` in devkit, every two minutes) commits whatever is
left with your message, pushes, and opens the PR if there is none, which merges with no
gate in a direct project (`[delivery] flow` in `.devkit.toml`). Every check runs after
the merge; a red one comes back to a fresh fixer, and a conflict to a resolver. So there
is nothing to wait for: no `gh pr checks --watch`, no "let me run the tests once more".

**Leave the worktree alone.** Do not reap a box, delete the branch or stop a stack:
`worktree.py reconcile` and `session_trees.py` own that.

## Never end by telling the person to do something

This section holds for every session, `/ship` or not. Nothing may depend on the person
remembering. They forget what is left waiting on them, so a session NEVER ends with "you
need to", "after this merges, run" or "on the other PC". **Anything that must happen
later goes in a task file, in the same change**, and every PC carries it out on its own:

```toml
# .devkit-queue/<name>.toml
when = "green"     # or merged, released, pr:<repo>#<n>, task:<name>, task:<repo>/<name>
on = "any"         # or every-machine, docker, or a hostname
run = "<command>"  # or agent = "<prompt>", or elevated = "<verb>": exactly one of the three
```

`when` and `on` are optional, and these are their defaults. Work that needs a vendored
change waits with `when = "adopted:<devkit commit>"`, true once this project's
`DEVKIT_VERSION` holds that commit. A task that must wait for the clock waits in the
queue rather than costing a session per retry: for a recurring window, `when = "outside:09:30-16:00 UTC-05:00 mon-fri"` (or `inside:`); for one moment,
`when = "after:2026-11-12T16:05-05:00"`, ISO 8601. Both take a UTC offset, never a zone
name. `run` executes in that project's checkout, and a failure goes to an agent on that
machine with the output. `agent` starts a fresh session. `elevated` takes
`restart-wsl-service` and nothing else. A task is its path and the commit that added it:
a revert removes it, an edit never runs a finished one again, so **to run something
again, add a new file**. A malformed file becomes an agent task saying so.

**Anything that does not fit, a case nobody thought of, is a plain `agent` task**, written
for a session with no memory of this one: what to do, in which files, and what finished
looks like. **A session with no code change still commits that one file.**

Only what truly needs the human, a payment or a login, reaches them, and only through a
task the agent attempts first. Your report may say what you queued; it may not leave
anyone a job.
