#!/usr/bin/env python3
"""Vendor the shared agent-harness scripts into this project, and drift-check them.

The hook/harness scripts are meant to be identical across projects (see
`.devkit.toml` for the per-project seam). Rather than fork them per repo,
one **shared harness repo is the source of truth** and each project commits a
**vendored copy** -- so cloning a single project still gets everything, with no
submodule. This script keeps that copy honest:

  - `--check` (default): fail (exit 1) if any vendored file drifts from the shared
    repo. Wired into the PR gate. **No-ops clean (exit 0) when the shared repo is
    not configured and this project has never pulled**, so CI is safe before a
    project adopts the shared repo; once `DEVKIT_VERSION` exists, an unresolvable
    source is itself a failure rather than a skip (see `main`).
  - `--pull`: copy the shared repo's version into this project (adopt upstream).
    Also remove exact paths retired by the shared repo; project-owned skill state
    is never included in that retirement list.
  - `--push`: copy this project's version into the shared repo (author a change /
    seed a fresh shared repo from the project that currently owns the code).
  - `--list`: print the manifest and resolved source, then exit.

The source resolves from `--src` or `$DEVKIT_DIR`; `.devkit.toml` stays project-owned.

`MANIFEST` is the reviewed, portable subset; extend it as more scripts are decoupled.
It and the other path lists live in `devkit_manifest.py` beside this file.
Pure helpers are unit-tested in `scripts/hooks/tests/test_sync_devkit.py`.

`BLOCK_MANIFEST` gates a *region* delimited by `<!-- devkit:begin <id> -->` /
`<!-- devkit:end <id> -->` byte-for-byte while
the rest of the file stays the project's own. It exists for `CLAUDE.md`, which can
never join `MANIFEST` -- see the tier's own comment block below for why the policy has
to be inline rather than behind a pointer. Every mode handles both tiers; a block that
cannot be located is reported, never skipped.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

# For `project_settings.py`, which the three functions below import **on use** rather
# than here. A project's very first pull runs the copy of this script the generator
# placed there, at a moment when nothing else in the MANIFEST exists yet -- so an import
# at module scope is a crash in exactly the bootstrap the pull exists for, and that is
# not a hypothesis: it is what `test_a_generated_project_satisfies_the_vendored_ci_contract`
# caught. The insert names *this* copy's directory, so a consumer's `workspace-status.py`
# loading this file by path cannot reach a sibling checkout's tier.
sys.path.insert(0, str(Path(__file__).resolve().parent))

REPO_ROOT = (Path(__file__).parent / "..").resolve()
SRC_ENV = "DEVKIT_DIR"
# Records which shared-repo commit this project's vendored copy corresponds to.
# Committed, per-project (like .devkit.toml), so NOT in MANIFEST.
VERSION_FILE = "DEVKIT_VERSION"
# Receipt of the exact files the last pull managed, with content hashes. Unlike the
# version stamp this is operational: it lets a later manifest retire paths safely
# without growing a permanent tombstone list. It is per-project and never vendored.
RECEIPT_FILE = "DEVKIT_FILES.json"
# The consumer-side pin `--pull` keeps in step with the files it copies. Both are
# per-project files, never vendored. The gate at commit time clones devkit at this
# `rev:` and byte-compares, so a pin that lags the stamp reports every file added
# upstream since as drift -- 38 of them, none of which is the actual problem.
PRECOMMIT_FILE = ".pre-commit-config.yaml"
DEVKIT_REPO = "https://github.com/alexandrec90/devkit"
# The second consumer-side pin, per RELEASING.md: the PR gate checks devkit out at
# this ref and runs `--check` against it. It must move with the pin above and the
# stamp -- bumping it alone turns a green gate red by design, and leaving it behind
# makes CI compare the vendored tree against a revision it was never pulled from.
PR_GATE_FILE = ".github/workflows/pr-gate.yml"
DEVKIT_SLUG = "alexandrec90/devkit"

# Windows only, and load-bearing whenever this script runs unattended.
#
# A process with no console of its own -- `pythonw.exe`, which is how every scheduled
# devkit job runs -- makes Windows allocate a **brand new console window** for each
# console child it spawns. The nightly upgrade pass spawns this script exactly that way,
# so an unflagged `git` here is a window flashing on the desktop for every command a pull
# makes, times every project in the pass. That is what it did: the flag was added to the
# jobs' own spawns and stopped at the process boundary, because a scheduled job's reach
# is longer than the script the scheduler names.
#
# A child that carries the flag gets a console with **no window**, and its own
# descendants inherit that console -- which is why flagging the outermost spawn in each
# script is enough, and why the flag has to be on every script the scheduler can reach.
# It is ignored for a GUI-subsystem child (`pythonw.exe` itself), so the flag on the
# spawn that started this script bought nothing here.
#
# `getattr` supplies zero off Windows, where the flag does not exist.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def console_python() -> str:
    """The console interpreter beside `sys.executable`, for spawning a Python child.

    `NO_WINDOW` is necessary and **not sufficient**. Windows ignores
    `CREATE_NO_WINDOW` for a GUI-subsystem child, so passing the flag alongside
    `pythonw.exe` -- which is what `sys.executable` is under a scheduled job -- leaves
    that child console-*less*, the exact condition that makes Windows open a fresh
    visible console for each of *its* children. Spawn a console interpreter with the
    flag instead and the child gets a hidden console that every descendant inherits.
    Pair the two; neither alone suppresses a window. Identity off Windows, and under
    any session that already has a console.
    """
    executable = Path(sys.executable)
    if executable.name.lower() != "pythonw.exe":
        return sys.executable
    console = executable.with_name("python.exe")
    # An embedded install could ship `pythonw.exe` with no console twin next to it.
    return str(console) if console.exists() else sys.executable


def _load_by_path(name: str, path: Path):
    """The module at `path`, registered as `name`; None when it cannot be loaded.

    By path, not by import, for the two files this script may run without: the
    consumer's own `harness_config.py`, which does not exist yet on a project's first
    `--pull`, and -- on the pull that first delivers it -- `devkit_manifest.py` (see
    below). Registered in `sys.modules` before `exec_module`, because `harness_config` is
    nothing but frozen dataclasses and `@dataclass` looks its defining module up by name
    (`scripts/CLAUDE.md`).
    """
    import importlib.util

    if not path.is_file():
        return None
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    except (OSError, ImportError, SyntaxError, AttributeError, TypeError):
        sys.modules.pop(name, None)
        return None


def resolve_src(arg: str | None, env: Mapping[str, str]) -> Path | None:
    """The shared-repo root from `--src` or `$DEVKIT_DIR`, or None when unset."""
    raw = arg or env.get(SRC_ENV)
    return Path(raw).expanduser().resolve() if raw else None


# --- the path lists -----------------------------------------------------------
# `MANIFEST`, the retired lists and the gated tier live in `devkit_manifest.py`, beside
# this file, and are re-exported here: every function below reads them from THIS
# module's globals at call time, so a test that replaces `sync_devkit.MANIFEST` still
# drives every mode through its own list.
#
# The import can fail exactly once per consumer, and it is the dangerous case. The pull
# that adopts the release which cut the lists out runs the consumer's OLD copy of this
# script, whose inline MANIFEST does not name `devkit_manifest.py` -- so this file
# arrives without it, and `upgrade-project.py`'s next pull runs it. Running on an empty
# or partial list there is not a degraded pull: `remove_receipt_retired` reads every
# receipt entry missing from the list as retired and deletes it, which is every
# vendored file the consumer has. So the source the pull was given supplies the list,
# and with no source there is no list and no run.
try:
    import devkit_manifest as _lists
except ModuleNotFoundError as missing:
    if missing.name != "devkit_manifest":
        raise
    _cli = argparse.ArgumentParser(add_help=False)
    _cli.add_argument("--src")
    _src = resolve_src(_cli.parse_known_args(sys.argv[1:])[0].src, os.environ)
    _path = _src / "scripts" / "devkit_manifest.py" if _src else None
    _loaded = _load_by_path("devkit_manifest", _path) if _path else None
    if _loaded is None:
        _refusal = (
            f"sync-devkit: {Path(__file__).parent / 'devkit_manifest.py'} is missing, and "
            f"no source supplies it ({_path or f'no --src and no ${SRC_ENV}'}). It holds "
            "the MANIFEST; running without it would delete every vendored file. Re-run "
            f"with --src <devkit checkout> or ${SRC_ENV} set, and the pull delivers it."
        )
        if __name__ == "__main__":
            sys.exit(_refusal)
        raise ImportError(_refusal, name="devkit_manifest") from None
    _lists = _loaded

MANIFEST: tuple[str, ...] = _lists.MANIFEST
RETIRED_PATHS: tuple[str, ...] = _lists.RETIRED_PATHS
FRONTEND_GATE: str = _lists.FRONTEND_GATE
DEFAULT_FRONTEND_SRC: str = _lists.DEFAULT_FRONTEND_SRC
GATED_MANIFEST: dict[str, tuple[str, ...]] = _lists.GATED_MANIFEST


def frontend_src(root: Path) -> str:
    """`[frontend] src` for the consumer at `root` when that tier is on; "" when it is
    off, undeclared, or the config helper is not there to read it.

    Normalised to a forward-slash prefix ending in `/`, which is how every path in the
    manifest is spelled, so a gated entry joins onto it without a second spelling.
    """
    module = _load_by_path(
        "_sync_devkit_harness_config",
        Path(__file__).resolve().parent / "hooks" / "harness_config.py",
    )
    if module is None:
        return ""
    try:
        frontend = module.load(root).frontend
    except (OSError, ValueError, AttributeError, TypeError):
        return ""
    if not frontend.enabled:
        return ""
    return str(frontend.src).replace("\\", "/").rstrip("/") + "/"


def gated_paths(root: Path) -> tuple[str, ...]:
    """The gated entries the consumer at `root` should hold, at that project's layout."""
    src = frontend_src(root)
    if not src:
        return ()
    return tuple(src + name for name in GATED_MANIFEST[FRONTEND_GATE])


