#!/usr/bin/env python3
"""Mechanical checks, lint and push for /ship.

The shipped-marker half is gone with `branch-per-task.py`. The marker existed to tell
the *next prompt* that this branch was spent, so the branch hook could leave it — and
with agent work happening in a box that is destroyed after its PR merges, there is no
next prompt on a spent branch to warn.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))
import task_branch as tb
import toolchain

REPO_ROOT = Path(__file__).resolve().parents[1]
LINT_ALL = REPO_ROOT / "scripts" / "lint-all.py"
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NOT_SHIPPABLE = 3
EXIT_DIRTY_TREE = 4
EXIT_LINT_FAILED = 5
EXIT_PUSH_FAILED = 6
EXIT_FIXERS_FAILED = 7

# Where a venv puts pre-commit's console script: `Scripts/` on Windows, `bin/` elsewhere.
PRE_COMMIT_TAILS = (Path("Scripts") / "pre-commit.exe", Path("bin") / "pre-commit")


def venv_module_command(launcher: Path) -> list[str]:
    """`<venv python> -m pre_commit` when the venv's interpreter sits beside `launcher`,
    else the launcher itself.

    uv writes the interpreter's absolute path into every console script, so one written
    from a tree since deleted dies with `uv trampoline failed to canonicalize script
    path` -- devkit's `claude --worktree` trees share the checkout's `.venv` through a
    link, and the tree that last synced into it is gone the day it merges. The
    interpreter reads `pyvenv.cfg` and survives that. The launcher still says pre-commit
    is installed here. Mirrored in `git_policy.framework._pre_commit_command`.
    """
    python = launcher.with_name("python.exe" if launcher.suffix == ".exe" else "python")
    return [str(python), "-m", "pre_commit"] if python.is_file() else [str(launcher)]


# What `claude --worktree <name>` names the branch it cuts: the literal string
# `worktree-` followed by the name, with any `/` replaced by `+`. It is hard-coded in the
# CLI -- there is no setting for it -- so a session that isolates itself with the built-in
# flag can never present the `<namespace>/<topic>` spelling below, however it is invoked.
# Refusing it would mean `/ship` worked from a devkit box and not from Claude Code's own
# worktree, which is a rule about provenance rather than about the property being checked.
CLI_WORKTREE_PREFIX = "worktree-"


def is_shippable(branch: str, default: str) -> tuple[bool, str]:
    """Return whether branch is an isolated task branch suitable for a PR.

    Two accepted spellings, and the test is the same one in both cases -- *is this branch
    disposable* -- rather than who cut it:

    - ``<namespace>/<topic>``, which identifies a short-lived task branch without
      coupling shipping to whichever agent created it (``agent/``, ``claude/``,
      ``codex/``).
    - ``worktree-<topic>``, which is what ``claude --worktree`` cuts.

    Anything else remains reserved for default and long-lived home branches.
    """
    if not branch:
        return False, "HEAD is detached; check out a task branch before shipping."
    if branch == default:
        return False, f"'{default}' is the default branch; ship from a namespaced task branch."
    if branch.startswith(CLI_WORKTREE_PREFIX) and branch != CLI_WORKTREE_PREFIX:
        return True, ""
    namespace, separator, topic = branch.partition("/")
    if not separator or not namespace or not topic:
        return False, (
            f"'{branch}' is not a namespaced task branch; refusing to ship it. "
            "Use a branch such as agent/fix-thing."
        )
    return True, ""


def tree_clean(porcelain: str) -> bool:
    return not porcelain.strip()


def toolchain_report(root: Path = REPO_ROOT) -> list[str]:
    """What the checkout lacks before its gates can run, one line each. Empty when provisioned.

    Printed by `--preflight`, the first step of `/ship`, because the alternative is where
    this was found: at step 2's `git commit`, when the pre-commit gate resolved a
    `language: system` entry against a `PATH` with no venv on it and refused the commit.
    A linked worktree checks out tracked files only, so a session in a fresh one has no
    `.venv` and no `node_modules` until something installs them, and the agent that hit
    it installed the one missing tool by hand -- which got one commit through and left
    the lint gate at step 3 to fail the same way. Named here, the fix is one command run
    before anything is committed. The ladder is `scripts/hooks/toolchain.py`, shared with
    the SessionStart report so the two cannot name different commands.
    """
    lines = [gap.line for gap in toolchain.missing_toolchain(root)]
    if lines:
        lines.append(
            "provision before committing: the commit-time pre-commit gate and the lint gate "
            "both run from the toolchain above, and neither installs it."
        )
    return lines


def backoff_delays() -> list[int]:
    return [2, 4, 8, 16]


def _git(*args: str, capture: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=capture,
        text=True,
        check=False,
    )


def current_branch() -> str:
    result = _git("branch", "--show-current")
    return result.stdout.strip() if result.returncode == 0 else ""


def default_branch() -> str:
    return tb.detect_default_branch(_git, fallback="main")


def _porcelain() -> str:
    result = _git("status", "--porcelain")
    return result.stdout if result.returncode == 0 else ""


def branch_diff_files(base: str, git=_git) -> list[str]:
    """The files this branch changed, measured from where it left the default branch.

    The gate used to ask for `--changed`, whose set is the working tree versus HEAD --
    and `main()` has just refused to proceed unless that set is *empty*. So the lint
    gate ran on nothing, every time, and reported LINT PASSED for it. A branch about to
    become a PR is reviewed as a whole, so the whole branch is what to lint.

    Returns [] when the merge base cannot be found (a base ref absent locally, a shallow
    clone); the caller then falls back to the old behaviour rather than linting nothing
    silently.
    """
    ref = f"origin/{base}"
    if git("rev-parse", "--verify", "--quiet", ref).returncode != 0:
        ref = base
    merge_base = git("merge-base", ref, "HEAD")
    if merge_base.returncode != 0 or not merge_base.stdout.strip():
        return []
    # --diff-filter=d: a path deleted on this branch has nothing left to lint, and a
    # linter handed a missing file fails the run on a usage error.
    diff = git("diff", "--name-only", "--diff-filter=d", merge_base.stdout.strip(), "HEAD")
    if diff.returncode != 0:
        return []
    return [line.strip() for line in diff.stdout.splitlines() if line.strip()]


def runner_supports_paths(help_text: str) -> bool:
    """Whether this project's lint runner accepts `--paths`.

    `lint-all.py` is project-owned, not vendored, so a project can be older than this
    file. Probing `--help` beats parsing argparse's exit-2 usage error out of a stream
    the gate otherwise passes straight through to the terminal.
    """
    return "--paths" in help_text


def lint_python(
    root: Path = REPO_ROOT,
    env: dict[str, str] | None = None,
    run: toolchain.Runner = subprocess.run,
) -> str:
    """The interpreter to run the lint runner under: the tree's `.venv` when this one lacks ruff.

    `lint-all.py` is project-owned, so a project's copy can predate the template's own
    re-run under the venv -- and that copy, handed an interpreter without ruff or mypy,
    skips both and prints `clean`, the gate passing on a check that never ran (db3e57f7,
    roguelike). Choosing the interpreter here reaches every such copy from the vendored
    side; `toolchain.rerun_target` is the same ladder the template's runner climbs.
    """
    target = toolchain.rerun_target(root, "ruff", env, run)
    return sys.executable if target is None else str(target)


def _lint_argv(paths: list[str], help_text: str, python: str = sys.executable) -> list[str]:
    """The lint command to run, given the branch's files and the runner's capabilities."""
    if paths and runner_supports_paths(help_text):
        return [python, str(LINT_ALL), "--paths", *paths]
    return [python, str(LINT_ALL), "--changed"]


def _lint_help(python: str = sys.executable) -> str:
    try:
        probe = subprocess.run(
            [python, str(LINT_ALL), "--help"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return ""
    return (probe.stdout or "") + (probe.stderr or "")


def _run_lint(base: str = "") -> bool:
    if not LINT_ALL.is_file():
        print(f"ship: required lint runner is missing: {LINT_ALL}", file=sys.stderr)
        return False
    paths = branch_diff_files(base) if base else []
    python = lint_python()
    argv = _lint_argv(paths, _lint_help(python), python)
    if paths and "--paths" not in argv:
        # Say it rather than quietly linting the empty working tree: the gate is about
        # to run, pass, and mean nothing, and only this line explains why.
        print(
            f"ship: {LINT_ALL.name} has no --paths, so the gate can only see the working "
            "tree -- which is clean. It will check nothing. Add --paths to this "
            "project's lint runner (see devkit's) to lint the branch diff.",
            file=sys.stderr,
        )
    try:
        result = subprocess.run(argv, cwd=REPO_ROOT, check=False)
    except OSError as exc:
        print(f"ship: could not run lint gate: {exc}", file=sys.stderr)
        return False
    return result.returncode == 0


TRANSIENT_PUSH_MARKERS = ("could not resolve", "timed out", "connection", "network")


def push_failure(stderr: str) -> str:
    """Why a push failed, in words that send the reader to the right place.

    Every failure used to read "push failed after retries", and a session that got it
    went to diagnose the network when the pre-push gate had failed a test. Retrying is
    only for the transient markers; anything else was refused once, and git tells the
    two refusals apart: a remote rejection names the ref (`! [rejected]`), and a local
    pre-push hook that exits non-zero leaves only `failed to push some refs`, with the
    hook's own output above it.
    """
    text = stderr.lower()
    if any(marker in text for marker in TRANSIENT_PUSH_MARKERS):
        return f"push failed on a network error after {len(backoff_delays())} retries."
    if "[rejected]" in text or "[remote rejected]" in text:
        return "push rejected by the remote (not a network error): see git's output above."
    if "failed to push some refs" in text:
        return (
            "push refused before anything was sent -- the pre-push gate failed (not a network "
            "error). Its findings are above and in logs/test-failures.log / logs/lint-errors.log."
        )
    return "push failed (not a network error): see git's output above."


def _push(branch: str, sleep=time.sleep) -> bool:
    """Push the task branch, retrying only recognizably transient failures."""
    delays = backoff_delays()
    for attempt in range(len(delays) + 1):
        result = _git("push", "-u", "origin", branch)
        if result.returncode == 0:
            return True
        stderr = result.stderr or ""
        transient = any(marker in stderr.lower() for marker in TRANSIENT_PUSH_MARKERS)
        if not transient or attempt == len(delays):
            for stream in (result.stdout, stderr):
                if stream and stream.strip():
                    print(stream.rstrip(), file=sys.stderr)
            print(f"ship: {push_failure(stderr)}", file=sys.stderr)
            return False
        sleep(delays[attempt])
    return False


def changed_paths(porcelain: str) -> list[str]:
    """Every path `git status --porcelain` reports, deletions dropped: what the commit
    stage is run over before any of it is staged.

    Untracked (`??`) paths count -- a new file is exactly the one no hook has formatted
    yet. A deletion is skipped for the reason `branch_diff_files` gives, a rename is read
    at its destination, and a path git quoted for a space is unquoted.
    """
    paths: list[str] = []
    for line in porcelain.splitlines():
        if len(line) < 4 or "D" in line[:2]:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if len(path) >= 2 and path[0] == path[-1] == '"':
            path = path[1:-1]
        paths.append(path)
    return paths


def pre_commit_command(
    root: Path,
    checkout: Path | None = None,
    which=shutil.which,
    find_spec=importlib.util.find_spec,
) -> list[str] | None:
    """Where this project's `pre-commit` is, looked for the way the global dispatcher
    looks (`git_policy.framework._pre_commit_command`): `.venv` of this tree, then of
    the checkout it was cut from, then `PATH`, then this interpreter.

    Mirrored rather than imported because `ship.py` is vendored into projects that do
    not carry the policy package -- and the two have to agree on *which* pre-commit
    runs, so that the fixers this step applies are the fixers the commit will meet.
    """
    bases = [root] if checkout is None or checkout == root else [root, checkout]
    for base in bases:
        for tail in PRE_COMMIT_TAILS:
            candidate = base / ".venv" / tail
            if candidate.is_file():
                return venv_module_command(candidate)
    found = which("pre-commit")
    if found:
        return [found]
    if find_spec("pre_commit") is not None:
        return [sys.executable, "-m", "pre_commit"]
    return None


def run_fixers(paths: list[str], command: list[str], runner=subprocess.run) -> tuple[int, str]:
    """Run the commit stage over `paths` until it is quiet: (exit code, the verdict).

    pre-commit exits 1 for a fixer that rewrote a file and for a check that failed, and
    only a second run tells them apart: a rewrite leaves nothing left to rewrite, a
    finding is still there. So the first pass is allowed to fail and the second decides.
    Streamed rather than captured -- pre-commit's own report is what the reader acts on.

    This is the step the edit-time `lint-fix.py` hook used to make unnecessary. Where
    that hook is off (`DEVKIT_HOOKS_OFF`, Codex), the commit-stage fixers are the first
    thing to format a file, and a fixer that rewrites fails the commit by design -- so
    every commit took two passes, with the rewrites staged by hand between them.
    """
    if not paths:
        return EXIT_OK, "nothing to fix: no changed paths in the working tree"
    argv = [*command, "run", "--files", *paths]
    for attempt in (1, 2):
        try:
            done = runner(argv, cwd=REPO_ROOT, check=False)
        except OSError as exc:
            return EXIT_FIXERS_FAILED, f"could not run pre-commit: {exc}"
        if done.returncode == 0:
            if attempt == 1:
                return EXIT_OK, "commit stage quiet: nothing rewritten, nothing reported"
            return EXIT_OK, (
                "fixers rewrote files on the first pass and are quiet now; "
                "stage the rewrites with the change"
            )
    return EXIT_FIXERS_FAILED, (
        "the commit stage still fails after the fixers ran: what is reported above "
        "needs a code change, not a re-run"
    )


def reconcile_baseline() -> tuple[tuple[int, int] | None, str]:
    """`untested_symbols.reconcile` with nothing to record, and the baseline's name.

    Imported here, not at the top: the scan reads every source file, and only `--fix`
    pays for it.
    """
    import untested_symbols

    root, cfg = untested_symbols.REPO_ROOT, untested_symbols.CFG
    return untested_symbols.reconcile(root, cfg, None), untested_symbols.BASELINE_NAME


def drop_covered_baseline(paths: list[str], reconcile=None) -> tuple[list[str], str]:
    """Drop the untested-symbol baseline's lines this change covered: `(paths, line)`.

    A test that covers a baselined symbol fails the vendored gate until its line goes,
    and nothing a session runs locally says so -- ibkr_trader #74 claimed a green full
    suite and went red on exactly that. Dropping a covered line only shrinks the debt,
    the one direction the file may move without a decision, so the commit stage makes
    it here like a formatter rewrite: the rewritten baseline joins `paths`, and the
    line says what changed. `before=None` records nothing new, ever.
    """
    try:
        result, name = (reconcile or reconcile_baseline)()
    except (OSError, SyntaxError, ValueError) as exc:
        return paths, f"could not reconcile the untested-symbol baseline: {exc}"
    if not result or not result[0]:
        return paths, ""
    kept = paths if name in paths else [*paths, name]
    return kept, f"dropped {result[0]} line(s) this change covered from {name}"


CODEX_CONTEXT_SCRIPT = "scripts/sync-codex-context.py"
CODEX_SKILLS_DIR = ".agents/skills"


def mirror_codex_skills(root: Path = REPO_ROOT) -> str:
    """Re-mirror `.claude/skills/` into `.agents/skills/`: the line saying so, else "".

    The vendored `test_sync_codex_context.py` fails a stale mirror, and only devkit's own
    pre-commit config re-mirrors at commit time -- a generated project's does not, so
    roguelike #81 edited `art-check`'s source and went red in CI on the copy. Every
    session's change is committed through `--fix`, which makes this the one place the
    mirror can be kept current in every consumer at once, like a formatter rewrite.

    Only where the mirror exists (a project that opted into Codex), and in-process via
    the vendored `sync-codex-context.py`, whose `mirror_tree` is the one definition of
    the mirror. Loaded here rather than through `scripts/precommit/_loader.py`, which is
    not vendored; absent, there is nothing to do and the gate names the stale files.
    """
    mirror, script = root / CODEX_SKILLS_DIR, root / CODEX_CONTEXT_SCRIPT
    if not mirror.is_dir() or not script.is_file():
        return ""
    try:
        spec = importlib.util.spec_from_file_location("sync_codex_context", script)
        if spec is None or spec.loader is None:
            raise ImportError(f"no loader for {script}")
        context = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = context
        spec.loader.exec_module(context)
    except (ImportError, OSError, SyntaxError) as exc:
        return f"could not load {CODEX_CONTEXT_SCRIPT} to re-mirror .agents/skills/: {exc}"
    source = root / ".claude" / "skills"
    wanted, held = context.relative_files(source), context.relative_files(mirror)
    changed = (wanted ^ held) | {
        rel for rel in wanted & held if (mirror / rel).read_bytes() != (source / rel).read_bytes()
    }
    if not changed:
        return ""
    context.mirror_tree(source, mirror)
    return f"re-mirrored {len(changed)} file(s) into {CODEX_SKILLS_DIR}/ for Codex"


def _fix(explicit: list[str]) -> int:
    paths, dropped = drop_covered_baseline(explicit or changed_paths(_porcelain()))
    if dropped:
        print(f"ship: {dropped}")
    common = _git("rev-parse", "--path-format=absolute", "--git-common-dir")
    where = (common.stdout or "").strip()
    checkout = Path(where).parent if common.returncode == 0 and where else None
    command = pre_commit_command(REPO_ROOT, checkout)
    if command is None:
        print(
            "ship: pre-commit was not found in this tree's .venv, the checkout's, PATH or "
            "this interpreter; provision first (--preflight names the command).",
            file=sys.stderr,
        )
        return EXIT_FIXERS_FAILED
    code, verdict = run_fixers(paths, command)
    if code:
        # What ran, before the verdict: a launcher that dies on its own (uv's one-line
        # `failed to canonicalize script path`) names neither itself nor its venv, and
        # without this the pass's `pre-commit.log` held that line alone -- a dozen calls
        # of binary scanning to find the stale `.venv` (4099febd). Kept off the last
        # line, which `ship_intent.refusal_line` may take into a signature.
        print(f"ship: ran {' '.join(command)} run --files ({len(paths)} path(s))", file=sys.stderr)
    print(f"ship: {verdict}", file=sys.stderr if code else sys.stdout)
    # After the fixers, so a skill source they rewrote is what the mirror copies; the
    # caller stages the whole tree (`ship_intent.commit_intent`), deletions included.
    mirrored = "" if code else mirror_codex_skills()
    if mirrored:
        print(f"ship: {mirrored}")
    return code


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    mode, rest = (argv[0], argv[1:]) if argv else ("", [])
    if mode not in ("", "--preflight", "--fix") or (mode != "--fix" and rest):
        print("usage: ship.py [--preflight | --fix [PATH ...]]", file=sys.stderr)
        return EXIT_USAGE

    # Before the branch rule, which is about where a new PR opens: `--fix` opens nothing,
    # and the pass runs it on an open PR's head whatever that branch is called (#390).
    if mode == "--fix":
        return _fix(rest)

    branch = current_branch()
    base = default_branch()
    ok, reason = is_shippable(branch, base)
    if not ok:
        print(f"ship: {reason}", file=sys.stderr)
        return EXIT_NOT_SHIPPABLE

    if mode == "--preflight":
        print(f"ship: branch={branch} base={base}")
        # Reported, not enforced: a checkout whose tools live outside `.venv` can still
        # ship, and the gates that need the toolchain fail on their own if it is absent.
        for line in toolchain_report():
            print(f"ship: {line}", file=sys.stderr)
        return EXIT_OK

    if not tree_clean(_porcelain()):
        print("ship: working tree is dirty; commit the intended changes first.", file=sys.stderr)
        return EXIT_DIRTY_TREE
    if not _run_lint(base):
        print("ship: branch-scope lint failed; see logs/lint-errors.log.", file=sys.stderr)
        return EXIT_LINT_FAILED
    if not _push(branch):
        return EXIT_PUSH_FAILED

    print(f"ship: pushed branch={branch} base={base}; open or reuse its PR before marking shipped.")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
