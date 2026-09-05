"""Unit tests for scripts/sync-devkit.py (harness vendoring + drift check)."""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import load_module

sh = load_module("scripts/sync-devkit.py")
# The settings tier the pull drives. Loaded here rather than off `sh`, because
# `sync-devkit.py` imports it on use and deliberately holds no reference: a project's
# first pull runs that script before this file exists. Its own contract is covered in
# `test_project_settings.py`; what is checked from here is the pull and the check
# reaching it with this module's retired list.
ps = load_module("scripts/project_settings.py")


def test_resolve_src_prefers_arg_then_env():
    assert sh.resolve_src("/a/b", {sh.SRC_ENV: "/c"}) == Path("/a/b").expanduser().resolve()
    assert sh.resolve_src(None, {sh.SRC_ENV: "/c"}) == Path("/c").expanduser().resolve()
    assert sh.resolve_src(None, {}) is None


def _seed(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


# --- the pin: the third thing an upgrade has to move ------------------------
# Files, DEVKIT_VERSION and the .pre-commit-config.yaml `rev:` describe one
# upstream revision. When only two of them move, the commit-time gate compares
# against the revision the pin names and reports every file added upstream since
# as drift -- a diagnosis that points at the files and never at the pin.

CONFIG = f"""\
repos:
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v5.0.0
    hooks:
      - id: check-yaml
  # A tag, never a branch -- one bad upstream commit must not redden this repo.
  - repo: {sh.DEVKIT_REPO}
    rev: v0.5.2 # keep in step with DEVKIT_VERSION
    hooks:
      - id: devkit-drift
"""


def test_the_pin_moves_to_the_pulled_tag():
    updated, previous = sh.bump_pin(CONFIG, "v0.5.3")
    assert previous == "v0.5.2"
    assert "rev: v0.5.3" in updated


def test_bumping_the_pin_leaves_other_repos_alone():
    """Only devkit's pin moves. Retargeting a third-party hook to a devkit tag
    would break the hook and be invisible until the next commit."""
    updated, _ = sh.bump_pin(CONFIG, "v0.5.3")
    assert "rev: v5.0.0" in updated
    assert updated.count("v0.5.3") == 1


def test_the_rationale_comment_survives_the_bump():
    # The comment is why the pin is a tag at all; a rewrite that drops it deletes
    # the reasoning and invites someone to point it at a branch.
    updated, _ = sh.bump_pin(CONFIG, "v0.5.3")
    assert "rev: v0.5.3 # keep in step with DEVKIT_VERSION" in updated
    assert "must not redden this repo" in updated


def test_a_project_without_a_devkit_pin_is_not_an_error():
    text = "repos:\n  - repo: https://example.com/other\n    rev: v1\n"
    updated, previous = sh.bump_pin(text, "v0.5.3")
    assert previous is None
    assert updated == text


GATE = f"""\
jobs:
  harness:
    steps:
      - uses: actions/checkout@v7
      - name: Check out the shared harness repo
        uses: actions/checkout@v7
        with:
          repository: {sh.DEVKIT_SLUG}
          ref: v0.5.2 # bump with the --pull it corresponds to
          path: .devkit-src
      - name: Check out a vendor fixture
        uses: actions/checkout@v7
        with:
          repository: someone/else
          ref: v1.2.3
"""


def test_the_gate_ref_moves_to_the_pulled_tag():
    updated, previous = sh.bump_gate_ref(GATE, "v0.5.3")
    assert previous == "v0.5.2"
    assert "ref: v0.5.3 # bump with the --pull it corresponds to" in updated


def test_only_devkits_checkout_step_is_retargeted():
    """A workflow checks out several repos. Retargeting the wrong one points a
    third-party checkout at a devkit tag, and CI fails somewhere unrelated."""
    updated, _ = sh.bump_gate_ref(GATE, "v0.5.3")
    assert "ref: v1.2.3" in updated
    assert updated.count("v0.5.3") == 1


def test_a_workflow_without_a_devkit_checkout_is_not_an_error():
    updated, previous = sh.bump_gate_ref("jobs:\n  x:\n    steps: []\n", "v0.5.3")
    assert previous is None
    assert "v0.5.3" not in updated


def test_pull_moves_both_consumer_pins(tmp_path, monkeypatch):
    """RELEASING.md: the `rev:` and the gate's `ref:` are two separate pins and both
    have to land in the same change as the files. Moving one is a half-upgrade."""
    src = _repo(tmp_path / "src", tag="v0.5.3", files={"scripts/hooks/x.py": "upstream"})
    repo = tmp_path / "proj"
    _seed(repo, sh.PRECOMMIT_FILE, CONFIG)
    _seed(repo, sh.PR_GATE_FILE, GATE)
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/hooks/x.py",))

    assert sh.main(["--pull", "--src", str(src)]) == 0
    assert sh.read_pin((repo / sh.PRECOMMIT_FILE).read_text()) == "v0.5.3"
    assert "ref: v0.5.3" in (repo / sh.PR_GATE_FILE).read_text()


def test_a_project_with_no_pr_gate_still_pulls(tmp_path, monkeypatch):
    # Not every consumer has a PR gate; its absence is not a failure.
    src = _repo(tmp_path / "src", tag="v0.5.3", files={"scripts/hooks/x.py": "upstream"})
    repo = tmp_path / "proj"
    _seed(repo, sh.PRECOMMIT_FILE, CONFIG)
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/hooks/x.py",))

    assert sh.main(["--pull", "--src", str(src)]) == 0
    assert not (repo / sh.PR_GATE_FILE).exists()


def test_the_pin_is_read_back_without_its_comment():
    assert sh.read_pin(CONFIG) == "v0.5.2"


def _receipt(root: Path, tag: str = "") -> None:
    sh.write_receipt(root, (), tag=tag)


def test_a_stale_pin_is_detected_from_the_project_alone(tmp_path):
    """No network, no source checkout: both inputs are committed in the project."""
    _seed(tmp_path, sh.PRECOMMIT_FILE, CONFIG)
    _receipt(tmp_path, "v0.5.3")
    assert sh.stale_pin(tmp_path) == ("v0.5.2", "v0.5.3")


def test_a_matching_pin_and_vendored_tag_is_not_stale(tmp_path):
    _seed(tmp_path, sh.PRECOMMIT_FILE, CONFIG)
    _receipt(tmp_path, "v0.5.2")
    assert sh.stale_pin(tmp_path) is None


def test_the_sha_stamp_is_never_what_the_pin_is_compared_against(tmp_path):
    """DEVKIT_VERSION holds a SHA by contract, and a SHA never equals a tag, so
    comparing against it would report every project stale forever. Regression for
    the stamp a consumer had to correct by hand."""
    _seed(tmp_path, sh.PRECOMMIT_FILE, CONFIG)
    _seed(tmp_path, sh.VERSION_FILE, "9d95e44\n")
    _receipt(tmp_path, "v0.5.2")
    assert sh.stale_pin(tmp_path) is None


def test_an_unrecorded_tag_reads_as_cannot_tell_not_stale(tmp_path):
    """A pull from before the receipt carried a tag, or an --allow-untagged one.
    A check that cried wolf on every un-upgraded project would be ignored."""
    _seed(tmp_path, sh.PRECOMMIT_FILE, CONFIG)
    _receipt(tmp_path, "")
    assert sh.stale_pin(tmp_path) is None


def test_a_project_missing_either_input_is_not_stale(tmp_path):
    # Pre-adoption, or a project that does not use the published hooks.
    assert sh.stale_pin(tmp_path) is None
    _seed(tmp_path, sh.PRECOMMIT_FILE, CONFIG)
    assert sh.stale_pin(tmp_path) is None


def test_classify_partitions_ok_drift_missing(tmp_path):
    src = tmp_path / "shared"
    repo = tmp_path / "proj"
    manifest = ("scripts/a.py", "scripts/b.py", "scripts/c.py")
    _seed(src, "scripts/a.py", "same")
    _seed(repo, "scripts/a.py", "same")  # ok
    _seed(src, "scripts/b.py", "upstream")
    _seed(repo, "scripts/b.py", "local-edit")  # drift
    _seed(repo, "scripts/c.py", "only-here")  # missing in src

    drifted, missing, ok = sh.classify(src, repo, manifest)
    assert ok == ["scripts/a.py"]
    assert drifted == ["scripts/b.py"]
    assert missing == ["scripts/c.py"]


# --- the two guards on --pull ------------------------------------------------


def _git(root: Path, *args: str):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def _repo(root: Path, tag: str = "", files: dict[str, str] | None = None) -> Path:
    """A one-commit git repo, optionally tagged. Real git: `describe --exact-match`
    and `status --porcelain` are the behaviours under test, and a fake would only
    assert that the fake works.

    `files` are committed, not left in the tree -- an uncommitted file would make
    the source dirty and trip the other guard, which is the bug this helper had on
    its first outing.
    """
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "f.txt").write_text("one")
    for rel, text in (files or {}).items():
        _seed(root, rel, text)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "one")
    if tag:
        _git(root, "tag", tag)
    return root