def source_map(root: Path) -> dict[str, str]:
    """Consumer-relative path -> devkit-relative path, for entries whose layouts differ.

    Empty for every MANIFEST entry, which is vendored at the same path on both sides,
    and empty again for a consumer whose `[frontend] src` happens to be devkit's own
    prefix. It has content only where the gated tier lands somewhere else: a
    single-package project sets `dir = "."` and `src = "src/"`, so the entry it should
    hold is `src/worktreePort.ts` while devkit's copy is at `frontend/src/`.

    Until this existed both sides were probed with the *consumer's* path, so devkit was
    asked for a file it has never had at that name. The pull skipped it as absent and
    the commit gate then reported it MISSING from the shared repo -- which is what
    stopped roguelike adopting v0.11.17 after the release itself had succeeded. The
    comment above predicted exactly this and deferred it as a layout mapping nobody had
    needed yet; roguelike is the consumer that needed it.

    Keyed on the whole path rather than assembled by prefix arithmetic, so a `src` that
    normalises oddly cannot produce a half-formed key that matches nothing.
    """
    src = frontend_src(root)
    if not src or src == DEFAULT_FRONTEND_SRC:
        return {}
    return {src + name: DEFAULT_FRONTEND_SRC + name for name in GATED_MANIFEST[FRONTEND_GATE]}


def gated_source_paths() -> tuple[str, ...]:
    """Where every gated entry lives in devkit, whatever any consumer's layout.

    For the readers that ask "which files does devkit vendor" about devkit itself --
    the unreleased-change check in `new-project.py` and `upgrade-project.py` -- rather
    than about a consumer. devkit's own `.devkit.toml` keeps the frontend tier off, so
    `manifest_for(devkit)` would answer without these and the release check would wave
    an unreleased edit to them through.
    """
    return tuple(DEFAULT_FRONTEND_SRC + name for name in GATED_MANIFEST[FRONTEND_GATE])


def manifest_for(root: Path) -> tuple[str, ...]:
    """Every path devkit vendors into the consumer at `root`: MANIFEST plus its gates.

    `MANIFEST` is read at call time rather than captured, so a test that replaces the
    module attribute still drives every mode through its own list.
    """
    return tuple(MANIFEST) + gated_paths(root)


def read_version(root: Path) -> str | None:
    """The stamped harness commit this project vendored, or None if never pulled."""
    try:
        return (root / VERSION_FILE).read_text().strip() or None
    except OSError:
        return None


def git_head(path: Path) -> str | None:
    """Short HEAD SHA of the git repo at `path`, or None (not a repo / no git)."""
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


def _git_out(path: Path, *args: str) -> str | None:
    """stdout of `git -C path <args>`, or None when git fails or is unavailable."""
    try:
        result = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


def source_tag(path: Path) -> str | None:
    """The tag naming the source's HEAD exactly, or None when HEAD is untagged.

    The pin in a consumer's `.pre-commit-config.yaml` can only name a tag, so an
    untagged HEAD is a revision the consumer is structurally unable to pin. Pulling
    from one produces a project whose files come from a revision its own gate can
    never be pointed at -- which is not a warning, it is an unfixable state.
    """
    return _git_out(path, "describe", "--tags", "--exact-match", "HEAD")


def source_dirty(path: Path) -> bool:
    """True when the source has uncommitted changes.

    A dirty source is worse than an untagged one: the files copied out are not the
    files at HEAD, but the stamp records HEAD anyway. The result passes every
    consistency check that compares a project against *itself* and matches no
    upstream revision that exists. Refusing is the only honest option.
    """
    return bool(_git_out(path, "status", "--porcelain"))


def bump_pin(text: str, tag: str, repo: str = DEVKIT_REPO) -> tuple[str, str | None]:
    """Retarget the `rev:` under `repo:` in a `.pre-commit-config.yaml` to `tag`.

    Returns `(new_text, previous_rev)`; `previous_rev` is None when no pin for
    `repo` was found, which is a project that does not use the published hooks
    rather than an error.

    Deliberately a line-level rewrite rather than a YAML round-trip: the config is
    dense with comments explaining *why* each pin is what it is (carameli's runs to
    fifteen lines), and every YAML library in the stdlib-only budget available here
    would drop them.
    """
    lines = text.splitlines(keepends=True)
    in_repo = False
    previous: str | None = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("- repo:"):
            in_repo = stripped.split("- repo:", 1)[1].strip() == repo
            continue
        if not in_repo or not stripped.startswith("rev:"):
            continue
        # Keep the indentation and any trailing comment: the comment is usually the
        # rationale for pinning a tag at all.
        head, sep, tail = line.partition("rev:")
        value = tail.strip()
        previous = value.split("#", 1)[0].strip()
        comment = f" #{value.split('#', 1)[1]}" if "#" in value else ""
        newline = "\n" if line.endswith("\n") else ""
        lines[index] = f"{head}{sep} {tag}{comment.rstrip()}{newline}"
        break
    return "".join(lines), previous


def bump_gate_ref(text: str, tag: str, slug: str = DEVKIT_SLUG) -> tuple[str, str | None]:
    """Retarget the `ref:` of the workflow step that checks devkit out, to `tag`.

    Returns `(new_text, previous_ref)`. Matches on the `repository: <slug>` line and
    then the next `ref:` in that step, so a workflow that checks out several repos
    only has devkit's moved. Line-level for the same reason as `bump_pin`: the
    surrounding comments carry the ordering constraints the step depends on.
    """
    lines = text.splitlines(keepends=True)
    armed = False
    previous: str | None = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("repository:"):
            armed = stripped.split("repository:", 1)[1].strip() == slug
            continue
        if not armed or not stripped.startswith("ref:"):
            continue
        head, sep, tail = line.partition("ref:")
        value = tail.strip()
        previous = value.split("#", 1)[0].strip()
        comment = f" #{value.split('#', 1)[1]}" if "#" in value else ""
        newline = "\n" if line.endswith("\n") else ""
        lines[index] = f"{head}{sep} {tag}{comment.rstrip()}{newline}"
        break
    return "".join(lines), previous


