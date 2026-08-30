"""Tests for the Codex command-hook compatibility adapter."""

import json
import subprocess
from pathlib import Path

from conftest import load_module

hook = load_module("scripts/hooks/codex-hook-adapter.py")


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_pretool_failure_becomes_json_denial(monkeypatch, tmp_path):
    monkeypatch.setattr(
        hook.subprocess,
        "run",
        lambda *args, **kwargs: _completed(42, stdout="blocked by project policy"),
    )

    code, stdout, stderr = hook.run_hook(
        "PreToolUse", ["python3", "guard.py"], "{}", repo_root=tmp_path
    )

    assert code == 0
    assert stderr == ""
    payload = json.loads(stdout)
    specific = payload["hookSpecificOutput"]
    assert specific["hookEventName"] == "PreToolUse"
    assert specific["permissionDecision"] == "deny"
    assert specific["permissionDecisionReason"] == "blocked by project policy"


def test_stop_failure_becomes_continuation_decision(monkeypatch, tmp_path):
    monkeypatch.setattr(
        hook.subprocess,
        "run",
        lambda *args, **kwargs: _completed(2, stderr="lint failed"),
    )

    code, stdout, stderr = hook.run_hook("Stop", ["python3", "stop.py"], "{}", repo_root=tmp_path)

    assert code == 0
    assert stderr == ""
    assert json.loads(stdout) == {"decision": "block", "reason": "lint failed"}


def test_posttool_failure_becomes_block_feedback(monkeypatch, tmp_path):
    monkeypatch.setattr(
        hook.subprocess,
        "run",
        lambda *args, **kwargs: _completed(2, stderr="formatting failed"),
    )

    _, stdout, _ = hook.run_hook(
        "PostToolUse", ["python3", "lint-fix.py"], "{}", repo_root=tmp_path
    )

    assert json.loads(stdout) == {
        "decision": "block",
        "reason": "formatting failed",
    }


def test_permission_failure_uses_permission_request_schema(monkeypatch, tmp_path):
    monkeypatch.setattr(
        hook.subprocess,
        "run",
        lambda *args, **kwargs: _completed(42, stderr="approval denied"),
    )

    _, stdout, _ = hook.run_hook(
        "PermissionRequest", ["python3", "policy.py"], "{}", repo_root=tmp_path
    )

    assert json.loads(stdout) == {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {
                "behavior": "deny",
                "message": "approval denied",
            },
        }
    }


def test_compaction_failure_uses_common_stop_fields(monkeypatch, tmp_path):
    monkeypatch.setattr(
        hook.subprocess,
        "run",
        lambda *args, **kwargs: _completed(1, stderr="compact guard failed"),
    )

    _, stdout, _ = hook.run_hook("PreCompact", ["python3", "guard.py"], "{}", repo_root=tmp_path)

    assert json.loads(stdout) == {
        "continue": False,
        "stopReason": "compact guard failed",
        "systemMessage": "compact guard failed",
    }


def test_success_preserves_output_and_sets_project_dir(monkeypatch, tmp_path):
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return _completed(0, stdout='{"continue":true}\n', stderr="note\n")

    monkeypatch.setattr(hook.subprocess, "run", fake_run)

    code, stdout, stderr = hook.run_hook(
        "UserPromptSubmit", ["python3", "branch.py"], '{"prompt":"x"}', repo_root=tmp_path
    )

    assert (code, stdout, stderr) == (0, '{"continue":true}\n', "")
    assert captured["cwd"] == tmp_path
    assert captured["env"]["CLAUDE_PROJECT_DIR"] == str(tmp_path)
    assert captured["env"]["PYTHONUTF8"] == "1"
    assert captured["input"] == '{"prompt":"x"}'


def test_the_adapter_announces_itself_to_the_hooks_it_runs(monkeypatch, tmp_path):
    """`CLAUDE_PROJECT_DIR` above makes a hook look like it is running under Claude
    Code, and the two response contracts are not the same in both directions: a rc-0
    hook's stdout is passed through verbatim, so a Claude-only member is silently
    dropped and the unmodified call proceeds. A hook that would be unsafe under that
    needs a way to tell, and only this process knows."""
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return _completed(0, stdout="", stderr="")

    monkeypatch.setattr(hook.subprocess, "run", fake_run)
    monkeypatch.delenv(hook.ADAPTER_ENV, raising=False)

    hook.run_hook("PreToolUse", ["python3", "guard.py"], "{}", repo_root=tmp_path)

    assert captured["env"][hook.ADAPTER_ENV] == "codex"


