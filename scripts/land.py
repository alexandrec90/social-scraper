#!/usr/bin/env python3
"""Land the work in this checkout: commit it, open its PR, get it merged, sync the checkout.

What the `/land` skill runs, so a session ends with its change on the default branch and
in the person's own checkout, with nobody asked to merge anything. The owner's decision
(2026-11-24): a PR merges as it opens, except one touching the code that transacts money.

1. Commit everything with the given message, on a fresh branch when the checkout is on
   the default one. No local hook gates it (`git_policy`'s framework is skipped).
2. Push, and open the branch's PR, or reuse the one already open.
3. **Money.** A change to a path `[delivery] guarded` names (`.devkit.toml`) is labelled
   `automerge`: it merges itself once its gate is green, and this script does not wait.
4. **Everything else** is labelled `merge-now`, which the merge queue
   (`merge-dependabot-prs.py`, started by that very label) merges with no gate. This
   waits for it, `QUEUE_WAIT` at most, then merges the PR itself if it is still open and
   mergeable: a repository whose queue is not there yet must not strand it either.
5. A conflict is left open and labelled: the scheduled fix pass sends a resolver, and
   the queue merges it after. Nothing here rebases or waits on a person.
6. Once merged, the checkout the worktrees hang off fast-forwards to the merge, when it
   sits clean on the default branch.

Vendored from devkit (`sync-devkit.py`'s MANIFEST), stdlib only, and everything it asks
GitHub goes through `gh`. Tested in `scripts/hooks/tests/test_land.py`.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
MERGER_PATH = HERE / "merge-dependabot-prs.py"

# How long the queue gets before this merges the PR itself. A drain is an Actions job: it
# starts in tens of seconds, and merges one PR at a time per repository.
QUEUE_WAIT = 180.0
POLL_EVERY = 5.0

# The branch a commit made on the default branch moves to.
BRANCH_PREFIX = "land/"

# What reads as off to `git_policy`; anything else set skips the commit and push gates.
SKIP_ENV = {"DEVKIT_SKIP_PRE_COMMIT_FRAMEWORK": "1"}

LANDED = "landed"
GATED = "waits-for-gate"
CONFLICT = "conflict"
QUEUED = "queued"
HELD = "held"


def load_merger():
    """`merge-dependabot-prs.py`, which is vendored beside this file: its REST helpers and
    its reading of `[delivery] guarded`, so the two can never disagree on what is money."""
    spec = importlib.util.spec_from_file_location("merge_dependabot_prs", MERGER_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {MERGER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # before exec: see scripts/CLAUDE.md
    spec.loader.exec_module(module)
    return module


class LandError(RuntimeError):
    """A step that failed, with what it said."""


@dataclass
class Repo:
    root: Path
    slug: str
    default: str


def utf8_run(argv, **kwargs):
    """`subprocess.run` decoding UTF-8: on Windows `text=True` means cp1252, and a PR body
    it cannot decode comes back as no output at all."""
    kwargs = {key: value for key, value in kwargs.items() if key not in ("text", "check")}
    return subprocess.run(argv, encoding="utf-8", errors="replace", check=False, **kwargs)


def git(args: list[str], run, cwd: Path, check: bool = True) -> str:
    result = run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **SKIP_ENV},
    )
    if check and result.returncode != 0:
        raise LandError(f"git {' '.join(args)}: {(result.stderr or result.stdout).strip()}")
    return result.stdout.strip() if result.returncode == 0 else ""


def slug_of(url: str) -> str:
    """`owner/name` from a GitHub remote URL, https or ssh."""
    match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url.strip())
    if not match:
        raise LandError(f"origin is not a GitHub repository: {url!r}")
    return match.group(1)


def read_repo(cwd: Path, run) -> Repo:
    root = Path(git(["rev-parse", "--show-toplevel"], run, cwd))
    slug = slug_of(git(["remote", "get-url", "origin"], run, root))
    head = git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], run, root, check=False)
    return Repo(root, slug, head.removeprefix("origin/") or "main")


def branch_name(title: str, now: float) -> str:
    words = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40].rstrip("-")
    return f"{BRANCH_PREFIX}{words or 'change'}-{time.strftime('%m%d-%H%M%S', time.localtime(now))}"


def commit(repo: Repo, title: str, body: str, run, now: float) -> str:
    """Commit every change in the tree; the branch the commit is on."""
    branch = git(["branch", "--show-current"], run, repo.root, check=False)
    if not branch or branch == repo.default:
        branch = branch_name(title, now)
        git(["switch", "-c", branch], run, repo.root)
    git(["add", "-A"], run, repo.root)
    staged = git(["diff", "--cached", "--name-only"], run, repo.root)
    if staged:
        message = f"{title}\n\n{body}".strip() + "\n"
        git(["commit", "--quiet", "-m", message], run, repo.root)
    return branch


def changed_files(repo: Repo, run) -> list[str]:
    git(["fetch", "--quiet", "origin", repo.default], run, repo.root)
    # No rename detection, which lists only the new path; `-z`, or git quotes non-ASCII.
    # Either would hide a guarded path from `needs_gate`.
    span = f"origin/{repo.default}...HEAD"
    listed = git(["diff", "--name-only", "--no-renames", "-z", span], run, repo.root)
    return [path for path in listed.split("\0") if path]


def open_pr(repo: Repo, branch: str, title: str, body: str, merger, run) -> int:
    """The number of `branch`'s open PR, opened here when there is none."""
    owner = repo.slug.split("/", 1)[0]
    found = merger.gh_json(f"repos/{repo.slug}/pulls?state=open&head={owner}:{branch}", run)
    if found:
        return int(found[0]["number"])
    created = merger.gh(
        [
            "api",
            "--method",
            "POST",
            f"repos/{repo.slug}/pulls",
            "-f",
            f"title={title}",
            "-f",
            f"body={body}",
            "-f",
            f"head={branch}",
            "-f",
            f"base={repo.default}",
            "--jq",
            ".number",
        ],
        run,
    )
    return int(created.strip())


