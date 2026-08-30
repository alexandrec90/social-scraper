"""Execution contract for every hook this repo wires into Claude Code.

**This file is vendored into every consuming project**, so nothing here may assert a
value specific to one project. The set of hooks under test is read from that repo's own
`.claude/settings.json`, and every project-varying value comes from `harness_config`.

The gap this closes. Every other test in this tree calls a hook's *pure functions* --
`decide()`, `offenders()`, `verify()` -- and asserts the answer. None of them runs a
hook the way Claude Code runs it: as a subprocess, fed JSON on stdin, judged by its
**exit code**. That seam is where the harness has actually failed, and it failed
silently every time, because a hook that misbehaves as a process still passes a suite
that only ever imports it.

Three failures from this workspace's own transcripts, none of which any existing test
could have caught:

  - **391 sessions' worth of `exit=42`** (2026-07-21 to 2026-07-26). The Bash gate
    blocked by exiting 42. Claude Code's contract reserves 2 for "block, and feed
    stderr back to the model"; *every other* non-zero code is a non-blocking hook
    **error**, reported to the human and then the tool call proceeds anyway. So for six
    days the gate did not gate: it emitted a bare "Failed with non-blocking status
    code: No stderr output" -- no reason, no remedy, nothing the agent could act on --
    and let the command run. `test_enforce_capped_bash.py` was green throughout,
    because `decide()` returned the right *decision* the whole time.
    `test_exit_codes_honour_the_hook_contract` is the regression: it reads the codes
    out of the source, so it fails on the constant rather than waiting for a session to
    trip over it.

  - **`lint-fix.py` crashing on a stray debug f-string** (2026-07-24) and
    **`task_slug.py` crashing on unresolved merge-conflict markers in a module it
    imports** (2026-08-19). Both are `SyntaxError` at interpreter start, so both die on
    *any* input; both nevertheless reached a live session, because a broken module is
    an import error in the test suite too and the suite was not what ran.
    `test_hook_survives_degenerate_stdin` catches them by paying one subprocess per
    wired hook -- the cheapest possible thing that actually executes the file.

Why not drive the real `claude` CLI. That test exists -- `tests/test_claude_hooks_live.py`,
marked `claude_live` and `paid` -- and it is the right tool for "does the model comply
with the block message". It cannot be the gate here: it needs the CLI installed, an API
key, and a budget, so it cannot run in a consuming project's CI, which is exactly where
a vendored hook first meets a repo devkit has never seen. This file is the CI-viable
half: no CLI, no network, no key, and it asserts the part that is a *contract* rather
than a behaviour.

Keeping the runs inert. These hooks have side effects by design -- `stop.py` runs lint
and tests, `worktree-guard.py` spawns a worktree, `lint-fix.py` rewrites the file it is
handed. A contract test that triggered any of those would be a slow test that mutates
the tree it is testing. The degenerate payloads are what make it safe: a hook given
`{}` has no `tool_name`, no `file_path` and no session, so every one of them takes its
"nothing to do here" branch. `stop.py` is the exception -- absent `stop_hook_active` it
would run the whole gate -- so its documented opt-out is set for every run. That the
opt-out is read from `harness_config` rather than spelled here is the usual vendoring
rule: the variable is prefixed per project.
"""

import ast
import json
import os
import shlex
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

import pytest
from conftest import REPO_ROOT, load_module

SETTINGS = REPO_ROOT / ".claude" / "settings.json"
MANIFEST = REPO_ROOT / ".devkit.toml"

# Claude Code's hook contract, and the whole point of this file. 0 allows the call; 2
# blocks it and feeds stderr to the model. 1 is the ordinary "this hook errored"
# code -- non-blocking, reported, tool proceeds -- which is a legitimate thing for a
# hook to do on an internal failure. Anything else is a code the runtime has no meaning
# for, and it degrades to the same silent non-blocking error that hid the Bash gate for
# six days.
CONTRACT_EXIT_CODES = frozenset({0, 1, 2})

# One subprocess per hook per payload. Generous, because a cold interpreter on Windows
# with an antivirus scanner in the path is slow, and a flaky ceiling in a vendored test
# is worse than a slow one. A hook that needs longer than this at *startup* is a defect
# regardless: it is blocking an agent's tool call for the duration.
HOOK_TIMEOUT = 60.0