def test_a_tagged_clean_source_is_pullable(tmp_path):
    src = _repo(tmp_path / "src", tag="v9.9.9")
    assert sh.source_tag(src) == "v9.9.9"
    assert not sh.source_dirty(src)


def test_an_untagged_source_has_no_tag(tmp_path):
    assert sh.source_tag(_repo(tmp_path / "src")) is None


def test_uncommitted_changes_make_a_source_dirty(tmp_path):
    src = _repo(tmp_path / "src", tag="v9.9.9")
    (src / "f.txt").write_text("changed")
    assert sh.source_dirty(src)


def test_a_non_repo_is_neither_tagged_nor_dirty(tmp_path):
    # Not a git checkout at all: report nothing rather than crashing the gate.
    plain = tmp_path / "plain"
    plain.mkdir()
    assert sh.source_tag(plain) is None
    assert not sh.source_dirty(plain)


def test_pull_refuses_a_dirty_source_without_copying_anything(tmp_path, monkeypatch):
    """The refusal has to come before the copy: a half-upgraded project with a
    matching stamp is the state that cannot be diagnosed afterwards."""
    src = _repo(tmp_path / "src", tag="v9.9.9", files={"scripts/hooks/x.py": "upstream"})
    (src / "f.txt").write_text("now uncommitted")
    repo = tmp_path / "proj"
    _seed(repo, "scripts/hooks/x.py", "old")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/hooks/x.py",))

    assert sh.main(["--pull", "--src", str(src)]) == 2
    assert (repo / "scripts/hooks/x.py").read_text() == "old"
    assert not (repo / sh.VERSION_FILE).exists()


def test_pull_refuses_an_untagged_source(tmp_path, monkeypatch):
    src = _repo(tmp_path / "src", files={"scripts/hooks/x.py": "upstream"})
    repo = tmp_path / "proj"
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/hooks/x.py",))

    assert sh.main(["--pull", "--src", str(src)]) == 2
    assert not (repo / "scripts/hooks/x.py").exists()


def test_pull_stamps_the_tag_and_bumps_the_pin_together(tmp_path, monkeypatch):
    """The whole point: three things move as one, or the upgrade is half-done."""
    src = _repo(tmp_path / "src", tag="v0.5.3", files={"scripts/hooks/x.py": "upstream"})
    repo = tmp_path / "proj"
    _seed(repo, sh.PRECOMMIT_FILE, CONFIG)
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/hooks/x.py",))

    assert sh.main(["--pull", "--src", str(src)]) == 0
    assert (repo / "scripts/hooks/x.py").read_text() == "upstream"
    # The stamp is the SHA (its documented contract); the tag lands in the receipt.
    assert re.fullmatch(r"[0-9a-f]{7,40}", (repo / sh.VERSION_FILE).read_text().strip())
    assert sh.read_receipt_tag(repo) == "v0.5.3"
    assert "rev: v0.5.3" in (repo / sh.PRECOMMIT_FILE).read_text()
    assert sh.stale_pin(repo) is None


def test_an_allowed_dirty_pull_is_stamped_provisional(tmp_path, monkeypatch):
    """Marked so it can never be mistaken for a release -- and so `stale_pin`
    keeps reporting it until a real upgrade replaces it."""
    src = _repo(tmp_path / "src", tag="v0.5.3", files={"scripts/hooks/x.py": "upstream"})
    (src / "f.txt").write_text("uncommitted")
    repo = tmp_path / "proj"
    _seed(repo, sh.PRECOMMIT_FILE, CONFIG)
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/hooks/x.py",))

    assert sh.main(["--pull", "--src", str(src), "--allow-dirty"]) == 0
    # No tag recorded: these files are at no release, so "cannot tell" is the
    # honest answer rather than naming one they did not come from.
    assert sh.read_receipt_tag(repo) == ""
    assert sh.stale_pin(repo) is None


def test_an_untagged_pull_leaves_the_pin_alone(tmp_path, monkeypatch):
    # There is no tag to move it to; pretending otherwise would pin a nonexistent rev.
    src = _repo(tmp_path / "src", files={"scripts/hooks/x.py": "upstream"})
    repo = tmp_path / "proj"
    _seed(repo, sh.PRECOMMIT_FILE, CONFIG)
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/hooks/x.py",))

    assert sh.main(["--pull", "--src", str(src), "--allow-untagged"]) == 0
    assert sh.read_pin((repo / sh.PRECOMMIT_FILE).read_text()) == "v0.5.2"


def test_check_noop_when_src_unset_and_project_never_pulled(tmp_path, capsys, monkeypatch):
    """Pre-adoption: nothing is vendored, so there is nothing a skip could hide.

    REPO_ROOT is patched rather than left at the real one because this test ships into
    every consumer, and a consumer's repo root IS stamped -- reading it would make the
    two cases the same test in devkit and the opposite one downstream.
    """
    monkeypatch.delenv(sh.SRC_ENV, raising=False)
    monkeypatch.setattr(sh, "REPO_ROOT", tmp_path)
    assert sh.main(["--check"]) == 0
    assert "skipping" in capsys.readouterr().out


def test_check_fails_when_src_unset_but_the_project_has_pulled(tmp_path, capsys, monkeypatch):
    """The trap: a stamped project has vendored files and this compared none of them.

    Exiting 0 here reports a gate that ran over nothing, which in a log is indistinguishable
    from a clean gate. The stamp is committed and `$DEVKIT_DIR` is a property of the
    machine, which is what lets this tell "not adopted yet" from "adopted, and this machine
    has no clone to check against" -- a second workstation, a fresh clone, or a CI job
    whose `env:` block was dropped.
    """
    monkeypatch.delenv(sh.SRC_ENV, raising=False)
    monkeypatch.setattr(sh, "REPO_ROOT", tmp_path)
    (tmp_path / sh.VERSION_FILE).write_text("abc1234\n")

    assert sh.main(["--check"]) == 1
    out = capsys.readouterr().out
    assert "NOTHING WAS CHECKED" in out
    # Both remedies, because one of them works with no devkit clone on the machine.
    assert sh.SRC_ENV in out
    assert "devkit-drift" in out


def test_check_passes_when_in_sync(tmp_path, monkeypatch):
    src = tmp_path / "shared"
    repo = tmp_path / "proj"
    _seed(src, "scripts/x.py", "v1")
    _seed(repo, "scripts/x.py", "v1")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/x.py",))
    assert sh.main(["--check", "--src", str(src)]) == 0


def test_check_fails_on_drift(tmp_path, monkeypatch):
    src = tmp_path / "shared"
    repo = tmp_path / "proj"
    _seed(src, "scripts/x.py", "upstream")
    _seed(repo, "scripts/x.py", "local")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/x.py",))
    assert sh.main(["--check", "--src", str(src)]) == 1


def test_pull_copies_shared_into_project(tmp_path, monkeypatch):
    src = tmp_path / "shared"
    repo = tmp_path / "proj"
    _seed(src, "scripts/x.py", "upstream")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/x.py",))
    # --allow-untagged throughout the mechanics tests below: `src` is a plain
    # directory, not a checkout, so the release guards have nothing to read. What
    # is under test here is the copy/retire/receipt behaviour, not the guards.
    assert sh.main(["--pull", "--src", str(src), "--allow-untagged"]) == 0
    assert (repo / "scripts/x.py").read_text() == "upstream"


def test_pull_removes_only_reviewed_retired_files(tmp_path, monkeypatch):
    src = tmp_path / "shared"
    repo = tmp_path / "proj"
    _seed(src, "scripts/x.py", "upstream")
    _seed(repo, ".claude/skills/old/SKILL.md", "obsolete")
    _seed(repo, ".claude/skills/old/state.json", "project-owned")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/x.py",))
    monkeypatch.setattr(sh, "RETIRED_PATHS", (".claude/skills/old/SKILL.md",))

    assert sh.main(["--pull", "--src", str(src), "--allow-untagged"]) == 0
    assert not (repo / ".claude/skills/old/SKILL.md").exists()
    assert (repo / ".claude/skills/old/state.json").read_text() == "project-owned"


def test_pull_receipt_removes_a_no_longer_managed_unchanged_file(tmp_path, monkeypatch):
    src = tmp_path / "shared"
    repo = tmp_path / "proj"
    _seed(src, "scripts/old.py", "old")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/old.py",))
    assert sh.main(["--pull", "--src", str(src), "--allow-untagged"]) == 0

    _seed(src, "scripts/new.py", "new")
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/new.py",))
    assert sh.main(["--pull", "--src", str(src), "--allow-untagged"]) == 0
    assert not (repo / "scripts/old.py").exists()
    assert (repo / "scripts/new.py").read_text() == "new"


