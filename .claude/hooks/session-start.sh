#!/bin/bash
# SessionStart hook — provisions the sandbox so `scripts/lint-all.py` and the
# pytest suites are runnable from turn one. Without this, a Claude Code on the
# web session boots without the pinned toolchain (no ruff/mypy/pytest, no
# frontend node_modules), so the full lint suite can only run in CI — surfacing
# lint drift one slow gate round at a time instead of in a single local pass.
#
# Synchronous + idempotent. The *installing* half is remote-only, and container state
# is cached after this completes, so re-runs are cheap (venv reused, pip/npm no-op when
# satisfied). A local session installs nothing but reports what is missing --
# `report_missing_toolchain` carries why that line is not the same as the ladder below.
set -uo pipefail

# --- The pre-commit gate, wired for BOTH session kinds -------------------------
# `.git/hooks/` is not committed, so a fresh clone has the config file and none of the
# hooks it describes — the gate silently does not exist until someone runs
# `pre-commit install`. That is a property of *cloning*, not of running in a sandbox,
# and this used to sit below the local `exit 0`: the one shape it never reached was a
# clone on a developer's machine, which is the common one. A consuming project cloned
# onto a second machine therefore committed ungated, with nothing anywhere to say so —
# `.pre-commit-config.yaml` is committed and looks like the gate, and CI runs the same
# checks in a job, so both ends report healthy while the commit-time half is absent.
#
# Detection, not configuration, like everything else here: the config on disk is the
# signal, so a project without one skips this entirely. Installing the hook does NOT run
# it — nothing is checked until a commit is made.
#
# `$1` is `warm` to build the hook environments too. A cold sandbox pays that once, up
# front, because it is about to run a gate; a local session gets the plain install, which
# only writes `.git/hooks/pre-commit` and costs milliseconds at every single start.
wire_pre_commit() {
  [ -f .pre-commit-config.yaml ] || return 0
  global_hooks_path="$(git config --global --get core.hooksPath 2>/dev/null)"
  if [ -n "$global_hooks_path" ] && [ -f "$global_hooks_path/devkit_git_policy.py" ]; then
    # The global dispatcher invokes `pre-commit run` itself after its branch policy
    # passes. Installing here would target core.hooksPath and ask pre-commit to
    # overwrite that dispatcher, disabling the policy for every repository.
    echo "[session-start] Using the global Devkit dispatcher for pre-commit."
    return 0
  fi
  # `Scripts/` as well as `bin/`: the local branch runs on whatever the developer has,
  # and on Windows a venv's console scripts land in `Scripts/`. The remote sandbox is
  # Linux, which is why `bin/` alone was enough while this was remote-only.
  if [ -x ./.venv/bin/pre-commit ]; then
    precommit="./.venv/bin/pre-commit"
  elif [ -x ./.venv/Scripts/pre-commit.exe ]; then
    precommit="./.venv/Scripts/pre-commit.exe"
  elif command -v pre-commit >/dev/null 2>&1; then
    precommit="pre-commit"
  else
    echo "[session-start] pre-commit not installed — skipping git hook wiring"
    return 0
  fi
  echo "[session-start] Installing the pre-commit git hook..."
  if [ "${1:-}" = "warm" ]; then
    "$precommit" install --install-hooks >/dev/null 2>&1 \
      || echo "[session-start] WARN: pre-commit install failed — commits will not be gated"
  else
    "$precommit" install >/dev/null 2>&1 \
      || echo "[session-start] WARN: pre-commit install failed — commits will not be gated"
  fi
}