# Every shape of stdin a hook must survive without crashing. The empty string is not
# hypothetical: Claude Code closes stdin with no payload when a hook fires for an event
# carrying no data, and a `json.load(sys.stdin)` with no guard raises there.
DEGENERATE_STDIN = (
    pytest.param("", id="empty"),
    pytest.param("   \n", id="whitespace"),
    pytest.param("not json at all", id="not-json"),
    pytest.param("null", id="json-null"),
    pytest.param("[]", id="json-list"),
    pytest.param("{}", id="empty-object"),
    pytest.param('{"tool_name": "Bash"}', id="tool-name-only"),
)


@dataclass(frozen=True)
class Wired:
    """One `command` entry in `.claude/settings.json`, and the script it names."""

    event: str
    matcher: str
    command: str
    script: Path | None
    args: tuple[str, ...] = ()

    def __str__(self) -> str:
        name = self.script.name if self.script else "inline"
        return f"{self.event}:{name}"


def script_from_command(command: str, root: Path) -> Path | None:
    """The in-repo script a hook command runs, or None when it runs none.

    Hook commands are shell strings, and the interpreter is deliberately not parsed:
    a settings file may spell it `python3`, `python`, or an absolute path, and on this
    workspace `python3` is a Store shim that cannot import pytest. What is wanted is
    only the *file*, which the caller then runs under `sys.executable`.

    Returns None for a command that names no file in the repo -- devkit wires one
    `PostToolUseFailure` hook as a literal `echo '{...}'`, which has no script to
    execute and nothing this file can assert about.
    """
    for token in command.replace("'", '"').split('"'):
        candidate = token.strip()
        if not candidate or not candidate.endswith((".py", ".sh")):
            continue
        # `${CLAUDE_PROJECT_DIR:-.}` is how every vendored command roots itself. The
        # substitution never happens here, so strip the literal prefix instead of
        # expanding it -- and accept a bare relative path too, which is what a
        # hand-written entry in a consuming project tends to look like.
        relative = candidate.split("}", 1)[-1].lstrip("/\\") if "}" in candidate else candidate
        resolved = (root / relative).resolve()
        if resolved.is_file():
            return resolved
    return None


def args_from_command(command: str, script: Path, root: Path) -> tuple[str, ...]:
    """The arguments the settings file passes to `script`, in the order it passes them.

    A hook is wired as a whole command line, and the flags on it are part of what the
    project asked for: carameli wires `prune-logs.py --quiet`, whose entire purpose is
    the silence `test_an_allowed_call_writes_no_loose_stdout` asserts. Running the
    script bare exercises a configuration the runtime never runs -- and that test
    reported the summary the script prints *when asked to be verbose* as a stray
    `print()`, red on a consumer's first CI run after adopting v0.11.0 while devkit's
    own suite stayed green (its one SessionStart hook is a shell script, so it is not
    in `python_hooks()` at all). That asymmetry is the vendored tier's standing trap:
    this file runs against every consumer's settings, not devkit's.

    Tolerant of a command it cannot split -- an unbalanced quote is a defect in the
    settings file, and the other tests here report it far better than a traceback out
    of the parametrizer would.
    """
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return ()
    for index, token in enumerate(tokens):
        if not token.endswith((".py", ".sh")):
            continue
        relative = token.split("}", 1)[-1].lstrip("/\\") if "}" in token else token
        candidate = Path(relative)
        resolved = candidate if candidate.is_absolute() else (root / relative)
        if resolved.resolve() == script:
            return tuple(tokens[index + 1 :])
    return ()


def wired_hooks(settings: dict, root: Path) -> list[Wired]:
    """Every hook command in a settings file, flattened across events and matchers.

    Pure and settings-shaped rather than reading the repo, so the malformed-config
    cases below can exercise it without a fixture directory.
    """
    found: list[Wired] = []
    for event, groups in (settings.get("hooks") or {}).items():
        for group in groups or ():
            for entry in group.get("hooks") or ():
                if entry.get("type") != "command":
                    continue
                command = entry.get("command") or ""
                script = script_from_command(command, root)
                found.append(
                    Wired(
                        event=event,
                        matcher=group.get("matcher", ""),
                        command=command,
                        script=script,
                        args=args_from_command(command, script, root) if script else (),
                    )
                )
    return found