def test_pull_receipt_preserves_a_locally_edited_retired_file(tmp_path, monkeypatch):
    src = tmp_path / "shared"
    repo = tmp_path / "proj"
    _seed(src, "scripts/old.py", "old")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/old.py",))
    assert sh.main(["--pull", "--src", str(src), "--allow-untagged"]) == 0
    _seed(repo, "scripts/old.py", "local edit")

    monkeypatch.setattr(sh, "MANIFEST", ())
    assert sh.main(["--pull", "--src", str(src), "--allow-untagged"]) == 0
    assert (repo / "scripts/old.py").read_text() == "local edit"


# --- retirement has to leave the *directory* gone too -----------------------
# Every retired skill that shipped a `.py` had run at least once, so it left a
# `__pycache__/` behind. That husk is gitignored, so it is invisible to `git status`
# and to the drift gate, and it made the bare `rmdir()` fail silently -- carameli
# carried nine empty skill directories for months after the skills were retired.


def test_pull_removes_the_bytecode_cache_left_beside_a_retired_file(tmp_path, monkeypatch):
    src = tmp_path / "shared"
    repo = tmp_path / "proj"
    _seed(src, "scripts/x.py", "upstream")
    _seed(repo, ".claude/skills/old/engine.py", "obsolete")
    _seed(repo, ".claude/skills/old/__pycache__/engine.cpython-314.pyc", "bytecode")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/x.py",))
    monkeypatch.setattr(sh, "RETIRED_PATHS", (".claude/skills/old/engine.py",))

    assert sh.main(["--pull", "--src", str(src), "--allow-untagged"]) == 0
    assert not (repo / ".claude/skills/old").exists()


def test_pull_prunes_a_husk_whose_retired_file_is_already_gone(tmp_path, monkeypatch):
    """The case that actually persisted: the file was deleted by hand (a sweep
    commit), so `retired_present` never matched it and the husk outlived every pull."""
    src = tmp_path / "shared"
    repo = tmp_path / "proj"
    _seed(src, "scripts/x.py", "upstream")
    _seed(repo, ".claude/skills/old/__pycache__/engine.cpython-314.pyc", "bytecode")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/x.py",))
    monkeypatch.setattr(sh, "RETIRED_PATHS", (".claude/skills/old/engine.py",))

    assert sh.main(["--pull", "--src", str(src), "--allow-untagged"]) == 0
    assert not (repo / ".claude/skills/old").exists()


def test_pull_keeps_a_cache_sitting_beside_project_owned_state(tmp_path, monkeypatch):
    """A surviving sibling means the directory is still someone's. Retirement removes
    the file it reviewed and stops -- it does not get to decide the cache is garbage."""
    src = tmp_path / "shared"
    repo = tmp_path / "proj"
    _seed(src, "scripts/x.py", "upstream")
    _seed(repo, ".claude/skills/old/engine.py", "obsolete")
    _seed(repo, ".claude/skills/old/state.json", "project-owned")
    _seed(repo, ".claude/skills/old/__pycache__/engine.cpython-314.pyc", "bytecode")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/x.py",))
    monkeypatch.setattr(sh, "RETIRED_PATHS", (".claude/skills/old/engine.py",))

    assert sh.main(["--pull", "--src", str(src), "--allow-untagged"]) == 0
    assert (repo / ".claude/skills/old/state.json").read_text() == "project-owned"
    assert (repo / ".claude/skills/old/__pycache__/engine.cpython-314.pyc").exists()


def test_pull_keeps_a_cache_holding_more_than_bytecode(tmp_path, monkeypatch):
    """`__pycache__` is only ever regenerated bytecode. Something else in there was
    put there deliberately, and deleting a directory tree on a guess is unrecoverable."""
    src = tmp_path / "shared"
    repo = tmp_path / "proj"
    _seed(src, "scripts/x.py", "upstream")
    _seed(repo, ".claude/skills/old/engine.py", "obsolete")
    _seed(repo, ".claude/skills/old/__pycache__/notes.md", "not bytecode")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/x.py",))
    monkeypatch.setattr(sh, "RETIRED_PATHS", (".claude/skills/old/engine.py",))

    assert sh.main(["--pull", "--src", str(src), "--allow-untagged"]) == 0
    assert (repo / ".claude/skills/old/__pycache__/notes.md").read_text() == "not bytecode"


def test_pull_receipt_removal_also_prunes_the_bytecode_cache(tmp_path, monkeypatch):
    """The receipt path unlinks files too, and had the identical bare `rmdir()`."""
    src = tmp_path / "shared"
    repo = tmp_path / "proj"
    _seed(src, "scripts/gone/old.py", "old")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/gone/old.py",))
    assert sh.main(["--pull", "--src", str(src), "--allow-untagged"]) == 0
    _seed(repo, "scripts/gone/__pycache__/old.cpython-314.pyc", "bytecode")

    monkeypatch.setattr(sh, "MANIFEST", ())
    assert sh.main(["--pull", "--src", str(src), "--allow-untagged"]) == 0
    assert not (repo / "scripts/gone").exists()


def test_pruning_leaves_a_directory_that_still_has_live_sources(tmp_path, monkeypatch):
    """Retired paths share parents with live ones -- `scripts/hooks/` holds most of the
    harness. The sweep must not treat a busy directory's cache as abandoned."""
    repo = tmp_path / "proj"
    _seed(repo, "scripts/hooks/live.py", "live")
    _seed(repo, "scripts/hooks/__pycache__/live.cpython-314.pyc", "bytecode")

    sh._prune_dir(repo / "scripts/hooks")

    assert (repo / "scripts/hooks/live.py").exists()
    assert (repo / "scripts/hooks/__pycache__/live.cpython-314.pyc").exists()


def test_pruning_a_directory_that_does_not_exist_is_not_an_error(tmp_path):
    sh._prune_dir(tmp_path / "never-existed")


def test_check_fails_while_a_retired_file_is_present(tmp_path, monkeypatch):
    src = tmp_path / "shared"
    repo = tmp_path / "proj"
    _seed(src, "scripts/x.py", "same")
    _seed(repo, "scripts/x.py", "same")
    _seed(repo, ".claude/skills/old/SKILL.md", "obsolete")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/x.py",))
    monkeypatch.setattr(sh, "RETIRED_PATHS", (".claude/skills/old/SKILL.md",))

    assert sh.main(["--check", "--src", str(src)]) == 1


def test_push_copies_project_into_shared(tmp_path, monkeypatch):
    src = tmp_path / "shared"
    repo = tmp_path / "proj"
    _seed(repo, "scripts/x.py", "authored-here")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/x.py",))
    assert sh.main(["--push", "--src", str(src)]) == 0
    assert (src / "scripts/x.py").read_text() == "authored-here"


def test_list_prints_manifest(capsys):
    assert sh.main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "scripts/hooks/harness_config.py" in out


def test_manifest_files_exist_in_repo():
    # The vendored manifest must reference real files in this repo.
    for rel in sh.MANIFEST:
        assert (sh.REPO_ROOT / rel).exists(), f"manifest lists missing file: {rel}"


def test_version_file_not_in_manifest():
    # DEVKIT_VERSION is a per-project artifact, never synced/drift-checked.
    assert sh.VERSION_FILE not in sh.MANIFEST
    assert sh.RECEIPT_FILE not in sh.MANIFEST


# ---- version stamping ------------------------------------------------------


def test_read_version_roundtrip(tmp_path):
    assert sh.read_version(tmp_path) is None
    (tmp_path / sh.VERSION_FILE).write_text("abc1234\n")
    assert sh.read_version(tmp_path) == "abc1234"


def test_git_head_parses_sha(monkeypatch):
    import subprocess as _sp

    monkeypatch.setattr(
        sh.subprocess, "run", lambda *a, **k: _sp.CompletedProcess([], 0, "deadbee\n", "")
    )
    assert sh.git_head(Path(".")) == "deadbee"


def test_git_head_none_on_failure(monkeypatch):
    import subprocess as _sp

    monkeypatch.setattr(
        sh.subprocess, "run", lambda *a, **k: _sp.CompletedProcess([], 128, "", "not a git repo")
    )
    assert sh.git_head(Path(".")) is None


def test_pull_stamps_harness_version(tmp_path, monkeypatch):
    src = tmp_path / "shared"
    repo = tmp_path / "proj"
    _seed(src, "scripts/x.py", "v1")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/x.py",))
    monkeypatch.setattr(sh, "git_head", lambda p: "abc1234")
    # Untagged, so the stamp falls back to the SHA -- and `stale_pin` will keep
    # reporting it until a tagged pull replaces it.
    assert sh.main(["--pull", "--src", str(src), "--allow-untagged"]) == 0
    assert (repo / sh.VERSION_FILE).read_bytes() == b"abc1234\n"


# --- the marker-block tier ----------------------------------------------------
# Vendoring a region of a per-project file. MANIFEST cannot hold `CLAUDE.md` -- every
# project's own prose would read as drift -- so the gated unit is the span between a
# marker pair. What these pin is that an *ungatable* block is never mistaken for a
# clean one: the whole point is that policy nobody is comparing must be loud.