def label(repo: Repo, number: int, name: str, merger, run) -> None:
    """GitHub creates a label the repository has never had when it is first applied."""
    path = f"repos/{repo.slug}/issues/{number}/labels"
    merger.gh(["api", "--method", "POST", path, "-f", f"labels[]={name}"], run)


def await_merge(repo: Repo, number: int, merger, run, sleep, clock) -> dict:
    """The PR once the queue merged it, or as it stands when `QUEUE_WAIT` ran out."""
    deadline = clock() + QUEUE_WAIT
    while True:
        pr = merger.gh_json(f"repos/{repo.slug}/pulls/{number}", run) or {}
        if pr.get("merged") or pr.get("state") == "closed" or clock() >= deadline:
            return pr
        sleep(POLL_EVERY)


def merge_it(repo: Repo, number: int, merger, run, sleep, clock) -> str:
    """Wait for the queue, then merge it here if the queue never came."""
    pr = await_merge(repo, number, merger, run, sleep, clock)
    if pr.get("merged"):
        return LANDED
    pr = merger.settled(repo.slug, number, run, sleep, pr)
    if pr.get("mergeable") is False:
        return CONFLICT
    if pr.get("mergeable") is not True or pr.get("state") == "closed":
        return QUEUED
    merger.merge_pr(repo.slug, number, run, str(pr.get("title") or ""), str(pr.get("body") or ""))
    merger.delete_branch(repo.slug, pr, run)
    return LANDED


def main_checkout(repo: Repo, run) -> Path:
    """The checkout every worktree of this repository hangs off: the first one listed."""
    listing = git(["worktree", "list", "--porcelain"], run, repo.root)
    first = next((line for line in listing.splitlines() if line.startswith("worktree ")), "")
    return Path(first.removeprefix("worktree ")) if first else repo.root


