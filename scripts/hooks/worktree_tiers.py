"""Every directory a worktree is cut into on this machine, and which checkout one of
them belongs to.

Three tiers, in shapes that are not variations of each other:

| Tier | Where a worktree lands | Reaped by |
| --- | --- | --- |
| claude | `<checkout>/.claude/worktrees/<name>` -- *nested* | nothing |
| codex | `<CODEX_HOME>/worktrees/<repo-hash>/<name>` -- *detached* | nothing |
| box | `<workspace>/.worktrees/<project>--<topic>` | `worktree.py reconcile` |

Four things cut into them and only one is devkit: `claude --worktree <topic>`, a Remote
Control server started with `--spawn worktree`, and this harness's own quick-pick
(`agent-worktree.py new`, `fix-prs.py`) all land in the claude tier; `codex --worktree`
lands in its own; `worktree.py new` cuts a box. The third column is why that matters --
a box carries a lease and is destroyed on a schedule, and the other two rows are cut by
programs that register the result nowhere and clean up nothing.

**The difference is not the directory name, it is whether the path names the checkout.**
A nested worktree answers "which checkout owns me" by counting directories upward, which
is pure, needs no git and works for a path that has already been deleted. A detached one
cannot: `<repo-hash>` is an opaque digest, so the only thing on disk that names the
checkout is the worktree's own `.git` *file*, and reading it requires the worktree to
still exist. Four modules had privately implemented the nested arithmetic and each of
them silently answered a detached worktree with a directory nobody ever wrote -- a
workspace file under `~/.codex/`, a ledger row filed under a hash, a scheduled task
registered against a checkout that gets reaped. So both halves live here: `TIERS` is the
shape, `owning_checkout` is the resolver that prefers arithmetic and falls back to git's
own pointer.

**`TIERS` is the first two rows; `ALL_TIERS` is all three.** The box tier is deliberately
outside the list `match()` answers for, because a box is not a variant of the others --
it anchors on the *workspace* rather than on a checkout, so `owning_checkout` has no
answer for one, and it carries a port lease and a container stack that only
`worktree.py reap` knows how to release. Every `TIERS` reader would be wrong about a box
in a different way: the delete menu would offer `git worktree remove` on it and leak the
lease, the Stop gate would verify the wrong tree, the ledger would file its rows under
the workspace. What all three tiers do share is their **names**, and that is what is
shared here: `BOXES_DIR_NAME` for the one directory the box tier is, `MARKER_NAMES` for
a tree walk that must not descend into any of them, and `ALL_TIERS` for a caller whose
question really is "every worktree root on this machine" -- the mirror in
`frontend/src/worktreePort.ts` and, one day, a reaper for the two rows that have none.

**Stdlib only, and in `scripts/hooks/` for that reason.** Hooks run before a virtualenv
exists and from consumer checkouts that ship no workspace scripts, so this could not live
beside `sweep.py` where its first caller was. It is in `sync-devkit.py`'s `MANIFEST`:
the other vendored files import it plainly, the way every hook imports `harness_config`,
and a consumer that pulled one without the other gets an `ImportError` inside a hook,
which exits non-2 and silently disables the gate it lives in. They arrive in one
`--pull`, so a guard would buy nothing but a second code path only a hand-assembled copy
can reach.

Every function here is pure apart from `git_checkout`, which reads one file. Tested in
`scripts/hooks/tests/test_worktree_tiers.py`.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Tier:
    """One agent CLI's worktree directory, described so a path can be matched to it.

    `segments` are the directory names between the anchor and the worktree's *own*
    parent chain, outermost first; `depth` is how many directories sit between the last
    of them and the worktree itself. Claude's `<checkout>/.claude/worktrees/<name>` is
    `(".claude", "worktrees")` at depth 1; Codex's `<home>/worktrees/<hash>/<name>` is
    `("worktrees",)` at depth 2, and the extra level is the digest that makes the tier
    detached.

    `home_env`/`home_default` are empty for a tier anchored at a directory the path
    itself names, and that emptiness is the flag every function here branches on -- it is
    exactly the distinction between a tier whose owner can be computed and one whose
    owner must be read. For the two tiers in `TIERS` that anchor is the owning checkout;
    for `BOX_TIER`, which is not in `TIERS` and which nothing below is applied to, it is
    the workspace root.
    """

    agent: str
    segments: tuple[str, ...]
    depth: int
    home_env: str = ""
    home_default: str = ""

    @property
    def detached(self) -> bool:
        """Whether this tier lives outside the checkout it belongs to."""
        return bool(self.home_env or self.home_default)


# The list. Adding a runtime is one row -- nothing below names an agent.
#
# The first row is the DEFAULT tier: it is where this harness cuts a new worktree
# (`agent-worktree.py new`, `fix-prs.py`), and the only one whose names are shown
# unqualified. The others are discovered and removed but never written to, because a
# machine with two conventions for where `codex --worktree` puts things is worse than
# one that just uses the built-in.
#
# Codex reads `CODEX_HOME` for its state directory and defaults it to `~/.codex`; the
# `[features] worktrees` flag in its config turns the tier on but does not move it.
TIERS: tuple[Tier, ...] = (
    Tier(agent="claude", segments=(".claude", "worktrees"), depth=1),
    Tier(
        agent="codex",
        segments=("worktrees",),
        depth=2,
        home_env="CODEX_HOME",
        home_default="~/.codex",
    ),
)

DEFAULT_TIER = TIERS[0]

# The box tier: `<workspace>/.worktrees/<project>--<topic>`, cut by `worktree.py new`,
# holding a port lease and a `COMPOSE_PROJECT_NAME`, destroyed by `worktree.py reconcile`
# once its PR merges. Kept OUT of `TIERS` -- see the module docstring: it anchors on the
# workspace, and every function below would answer for it wrongly.
#
# The name is here rather than in `worktree.py` because `worktree.py` is a workspace
# script and the copies that needed it were not -- a Stop hook in a consumer checkout, a
# disk reclaimer and a tree walk that both run before any virtualenv, the Codex hook
# generator, two ratchets: six private spellings of one directory. Three now read this
# constant; the three that cannot are pinned equal to it by
# `tests/test_worktree_tiers_single_source.py`, which carries the reason for each.
BOXES_DIR_NAME = ".worktrees"
BOX_TIER = Tier(agent="devkit", segments=(BOXES_DIR_NAME,), depth=1)

# Every root a worktree is cut into on this machine. `TIERS` is what `match()` answers
# for; this is what a caller enumerating the machine wants -- `tests/test_worktree_port.py`
# holds the TypeScript mirror equal to exactly this tuple.
ALL_TIERS: tuple[Tier, ...] = (*TIERS, BOX_TIER)

# The innermost directory name of every tier, for a tree walk that must not descend into
# another checkout's copy of the repo. `.claude` is deliberately absent: it is the Claude
# tier's OUTER segment, and it also holds the rules, skills and settings a walk is
# usually there to find -- skipping it would be skipping the point.
MARKER_NAMES = frozenset(tier.segments[-1] for tier in ALL_TIERS)

# What `label` puts in front of a worktree's directory name when it is not in the default
# tier. A forward slash because it cannot occur in a directory name on either platform,
# so a label is unambiguous without carrying a second field.
LABEL_SEP = "/"


def same_dir(left: Path | str, right: Path | str) -> bool:
    """Whether two paths name one directory, without touching the filesystem.

    Case-folded posix, the spelling every path comparison in the harness uses: git prints
    forward slashes on Windows too, and the fold is what makes `C:/Users` and `c:/users` the
    same directory there. Not `resolve()`, so this still answers for a path that is gone.
    """
    return Path(left).as_posix().lower().rstrip("/") == Path(right).as_posix().lower().rstrip("/")


def home_of(tier: Tier, env: dict | None = None) -> Path | None:
    """A detached tier's absolute base; None for a tier anchored at the checkout.

    The env override is read rather than assumed so a test can point a tier somewhere
    real, and so a machine that has moved `CODEX_HOME` is not silently matched against a
    directory it does not use.
    """
    if not tier.detached:
        return None
    source = os.environ if env is None else env
    raw = (source.get(tier.home_env, "") or "").strip() or tier.home_default
    return Path(raw).expanduser()


def match(path: Path | str, env: dict | None = None) -> tuple[Tier, Path, str] | None:
    """`(tier, anchor, name)` when `path` has an agent tier's shape; None when it has not.

    `anchor` is the directory the tier hangs off -- the owning checkout for a nested
    tier, the runtime's home for a detached one, which is why this returns it raw and
    `owning_checkout` is what callers asking about a checkout should use. `name` is the
    worktree's own directory name.

    Pure, and deliberately shape-only: a directory that merely looks like one of these
    is treated as one. The alternative is asking git, which costs a subprocess in a hook
    and returns nothing for a path that has been removed -- and both callers that care
    about the difference already check for a `.git` beside it.
    """
    candidate = Path(path)
    parents = candidate.parents
    for tier in TIERS:
        base = tier.depth - 1
        anchor_at = base + len(tier.segments)
        if len(parents) <= anchor_at:
            continue
        if any(
            parents[base + offset].name != wanted
            for offset, wanted in enumerate(reversed(tier.segments))
        ):
            continue
        anchor = parents[anchor_at]
        home = home_of(tier, env)
        if home is not None and not same_dir(anchor, home):
            continue
        return tier, anchor, candidate.name
    return None


def tier_of(path: Path | str, env: dict | None = None) -> Tier | None:
    """Which agent cut a worktree at `path`, or None when nothing here did."""
    found = match(path, env)
    return None if found is None else found[0]


def is_worktree(path: Path | str, env: dict | None = None) -> bool:
    """Whether `path` is an agent CLI's worktree rather than an ordinary checkout.

    The question the six `install-*.py` guards ask: a scheduled task registered against
    a directory that a `--worktree` session deletes when it is done is a task that fails
    forever, and the failure is invisible because nobody is watching the scheduler.
    """
    return match(path, env) is not None


def is_box(path: Path | str) -> bool:
    """Whether `path` is one of the workspace's ephemeral boxes.

    Shape only, like `match`, and the shape is one directory: a box is whatever sits
    directly under `<workspace>/.worktrees/`. No `env`, because unlike the Codex tier
    this one's location is not configurable -- it is wherever the workspace file is.

    Separate from `is_worktree` rather than folded into it because the two answers lead
    opposite ways. A box is *managed*: it has a lease, a port and a reaper, and the
    remedy for a stale one is `worktree.py reap`. An agent CLI's worktree has none of
    those, so a caller asking "may I install a scheduled task against this directory"
    wants both and a caller asking "what may I `git worktree remove`" wants only the
    second. `tests/support.in_an_ephemeral_box` is the first kind and asks both here
    rather than testing a parent directory's name itself, which is what it used to do.
    """
    return Path(path).parent.name == BOXES_DIR_NAME


def nested_checkout(path: Path | str, env: dict | None = None) -> Path | None:
    """The checkout owning a *nested* worktree; None for anything else, detached included.

    Kept apart from `owning_checkout` because this half is pure and total: it answers for
    a path that no longer exists, which is what a script resolving its own workspace file
    from a directory it is about to leave needs. A detached tier has no such answer and
    returning a wrong one would be worse than returning none.
    """
    found = match(path, env)
    if found is None or found[0].detached:
        return None
    return found[1]


def git_checkout(path: Path | str) -> Path | None:
    """The checkout `path` was cut from, read off its `.git` pointer; None when it is not
    a worktree or the pointer cannot be read.

    A worktree's `.git` is a *file* holding `gitdir: <checkout>/.git/worktrees/<name>`,
    so this is one read rather than a `git` subprocess -- which matters because the
    callers are hooks, and a hook that spawns git on every event is a hook somebody turns
    off. It is also the only thing that answers for a detached tier, and it answers for a
    worktree cut by hand anywhere at all, which no shape in `TIERS` describes.
    """
    line = _pointer_line(path)
    if not line.startswith("gitdir:"):
        return None
    gitdir = Path(line.split(":", 1)[1].strip())
    return next((p.parent for p in gitdir.parents if p.name == ".git"), None)


def _pointer_line(path: Path | str) -> str:
    """A worktree's `.git` file, or "" when there is no readable file there.

    Split out so `git_checkout` reads as three flat steps: every way this can fail --
    a missing path, a `.git` *directory* (an ordinary checkout), an unreadable or
    undecodable file -- is the same answer, and folding them into one place is what
    keeps the parse above from nesting a suppression around a loop.
    """
    with contextlib.suppress(OSError, ValueError):
        pointer = Path(path) / ".git"
        if pointer.is_file():
            return pointer.read_text(encoding="utf-8").strip()
    return ""


def owning_checkout(path: Path | str, env: dict | None = None) -> Path | None:
    """The checkout an agent worktree at `path` belongs to; None when it is not one.

    Arithmetic first, git's pointer second, and the order is the point: the pure answer
    is exact for a nested tier and survives the directory being deleted, while the
    pointer read is what a detached tier has instead of an answer. A caller that gets
    None from this either is not in a worktree or is in one whose checkout cannot be
    determined without the directory being present -- two cases that want the same
    response, which is to treat `path` as its own checkout.
    """
    found = match(path, env)
    if found is None:
        return None
    if not found[0].detached:
        return found[1]
    return git_checkout(path)


def label(path: Path | str, env: dict | None = None) -> str:
    """What a menu calls this worktree: its directory name, qualified when it has to be.

    A checkout can hold worktrees in several tiers at once and the delete dropdown
    resolves a ticked row back to one of them **by this string**, so it has to be unique
    per checkout. Directory names are unique within a tier and nothing makes them unique
    across tiers, so every tier but the default one prefixes its agent -- `codex/fix-320`
    against a bare `fix-320`. The default tier stays unqualified because it is the one
    the harness itself cuts into, and re-labelling every existing row to say `claude/`
    would be churn in the one list an operator reads under time pressure.

    A path in no tier gets its own name back, so this is safe to call on anything.
    """
    found = match(path, env)
    if found is None:
        return Path(path).name
    tier, _anchor, name = found
    return name if tier is DEFAULT_TIER else f"{tier.agent}{LABEL_SEP}{name}"


def default_root(checkout: Path | str) -> Path:
    """Where a *new* worktree for `checkout` is cut -- the default tier's directory.

    One function rather than a constant so the tier list stays the only place a
    directory name is spelled. The default tier is nested by construction: a detached one
    would have to invent the runtime's digest, which is that runtime's business.
    """
    return Path(checkout).joinpath(*DEFAULT_TIER.segments)