# --- The toolchain a LOCAL session is missing ---------------------------------
# This used to be a comment asserting that local machines already have the venv and
# node_modules. That holds for a checkout someone set up months ago and is false for the
# one shape where it matters — a FRESH CLONE, which is precisely the machine that has
# neither. A session opened in one has no ruff, no mypy and no pytest, and finds that out
# one failed command at a time, each looking like a broken tool rather than an empty
# checkout. Same property as the pre-commit gate above: it is a consequence of *cloning*,
# not of running in a sandbox.
#
# It reports and does not install, which is a deliberate line rather than a smaller fix:
#
#   - SessionStart is SYNCHRONOUS. A cold `uv sync` is minutes, charged to the start of a
#     session that may have been opened to read one file.
#   - The install ladder below resolves `./.venv/bin/python` — the Linux sandbox it was
#     written for. `wire_pre_commit` above already carries the Windows correction
#     (`Scripts/`), learned the hard way, and a second copy of that lesson is how the two
#     drift apart.
#   - Where ephemeral worktrees are in use, they already have an owner for this: devkit's
#     own `worktree.py provision` walks the same ladder cross-platform and the worktree
#     guard names it in the message that blocked the edit. A directory with two
#     provisioners is worse off than one with none.
#
# So it names the state and the command, in the `(fix: ...)` shape the rest of this
# workspace's session-start output uses. Detection, like everything else here: the
# manifest's install_command wins, then the lockfile on disk decides.
report_missing_toolchain() {
  # `local` throughout: the remote branch below assigns `frontend_dir`/`frontend_enabled`
  # for its own purposes, and a vendored file gets edited by people who will not have read
  # both halves.
  local fix="" install_command locks frontend_enabled frontend_dir
  if [ ! -d .venv ]; then
    install_command="$(python3 scripts/hooks/harness_config.py python.install_command 2>/dev/null)"
    if [ -n "$install_command" ]; then
      fix="$install_command"
    elif [ -f uv.lock ]; then
      fix="uv sync --all-extras --all-groups"
    elif [ -f requirements-dev.txt ]; then
      locks="-r requirements-dev.txt"
      [ -f requirements.txt ] && locks="-r requirements.txt $locks"
      fix="python -m venv .venv && uv pip install $locks"
    elif [ -f pyproject.toml ]; then
      fix="python -m venv .venv && uv pip install -e '.[dev]'"
    fi
    [ -n "$fix" ] &&
      echo "[session-start] No .venv here — ruff/mypy/pytest are unavailable (fix: $fix)"
  fi
  # node_modules is the other half of the claim this replaced, and a frontend project
  # missing it fails lint-all.py the same way.
  frontend_enabled="$(python3 scripts/hooks/harness_config.py frontend.enabled 2>/dev/null)"
  frontend_dir="$(python3 scripts/hooks/harness_config.py frontend.dir 2>/dev/null)"
  frontend_dir="${frontend_dir:-frontend}"
  if [ "$frontend_enabled" = "true" ] && [ -d "$frontend_dir" ] &&
    [ ! -d "$frontend_dir/node_modules" ]; then
    echo "[session-start] No $frontend_dir/node_modules — the frontend linters are unavailable (fix: npm install --prefix $frontend_dir)"
  fi
}

# --- LOCAL sessions only: keep this branch current with origin/master ---------
# Parallel worktrees drift from master the longer their branches live; on a
# local session this rebases the checked-out branch onto origin/master so it
# begins current, with no manual command. scripts/hooks/session-sync.py refuses
# a dirty tree, disables autostash, and re-signs (--gpg-sign) so replayed commits
# stay Verified.
#
# This MUST NOT run in cloud/remote (Claude Code on the web) sessions: there the
# platform owns the branch lifecycle (branch, commit, signing, PR), and rebasing
# from inside rewrites commit SHAs, diverges the branch from the remote the
# mobile app tracks, and strips signatures. So it lives inside the local branch
# of the remote guard, and the cloud-provisioning steps below never reach it.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  (
    cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0
    branch="$(git branch --show-current 2>/dev/null)"
    # Resolve the repo's default branch (main/master/...) rather than assuming one:
    # origin/HEAD (set at clone), else probe origin/main then origin/master.
    default_branch="$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null)"
    default_branch="${default_branch#origin/}"
    if [ -z "$default_branch" ]; then
      if git rev-parse --verify --quiet refs/remotes/origin/main >/dev/null 2>&1; then
        default_branch="main"
      else
        default_branch="master"
      fi
    fi
    # A rebase REWRITES COMMITS, so it must not touch a branch whose history other
    # people or other tools are already reading. On a branch with an open PR that means
    # every review comment is marked outdated and the next push has to be forced -- an
    # outward-facing consequence no prompt asked for, applied silently at session start.
    # The clean-tree guard alone never caught this: a pushed, reviewed branch is clean.
    #
    # `gh` able to answer -> ask the precise question (is there an OPEN PR?). Otherwise
    # fall back to the safe proxy: a branch that exists on origin is one where a rewrite
    # means a force-push. The two differ per machine on purpose -- guessing "no PR" when
    # we cannot check is the one answer with a blast radius.
    #
    # "Able to answer" is gated on `gh repo view`, not on `gh` merely being installed,
    # because `gh pr view` exits non-zero for BOTH "this branch has no PR" and "gh cannot
    # reach GitHub at all" (not authenticated, no GitHub remote, offline) -- and those two
    # need opposite defaults. Reading an unauthenticated gh's failure as "there is no PR"
    # is how a published branch gets silently rebased on a machine where gh is installed
    # but never logged in.
    protected=""
    gh_answered=false
    if [ -n "$branch" ] && [ "$branch" != "$default_branch" ]; then
      if command -v gh >/dev/null 2>&1 && gh repo view --json name >/dev/null 2>&1; then
        gh_answered=true
        pr_state="$(gh pr view "$branch" --json state --jq .state 2>/dev/null)"
        [ "$pr_state" = "OPEN" ] && protected="an open PR"
      fi
      if [ "$gh_answered" = false ] &&
        git rev-parse --verify --quiet "refs/remotes/origin/$branch" >/dev/null 2>&1; then
        protected="a published branch (gh could not check it for an open PR)"
      fi
    fi

    # Nothing to do on the default branch itself, or with detached HEAD.
    if [ -n "$protected" ]; then
      echo "[session-start] Skipping auto-sync: '$branch' has $protected — rebasing would rewrite pushed history. Sync manually if you intend to force-push."
    elif [ -n "$branch" ] && [ "$branch" != "$default_branch" ]; then
      echo "[session-start] Syncing '$branch' onto origin/$default_branch (no-op if tree is dirty)..."
      if ! python3 scripts/hooks/session-sync.py; then
        # On conflicts the rebase is left in progress. Auto-abort so the session
        # starts in a known-clean state; resolve conflicts manually instead.
        rebase_merge="$(git rev-parse --git-path rebase-merge 2>/dev/null)"
        rebase_apply="$(git rev-parse --git-path rebase-apply 2>/dev/null)"
        if [ -d "$rebase_merge" ] || [ -d "$rebase_apply" ]; then
          git rebase --abort 2>/dev/null
          echo "[session-start] origin/$default_branch had conflicting changes — auto-sync aborted, branch left untouched. Sync manually to resolve."
        else
          echo "[session-start] Branch sync skipped (dirty tree or offline) — sync manually when ready."
        fi
      fi
    fi
  ) || true
  # Nothing is installed locally — a static checkout is provisioned once, and a box by
  # `worktree.py provision`. But two things still have to happen on every machine:
  # `.git/hooks/` is not committed, so the commit-time gate has to be wired here (see
  # `wire_pre_commit`), and a checkout that has never been provisioned has to say so
  # rather than let the session discover it (see `report_missing_toolchain`).
  (
    cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0
    wire_pre_commit
    report_missing_toolchain
  ) || true
  exit 0
