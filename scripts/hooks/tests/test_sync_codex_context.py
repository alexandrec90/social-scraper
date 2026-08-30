"""Tests for scripts/sync-codex-context.py tree helpers."""

from conftest import load_module

mod = load_module("scripts/sync-codex-context.py")


def test_relative_files_skips_prune_dirs(tmp_path):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "ignored.txt").write_text("x", encoding="utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "ignored.pyc").write_bytes(b"x")

    assert mod.relative_files(tmp_path) == {mod.Path("a.txt")}


def test_mirror_tree_copies_changed_and_removes_orphans(tmp_path):
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    (src / "nested").mkdir(parents=True)
    (src / "keep.txt").write_text("new", encoding="utf-8")
    (src / "nested" / "deep.txt").write_text("deep", encoding="utf-8")
    dest.mkdir()
    (dest / "orphan.txt").write_text("remove me", encoding="utf-8")
    (dest / "keep.txt").write_text("old", encoding="utf-8")

    mod.mirror_tree(src, dest)

    assert (dest / "keep.txt").read_text(encoding="utf-8") == "new"
    assert (dest / "nested" / "deep.txt").read_text(encoding="utf-8") == "deep"
    assert not (dest / "orphan.txt").exists()


def test_mirror_tree_removes_empty_destination(tmp_path):
    src = tmp_path / "missing"
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "stale.txt").write_text("stale", encoding="utf-8")

    mod.mirror_tree(src, dest)

    assert not dest.exists()


def test_main_adopts_project_hooks_without_an_existing_codex_directory(tmp_path, monkeypatch):
    """Running the named Codex sync task is the opt-in; an empty/missing destination
    must not silently reduce it to a skills-only operation.
    """
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text('{"hooks": {}}', encoding="utf-8")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    generator = mod.Path(mod.__file__).with_name("sync-codex-hooks.py")
    (scripts / generator.name).write_text(generator.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)

    assert mod.main() == 0

    artifact = tmp_path / ".codex" / "hooks.json"
    assert artifact.exists()
    assert artifact.read_text(encoding="utf-8") == '{\n  "hooks": {}\n}\n'