def declared_exit_codes(source: str) -> set[int]:
    """Every integer exit code a hook script can hand back, read statically.

    Static rather than executed, because the codes that matter are the ones on paths a
    test would have to *provoke* -- and provoking a block is what makes a runtime check
    hook-specific and side-effectful. Two spellings are collected, which between them
    cover how every hook in the tree is written:

      - `sys.exit(2)` / `raise SystemExit(2)` -- the literal at the call site;
      - `EXIT_BLOCK = 2` -- a module-level constant whose name says it is an exit code,
        which is the form that carried the 42.

    A computed code (`sys.exit(len(errors))`) is invisible here and deliberately so:
    this is a cheap check for a specific, repeated mistake, not a proof. Returning the
    empty set for a script it cannot read is the honest answer, and the caller treats
    a script with no readable codes as trivially conformant rather than as a failure.
    """
    codes: set[int] = set()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name in {"exit", "_exit"} and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, int):
                    codes.add(first.value)
        elif isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            if getattr(node.exc.func, "id", None) == "SystemExit" and node.exc.args:
                first = node.exc.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, int):
                    codes.add(first.value)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                named = getattr(target, "id", "")
                if named.startswith("EXIT") and isinstance(node.value, ast.Constant):
                    if isinstance(node.value.value, int):
                        codes.add(node.value.value)
    return codes


def env_prefix() -> str:
    """The project's control-env prefix, read from `.devkit.toml` and nothing else.

    `harness_config.Config.env()` is the authority on this and is *not* used here, on
    purpose. This file's whole job is to keep running when a hook module is broken --
    the 2026-08-19 outage was a hook dying because a module it imports had unresolved
    conflict markers in it -- and importing that module at collection time would take
    the entire contract suite down with the very defect it exists to report. So the
    prefix comes from the manifest via `tomllib`, which is stdlib and reads data rather
    than executing it. `test_the_env_prefix_matches_harness_config` pins the two
    together, and pays the import inside a single test so only that one goes red.
    """
    default = "DEVKIT"
    if not MANIFEST.is_file():
        return default
    try:
        data = tomllib.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return default
    project = data.get("project")
    if not isinstance(project, dict):
        return default
    found = project.get("env_prefix", default)
    return found if isinstance(found, str) and found else default


def inert_env() -> dict[str, str]:
    """The environment every hook subprocess runs under.

    `stop.py` is the one hook whose no-op branch is not reachable from a degenerate
    payload: absent `stop_hook_active` it assumes a real Stop and runs the project's
    whole lint-and-test gate, which would turn one contract test into the slowest thing
    in CI. Its documented opt-out is the seam, and the variable is prefixed per project
    (`DEVKIT_`, `CARAMELI_`, ...) -- spelling one of them here would be exactly the
    hard-coded project value this tree forbids.
    """
    return {**os.environ, f"{env_prefix()}_SKIP_STOP_VERIFY": "1"}


def run_hook(
    script: Path,
    payload: str,
    timeout: float = HOOK_TIMEOUT,
    args: tuple[str, ...] = (),
):
    """Run one hook the way the runtime does: as a subprocess, JSON on stdin.

    Under `sys.executable`, not the interpreter the settings file names -- see
    `script_from_command`. With the arguments the settings file passes -- see
    `args_from_command`. Never `check=True`: a non-zero code is the thing under test.
    """
    return subprocess.run(
        [sys.executable, str(script), *args],
        input=payload,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=REPO_ROOT,
        env=inert_env(),
    )


def settings() -> dict:
    if not SETTINGS.is_file():
        pytest.skip("project wires no .claude/settings.json")
    return json.loads(SETTINGS.read_text(encoding="utf-8"))


def python_hooks() -> list[Wired]:
    """The wired hooks this file can execute: in-repo, and Python.

    `.sh` entries are covered by the existence assertion only. They are executed by
    whatever shell the runtime has, which on Windows is not a given, and a vendored
    test may not assume one.
    """
    return [w for w in wired_hooks(settings(), REPO_ROOT) if w.script and w.script.suffix == ".py"]


def _ids(items: list[Wired]) -> list[str]:
    return [str(w) for w in items]


# --- The wiring resolves ---------------------------------------------------------


def test_every_wired_hook_names_a_file_that_exists():
    """A settings entry pointing at nothing is a hook that silently never runs.

    The failure is invisible from the settings file, which still *looks* like the gate
    is wired -- and it is the shape a rename produces, since `sync-devkit.py --pull`
    moves the script while `settings.json` is not vendored and does not move with it.
    """
    unresolved = [
        w.command
        for w in wired_hooks(settings(), REPO_ROOT)
        if w.script is None and (".py" in w.command or ".sh" in w.command)
    ]
    assert unresolved == [], f"settings.json names hook scripts that do not exist: {unresolved}"