POLICY = "\n## Testing\n\nEvery change ships with tests.\n"


def _host(block_id: str, body: str, before: str = "# Project\n", after: str = "\n# Local\n") -> str:
    begin, end = sh.block_markers(block_id)
    return f"{before}{begin}{body}{end}{after}"


def test_find_block_locates_the_span_between_markers():
    text = _host("engineering", POLICY)
    span, problem = sh.find_block(text, "engineering")
    assert problem == ""
    assert span is not None
    assert text[span[0] : span[1]] == POLICY


def test_find_block_names_each_way_a_block_is_unusable():
    """Each of these is a gate that is not running; the reason reaches the operator
    verbatim, because "no block" and "inverted markers" need different fixes."""
    begin, end = sh.block_markers("eng")
    cases = {
        "# nothing here\n": "no 'eng' block",
        f"{begin}\nbody\n": "'eng' begin marker with no end",
        f"body\n{end}\n": "'eng' end marker with no begin",
        f"{end}\nbody\n{begin}\n": "'eng' markers are inverted",
        f"{begin}a{end}\n{begin}b{end}": "duplicate 'eng' markers",
    }
    for text, expected in cases.items():
        span, problem = sh.find_block(text, "eng")
        assert span is None
        assert problem == expected


def test_extract_block_returns_none_when_unusable():
    assert sh.extract_block(_host("eng", POLICY), "eng") == POLICY
    assert sh.extract_block("# no markers\n", "eng") is None


def test_replace_block_leaves_everything_outside_the_markers_alone():
    """The half of the guarantee the project cares about: devkit owns the region and
    nothing else. A splice that reflowed the host file would make every pull a diff
    nobody asked for."""
    text = _host("eng", "\nold\n", before="# Mine\n\nkeep me\n", after="\n## Also mine\n")
    spliced, problem = sh.replace_block(text, "eng", POLICY)
    assert problem == ""
    assert "keep me" in spliced
    assert "## Also mine" in spliced
    assert "old" not in spliced
    assert sh.extract_block(spliced, "eng") == POLICY


def test_replace_block_reports_instead_of_appending():
    """A host with no markers must not be silently given the policy at some guessed
    offset -- where the block lands in someone's CLAUDE.md is theirs to decide."""
    spliced, problem = sh.replace_block("# No markers\n", "eng", POLICY)
    assert spliced == ""
    assert problem == "no 'eng' block"


def test_replace_block_round_trips_exactly():
    """`--check` runs straight after `--pull` in CI. If a splice normalised so much as
    a trailing newline, the pull would report drift it had just created."""
    text = _host("eng", POLICY)
    spliced, _ = sh.replace_block(text, "eng", sh.extract_block(text, "eng") or "")
    assert spliced == text


@pytest.mark.parametrize("eol", [b"\n", b"\r\n"], ids=["lf-host", "crlf-host"])
def test_block_splice_preserves_the_hosts_line_endings(tmp_path, eol):
    """`_read_text`/`_write_text` disable newline translation, so a host keeps exactly
    the endings it had. Both directions are parametrized because the default
    translating pair corrupts a *different* one on each platform, and a single case
    would be a test that never fails on the machine running it:

      - LF host on Windows: the read leaves LF, the write expands it to `os.linesep`
        -- the whole file becomes CRLF.
      - CRLF host on POSIX: the read collapses to LF, the write leaves it -- the whole
        file becomes LF.

    Either way it is a whole-file diff produced by a splice that changed one
    paragraph, on a file the project owns.
    """
    src, repo = tmp_path / "shared", tmp_path / "proj"
    body = eol + b"## Testing" + eol + eol + b"Every change ships with tests." + eol
    for root, prose in ((src, b"# devkit"), (repo, b"# proj" + eol + eol + b"Keep me.")):
        host = root / "CLAUDE.md"
        host.parent.mkdir(parents=True, exist_ok=True)
        stale = body if root is src else eol + b"stale" + eol
        begin, end = (m.encode() for m in sh.block_markers("eng"))
        host.write_bytes(prose + eol + begin + stale + end + eol + b"# Tail" + eol)

    written, failed = sh.sync_blocks(src, repo, (("CLAUDE.md", "eng"),))
    assert (written, failed) == (["CLAUDE.md#eng"], [])

    landed = (repo / "CLAUDE.md").read_bytes()
    assert landed.count(eol) == landed.count(b"\n"), f"mixed endings in {landed!r}"
    assert (b"\r" in landed) is (eol == b"\r\n")
    assert b"Keep me." in landed
    assert b"stale" not in landed


def _seed_block(root: Path, rel: str, block_id: str, body: str, before: str = "# H\n") -> None:
    _seed(root, rel, _host(block_id, body, before=before))


def test_classify_blocks_partitions_by_region_not_by_file(tmp_path):
    """The whole reason the tier exists: the hosts differ (each project's own prose)
    while the vendored region matches, and that must classify as in sync."""
    src, repo = tmp_path / "shared", tmp_path / "proj"
    _seed_block(src, "CLAUDE.md", "eng", POLICY, before="# devkit\n")
    _seed_block(repo, "CLAUDE.md", "eng", POLICY, before="# carameli\n\nWholly different.\n")
    drifted, unusable, ok = sh.classify_blocks(src, repo, (("CLAUDE.md", "eng"),))
    assert (drifted, unusable) == ([], [])
    assert ok == ["CLAUDE.md#eng"]


def test_classify_blocks_reports_a_changed_region(tmp_path):
    src, repo = tmp_path / "shared", tmp_path / "proj"
    _seed_block(src, "CLAUDE.md", "eng", POLICY)
    _seed_block(repo, "CLAUDE.md", "eng", "\n## Testing\n\nSometimes.\n")
    drifted, unusable, ok = sh.classify_blocks(src, repo, (("CLAUDE.md", "eng"),))
    assert drifted == ["CLAUDE.md#eng"]
    assert (unusable, ok) == ([], [])


def test_classify_blocks_never_counts_an_ungatable_block_as_ok(tmp_path):
    """A missing marker pair means this project is carrying no gated policy at all.
    Silence here would be the failure mode the dispatch-coherence rule exists for."""
    src, repo = tmp_path / "shared", tmp_path / "proj"
    _seed_block(src, "CLAUDE.md", "eng", POLICY)
    _seed(repo, "CLAUDE.md", "# carameli\n\nNo markers at all.\n")
    drifted, unusable, ok = sh.classify_blocks(src, repo, (("CLAUDE.md", "eng"),))
    assert (drifted, ok) == ([], [])
    assert unusable == ["CLAUDE.md#eng (this project: no 'eng' block)"]


def test_classify_blocks_names_the_side_whose_host_is_absent(tmp_path):
    src, repo = tmp_path / "shared", tmp_path / "proj"
    _seed_block(src, "CLAUDE.md", "eng", POLICY)
    repo.mkdir(parents=True, exist_ok=True)
    _, unusable, _ = sh.classify_blocks(src, repo, (("CLAUDE.md", "eng"),))
    assert unusable == ["CLAUDE.md#eng (CLAUDE.md absent in this project)"]


def test_sync_blocks_writes_the_region_and_keeps_local_prose(tmp_path):
    src, repo = tmp_path / "shared", tmp_path / "proj"
    _seed_block(src, "CLAUDE.md", "eng", POLICY)
    _seed_block(repo, "CLAUDE.md", "eng", "\nstale\n", before="# carameli\n\nMine.\n")
    written, failed = sh.sync_blocks(src, repo, (("CLAUDE.md", "eng"),))
    assert (written, failed) == (["CLAUDE.md#eng"], [])
    landed = (repo / "CLAUDE.md").read_text(encoding="utf-8")
    assert sh.extract_block(landed, "eng") == POLICY
    assert "Mine." in landed
    assert "stale" not in landed


def test_sync_blocks_leaves_the_host_untouched_when_it_cannot_splice(tmp_path):
    """A refusal must not half-write. Same contract as `--pull`'s dirty-source guard."""
    src, repo = tmp_path / "shared", tmp_path / "proj"
    _seed_block(src, "CLAUDE.md", "eng", POLICY)
    _seed(repo, "CLAUDE.md", "# carameli\n\nNo markers.\n")
    written, failed = sh.sync_blocks(src, repo, (("CLAUDE.md", "eng"),))
    assert written == []
    assert failed == ["CLAUDE.md#eng (destination: no 'eng' block)"]
    assert (repo / "CLAUDE.md").read_text(encoding="utf-8") == "# carameli\n\nNo markers.\n"


def test_pull_splices_configured_blocks(tmp_path, monkeypatch):
    src, repo = tmp_path / "shared", tmp_path / "proj"
    _seed(src, "scripts/x.py", "v1")
    _seed_block(src, "CLAUDE.md", "eng", POLICY)
    _seed_block(repo, "CLAUDE.md", "eng", "\nstale\n", before="# proj\n")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/x.py",))
    monkeypatch.setattr(sh, "BLOCK_MANIFEST", (("CLAUDE.md", "eng"),))
    monkeypatch.setattr(sh, "git_head", lambda p: "abc1234")
    assert sh.main(["--pull", "--src", str(src), "--allow-untagged"]) == 0
    assert sh.extract_block((repo / "CLAUDE.md").read_text(encoding="utf-8"), "eng") == POLICY


