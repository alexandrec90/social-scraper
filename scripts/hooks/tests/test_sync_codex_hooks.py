"""Unit tests for the .codex/hooks.json generator (from .claude/settings.json)."""

import json
import shutil
import subprocess
import textwrap

import pytest
from conftest import load_module

hook = load_module("scripts/sync-codex-hooks.py")
sync_devkit = load_module("scripts/sync-devkit.py")


def test_codex_runtime_and_tests_are_vendored():
    """Generated commands must not depend on files held by only one consumer."""
    required = {
        "scripts/hooks/codex-hook-adapter.py",
        "scripts/hooks/codex-session-start.py",
        "scripts/hooks/tests/test_codex_hook_adapter.py",
        "scripts/hooks/tests/test_codex_session_start.py",
    }

    assert required <= set(sync_devkit.MANIFEST)
    for relative in required:
        assert (sync_devkit.REPO_ROOT / relative).is_file(), relative


class TestRewriteCommand:
    def test_resolves_project_dir_from_git_root(self):
        assert (
            hook.rewrite_command('python3 "${CLAUDE_PROJECT_DIR:-.}/scripts/hooks/example-hook.py"')
            == 'python3 "__CODEX_PROJECT_ROOT__/scripts/hooks/example-hook.py"'
        )

    def test_keeps_claude_subpath_after_prefix(self):
        # The .claude/... portion is a real path Codex reads directly.
        assert (
            hook.rewrite_command('python3 "${CLAUDE_PROJECT_DIR:-.}/.claude/skills/example/x.py"')
            == 'python3 "__CODEX_PROJECT_ROOT__/.claude/skills/example/x.py"'
        )

    def test_command_without_prefix_is_unchanged(self):
        # Inline echo retros carry no project-dir path.
        cmd = "echo '{\"hookSpecificOutput\": {}}'"
        assert hook.rewrite_command(cmd) == cmd

    def test_rewrites_every_occurrence(self):
        cmd = "${CLAUDE_PROJECT_DIR:-.}/a && ${CLAUDE_PROJECT_DIR:-.}/b"
        root = hook.CODEX_ROOT_EXPR
        assert hook.rewrite_command(cmd) == f"{root}/a && {root}/b"


