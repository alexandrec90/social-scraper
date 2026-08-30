"""Unit tests for the enforce-capped-bash PreToolUse hook decision logic.

Vendored tier: `decide` takes an injectable `max_bytes` precisely so these tests do
not depend on the `[bash]` block of whichever repo they run in.

The largest block here is `HISTORICAL_FALSE_POSITIVES`, and it is the point of the
rewrite rather than an appendix to it. Every entry is a real command from this
workspace's transcripts that the previous allow-list gate blocked, recovered by replaying
the sessions. Under that design each one needed its own regex, its own commit and its own
wasted turn to fix; under a blocklist they are all allowed for the same reason -- none of
them is a tree-scaling command -- and the suite's job is to keep it that way.

The mirror of it is `TREE_SCALING`, which pins the nine commands the gate does still
block, and `test_a_cap_admits_every_noisy_command`, which pins that being blocked is
never a dead end.
"""

import json

import pytest
from conftest import load_module

hook = load_module("scripts/hooks/enforce-capped-bash.py")


def payload(tool_name, command=None):
    body = {"tool_name": tool_name, "tool_input": {}}
    if command is not None:
        body["tool_input"]["command"] = command
    return json.dumps(body)


def allows(command):
    return hook.decide(payload("Bash", command), max_bytes=4000)[0] == 0


def blocks(command):
    return hook.decide(payload("Bash", command), max_bytes=4000)[0] == hook.EXIT_BLOCK


# --- decide: payload handling ---


def test_empty_stdin_allows():
    assert hook.decide("") == (0, "")
    assert hook.decide("   \n") == (0, "")


def test_malformed_json_allows_with_note():
    code, msg = hook.decide("{not json")
    assert code == 0
    assert "unable to parse" in msg


def test_non_bash_tool_allows_silently():
    assert hook.decide(payload("Read", "ls -R /")) == (0, "")


def test_a_missing_command_allows():
    """The payload shape is the harness's defect, and a block naming nothing is unactionable.

    The allow-list blocked here because it could not prove boundedness of a command it
    could not see. A blocklist has nothing to name, so it has nothing to say.
    """
    assert hook.decide(payload("Bash")) == (0, "")


@pytest.mark.parametrize(
    "key",
    ["tool_input.command", "toolInput.command", "input.command", "command"],
)
def test_every_payload_spelling_is_read(key):
    body = {"tool_name": "Bash"}
    cur = body
    parts = key.split(".")
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = "ls -R /"
    assert hook.decide(json.dumps(body), max_bytes=4000)[0] == hook.EXIT_BLOCK


# --- the closed blocklist ---

TREE_SCALING = [
    "ls",
    "ls -la",
    "ls -R /",
    'ls "C:/Users/Administrator/Desktop/vs_code/"',
    "cat README.md",
    "cat .gitignore",
    "find . -name '*.py'",
    "tree -L 3",
    "du -sh *",
    "env",
    "printenv",
    "git status",
    "git status --short",
    "git status --porcelain",
    "git -C /some/box status",
    "git log",
    "git log --oneline",
    "git log --oneline -5 -p",
    "git diff",
    "git diff HEAD",
    "git show abc123",
    "git show abc123:path/to/file.py",
]


@pytest.mark.parametrize("command", TREE_SCALING)
def test_a_tree_scaling_command_blocks(command):
    assert blocks(command)


@pytest.mark.parametrize("command", TREE_SCALING)
def test_a_cap_admits_every_noisy_command(command):
    """Being on the blocklist is never a dead end: three spellings take any of them off it.

    This is the property the old gate could not offer, because it blocked shapes -- a
    heredoc, a loop, a brace group -- that no cap and no wrapper could rescue.
    """
    assert allows(f"{command} | head -c 4000")
    assert allows(f"{command} > /tmp/out.txt")
    assert allows(f'python3 scripts/hooks/invoke-capped.py --command "{command}"')


@pytest.mark.parametrize(
    "command",
    [
        "git log --oneline -5",
        "git log -n 20 --format=%H",
        "git log --max-count=3",
        "git -C /some/box log --oneline -3",
        "git diff --stat",
        "git diff --name-only",
        "git diff --cached --name-status",
        "git diff --quiet",
        "git show --stat HEAD",
    ],
)
def test_the_bounded_spellings_of_a_noisy_git_command_allow(command):
    """A count bounds a log and a summary flag bounds a diff, which is the whole exemption.

    `--name-only` heads most scripted uses of `git diff` in this workspace; blocking it
    would rebuild the allow-list's false-positive tier one flag at a time.
    """
    assert allows(command)