def test_pull_fails_when_a_configured_block_cannot_land(tmp_path, monkeypatch):
    """Exit 1, not a warning: a pull that reports success while the policy stayed
    behind is the half-upgrade the version stamp cannot describe."""
    src, repo = tmp_path / "shared", tmp_path / "proj"
    _seed(src, "scripts/x.py", "v1")
    _seed_block(src, "CLAUDE.md", "eng", POLICY)
    _seed(repo, "CLAUDE.md", "# proj\n\nNo markers.\n")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/x.py",))
    monkeypatch.setattr(sh, "BLOCK_MANIFEST", (("CLAUDE.md", "eng"),))
    monkeypatch.setattr(sh, "git_head", lambda p: "abc1234")
    assert sh.main(["--pull", "--src", str(src), "--allow-untagged"]) == 1
    # The file half still landed, so the receipt describes what is actually on disk.
    assert (repo / "scripts/x.py").exists()


def test_check_fails_on_a_drifted_block(tmp_path, monkeypatch):
    src, repo = tmp_path / "shared", tmp_path / "proj"
    _seed(src, "scripts/x.py", "v1")
    _seed(repo, "scripts/x.py", "v1")
    _seed_block(src, "CLAUDE.md", "eng", POLICY)
    _seed_block(repo, "CLAUDE.md", "eng", "\nreworded locally\n")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/x.py",))
    monkeypatch.setattr(sh, "BLOCK_MANIFEST", (("CLAUDE.md", "eng"),))
    assert sh.main(["--check", "--src", str(src)]) == 1


def test_check_passes_when_only_the_hosts_differ(tmp_path, monkeypatch):
    src, repo = tmp_path / "shared", tmp_path / "proj"
    _seed(src, "scripts/x.py", "v1")
    _seed(repo, "scripts/x.py", "v1")
    _seed_block(src, "CLAUDE.md", "eng", POLICY, before="# devkit\n")
    _seed_block(repo, "CLAUDE.md", "eng", POLICY, before="# proj\n\nEntirely my own.\n")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/x.py",))
    monkeypatch.setattr(sh, "BLOCK_MANIFEST", (("CLAUDE.md", "eng"),))
    assert sh.main(["--check", "--src", str(src)]) == 0


def test_block_manifest_ships_empty_until_the_content_moves():
    """The tier is inert on arrival by design -- the helpers above are what is under
    test. Delete this the moment a block is configured; it is a scope marker, and
    leaving it would make the migration look like a regression."""
    assert sh.BLOCK_MANIFEST == ()


# --- unwiring a retired hook ------------------------------------------------
# A pull DELETES retired scripts, so a settings entry still naming one is not inert:
# the harness runs it, the interpreter fails, and every prompt in that project carries
# a hook error. These assert the pull cleans up after itself.


def _settings(*commands: str) -> dict:
    return {
        "model": "opus",
        "hooks": {
            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": commands[0]}]}],
            "PreToolUse": [
                {
                    "matcher": "^(Edit|Write)$",
                    "hooks": [{"type": "command", "command": c} for c in commands[1:]],
                }
            ],
        },
    }


def test_a_retired_hook_command_is_dropped():
    payload = _settings(
        'python3 "${CLAUDE_PROJECT_DIR:-.}/scripts/hooks/branch-per-task.py"',
        'python3 "${CLAUDE_PROJECT_DIR:-.}/scripts/hooks/lint-fix.py"',
    )
    pruned, dropped = ps.prune_hook_commands(payload, ("scripts/hooks/branch-per-task.py",))
    assert dropped == ["branch-per-task.py"]
    assert "UserPromptSubmit" not in pruned["hooks"]
    assert pruned["hooks"]["PreToolUse"][0]["hooks"][0]["command"].endswith('lint-fix.py"')


def test_a_surviving_hook_in_the_same_group_is_kept():
    """The group is shared, so this must drop a command, not the matcher it sits in."""
    payload = _settings(
        'python3 "x/scripts/hooks/branch-per-task.py"',
        'python3 "x/scripts/hooks/branch-on-write.py"',
        'python3 "x/scripts/hooks/lint-fix.py"',
    )
    pruned, dropped = ps.prune_hook_commands(
        payload, ("scripts/hooks/branch-per-task.py", "scripts/hooks/branch-on-write.py")
    )
    assert sorted(dropped) == ["branch-on-write.py", "branch-per-task.py"]
    kept = pruned["hooks"]["PreToolUse"][0]["hooks"]
    assert len(kept) == 1 and kept[0]["command"].endswith('lint-fix.py"')


def test_an_emptied_event_is_removed_not_left_as_a_husk():
    """`{"hooks": []}` is a shape the next reader cannot tell from an accident."""
    payload = _settings('python3 "x/scripts/hooks/branch-per-task.py"')
    pruned, _ = ps.prune_hook_commands(payload, ("scripts/hooks/branch-per-task.py",))
    assert pruned["hooks"] == {}
    assert pruned["model"] == "opus"  # everything outside `hooks` is untouched


def test_nothing_is_dropped_when_no_hook_is_retired():
    payload = _settings('python3 "x/scripts/hooks/lint-fix.py"')
    pruned, dropped = ps.prune_hook_commands(payload, ("scripts/hooks/branch-per-task.py",))
    assert dropped == []
    assert pruned == payload


@pytest.mark.parametrize("payload", [None, [], "text", {}, {"hooks": "nonsense"}])
def test_a_settings_shape_this_does_not_understand_is_returned_untouched(payload):
    assert ps.prune_hook_commands(payload, ("scripts/hooks/branch-per-task.py",)) == (payload, [])


def test_prune_settings_rewrites_the_file(tmp_path):
    path = tmp_path / sh.SETTINGS_FILE
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(_settings('python3 "x/scripts/hooks/branch-per-task.py"')), encoding="utf-8"
    )
    assert sh.settings_pass(tmp_path, ("scripts/hooks/branch-per-task.py",)) == [
        f"(unwired retired hook) {sh.SETTINGS_FILE}: branch-per-task.py"
    ]
    assert "branch-per-task" not in path.read_text(encoding="utf-8")


def test_prune_settings_leaves_an_unparseable_file_exactly_as_it_was(tmp_path):
    """Rewriting a settings file this could not read is how a pull would take a
    project's whole harness config with it."""
    path = tmp_path / sh.SETTINGS_FILE
    path.parent.mkdir(parents=True)
    path.write_text("{ not json, branch-per-task.py", encoding="utf-8")
    assert sh.settings_pass(tmp_path, ("scripts/hooks/branch-per-task.py",)) == []
    assert path.read_text(encoding="utf-8") == "{ not json, branch-per-task.py"


def test_prune_settings_is_silent_when_there_is_no_settings_file(tmp_path):
    assert sh.settings_pass(tmp_path, ("scripts/hooks/branch-per-task.py",)) == []


def test_a_live_hook_merely_mentioning_a_retired_basename_is_kept():
    """Regression. Matching on the BASENAME made `README.md` a retired "hook", because
    `.claude/skills/state-tools/README.md` is in RETIRED_PATHS -- and carameli wires a
    markdownlint hook whose command lists `"README.md"` among its arguments. A pull
    would have silently deleted that hook from its settings.

    Matching on the repo-relative path is both precise and correct: a hook command
    embeds the path (`.../scripts/hooks/branch-on-write.py`), never the bare name.
    """
    lint = 'markdownlint-cli2 --config .config.yaml "docs/roadmap.md" "README.md"'
    payload = _settings(lint)
    pruned, dropped = ps.prune_hook_commands(payload, (".claude/skills/state-tools/README.md",))
    assert dropped == []
    assert pruned == payload


def test_only_scripts_can_be_retired_hooks():
    """A retired skill, rule or test file can never be a hook command, so it must not
    even be a candidate -- that is what keeps a name like `README.md` out of the
    matching set in the first place."""
    candidates = sh.retired_hook_paths(
        (
            "scripts/hooks/branch-on-write.py",
            "scripts/hooks/tests/test_branch_on_write.py",
            ".claude/skills/state-tools/README.md",
            ".claude/skills/test-skill/write-artifacts.py",
        )
    )
    assert "scripts/hooks/branch-on-write.py" in candidates
    assert not any(c.endswith(".md") for c in candidates)
    assert not any(c.startswith(".claude/") for c in candidates)


def test_the_retired_branch_hooks_are_listed_so_a_pull_unwires_them():
    """Reversion check: drop these from RETIRED_PATHS and every consumer keeps a hook
    entry pointing at a file the same pull deleted."""
    for rel in (
        "scripts/hooks/branch-per-task.py",
        "scripts/hooks/branch-on-write.py",
        "scripts/hooks/tests/test_branch_on_write.py",
    ):
        assert rel in sh.RETIRED_PATHS
        assert rel not in sh.MANIFEST


