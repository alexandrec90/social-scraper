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