def test_the_repo_wires_at_least_one_hook():
    """Guards every parametrised test below from passing on an empty collection."""
    assert python_hooks(), "no Python hooks wired; the contract tests below assert nothing"


# --- The exit-code contract ------------------------------------------------------


def test_exit_codes_honour_the_hook_contract():
    """No hook may exit with a code the runtime has no meaning for.

    The direct regression for the six days the Bash gate spent exiting 42: blocked in
    its own head, non-blocking in the runtime's, and green in its own test suite. Read
    statically so the assertion covers the block path without provoking a block.
    """
    offenders: dict[str, set[int]] = {}
    for wired in python_hooks():
        assert wired.script is not None  # narrowed by python_hooks()
        codes = declared_exit_codes(wired.script.read_text(encoding="utf-8"))
        rogue = codes - CONTRACT_EXIT_CODES
        if rogue:
            offenders[wired.script.name] = rogue
    assert offenders == {}, (
        "hook scripts declare exit codes outside Claude Code's contract "
        f"{sorted(CONTRACT_EXIT_CODES)}: {offenders}. Every non-zero code except 2 is "
        "reported as a non-blocking hook *error* and the tool call proceeds anyway, so "
        "a gate that means to block must use 2."
    )


# --- The runtime contract --------------------------------------------------------


@pytest.mark.parametrize("payload", DEGENERATE_STDIN)
@pytest.mark.parametrize("wired", python_hooks(), ids=_ids(python_hooks()))
def test_hook_survives_degenerate_stdin(wired: Wired, payload: str):
    """A hook must never crash, whatever arrives on stdin.

    Covers the two `SyntaxError` outages directly -- a module that will not compile
    fails here on every payload -- and, more generally, pins the rule that a hook
    handed input it does not understand gets out of the way. A traceback in this seam
    is the worst available outcome: it is noise the agent cannot act on, it is
    attributed to devkit rather than to the payload, and it costs a turn to report.
    """
    assert wired.script is not None
    try:
        result = run_hook(wired.script, payload, args=wired.args)
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"{wired.script.name} did not exit within {HOOK_TIMEOUT}s on {payload!r}; "
            "a hook blocks the agent's tool call for its whole runtime"
        )
    assert "Traceback (most recent call last)" not in result.stderr, (
        f"{wired.script.name} crashed on {payload!r}:\n{result.stderr[:2000]}"
    )
    assert result.returncode in CONTRACT_EXIT_CODES, (
        f"{wired.script.name} exited {result.returncode} on {payload!r}; "
        f"the contract allows {sorted(CONTRACT_EXIT_CODES)}"
    )


@pytest.mark.parametrize("wired", python_hooks(), ids=_ids(python_hooks()))
def test_a_blocking_exit_always_carries_a_reason(wired: Wired):
    """Exit 2 with an empty stderr is a block the agent is told nothing about.

    This is the second half of the 42 outage and the more expensive half: the runtime
    printed "No stderr output" 391 times, so even once the code was right, a silent
    block would have left the agent guessing. Only exercised on the degenerate
    payloads, which is enough -- a hook that blocks on `{}` and says why is a hook
    whose blocking path writes to stderr.
    """
    assert wired.script is not None
    for payload in ("", "{}"):
        result = run_hook(wired.script, payload, args=wired.args)
        if result.returncode == 2:
            assert result.stderr.strip(), (
                f"{wired.script.name} blocked (exit 2) on {payload!r} with no stderr; "
                "the model is shown stderr and nothing else, so a silent block is "
                "indistinguishable from a broken hook"
            )


@pytest.mark.parametrize("wired", python_hooks(), ids=_ids(python_hooks()))
def test_an_allowed_call_writes_no_loose_stdout(wired: Wired):
    """On exit 0 a hook's stdout is JSON or nothing -- never prose.

    Claude Code parses a hook's stdout as its structured result for several events;
    for the rest it is injected into the transcript. Either way a stray `print()` is
    context the agent pays for on every single tool call, which at the rate these fire
    is the most expensive kind of harmless bug.
    """
    assert wired.script is not None
    result = run_hook(wired.script, "{}", args=wired.args)
    if result.returncode != 0 or not result.stdout.strip():
        return
    try:
        json.loads(result.stdout)
    except json.JSONDecodeError:
        pytest.fail(
            f"{wired.script.name} wrote non-JSON to stdout while allowing the call:\n"
            f"{result.stdout[:1000]}"
        )


