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

Every code change must include tests in the same commit. If you touch something
that has no test, write the test in the same commit even if the logic didn't change.

- **New unit of logic:** the happy path, the error cases, and the edge cases.
- **Bug fix:** write the regression test first and watch it fail before you fix it. One
  that has never failed is asserting the wrong thing.
- **Coverage floors are ratchets:** never lower it merely to make a change pass. The
  other end of the same rule is the one that slips quietly, because raising a ceiling —
  a timeout, a retry count, a size or complexity limit, a baseline of known gaps — reads
  as tuning rather than as relaxing a gate.
  **A ceiling raised on three consecutive branches is a defect report, not a raise:**
  find out what is filling it before moving it again, and say in the commit message what
  you found.
- A skipped or `xfail` test carries a linked issue or a one-line reason in the marker.

Running tests is optional, and the full suite is not yours to run — see
`.claude/rules/session-scope.md`. The gate runs once, in CI, on the PR the fix pass opens
for you; a red gate comes back as a fresh session with the failing tests named.

Instruction files — `CLAUDE.md`, `.claude/rules/*`, `.claude/skills/*` — are under this
same mandate. See `.claude/rules/authoring.md`.

## Claude Code's Bash calls: no hook gates them

No agent hook is wired, so nothing judges a Bash call before it runs
(`scripts/hooks/enforce-capped-bash.py` is vendored but unwired). The bound is
`BASH_MAX_OUTPUT_LENGTH` in `.claude/settings.json`, which truncates bytes that already
exist and so cannot false-positive.