# --- un-vendoring is not retirement ------------------------------------------
# The defect: a path that leaves the MANIFEST because it became a `templates/` file was
# deleted from every consumer whose copy still matched the last pull, and nothing put it
# back -- `templates/` is a one-shot copy that only `new-project.py` reads. Two projects'
# PR gates died on an unresolvable local action; a third's nightly was the only workflow
# using it, so nothing failed where anyone was looking and it stayed broken for a week.


def _templated(src: Path, rel: str, text: str = "runs:\n") -> None:
    """Put `rel` into a source's `templates/core/` tier, under the generator's spelling."""
    parts = rel.split("/")
    parts[0] = f"dot-{parts[0][1:]}" if parts[0].startswith(".") else parts[0]
    path = src / "templates" / "core" / Path(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _receipted(root: Path, rel: str, text: str) -> None:
    """A file the last pull wrote, recorded in the receipt with its real digest."""
    _seed(root, rel, text)
    receipt = json.loads((root / sh.RECEIPT_FILE).read_text(encoding="utf-8"))
    receipt["files"][rel] = sh._sha256(root / rel)
    (root / sh.RECEIPT_FILE).write_text(json.dumps(receipt), encoding="utf-8")


def _blank_receipt(root: Path) -> None:
    (root / sh.RECEIPT_FILE).write_text(json.dumps({"files": {}}), encoding="utf-8")


ACTION = ".github/actions/setup-python-env/action.yml"


def test_an_unvendored_file_survives_the_pull_that_unvendored_it(tmp_path):
    """The regression, in the exact shape that hit three repos. The file left the
    MANIFEST, the consumer had never edited it, and `--pull` deleted it."""
    root, src = tmp_path / "project", tmp_path / "devkit"
    root.mkdir()
    _blank_receipt(root)
    _receipted(root, ACTION, "runs:\n  using: composite\n")
    _templated(src, ACTION)

    removed, preserved, unvendored = sh.remove_receipt_retired(root, (), src)

    assert (root / ACTION).is_file(), "the pull deleted a file nothing will put back"
    assert unvendored == [ACTION]
    assert removed == [] and preserved == []


def test_a_genuinely_retired_file_is_still_deleted(tmp_path):
    """The other half. A receipt entry that is in neither the MANIFEST nor `templates/`
    really is obsolete, and never tidying those leaves consumers accumulating dead
    files -- which is what this removal pass exists for."""
    root, src = tmp_path / "project", tmp_path / "devkit"
    root.mkdir()
    _blank_receipt(root)
    _receipted(root, "scripts/hooks/gone.py", "print('old')\n")
    (src / "templates" / "core").mkdir(parents=True)

    removed, _, unvendored = sh.remove_receipt_retired(root, (), src)

    assert removed == ["scripts/hooks/gone.py"]
    assert not (root / "scripts/hooks/gone.py").exists()
    assert unvendored == []


def test_a_locally_edited_file_is_still_preserved(tmp_path):
    """Unchanged behaviour, and the accident that saved the one repo that survived:
    its copy no longer matched, so the sha check spared it."""
    root, src = tmp_path / "project", tmp_path / "devkit"
    root.mkdir()
    _blank_receipt(root)
    _receipted(root, "scripts/hooks/edited.py", "original\n")
    _seed(root, "scripts/hooks/edited.py", "mine now\n")

    removed, preserved, unvendored = sh.remove_receipt_retired(root, (), src)

    assert preserved == ["scripts/hooks/edited.py"]
    assert removed == [] and unvendored == []


def test_a_source_without_templates_falls_back_to_deleting(tmp_path):
    """`src=None` and a source with no `templates/` are the same answer: cannot tell.
    Preserving everything there would turn every real retirement into permanent cruft."""
    root = tmp_path / "project"
    root.mkdir()
    _blank_receipt(root)
    _receipted(root, "scripts/hooks/gone.py", "print('old')\n")

    removed, _, unvendored = sh.remove_receipt_retired(root, (), None)

    assert removed == ["scripts/hooks/gone.py"]
    assert unvendored == []


def test_template_outputs_speaks_the_generators_spelling(tmp_path):
    """`dot-` is a leading dot and `.tmpl` is stripped, or nothing here compares equal
    to a MANIFEST path and the guard silently never fires."""
    src = tmp_path / "devkit"
    _templated(src, ACTION)
    _templated(src, ".github/workflows/pr-gate.yml", "on:\n")
    (src / "templates" / "core" / "dot-github" / "workflows" / "pr-gate.yml").rename(
        src / "templates" / "core" / "dot-github" / "workflows" / "pr-gate.yml.tmpl"
    )
    _templated(src, "scripts/notify.py", "print(1)\n")

    outputs = sh.template_outputs(src)

    assert ACTION in outputs
    assert ".github/workflows/pr-gate.yml" in outputs, "the .tmpl suffix is not part of it"
    assert "scripts/notify.py" in outputs, "a path with no leading dot is untouched"


def test_template_outputs_is_empty_when_there_is_nothing_to_read(tmp_path):
    assert sh.template_outputs(None) == set()
    assert sh.template_outputs(tmp_path / "no-such-devkit") == set()


def test_the_setup_action_is_the_case_this_guards(tmp_path):
    """Reversion check, naming the file: un-vendor it again with this guard removed and
    every consumer that never customised it loses the action on its next pull."""
    assert ACTION not in sh.MANIFEST
    assert ACTION not in sh.RETIRED_PATHS, "it was un-vendored, not retired"


# --- the generated Codex artifact ------------------------------------------
# `--pull` copies the generator; the file Codex actually reads was written by the
# generator that came before it, and nothing on either side compared the two. So the
# Claude-only Bash cap kept blocking Codex sessions in every project that had already
# generated a `.codex/hooks.json` -- long after `sync-codex-hooks.py` stopped emitting
# it -- and each block's suggested remedy (`invoke-capped.py`) is a wrapper the session
# then carried on every command after it.

CODEX_SETTINGS = json.dumps(
    {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "^Bash$",
                    "hooks": [
                        {
                            "type": "command",
                            "command": (
                                'python3 "${CLAUDE_PROJECT_DIR:-.}/scripts/hooks/'
                                'enforce-capped-bash.py"'
                            ),
                        }
                    ],
                }
            ]
        }
    },
    indent=2,
)
# What the pre-`REDUNDANT_HANDLERS` generator wrote, and what every consumer that has
# not regenerated is still handing Codex. The cap command has to be really in here: an
# artifact that merely differs would let the assertions below pass without the fix.
STALE_CODEX_HOOKS = (
    json.dumps(
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "^Bash$",
                        "hooks": [
                            {
                                "type": "command",
                                "command": (
                                    'python3 "__CODEX_PROJECT_ROOT__/scripts/hooks/'
                                    'codex-hook-adapter.py" --event PreToolUse -- python3 '
                                    '"__CODEX_PROJECT_ROOT__/scripts/hooks/'
                                    'enforce-capped-bash.py"'
                                ),
                            }
                        ],
                    }
                ]
            }
        },
        indent=2,
    )
    + "\n"
)


def _codex_project(root: Path, hooks_json: str | None = STALE_CODEX_HOOKS) -> Path:
    """A project with the real generator vendored, so the subprocess call is the real one."""
    _seed(root, sh.SETTINGS_FILE, CODEX_SETTINGS)
    _seed(
        root,
        sh.CODEX_GENERATOR,
        (sh.REPO_ROOT / sh.CODEX_GENERATOR).read_text(encoding="utf-8"),
    )
    if hooks_json is not None:
        _seed(root, sh.CODEX_HOOKS_FILE, hooks_json)
    return root


def test_a_codex_artifact_from_an_older_generator_is_stale(tmp_path):
    assert sh.codex_hooks_stale(_codex_project(tmp_path)) is True


def test_a_project_that_never_opted_into_codex_is_not_stale(tmp_path):
    """Generating one here would wire Codex hooks into a project that asked for none."""
    root = _codex_project(tmp_path, hooks_json=None)
    assert sh.codex_hooks_stale(root) is False
    assert sh.regenerate_codex_hooks(root) is False
    assert not (root / sh.CODEX_HOOKS_FILE).exists()


def test_a_project_without_the_generator_is_not_stale(tmp_path):
    """A consumer several releases behind has the artifact and not yet the script. It
    must fail its gate on the vendored drift, not on a check that cannot run."""
    _seed(tmp_path, sh.SETTINGS_FILE, CODEX_SETTINGS)
    _seed(tmp_path, sh.CODEX_HOOKS_FILE, STALE_CODEX_HOOKS)
    assert sh.codex_hooks_stale(tmp_path) is False