class TestWrapCommand:
    def test_wraps_python_handler_with_event_adapter(self):
        result = hook.wrap_command(
            "PreToolUse", 'python3 "${CLAUDE_PROJECT_DIR:-.}/scripts/hooks/example-hook.py"'
        )
        assert "codex-hook-adapter.py" in result
        assert "--event PreToolUse -- python3" in result
        assert "__CODEX_PROJECT_ROOT__/scripts/hooks/example-hook.py" in result

    def test_session_start_bash_handler_uses_cross_platform_bridge(self):
        result = hook.wrap_command(
            "SessionStart", 'bash "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/session-start.sh"'
        )
        assert "--event SessionStart -- python3" in result
        assert "codex-session-start.py" in result
        assert ".claude/hooks/session-start.sh" not in result

    def test_leaves_shell_builtin_unwrapped(self):
        command = 'echo \'{"systemMessage":"x"}\''
        assert hook.wrap_command("PostToolUse", command) == command

    def test_windows_command_uses_the_same_cross_platform_launcher(self):
        result = hook.wrap_command(
            "PreToolUse",
            'python3 "${CLAUDE_PROJECT_DIR:-.}/scripts/hooks/example-hook.py"',
            windows=True,
        )
        assert result.startswith(hook.CODEX_LAUNCHER)
        assert 'python3 "__CODEX_PROJECT_ROOT__/scripts/hooks/example-hook.py"' in result
        assert "$(git rev-parse" not in result

    def test_windows_absolute_root_needs_no_shell_lookup(self):
        result = hook.wrap_command(
            "PreToolUse", 'python3 "x/guard.py"', root="C:/ws/devkit", windows=True
        )
        assert result.startswith('python3 "C:/ws/devkit/scripts/hooks/codex-hook-adapter.py"')
        assert "for /f" not in result

    def test_launcher_executes_from_a_nested_working_directory(self, tmp_path):
        subprocess.run(
            ["git", "init", "--quiet"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        )
        hooks_dir = tmp_path / "scripts" / "hooks"
        hooks_dir.mkdir(parents=True)
        shutil.copyfile(
            sync_devkit.REPO_ROOT / "scripts/hooks/codex-hook-adapter.py",
            hooks_dir / "codex-hook-adapter.py",
        )
        (tmp_path / "recorder.py").write_text(
            textwrap.dedent(
                """\
                import json
                import sys
                from pathlib import Path

                payload = json.load(sys.stdin)
                (Path(__file__).parent / "seen-event.txt").write_text(
                    payload["hook_event_name"], encoding="utf-8"
                )
                print("{}")
                """
            ),
            encoding="utf-8",
            newline="",
        )
        nested = tmp_path / "src" / "feature"
        nested.mkdir(parents=True)
        command = hook.wrap_command(
            "UserPromptSubmit", 'python3 "${CLAUDE_PROJECT_DIR:-.}/recorder.py"'
        )

        result = subprocess.run(  # noqa: S602 - generated shell command under test
            command,
            cwd=nested,
            input='{"hook_event_name":"UserPromptSubmit"}',
            capture_output=True,
            text=True,
            shell=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        assert (tmp_path / "seen-event.txt").read_text(encoding="utf-8") == "UserPromptSubmit"


class TestToCodexHooks:
    def test_wraps_in_hooks_key(self):
        assert hook.to_codex_hooks({"hooks": {}}) == {"hooks": {}}

    def test_missing_hooks_yields_empty(self):
        assert hook.to_codex_hooks({"env": {"A": "1"}}) == {"hooks": {}}

    def test_carries_supported_events_and_wraps_commands(self):
        claude = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {"type": "command", "command": 'bash "${CLAUDE_PROJECT_DIR:-.}/x.sh"'}
                        ]
                    }
                ],
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {
                                "type": "command",
                                "command": 'python3 "${CLAUDE_PROJECT_DIR:-.}/p.py"',
                            }
                        ],
                    }
                ],
            }
        }
        result = hook.to_codex_hooks(claude)
        assert set(result["hooks"]) == {"SessionStart", "PreToolUse"}
        session = result["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        assert session.startswith(hook.CODEX_ADAPTER)
        assert session.endswith('--event SessionStart -- bash "__CODEX_PROJECT_ROOT__/x.sh"')
        # The matcher passes through untouched.
        assert result["hooks"]["PreToolUse"][0]["matcher"] == "Bash"
        pretool = result["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        assert pretool.startswith(hook.CODEX_ADAPTER)
        assert pretool.endswith('--event PreToolUse -- python3 "__CODEX_PROJECT_ROOT__/p.py"')
        windows = result["hooks"]["PreToolUse"][0]["hooks"][0]["commandWindows"]
        assert windows == pretool

    def test_preserves_and_wraps_an_explicit_windows_command(self):
        claude = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {
                                "type": "command",
                                "command": 'python3 "${CLAUDE_PROJECT_DIR:-.}/posix.py"',
                                "commandWindows": 'python "${CLAUDE_PROJECT_DIR:-.}/windows.py"',
                            }
                        ],
                    }
                ]
            }
        }
        entry = hook.to_codex_hooks(claude)["hooks"]["PreToolUse"][0]["hooks"][0]
        assert entry["command"].endswith('/posix.py"')
        assert entry["commandWindows"].endswith('-- python "__CODEX_PROJECT_ROOT__/windows.py"')

    def test_drops_the_redundant_bash_cap_policy(self):
        claude = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
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
        }

        result = hook.to_codex_hooks(claude)

        assert result == {"hooks": {}}

    def test_drops_only_the_bash_cap_handler_from_a_shared_group(self):
        claude = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {
                                "type": "command",
                                "command": (
                                    'python3 "${CLAUDE_PROJECT_DIR:-.}/scripts/hooks/'
                                    'enforce-capped-bash.py"'
                                ),
                            },
                            {"type": "command", "command": "python3 keep.py"},
                        ],
                    }
                ]
            }
        }

        result = hook.to_codex_hooks(claude)
        handlers = result["hooks"]["PreToolUse"][0]["hooks"]

        assert len(handlers) == 1
        assert handlers[0]["command"].endswith("-- python3 keep.py")

    def test_raises_too_short_codex_session_start_timeout(self):
        claude = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "python status.py",
                                "timeout": 20,
                            },
                            {
                                "type": "command",
                                "command": "python slow-status.py",
                                "timeout": 90,
                            },
                        ]
                    }
                ]
            }
        }

        entries = hook.to_codex_hooks(claude)["hooks"]["SessionStart"][0]["hooks"]

        assert entries[0]["timeout"] == hook.MIN_CODEX_SESSION_START_TIMEOUT
        assert entries[1]["timeout"] == 90

    def test_preserves_hook_entry_fields(self):
        claude = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "run"}]}]}}
        entry = hook.to_codex_hooks(claude)["hooks"]["Stop"][0]["hooks"][0]
        assert entry == {"type": "command", "command": "run"}

    def test_drops_unsupported_event(self):
        claude = {
            "hooks": {
                "PostToolUseFailure": [
                    {"matcher": ".*", "hooks": [{"type": "command", "command": "echo nope"}]}
                ]
            }
        }
        assert hook.to_codex_hooks(claude) == {"hooks": {}}

    def test_rejects_unclassified_event(self):
        claude = {"hooks": {"FutureHookEvent": []}}

        with pytest.raises(ValueError, match=r"Unclassified Claude hook event.*FutureHookEvent"):
            hook.to_codex_hooks(claude)

    def test_drops_skill_tool_matcher_but_keeps_edit_matcher(self):
        claude = {
            "hooks": {
                "PostToolUse": [
                    {"matcher": "^Skill$", "hooks": [{"type": "command", "command": "echo nope"}]},
                    {
                        "matcher": "^apply_patch$",
                        "hooks": [{"type": "command", "command": "python3 lint.py"}],
                    },
                ]
            }
        }
        groups = hook.to_codex_hooks(claude)["hooks"]["PostToolUse"]
        assert [group["matcher"] for group in groups] == ["^apply_patch$"]

    def test_does_not_mutate_input(self):
        claude = {
            "hooks": {
                "Stop": [
                    {"hooks": [{"command": "${CLAUDE_PROJECT_DIR:-.}/s.py", "type": "command"}]}
                ]
            }
        }
        hook.to_codex_hooks(claude)
        assert claude["hooks"]["Stop"][0]["hooks"][0]["command"] == "${CLAUDE_PROJECT_DIR:-.}/s.py"


