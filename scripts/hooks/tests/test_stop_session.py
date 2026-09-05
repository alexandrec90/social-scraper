"""Whose work a Stop is about, and in which tree.

Vendored, so nothing here may assume one project's layout: every transcript and every
lease file is built in `tmp_path`, and the only shape asserted on is the harness's own.
"""

from __future__ import annotations

import json
from pathlib import Path

from conftest import load_module

hook = load_module("scripts/hooks/stop_session.py")


def _leases(root: Path, boxes: dict) -> None:
    """A workspace lease file beside `root`, the way `worktree.py` writes one."""
    path = root.parent / hook.BOXES_DIR_NAME / hook.LEASE_FILE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"boxes": boxes}), encoding="utf-8")


def _box(root: Path, name: str) -> Path:
    path = root.parent / hook.BOXES_DIR_NAME / name
    (path / ".git").mkdir(parents=True)
    return path


def _payload(tmp_path: Path, *records) -> str:
    """A stop payload naming a transcript holding `records`, one JSON object per line."""
    path = tmp_path / "transcript.jsonl"
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8", newline="\n"
    )
    return json.dumps({"session_id": "s1", "transcript_path": str(path)})


def _tool_use(name: str) -> dict:
    """The shape a transcript records a tool call in, nested as the harness nests it."""
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name}]}}


# --- which tree ------------------------------------------------------------


def test_the_session_box_is_preferred_over_the_checkout(tmp_path):
    """A session whose every edit was routed into a box had its gate pointed at a
    checkout it never touched, and blocked on whatever branch that checkout was on."""
    root = tmp_path / "proj"
    root.mkdir()
    box = _box(root, "proj--task-0101")
    _leases(root, {"proj--task-0101": {"project": "proj", "session": "abcdef0123", "kind": "task"}})

    assert hook.session_box("abcdef0123", root) == box
    assert hook.verify_root(json.dumps({"session_id": "abcdef0123"}), root) == box


def test_absence_falls_through_to_the_checkout(tmp_path):
    """Every consuming project has no `.worktrees/` at all; so does CI and a fresh clone.
    All of them must behave exactly as they did before this existed."""
    root = tmp_path / "proj"
    root.mkdir()
    assert hook.session_box("abcdef0123", root) is None
    assert hook.verify_root("{}", root) == root
    assert hook.verify_root("not json", root) == root


def test_a_preview_box_is_never_this_sessions_work(tmp_path):
    """A throwaway copy of somebody else's branch: verifying it would block the session
    on a tree it did not write and cannot fix."""
    root = tmp_path / "proj"
    root.mkdir()
    _box(root, "proj--preview-0101")
    _leases(
        root,
        {"proj--preview-0101": {"project": "proj", "session": "abcdef0123", "kind": "preview"}},
    )
    assert hook.session_box("abcdef0123", root) is None


def test_a_husk_falls_back_rather_than_verifying_an_untracked_tree(tmp_path):
    """A box whose `git worktree remove` died partway has no `.git`, and every check
    would run against a tree git has stopped tracking."""
    root = tmp_path / "proj"
    root.mkdir()
    (root.parent / hook.BOXES_DIR_NAME / "proj--task-0101").mkdir(parents=True)
    _leases(root, {"proj--task-0101": {"project": "proj", "session": "abcdef0123", "kind": "task"}})
    assert hook.session_box("abcdef0123", root) is None


def test_an_unreadable_lease_file_is_none_not_an_exception(tmp_path):
    """A Stop gate must never fail *because* of this lookup; the worst it may do is
    decline to improve on the default."""
    root = tmp_path / "proj"
    root.mkdir()
    path = root.parent / hook.BOXES_DIR_NAME / hook.LEASE_FILE_NAME
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert hook.session_box("abcdef0123", root) is None
    assert hook.session_box("", root) is None


