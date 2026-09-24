# Evidence behind `.claude/rules/engineering.md`

The measurements and the incidents that produced the policies in
[`rules/engineering.md`](rules/engineering.md). It is a **reference file, not a rule**:
nothing loads it automatically, and each policy section over there carries a one-line
pointer to the heading here that explains it.

It lives outside `rules/` deliberately. Every `.md` under `.claude/rules/` is loaded as
a rule — unscoped ones on **every API call of every session** — and this is exactly the
prose that should be paid for only when someone goes looking for it. Same argument as
[`scripts/windowless-jobs.md`](../scripts/windowless-jobs.md) and
[`scripts/worktree-guard.md`](../scripts/worktree-guard.md).

This file is vendored (it is in `sync-devkit.py`'s `MANIFEST`), because the pointers
that reach it are vendored too. A local edit here is drift.

## Which side of the split each rule family falls on

The policy is correctness and security on, anything a formatter can decide off. In
practice that means **on** for undefined names, unreachable code, shadowed builtins and
mutable default arguments; for injection sinks, unsafe deserialisation and hard-coded
secrets; and for unclosed files and bare `except`. **Off** for line length, quote style
and import order, which `ruff format` settles before a review ever sees them.

## Rule families are how cosmetic rules get in

**Adding a family prefix to `select` enables every member, including the cosmetic
ones.** `"E"` is not one rule; it is nineteen, and `E501` (line-too-long) is one of
them. Nobody in this workspace ever decided to cap line length — `select = ["E", "F",
"I", "UP"]` was added once, E501 came along, and the same commit already carried two
`per-file-ignores` entries turning it back off. It spread to every generated project
from there and was suppressed one directory at a time for years.

So, when adding a family: **read its members and ignore the cosmetic ones in the same
change.** A rule already exempted in two or three directories is not a rule anyone
wants — that is the signal it should be off globally, not exempted a fourth time.

Currently off by this policy, and they are not to be re-enabled without a reason that
names a defect they would catch:

| Selector | What it enforces |
| --- | --- |
| `I` | import ordering |
| `UP` | preferred modern syntax |
| `SIM` | readability rewrites |
| `N` | naming conventions |
| `T20` | stray `print()` calls |
| `E101 E401 E501 E701 E702 E703 E731 E741 E742 E743` | the cosmetic members of `E` |

`E402`, `E711`–`E714`, `E721`, `E722`, `E902` and `E999` stay on: those catch real
defects. So do `F`, `B`, `ASYNC`, `S` and `RUF`.

`line-length` is a **formatter** setting and stays. Dropping E501 does not stop code
being wrapped; it stops the wrapping being a commit failure.

Two things make this stick rather than drift back:

- devkit's `test_generated_projects_do_not_enforce_cosmetic_rules` fails if a newly
  generated project would enforce any of the above. It tests *reachability*, because
  dropping a family from `select` and listing a code in `ignore` are equally effective
  and a check on one spelling would miss the other.
- Selectors do not span linters. `S` is flake8-bandit and does **not** select `SIM108`
  from flake8-simplify; only the numeric part matches as a prefix, which is why `E5`
  covers `E501`. Assume otherwise and you will disable, or fail to disable, the wrong
  set.

Turning these off is a deliberate deletion of obsolete checks, which the closing
paragraph of *When a linter is wrong* in [`rules/engineering.md`](rules/engineering.md)
permits explicitly. It is **not** licence to skip a failing check: everything still
enabled gets fixed or reported, never ignored.

## What polling a CI gate cost

The expensive part of waiting is not the `gh` command — it is that **every poll is a
full API round trip that re-sends the whole conversation**. Measured over ~16k API calls
in the workspace this rule was written for (2026-08): 307 polling calls burned 36M
billed input tokens, ~2.5% of all spend, at an average context of 117k tokens per poll.
Polls land at the *end* of a session, where context is largest, so they are the most
expensive place a call can go — one late poll cost more than five whole sessions did.

### devkit#180: "no checks reported" was a gate that would never start

GitHub builds a `pull_request` run against the *merge* ref, and a PR that has gone
`CONFLICTING` has no merge ref to build — so no workflow is queued for the head commit
at all, and `--watch` waits on something nothing will ever produce. The PR page does not
read that way: it keeps showing the last green gate, from an **older** commit, so
devkit#180 sat for hours with zero check runs on its head commit and read as gated and
passing.

### devkit#222: the same message, from a gate that had not started *yet*

For the first seconds after a push, GitHub has accepted the commit and not yet attached
a run to it, so `gh pr checks --watch` finds nothing to watch and **returns immediately**
saying "no checks reported" — on a healthy PR, with nothing wrong and nothing to fix. It
happened twice on devkit#222 and cost an extra `gh pr view` and a second watch each
time, because the paragraph documenting devkit#180 framed that message as the
`CONFLICTING` case alone.

Tell them apart by **how long the call took, not by what it said**: a `--watch` that
returns in about a second never waited for anything. A `CONFLICTING` PR gives the same
message and does not get better on a retry — which is what asking `mergeStateStatus`
once, at the front, settles before the ambiguity can arise.

## The worktree isolation guard: what it actually reads

Read out of the installed Claude Code binary and reproduced by hand in a worktree
session. The guard is one function, reached only when the session has an isolation root,
and the call site selects it on the shell: the PowerShell tool reaches the
working-directory check that sits above it and nothing else.

Two spellings of one path, one session, the same target directory:

```
git -C ~/…/eager-kindling-russell rev-parse       → refused, "computed at runtime"
git -C "$HOME/…/eager-kindling-russell" rev-parse → C:/Users/…/eager-kindling-russell
```

The tilde is never expanded before the check — the path predicate rejects any string
containing `~` outright, alongside `/proc/self`, `/dev/fd` and a leading `/../`.

The substring match is the expensive one. A statement the parser cannot reduce to a
simple list is tested against `/git/i` over the entire line, unanchored:

```
for f in a b; do echo "$f"; done && echo done-ok     → runs
for f in a b; do echo "$f"; done && echo legitimate  → "names git in a form too complex"
```

Both ran seconds apart in the same session. **The earlier wording of this rule was wrong
on two of its three examples** — it named a heredoc and a `$HOME` argument as refusals in
their own right, and each of them runs. Only the `cd … &&` example held up, and the guard
has a branch for exactly that shape. The two wrong ones pointed the wrong way: an agent
reading them avoids `$HOME` for the tilde, which is the one spelling that is actually
refused. Nothing gates a sentence like that, which is why it survived until a session
ran the commands instead of believing it.

## Why capped Bash is a blocklist, not a proof obligation

`enforce-capped-bash.py` is vendored but no longer wired as a hook; this is the record
of why its design is what it is, should it ever be wired again.

**This gate used to work the other way round**, and the reversal is worth knowing
because the old design is the intuitive one. It required every call to *prove* it was
bounded and blocked whatever it could not recognise — which means modelling the shell,
and 46% of every block it ever issued turned out to be its own false positive rather
than a command anyone needed to change.

That is why a block on anything outside the closed list is a defect in the gate, to be
reported with the exact command rather than worked around, and why a correct command is
never rewritten to satisfy it.

The mirror-image waste is preemptive wrapping, which the gate cannot teach anyone out of
because it never fires on it: 42% of one month's Bash calls in this workspace carried a
wrapper they did not need, paying visible indirection for no second bound.

## `$DEVKIT_DIR` unset: why the stamp decides

Before adoption every mode of `sync-devkit.py` no-ops clean (exit 0): nothing is
vendored, so the gate has nothing to miss. Once `DEVKIT_VERSION` exists, the same
silence would report a comparison that never ran, so it **fails** instead.

`$DEVKIT_DIR` is a property of the machine and `DEVKIT_VERSION` is committed, which is
what makes the distinction reliable: a second workstation, a fresh clone or a CI job
missing its `env:` block is where the gate would otherwise go quiet.

On a machine with no devkit clone at all, the drift check that still works is
`pre-commit run devkit-drift --all-files` — the same comparison, made against the rev
pinned in `.pre-commit-config.yaml` rather than against a checkout.

## Reporting a *harness* defect: name the copy's age

The harness scripts are a **vendored copy**, and every consuming project is routinely weeks
of fixes behind devkit — so a refusal or a crash you hit there may already be fixed
upstream, and a report of one costs a human a relay and a false-positive triage. Spend
one command before reporting one, and put its answer in the report:

```bash
python scripts/sync-devkit.py --check     # clean = this copy matches upstream
```

A report that names `DEVKIT_VERSION` and what `--check` said can be triaged; one that
does not cannot. An old copy is still worth reporting once you know that is what it is —
this is **not** a reason to route around the defect.

## Why a vendored file may depend on one the project owns

`lint-all.py` and `run-tests.py` are the project's, not devkit's, and vendored scripts
call them. Those dependencies are asserted by
`scripts/hooks/tests/test_repo_contract.py`, because at runtime a missing one is a
silent skip by design — the gate reports green having run nothing.

## Switching the harness off: `DEVKIT_HOOKS_OFF`

**No coding-agent hook is wired** — not in devkit, not in a generated project, and
`sync-devkit.py --pull` strips a consumer's `hooks` block — so today this switch has
nothing live to stand down. The hook scripts that consult it are still vendored, and
the switch is kept so that re-wiring one would come back switchable. It is read from the
**environment** so one line reaches every project, and deleting the line restores them.

| Value | Effect |
| --- | --- |
| unset, or `0`/`false`/`no`/`off` | every hook runs — the default, and the only thing a value that reads as "off" to a human may mean |
| `1`, `all`, `*`, `true`, `yes`, `on` | every switchable hook stands down |
| `stop`, `lint-fix`, `capped-bash`, `session-start`, `branch-tier` | that hook only; comma- or semicolon-separated for several |

`SWITCHABLE_HOOKS` in `harness_config.py` is the list this table copies, and the one to
believe when the two disagree. A name in it that no hook script reads would be a value
an operator can set and watch do nothing, so
`test_every_switchable_hook_is_a_hook_that_actually_consults_the_switch` searches the
hook sources for each. Shell callers ask through `harness_config.py --hook-off <name>`
rather than re-reading the variable, so the aliases and the off-values asymmetry have
one owner.

`branch-tier` covers both `worktree-guard.py` and `task_slug.py`: the slug exists to name
the box the guard cuts. With neither wired, a box is cut on purpose — `claude
--worktree`, `/ship`, or `agent-box.py spawn`/`ship` behind the `Agent:` workspace tasks.
`test_the_branch_tier_consults_the_harness_kill_switch` and
`test_a_stood_down_branch_tier_returns_before_it_reads_stdin` in
`tests/test_worktree_guard.py` keep the scripts honouring it.

CI is unaffected either way: the PR gate runs the same checks server-side.

The vendored suite clears the variable in an autouse fixture, on the same reasoning as
the ledger fixture beside it: the switch lives in the environment of every agent session
on a machine where it is set, so a suite that inherits it is one whose end-to-end hook
tests pass by agreeing that nothing happened.