def test_sync_writes_hooks_only(tmp_path):
    src = tmp_path / "claude.json"
    dest = tmp_path / "hooks.json"
    src.write_text(
        json.dumps(
            {
                "env": {"X": "1"},
                "permissions": {"allow": ["Read(*)"]},
                "hooks": {
                    "PostToolUse": [
                        {
                            "matcher": "^Edit$",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": 'python3 "${CLAUDE_PROJECT_DIR:-.}/lint.py"',
                                }
                            ],
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    assert hook.sync(src, dest) == 0
    written = json.loads(dest.read_text(encoding="utf-8"))
    # Only the hooks block is carried over -- no env/permissions leak into Codex.
    assert set(written) == {"hooks"}
    command = written["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
    assert "--event PostToolUse -- python3" in command
    assert command.endswith('python3 "__CODEX_PROJECT_ROOT__/lint.py"')
    # Must emit LF (repo enforces eol=lf), never CRLF, regardless of OS.
    assert b"\r\n" not in dest.read_bytes()


def test_sync_missing_src_is_noop(tmp_path):
    src = tmp_path / "missing.json"
    dest = tmp_path / "hooks.json"
    assert hook.sync(src, dest) == 0
    assert not dest.exists()


# --- the generated artifact is committed, so it can go stale ------------------
# The generator is vendored and its output is not. `REDUNDANT_HANDLERS` dropped the
# Claude-only Bash cap the day it landed, and Codex sessions went on being blocked by
# it for as long as the already-generated `.codex/hooks.json` survived -- because
# `--pull` copies the generator and, until `is_stale`, nothing read what it produced.
# The block's own remedy is `invoke-capped.py`, so each block bought a session a
# wrapper it then reused on every command after it. These are that regression.


def _settings_with_cap(path):
    """A settings file wiring the Claude-only Bash cap, as every consumer's does."""
    path.write_text(
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
                                        f'python3 "${{CLAUDE_PROJECT_DIR:-.}}/'
                                        f'{hook.CLAUDE_BASH_CAP}"'
                                    ),
                                }
                            ],
                        },
                        {
                            "matcher": "^Edit$",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": 'python3 "${CLAUDE_PROJECT_DIR:-.}/guard.py"',
                                }
                            ],
                        },
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    return path


