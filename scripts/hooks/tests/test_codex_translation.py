"""The gate on the third delivery path: what Claude's hooks become under Codex.

**This file is vendored into every consuming project**, so nothing here may assert a
value specific to one repo. Everything it checks is derived from that project's own
`.claude/settings.json` and from the sources it names.

The gap this closes, in the words of `scripts/CLAUDE.md`: vendoring a generator does not
vendor its output. `sync-devkit.py --pull` adopts a new `sync-codex-hooks.py` and changes
nothing about what Codex actually runs, and `codex_hooks_stale` closes that half -- the
committed `.codex/hooks.json` has to match what the generator would write today.

What neither of them can see is the half this file is for: **the wiring can be perfectly
in sync and the translation still be wrong.** A hook that exits 0 has its stdout handed
to Codex, which validates it against a fixed per-event schema -- so a hook that grows a
member that schema does not carry does not begin to fail, it stops taking effect, or
worse takes its whole response down with it. There is no exit code, no stderr and no red
anywhere; the only evidence is a commit on the wrong branch, days later.

That is also why this lives in the **free** tier rather than in the live-CLI suite. A
regression in the translation was previously reachable only by launching a paid Codex
session, and the default suite of the task that could do it is `claude` -- so the run
that says "harness hook tests: passed" had never exercised Codex at all. A gate nobody
can afford to run is a gate that reports on nothing.

What makes a free gate possible at all is that Codex publishes the contract: its binary
embeds a JSON Schema per hook event, and `scripts/extract-codex-schema.py` commits them
as `codex-hook-schema.json`. So these checks compare real hooks against a real contract
offline, rather than against anybody's recollection of one -- which is what the first
version of this gate did, and it was wrong about `updatedInput`.

The checks, and the reason each is shaped the way it is:

- **The adapter's own responses survive its own translation.** `failure_output` is what
  an agent under Codex actually receives from a blocked hook, so a member Codex will not
  take would break every block silently. Pure, no I/O.
- **Every ported handler's response literals are checked.** Static, by `ast`: the
  handlers are real hooks -- `stop.py` runs the project's whole lint and test gate --
  so *running* them here would be a suite that costs more than the thing it guards, and
  a per-handler opt-out list is how such a gate stops covering anything. Reading their
  source costs nothing and catches the case that matters: a new member nobody checked.
- **A lost decision is refused rather than passed through**, end to end, against a
  fixture hook. The fixture's members are the real names, unlike
  `test_untested_symbols.py`'s invented ones, because here the *name* is the contract
  under test rather than an example of one.
"""

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import REPO_ROOT, load_module

adapter = load_module("scripts/hooks/codex-hook-adapter.py")
sync = load_module("scripts/sync-codex-hooks.py")

SETTINGS = REPO_ROOT / ".claude" / "settings.json"

# The generator writes handler commands with this prefix, and the source settings carry
# it too. `sync-codex-hooks.py` owns the spelling; this is the read half.
PROJECT_DIR_TOKEN = re.compile(r"\$\{CLAUDE_PROJECT_DIR:-\.\}/")


def _settings() -> dict:
    if not SETTINGS.is_file():
        pytest.skip(f"{SETTINGS.name} is absent: this project wires no hooks to port")
    return json.loads(SETTINGS.read_text(encoding="utf-8"))


def ported_handlers(settings: dict) -> list[tuple[str, str]]:
    """`(event, command)` for every handler `sync-codex-hooks.py` would give to Codex.

    Read back out of the generator's own output rather than re-deriving it, so a change
    to what gets ported cannot leave this gate testing the old set.
    """
    codex = sync.to_codex_hooks(settings)
    found = []
    for event, matchers in codex.get("hooks", {}).items():
        for entry in matchers:
            for handler in entry.get("hooks", []):
                found.append((event, str(handler.get("command", ""))))
    return found


