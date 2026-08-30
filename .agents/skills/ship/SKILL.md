---
name: ship
description: 'Ship the completed task branch: verify it, commit the intended diff, push it, and open or reuse its GitHub pull request.'
argument-hint: 'Optional PR title, or context such as which box/worktree to ship from'
---

# Ship the current task

> **Three of the commands below are on Claude Code's Bash blocklist** -- `git status`,
> a raw `git diff` and an uncounted `git log` all grow with the repo, so
> `scripts/hooks/enforce-capped-bash.py` blocks those issued bare. Route each through
> `python3 scripts/hooks/invoke-capped.py --command "<the command>"`, which keeps a head
> *and* a tail window and preserves the exit code. Pass no `--max-bytes`: it defaults to
> this project's `[bash] max_bytes`, and a number written here would be one project's
> value baked into a file every project vendors byte-for-byte.
>
> **`git commit` and `gh pr create` are not on that list and must be issued bare.** Their
> message is authored and multi-line, and it does not survive the wrapper's `cmd.exe`;
> the `| head -c N` fallback masks the exit code, so a commit a pre-commit hook rejected
> would read as a success. Nothing else in this skill needs a wrapper at all.
>
> **Codex runs the numbered commands directly.** Its shell runner already caps captured
> output, and `scripts/sync-codex-hooks.py` omits the redundant gate. Adding the wrapper
> in Codex only adds transcript noise and can change shell semantics.
>
> **The two that carry an authored message are the exception — issue `git commit` and
> `gh pr create` bare.** The gate exempts them, and wrapping one destroys it: a
> multi-line message does not survive `cmd.exe`, and the `| head -c N` fallback masks
> the exit code, so a commit a pre-commit hook rejected reports success and step 3
> pushes a branch with nothing on it.
>
> **Pass those two their text from a file — `git commit -F <path>` and `gh pr create
> --body-file <path>` — never inline in double quotes.** A message worth writing about
> this codebase names identifiers, and a Markdown body names them in backticks; inside
> double quotes those are command substitution, which the shell really would expand, so
> the gate is right to refuse the call. `-m "…"` and `--body "…"` therefore fail on
> exactly the messages worth writing, and the block message names the cap rather than
> the backtick, so the cause is invisible. Single quotes escape it for a one-liner; a
> file is what survives a multi-paragraph body.

Run each step in order. Stop on failure; never open a PR for an unverified branch.

1. Run `python scripts/ship.py --preflight`. It must report a namespaced task branch
   and the repository's detected default branch. The namespace is agent-neutral, so
   branches such as `agent/...`, `claude/...`, and `codex/...` are all valid.
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
5. Report the PR number and URL.

Do not enable auto-merge, wait on CI, or start an autofix loop unless the user asks.

## Do not clean up after yourself

Shipping ends at the PR. If this branch is in an ephemeral box, **leave the box
alone** — do not reap it, do not delete the branch, do not stop its stack.

`worktree.py reconcile` owns that, and it waits for the PR to actually merge before
destroying anything. Reaping here would do it on the strength of the push instead,
which is the one moment the work exists only locally if the PR was never created.