def test_session_start_success_suppresses_operational_output(monkeypatch, tmp_path):
    monkeypatch.setattr(
        hook.subprocess,
        "run",
        lambda *args, **kwargs: _completed(
            0,
            stdout="[session-start] branch sync skipped\n",
            stderr="informational detail\n",
        ),
    )

    code, stdout, stderr = hook.run_hook(
        "SessionStart", ["python3", "session-start.py"], "{}", repo_root=tmp_path
    )

    assert (code, stdout, stderr) == (0, "", "")


def test_missing_command_is_reported_without_failing_adapter():
    code, stdout, stderr = hook.run_hook("SessionStart", [], "{}")
    assert code == 0
    assert stderr == ""
    assert "no command" in json.loads(stdout)["systemMessage"].lower()


def test_failure_reason_is_byte_capped_with_head_and_tail():
    text = "A" * 5000 + "TAIL"
    capped = hook.cap_reason(text, max_bytes=100)
    assert len(capped.encode()) <= 100
    assert capped.startswith("A")
    assert capped.endswith("TAIL")
    assert "truncated" in capped


def test_parse_args_strips_separator():
    args = hook.parse_args(["--event", "Stop", "--", "python3", "stop.py"])
    assert args.event == "Stop"
    assert args.command == ["python3", "stop.py"]


# --- The success path: what a rc-0 hook's stdout becomes ----------------------------
#
# The failure path above is the loud one. These cover the quiet one: a hook that exits 0
# has its stdout passed through, and whether Codex acts on it is decided by a schema
# this repo does not own -- so a hook that grows a member Codex will not take stops
# taking effect rather than starting to fail.
#
# The classification is read from `codex-hook-schema.json`, extracted from the Codex
# binary. These tests read the committed snapshot rather than a fixture, because a
# fixture would let the adapter agree with an invented contract; the snapshot is the
# only copy of the real one that exists offline.


def test_response_members_dots_the_nested_ones():
    found = hook.response_members(
        {"decision": "block", "hookSpecificOutput": {"hookEventName": "PreToolUse"}}
    )
    assert set(found) == {"decision", "hookSpecificOutput", "hookSpecificOutput.hookEventName"}


def test_response_members_of_a_non_object_is_empty():
    """A hook printing prose is emitting context, not a decision."""
    assert hook.response_members("just some text") == []
    assert hook.response_members(["a", "list"]) == []


def test_the_snapshot_is_the_contract_this_adapter_judges_by():
    """No snapshot, no findings: a vendored copy predating it must not start refusing."""
    assert hook.accepted_members("PreToolUse")
    assert hook.accepted_members("PreToolUse", schema_file=Path("nope.json")) == frozenset()
    assert hook.accepted_members("NotAnEvent") == frozenset()


def test_the_accepted_set_is_per_event_not_global():
    """The defect that a flat allowlist cannot express, whatever it contains.

    PreToolUse's verdict member is `permissionDecision`; PermissionRequest's is
    `decision`. Each is a *lost decision* under the other's event.
    """
    assert hook.classify_member("hookSpecificOutput.permissionDecision", "PreToolUse") == "portable"
    assert hook.classify_member("hookSpecificOutput.permissionDecision", "Stop") == "lost"
    assert hook.classify_member("hookSpecificOutput.decision", "PermissionRequest") == "portable"


def test_updated_input_is_portable_on_pretooluse():
    """The reversion check for the guess this replaced.

    A hand-written list called `hookSpecificOutput.updatedInput` Claude-only, on the
    strength of the guard having been changed to stop emitting it. Codex's own
    PreToolUse schema accepts it -- so refusing it would have converted every re-aim
    into a deny. If this fails, the snapshot was refreshed from a Codex that withdrew
    it, and `worktree-guard.redirect_blocker` is then load-bearing rather than cautious.
    """
    assert hook.classify_member("hookSpecificOutput.updatedInput", "PreToolUse") == "portable"


def test_an_unknown_event_claims_nothing_either_way():
    assert hook.classify_member("anything", "SomeFutureEvent") == "unknown-event"