def test_regenerate_drops_the_bash_cap_the_stale_artifact_still_carried(tmp_path):
    """The bug, end to end: after regeneration Codex no longer runs the Claude-only
    gate, so nothing blocks its shell commands into the invoke-capped spiral."""
    root = _codex_project(tmp_path)
    assert sh.regenerate_codex_hooks(root) is True
    rewritten = (root / sh.CODEX_HOOKS_FILE).read_text(encoding="utf-8")
    assert "enforce-capped-bash.py" not in rewritten
    assert sh.codex_hooks_stale(root) is False


def test_a_generator_that_speaks_is_still_heard(tmp_path, monkeypatch, capsys):
    """`regenerate_codex_hooks` spawns the generator with `CREATE_NO_WINDOW`, so a
    scheduled upgrade pass does not flash a console window per project. That flag hands
    the child a console of its own, and a child that captures nothing writes to *that* --
    so the spawn has to capture and re-emit, or the generator's account of the rewrite
    disappears from a `--pull` someone is reading.

    Today's generator happens to be silent on the write path, which is exactly why this
    is tested against a stub that is not: the property has to hold for the generator this
    becomes, and a test written against the silent one would assert nothing.
    """
    speaks = "import sys; print('wrote the file'); print('and warned', file=sys.stderr)"

    def generator(_root, *args):
        # Exit 1 for the staleness probe, so the regeneration is actually attempted.
        return [sys.executable, "-c", "raise SystemExit(1)" if "--check" in args else speaks]

    monkeypatch.setattr(sh, "_codex_generator", generator)
    assert sh.regenerate_codex_hooks(_codex_project(tmp_path)) is True
    captured = capsys.readouterr()
    assert "wrote the file" in captured.out
    assert "and warned" in captured.err


def test_regenerating_a_current_artifact_is_a_no_op(tmp_path):
    root = _codex_project(tmp_path)
    sh.regenerate_codex_hooks(root)
    assert sh.regenerate_codex_hooks(root) is False


def test_pull_regenerates_the_stale_codex_artifact(tmp_path, monkeypatch, capsys):
    """The wiring that matters: adopting the fixed generator must also rewrite the file
    Codex reads. Without this the pull lands, the gate goes green, and every Codex
    session keeps being blocked by the hook the pull was meant to remove."""
    src = _repo(tmp_path / "src", tag="v0.5.3", files={"scripts/hooks/x.py": "upstream"})
    repo = _codex_project(tmp_path / "proj")
    _seed(repo, sh.PRECOMMIT_FILE, CONFIG)
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/hooks/x.py",))

    assert sh.main(["--pull", "--src", str(src)]) == 0
    assert "enforce-capped-bash.py" not in (repo / sh.CODEX_HOOKS_FILE).read_text(encoding="utf-8")
    assert sh.CODEX_HOOKS_FILE in capsys.readouterr().out


def test_check_fails_on_a_stale_codex_artifact_with_everything_else_in_sync(
    tmp_path, monkeypatch, capsys
):
    """A project can be byte-perfect on every vendored file and still be running hook
    wiring it no longer describes -- which is precisely the state every consumer was in."""
    src = _repo(tmp_path / "src", tag="v0.5.3", files={"scripts/hooks/x.py": "upstream"})
    repo = _codex_project(tmp_path / "proj")
    _seed(repo, "scripts/hooks/x.py", "upstream")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/hooks/x.py",))

    assert sh.main(["--check", "--src", str(src)]) != 0
    reported = capsys.readouterr().err
    assert "STALE" in reported
    # Not "the vendored harness drifted, run --pull": nothing upstream differs, and
    # advice that does not describe the failure is how a red gate becomes noise.
    assert "drifted from the shared repo" not in reported


def test_check_passes_once_the_codex_artifact_is_regenerated(tmp_path, monkeypatch):
    src = _repo(tmp_path / "src", tag="v0.5.3", files={"scripts/hooks/x.py": "upstream"})
    repo = _codex_project(tmp_path / "proj")
    _seed(repo, "scripts/hooks/x.py", "upstream")
    sh.regenerate_codex_hooks(repo)
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/hooks/x.py",))

    assert sh.main(["--check", "--src", str(src)]) == 0


# --- the interpreter the generator is spawned with --------------------------
# `NO_WINDOW` above is necessary and not sufficient, and the gap is invisible until a
# scheduled job hits it: Windows **ignores** `CREATE_NO_WINDOW` for a GUI-subsystem
# child, so spawning `sys.executable` from a job -- where it is `pythonw.exe` -- leaves
# the child with no console at all, and Windows then allocates a fresh visible one for
# each of *its* children. This script is exactly that hop: a nightly upgrade pass spawns
# it per project, and it spawns `git` and the codex generator in turn. The flag on the
# spawn that started it bought nothing; `console_python` is what makes the flag on the
# spawns below apply to anything.


def test_the_generator_is_spawned_with_a_console_interpreter(tmp_path, monkeypatch):
    console = tmp_path / "python.exe"
    gui = tmp_path / "pythonw.exe"
    for path in (console, gui):
        path.write_text("", encoding="utf-8")
    monkeypatch.setattr(sh.sys, "executable", str(gui))
    assert sh.console_python() == str(console)
    assert sh._codex_generator(Path("/repo"))[0] == str(console)


def test_a_console_interpreter_is_left_alone(tmp_path, monkeypatch):
    """Every interactive caller, every POSIX machine, and this test run."""
    console = tmp_path / "python.exe"
    console.write_text("", encoding="utf-8")
    monkeypatch.setattr(sh.sys, "executable", str(console))
    assert sh.console_python() == str(console)


def test_a_missing_console_twin_falls_back_rather_than_raising(tmp_path, monkeypatch):
    """An embedded install can ship `pythonw.exe` alone. Raising here would fail a
    `--pull` over a window that may not even appear."""
    gui = tmp_path / "pythonw.exe"
    gui.write_text("", encoding="utf-8")
    monkeypatch.setattr(sh.sys, "executable", str(gui))
    assert sh.console_python() == str(gui)


# --- adopting the untested-symbol ratchet ------------------------------------
# The baseline is per-project debt and is never vendored, so a pull that delivered
# the gate without writing one would turn the consumer's next PR gate red on every
# public callable it has. Seeding here is the whole reason that is not what happens.

SCANNER = "scripts/hooks/untested_symbols.py"
CONFIG_MODULE = "scripts/hooks/harness_config.py"


def _ratchet_project(root: Path, source: str = "def alpha():\n    pass\n") -> Path:
    """A project with the real scanner vendored, since `--seed` runs it for real.

    Written as UTF-8 explicitly: these two are prose-heavy and the default encoding on
    a Windows console is not UTF-8, so `_seed` would hand the subprocess a file its own
    interpreter cannot parse — a failure that reads as the seeder being broken.
    """
    for rel in (SCANNER, CONFIG_MODULE):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text((sh.REPO_ROOT / rel).read_text(encoding="utf-8"), encoding="utf-8")
    # `sources` narrowed to the project's own tree: the vendored hooks above are code
    # devkit answers for, and a consumer's debt list is not the place to record it.
    _seed(root, ".devkit.toml", '[paths]\napp = "src/"\n\n[test_contract]\nsources = ["src"]\n')
    _seed(root, "src/acme.py", source)
    return root


def test_seeding_records_the_debt_the_project_already_has(tmp_path):
    root = _ratchet_project(tmp_path)
    assert sh.seed_untested_baseline(root) == 1
    assert "src/acme.py::alpha" in sh.read_untested_baseline(root)


def test_seeding_is_skipped_when_the_project_has_already_adopted(tmp_path):
    """Called on every pull, so this is the common case. Re-seeding would launder
    whatever went untested since adoption into the debt list."""
    root = _ratchet_project(tmp_path)
    _seed(root, sh.UNTESTED_BASELINE_FILE, "")
    assert sh.seed_untested_baseline(root) is None
    assert sh.read_untested_baseline(root) == []


def test_seeding_is_skipped_when_the_scanner_was_not_vendored(tmp_path):
    """A `--pull` from a devkit old enough not to ship it, or a partial manifest."""
    _seed(tmp_path, ".devkit.toml", "")
    assert sh.seed_untested_baseline(tmp_path) is None
    assert not (tmp_path / sh.UNTESTED_BASELINE_FILE).exists()


def test_a_scanner_that_fails_leaves_no_half_written_baseline(tmp_path):
    """Best-effort, like every other post-copy step: a pull must not die on it."""
    root = _ratchet_project(tmp_path)
    _seed(root, SCANNER, "raise SystemExit(3)\n")
    assert sh.seed_untested_baseline(root) is None


def test_the_baseline_reader_drops_comments_and_blank_lines(tmp_path):
    _seed(tmp_path, sh.UNTESTED_BASELINE_FILE, "# header\n\nsrc/acme.py::alpha\n")
    assert sh.read_untested_baseline(tmp_path) == ["src/acme.py::alpha"]


def test_the_baseline_of_a_project_that_never_adopted_reads_empty(tmp_path):
    assert sh.read_untested_baseline(tmp_path) == []