def _retarget(root: Path, rel: str, tag: str, bump) -> None:
    """Apply a pin rewrite to `root/rel`, reporting what moved. Absent file is fine:
    not every project has a PR gate, and pre-adoption neither file exists."""
    path = root / rel
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    updated, previous = bump(text, tag)
    if previous is None:
        print(f"  (no devkit pin in {rel}; nothing to bump)")
    elif previous == tag:
        print(f"  {rel} already at {tag}")
    else:
        path.write_text(updated, encoding="utf-8", newline="\n")
        print(f"  bumped {rel}: {previous} -> {tag}")


def read_pin(text: str, repo: str = DEVKIT_REPO) -> str | None:
    """The `rev:` currently pinned for `repo`, or None when there is no such pin."""
    return bump_pin(text, "", repo)[1] or None


def stale_pin(root: Path) -> tuple[str, str] | None:
    """`(pinned, vendored_tag)` when the pin and the vendored release disagree.

    The single check that would have named carameli's failure immediately. Both
    inputs are per-project and committed, so this needs no network and no source
    checkout -- it is a comparison the project can make against itself.

    Compares the pin against the *receipt's* recorded tag, not against
    `DEVKIT_VERSION`: that file holds a SHA by contract, and a SHA never equals a
    tag, so comparing it would report every project as stale forever.

    Returns None when the tag was never recorded -- a pull from before this existed,
    or an `--allow-untagged` one. "Cannot tell" is not "stale", and a check that
    cried wolf on every un-upgraded project would be ignored within a week.
    """
    try:
        pinned = read_pin((root / PRECOMMIT_FILE).read_text(encoding="utf-8"))
    except OSError:
        return None
    vendored = read_receipt_tag(root)
    if not pinned or not vendored or pinned == vendored:
        return None
    return pinned, vendored


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _read_text(path: Path) -> str | None:
    """UTF-8 text of `path`, or None when absent/unreadable.

    `newline=""` disables universal-newline translation, so the host file's own line
    endings survive a round trip. Without it, splicing a block into a CRLF file would
    rewrite the other four hundred lines to LF and report the whole file as changed --
    on Windows, on every pull. Passed to `open` rather than `Path.read_text` because
    that keyword only reached `Path` in 3.13 and the floor here is 3.12.
    """
    with (
        contextlib.suppress(OSError, UnicodeDecodeError),
        path.open(encoding="utf-8", newline="") as handle,
    ):
        return handle.read()
    return None


def _write_text(path: Path, text: str) -> None:
    """Counterpart to `_read_text`; writes bytes through unchanged."""
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def _sha256(path: Path) -> str | None:
    data = _read(path)
    return hashlib.sha256(data).hexdigest() if data is not None else None


