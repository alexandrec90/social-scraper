---
description: Baseline engineering policy shared by every devkit project — test coverage, script conventions, the vendored harness seam, and the instruction-feedback loop
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
- **Coverage floors are ratchets:** never lower it merely to make a change pass.
- **Run targeted tests** — the module you touched — plus the linter. Leave full-suite runs
  to CI; a fresh-venv full run surfaces version skew unrelated to your change.
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

**"No checks reported" has two causes needing opposite responses**: a gate that has not
started *yet*, and one that never will, because a `CONFLICTING` PR has no merge ref to
build against. Ask once, after a push — `gh pr view <N> --json
mergeStateStatus,statusCheckRollup`. `CONFLICTING` means merge `origin/<default>` and
push. `BLOCKED`/`UNSTABLE`/`CLEAN` mean the run exists and `--watch` is right. `UNKNOWN` is
the ordinary answer in the seconds after a push and says nothing either way. If you get the
message anyway, tell the two apart by **how long the call took, not what it said**: a
`--watch` back in about a second never waited, so re-issue it once.

When the gate will outlast anything useful you could do meanwhile, stop: report that the
branch is pushed and the gate is running, and let the result arrive in a fresh session.
What polling has cost is in
[`.claude/engineering-evidence.md`](../engineering-evidence.md).

## Scripts

All scripts under `scripts/` are Python — a local desktop and a CI runner are rarely the
same OS.

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
Style is not a judgement call worth an agent's turn: `ruff format` runs on every edit via
the `lint-fix.py` PostToolUse hook and again in CI, so line length, quote style and import
order never reach a review. **On** for correctness, security and resource-handling; **off**
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

## Guardrail: the instruction-file feedback loop

If an instruction in a skill, a rule or a `CLAUDE.md` sent you into a dead end or a wasted
operation — or a mistake you made would have been prevented by one that isn't there — flag
it in your report with the file, the line, and a proposed edit.

**Never silently work around a bad instruction.** That fixes your turn and leaves the next
agent at the same wall; these files only improve if the failures they cause are reported as
defects in them.

Give the flag a durable copy too — it complements the flag in your reply rather than
replacing it, and exits 0 on a machine with no `$DEVKIT_DIR`:

```bash
python scripts/hooks/report-harness-defect.py --message "<what went wrong>" --command "<the exact command, when one triggered it>"
```

When the defect is in the **vendored harness** rather than in prose, run
`python scripts/sync-devkit.py --check` first and put its answer, with `DEVKIT_VERSION`, in
the report: this copy is routinely weeks of fixes behind devkit, and why that decides
whether a report can be triaged at all is in
[`.claude/engineering-evidence.md`](../engineering-evidence.md). An old copy is still worth
reporting once you know that is what it is — never a reason to route around a hook.
