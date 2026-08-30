#!/usr/bin/env python3
"""Sync the Codex-specific artifacts that cannot consume Claude paths directly.

Claude project instructions are not copied: Codex reads ``CLAUDE.md`` through
``project_doc_fallback_filenames``. Only two compatibility artifacts remain:

1. Mirror ``.claude/skills/`` to ``.agents/skills/``, where Codex discovers
   repository-scoped skills.
2. Regenerate ``.codex/hooks.json`` from ``.claude/settings.json`` via
   ``sync-codex-hooks.py``. The generator owns the compatibility translation and
   creates ``.codex/`` on first use.

The pure tree helper is unit-tested in
``scripts/hooks/tests/test_sync_codex_context.py``.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PRUNE_DIRS = {".git", "node_modules", ".venv", "__pycache__"}


def relative_files(root: Path) -> set[Path]:
    """Return files below ``root`` without traversing generated/cache directories."""
    if not root.is_dir():
        return set()
    files: set[Path] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in PRUNE_DIRS]
        base = Path(dirpath)
        files.update((base / name).relative_to(root) for name in filenames)
    return files


def mirror_tree(src: Path, dest: Path) -> None:
    """Mirror authored files from ``src`` to ``dest`` and remove stale outputs."""
    src_rel = relative_files(src)
    if src_rel:
        dest.mkdir(parents=True, exist_ok=True)

    for rel in src_rel:
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src / rel, target)

    if not dest.is_dir():
        return

    for path in list(dest.rglob("*")):
        if (
            path.is_file()
            and "__pycache__" not in path.parts
            and path.relative_to(dest) not in src_rel
        ):
            path.unlink()

    for path in sorted(
        (candidate for candidate in dest.rglob("*") if candidate.is_dir()),
        key=lambda candidate: len(candidate.parts),
        reverse=True,
    ):
        if not any(path.iterdir()):
            path.rmdir()
    if dest.is_dir() and not any(dest.iterdir()):
        dest.rmdir()


def main() -> int:
    claude_dir = REPO_ROOT / ".claude"
    mirror_tree(claude_dir / "skills", REPO_ROOT / ".agents" / "skills")
    print("  mirrored .claude/skills/ -> .agents/skills/")

    claude_settings = claude_dir / "settings.json"
    codex_hooks = REPO_ROOT / ".codex" / "hooks.json"
    if claude_settings.exists():
        result = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "sync-codex-hooks.py"),
                str(claude_settings),
                str(codex_hooks),
            ],
            cwd=REPO_ROOT,
        )
        if result.returncode != 0:
            print("sync-codex-hooks.py failed", file=sys.stderr)
            return 1
        print("  regenerated .codex/hooks.json")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