class TestIsStale:
    def test_a_file_written_by_a_previous_generator_is_stale(self, tmp_path):
        """The exact shape of the bug: the artifact still carries the Bash cap that the
        generator no longer emits, so Codex keeps being blocked by a hook this repo has
        already stopped describing."""
        src = _settings_with_cap(tmp_path / "settings.json")
        dest = tmp_path / "hooks.json"
        # What the pre-`REDUNDANT_HANDLERS` generator produced: the cap ported through.
        dest.write_text(
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
                                            f"adapter --event PreToolUse -- "
                                            f"python3 {hook.CLAUDE_BASH_CAP}"
                                        ),
                                    }
                                ],
                            }
                        ]
                    }
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        assert hook.is_stale(src, dest) is True

    def test_a_freshly_generated_file_is_not_stale(self, tmp_path):
        src = _settings_with_cap(tmp_path / "settings.json")
        dest = tmp_path / "hooks.json"
        hook.sync(src, dest)
        assert hook.is_stale(src, dest) is False

    def test_a_crlf_checkout_is_not_reported_as_stale(self, tmp_path):
        """A consumer whose git checked the artifact out with CRLF would otherwise fail
        its own PR gate forever, on a difference regenerating does not remove."""
        src = _settings_with_cap(tmp_path / "settings.json")
        dest = tmp_path / "hooks.json"
        dest.write_text(hook.render(src).replace("\n", "\r\n"), encoding="utf-8", newline="")
        assert hook.is_stale(src, dest) is False

    def test_a_project_without_codex_has_nothing_to_be_stale(self, tmp_path):
        src = _settings_with_cap(tmp_path / "settings.json")
        assert hook.is_stale(src, tmp_path / "absent.json") is False

    def test_missing_settings_is_not_stale(self, tmp_path):
        dest = tmp_path / "hooks.json"
        dest.write_text("{}\n", encoding="utf-8")
        assert hook.is_stale(tmp_path / "absent.json", dest) is False


class TestCheckMode:
    def test_check_exits_one_on_a_stale_artifact_and_writes_nothing(self, tmp_path, capsys):
        src = _settings_with_cap(tmp_path / "settings.json")
        dest = tmp_path / "hooks.json"
        dest.write_text('{"hooks": {}}\n', encoding="utf-8")

        assert hook.main(["--check", str(src), str(dest)]) == 1
        # Reporting must not repair: a gate that fixes what it measures is a gate that
        # passes on the second run having told nobody.
        assert dest.read_text(encoding="utf-8") == '{"hooks": {}}\n'
        assert "sync-codex-context.py" in capsys.readouterr().err

    def test_check_exits_zero_when_the_artifact_is_current(self, tmp_path):
        src = _settings_with_cap(tmp_path / "settings.json")
        dest = tmp_path / "hooks.json"
        hook.sync(src, dest)
        assert hook.main(["--check", str(src), str(dest)]) == 0

    def test_flags_do_not_consume_the_positional_pair(self, tmp_path):
        """`--check src dest` must still address `src`/`dest`; reading argv positionally
        would make the flag silently retarget the check at this repo's own pair."""
        src = _settings_with_cap(tmp_path / "settings.json")
        dest = tmp_path / "hooks.json"
        assert hook.main([str(src), str(dest)]) == 0
        assert hook.is_stale(src, dest) is False


def test_the_bash_cap_never_reaches_codex_from_this_repos_own_settings():
    """The end-to-end guarantee, against the real settings file rather than a fixture.

    `REDUNDANT_HANDLERS` matches on a path substring, so renaming the cap script, moving
    it, or wiring it under a second matcher would each restore the port silently. This
    fails in whichever project that happens in, which is the only place it can be seen.
    """
    settings = sync_devkit.REPO_ROOT / ".claude/settings.json"
    if not settings.is_file():
        pytest.skip("project has no .claude/settings.json to port")
    emitted = json.dumps(hook.to_codex_hooks(json.loads(settings.read_text(encoding="utf-8"))))
    assert hook.CLAUDE_BASH_CAP not in emitted, (
        "Codex would run the Claude-only Bash cap. Its shell runner already caps "
        "captured output, so the gate only blocks -- and the block's own remedy is to "
        "wrap every later command in invoke-capped.py."
    )


def test_the_committed_codex_artifact_matches_the_generator():
    """This project's own `.codex/hooks.json`, if it has one, is current."""
    settings = sync_devkit.REPO_ROOT / ".claude/settings.json"
    artifact = sync_devkit.REPO_ROOT / ".codex/hooks.json"
    if not artifact.is_file():
        pytest.skip("project has not opted into .codex/")
    assert not hook.is_stale(settings, artifact), (
        f"{artifact} is not what {settings} generates today -- Codex is running hook "
        f"wiring this repo no longer describes. Run `python scripts/sync-codex-context.py`."
    )


# --- the workstation-level pair ---------------------------------------------
# ~/.claude/settings.json wires the hooks that are NOT vendored into any repo -- the
# home-branch edit guard and the task-slug recorder. Codex had neither, so its edits
# landed on home branches with nothing catching them.


