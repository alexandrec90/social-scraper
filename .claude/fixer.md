# Fixer sessions

For a session the fix pass dispatched. Its opening prompt says so in its first sentence,
which `scripts/fix_prompts.py` in devkit writes, and nothing else is a fixer. A session
a person started is a project session, whatever it is working on.

Vendored from devkit, and deliberately **outside `.claude/rules/`**: every file there is
loaded into every session, and a project session has no use for this one.

## Why a fixer's instructions differ

The rules under `.claude/rules/` are written for a project session. A person asked for a
change, and everything around it (the gate, the harness, the commit, the PR) belongs to
the fix pass. A fixer *is* the fix pass at work. It is sent at one named red thing, and
it is done when that thing is green, or when one of the three blockers in its prompt's
last sentence stands in the way.

## Where this file and a rule differ, this file wins

| The project-session instruction | For a fixer |
| --- | --- |
| `engineering.md`, *the harness is not your job* | In devkit, the harness is the job: fix the vendored file, the test or the template the failure names. In a consumer, a vendored file is drift-checked and cannot change there, so a failure whose fix is vendored is the *outside this repository* blocker: name the file and stop. |
| `engineering.md`, *never silently work around a refusal* | Still true of a refusal you did not cause and cannot fix: quote the command and stop. A dead end that *is* the failure you were sent at is the work, not a reason to stop. |
| `session-scope.md`, no commit, push or PR | Still true: every fixer finishes with the `ship` skill, and the pass commits, pushes and opens or updates the PR. What differs is that a fixer runs the targeted tests and the linter first, as its prompt says. |
| *the change the user asked for and nothing else* | The scope is whatever turns the named failure green: another branch in its own worktree, the PR's own diff, room made in a module at its limit, a vendored file here in devkit. |

## Before you stop

The prompt's last sentence is the only way to stop without a fix, and it names three
blockers. Check these first, because each has been the reason a fixer stopped when it
did not need to:

- **Does the failure reproduce where you are?** If not, find where it does. A PR that is
  red while its base is green is red on its own diff, so fetch its head, add a worktree
  on it and work there.
- **Is the obstacle a choice?** Never a blocker. Two ways to fix it, a design fork, a
  question for the project's owner: the option you would recommend is the decision. Do
  it, and put the reason and the alternatives in the intent. A sweep once stopped on a
  three-option question whose recommended option was already marked.
- **Is the obstacle a limit?** A module's size never is: `file_lines`, `imports` and
  `definitions` only warn. A function or class limit is fixed in the code by reshaping
  what is there; `scripts/hooks/structure_check.py --record` is the documented exception
  for growth the change cannot avoid, and it needs the reason written down.
- **Is the obstacle an instruction?** If it is one written for project sessions, the
  table above answers it.

## Report

Whatever the outcome, report: what was red, what you changed and where (the branch or
the PR), and what now passes. If you stopped, `logs/fix-blocked.md` is the report the
pass reads: name the blocker there and what you tried. If the failure was already fixed
and you changed nothing, the ship skill's intent is still the report, saying what fixed
it. A final message alone is none: the pass reads an outcome only from those two files.