def sync_checkout(repo: Repo, run) -> str:
    """Fast-forward the main checkout to the merge; a sentence saying what happened."""
    target = main_checkout(repo, run)
    git(["fetch", "--quiet", "origin", repo.default], run, repo.root)
    on = git(["branch", "--show-current"], run, target, check=False)
    if on != repo.default:
        return f"{target} is on {on or 'a detached HEAD'}, not {repo.default}: left as it is"
    if git(["status", "--porcelain", "--untracked-files=no"], run, target, check=False):
        return f"{target} has uncommitted changes: left for the scheduled sync"
    git(["merge", "--ff-only", "--quiet", f"origin/{repo.default}"], run, target, check=False)
    tip = git(["rev-parse", f"origin/{repo.default}"], run, target, check=False)
    if git(["rev-parse", "HEAD"], run, target, check=False) != tip:
        return f"{target} has diverged from origin/{repo.default}: left for the scheduled sync"
    return f"{target} is at origin/{repo.default}"


def settings_at(repo: Repo, ref: str, merger, run) -> dict:
    """`.devkit.toml` as `ref` has it; nothing when it is absent or unreadable."""
    shown = git(["show", f"{ref}:.devkit.toml"], run, repo.root, check=False)
    try:
        return merger.tomllib.loads(shown) if shown else {}
    except merger.tomllib.TOMLDecodeError:
        return {}


def needs_gate(repo: Repo, files: list[str], merger, run) -> str:
    """Why this change waits for its gate, or "" when it merges now.

    The flow is the default branch's, as the merge queue reads it, so a branch cannot
    switch its own gate off. The guarded paths are both sides', so it cannot drop its
    guard either.
    """
    base = settings_at(repo, f"origin/{repo.default}", merger, run)
    if not merger.flows_directly(base):
        return f'is in a gated project ({repo.default} is not `flow = "direct"`)'
    globs = merger.guarded_globs(base) + merger.guarded_globs(
        settings_at(repo, "HEAD", merger, run)
    )
    money = merger.guarded_paths(files, globs)
    return f"touches {', '.join(money)}" if money else ""


def land(args, run=utf8_run, sleep=time.sleep, clock=time.monotonic, merger=None) -> str:
    """Every step; the outcome: `LANDED`, `GATED`, `CONFLICT`, `QUEUED` or `HELD`."""
    merger = merger or load_merger()
    repo = read_repo(Path(args.cwd), run)
    branch = commit(repo, args.title, args.body, run, time.time())
    files = changed_files(repo, run)
    if not files:
        raise LandError(f"nothing differs from origin/{repo.default}: there is nothing to land")
    git(["push", "--quiet", "-u", "origin", f"HEAD:refs/heads/{branch}"], run, repo.root)
    number = open_pr(repo, branch, args.title, args.body, merger, run)
    # A reused PR may already be held, whatever this run was told.
    pr = merger.gh_json(f"repos/{repo.slug}/pulls/{number}", run) or {}
    if merger.held({"body": args.body, "labels": []}) or merger.held(pr) or pr.get("draft"):
        print(f"#{number} is held or a draft: it waits for the person.")
        return HELD
    gate_for = needs_gate(repo, files, merger, run)
    if gate_for:
        label(repo, number, merger.AUTOMERGE_LABEL, merger, run)
        print(f"#{number} {gate_for}: it merges itself once its gate is green.")
        return GATED
    label(repo, number, merger.MERGE_NOW_LABEL, merger, run)
    outcome = merge_it(repo, number, merger, run, sleep, clock)
    url = f"https://github.com/{repo.slug}/pull/{number}"
    if outcome == LANDED:
        print(f"{url} merged. {sync_checkout(repo, run)}.")
    elif outcome == CONFLICT:
        print(f"{url} conflicts: the fix pass resolves it, and it merges itself after.")
    else:
        print(f"{url} is labelled merge-now and the queue merges it.")
    return outcome


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--title", required=True, help="the commit subject and PR title")
    body = parser.add_mutually_exclusive_group()
    body.add_argument("--body", default="", help="the commit body and PR description")
    body.add_argument("--body-file", type=Path, help="read the body from this file")
    parser.add_argument("--cwd", default=".", help="a directory inside the checkout")
    args = parser.parse_args(argv)
    if args.body_file:
        args.body = args.body_file.read_text(encoding="utf-8")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        land(args)
    except (LandError, RuntimeError) as failed:
        print(f"land: {failed}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
