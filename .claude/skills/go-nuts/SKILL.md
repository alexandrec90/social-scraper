---
name: go-nuts
description: Unattended run. Work toward the given goal for as long as it takes, on your own judgement, without asking the user anything. Invoke only when the user types /go-nuts.
argument-hint: <what to achieve>
disable-model-invocation: true
---

# Go nuts

The user has left for the day. The goal is what they typed after the command:

$ARGUMENTS

Nobody will answer a question until tomorrow, so a question is a stall, not a check-in.
Treat every reversible choice as already approved.

## Decide, don't ask

- Never use AskUserQuestion. Never end a turn with "want me to…?", "shall I…?", or a
  menu of options.
- When you would have written "I recommend X", do X.
- When no option is clearly best, take the one that is cheapest to undo, and move on.
- Record every judgement call in `logs/overnight.md` as one line: what you chose, the
  alternative you rejected, and why. Append; never replace lines already there. That
  file is how the user reverses what they dislike, so a decision missing from it is one
  they cannot find.

## Keep going

- Do not stop at a milestone to report progress. Stop when the goal is met, or when
  everything left needs something only the user can supply.
- When one path is blocked, write the blocker in `logs/overnight.md` and work on
  whatever else advances the goal. A single blocker ends the run only when nothing else
  is left.
- A refusal from the harness is not a blocker to route around. Handle it the way
  `.claude/rules/engineering.md` says, then take a different path to the goal.
- When a test or check you ran fails, fix it. Do not narrate it and wait.

## Still off-limits

Being wrong is fine; being wrong in a way that cannot be undone is not. Unless the goal
above says otherwise, do not:

- delete branches, files or data you did not create this run;
- publish, send or post anything outside the repository: no messages, releases,
  issues or public artifacts;
- spend money, or change credentials or machine-wide settings.

`.claude/rules/session-scope.md` still holds: the run does not commit, push or open a PR.

## When you finish

Finish with the `/ship` skill, even when only part of the goal is done. Its body says
what got done, what did not and why, and points at `logs/overnight.md`. List the
decisions the user is most likely to want reversed first. Then report the same in a
few lines and stop.
