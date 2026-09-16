---
name: ship
description: 'Ship the completed task branch: verify it, commit the intended diff, push it, and open or reuse its GitHub pull request.'
argument-hint: 'Optional PR title, or context such as which box/worktree to ship from'
disable-model-invocation: true
---

# Ship the current task

> **Pass an authored message from a file — `git commit -F <path>` and `gh pr create
> --body-file <path>` — never inline in double quotes.** This holds everywhere and has
> nothing to do with any hook: a message worth writing about this codebase names
> identifiers, and a Markdown body names them in backticks, which inside double quotes is
> command substitution the shell really does expand. `-m "…"` and `--body "…"` therefore
> fail on exactly the messages worth writing.
>
> **Where `scripts/hooks/enforce-capped-bash.py` is running, three commands below are on
> its blocklist** — `git status`, a raw `git diff` and an uncounted `git log` — because
> each grows with the repo. Route those three through `python3
> scripts/hooks/invoke-capped.py --command "<the command>"`, which keeps a head *and* a
> tail window and preserves the exit code; pass no `--max-bytes`, so it takes the
> project's `[bash] max_bytes` rather than baking one project's number into a vendored
> file. **Nothing else here needs a wrapper**, and `git commit` and `gh pr create` must
> never get one: the gate exempts them, and their multi-line message does not survive the
> wrapper's `cmd.exe`.
>
> Two cases where that paragraph does not apply at all, and wrapping only adds noise:
> **Codex**, whose shell runner caps output itself so `scripts/sync-codex-hooks.py` omits
> the gate; and **any machine with the harness switched off** (`DEVKIT_HOOKS_OFF`), where
> the hook exits without deciding anything. Issue the commands bare in both.

Run each step in order. Stop on failure; never open a PR for an unverified branch.

1. Run `python scripts/ship.py --preflight`. It must report a namespaced task branch
   and the repository's detected default branch. The namespace is agent-neutral, so
   `agent/...`, `claude/...` and `codex/...` are all valid, as is the `worktree-<topic>`
   spelling `claude --worktree` cuts and cannot be asked to change. **It also names
   what this checkout is missing** — no `.venv`, no `node_modules` — with the command
   that installs it. Run that command now, before anything is committed. A linked
   worktree checks out tracked files only, and the commit-time pre-commit gate and
   step 3's lint gate both run from that toolchain: a `language: system` hook resolves
   its entry point against `PATH`, which in a fresh worktree has no venv on it, so the
   gate refuses the commit with `Executable '...' not found`. Installing the one tool
   it named by hand and putting it on `PATH` gets that commit through and leaves the
   next gate to fail the same way; the named command is the whole fix.
2. Review the change. Get the file list from `git status --short`, then read the
   changes with the Read tool rather than paging a capped `git diff` — a cap drops the
   middle of a large diff, which is the one part a truncated read hides from you. Run
   the targeted tests for the changed behavior — and when a `scripts/` file changed,
   that is **two test trees, not one**. `run-tests.py` takes its scope from
   `testpaths`, the project's own suite; the vendored `scripts/hooks/tests/` sits
   outside it by design and runs as its own step, so a green `run-tests.py` says
   nothing about it. Run `pytest scripts/hooks/tests/` too: it applies gates a project
   suite does not, such as requiring every public symbol in a script to be named by a
   test. Skipping it does not get you past that gate — it moves the failure to the
   Stop hook or CI, after the push. Stage only the intended files, then
   commit with an imperative subject and a body explaining why, written to a file and
   passed with `git commit -F`. The skill argument, when supplied, is only the subject
   when it *reads* as one — imperative, about the change. An argument that names
   context instead (a box path, a worktree, task notes) scopes where and what to
   ship; author the subject from the change as usual. This clause used to say "use
   the argument as the subject" unconditionally, which turned a box path passed as
   context into the commit's headline.
3. Run `python scripts/ship.py`. This requires a clean tree, runs the changed-scope
   lint gate, and pushes the current branch with retry handling. Fix any failure and
   rerun this step.
4. Run `gh pr view --json number,url,state` to find an existing PR for the branch.
   Reuse it when present. Otherwise inspect the repository's PR template and run
   `gh pr create` with the detected base branch, current branch, commit subject (or
   the argument, when it reads as a title), and a concise body covering the change and
   verification — written to a file and passed with `--body-file`.
5. Apply the `automerge` label: `gh pr edit <number> --add-label automerge`. When that
   fails because the repository has no such label, create it once and retry —
   `gh label create automerge --color 0e8a16 --description "PR the harness may merge once the gate passes"`,
   the spelling `sweep.AUTOMERGE_LABEL` and `dependabot-automerge.yml` both use. A
   failure here is never fatal to the ship: report it and leave the PR labelled by hand.
6. Report the PR number and URL, and **stop**.

## Shipping ends at the push — the gate is somebody else's turn

**Never wait for the PR to go green, and never poll it.** Watching a gate is the most
expensive thing a session can do with its remaining context: it happens at the tail of a
long conversation, where every turn re-sends the whole transcript, and it produces
nothing a later session could not read in a single call. A gate also routinely outruns
the Bash tool's ceiling, so even the "one blocking call" spelling
`.claude/rules/engineering.md` prescribes for a deliberate wait decays into a poll loop
with the timeout as its interval.

Step 5 is what makes waiting unnecessary rather than merely forbidden. `worktree.py
reconcile` runs on a schedule, squash-merges any green PR carrying the label, and reaps
its box afterwards — so delivery is already automatic by the time you report, and sitting
on the PR does not make it more so. **Applying the label is the review decision**: it
says this branch may land on the strength of its gate, which is what the harness's own
task branches are for. A change that wants a human's eyes instead is one to say so about
in the report, having left the label off.

So: no `gh pr checks --watch`, no repeated `gh pr view`, no autofix loop, and no "let me
just confirm it passed". Report that the branch is pushed and the gate is running. If it
fails, the run is on the PR, the label merges nothing, and a fresh session fixes it at
the session floor rather than at the tail of this one.

Two things this does not forbid: **diagnosing a failure you were asked to fix** is the
work itself rather than waiting, and the single `gh pr view` in step 4. The waste begins
at the second identical poll.

Do not start an autofix loop, or reach for `gh pr merge`, unless the user asks. The
scheduled pass is the only thing that merges.

## Do not clean up after yourself

Shipping ends at the PR. If this branch is in an ephemeral box, **leave the box
alone** — do not reap it, do not delete the branch, do not stop its stack.

`worktree.py reconcile` owns that, and it waits for the PR to actually merge before
destroying anything. Reaping here would do it on the strength of the push instead,
which is the one moment the work exists only locally if the PR was never created.
