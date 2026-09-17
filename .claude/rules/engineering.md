---
description: Baseline engineering policy shared by every devkit project — test coverage, script conventions, the vendored harness seam, and the harness feedback loop that files every defect report on the central ledger
---

# Rule: Baseline engineering policy

Deliberately **unscoped** (no `paths:`) — the small set of rules that hold everywhere, so
there is no glob that should exempt a file from them.

**Vendored from devkit and byte-identical in every project.** It is in `sync-devkit.py`'s
`MANIFEST`, so a local edit is reported as drift by the PR gate rather than quietly
becoming this project's private opinion. Change it in devkit and let projects `--pull`. A
project's `CLAUDE.md` should **point at this file, not restate it**: a second copy looks
authoritative, is not gated, and diverges the first time either is edited.

This file is the decisions. The measurements and incidents behind them are in
[`.claude/engineering-evidence.md`](../engineering-evidence.md) — vendored alongside it,
and loaded only when a pointer sends you there.

## Testing

Every code change must include tests in the same commit. Every endpoint and every testable
unit of logic must have test coverage — gaps are not acceptable. If you touch something
that has no test, write the test in the same commit even if the logic didn't change.

- **New unit of logic:** the happy path, the error cases, and the edge cases.
- **Bug fix:** write the regression test first and watch it fail before you fix it. One
  that has never failed is asserting the wrong thing.
- **Reversion check:** before calling a change complete, identify which test would
  fail if the changed behavior were reverted. If none would, it is not covered yet.
- **Coverage floors are ratchets:** never lower it merely to make a change pass. The
  other end of the same rule is the one that slips quietly, because raising a ceiling —
  a timeout, a retry count, a size or complexity limit, a baseline of known gaps — reads
  as tuning rather than as relaxing a gate.
  **A ceiling raised on three consecutive branches is a defect report, not a raise:**
  find out what is filling it before moving it again, and say in the commit message what
  you found.
- **Run targeted tests** — the module you touched — plus the linter, while you work. The
  whole gate runs once, at push time: the `devkit-push-gate` pre-commit hook runs
  `lint-all.py`, `run-tests.py` and the hook tests before a push leaves, so a failure is
  read from `logs/` here rather than from a CI artifact.
- **Fix failures in the code, not in the assertion.** Relaxing an assertion to get green
  deletes the only evidence that something is wrong.
- A skipped or `xfail` test carries a linked issue or a one-line reason in the marker.

If the toolchain isn't available locally, still write the tests and leave execution to CI.
"I couldn't run it" defers the run, never the writing.

Instruction files — `CLAUDE.md`, `.claude/rules/*`, `.claude/skills/*` — are under this
same mandate. See `.claude/rules/authoring.md`.

## Claude Code's Bash calls: a short blocklist, not a proof obligation

`scripts/hooks/enforce-capped-bash.py` blocks one thing: a statement whose output grows
with the **repository** rather than with the command you wrote. The list is closed — `ls`,
`cat`, `find`, `tree`, `du`, `env`, `git status`, an uncounted `git log`, and a raw
`git diff`/`git show`.

**Everything else runs uncapped, and wrapping it is a mistake.** A `grep`, a `python -c`,
a test run, a `curl`, a heredoc: issue them bare. Routing every call through the wrapper
by reflex buys no second bound and has happened here at scale.

Three spellings take a named command off the list:

| Spelling | Trade-off |
| --- | --- |
| `\| head -c N`, `tail -c N`, `wc -l`, `grep -c <pat>` | **masks the exit code**, including a background task's completion status |
| `<cmd> > <file>` | strongest bound; the output never enters context |
| `python3 scripts/hooks/invoke-capped.py --command "<cmd>"` | keeps a head *and* tail window, preserves the exit code |

The wrapper runs through the platform shell — **`cmd.exe` on Windows** — so heredocs,
single-quoted paths and escaped alternation do not survive it. For `ls`, `cat` and `find`
the better answer is usually the Glob, Read and Grep tools, which cost no subprocess and
page rather than dump. The unconditional bound is `BASH_MAX_OUTPUT_LENGTH` in
`.claude/settings.json`, which truncates bytes that already exist and so cannot
false-positive. **Codex never sees this gate** — `scripts/sync-codex-hooks.py` omits it
from `.codex/hooks.json` and Codex caps output itself, so issue commands there directly,
the nine included.

**If it blocks something that is not one of the nine, that is a defect in it** — report it
per the guardrail below with the exact command, and never rewrite a correct command to
satisfy it. Why it is a blocklist rather than a proof obligation, and what preemptive
wrapping has cost, are in
[`.claude/engineering-evidence.md`](../engineering-evidence.md).

**A refusal that says the command "is too complex to verify that it stays inside the
worktree", "names git in a form too complex to verify", or "cannot be shown not to be
git", is not this hook and not any devkit hook.** It is Claude Code's own isolation guard
for a `claude --worktree` session. No setting turns it off and nothing in devkit can
change what it accepts — report it to Claude Code, not to this harness, where one week's
backlog once carried three of these filed as guard defects.