def test_a_counted_log_asked_for_patches_still_blocks():
    """`-p` revokes what the count earned -- one commit's diff has no bound of its own."""
    assert blocks("git log --oneline -5 -p")
    assert blocks("git log -3 --patch")


def test_the_block_message_names_the_command_and_the_way_out():
    _, msg = hook.decide(payload("Bash", "ls -R /"), max_bytes=4000)
    assert "`ls`" in msg
    assert "Glob" in msg
    assert "invoke-capped.py" in msg
    assert "4000" in msg
    assert "do not wrap commands it did not name" in msg


def test_the_message_quotes_the_configured_cap_not_a_literal():
    _, msg = hook.decide(payload("Bash", "ls"), max_bytes=9999)
    assert "9999" in msg


def test_each_offending_statement_is_named_once():
    _, msg = hook.decide(payload("Bash", "ls -la; ls -R /; cat big"), max_bytes=4000)
    assert msg.count("`ls` grows") == 1
    assert "`cat`" in msg


# --- what the gate no longer claims ---

HISTORICAL_FALSE_POSITIVES = [
    # Shapes the wrapper cannot rescue: a heredoc, a loop, a group, a case arm.
    "git add -A && git commit -q -F - <<'MSG'\nSubject line\n\nBody with `backticks`.\nMSG",
    "python - <<'PY'\nimport json\nprint(json.dumps({'a': 1}))\nPY",
    "until [ \"$(docker inspect --format '{{.State.Health.Status}}' db-1)\" = healthy ]; "
    "do sleep 2; done; echo ready",
    "for i in $(seq 1 60); do if gh api graphql -f query='{viewer{login}}' >/dev/null 2>&1; "
    "then echo up; exit 0; fi; sleep 3; done",
    "while read -r line; do echo $line; done < paths.txt",
    "case $(uname) in Linux) pwd ;; esac",
    "{ pwd; git rev-parse HEAD; } | head -c 400",
    'R=/a; L=/b; { grep x "$R"; tail -c 400 "$L"; } | head -c 4000',
    # Prose that read as a flag or a substitution.
    'git commit -m "Add a --verbose flag"',
    "git commit -m 'fix `foo` handling'",
    'gh pr close 67 --comment "Superseded by #68, which deletes \\`adds_nothing\\`."',
    'gh pr create --title "Stop the gate blocking a numeric `sed -n` range" --body-file b.md',
    "gh pr edit 12 --body-file body.md",
    # Bounded reads the allow-list had not learned yet.
    "sed -n '1,30p' README.md",
    "sed -n 1,30p README.md",
    "git -C /some/box rev-parse HEAD",
    "git -C /some/box config --local core.hooksPath",
    "git branch --show-current",
    "git fetch origin main --quiet",
    "git fetch origin && git merge --ff-only origin/main",
    "DEVKIT_SKIP_BRANCH_POLICY=1 git commit -F msg.txt",
    "DIR=/some/path",
    "set -euo pipefail",
    "python -m pytest tests/ -q 2>&1 | tail -c 2500",
    "gh run view --job 918478 --log-failed 2>&1 | tail -c 4000",
    # Not tree-scaling, merely unrecognised -- the class the rewrite exists to stop.
    'python -c "import sys; print(sys.version)"',
    "grep -rn 'pattern' --include=*.py .",
    "rg -c TODO src/",
    "awk '{print $1}' data.txt",
    "curl -s http://localhost:8000/health",
    "wc -l frontend/src/*.ts",
    "docker compose ps",
    "npm run build",
    "uv run pytest -q",
    "python scripts/worktree.py list",
]


@pytest.mark.parametrize("command", HISTORICAL_FALSE_POSITIVES)
def test_a_historically_blocked_command_is_allowed(command):
    assert allows(command), f"regression: {command!r} would block again"


def test_an_unknown_command_fails_open():
    """The design decision, stated as a test.

    An allow-list must block what it cannot parse, which is how it reaches a 46% false
    positive rate. A blocklist allows it, and `BASH_MAX_OUTPUT_LENGTH` bounds the result.
    """
    assert allows("some-tool --that-nobody-has-heard-of --print-everything")


# --- parsing that prevents a block rather than causing one ---