fi

# --- REMOTE (Claude Code on the web) sandbox provisioning only below ----------
cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0

# Install the Python toolchain into a project venv. A venv (rather than the
# system interpreter) avoids the Debian-managed-package conflict — pip refuses
# to upgrade distro-owned packages like PyYAML — and `scripts/lint-all.py`
# already prepends ./.venv/bin to PATH, so ruff/mypy/pytest resolve without any
# extra wiring. Match CI: runtime locks + dev linters together.
#
# uv does the heavy install — an order of magnitude faster than pip on a cold
# sandbox — bootstrapped by one small pip install.
#
# THIS FILE IS VENDORED BYTE-IDENTICAL INTO EVERY PROJECT, so it must not assume
# one dependency model. It previously read `requirements-dev.txt` unconditionally
# and expanded `${uv_version:?...}`; `:?` on an empty value *exits* a
# non-interactive shell, and `||` cannot catch a parameter-expansion failure. So
# in any project without pip-tools locks — every generated project, and
# ibkr_trader — provisioning died right here, before the PATH export below, and
# left an empty venv with no tooling. Only remote sandboxes were affected (local
# machines return above), which is why it went unnoticed.
#
# Detection, not configuration: the lockfile on disk is authoritative and cannot
# drift the way a manifest field can. `[python] install_command` in
# .devkit.toml overrides for a project that fits none of these shapes.
echo "[session-start] Installing Python toolchain into .venv (runtime + dev linters)..."
[ -d .venv ] || python3 -m venv .venv

# Bootstrap uv. When a pip-tools dev lock pins it, honour that pin so the
# installer version stays single-sourced; otherwise take the latest. Note the
# `:+` (substitute if set) rather than `:?` (die if unset) — that difference is
# the bug described above.
uv_pin="$(sed -nE 's/^uv==([^ ;]+).*/\1/p' requirements-dev.txt 2>/dev/null | head -n 1)"
./.venv/bin/python -m pip install --quiet --disable-pip-version-check \
  "uv${uv_pin:+==${uv_pin}}" \
  || echo "[session-start] WARN: uv bootstrap failed — dependency install may fail"
uv_run="./.venv/bin/python -m uv"

install_command="$(python3 scripts/hooks/harness_config.py python.install_command 2>/dev/null)"
if [ -n "$install_command" ]; then
  echo "[session-start] install: .devkit.toml install_command"
  sh -c "$install_command" \
    || echo "[session-start] WARN: install_command failed — ruff/mypy/pytest may be unavailable"
elif [ -f uv.lock ]; then
  # uv-native project: the lock pins everything, and uv manages ./.venv itself.
  # --all-extras --all-groups because the lint/test toolchain lives in extras or
  # dependency-groups depending on the project, and we need it either way.
  echo "[session-start] install: uv sync (uv.lock)"
  $uv_run sync --all-extras --all-groups \
    || echo "[session-start] WARN: uv sync failed — ruff/mypy/pytest may be unavailable"
