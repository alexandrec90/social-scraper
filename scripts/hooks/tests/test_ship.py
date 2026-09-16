"""Unit tests for the deterministic mechanics behind /ship."""

from conftest import load_module

ship = load_module("scripts/ship.py")


class _Result:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_namespaced_task_branches_are_shippable_regardless_of_agent():
    for branch in ("agent/fix-thing", "claude/fix-thing", "codex/fix-thing", "feature/x"):
        assert ship.is_shippable(branch, "main") == (True, "")
    for branch in ("", "main", "carameli-b"):
        ok, _ = ship.is_shippable(branch, "main")
        assert not ok


def test_the_branch_claude_s_own_worktree_flag_cuts_is_shippable():
    """`claude --worktree fix-thing` cuts `worktree-fix-thing`, and a name containing a
    slash has it replaced (`claude/x` -> `worktree-claude+x`), so the built-in flag can
    never produce the `<namespace>/<topic>` spelling. That is hard-coded in the CLI with
    no setting behind it, so refusing the branch would make `/ship` work from a devkit box
    and not from Claude Code's own worktree -- a rule about provenance, not about whether
    the branch is disposable."""
    for branch in ("worktree-fix-thing", "worktree-claude+fix-thing"):
        assert ship.is_shippable(branch, "main") == (True, "")


def test_the_bare_worktree_prefix_is_not_a_topic():
    """`worktree-` alone names no task. Accepting it would turn the prefix test into a
    substring check that any branch merely starting with it passes."""
    ok, _ = ship.is_shippable("worktree-", "main")
    assert not ok


def test_default_branch_uses_shared_detection(monkeypatch):
    monkeypatch.setattr(ship.tb, "detect_default_branch", lambda git, fallback: "trunk")
    assert ship.default_branch() == "trunk"


def test_tree_clean_ignores_whitespace():
    assert ship.tree_clean(" \n")
    assert not ship.tree_clean(" M app.py\n")


def test_push_retries_transient_failure(monkeypatch):
    results = [_Result(1, stderr="connection timed out"), _Result(0)]
    monkeypatch.setattr(ship, "_git", lambda *args: results.pop(0))
    slept: list[int] = []
    assert ship._push("claude/x", sleep=slept.append)
    assert slept == [2]


def test_push_does_not_retry_rejection(monkeypatch):
    calls = []
    monkeypatch.setattr(
        ship,
        "_git",
        lambda *args: calls.append(args) or _Result(1, stderr="non-fast-forward"),
    )
    assert not ship._push("claude/x", sleep=lambda _: None)
    assert len(calls) == 1


def _wire_main(monkeypatch, *, branch="claude/x", clean=True, lint=True, push=True):
    monkeypatch.setattr(ship, "current_branch", lambda: branch)
    monkeypatch.setattr(ship, "default_branch", lambda: "main")
    monkeypatch.setattr(ship, "_porcelain", lambda: "" if clean else " M x.py\n")
    monkeypatch.setattr(ship, "_run_lint", lambda *args: lint)
    monkeypatch.setattr(ship, "_push", lambda value: push)


def test_preflight_reports_branch_and_base(monkeypatch, capsys):
    _wire_main(monkeypatch)
    assert ship.main(["--preflight"]) == ship.EXIT_OK
    assert "branch=claude/x base=main" in capsys.readouterr().out


# --- what a fresh worktree is missing ------------------------------------------
# A linked worktree checks out tracked files only. The first thing to notice used to be
# the commit-time pre-commit gate, whose `language: system` hooks resolve against a PATH
# with no venv on it -- so `/ship` learned it at its commit rather than its preflight.


def _fresh_worktree(tmp_path):
    """A checkout with a lockfile, a switched-on frontend, and neither toolchain."""
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "p"\n', encoding="utf-8")
    (tmp_path / ".devkit.toml").write_text(
        '[frontend]\nenabled = true\ndir = "web"\n', encoding="utf-8"
    )
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "package-lock.json").write_text("{}\n", encoding="utf-8")
    return tmp_path


def test_a_fresh_worktree_is_told_what_to_install_and_that_the_gates_need_it(tmp_path):
    lines = ship.toolchain_report(_fresh_worktree(tmp_path))
    assert lines[0].startswith("No .venv here")
    assert "(fix: uv sync --all-extras --all-groups)" in lines[0]
    assert lines[1].startswith("No web/node_modules")
    assert "(fix: npm ci --prefix web)" in lines[1]
    assert "pre-commit gate" in lines[-1] and "lint gate" in lines[-1]


def test_a_provisioned_checkout_is_told_nothing(tmp_path):
    root = _fresh_worktree(tmp_path)
    (root / ".venv").mkdir()
    (root / "web" / "node_modules").mkdir()
    assert ship.toolchain_report(root) == []