def handler_sources(settings: dict) -> list[tuple[str, Path]]:
    """`(event, path)` for each ported handler that is a Python file in this repo.

    A `bash` handler and a handler outside the repo are both skipped, and neither is a
    silent gap worth hiding: a shell script cannot emit a structured response at all,
    and a path this repo does not own is not this repo's to gate.

    Resolution goes through the *source* settings rather than through the generated
    command, because the generator rewrites `${CLAUDE_PROJECT_DIR:-.}` into a Codex root
    expression that is not a path on this machine. Whether a handler was ported is then
    a basename lookup in the generated wiring -- one question asked of the generator
    rather than a second copy of its porting rules here.
    """
    ported = " ".join(command for _, command in ported_handlers(settings))
    found = []
    for event, entries in (settings.get("hooks") or {}).items():
        for entry in entries:
            for handler in entry.get("hooks", []):
                for token in re.findall(r'"([^"]+)"', str(handler.get("command", ""))):
                    relative = PROJECT_DIR_TOKEN.sub("", token)
                    candidate = (REPO_ROOT / relative).resolve()
                    if candidate.suffix != ".py" or not candidate.is_file():
                        continue
                    if candidate.name in ported:
                        found.append((event, candidate))
    return found


def response_literals(source: str) -> set[str]:
    """Every hook-response member a module builds as a dict literal, dotted.

    Two shapes, which is every shape the response contract has: a dict carrying a
    `hookSpecificOutput` key (its own keys are top-level members, and that value's keys
    are the nested ones), and a bare `hookSpecificOutput` body recognised by its
    `hookEventName` key -- the spelling a hook uses when it builds the inner dict
    separately.
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Dict):
            continue
        keys = [
            k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)
        ]
        if "hookSpecificOutput" in keys:
            names.update(keys)
            # `keys` drops non-constant keys, so the value is found by matching the key
            # node rather than by indexing into a filtered list.
            for key_node, value in zip(node.keys, node.values, strict=False):
                matched = (
                    isinstance(key_node, ast.Constant) and key_node.value == "hookSpecificOutput"
                )
                if matched and isinstance(value, ast.Dict):
                    names.update(
                        f"hookSpecificOutput.{k.value}"
                        for k in value.keys
                        if isinstance(k, ast.Constant) and isinstance(k.value, str)
                    )
        elif "hookEventName" in keys:
            names.update(f"hookSpecificOutput.{key}" for key in keys)
    return names


def test_the_contract_snapshot_is_present_and_covers_the_ported_events():
    """No snapshot, no gate: every check below degrades to `unknown-event` without it.

    `SessionEnd` is the documented absence rather than a hole. Codex publishes a
    `session-end.command.input` schema and no output one at all, which is the strongest
    possible statement that nothing a SessionEnd hook prints is read -- so the adapter
    must not be building a decision for it, which the next test asserts.
    """
    snapshot = json.loads(
        (REPO_ROOT / "scripts" / "hooks" / "codex-hook-schema.json").read_text(encoding="utf-8")
    )
    assert snapshot["codex_version"]
    covered = set(snapshot["events"])
    missing = set(sync.SUPPORTED_EVENTS) - covered
    assert missing == {"SessionEnd"}, (
        f"the generator ports {sorted(missing)} but Codex publishes no output schema for "
        "them. Either Codex changed and the snapshot needs refreshing "
        "(python scripts/extract-codex-schema.py --check), or the generator has begun "
        "porting an event whose response nothing reads."
    )


def test_every_response_the_adapter_itself_emits_survives_translation():
    """A member added to `failure_output` that Codex will not take breaks every block."""
    for event in sorted(set(sync.SUPPORTED_EVENTS) - {"SessionEnd"}):
        payload = json.dumps(adapter.failure_output(event, "reason"))
        lost, dropped = adapter.response_problems(payload, event)
        assert not lost, f"{event}: adapter emits a decision member Codex will not take: {lost}"
        assert not dropped, f"{event}: adapter emits a member Codex would reject: {dropped}"


def test_every_ported_handler_builds_only_members_codex_takes():
    """The regression gate: a hook grows a member, and nothing said what Codex does with it.

    Reported per event, because the accepted set is per event -- the same member can be
    fine on PreToolUse and a lost decision on Stop.
    """
    settings = _settings()
    sources = handler_sources(settings)
    if not sources:
        pytest.skip("this project ports no Python handler to Codex")
    problems: dict[str, dict[str, list[str]]] = {}
    for event, path in sources:
        found = response_literals(path.read_text(encoding="utf-8"))
        verdicts = {name: adapter.classify_member(name, event) for name in sorted(found)}
        bad = {
            kind: [n for n, v in verdicts.items() if v == kind]
            for kind in ("lost", "dropped")
            if any(v == kind for v in verdicts.values())
        }
        if bad:
            problems[f"{path.name} on {event}"] = bad
    assert not problems, (
        f"these hook responses do not match Codex's schema for their event: {problems}. "
        "`lost` means the decision itself is not carried and the adapter will refuse the "
        "call; `dropped` means Codex would reject the response for carrying it. Fix the "
        "hook, or record why the member is right and let the adapter strip it."
    )


def test_a_lost_decision_is_refused_rather_than_passed_through(tmp_path):
    """End to end: the response an agent gets says the hook could not be honoured.

    `decision` on PermissionRequest is the case, and it is a real one -- the member is
    Claude's spelling of a permission verdict, and Codex's PermissionRequest schema
    carries `hookSpecificOutput.decision` instead. Passed through, the agent would be
    allowed to do the thing the hook refused.
    """
    hook = tmp_path / "refusing_hook.py"
    hook.write_text(
        "import json\nprint(json.dumps({'decision': 'block', 'reason': 'routed into the box'}))\n",
        encoding="utf-8",
    )
    code, stdout, _ = adapter.run_hook(
        "PermissionRequest", [sys.executable, str(hook)], "{}", repo_root=tmp_path
    )
    assert code == 0
    decision = json.loads(stdout)["hookSpecificOutput"]["decision"]
    assert decision["behavior"] == "deny"
    # The hook's own words survive the refusal: without them the agent is told no and
    # not what to do instead, which is a loud wrong answer in place of a quiet one.
    assert "routed into the box" in decision["message"]


def test_a_member_codex_would_reject_is_stripped_and_recorded(tmp_path, monkeypatch):
    """Stripped, not passed through: the schemas are `additionalProperties: false`, so
    one unknown member costs the accepted ones too."""
    hook = tmp_path / "novel_hook.py"
    hook.write_text(
        "import json\n"
        "print(json.dumps({'decision': 'block', 'reason': 'no', "
        "'hookSpecificOutput': {'hookEventName': 'Stop', 'somethingBrandNew': 1}}))\n",
        encoding="utf-8",
    )
    seen: list[tuple] = []
    monkeypatch.setattr(adapter, "_record_gap", lambda *a: seen.append(a))
    _code, stdout, _ = adapter.run_hook(
        "Stop", [sys.executable, str(hook)], "{}", repo_root=tmp_path
    )
    kept = json.loads(stdout)
    assert kept == {"decision": "block", "reason": "no"}
    assert seen and "hookSpecificOutput.somethingBrandNew" in seen[0][1]


def test_the_adapter_marks_the_runtime_so_a_hook_can_tell(tmp_path):
    """`DEVKIT_HOOK_ADAPTER` is the seam the guard and the events ledger both read."""
    hook = tmp_path / "echo_env.py"
    hook.write_text(
        "import os\nprint(os.environ.get('DEVKIT_HOOK_ADAPTER', 'unset'))\n", encoding="utf-8"
    )
    _code, stdout, _ = adapter.run_hook(
        "PostToolUse", [sys.executable, str(hook)], "{}", repo_root=tmp_path
    )
    assert stdout.strip() == "codex"


def test_the_generator_and_the_adapter_agree_on_which_events_exist():
    """An event the generator ports but the adapter has no response for is a silent gap.

    `failure_output`'s fallthrough is `systemMessage`, which is not a decision -- a
    blocked hook on such an event would report and then let the call run.
    """
    deciding = (
        {"PreToolUse", "PermissionRequest"}
        | adapter.CONTINUATION_EVENTS
        | adapter.COMMON_STOP_EVENTS
    )
    ported = set(sync.SUPPORTED_EVENTS)
    assert deciding <= ported | sync.UNSUPPORTED_EVENTS, (
        "the adapter builds a decision for an event no longer in the generator's "
        f"vocabulary: {sorted(deciding - (ported | sync.UNSUPPORTED_EVENTS))}"
    )


def test_the_ledger_records_which_runtime_a_hook_ran_under():
    """Without it, a Codex-only defect and a Claude one are one row shape."""
    events = load_module("scripts/hooks/harness_events.py")
    assert events.agent_name({}) == events.NATIVE_AGENT
    assert events.agent_name({events.ADAPTER_ENV: "codex"}) == "codex"
    assert events.ADAPTER_ENV == adapter.ADAPTER_ENV


def test_the_adapter_is_a_real_program_not_only_an_import():
    """It is spawned by path from generated wiring, so a syntax error is a dead session."""
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "hooks" / "codex-hook-adapter.py"), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