It judges the shape of the command line rather than what the command does, and it runs
**only on the Bash tool**. The same statement issued through the PowerShell tool is never
parsed, while the working-directory check that holds the session inside its worktree
applies to both — so on Windows the PowerShell tool is the answer for a compound
statement, not a workaround.

| Shape it refuses | What to issue instead |
| --- | --- |
| `~` in a path | `$HOME`, which resolves; the tilde is rejected unexpanded |
| `git -C`, `--git-dir`, `env -C`, `GIT_DIR=`, `cd … &&` before git | bare `git`, from the worktree |
| command substitution, `for`/`while`, a subshell | one call each — a plain `&&` list is fine |
| the letters `git` inside any of the above | nothing; it is a false positive |

**The last row is the one that wastes turns.** Inside a statement the parser could not
reduce to a simple list, the guard tests the *whole line* for `git` as an unanchored
substring, so `.gitignore`, `github.com`, `digit` and `legitimate` each read as "names
git" — rename the variable or split the statement, because no spelling of the real
command will satisfy it. A heredoc and a `$HOME` argument are **not** triggers on their
own; put a file write through the Write or Edit tool regardless, which sidesteps the
parse entirely. The reproductions are in
[`.claude/engineering-evidence.md`](../engineering-evidence.md).

## Waiting on a CI gate: one blocking call, not a poll loop

```bash
gh pr checks <N> --watch --fail-fast      # with run_in_background: true
```

`--watch` returns only once every check has settled, collapsing N polls into one call plus
the completion notification. **Backgrounding is the half that is easy to drop**: a gate
routinely outruns the Bash tool's ceiling, and a foreground `--watch` that times out is a
poll loop with the timeout as its interval.

This condemns neither **diagnosing a failure** (`gh run view --log-failed` and the greps
after it are the work, not waiting — send them to a file where the volume warrants) nor
**asking once**. The waste begins at the *second* identical poll.

**"No checks reported" has three causes needing opposite responses.** Ask once, after a
push — `gh pr view <N> --json mergeStateStatus,statusCheckRollup`:

| What you see | What it is | What to do |
| --- | --- | --- |
| `CONFLICTING` | no merge ref to build against, so the gate never will run | merge `origin/<default>` and push |
| `BLOCKED`/`CLEAN` | the run exists | `--watch` is right |
| `UNKNOWN` | the ordinary answer in the seconds after a push | says nothing either way; ask again |
| `UNSTABLE` **with an empty rollup** | a run exists that the PR cannot show you | `gh run list --branch <head> --event workflow_dispatch` |

That last row is the one that reads as the first and is its opposite. **A push made with
`GITHUB_TOKEN` raises no `pull_request` event**, so a workflow that commits to a PR branch
— a lock repair, a generated-file sync — leaves the only gate evidence on a run it
dispatched itself, and a `workflow_dispatch` run is not in the PR's check rollup. The PR
reads exactly like one whose gate has not started. carameli #347 sat four days that way
while its dispatched gate had *failed*, on a real test, with the fix a one-line command.
`UNSTABLE` is the tell: a gate that has not started yet cannot make a PR unstable.

If you get the message anyway, tell the not-started case from the rest by **how long the
call took, not what it said**: a `--watch` back in about a second never waited, so
re-issue it once.

When the gate will outlast anything useful you could do meanwhile, stop: report that the
branch is pushed and the gate is running, and let the result arrive in a fresh session.
What polling has cost is in
[`.claude/engineering-evidence.md`](../engineering-evidence.md).

## Scripts

All scripts under `scripts/` are Python — a local desktop and a CI runner are rarely the
same OS.

**One exception, and it is the only one that can exist:** a *bootstrap* that provisions
the interpreter cannot be written in it. A script that runs before Python is installed —
and nothing else — may be a native shell script, and it says in its own header why it is
not Python. Anything that can assume an interpreter does; "it was easier in PowerShell"
is not the exception, and a second native script doing work the first could have handed
to Python is the shape this clause exists to refuse.

- **Expose pure importable functions** behind `if __name__ == '__main__'`, so the logic is
  testable without spawning a subprocess, and keep side effects inside `main()`: the suite
  imports these modules.
- **Every new script ships with its tests in the same change.**
- **Hook scripts (`scripts/hooks/`) are stdlib only.** They run before the virtualenv is
  active, so an import of anything installed is a crash in the one context that cannot
  report it well.
- **Failure artifacts:** any script whose failures an agent is expected to act on writes
  them to a **parseable file under `logs/`** — on failure too, overwritten per run.
  Streamed terminal output scrolls away; keep the terminal to a status line plus the path.

## Lint policy

Lint catches **correctness and security** problems — the ones a human reviewer reads past.
Style is not a judgement call worth an agent's turn: `ruff format` runs at commit time from
`.pre-commit-config.yaml`, on every agent edit where the `lint-fix.py` PostToolUse hook is
enabled, and again in CI, so line length, quote style and import order never reach a
review. **On** for correctness, security and resource-handling; **off**
for anything a formatter can decide. A rule that fires on something a formatter would fix
is misconfigured — turn it off rather than teaching everyone to ignore it.