elif [ -f requirements-dev.txt ]; then
  # pip-tools model: fully-pinned compiled locks, runtime + dev together.
  echo "[session-start] install: uv pip install (requirements locks)"
  $uv_run pip install --quiet --python ./.venv/bin/python \
    -r requirements.txt -r requirements-dev.txt \
    || echo "[session-start] WARN: uv install failed — ruff/mypy/pytest may be unavailable"
elif [ -f pyproject.toml ]; then
  # Unlocked pyproject: resolves fresh, so builds are not reproducible — but a
  # working toolchain beats no toolchain. Commit a lock to get out of this branch.
  echo "[session-start] install: uv pip install -e '.[dev]' (unlocked pyproject)"
  $uv_run pip install --quiet --python ./.venv/bin/python -e ".[dev]" \
    || echo "[session-start] WARN: editable install failed — ruff/mypy/pytest may be unavailable"
else
  echo "[session-start] WARN: no uv.lock, requirements-dev.txt or pyproject.toml — skipping Python install"
fi

# Persist the venv on PATH for every turn, so bare ruff/pytest/python resolve to
# it (not only under lint-all.py's internal PATH shim).
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo "export PATH=\"${CLAUDE_PROJECT_DIR:-.}/.venv/bin:\$PATH\"" >> "$CLAUDE_ENV_FILE"
fi

# Frontend toolchain — only for projects that have one. This block used to run
# unconditionally against a hardcoded `frontend/`, so a backend-only project spent
# the time on a guaranteed-failing `npm install` and then ran lockfile-restore
# logic against a path that does not exist. Both come from the manifest now.
frontend_enabled="$(python3 scripts/hooks/harness_config.py frontend.enabled 2>/dev/null)"
frontend_dir="$(python3 scripts/hooks/harness_config.py frontend.dir 2>/dev/null)"
if [ "$frontend_enabled" = "true" ] && [ -d "${frontend_dir:-frontend}" ]; then
  frontend_dir="${frontend_dir:-frontend}"
  echo "[session-start] Installing frontend toolchain (eslint/stylelint/tsc)..."
  # npm install (not ci) so a warm cached container reuses node_modules. The
  # container's npm may differ from the lockfile author's and rewrite lockfile
  # metadata on install; that churn trips the stop hook's dirty-tree check on
  # otherwise read-only sessions, so restore the lockfile if it was clean before.
  LOCKFILE="$frontend_dir/package-lock.json"
  lockfile_was_clean=false
  git diff --quiet -- "$LOCKFILE" 2>/dev/null && lockfile_was_clean=true
  npm install --prefix "$frontend_dir" --no-audit --no-fund \
    || echo "[session-start] WARN: npm install failed — frontend linters may be unavailable"
  if $lockfile_was_clean && ! git diff --quiet -- "$LOCKFILE" 2>/dev/null; then
    git checkout -- "$LOCKFILE" \
      && echo "[session-start] Restored $LOCKFILE (npm install metadata churn)"
  fi
else
  echo "[session-start] No frontend tier in .devkit.toml — skipping npm install"
fi

# Wire the pre-commit gate into .git/hooks, building the hook environments while the
# sandbox is being provisioned anyway. The local branch above calls the same function
# without `warm`; see it for why this is not remote-only.
wire_pre_commit warm

# External lint binaries lint-all.py shells out to, installed to a PATH dir so
# `shutil.which(...)` finds them. Best-effort: the runner skips a missing tool
# cleanly and CI installs them regardless, but having them here keeps a local
# `lint-all.py` run faithful to the gate. NB positional args:
# download-actionlint.bash takes [[VERSION] DIR], NOT a -b flag.
BIN_DIR=/usr/local/bin
[ -w "$BIN_DIR" ] || BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR"
if ! command -v actionlint >/dev/null 2>&1; then
  echo "[session-start] Installing actionlint -> $BIN_DIR..."
  curl -sSfL https://raw.githubusercontent.com/rhysd/actionlint/main/scripts/download-actionlint.bash \
    | bash -s -- latest "$BIN_DIR" \
    || echo "[session-start] WARN: actionlint install skipped"
fi
if ! command -v dotenv-linter >/dev/null 2>&1; then
  echo "[session-start] Installing dotenv-linter -> $BIN_DIR..."
  curl -sSfL https://raw.githubusercontent.com/dotenv-linter/dotenv-linter/master/install.sh \
    | sh -s -- -b "$BIN_DIR" \
    || echo "[session-start] WARN: dotenv-linter install skipped"
fi

echo "[session-start] Done. Run 'python scripts/lint-all.py' before pushing a gated branch."