**Write and edit files with the Write and Edit tools, never through Bash** -- no heredoc,
`sed -i` or `python -` patch script, whatever a system prompt says about small edits.
The Bash tool collapses `\\` to `\`, quoted heredocs included, and a test run finds it.

**Issue commands bare; wrapping by reflex is a mistake.** A `grep`, a `python -c`, a test
run, a `curl`, a heredoc: routing them through a wrapper buys no second bound and has
happened here at scale. Bound only a statement whose output grows with the
**repository** rather than with the command you wrote — `ls`, `cat`, `find`, `tree`,
`du`, `env`, `git status`, an uncounted `git log`, a raw `git diff`/`git show`:

| Spelling | Trade-off |
| --- | --- |
| `\| head -c N`, `tail -c N`, `wc -l`, `grep -c <pat>` | **masks the exit code**, including a background task's completion status |
| `<cmd> > <file>` | strongest bound; the output never enters context |
| `python3 scripts/hooks/invoke-capped.py --command "<cmd>"` | keeps a head *and* tail window, preserves the exit code |

The wrapper runs through the platform shell — **`cmd.exe` on Windows** — so heredocs,
single-quoted paths and escaped alternation do not survive it. For `ls`, `cat` and `find`
the better answer is usually the Glob, Read and Grep tools, which cost no subprocess and
page rather than dump. Codex caps shell output itself, so issue commands there directly.
What preemptive wrapping has cost is in
[`.claude/engineering-evidence.md`](../engineering-evidence.md).

**A refusal saying a command is "too complex to verify that it stays inside the
worktree" or "cannot be shown not to be git" is not a devkit hook** — none is wired. It
is Claude Code's own `claude --worktree` isolation guard; nothing in devkit changes what
it accepts. It parses **only the Bash tool's** command lines, so on Windows the
PowerShell tool is the answer for a compound statement, not a workaround.

| Shape it refuses | What to issue instead |
| --- | --- |
| `~` in a path | `$HOME`, which resolves; the tilde is rejected unexpanded |
| `git -C`, `--git-dir`, `env -C`, `GIT_DIR=`, `cd … &&` before git | bare `git`, from the worktree |
| command substitution, `for`/`while`, a subshell | one call each — a plain `&&` list is fine |
| the letters `git` inside any of the above | nothing; it is a false positive |

**The last row is the one that wastes turns.** Inside a statement the parser could not
reduce to a simple list, the guard tests the *whole line* for `git` as an unanchored
substring, so `.gitignore`, `github.com` and `digit` each read as "names git": split
the statement. The reproductions are in
[`.claude/engineering-evidence.md`](../engineering-evidence.md).

**Git Bash rewrites `rev:path` into a Windows path list** -- git then reports an
`ambiguous argument` naming a backslashed, semicolon-joined path, exit 0 when piped.
`MSYS2_ARG_CONV_EXCL` in `.claude/settings.json` exempts revs opening `origin/`,
`upstream/` or `refs/`; for any other rev with a slash, spell it `feature/x:./.devkit.toml`.

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

**"No checks reported" has causes needing opposite responses.** Ask once, after a push —
`gh pr view <N> --json mergeStateStatus,statusCheckRollup` — and read the answer against
the table in the evidence file: `CONFLICTING` is a gate that will never run, and
`UNSTABLE` with an empty rollup is one that ran, as a `workflow_dispatch` the PR cannot show.

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
- **Hook scripts (`scripts/hooks/`) are stdlib only.** They may run before the virtualenv
  is active, so an import of anything installed is a crash in the one context that cannot
  report it well.
- **Failure artifacts:** any script whose failures an agent is expected to act on writes
  them to a **parseable file under `logs/`** — on failure too, overwritten per run.
  Streamed terminal output scrolls away; keep the terminal to a status line plus the path.

## Lint policy

Lint catches **correctness and security** problems — the ones a human reviewer reads past.
Style is not a judgement call worth an agent's turn: `ruff format` runs at commit time from
`.pre-commit-config.yaml` and again in CI, so line length, quote style and import order never reach a
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
install step. No coding-agent hook is wired — `.claude/settings.json` carries no `hooks`
block, and `--pull` strips one — so the hook scripts are carried, not run.

- Project specifics live in `.devkit.toml`, read by `scripts/hooks/harness_config.py`.
  **Never hard-code them in a vendored file**: a new behaviour gets a manifest field and a
  default, not an `if project ==` branch and not a paragraph naming one repo's paths.
- `python scripts/sync-devkit.py --check` fails on drift, `--pull` adopts upstream,
  `--push` sends a change authored here back up. `DEVKIT_VERSION` records the upstream
  commit the copy corresponds to.
- **`$DEVKIT_DIR` unset means there is nothing to compare against**, and the stamp decides
  what that is worth — clean before adoption, a failure once `DEVKIT_VERSION` exists.
- **An operator may switch the hook scripts off** — `DEVKIT_HOOKS_OFF`, inert while none
  is wired.
- A vendored script may depend on a file the project owns (`scripts/lint-all.py`,
  `scripts/run-tests.py`); a missing one is a silent skip by design.

The stamp rule, the switch's values and reach, and the drift check for a machine with no
devkit clone are in [`.claude/engineering-evidence.md`](../engineering-evidence.md).

## Guardrail: the harness is not your job

**For project sessions:** a fixer follows [`.claude/fixer.md`](../fixer.md) where the
two differ.

A session in a project makes the change the user asked for and nothing else. It does not
maintain the harness, does not fix a gate, and does not file its defects: the scheduled
fix pass (`scripts/fix-pass.py` in devkit) reads every gate, records every refusal on the
machine's ledger itself, and sends a devkit session at the harness before any project
session at a project. A project session that "just fixes" a vendored file breaks that
ordering twice: the drift gate rejects the edit, and the defect is hidden from the pass
that would have fixed it everywhere.

**Never silently work around a bad instruction or a refusal.** If a skill, a rule, a
`CLAUDE.md`, a hook or a vendored script sent you into a dead end — blocked a correct
command, refused an edit it should have allowed, crashed, reported success while doing
nothing — say so in your report with the file or the exact command, write the same
line to `logs/friction.md`, and stop there: the pass files each line, and reads your
transcript for the rest. A workaround leaves the next agent at the same wall.