def test_a_noisy_word_inside_a_heredoc_body_is_data():
    """A commit message mentioning `cat` is prose, not a statement."""
    assert allows("git commit -F - <<'EOF'\nRewrite how we cat the log\nls the tree\nEOF")


def test_a_noisy_word_inside_a_comment_is_not_a_command():
    assert allows("pwd # then ls -R / to see the tree")


def test_a_noisy_word_inside_a_quoted_argument_is_not_a_command():
    assert allows('python3 scripts/hooks/invoke-capped.py --command "cd x; ls -R"')
    assert allows("grep -n 'git status' scripts/hooks/enforce-capped-bash.py")


def test_a_here_string_does_not_swallow_the_statements_after_it():
    assert blocks("cat <<< 'seed'; ls -R /")


def test_a_group_is_held_together_so_its_cap_binds_every_member():
    assert allows("{ pwd; cat big.log; } | head -c 400")
    assert allows("(pwd; git status) > /tmp/out.txt")


def test_an_uncapped_group_is_still_judged_on_its_members():
    assert blocks("{ pwd; cat big.log; }")


def test_a_control_keyword_does_not_launder_the_command_behind_it():
    assert blocks("for f in *.py; do cat $f; done")
    assert blocks("if true; then ls -R /; fi")
    assert blocks("FOO=bar ls -R /")


def test_only_the_producing_end_of_a_pipeline_is_judged():
    """A downstream stage bounds bytes; it cannot create them."""
    assert blocks("ls -R / | sort")
    assert allows("git rev-parse HEAD | cat")


@pytest.mark.parametrize(
    "cap",
    [
        "| head -c 400",
        "| tail -c 400",
        "| head -20",
        "| tail -n 20",
        "| head",
        "| tail",
        "| wc -l",
        "| grep -c warn",
        "| grep -rc warn",
        "| grep --count warn",
        "> out.txt",
    ],
)
def test_every_cap_spelling_counts(cap):
    assert allows(f"ls -R / {cap}")


def test_an_uncounted_head_is_still_a_cap():
    """Ten lines is `head`'s default, so the count was never what made it a bound.

    Requiring one blocked `git status --porcelain | head`, which is the shape a session
    reaches for first -- a report of this exact false positive is what removed it.
    """
    assert allows("git status --porcelain | head")
    assert allows("cat big.log | tail")
    assert allows("ls -R / | head foo.txt")


@pytest.mark.parametrize(
    "segment",
    [
        "| tail -f",
        "| tail --follow",
        "| tail -n +5",
        "| head -n -5",
    ],
)
def test_an_unbounded_head_or_tail_is_not_a_cap(segment):
    """The flags that make `head`/`tail` unbounded: following, and counting from the
    other end. An optional count must not admit these by matching the bare name."""
    assert blocks(f"cat big.log {segment}")


def test_a_counting_grep_bounds_a_blocked_head():
    """One number per input is a bound, and this spelling was blocking real work.

    `git show <ref>:<file> | grep -c <pattern>` -- reading whether a symbol survives in
    a tagged tree -- was refused for want of a `wc`, and the block message's remedies
    (`--stat`, `--name-only`) answer a different question than the one being asked.
    """
    assert allows('git show v1.2.3:scripts/hooks/hook.py | grep -c "def main"')
    assert allows("cat big.log | egrep -c warn")


def test_grep_context_is_not_a_count():
    """`-C 3` prints three lines around every match, so the case distinction is load-bearing."""
    assert blocks("git show HEAD | grep -C 3 warn")
    assert blocks("cat big.log | grep warn")


def test_a_descriptor_duplication_is_not_a_redirect():
    """`2>&1` moves stderr and bounds nothing; the old gate got this right and it stays."""
    assert blocks("ls -R / 2>&1")


# --- PowerShell (the Codex override) ---


def ps_allows(command):
    return hook.decide(payload("Bash", command), max_bytes=4000, command_shell="powershell")[0] == 0


def ps_blocks(command):
    code, _ = hook.decide(payload("Bash", command), max_bytes=4000, command_shell="powershell")
    return code == hook.EXIT_BLOCK


def test_powershell_tree_scaling_commands_block():
    assert ps_blocks("Get-ChildItem -Recurse")
    assert ps_blocks("Get-Content big.log")


def test_powershell_native_caps_allow():
    assert ps_allows("Get-ChildItem | Select-Object -First 20")
    assert ps_allows("Get-Content big.log -TotalCount 40")
    assert ps_allows("Get-Content big.log -Tail 40")
    assert ps_allows("Get-ChildItem -Recurse | Measure-Object")
    assert ps_allows("Get-ChildItem > out.txt")