def test_the_baseline_is_not_vendored(tmp_path):
    """It is one repo's debt. Vendoring it would ship devkit's list into every
    consumer and mark their untested code as covered."""
    assert sh.UNTESTED_BASELINE_FILE not in sh.MANIFEST


# --- seeding the structure ratchet ---------------------------------------------------
#
# Same shape as the untested-symbol seeding above, for the same reason: the pull that
# delivers the gate must not be the pull that reddens it.

STRUCTURE_CHECKER = "scripts/hooks/structure_check.py"
STRUCTURE_SCANNER = "scripts/hooks/structure_scan.py"


def _structure_project(root: Path, source: str = "x = 1  # noqa\n") -> Path:
    for rel in (STRUCTURE_CHECKER, STRUCTURE_SCANNER, CONFIG_MODULE):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text((sh.REPO_ROOT / rel).read_text(encoding="utf-8"), encoding="utf-8")
    # `paths` narrowed to the project's own tree, so the vendored hooks copied in
    # above are not measured as the consumer's debt.
    _seed(root, ".devkit.toml", '[paths]\napp = "src/"\n\n[structure]\npaths = ["src"]\n')
    _seed(root, "src/main.py", source)
    return root


def test_structure_seeding_records_the_debt_the_project_already_has(tmp_path):
    root = _structure_project(tmp_path)
    assert sh.seed_structure_baseline(root) == 1
    assert "suppressions::src/main.py = 1" in sh.read_structure_baseline(root)


def test_structure_seeding_is_skipped_when_the_project_has_already_adopted(tmp_path):
    root = _structure_project(tmp_path)
    _seed(root, sh.STRUCTURE_BASELINE_FILE, "")
    assert sh.seed_structure_baseline(root) is None
    assert sh.read_structure_baseline(root) == []


def test_structure_seeding_is_skipped_when_the_checker_was_not_vendored(tmp_path):
    _seed(tmp_path, ".devkit.toml", "")
    assert sh.seed_structure_baseline(tmp_path) is None
    assert not (tmp_path / sh.STRUCTURE_BASELINE_FILE).exists()


def test_a_checker_that_fails_leaves_no_structure_baseline(tmp_path):
    root = _structure_project(tmp_path)
    _seed(root, STRUCTURE_CHECKER, "raise SystemExit(3)\n")
    assert sh.seed_structure_baseline(root) is None


def test_the_structure_baseline_reader_drops_comments_and_blank_lines(tmp_path):
    _seed(tmp_path, sh.STRUCTURE_BASELINE_FILE, "# header\n\nfile_lines::src/a.py = 900\n")
    assert sh.read_structure_baseline(tmp_path) == ["file_lines::src/a.py = 900"]


def test_the_structure_baseline_is_not_vendored():
    assert sh.STRUCTURE_BASELINE_FILE not in sh.MANIFEST
    assert STRUCTURE_CHECKER in sh.MANIFEST
    assert STRUCTURE_SCANNER in sh.MANIFEST


def test_pull_adopts_the_ratchet_and_says_so(tmp_path, monkeypatch, capsys):
    """End to end, because the ordering is the part that can go wrong: seeding runs the
    scanner the same pull just copied in, so it has to happen after the copy."""
    src, dst = tmp_path / "shared", tmp_path / "proj"
    _seed(src, "scripts/x.py", "v1")
    _ratchet_project(dst)
    monkeypatch.setattr(sh, "REPO_ROOT", dst)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/x.py",))
    monkeypatch.setattr(sh, "git_head", lambda p: "abc1234")
    assert sh.main(["--pull", "--src", str(src), "--allow-untagged"]) == 0
    assert "untested-symbol ratchet" in capsys.readouterr().out
    assert sh.read_untested_baseline(dst) == ["src/acme.py::alpha"]


def test_a_second_pull_does_not_say_it_again(tmp_path, monkeypatch, capsys):
    """Adoption happens once. Reporting it on every pull would read as the debt being
    rewritten each time, which is the one thing the seeder must never do."""
    src, dst = tmp_path / "shared", tmp_path / "proj"
    _seed(src, "scripts/x.py", "v1")
    _ratchet_project(dst)
    monkeypatch.setattr(sh, "REPO_ROOT", dst)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/x.py",))
    monkeypatch.setattr(sh, "git_head", lambda p: "abc1234")
    sh.main(["--pull", "--src", str(src), "--allow-untagged"])
    capsys.readouterr()
    assert sh.main(["--pull", "--src", str(src), "--allow-untagged"]) == 0
    assert "untested-symbol ratchet" not in capsys.readouterr().out


# --- the settings pass, from the two modes that drive it ----------------------
# `project_settings.py` owns the edits and is tested there; these two hold the wiring
# between the modes and that tier, which is the half that was missing for a release --
# the shim shipped in the MANIFEST and no command anywhere ran it.


def test_the_two_modules_name_the_same_settings_file():
    """The one constant that is spelled twice, and why. `sync-devkit.py` must import the
    settings tier lazily -- a project's first pull runs it before that file exists -- so
    it cannot read the path from there at module scope. Two copies with no gate is how a
    rename lands in one of them; this is the gate."""
    assert sh.SETTINGS_FILE == ps.SETTINGS_FILE


def test_the_bootstrap_pull_runs_without_the_settings_tier(tmp_path, monkeypatch, capsys):
    """A generated project runs the copy of `sync-devkit.py` the generator placed there,
    at a moment when nothing else in the MANIFEST exists. An import at module scope made
    that pull a traceback -- the one pull that cannot be retried differently."""
    src, dst = tmp_path / "shared", tmp_path / "proj"
    _seed(src, "scripts/x.py", "v1")
    monkeypatch.setattr(sh, "REPO_ROOT", dst)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/x.py",))
    monkeypatch.setattr(sh, "git_head", lambda p: "abc1234")
    monkeypatch.setitem(sys.modules, "project_settings", None)
    assert sh.main(["--pull", "--src", str(src), "--allow-untagged"]) == 0
    assert sh.settings_pass(dst) == []
    assert sh.retired_hook_paths() == ()
    assert sh.local_faults(dst) == ([], "")
    assert "Traceback" not in capsys.readouterr().err


def _unguarded_project(root: Path) -> Path:
    _seed(root, ps.GUARD_HOOK, "# the shim\n")
    _seed(root, sh.SETTINGS_FILE, "{}")
    return root


def test_pull_wires_the_guard_and_says_so(tmp_path, monkeypatch, capsys):
    """End to end, because the ordering is the part that can go wrong: the wiring names
    a file the same pull delivers, so it has to run after the copy."""
    src, dst = tmp_path / "shared", tmp_path / "proj"
    _seed(src, "scripts/x.py", "v1")
    _unguarded_project(dst)
    monkeypatch.setattr(sh, "REPO_ROOT", dst)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/x.py",))
    monkeypatch.setattr(sh, "git_head", lambda p: "abc1234")
    assert sh.main(["--pull", "--src", str(src), "--allow-untagged"]) == 0
    assert "cross-checkout edit guard" in capsys.readouterr().out
    assert ps.guard_unwired(dst) is False


def test_check_fails_on_an_unwired_guard_without_calling_it_drift(tmp_path, monkeypatch, capsys):
    """`--pull` fixes it, but nothing upstream differs -- and "the harness drifted"
    sends the reader to compare files that are identical."""
    src, dst = tmp_path / "shared", tmp_path / "proj"
    _seed(src, "scripts/x.py", "v1")
    _seed(dst, "scripts/x.py", "v1")
    _unguarded_project(dst)
    monkeypatch.setattr(sh, "REPO_ROOT", dst)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/x.py",))
    assert sh.main(["--src", str(src)]) == 1
    err = capsys.readouterr().err
    assert "UNWIRED" in err
    assert "no hook runs the cross-checkout edit guard" in err
    assert "drifted from the shared repo" not in err


def test_check_passes_once_the_guard_is_wired(tmp_path, monkeypatch):
    src, dst = tmp_path / "shared", tmp_path / "proj"
    _seed(src, "scripts/x.py", "v1")
    _seed(dst, "scripts/x.py", "v1")
    sh.settings_pass(_unguarded_project(dst))
    monkeypatch.setattr(sh, "REPO_ROOT", dst)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/x.py",))
    assert sh.main(["--src", str(src)]) == 0


def test_the_retired_list_reaches_the_settings_pass_from_this_module(tmp_path, monkeypatch):
    """`RETIRED_PATHS` is read at call time so a caller can replace it on this module;
    the pass must not have captured its own copy at import."""
    root = _unguarded_project(tmp_path)
    command = 'python3 "x/scripts/hooks/made-up.py"'
    _seed(
        root, sh.SETTINGS_FILE, json.dumps({"hooks": {"Stop": [{"hooks": [{"command": command}]}]}})
    )
    monkeypatch.setattr(sh, "RETIRED_PATHS", ("scripts/hooks/made-up.py",))
    assert any("made-up.py" in note for note in sh.settings_pass(root))