# --- The gate actually gates -----------------------------------------------------


def capped_bash_gate() -> Wired | None:
    """The wired PreToolUse Bash gate, found by wiring rather than by name."""
    for wired in python_hooks():
        if wired.event == "PreToolUse" and wired.script and "capped-bash" in wired.script.name:
            return wired
    return None


def test_the_bash_gate_blocks_with_two_and_says_why():
    """End-to-end proof that a real block is a *blocking* exit carrying a reason.

    `ls -la` is on the gate's closed blocklist in every project -- the list is policy,
    not configuration -- and the gate is a pure decision with no side effects, which
    makes it the one hook whose blocking path is safe to provoke from a test. This is
    the assertion that would have gone red on day one of the 42.
    """
    gate = capped_bash_gate()
    if gate is None or gate.script is None:
        pytest.skip("project does not wire the capped-Bash gate")
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls -la"}})
    result = run_hook(gate.script, payload, args=gate.args)
    assert result.returncode == 2, (
        f"the Bash gate exited {result.returncode} on an uncapped `ls -la`; anything "
        "but 2 is a non-blocking error and the command runs anyway"
    )
    assert result.stderr.strip(), "the gate blocked without telling the agent why"


def test_the_bash_gate_allows_a_capped_command():
    """The other direction, so the test above cannot be satisfied by blocking everything."""
    gate = capped_bash_gate()
    if gate is None or gate.script is None:
        pytest.skip("project does not wire the capped-Bash gate")
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo hello"}})
    result = run_hook(gate.script, payload, args=gate.args)
    assert result.returncode == 0, (
        f"the Bash gate exited {result.returncode} on a command that produces no "
        f"repository-sized output:\n{result.stderr[:1000]}"
    )


# --- The helpers this file leans on ----------------------------------------------


def test_the_env_prefix_matches_harness_config():
    """`env_prefix()` must agree with the authority it deliberately does not import.

    The one test in this file that loads a hook module, so a broken harness tree costs
    exactly this assertion instead of the whole suite -- which is the arrangement
    `env_prefix` exists to buy.
    """
    cfg = load_module("scripts/hooks/harness_config.py")
    authoritative = cfg.load(REPO_ROOT).env("SKIP_STOP_VERIFY")
    assert f"{env_prefix()}_SKIP_STOP_VERIFY" == authoritative


def test_env_prefix_falls_back_when_the_manifest_is_unreadable(monkeypatch, tmp_path):
    """A missing or malformed `.devkit.toml` yields the default, never an exception."""
    monkeypatch.setattr(sys.modules[__name__], "MANIFEST", tmp_path / "absent.toml")
    assert env_prefix() == "DEVKIT"
    broken = tmp_path / "broken.toml"
    broken.write_text("[project\nenv_prefix =", encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "MANIFEST", broken)
    assert env_prefix() == "DEVKIT"


def test_env_prefix_reads_a_project_override(monkeypatch, tmp_path):
    manifest = tmp_path / ".devkit.toml"
    manifest.write_text('[project]\nenv_prefix = "CARAMELI"\n', encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "MANIFEST", manifest)
    assert env_prefix() == "CARAMELI"


def test_declared_exit_codes_reads_both_spellings():
    codes = declared_exit_codes(
        "EXIT_BLOCK = 42\n"
        "import sys\n"
        "def f():\n"
        "    sys.exit(3)\n"
        "def g():\n"
        "    raise SystemExit(7)\n"
        "def h():\n"
        "    sys.exit(0)\n"
    )
    assert codes == {0, 3, 7, 42}


def test_declared_exit_codes_ignores_a_computed_code():
    """Documented blind spot, asserted so it stays a known one rather than a surprise."""
    assert declared_exit_codes("import sys\nsys.exit(len('ab'))\n") == set()


def test_script_from_command_resolves_the_project_dir_prefix(tmp_path):
    target = tmp_path / "scripts" / "hooks" / "thing.py"
    target.parent.mkdir(parents=True)
    target.write_text("", encoding="utf-8")
    command = 'python3 "${CLAUDE_PROJECT_DIR:-.}/scripts/hooks/thing.py"'
    assert script_from_command(command, tmp_path) == target.resolve()


def test_script_from_command_returns_none_for_an_inline_command(tmp_path):
    assert script_from_command("echo '{\"ok\": true}'", tmp_path) is None


