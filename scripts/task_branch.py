"""Shared helpers for naming and recognizing agent task branches.

Used by `scripts/task_slug.py` (UserPromptSubmit: records what this session's task
is called), `scripts/worktree.py` and `scripts/sweep.py` (which name the branches
they cut), and `scripts/ship.py` for the per-worktree shipped-marker contract.

**Nothing here cuts a branch any more.** `branch-per-task.py` and
`branch-on-write.py` did, inside whatever checkout the session was in, and their
whole family of helpers -- `auto_branch_decision`, `should_branch`, `checkout_base`,
`checkout_argv`, `spent_branch_notice`, `platform_manages_branch`,
`TASK_INTENT_MARKER_NAME` and `SHIPPED_MARKER_NAME` -- went with them. Cutting a branch *in place* is what let
a checkout outlive its task, and every state `sweep.py` hunts for follows from that.
An agent's work now lands in an ephemeral worktree that `worktree.py` cuts off
`origin/<default>` and destroys at the end, so the branch decision is made once, by
the thing that creates the checkout.

What survives is naming: turning a prompt into a slug (`slug_from_prompt`) and a slug
into a unique branch name (`branch_name`).

Pure and stdlib-only so the hooks can import it before the venv is active. Tested
in `scripts/hooks/tests/test_task_branch.py`.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

# Fallback default branch. The real default is resolved per-repo by
# `detect_default_branch()` (callers pass it in), so this is only the value the
# pure helpers assume when a caller does not override -- keeping the module
# project-agnostic while the branch name stays out of the logic.
DEFAULT_BRANCH = "master"
# Branches created by devkit use a vendor-neutral namespace. Older devkit branches
# and branches created directly by supported coding agents remain managed so adopting
# the neutral name does not strand work or prevent cleanup.
BRANCH_PREFIX = "agent/"
# Branches an unattended job cuts, rather than a session working on this project's code.
# A *sub-namespace* of `BRANCH_PREFIX` rather than a namespace beside it, and that is the
# whole design: every check in the tier asks `is_managed_task_branch`, so `/ship`, the
# sweep, `reap`, `reconcile` and the guard keep their answers unchanged, while the
# provenance becomes readable to the callers that have to tell the two apart. The nightly
# vendoring sweep cuts a branch in every consumer at once, none of it anyone's task, and
# `preview-task.py` was offering all of them above the change someone had just asked to
# look at.
AUTOMATION_PREFIX = BRANCH_PREFIX + "auto/"
MANAGED_BRANCH_PREFIXES = (AUTOMATION_PREFIX, BRANCH_PREFIX, "claude/", "codex/")
SLUG_MAX_LEN = 40
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")

# Words that carry no naming value. Stripped by `topic()` so the slug is built
# from what the task is *about* rather than from whatever words the prompt
# happened to open with. Deliberately conservative: articles, pronouns, aux
# verbs, prepositions, conjunctions, politeness and hedging -- never a verb that
# says what to do (add/fix/remove/rename/...), which is the most useful word in
# a branch name.
_FILLER: frozenset[str] = frozenset({
    # determiners and pronouns
    "a", "an", "the", "this", "that", "these", "those", "it", "its", "there", "here",
    "i", "we", "you", "me", "my", "our", "your", "us",
    # auxiliaries and modals
    "is", "are", "was", "were", "be", "been", "being", "am",
    "do", "does", "did", "doing", "done", "have", "has", "had", "get", "got",
    "can", "could", "should", "would", "will", "shall", "may", "might", "must",
    # politeness, hedging, discourse markers
    "please", "kindly", "thanks", "thank", "hey", "hi", "ok", "okay",
    "now", "just", "also", "really", "very", "quite", "maybe", "perhaps", "actually",
    "think", "thinks", "want", "wants", "need", "needs", "like", "let", "lets",
    # prepositions, conjunctions, interrogatives, quantifiers
    "to", "of", "in", "on", "at", "by", "for", "with", "from", "into", "onto", "as",
    "and", "or", "but", "so", "if", "then", "than", "when", "while", "because",
    "what", "which", "who", "whom", "how", "why", "where",
    "not", "no", "dont", "doesnt", "isnt", "wasnt", "cant",
    "some", "any", "all", "more", "most", "less", "much", "many",
    "possible", "instead", "about", "up", "out", "over", "again", "still",
})  # fmt: skip
# How many content words to keep. Six fits inside SLUG_MAX_LEN in practice while
# staying long enough to distinguish two tasks on the same area of the code.
TOPIC_WORDS_MAX = 6
_SENTENCE_SPLIT_RE = re.compile(r"[.?!\n]")


# Per-worktree counter of consecutive blocked stops, so pre-stop verification can
# re-check a fix instead of blocking exactly once. Read by `stop.py`; named here
# because this is the module that owns per-worktree marker names.
STOP_ROUNDS_MARKER_NAME = "agent-stop-rounds"


def worktree_file(git: Callable[..., subprocess.CompletedProcess[str]], name: str) -> Path | None:
    """Path of the per-worktree marker `name`, or None when git cannot resolve it.

    Markers live under the worktree's own git dir so parallel worktrees never share
    one, and `git rev-parse --git-path` is the only correct way to find that: for a
    linked worktree it returns that worktree's private dir, where a hand-built
    `<repo>/.git/<name>` would collide with every sibling checkout.

    `git(*args)` is injected (same contract as `detect_default_branch`) so this is
    unit-testable without spawning git.
    """
    try:
        result = git("rev-parse", "--git-path", name)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    raw = (result.stdout or "").strip()
    return Path(raw) if raw else None


def detect_default_branch(
    git: Callable[..., subprocess.CompletedProcess[str]], fallback: str = "main"
) -> str:
    """The remote's default branch, project-agnostically. Replaces a hardcoded name.

    Resolves `origin/HEAD` (the symbolic ref set at clone time); if that is not set,
    probes `origin/main` then `origin/master`; if neither exists, returns `fallback`.
    `git(*args)` must return a CompletedProcess capturing stdout -- injected so this
    stays pure and unit-testable without spawning git.
    """
    head = git("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    ref = (head.stdout or "").strip()
    if head.returncode == 0 and ref.startswith("refs/remotes/origin/"):
        return ref.rsplit("/", 1)[1]
    for candidate in ("main", "master"):
        if (
            git("rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{candidate}").returncode
            == 0
        ):
            return candidate
    return fallback


def parse_prompt(raw_stdin: str) -> str:
    """Extract the prompt text from a UserPromptSubmit payload; '' when absent."""
    try:
        payload = json.loads(raw_stdin)
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    value = payload.get("prompt", "")
    return value if isinstance(value, str) else ""


def topic(text: str, max_words: int = TOPIC_WORDS_MAX) -> str:
    """Condense a prompt down to the few words worth naming a branch after.

    Slugifying the prompt directly names the branch after its first 40 characters,
    which is usually preamble: "The hook that creates the branch - I think it uses
    a generic name..." becomes `the-hook-that-creates-the`. This keeps the first
    sentence (prompts state the task first, then elaborate), drops `_FILLER`, and
    keeps the leading `max_words` content words in their original order -- giving
    `hook-creates-branch-generic-name` from the same prompt, at no token cost.

    Returns '' when nothing survives; callers fall back to plain `slugify`.
    """
    # First sentence with something in it. A one-word opener ("Question.", "Hey!")
    # is a lead-in, not the task, so skip past it to the next sentence.
    segments = _SENTENCE_SPLIT_RE.split(text.strip().lower())
    words: list[str] = []
    for segment in segments:
        words = _SLUG_STRIP_RE.sub(" ", segment).split()
        if len(words) > 1:
            break
    kept = [w for w in words if w not in _FILLER]
    return "-".join(kept[:max_words])


def slugify(text: str, max_len: int = SLUG_MAX_LEN) -> str:
    """Turn free text into a branch-safe slug (lowercase, hyphenated)."""
    slug = _SLUG_STRIP_RE.sub("-", text.strip().lower()).strip("-")
    if len(slug) > max_len:
        # Trim at a word boundary so the slug stays readable, not mid-token.
        slug = slug[:max_len].rsplit("-", 1)[0] if "-" in slug[:max_len] else slug[:max_len]
        slug = slug.strip("-")
    return slug or "task"


def slug_from_prompt(text: str, max_len: int = SLUG_MAX_LEN) -> str:
    """The branch slug for a prompt: its topic, or the raw text when none survives."""
    return slugify(topic(text) or text, max_len=max_len)


def managed_branch_prefix(branch: str) -> str:
    """The managed task-branch prefix used by ``branch``, or ``""`` when absent.

    The **longest** match, not the first one listed. `agent/auto/` and `agent/` both
    prefix an automation branch, and every caller uses this to strip the prefix off a
    topic -- so a first-match answer leaves `auto/` in whatever is built from that topic,
    which for `worktree.box_name` is a path separator inside a directory name and a
    `COMPOSE_PROJECT_NAME` compose refuses. Matching by length rather than by position
    also keeps the tuple above an unordered set, which is what nobody editing it will
    think to preserve.
    """
    matched = [prefix for prefix in MANAGED_BRANCH_PREFIXES if branch.startswith(prefix)]
    return max(matched, key=len, default="")


def is_managed_task_branch(branch: str) -> bool:
    """Whether devkit may apply task-branch lifecycle operations to ``branch``."""
    return bool(managed_branch_prefix(branch))


def is_automation_branch(branch: str) -> bool:
    """Whether ``branch`` was cut by an unattended job rather than by an agent session.

    The question a UI-review menu asks about a ref, and the reason `AUTOMATION_PREFIX`
    is a namespace rather than a word inside the slug: `agent/auto-merge-label-0823` is
    a task someone gave an agent, and no substring test can tell it from a job's own
    branch. A path segment can.
    """
    return branch.startswith(AUTOMATION_PREFIX)


def branch_name(
    slug: str,
    existing: set[str],
    today: _dt.date | None = None,
    prefix: str = BRANCH_PREFIX,
) -> str:
    """Unique `<prefix><slug>-<mmdd>` name, disambiguated with -N against existing.

    `prefix` defaults to the session namespace, so the only callers that pass it are the
    unattended jobs naming themselves `AUTOMATION_PREFIX`.
    """
    today = today or _dt.date.today()
    base = f"{prefix}{slug}-{today:%m%d}"
    if base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"