def test_a_decorative_member_codex_does_not_take_is_dropped_not_lost():
    """`additionalContext` is real on PreToolUse and absent from Stop's schema."""
    assert hook.classify_member("hookSpecificOutput.additionalContext", "PreToolUse") == "portable"
    assert hook.classify_member("hookSpecificOutput.additionalContext", "Stop") == "dropped"


def test_response_problems_ignores_non_json_stdout():
    assert hook.response_problems("not json at all", "PreToolUse") == ([], [])
    assert hook.response_problems("", "PreToolUse") == ([], [])


def test_response_problems_separates_the_two_kinds():
    payload = json.dumps(
        {"decision": "block", "hookSpecificOutput": {"hookEventName": "Stop", "brandNew": True}}
    )
    lost, dropped = hook.response_problems(payload, "Stop")
    assert lost == []
    assert dropped == [
        "hookSpecificOutput",
        "hookSpecificOutput.hookEventName",
        "hookSpecificOutput.brandNew",
    ]


def test_strip_members_removes_only_what_it_is_given():
    payload = json.dumps({"decision": "block", "reason": "no", "hookSpecificOutput": {"a": 1}})
    kept = json.loads(hook.strip_members(payload, ["hookSpecificOutput"]))
    assert kept == {"decision": "block", "reason": "no"}


def test_strip_members_reaches_a_nested_one():
    payload = json.dumps({"hookSpecificOutput": {"hookEventName": "Stop", "novel": 1}})
    kept = json.loads(hook.strip_members(payload, ["hookSpecificOutput.novel"]))
    assert kept == {"hookSpecificOutput": {"hookEventName": "Stop"}}


def test_strip_members_leaves_prose_alone():
    assert hook.strip_members("not json", ["decision"]) == "not json"


def test_dropped_reason_carries_the_hooks_own_words():
    payload = json.dumps(
        {
            "decision": "block",
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": "re-issue against the box",
            },
        }
    )
    reason = hook.dropped_reason("PermissionRequest", ["decision"], payload)
    assert "decision" in reason
    assert "re-issue against the box" in reason


def test_translate_success_refuses_a_lost_decision(tmp_path, monkeypatch):
    """PermissionRequest's schema has no top-level `decision`, and it is the verdict."""
    monkeypatch.setattr(hook, "_record_gap", lambda *args: None)
    payload = json.dumps({"decision": "block", "reason": "denied"})
    stdout, lost, _ = hook.translate_success("PermissionRequest", payload, repo_root=tmp_path)
    assert lost == ["decision", "reason"]
    assert json.loads(stdout)["hookSpecificOutput"]["decision"]["behavior"] == "deny"


def test_translate_success_leaves_a_portable_response_alone(tmp_path):
    payload = json.dumps({"decision": "block", "reason": "no"})
    stdout, lost, dropped = hook.translate_success("PostToolUse", payload, repo_root=tmp_path)
    assert (stdout, lost, dropped) == (payload, [], [])


def test_translate_success_strips_a_member_codex_would_reject(tmp_path, monkeypatch):
    """Stripped rather than passed through: every Codex output schema is
    `additionalProperties: false`, so one unknown member costs the whole response."""
    seen = []
    monkeypatch.setattr(hook, "_record_gap", lambda *args: seen.append(args))
    payload = json.dumps(
        {
            "decision": "block",
            "reason": "no",
            "suppressOutput": True,
            "hookSpecificOutput": {"hookEventName": "Stop"},
        }
    )
    stdout, lost, dropped = hook.translate_success("Stop", payload, repo_root=tmp_path)
    assert lost == []
    assert "hookSpecificOutput" in dropped
    kept = json.loads(stdout)
    assert kept == {"decision": "block", "reason": "no", "suppressOutput": True}
    assert seen and seen[0][3] == "dropped"


def test_session_start_still_emits_nothing(monkeypatch, tmp_path):
    """The one event whose stdout is discarded outright; classification must not undo it."""
    monkeypatch.setattr(
        hook.subprocess, "run", lambda *a, **k: _completed(0, stdout="workspace status")
    )
    code, stdout, _ = hook.run_hook("SessionStart", ["python3", "s.py"], "{}", repo_root=tmp_path)
    assert (code, stdout) == (0, "")
