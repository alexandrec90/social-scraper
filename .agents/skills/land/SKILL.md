---
name: land
description: 'Ship the work in this checkout: commit it, open its PR and get it merged into the default branch and the person''s own checkout, with nobody merging by hand. Use when the person says /land, "ship it", "create a PR" or "merge this". Not Claude Code''s built-in /ship, which never merges.'
argument-hint: '[hold]'
disable-model-invocation: false
---

# Land the current change

> Depends on `gh` being logged in and `origin` being a GitHub repository.

Arguments: `$ARGUMENTS`

One command does all of it: `scripts/land.py` commits, pushes, opens the PR (or reuses
the branch's open one), and gets it merged. Nothing waits on a person, and nothing runs
a gate in front of the merge, with one exception the script decides for you: a change to
a path the project's `.devkit.toml` lists under `[delivery] guarded` -- the code that
transacts money -- waits for its CI gate, then merges itself.

1. Read the change: `git status --short`, then the diff of each file with the Read
   tool. Run no tests or linter for this: every check runs after the merge, and a red
   one comes back to a fresh session.
2. Write the message. The title is the commit subject: imperative, about the change,
   under seventy characters. The body says *why*, in Markdown, identifiers in
   backticks. It is the PR description and the only changelog anyone gets, so write it
   for a reader who did not watch the work. Write the body to `logs/land-body.md` with
   the Write tool (`logs/` is ignored, so it is never committed).
   - Only when the arguments above are `hold`: end the body with `Hold: yes` standing
     alone on its own line. The PR then waits for the person. Never add it on your own
     judgement, nor because a change looks risky to you.
3. Run it from the checkout, with the machine's Python (it needs nothing installed):

   ```bash
   python scripts/land.py --title "<subject>" --body-file logs/land-body.md
   ```

4. Report the line it prints and stop. It is one of:
   - **merged** -- the change is on the default branch, and the checkout the worktrees
     hang off has been fast-forwarded to it (or the line says why it was left alone);
   - **merges itself once its gate is green** -- it touched a guarded path;
   - **conflicts** -- the scheduled fix pass sends a resolver, and it merges after;
   - **the queue merges it** -- GitHub had not decided mergeability yet;
   - **held** -- the person asked for `hold`, and it waits for them.

None of those is a job for the person, so never end with "you need to merge" or "after
this merges, run ...". Anything that must happen later goes in a task file in the same
change: `.claude/ship-intent.md` gives the format, in its last section.

If the script fails, report its error verbatim, add the same line to `logs/friction.md`,
and stop: do not merge, rebase or force-push around it by hand.

Leave the worktree alone afterwards: the harness reaps it once the PR has merged.
