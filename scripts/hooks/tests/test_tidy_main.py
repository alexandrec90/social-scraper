"""Tests for tidying the default branch after a merge.

**Vendored, so nothing here may name a project.** A fake `subprocess.run` answers git.

What they pin is that this step can never hold anything up or take anything down: a lost
race is retried, a conflict is dropped for the next merge's tidy, and a workflow file
never goes in the push, because `GITHUB_TOKEN` would refuse all of it.
"""

from types import SimpleNamespace

from conftest import load_module

tidy = load_module("scripts/tidy-main.py")


class FakeRun:
    def __init__(self, porcelain=" M a.py\n", refuse: dict[str, int] | None = None):
        self.calls: list[list[str]] = []
        self.porcelain = porcelain
        self.refuse = dict(refuse or {})

    def __call__(self, argv, **_kwargs):
        self.calls.append(list(argv))
        word = argv[1] if argv[0] == "git" else "tidy"
        if self.refuse.get(word, 0) > 0:
            self.refuse[word] -= 1
            return SimpleNamespace(stdout="", stderr=f"{word} refused", returncode=1)
        stdout = self.porcelain if word == "status" else ""
        return SimpleNamespace(stdout=stdout, stderr="", returncode=0)

    def did(self, word: str) -> int:
        return sum(1 for call in self.calls if call[:2] == ["git", word])


ENV = {"TIDY_BRANCH": "main"}


def test_a_tidied_tree_is_committed_with_the_prefix_and_pushed():
    run = FakeRun()
    assert tidy.main(ENV, run) == 0
    commit = next(call for call in run.calls if call[:2] == ["git", "commit"])
    assert commit[-1].startswith("Tidy:"), "the search for a breaking commit steps over this"
    assert ["git", "push", "origin", "HEAD:main"] in run.calls


def test_nothing_changed_pushes_nothing():
    run = FakeRun(porcelain="")
    assert tidy.main(ENV, run) == 0
    assert run.did("commit") == 0
    assert run.did("push") == 0


def test_a_workflow_file_is_restored_before_anything_is_staged():
    run = FakeRun()
    tidy.main(ENV, run)
    restore = run.calls.index(["git", "checkout", "--", ".github/workflows"])
    stage = run.calls.index(["git", "add", "-A"])
    assert restore < stage


def test_a_lost_race_is_rebased_and_pushed_again():
    run = FakeRun(refuse={"push": 1})
    assert tidy.main(ENV, run) == 0
    assert run.did("pull") == 1
    assert run.did("push") == 2


def test_a_conflict_is_left_for_the_next_tidy_and_is_not_a_failure():
    """The merge that beat it starts this job again, over a tree that includes it."""
    run = FakeRun(refuse={"push": 9, "pull": 1})
    assert tidy.main(ENV, run) == 0
    assert ["git", "rebase", "--abort"] in run.calls


def test_a_tidy_that_could_not_run_pushes_nothing():
    run = FakeRun(refuse={"tidy": 1})
    assert tidy.main(ENV, run) == 1
    assert run.did("push") == 0


def test_git_runs_in_the_repository_and_never_raises_on_a_refusal():
    seen = {}

    def spy(argv, **kwargs):
        seen.update(kwargs, argv=argv)
        return SimpleNamespace(returncode=1)

    assert tidy.git(spy, "status").returncode == 1
    assert seen["argv"] == ["git", "status"]
    assert seen["cwd"] == tidy.REPO_ROOT
    assert seen["check"] is False


def test_push_gives_up_on_a_branch_that_keeps_moving():
    run = FakeRun(refuse={"push": 99})
    assert "still refused" in tidy.push(run, "main")
    assert run.did("push") == tidy.PUSH_TRIES


def test_a_push_refused_for_good_is_not_called_left_for_later():
    """A rebase that succeeds and a push that keeps failing is not a race, so retrying on
    the next merge would only repeat it. The job has to go red to be seen."""
    assert tidy.main(ENV, FakeRun(refuse={"push": 99})) == 1


def test_it_refuses_to_guess_where_to_push():
    assert tidy.main({"TIDY_BRANCH": ""}, FakeRun()) == 1