def test_script_from_command_returns_none_when_the_file_is_absent(tmp_path):
    command = 'python3 "${CLAUDE_PROJECT_DIR:-.}/scripts/hooks/gone.py"'
    assert script_from_command(command, tmp_path) is None


def test_run_hook_passes_the_wired_arguments_to_the_process(tmp_path):
    """The failure this file shipped with, reduced to its two lines.

    A hook wired with a flag that suppresses its output was run without the flag, so
    the output it is *supposed* to produce unflagged was reported as a stray print.
    Asserting the parser alone would not have caught it: `Wired.args` was right and
    the subprocess still never saw them.
    """
    script = tmp_path / "chatty.py"
    script.write_text(
        "import sys\nif '--quiet' not in sys.argv[1:]:\n    print('[chatty] something happened')\n",
        encoding="utf-8",
    )
    assert run_hook(script, "{}").stdout.strip() == "[chatty] something happened"
    assert run_hook(script, "{}", args=("--quiet",)).stdout.strip() == ""


def _wired_script(tmp_path):
    target = tmp_path / "scripts" / "prune-logs.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("", encoding="utf-8")
    return target.resolve()


def test_args_from_command_keeps_the_flags_the_settings_file_passes(tmp_path):
    """The regression: `--quiet` is why the hook is silent, so dropping it is a lie.

    Run without it, carameli's `prune-logs.py` prints the summary it exists to print,
    and `test_an_allowed_call_writes_no_loose_stdout` called that loose stdout.
    """
    target = _wired_script(tmp_path)
    command = 'python3 "${CLAUDE_PROJECT_DIR:-.}/scripts/prune-logs.py" --quiet'
    assert args_from_command(command, target, tmp_path) == ("--quiet",)


def test_args_from_command_keeps_order_and_values(tmp_path):
    target = _wired_script(tmp_path)
    command = 'python3 "${CLAUDE_PROJECT_DIR:-.}/scripts/prune-logs.py" --keep 5 --quiet'
    assert args_from_command(command, target, tmp_path) == ("--keep", "5", "--quiet")


def test_args_from_command_is_empty_for_a_bare_invocation(tmp_path):
    target = _wired_script(tmp_path)
    command = 'python3 "${CLAUDE_PROJECT_DIR:-.}/scripts/prune-logs.py"'
    assert args_from_command(command, target, tmp_path) == ()


def test_args_from_command_ignores_arguments_before_the_script(tmp_path):
    """`python3 -u thing.py --quiet` passes `-u` to the interpreter, not to the hook."""
    target = _wired_script(tmp_path)
    command = 'python3 -u "${CLAUDE_PROJECT_DIR:-.}/scripts/prune-logs.py" --quiet'
    assert args_from_command(command, target, tmp_path) == ("--quiet",)


def test_args_from_command_tolerates_an_unbalanced_quote(tmp_path):
    """A settings file this malformed is reported by the other tests, not by a crash."""
    target = _wired_script(tmp_path)
    assert args_from_command('python3 "scripts/prune-logs.py --quiet', target, tmp_path) == ()


def test_wired_hooks_records_the_arguments_on_each_entry(tmp_path):
    target = _wired_script(tmp_path)
    found = wired_hooks(
        {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": (
                                    'python3 "${CLAUDE_PROJECT_DIR:-.}'
                                    '/scripts/prune-logs.py" --quiet'
                                ),
                            }
                        ]
                    }
                ]
            }
        },
        tmp_path,
    )
    assert [(w.script, w.args) for w in found] == [(target, ("--quiet",))]


def test_wired_hooks_flattens_events_and_skips_non_command_entries(tmp_path):
    found = wired_hooks(
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "^Bash$",
                        "hooks": [
                            {"type": "command", "command": "a"},
                            {"type": "prompt", "prompt": "ignored"},
                        ],
                    }
                ],
                "Stop": [{"hooks": [{"type": "command", "command": "b"}]}],
            }
        },
        tmp_path,
    )
    assert [(w.event, w.matcher, w.command) for w in found] == [
        ("PreToolUse", "^Bash$", "a"),
        ("Stop", "", "b"),
    ]


def test_wired_hooks_tolerates_an_empty_or_absent_hooks_block(tmp_path):
    assert wired_hooks({}, tmp_path) == []
    assert wired_hooks({"hooks": {}}, tmp_path) == []
    assert wired_hooks({"hooks": {"Stop": []}}, tmp_path) == []
    assert wired_hooks({"hooks": {"Stop": [{"hooks": []}]}}, tmp_path) == []