def test_a_python_command_is_wrapped_like_python3():
    """The workstation hooks are hand-written with the interpreter Windows has. A
    prefix list that only knew `python3 ` let every user-level hook through unwrapped,
    so Codex got the raw command and none of the adapter's exit-code translation."""
    wrapped = hook.wrap_command("PreToolUse", 'python "C:/devkit/scripts/guard.py"')
    assert "codex-hook-adapter.py" in wrapped
    assert "--event PreToolUse" in wrapped


def test_an_absolute_root_replaces_the_repo_root_token():
    """A user hook needs a stable root even when the session is not in a checkout."""
    wrapped = hook.wrap_command("PreToolUse", 'python "x/guard.py"', root="C:/ws/devkit")
    assert "C:/ws/devkit/scripts/hooks/codex-hook-adapter.py" in wrapped
    assert "git rev-parse" not in wrapped


def test_an_absolute_root_produces_a_windows_override_without_git_lookup():
    entry = hook.to_codex_hooks(
        {
            "hooks": {
                "PreToolUse": [{"hooks": [{"type": "command", "command": 'python "x/guard.py"'}]}]
            }
        },
        root="C:/ws/devkit",
    )["hooks"]["PreToolUse"][0]["hooks"][0]
    assert "git rev-parse" not in entry["commandWindows"]
    assert entry["commandWindows"].startswith(
        'python3 "C:/ws/devkit/scripts/hooks/codex-hook-adapter.py"'
    )


def test_the_per_repo_form_still_resolves_through_the_git_root():
    """Repo-level files must stay repo-relative so a fresh clone works anywhere."""
    wrapped = hook.wrap_command("PreToolUse", 'python3 "x/guard.py"')
    assert hook.CODEX_LAUNCHER in wrapped
    assert "__CODEX_PROJECT_ROOT__" in wrapped


def test_session_start_is_redirected_through_the_absolute_codex_entrypoint():
    wrapped = hook.wrap_command(
        "SessionStart", 'bash ".claude/hooks/session-start.sh"', root="C:/ws/devkit"
    )
    assert "C:/ws/devkit/scripts/hooks/codex-session-start.py" in wrapped


def test_stable_root_never_points_into_a_box(tmp_path, monkeypatch):
    """A box is destroyed when its PR merges. The workstation file is read for as long
    as the machine exists, so a root resolved to a box would leave every Codex hook
    invoking a deleted directory -- and running --user from a box is now the normal
    way this repo is worked on."""
    box = tmp_path / ".worktrees" / "devkit--topic-0808" / "scripts"
    box.mkdir(parents=True)
    (tmp_path / "devkit").mkdir()
    monkeypatch.setattr(hook, "__file__", str(box / "sync-codex-hooks.py"))
    assert hook.stable_root() == (tmp_path / "devkit").as_posix()


def test_stable_root_is_the_repo_itself_outside_a_box(tmp_path, monkeypatch):
    scripts = tmp_path / "devkit" / "scripts"
    scripts.mkdir(parents=True)
    monkeypatch.setattr(hook, "__file__", str(scripts / "sync-codex-hooks.py"))
    assert hook.stable_root() == (tmp_path / "devkit").as_posix()


def test_sync_creates_the_destination_directory(tmp_path):
    """~/.codex may exist while ~/.codex/hooks.json's parent chain does not on a fresh
    machine; the old code raised FileNotFoundError instead of writing."""
    src = tmp_path / "settings.json"
    src.write_text(json.dumps({"hooks": {}}), encoding="utf-8")
    dest = tmp_path / "nested" / "deeper" / "hooks.json"
    assert hook.sync(src, dest) == 0
    assert dest.is_file()


def test_the_user_pair_carries_the_guard_and_the_slug_recorder(tmp_path):
    """The regression this whole change exists for: both must reach Codex."""
    src = tmp_path / "settings.json"
    src.write_text(
        json.dumps(
            {
                "hooks": {
                    "UserPromptSubmit": [
                        {"hooks": [{"type": "command", "command": 'python "d/task_slug.py"'}]}
                    ],
                    "PreToolUse": [
                        {
                            "matcher": "^(Edit|Write)$",
                            "hooks": [
                                {"type": "command", "command": 'python "d/worktree-guard.py"'}
                            ],
                        }
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    dest = tmp_path / "hooks.json"
    hook.sync(src, dest, root="C:/ws/devkit")
    body = dest.read_text(encoding="utf-8")
    assert "task_slug.py" in body
    assert "worktree-guard.py" in body
    assert "git rev-parse" not in body