def read_receipt(root: Path) -> dict[str, str]:
    """Return {managed path: sha256} from the last pull; malformed receipts are empty."""
    try:
        raw = json.loads((root / RECEIPT_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    files = raw.get("files") if isinstance(raw, dict) else None
    if not isinstance(files, dict):
        return {}
    return {
        path: digest
        for path, digest in files.items()
        if isinstance(path, str) and isinstance(digest, str)
    }


def read_receipt_tag(root: Path) -> str:
    """The release tag the last pull vendored from; "" when unrecorded.

    Lives here rather than in `DEVKIT_VERSION` because that file has a contract:
    it records the upstream short **SHA**, and the vendored
    `test_harness_version_records_a_commit` asserts a 7-40 char hex value. Stamping
    a tag there broke that contract and had to be corrected by hand in a consumer.

    But the *pin* can only ever be a tag, so comparing the two needs the tag
    recorded somewhere. The receipt is already written by every pull, already
    per-project, and already never vendored -- so it is the honest home for it.
    """
    try:
        raw = json.loads((root / RECEIPT_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return ""
    tag = raw.get("devkit_tag") if isinstance(raw, dict) else None
    return tag if isinstance(tag, str) else ""


def write_receipt(root: Path, manifest: tuple[str, ...], tag: str = "") -> None:
    files = {rel: digest for rel in manifest if (digest := _sha256(root / rel)) is not None}
    payload: dict[str, object] = {"version": 1, "files": files}
    if tag:
        payload["devkit_tag"] = tag
    (root / RECEIPT_FILE).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def classify(
    src: Path, repo_root: Path, manifest: tuple[str, ...]
) -> tuple[list[str], list[str], list[str]]:
    """Partition the manifest into (drifted, missing_in_src, ok) by byte comparison.

    `missing_in_src` are files absent from the shared repo (it does not have them
    yet -- e.g. before a first `--push`); they are reported, never silently OK.

    The shared repo is probed at *its* spelling of each path, which differs from the
    consumer's only for the gated tier (`source_map`). Probing it at the consumer's
    spelling is what made a project whose `[frontend] src` is not devkit's own report
    two files as MISSING from a repo that has always had them.
    """
    sources = source_map(repo_root)
    drifted: list[str] = []
    missing: list[str] = []
    ok: list[str] = []
    for rel in manifest:
        src_bytes = _read(src / sources.get(rel, rel))
        if src_bytes is None:
            missing.append(rel)
            continue
        if _read(repo_root / rel) == src_bytes:
            ok.append(rel)
        else:
            drifted.append(rel)
    return drifted, missing, ok


# --- the marker-block tier ----------------------------------------------------
# MANIFEST vendors whole files. That covers everything a project does not own, and
# cannot cover the one file the agents read first: `CLAUDE.md` is per-project by
# definition, so a whole-file compare would report each project's own prose as drift.
#
# Pointing at a vendored rule file instead -- what devkit did -- is a Claude-only
# convention. Codex has no `.claude/rules/`, reads straight past it, and so runs on a
# different text; an eval calibrated against one arrangement then says nothing about
# the other. Inlining the policy fixes that and brings back the original problem: an
# ungated copy inside a per-project file drifts freely while reading as authoritative,
# which is the exact failure the vendored instruction tier was built to end.
#
# So the vendored unit here is a *region*. Between the markers devkit owns the bytes
# and `--check` gates them exactly as it gates a MANIFEST file; outside them the
# project writes what it likes. Same guarantee, applied to a slice of a file.
#
# HTML comments, so the markers are invisible in rendered Markdown and inert as
# instructions -- an agent reading the file sees policy, not bookkeeping.
BLOCK_BEGIN = "<!-- devkit:begin {block_id} -->"
BLOCK_END = "<!-- devkit:end {block_id} -->"

# (host file, block id). Hosts are per-project and deliberately absent from MANIFEST.
# Empty until `.claude/rules/engineering.md` moves inline: shipping the mechanism
# first keeps that migration a single reviewable content change, and an empty tuple
# is inert in every mode. The helpers below are unit-tested regardless, so the tier
# is not first exercised in a consumer.
BLOCK_MANIFEST: tuple[tuple[str, str], ...] = ()


def block_markers(block_id: str) -> tuple[str, str]:
    """The literal begin/end markers delimiting `block_id`."""
    return BLOCK_BEGIN.format(block_id=block_id), BLOCK_END.format(block_id=block_id)


def find_block(text: str, block_id: str) -> tuple[tuple[int, int] | None, str]:
    """Offsets of the content between `block_id`'s markers: `(span, "")` on success.

    On failure returns `(None, reason)`. Every caller reports the reason rather than
    treating it as "nothing to do": a block that cannot be located is policy this file
    is not carrying, and the quietest way to lose a gate is to let that read as OK.
    """
    begin, end = block_markers(block_id)
    if text.count(begin) > 1 or text.count(end) > 1:
        return None, f"duplicate '{block_id}' markers"
    start, stop = text.find(begin), text.find(end)
    if start < 0 and stop < 0:
        return None, f"no '{block_id}' block"
    if start < 0:
        return None, f"'{block_id}' end marker with no begin"
    if stop < 0:
        return None, f"'{block_id}' begin marker with no end"
    if stop < start:
        return None, f"'{block_id}' markers are inverted"
    return (start + len(begin), stop), ""


def extract_block(text: str, block_id: str) -> str | None:
    """The region's exact characters, or None when the block is unusable."""
    span, _ = find_block(text, block_id)
    return None if span is None else text[span[0] : span[1]]


def replace_block(text: str, block_id: str, content: str) -> tuple[str, str]:
    """`text` with `block_id`'s region replaced by `content`; `("", reason)` on failure.

    Splices verbatim -- no reindenting, no newline normalisation -- so a pulled block
    is character-identical to its source and `--check` is stable immediately after a
    `--pull`. A round trip that "tidied" the content would report drift it created.
    """
    span, problem = find_block(text, block_id)
    if span is None:
        return "", problem
    return text[: span[0]] + content + text[span[1] :], ""


def classify_blocks(
    src: Path, repo_root: Path, blocks: tuple[tuple[str, str], ...]
) -> tuple[list[str], list[str], list[str]]:
    """Partition `blocks` into (drifted, unusable, ok), mirroring `classify`.

    `unusable` collects every way a block fails to be comparable -- host file absent
    on either side, markers missing or malformed on either side. Reported, never
    counted OK, for the reason in `find_block`.
    """
    drifted: list[str] = []
    unusable: list[str] = []
    ok: list[str] = []
    for host, block_id in blocks:
        label = f"{host}#{block_id}"
        src_text, dest_text = _read_text(src / host), _read_text(repo_root / host)
        if src_text is None or dest_text is None:
            side = "shared repo" if src_text is None else "this project"
            unusable.append(f"{label} ({host} absent in {side})")
            continue
        src_content = extract_block(src_text, block_id)
        dest_content = extract_block(dest_text, block_id)
        if src_content is None:
            unusable.append(f"{label} (shared repo: {find_block(src_text, block_id)[1]})")
        elif dest_content is None:
            unusable.append(f"{label} (this project: {find_block(dest_text, block_id)[1]})")
        elif src_content == dest_content:
            ok.append(label)
        else:
            drifted.append(label)
    return drifted, unusable, ok


def sync_blocks(
    from_root: Path, to_root: Path, blocks: tuple[tuple[str, str], ...]
) -> tuple[list[str], list[str]]:
    """Splice each block from_root -> to_root. Returns (written, failed-with-reason).

    A failure leaves the host file untouched and is returned for reporting, never
    swallowed: a block that did not land is policy the destination is not carrying,
    and `--pull` claiming success would hide it until the next `--check`.
    """
    written: list[str] = []
    failed: list[str] = []
    for host, block_id in blocks:
        label = f"{host}#{block_id}"
        source, target = _read_text(from_root / host), _read_text(to_root / host)
        if source is None or target is None:
            side = "source" if source is None else "destination"
            failed.append(f"{label} ({host} absent in {side})")
            continue
        content = extract_block(source, block_id)
        if content is None:
            failed.append(f"{label} (source: {find_block(source, block_id)[1]})")
            continue
        spliced, problem = replace_block(target, block_id, content)
        if problem:
            failed.append(f"{label} (destination: {problem})")
            continue
        if spliced != target:
            _write_text(to_root / host, spliced)
        written.append(label)
    return written, failed


def copy_ends(rel: str, sources: Mapping[str, str], pull: bool) -> tuple[str, str]:
    """`(from_rel, to_rel)` for one entry, given the direction.

    `manifest` is spelled at the consumer's layout throughout, because the receipt and
    the retired sweep are about this project's own files. Only the devkit end is ever
    remapped, and which end that is is the whole of the direction.
    """
    devkit_rel = sources.get(rel, rel)
    return (devkit_rel, rel) if pull else (rel, devkit_rel)


def _copy(from_rel: str, to_rel: str, from_root: Path, to_root: Path) -> bool:
    """Copy one manifest file from_root -> to_root. False when the source is absent.

    Two relative paths rather than one, because the gated tier does not live at the same
    place on both sides -- see `source_map`. They are equal for every other entry.
    """
    source = from_root / from_rel
    if not source.exists():
        return False
    dest = to_root / to_rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    return True


_BYTECODE_CACHE = "__pycache__"
_BYTECODE_SUFFIXES = frozenset({".pyc", ".pyo"})


def _prune_dir(parent: Path) -> None:
    """Remove `parent` once the last file devkit managed there is gone.

    This replaces a bare `rmdir()`, which silently failed whenever a `__pycache__/`
    survived -- the usual case, since every retired skill that shipped a `.py` had run
    at least once. Nothing catches the leftover: the cache is gitignored, so an empty
    husk is invisible to `git status`, to the drift gate, and to a reviewer. Carameli
    carried nine of them for months after the skills themselves were retired.

    The cache is deleted only when it is the *sole* remaining entry and holds nothing
    but bytecode. Beside a surviving source file it is live, and any other sibling --
    a `state.json`, a project-owned `check-specs.json` -- means the directory is still
    someone's, which retirement does not get to overrule.
    """
    with contextlib.suppress(OSError):
        if [item.name for item in parent.iterdir()] == [_BYTECODE_CACHE]:
            cache = parent / _BYTECODE_CACHE
            if all(item.suffix in _BYTECODE_SUFFIXES for item in cache.iterdir()):
                shutil.rmtree(cache)
        parent.rmdir()


def retired_present(root: Path) -> list[str]:
    return [rel for rel in RETIRED_PATHS if (root / rel).is_file()]


# The settings file is the project's own (never vendored — see CLAUDE.md), which is
# exactly why `--pull` has to touch it: the pull unwires every agent hook, an edit to a
# file no MANIFEST covers. That
# tier lives in `project_settings.py` — a different contract from copying, and this
# module was past every structural limit it holds other files to. Spelled out here
# rather than read from there because this module must load before that file exists
# (see the sys.path comment at the top); `test_the_two_modules_name_the_same_settings
# _file` is what stops the two copies drifting.
SETTINGS_FILE = ".claude/settings.json"


def retired_hook_paths(retired: tuple[str, ...] | None = None) -> tuple[str, ...]:
    """`project_settings.retired_hook_paths`, over this module's retired list.

    Kept here, thin, because `retired=None` must read `RETIRED_PATHS` **at call time**:
    `workspace-status.py` loads this module by path and its test replaces that list on
    the loaded module to prove the names are read from here rather than copied. A
    default captured at def time silently ignores that, and the test would have been
    asserting nothing.

    Empty when the tier is not on disk yet, which is the bootstrap pull and nothing
    else: a copy that has vendored the MANIFEST and lost this file is drift, and
    `--check` reports it as drift rather than through this.
    """
    try:
        import project_settings
    except ModuleNotFoundError:
        return ()
    return project_settings.retired_hook_paths(RETIRED_PATHS if retired is None else retired)


def settings_pass(root: Path, retired: tuple[str, ...] | None = None) -> list[str]:
    """The pull's pass over this project's settings file; one note per change made.

    Unwires every agent hook the settings still carry. `project_settings.settings_pass`
    owns that, and the notes it returns are already worded for the report below.
    """
    try:
        import project_settings
    except ModuleNotFoundError:
        return []
    return project_settings.settings_pass(root, retired_hook_paths(retired))


def local_faults(root: Path) -> tuple[list[tuple[str, str]], str]:
    """`(faults, summary)` for what `--check` finds outside the MANIFEST.

    The generated Codex mirror is never compared against upstream, so it is not drift --
    but Codex reads it rather than the settings it came from, so a stale one is reported.

    Returns the summary already worded, so `main` neither grows a branch per fault nor
    words it there; both are what pushed that function past its limits.
    """
    try:
        import project_settings
    except ModuleNotFoundError:
        return [], ""
    faults = project_settings.check_notes(root, CODEX_HOOKS_FILE, codex_hooks_stale(root))
    return faults, project_settings.check_summary(faults)


CODEX_HOOKS_FILE = ".codex/hooks.json"
CODEX_GENERATOR = "scripts/sync-codex-hooks.py"


def _codex_generator(root: Path, *flags: str) -> list[str]:
    return [
        console_python(),
        str(root / CODEX_GENERATOR),
        *flags,
        str(root / SETTINGS_FILE),
        str(root / CODEX_HOOKS_FILE),
    ]


def codex_hooks_stale(root: Path) -> bool:
    """Whether this project's committed `.codex/hooks.json` is what it would generate.

    The generator is vendored; **its output is not**, and that asymmetry is the whole
    reason this exists. `--pull` copies `sync-codex-hooks.py`, so a fix to what Codex
    should be running lands in every consumer — and changes nothing, because the file
    Codex actually reads was written by the *previous* generator and no gate on either
    side ever looked at it. `regenerate_codex_hooks` closes it for a project that pulls;
    this closes it for one whose file went stale some other way, so the PR gate says so
    instead of a Codex session discovering it a blocked command at a time.

    Runs the generator out-of-process rather than importing it: these are sibling
    scripts at fixed vendored paths, and `sync-codex-context.py` already invokes it the
    same way. Best-effort — a generator that is absent or that raises leaves this
    reporting nothing, because a project with no `.codex/` has nothing to be stale.
    """
    if not (root / CODEX_HOOKS_FILE).is_file() or not (root / CODEX_GENERATOR).is_file():
        return False
    try:
        result = subprocess.run(
            _codex_generator(root, "--check"),
            capture_output=True,
            text=True,
            creationflags=NO_WINDOW,
        )
    except OSError:
        return False
    return result.returncode == 1


def regenerate_codex_hooks(root: Path) -> bool:
    """Rewrite `.codex/hooks.json` from the just-pulled generator. True if it changed.

    Only for a project that already has the file: generating one for a project that
    never opted into `.codex/` would wire Codex hooks nobody asked for.
    """
    if not codex_hooks_stale(root):
        return False
    try:
        # Captured and re-emitted rather than left to inherit. `NO_WINDOW` binds a child
        # that captures nothing to the console it was just given, so the flag alone
        # silently takes whatever the generator says out of a `--pull` a human is
        # reading. Both streams, and without asking what today's generator prints: that
        # is the generator's business to change, and this is the only place it could be
        # lost.
        result = subprocess.run(
            _codex_generator(root), capture_output=True, text=True, creationflags=NO_WINDOW
        )
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        return result.returncode == 0
    except OSError:
        return False


# Not in MANIFEST, and it never will be: the file's content is a fact about one repo.
# The name is shared so a pull can tell "already adopted" from "adopting now".
UNTESTED_BASELINE_FILE = ".devkit-untested.txt"


def seed_untested_baseline(root: Path) -> int | None:
    """Record this project's existing untested symbols on adoption. `None` if not.

    The ratchet's baseline is the debt a repo already has, and until it exists every
    public callable in the project reads as new debt. Seeding it here is what makes
    adopting the gate a no-op instead of a red PR gate on the pull that delivers it —
    and it has to be *here* rather than a step someone runs afterwards, because the
    someone is the thing that does not happen.

    The scanner refuses to overwrite, so this is safe to call on every pull: a project
    that has already adopted keeps its list, and new untested code cannot be laundered
    into it. Out-of-process for the same reason `regenerate_codex_hooks` is: the file
    was copied in a moment ago, and running it as a subprocess means a broken one fails
    visibly here rather than at this script's import.
    """
    script = root / "scripts/hooks/untested_symbols.py"
    if not script.is_file() or (root / UNTESTED_BASELINE_FILE).exists():
        return None
    try:
        result = subprocess.run(
            # `console_python()`, not `sys.executable`: an upgrade sweep runs this from
            # a scheduled job under `pythonw.exe`, where a GUI-subsystem child ignores
            # CREATE_NO_WINDOW and Windows gives its children visible consoles instead.
            [console_python(), str(script), "--seed"],
            cwd=str(root),
            capture_output=True,
            text=True,
            creationflags=NO_WINDOW,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return len(read_untested_baseline(root))


# The structural ratchet's twin of the file above, on the same terms: never vendored,
# shared name so a pull can tell adoption from a repeat.
STRUCTURE_BASELINE_FILE = ".devkit-structure.txt"


def seed_structure_baseline(root: Path) -> int | None:
    """Record this project's existing structural debt on adoption. `None` if not.

    Same argument as `seed_untested_baseline`: until the baseline exists every
    oversized function and every `# noqa` reads as new debt, so the pull that delivers
    the gate would redden the gate. The scanner refuses to overwrite, so calling this
    on every pull is safe, and it runs out of process for the same reason.
    """
    script = root / "scripts/hooks/structure_check.py"
    if not script.is_file() or (root / STRUCTURE_BASELINE_FILE).exists():
        return None
    try:
        result = subprocess.run(
            [console_python(), str(script), "--seed"],
            cwd=str(root),
            capture_output=True,
            text=True,
            creationflags=NO_WINDOW,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return len(read_structure_baseline(root))


def tighten_structure_baseline(root: Path) -> tuple[int, int] | None:
    """Drop from an existing baseline what the code no longer earns. `None` if not run.

    The seed's mirror, for every pull after the first. A release that shrinks a vendored
    module -- or, as v0.11.15 did, stops scanning vendored paths at all -- leaves the
    consumer's baseline holding numbers its code no longer earns, and the vendored
    `test_the_baseline_holds_only_what_the_code_still_earns` reddens the adoption PR on
    files the consumer never edited. Three releases running were re-tightened by hand.
    `--tighten` only drops and lowers, so this can only shrink the debt a pull records,
    and it runs out of process for the seed's reason: it is the scanner this pull just
    delivered. `(dropped, lowered)` is read off the file rather than the exit code,
    because the checker judges after it tightens -- debt the consumer added itself is
    still its gate's to report, not a reason for the pull to leave the stale lines in.
    """
    script = root / "scripts/hooks/structure_check.py"
    if not script.is_file() or not (root / STRUCTURE_BASELINE_FILE).is_file():
        return None
    before = structure_baseline_values(root)
    try:
        result = subprocess.run(
            [console_python(), str(script), "--tighten"],
            cwd=str(root),
            capture_output=True,
            text=True,
            creationflags=NO_WINDOW,
        )
    except OSError:
        return None
    after = structure_baseline_values(root)
    dropped = sum(1 for key in before if key not in after)
    lowered = sum(1 for key, value in before.items() if key in after and after[key] < value)
    # A red exit that moved nothing is a checker that did not get as far as tightening
    # (a config error, a crash), not a baseline with nothing to drop.
    if result.returncode != 0 and not (dropped or lowered):
        return None
    return dropped, lowered


def structure_baseline_values(root: Path) -> dict[str, int]:
    """`key -> recorded value` for every finding line, so a tighten can be measured."""
    values: dict[str, int] = {}
    for line in read_structure_baseline(root):
        key, sep, value = line.rpartition(" = ")
        if sep and value.strip().isdigit():
            values[key.strip()] = int(value)
    return values


def read_structure_baseline(root: Path) -> list[str]:
    """The recorded findings, so a pull can report how much debt it just wrote down."""
    path = root / STRUCTURE_BASELINE_FILE
    if not path.is_file():
        return []
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def read_untested_baseline(root: Path) -> list[str]:
    """The recorded gaps, so a pull can report how much debt it just wrote down."""
    path = root / UNTESTED_BASELINE_FILE
    if not path.is_file():
        return []
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def remove_retired(root: Path) -> list[str]:
    """Delete only reviewed retired files, never project-owned sibling state."""
    removed = retired_present(root)
    for rel in removed:
        (root / rel).unlink()
    # Sweep every retired path's directory, not only those that still held a file.
    # A path retired several releases ago -- or whose file went by hand, as carameli's
    # did in a sweep commit -- leaves the same husk, and `retired_present` cannot see
    # it because there is no longer a file to match. Pruning the whole list makes the
    # cleanup idempotent rather than one-shot, so a project that missed the release
    # that retired something still gets it tidied on its next pull.
    for rel in RETIRED_PATHS:
        _prune_dir((root / rel).parent)
    return removed


def receipt_retired_present(root: Path, manifest: tuple[str, ...]) -> list[str]:
    current = set(manifest)
    return sorted(
        rel for rel in read_receipt(root) if rel not in current and (root / rel).is_file()
    )


def template_outputs(src: Path | None) -> set[str]:
    """Every project-relative path the source's `templates/` tier can produce.

    The `dot-` prefix on a path component is the generator's spelling of a leading dot
    (`dot-github/workflows/pr-gate.yml.tmpl` -> `.github/workflows/pr-gate.yml`), and a
    trailing `.tmpl` marks a file that is rendered rather than copied. Both are stripped
    here so the result is directly comparable with a MANIFEST path.

    Best effort by design: a source with no `templates/` (an older devkit, or a consumer
    pushing back) yields the empty set, and every caller treats that as "cannot tell".
    """
    if src is None:
        return set()
    root = src / "templates"
    if not root.is_dir():
        return set()
    outputs: set[str] = set()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        # `templates/<preset>/...` -- the preset directory is not part of the output.
        parts = path.relative_to(root).parts[1:]
        if not parts:
            continue
        renamed = [
            part[4:] and f".{part[4:]}" if part.startswith("dot-") else part for part in parts
        ]
        renamed[-1] = renamed[-1].removesuffix(".tmpl")
        outputs.add("/".join(renamed))
    return outputs


def remove_receipt_retired(
    root: Path, manifest: tuple[str, ...], src: Path | None = None
) -> tuple[list[str], list[str], list[str]]:
    """Remove no-longer-managed files. `(removed, preserved, unvendored)`.

    Three ways a path in the receipt can be absent from the MANIFEST, and only one of
    them is a deletion:

    - **Locally edited.** Preserved, as before: the sha no longer matches what the last
      pull wrote, so someone owns it now.
    - **Un-vendored** -- it left the MANIFEST because it became a `templates/` file.
      **Preserved**, and this is the fix. Deleting it was silent data loss: `templates/`
      is a one-shot copy consulted only by `new-project.py`, so nothing put the file
      back and nothing on either side reported the gap. When
      `.github/actions/setup-python-env/action.yml` was un-vendored, the pull deleted it
      from every consumer that had *not* customised it -- and customising it was the
      only thing that saved the two that survived, by accident of the sha check above.
      Two projects' PR gates then died on an unresolvable local action, and a third's
      nightly failed silently for a week because only its nightly used the action.
    - **Retired.** Genuinely obsolete, nothing should have it. Deleted, as before.

    The distinction needs the source, so `src=None` (a caller that cannot supply one)
    falls back to the old behaviour rather than preserving everything -- a receipt entry
    that is neither in the MANIFEST nor in `templates/` really is retired, and never
    tidying those would leave a consumer accumulating dead files forever.
    """
    receipt = read_receipt(root)
    current = set(manifest)
    templates = template_outputs(src)
    removed: list[str] = []
    preserved: list[str] = []
    unvendored: list[str] = []
    for rel, expected in sorted(receipt.items()):
        path = root / rel
        if rel in current or not path.is_file():
            continue
        if rel in templates:
            unvendored.append(rel)
            continue
        if _sha256(path) != expected:
            preserved.append(rel)
            continue
        path.unlink()
        removed.append(rel)
        _prune_dir(path.parent)
    return removed, preserved, unvendored


def build_parser() -> argparse.ArgumentParser:
    """The CLI. Split out of `main` with the four mode bodies below it.

    Seven consecutive raises of this module's structural baseline recorded the same
    deferral, each citing the bootstrap constraint: `sync-devkit.py` is copied ALONE
    into a project for its first `--pull`, before any sibling exists, so it cannot
    import one and the usual remedy -- a second module -- is unavailable.

    That constraint was never the reason `main` was 271 lines at complexity 74. Of
    the 53 definitions here the other 52 average 14 lines; the shape was one
    function holding four independent modes, and splitting *within the file* costs
    the bootstrap nothing. `file_lines` barely moves and `definitions` rises, which
    is the trade the 2026-09-06 record already named: the per-function half is the
    one worth winning.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="fail on drift (default)")
    mode.add_argument("--pull", action="store_true", help="copy shared repo -> this project")
    mode.add_argument("--push", action="store_true", help="copy this project -> shared repo")
    mode.add_argument("--list", action="store_true", help="print manifest + source, then exit")
    parser.add_argument("--src", help=f"shared harness repo root (else ${SRC_ENV})")
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help=(
            "--pull from a source with uncommitted changes, stamping the version "
            "'<rev>-dirty'. For iterating on devkit locally; never for an upgrade"
        ),
    )
    parser.add_argument(
        "--allow-untagged",
        action="store_true",
        help=(
            "--pull from an untagged source. Leaves the .pre-commit-config.yaml pin "
            "stale, because there is no tag to move it to"
        ),
    )
    return parser


def run_list(src: Path | None, manifest: tuple[str, ...]) -> int:
    """`--list`: the manifest and where it would be compared against, then stop."""
    print(f"source: {src or '(unset)'}")
    print(f"vendored version: {read_version(REPO_ROOT) or '(never pulled)'}")
    for rel in manifest:
        print(f"  {rel}")
    if BLOCK_MANIFEST:
        print("vendored blocks (regions of per-project files):")
        for host, block_id in BLOCK_MANIFEST:
            print(f"  {host}#{block_id}")
    if RETIRED_PATHS:
        print("retired on pull:")
        for rel in RETIRED_PATHS:
            print(f"  {rel}")
    return 0


def unconfigured_verdict() -> int:
    """What an unset `$DEVKIT_DIR` means, which depends entirely on the stamp."""
    # Unconfigured. Before adoption that is correct and every mode no-ops clean, so a
    # project generated an hour ago -- no vendored files, and nothing to compare them
    # against -- passes its own PR gate.
    #
    # After adoption the same silence is a lie. A stamped project HAS vendored files,
    # `--check` is the gate over them, and exit 0 reports a comparison that never ran.
    #
    # The stamp is the one signal that separates the two, and the reason it works is
    # that it is committed. `$DEVKIT_DIR` is a property of the *machine* -- a second
    # workstation, a fresh clone, a CI job whose `env:` block was dropped -- and every
    # one of those is a place where the gate goes quiet exactly when the project has
    # the most vendored code to compare. `DEVKIT_VERSION` travels with the repo, so it
    # can tell "not adopted yet" from "adopted, and this machine cannot check it".
    stamped = read_version(REPO_ROOT)
    if stamped is None:
        print(f"sync-harness: ${SRC_ENV} unset and no --src; nothing to do (skipping).")
        return 0
    print(
        f"sync-harness: this project vendors devkit ({VERSION_FILE} = {stamped}), but "
        f"${SRC_ENV} is unset and no --src was given. There is nothing to compare "
        f"against, so NOTHING WAS CHECKED.\n"
        f"  point it at a devkit checkout:  {SRC_ENV}=<path> python scripts/sync-devkit.py\n"
        f"  or drift-check without a clone: pre-commit run devkit-drift --all-files\n"
        f"(that hook compares against the devkit rev pinned in {PRECOMMIT_FILE}, so it "
        f"needs no ${SRC_ENV} and no local devkit at all)"
    )
    return 1


def pull_refusal(src: Path, allow_dirty: bool, allow_untagged: bool) -> int:
    """`2` when this source may not be pulled from, `0` when it may.

    Both guards run *before* anything is copied: a refusal must leave the project
    exactly as it was, not half-upgraded with a stamp to match.
    """
    # Both guards run *before* anything is copied: a refusal must leave the
    # project exactly as it was, not half-upgraded with a stamp to match.
    if source_dirty(src) and not allow_dirty:
        print(
            f"sync-harness: {src} has uncommitted changes. Pulling from it copies "
            f"files that are at no upstream revision, while the stamp claims HEAD "
            f"-- the state that made a consumer's drift gate unfixable. Commit "
            f"there first, or pass --allow-dirty to stamp it as provisional.",
            file=sys.stderr,
        )
        return 2
    tag = source_tag(src)
    if tag is None and not allow_untagged:
        print(
            f"sync-harness: {src} HEAD is not tagged. A consumer pins devkit by "
            f"tag, so an untagged pull can never be pinned to what it vendored. "
            f"Tag the release first, or pass --allow-untagged to pull anyway "
            f"(leaving {PRECOMMIT_FILE} stale -- and the drift gate red).",
            file=sys.stderr,
        )
        return 2
    return 0


@dataclass(frozen=True)
class SyncOutcome:
    """What one `--pull`/`--push` did, separated from saying so.

    The copy is a sequence of side effects whose *order* is the whole correctness
    argument -- deletions before the settings prune, the prune before the Codex
    regeneration, the stamp before the baseline seeds -- and it used to be interleaved
    with twenty print statements enforcing no order at all. Splitting the two is what
    takes `run_sync` off the structural baseline, and it also means the ordering
    comments now sit in a function short enough to read end to end.
    """

    copied: list[str]
    skipped: list[str]
    removed: list[str]
    preserved: list[str]
    unvendored: list[str]
    unwired: list[str]
    blocks_written: list[str]
    blocks_failed: list[str]
    codex_regenerated: bool
    seeded: int | None
    seeded_structure: int | None
    tightened: tuple[int, int] | None
    pull: bool


def copy_manifest(
    src: Path, manifest: tuple[str, ...], *, pull: bool
) -> tuple[list[str], list[str], list[str]]:
    """`(copied, blocks written, blocks that could not be spliced)`, either direction.

    The one genuinely symmetric half: the ends swap and nothing else does. Everything
    a pull additionally does -- deleting, pruning, stamping, seeding, pinning -- a push
    has no business doing, which is why the two callers below share only this.
    """
    from_root, to_root = (src, REPO_ROOT) if pull else (REPO_ROOT, src)
    sources = source_map(REPO_ROOT)
    copied = [rel for rel in manifest if _copy(*copy_ends(rel, sources, pull), from_root, to_root)]
    blocks_written, blocks_failed = sync_blocks(from_root, to_root, BLOCK_MANIFEST)
    return copied, list(blocks_written), list(blocks_failed)


def apply_push(src: Path, manifest: tuple[str, ...]) -> SyncOutcome:
    """`--push`: copy this project's files up, and stop.

    No deletions, no stamp, no seeds, no pin. A push is a devkit author sending one
    change home; every other side effect in `apply_pull` describes a consumer adopting
    a release, and doing any of them here would have this project rewrite the source.
    """
    copied, blocks_written, blocks_failed = copy_manifest(src, manifest, pull=False)
    return SyncOutcome(
        copied=copied,
        skipped=[rel for rel in manifest if rel not in copied],
        removed=[],
        preserved=[],
        unvendored=[],
        unwired=[],
        blocks_written=blocks_written,
        blocks_failed=blocks_failed,
        codex_regenerated=False,
        seeded=None,
        seeded_structure=None,
        tightened=None,
        pull=False,
    )


def apply_pull(src: Path, manifest: tuple[str, ...]) -> SyncOutcome:
    """`--pull`: copy upstream's files down, then every side effect adoption needs.

    **The order is the correctness argument**, and each step's comment says what it is
    ordered against. Written as a straight line rather than as `apply_push` with nine
    `if pull` ternaries hung off it, which is what it was: the ternaries carried the
    ordering constraints past the eye and cost complexity 32 in a function whose
    sequence is the only thing a reader is here for.
    """
    managed_removed, preserved, unvendored = remove_receipt_retired(REPO_ROOT, manifest, src)
    copied, blocks_written, blocks_failed = copy_manifest(src, manifest, pull=True)
    removed = remove_retired(REPO_ROOT) + managed_removed
    # After the deletions, never before: pruning a hook whose script survived the
    # pull would disable a live hook.
    unwired = settings_pass(REPO_ROOT)
    # After the settings pass, never before: the Codex file is generated *from*
    # those settings, so regenerating first would bake back in whatever the prune
    # is about to remove.
    codex_regenerated = regenerate_codex_hooks(REPO_ROOT)
    # Before the seeds: `structure_check.vendored_paths` keys off this file, so a
    # baseline seeded while the stamp is absent grandfathers every vendored module
    # into the consumer's numbers -- and once the stamp lands the gate stops scanning
    # them, so all 49 keys read as stale and a generated project is red on arrival.
    (REPO_ROOT / VERSION_FILE).write_text(
        f"{git_head(src) or 'unknown'}\n", encoding="utf-8", newline="\n"
    )
    # After the copy, because it runs the scanner this pull just delivered.
    seeded = seed_untested_baseline(REPO_ROOT)
    seeded_structure = seed_structure_baseline(REPO_ROOT)
    # After the seed, on every pull that did not just seed: a fresh baseline is exact
    # by construction, and an adopted one holds whatever the last release earned.
    tightened = tighten_structure_baseline(REPO_ROOT) if seeded_structure is None else None
    finalise_pull(src, manifest)
    return SyncOutcome(
        copied=copied,
        skipped=[rel for rel in manifest if rel not in copied],
        removed=removed,
        preserved=preserved,
        unvendored=unvendored,
        unwired=unwired,
        blocks_written=blocks_written,
        blocks_failed=blocks_failed,
        codex_regenerated=codex_regenerated,
        seeded=seeded,
        seeded_structure=seeded_structure,
        tightened=tightened,
        pull=True,
    )


def apply_sync(src: Path, manifest: tuple[str, ...], *, pull: bool) -> SyncOutcome:
    """Copy the manifest one way and run whatever that direction owes."""
    return apply_pull(src, manifest) if pull else apply_push(src, manifest)


def finalise_pull(src: Path, manifest: tuple[str, ...]) -> None:
    """The receipt and the two pins -- the pull's third moving part.

    The stamp itself is written by `apply_sync`, before the baselines are seeded. It is
    the SHA, always: `DEVKIT_VERSION` records the upstream *commit*, and the vendored
    `test_harness_version_records_a_commit` asserts exactly that. The tag goes in the
    receipt instead, where `stale_pin` reads it. No tag is recorded for an untagged or
    dirty pull: there is no release those files correspond to, and `stale_pin` reporting
    "cannot tell" beats it asserting something untrue.

    `source_tag` is read here rather than handed down from `pull_refusal`, which asks
    the same question to decide whether to refuse. Two cheap `git` reads beat a guard
    that returns a value as well as a verdict.
    """
    tag = source_tag(src)
    provisional = source_dirty(src)
    write_receipt(REPO_ROOT, manifest, tag="" if provisional else (tag or ""))
    # Files, stamp and pin land together or the pull is a half-upgrade whose gate
    # fails later pointing at the wrong cause.
    if tag:
        _retarget(REPO_ROOT, PRECOMMIT_FILE, tag, bump_pin)
        _retarget(REPO_ROOT, PR_GATE_FILE, tag, bump_gate_ref)


def report_sync(outcome: SyncOutcome) -> None:
    """Say what `apply_sync` did, one line per thing a reader could act on."""
    verb = "pulled" if outcome.pull else "pushed"
    print(
        f"sync-harness: {verb} {len(outcome.copied)} file(s) and "
        f"{len(outcome.blocks_written)} block(s); removed {len(outcome.removed)} "
        f"retired; skipped {len(outcome.skipped)} absent."
    )
    for rel in outcome.skipped:
        print(f"  (absent) {rel}")
    for rel in outcome.preserved:
        print(f"  (preserved local edit) {rel}")
    for rel in outcome.unvendored:
        # Named on every pull, not just the one that moved it. The file is this
        # project's own from here on -- devkit will not update it again, and the
        # only thing that could tell you so is this line.
        print(f"  (now yours -- un-vendored, devkit no longer updates it) {rel}")
    for note in outcome.unwired:
        print(f"  {note}")
    if outcome.codex_regenerated:
        # Named, because it is the one file the pull rewrote that was never copied
        # from the source: it is generated here, from this project's own settings.
        print(f"  (regenerated from {SETTINGS_FILE}) {CODEX_HOOKS_FILE}")
    if outcome.seeded is not None:
        # Only on the pull that adopts the gate. Named because it is a claim about
        # this repo that nobody wrote by hand, and because the number is the debt
        # the project is now expected to burn down rather than add to.
        print(
            f"  (adopted the untested-symbol ratchet) {UNTESTED_BASELINE_FILE}: "
            f"{outcome.seeded} symbol(s) with no test naming them"
        )
    if outcome.seeded_structure is not None:
        print(
            f"  (adopted the structure ratchet) {STRUCTURE_BASELINE_FILE}: "
            f"{outcome.seeded_structure} finding(s) grandfathered"
        )
    if outcome.tightened and any(outcome.tightened):
        # Named because it is a project-owned file the pull just rewrote, and the
        # numbers are what the adoption commit carries that the MANIFEST did not.
        print(
            f"  (tightened the structure ratchet) {STRUCTURE_BASELINE_FILE}: dropped "
            f"{outcome.tightened[0]} line(s) the code no longer earns, lowered "
            f"{outcome.tightened[1]}"
        )


def report_failed_blocks(blocks_failed: list[str]) -> int:
    """`1` and a remedy when a configured block could not be spliced, else `0`.

    Reported after the stamp is written, not instead of it: the files that did land are
    on disk either way, and a receipt that omits them would misdescribe the tree. The
    exit code is what must not claim success -- a configured block that could not be
    spliced leaves the destination carrying no policy at all, which `--check` alone
    would find far later.
    """
    if not blocks_failed:
        return 0
    for entry in blocks_failed:
        print(f"BLOCK   {entry}", file=sys.stderr)
    print(
        f"sync-harness: {len(blocks_failed)} block(s) did not land. Add the "
        f"missing `{BLOCK_BEGIN.format(block_id='<id>')}` / "
        f"`{BLOCK_END.format(block_id='<id>')}` markers to the host file.",
        file=sys.stderr,
    )
    return 1


def run_sync(src: Path, manifest: tuple[str, ...], *, pull: bool) -> int:
    """`--pull` or `--push`: copy the manifest one way, then report what moved."""
    outcome = apply_sync(src, manifest, pull=pull)
    report_sync(outcome)
    return report_failed_blocks(outcome.blocks_failed)


@dataclass(frozen=True)
class CheckFindings:
    """Everything `--check` compared, before anything is said about it.

    Gathered apart from the reporting for the same reason `SyncOutcome` is: the
    comparisons are cheap and unconditional, while the *order* the findings are
    announced in is the whole usability argument -- the stale pin first, because it
    changes what the file list means. Interleaved, the function was complexity 26.
    """

    drifted: list[str]
    missing: list[str]
    retired: list[str]
    receipt_retired: list[str]
    block_drifted: list[str]
    block_unusable: list[str]
    block_ok: list[str]
    local: list[tuple[str, str]]
    local_summary: str

    @property
    def upstream(self) -> bool:
        """Anything `--pull` or `--push` would resolve. `local` is deliberately out."""
        return bool(
            self.drifted
            or self.missing
            or self.retired
            or self.receipt_retired
            or self.block_drifted
            or self.block_unusable
        )

    @property
    def clean(self) -> bool:
        return not (self.upstream or self.local)


def check_findings(src: Path, manifest: tuple[str, ...]) -> CheckFindings:
    """Compare this project's vendored tier against `src`, saying nothing."""
    drifted, missing, _ = classify(src, REPO_ROOT, manifest)
    block_drifted, block_unusable, block_ok = classify_blocks(src, REPO_ROOT, BLOCK_MANIFEST)
    # Faults in this project's own files: not drift, since `--check` never compares
    # them, but red for the same reason -- an unwired edit guard has no other symptom.
    local, local_summary = local_faults(REPO_ROOT)
    return CheckFindings(
        drifted=list(drifted),
        missing=list(missing),
        retired=list(retired_present(REPO_ROOT)),
        receipt_retired=list(receipt_retired_present(REPO_ROOT, manifest)),
        block_drifted=list(block_drifted),
        block_unusable=list(block_unusable),
        block_ok=list(block_ok),
        local=list(local),
        local_summary=local_summary,
    )


def report_stale_pin() -> None:
    """Named before the file list, because it changes what the file list *means*.

    A stale pin makes every file added upstream since the pin look like drift, and
    re-pulling -- the fix the listing implies -- cannot resolve any of it.
    """
    stale = stale_pin(REPO_ROOT)
    if not stale:
        return
    pinned, vendored = stale
    print(
        f"sync-harness: {PRECOMMIT_FILE} pins {pinned} but these files were "
        f"vendored from {vendored}. The pin is stale, so the differences below "
        f"are measured against the wrong revision -- bump the pin to {vendored}; "
        f"re-pulling will not fix them.",
        file=sys.stderr,
    )


def report_findings(found: CheckFindings) -> None:
    """One line per difference, then the remedy that actually resolves this set."""
    for rel in found.drifted:
        print(f"DRIFT   {rel}", file=sys.stderr)
    for rel in found.missing:
        print(f"MISSING {rel} (not in shared repo)", file=sys.stderr)
    for rel in found.retired:
        print(f"RETIRED {rel} (run --pull to remove)", file=sys.stderr)
    for rel in found.receipt_retired:
        print(f"RETIRED {rel} (recorded by {RECEIPT_FILE})", file=sys.stderr)
    for label in found.block_drifted:
        print(f"DRIFT   {label} (vendored block)", file=sys.stderr)
    for label in found.block_unusable:
        # Distinct from DRIFT on purpose: `--pull` fixes drift, and cannot fix a
        # missing marker pair. Saying so here saves the pull that would not help.
        print(f"BLOCK   {label} -- not comparable; --pull cannot fix this", file=sys.stderr)
    for _, message in found.local:
        # Never DRIFT: `--pull` fixes these, but nothing upstream differs, so the file
        # list and the advice that goes with it would both point at the wrong thing.
        print(message, file=sys.stderr)
    if found.upstream:
        print(
            "sync-harness: vendored harness drifted from the shared repo. "
            "Run `python scripts/sync-devkit.py --pull` to adopt upstream, "
            "or `--push` if this project authored the change.",
            file=sys.stderr,
        )
    else:
        # A `--pull` would resolve these too, but only incidentally, and telling
        # someone to adopt upstream when nothing upstream differs is the kind of advice
        # that gets a red gate reclassified as noise.
        print(
            f"sync-harness: every vendored file is in sync; {found.local_summary}.",
            file=sys.stderr,
        )


def run_check(src: Path, manifest: tuple[str, ...]) -> int:
    """The default mode: compare, and say what differs in the words of its remedy."""
    vendored, available = read_version(REPO_ROOT), git_head(src)
    if available and vendored and vendored != available:
        # Informational only -- drift is decided by content below, not version.
        print(f"sync-harness: vendored {vendored}, shared repo at {available} (newer available).")
    found = check_findings(src, manifest)
    if found.clean:
        blocks = f" and {len(found.block_ok)} block(s)" if BLOCK_MANIFEST else ""
        print(f"sync-harness: all {len(manifest)} vendored files{blocks} in sync with {src}.")
        return 0
    report_stale_pin()
    report_findings(found)
    return 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    src = resolve_src(args.src, os.environ)
    manifest = manifest_for(REPO_ROOT)

    if args.list:
        return run_list(src, manifest)
    if src is None:
        return unconfigured_verdict()
    if args.pull:
        refused = pull_refusal(src, args.allow_dirty, args.allow_untagged)
        if refused:
            return refused
    if args.pull or args.push:
        return run_sync(src, manifest, pull=args.pull)
    return run_check(src, manifest)


if __name__ == "__main__":
    sys.exit(main())
