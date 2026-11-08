"""Tests for `green`, the branch that says which commit passed its gate.

**Vendored, so nothing here may name a project.** Everything arrives in the environment
or from an API call, so a fake `subprocess.run` is the whole world.

What they pin: `green` only ever moves forward along the default branch. Everything that
reads it trusts that, and two gate runs finish in any order.
"""

import json
from types import SimpleNamespace

from conftest import load_module

green = load_module("scripts/green-ref.py")

REPO = "owner/repo"
OLD = "1" * 40
NEW = "2" * 40


class FakeRun:
    def __init__(self, statuses: dict[str, str], current: str = OLD, read_error: str = ""):
        self.calls: list[list[str]] = []
        self.statuses = statuses
        self.current = current
        self.read_error = read_error

    def __call__(self, argv, capture_output=False, text=False, check=False):
        self.calls.append(list(argv))
        joined = " ".join(argv)
        for fragment, status in self.statuses.items():
            if f"compare/{fragment}" in joined:
                return SimpleNamespace(
                    stdout=json.dumps({"status": status}), stderr="", returncode=0
                )
        if "git/ref/heads/green" in joined:
            if self.read_error:
                return SimpleNamespace(stdout="", stderr=self.read_error, returncode=1)
            if not self.current:
                return SimpleNamespace(stdout="", stderr="HTTP 404: Not Found", returncode=1)
            payload = {"object": {"sha": self.current}}
            return SimpleNamespace(stdout=json.dumps(payload), stderr="", returncode=0)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    def writes(self) -> list[list[str]]:
        return [call for call in self.calls if "--method" in call]


def env(**overrides) -> dict[str, str]:
    return {"GH_REPO": REPO, "RUN_HEAD_SHA": NEW, "DEFAULT_BRANCH": "main", **overrides}


def on_main() -> dict[str, str]:
    return {f"{NEW}...main": "identical", f"{OLD}...{NEW}": "ahead"}


def test_a_passing_commit_moves_green_forward_and_never_by_force():
    run = FakeRun(on_main())
    assert green.main(env(), run) == 0
    assert len(run.writes()) == 1
    write = run.writes()[0]
    assert "PATCH" in write
    assert f"sha={NEW}" in write
    assert "force=false" in write


def test_the_first_passing_commit_creates_it():
    run = FakeRun(on_main(), current="")
    assert green.main(env(), run) == 0
    write = run.writes()[0]
    assert "POST" in write
    assert "ref=refs/heads/green" in write


def test_a_late_run_for_an_older_commit_moves_nothing():
    """Two gate runs finish in any order, and the older one finishing last is routine."""
    run = FakeRun({f"{NEW}...main": "ahead", f"{OLD}...{NEW}": "behind"})
    assert green.main(env(), run) == 0
    assert run.writes() == []


def test_a_commit_not_on_the_default_branch_moves_nothing():
    run = FakeRun({f"{NEW}...main": "diverged"})
    assert green.main(env(), run) == 0
    assert run.writes() == []


def test_a_commit_already_there_moves_nothing():
    run = FakeRun({f"{NEW}...main": "identical"}, current=NEW)
    assert green.main(env(), run) == 0
    assert run.writes() == []


def test_a_failed_read_is_not_a_missing_branch():
    """Read as missing, a transient 500 would be answered by trying to create the branch."""
    run = FakeRun({f"{NEW}...main": "identical"}, read_error="HTTP 500: Server Error")
    try:
        green.main(env(), run)
    except green.GhError:
        assert run.writes() == []
    else:
        raise AssertionError("a failed read must not be swallowed")


def test_it_refuses_to_guess():
    assert green.main(env(RUN_HEAD_SHA=""), FakeRun({})) == 1


def test_relation_reads_githubs_compare():
    assert green.relation(REPO, OLD, NEW, FakeRun(on_main())) == "ahead"


def test_current_green_is_empty_for_a_missing_branch():
    assert green.current_green(REPO, FakeRun({}, current="")) == ""
    assert green.current_green(REPO, FakeRun({}, current=OLD)) == OLD


def test_advance_says_what_it_did():
    assert green.advance(REPO, NEW, "main", FakeRun(on_main())).startswith("green moved")


def test_gh_attaches_stderr_to_a_failure():
    def failing(argv, **_):
        return SimpleNamespace(stdout="", stderr="HTTP 503", returncode=1)

    try:
        green.gh(["api", "x"], failing)
    except green.GhError as error:
        assert "503" in str(error)
    else:
        raise AssertionError("a failed call must raise")