**Adding a family prefix to `select` enables every member, including the cosmetic ones.**
Read its members and ignore the cosmetic ones in the same change. A rule already exempted
in two or three directories is not a rule anyone wants: turn it off globally rather than
exempt it a fourth time. Which selectors are off, which side of the split each family falls
on, why a selector never spans linters, and the generated-project test that stops them
drifting back are in [`.claude/engineering-evidence.md`](../engineering-evidence.md) —
read it before editing any `select` or `ignore` list.

**Never silence a finding without naming the reason.** `# noqa`, `# type: ignore`,
`# nosec`, `eslint-disable` each claim the tool is wrong *here*. Write the claim down, and
prefer the rule-specific form so the suppression stops applying the moment a *different*
problem appears on that line — a bare `# noqa` is indistinguishable from "I gave up":

```python
result = subprocess.run(cmd, shell=True)  # noqa: S602 - agent-supplied tooling, not input
```

**When a linter is wrong there are two options, and skipping is not one.** Fix the
producer so the rule has nothing to say — it is usually right about something even when
wrong about the fix. Or suppress narrowly with the reason. When neither is honest, report
to the user with concrete options: what the rule wants, why it does not fit, what the
alternatives cost.

**Never skip a failing check, and never describe an error as "cosmetic", "harmless", or
"pre-existing" to justify leaving it.** An error is either actionable or noise to be
removed at the source; deciding it is ignorable trains everyone downstream to ignore the
next one. Same for tests: a failing test gets fixed or reported, never `skip`ped,
`xfail`ed, or deleted to make a run green. If a check is genuinely obsolete, delete the
check — deliberately, in its own change, with the reason in the commit message.

## The vendored agent harness

The hook scripts, this rule and the shared skills are vendored from devkit, the source of
truth. Each project commits its own copy, so a fresh clone needs no submodule and no
install step.

- Project specifics live in `.devkit.toml`, read by `scripts/hooks/harness_config.py`.
  **Never hard-code them in a vendored file**: a new behaviour gets a manifest field and a
  default, not an `if project ==` branch and not a paragraph naming one repo's paths.
- `python scripts/sync-devkit.py --check` fails on drift, `--pull` adopts upstream,
  `--push` sends a change authored here back up. `DEVKIT_VERSION` records the upstream
  commit the copy corresponds to.
- **`$DEVKIT_DIR` unset means there is nothing to compare against**, and the stamp decides
  what that is worth — clean before adoption, a failure once `DEVKIT_VERSION` exists.
- **An operator may switch the harness off** — `DEVKIT_HOOKS_OFF`.
- A vendored script may depend on a file the project owns (`scripts/lint-all.py`,
  `scripts/run-tests.py`); a missing one is a silent skip by design.

The stamp rule, the switch's values and reach, and the drift check for a machine with no
devkit clone are in [`.claude/engineering-evidence.md`](../engineering-evidence.md).

## Guardrail: the harness feedback loop

If an instruction in a skill, a rule or a `CLAUDE.md` sent you into a dead end or a wasted
operation — or a mistake you made would have been prevented by one that isn't there — flag
it in your report with the file, the line, and a proposed edit. **The harness itself is in
scope on the same terms**, and this is the half that goes unfiled because it does not look
like prose: a hook that blocked a correct command, a vendored script that crashed or
reported success while doing nothing, a guard that refused an edit it should have allowed,
a scheduled job whose artifact disagrees with what it did.

**Never silently work around a bad instruction.** That fixes your turn and leaves the next
agent at the same wall; these files only improve if the failures they cause are reported as
defects in them.

**Saying it in your reply does not file it.** A reply is read once, by someone who is
mid-task and did not ask to be a bug tracker; the next agent to hit the same wall sees
none of it. So every flag gets a durable copy on this machine's central ledger, **in the
turn you noticed it** rather than at the end of the task you may not finish. This
complements the flag in your reply rather than replacing it, and exits 0 on a machine with
no `$DEVKIT_DIR`:

```bash
python scripts/hooks/report-harness-defect.py --message "<what went wrong>" --command "<the exact command, when one triggered it>"
```

The ledger is machine-wide rather than per-project, and a devkit session works it down
with `/triage-harness` — which is why a report is worth filing from a repo that cannot fix
the thing it is about, and why "I mentioned it in chat" is the one outcome that leaves the
defect exactly where it was.

When the defect is in the **vendored harness** rather than in prose, run
`python scripts/sync-devkit.py --check` first and put its answer, with `DEVKIT_VERSION`, in
the report: this copy is routinely weeks of fixes behind devkit, and why that decides
whether a report can be triaged at all is in
[`.claude/engineering-evidence.md`](../engineering-evidence.md). An old copy is still worth
reporting once you know that is what it is — never a reason to route around a hook.