def test_powershell_leaves_everything_else_alone():
    assert ps_allows("Test-Path C:/x")
    assert ps_allows("$env:PATH -split ';'")
    assert ps_allows("python -m pytest -q")


def test_the_block_message_carries_the_harness_provenance_when_given_one():
    """The stamp is injected, not read: the footer names a SHA, and an assertion that
    had to predict it would pin this repo's current commit into the vendored suite."""
    _, msg = hook.decide(
        payload("Bash", "ls -la"), max_bytes=4000, stamp="(devkit harness deadbeef)"
    )
    assert msg.endswith("(devkit harness deadbeef)")
    # The stamp is a footer, not a replacement -- the actionable half still leads.
    assert "`ls`" in msg


def test_the_block_message_says_how_to_report_the_block_as_wrong():
    """This gate's own rule says a block on something outside the nine is a defect in
    it -- and until now the block never said where that goes.

    It matters most for the runtime that cannot read the rule. Codex reads past
    `.claude/rules/`, so a Codex session had no path to the reporter at all: on
    2026-08-28 the ledger held 1176 rows and not one agent-report from Codex, against
    seven of its guard-blocks the same day. A gate that offers no channel gets routed
    around, which is the failure the guardrail exists to prevent.
    """
    _, msg = hook.decide(payload("Bash", "ls -la"), max_bytes=4000)
    assert "report-harness-defect.py" in msg


def test_no_stamp_leaves_the_message_exactly_as_it_was():
    """A project whose harness cannot be identified gets the message unchanged, with
    no trailing blank line hinting that something failed to render."""
    _, msg = hook.decide(payload("Bash", "ls -la"), max_bytes=4000)
    assert not msg.endswith("\n")


def test_an_allowed_command_never_carries_a_stamp():
    """The footer costs bytes in an agent's context; it is only earned when the hook
    is the thing standing in the way."""
    code, msg = hook.decide(
        payload("Bash", "ls | wc -l"), max_bytes=4000, stamp="(devkit harness deadbeef)"
    )
    assert code == 0 and "deadbeef" not in msg


def test_the_powershell_message_names_native_caps_only():
    _, msg = hook.decide(
        payload("Bash", "Get-ChildItem -Recurse"), max_bytes=4000, command_shell="powershell"
    )
    assert "Select-Object -First N" in msg
    assert "head -c" not in msg
    assert "--shell powershell" in msg


# --- record_block: the harness-events ledger line behind every block ---


def ledger_lines(base):
    path = base / "logs" / "harness-events.log"
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def test_record_block_writes_session_and_command(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVKIT_DIR", str(tmp_path))
    body = {"tool_name": "Bash", "session_id": "s1", "tool_input": {"command": "ls -la"}}
    hook.record_block(json.dumps(body))
    (line,) = ledger_lines(tmp_path)
    assert "\tevent=capped-bash-block\t" in line
    assert "\tsession=s1\t" in line
    assert line.endswith("\tcommand=ls -la")
    assert "\tproject=" in line
    assert "\tversion=" in line


def test_record_block_on_malformed_payload_is_a_silent_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVKIT_DIR", str(tmp_path))
    hook.record_block("not json")
    assert ledger_lines(tmp_path) == []


def test_record_block_without_the_ledger_module_is_a_noop(tmp_path, monkeypatch):
    """A partially vendored consumer imports the gate without harness_events; a block
    must still block, with no ledger and no crash."""
    monkeypatch.setenv("DEVKIT_DIR", str(tmp_path))
    monkeypatch.setattr(hook, "harness_events", None)
    hook.record_block(payload("Bash", "ls -la"))
    assert ledger_lines(tmp_path) == []


def test_main_records_only_when_it_blocks(tmp_path, monkeypatch, capsys):
    import io

    monkeypatch.setenv("DEVKIT_DIR", str(tmp_path))
    monkeypatch.setattr("sys.stdin", io.StringIO(payload("Bash", "ls -la")))
    assert hook.main([]) == hook.EXIT_BLOCK
    assert len(ledger_lines(tmp_path)) == 1

    monkeypatch.setattr("sys.stdin", io.StringIO(payload("Bash", "ls | wc -l")))
    assert hook.main([]) == 0
    assert len(ledger_lines(tmp_path)) == 1
    capsys.readouterr()
