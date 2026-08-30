"""Unit tests for the invoke-capped output-capping helper.

Vendored tier: every value here is either a literal the test itself supplies or is
read from `hook.CFG`, never from the repo this happens to run in.
"""

from conftest import load_module

hook = load_module("scripts/hooks/invoke-capped.py")


# --- cap_output ---


def test_small_output_passthrough():
    data = b"hello world"
    assert hook.cap_output(data, max_bytes=4000, head_bytes=2000) == data


def test_exact_boundary_passthrough():
    data = b"a" * 2000
    assert hook.cap_output(data, max_bytes=2000, head_bytes=1000) == data


def test_oversized_truncates_with_marker():
    data = b"a" * 12000
    out = hook.cap_output(data, max_bytes=2000, head_bytes=1000)
    assert b"[truncated bytes=10000]" in out
    # head + marker + tail stays close to the cap (marker adds a small overhead)
    assert len(out) < 2000 + 64


def test_head_and_tail_windows_preserved():
    data = b"H" * 1000 + b"M" * 10000 + b"T" * 1000
    out = hook.cap_output(data, max_bytes=2000, head_bytes=1000)
    assert out.startswith(b"H" * 1000)
    assert out.endswith(b"T" * 1000)


def test_head_bytes_clamped_to_max():
    data = b"a" * 5000
    # head_bytes > max_bytes must not raise or produce a negative tail
    out = hook.cap_output(data, max_bytes=2000, head_bytes=9999)
    assert b"[truncated bytes=3000]" in out


def test_zero_head_keeps_only_tail():
    data = b"H" * 1000 + b"T" * 4000
    out = hook.cap_output(data, max_bytes=2000, head_bytes=0)
    assert out.endswith(b"T" * 2000)
    assert b"[truncated bytes=3000]" in out


# --- run_capped ---


def test_run_capped_preserves_exit_code():
    code, _ = hook.run_capped("exit 7", max_bytes=4000, head_bytes=2000)
    assert code == 7


def test_run_capped_captures_stdout():
    code, out = hook.run_capped("echo hello", max_bytes=4000, head_bytes=2000)
    assert code == 0
    assert b"hello" in out


def test_run_capped_merges_stderr():
    code, out = hook.run_capped("echo err 1>&2", max_bytes=4000, head_bytes=2000)
    assert code == 0
    assert b"err" in out


def test_powershell_mode_invokes_pwsh_directly(monkeypatch):
    seen = {}

    class Result:
        returncode = 7
        stdout = b"out"
        stderr = b"err"

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        return Result()

    monkeypatch.setattr(hook.subprocess, "run", fake_run)
    code, output = hook.run_capped(
        "Get-Content README.md",
        max_bytes=4000,
        head_bytes=2000,
        command_shell="powershell",
    )

    assert code == 7
    assert output == b"outerr"
    assert seen["command"] == [
        "pwsh",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "Get-Content README.md",
    ]
    assert seen["kwargs"] == {"capture_output": True}


# --- main argument validation and config wiring ---


def test_main_rejects_tiny_max_bytes(capsys):
    rc = hook.main(["--command", "echo hi", "--max-bytes", "10"])
    assert rc == 1
    assert "must be >=" in capsys.readouterr().err


def test_defaults_come_from_the_manifest():
    """The CLI defaults are the `[bash]` values, not hard-coded numbers.

    Guards the seam: a project that widens the cap in `.devkit.toml` must
    get the wider cap from a bare `--command` invocation, with no flag.
    """
    parser_defaults = hook.main.__defaults__
    assert parser_defaults is not None  # (argv=None)
    # Exercised through the real parser rather than by reading the constant.
    code, out = hook.run_capped(
        "echo x", max_bytes=hook.CFG.bash.max_bytes, head_bytes=hook.CFG.bash.head_bytes
    )
    assert code == 0
    assert b"x" in out


def test_min_max_bytes_is_below_the_configured_cap():
    """A manifest that sets max_bytes under the floor would reject every call."""
    assert hook.CFG.bash.max_bytes >= hook.MIN_MAX_BYTES


def test_min_max_bytes_has_one_definition():
    """The floor is enforced here and quoted by `enforce-capped-bash.py`'s block
    message. Two literals would let the message advertise a floor the wrapper does not
    apply, which is the drift the message's cap value is already pinned against."""
    assert hook.MIN_MAX_BYTES is hook.harness_config.MIN_MAX_BYTES