def test_preflight_prints_the_report_and_still_passes(monkeypatch, capsys):
    """Reported on stderr beside the branch line, and never a refusal: a checkout whose
    tools live outside `.venv` can still ship, and the gates fail on their own."""
    _wire_main(monkeypatch)
    monkeypatch.setattr(ship, "toolchain_report", lambda: ["No .venv here (fix: uv sync)"])
    assert ship.main(["--preflight"]) == ship.EXIT_OK
    captured = capsys.readouterr()
    assert "branch=claude/x base=main" in captured.out
    assert "ship: No .venv here (fix: uv sync)" in captured.err


def test_preflight_is_quiet_about_a_provisioned_checkout(monkeypatch, capsys):
    _wire_main(monkeypatch)
    monkeypatch.setattr(ship, "toolchain_report", list)
    assert ship.main(["--preflight"]) == ship.EXIT_OK
    assert capsys.readouterr().err == ""


def test_push_requires_clean_tree(monkeypatch):
    _wire_main(monkeypatch, clean=False)
    assert ship.main([]) == ship.EXIT_DIRTY_TREE


def test_unknown_arguments_are_rejected(monkeypatch):
    _wire_main(monkeypatch)
    assert ship.main(["--preflight", "--nonsense"]) == ship.EXIT_USAGE


# --- what the lint gate is actually pointed at --------------------------------


def _git_script(responses: dict[tuple[str, ...], _Result]):
    """A fake `_git` answering by argv prefix, defaulting to success with no output."""

    def fake(*args: str) -> _Result:
        for prefix, result in responses.items():
            if args[: len(prefix)] == prefix:
                return result
        return _Result(0)

    return fake


def test_the_lint_scope_is_the_branch_not_the_working_tree():
    """The regression this whole change exists for: ship demands a clean tree, so the
    working-tree diff it used to lint was empty on every single ship."""
    git = _git_script(
        {
            ("merge-base",): _Result(0, stdout="abc123\n"),
            ("diff",): _Result(0, stdout="app/main.py\ndocs/plan.md\n"),
        }
    )
    assert ship.branch_diff_files("main", git=git) == ["app/main.py", "docs/plan.md"]


def test_the_branch_diff_is_measured_from_the_remote_base_when_it_exists():
    seen: list[tuple[str, ...]] = []

    def git(*args: str) -> _Result:
        seen.append(args)
        if args[0] == "merge-base":
            return _Result(0, stdout="abc123\n")
        return _Result(0, stdout="")

    ship.branch_diff_files("main", git=git)
    assert ("merge-base", "origin/main", "HEAD") in seen


def test_a_base_ref_missing_locally_falls_back_to_the_bare_branch_name():
    seen: list[tuple[str, ...]] = []

    def git(*args: str) -> _Result:
        seen.append(args)
        if args[0] == "rev-parse":
            return _Result(1)
        if args[0] == "merge-base":
            return _Result(0, stdout="abc123\n")
        return _Result(0, stdout="")

    ship.branch_diff_files("main", git=git)
    assert ("merge-base", "main", "HEAD") in seen


def test_an_unfindable_merge_base_yields_no_paths_rather_than_a_wrong_scope():
    git = _git_script({("merge-base",): _Result(128, stderr="no merge base")})
    assert ship.branch_diff_files("main", git=git) == []


def test_deleted_paths_are_excluded_from_the_lint_scope():
    """A linter handed a path that no longer exists fails the run on a usage error."""
    seen: list[tuple[str, ...]] = []

    def git(*args: str) -> _Result:
        seen.append(args)
        if args[0] == "merge-base":
            return _Result(0, stdout="abc123\n")
        return _Result(0, stdout="")

    ship.branch_diff_files("main", git=git)
    assert any("--diff-filter=d" in args for args in seen)


def test_the_runner_is_asked_for_paths_only_when_it_understands_them():
    modern = ship._lint_argv(["a.py"], "usage: lint-all.py [--changed] [--paths FILE ...]")
    assert modern[-3:] == ["--paths", "a.py"] or modern[-2:] == ["--paths", "a.py"]

    legacy = ship._lint_argv(["a.py"], "usage: lint-all.py [--changed]")
    assert legacy[-1] == "--changed"


def test_an_empty_branch_diff_keeps_the_old_behaviour():
    assert (
        ship._lint_argv([], "usage: lint-all.py [--changed] [--paths FILE ...]")[-1] == "--changed"
    )


def test_runner_support_is_read_from_its_own_help():
    assert ship.runner_supports_paths("  --paths FILE [FILE ...]")
    assert not ship.runner_supports_paths("  --changed  lint only the working-tree diff")