def test_session_ids_match_by_prefix_but_not_by_a_short_one():
    """A box cut by hand carries `--session <first 8 hex>`, so the abbreviation has to
    keep naming the session that abbreviated it -- and a two-character overlap must not."""
    assert hook.sessions_match("abcdef0123", "abcdef0123")
    assert hook.sessions_match("abcdef01", "abcdef0123456")
    assert not hook.sessions_match("abc", "abcdef0123")
    assert not hook.sessions_match("", "abcdef0123")
    assert not hook.sessions_match("abcdef0123", "")


def test_the_session_id_is_read_off_the_payload():
    assert hook.session_id('{"session_id": "abc123"}') == "abc123"
    assert hook.session_id('{"session_id": "  abc123  "}') == "abc123"
    assert hook.session_id('{"session_id": 7}') == ""
    assert hook.session_id("[]") == ""
    assert hook.session_id("not json") == ""


def test_payload_is_a_dict_or_nothing():
    assert hook.payload('{"a": 1}') == {"a": 1}
    assert hook.payload("[1, 2]") == {}
    assert hook.payload("not json") == {}


# --- whose work ------------------------------------------------------------


def test_a_session_that_only_read_files_wrote_nothing(tmp_path):
    """The reported case: an advice session, blocked four times on failures another
    session in the same checkout was mid-way through committing."""
    assert hook.session_wrote_nothing(_payload(tmp_path, _tool_use("Read"), _tool_use("Grep")))


def test_one_write_tool_anywhere_settles_it(tmp_path):
    """The narrowness is the point. A `Bash` call can change a file with no write-tool
    use, so only an *empty* write set is a claim the transcript supports on its own; a
    partial one would silently shrink the gate."""
    assert hook.session_wrote_nothing(_payload(tmp_path, _tool_use("Read"), _tool_use("Edit"))) is (
        False
    )
    assert hook.session_wrote_nothing(_payload(tmp_path, _tool_use("Write"))) is False


def test_a_tool_name_appearing_as_text_is_not_a_use_of_it(tmp_path):
    """The cheap substring reject is a filter, never the answer: the record is parsed
    before anything is concluded from it."""
    assert hook.session_wrote_nothing(_payload(tmp_path, {"type": "user", "text": "Edit that"}))


def test_an_unreadable_transcript_reads_as_wrote_something(tmp_path):
    """Fails to False in every direction. The ordinary behaviour of the gate is to
    verify, and a hook that cannot read its own payload must not be what turns it off."""
    assert hook.session_wrote_nothing("{}") is False
    assert hook.session_wrote_nothing("not json") is False
    assert hook.session_wrote_nothing('{"transcript_path": ""}') is False
    gone = json.dumps({"transcript_path": str(tmp_path / "no.jsonl")})
    assert hook.session_wrote_nothing(gone) is False

    broken = tmp_path / "transcript.jsonl"
    broken.write_text('{"name": "Edit"\n', encoding="utf-8")
    assert hook.session_wrote_nothing(json.dumps({"transcript_path": str(broken)})) is False


def test_transcript_path_returns_none_when_the_payload_does_not_name_one():
    assert hook.transcript_path("{}") is None
    assert hook.transcript_path("[]") is None
    assert hook.transcript_path('{"transcript_path": "   "}') is None
    assert hook.transcript_path('{"transcript_path": "a.jsonl"}') == Path("a.jsonl")


def test_uses_a_write_tool_walks_lists_and_dicts_alike():
    """Walks rather than indexes: the envelope is the harness's to change, and a shape
    assumption that stopped matching would read as "no session ever writes anything"."""
    assert hook.uses_a_write_tool([{"deep": {"type": "tool_use", "name": "NotebookEdit"}}])
    assert not hook.uses_a_write_tool([{"deep": {"type": "tool_use", "name": "Read"}}])
    assert not hook.uses_a_write_tool({"name": "Edit"})  # a name with no tool_use type
    assert not hook.uses_a_write_tool("Edit")
