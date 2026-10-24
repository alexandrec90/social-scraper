"""Unit tests for `sibling_pins.py` -- runs lease a shared path-source sibling at a commit,
and a move waits for every run leasing it at another.

**This file is vendored into every consuming project**, so it builds its sibling out of
a throwaway `.git` pointer rather than asserting anything about the repo it runs in.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from conftest import load_module

sp = load_module("scripts/hooks/sibling_pins.py")

A, B = "a" * 40, "b" * 40
# A pid the tests' `alive` answers for, never this process. Derived, not a literal: Linux
# hands pids out in sequence, so a fixed 4242 was the pytest process's own on a CI runner,
# and `blockers` then read the other run's lease as this run's and moved under it.
OTHER = os.getpid() + 100_000


def _sibling(tmp_path: Path, relative: bool = False) -> Path:
    """A linked worktree as git lays one out: `.git` is a file naming its own git dir."""
    gitdir = tmp_path / "repo" / ".git" / "worktrees" / "lake"
    gitdir.mkdir(parents=True)
    target = tmp_path / "tier" / "lake"
    target.mkdir(parents=True)
    pointer = os.path.relpath(gitdir, target) if relative else str(gitdir)
    (target / ".git").write_text(f"gitdir: {pointer}\n", encoding="utf-8")
    return target


class State:
    """The sibling's HEAD, and a `move` that records each call and sets it."""

    def __init__(self, head: str, want: str, works: bool = True):
        self.at, self.want, self.works, self.moves = head, want, works, 0

    def head(self) -> str:
        return self.at

    def move(self) -> bool:
        self.moves += 1
        if self.works:
            self.at = self.want
        return self.works


def _claim(target: Path, state: State, alive=lambda pid: True, **kw):
    said: list[str] = []
    shared = sp.Sibling(target, state.want, state.head, state.move)
    pace = sp.Pace(pid=os.getpid(), alive=alive, say=said.append, **kw)
    return sp.claim(shared, Path("proj"), pace), said


def test_the_other_run_is_never_this_process():
    """Every test below that waits on OTHER's lease passes vacuously when it is ours."""
    assert OTHER != os.getpid()
    assert OTHER not in {11, 12, 13, 77, 78, 79}, "the pids the lease tests write by hand"


def test_the_lease_directory_is_inside_the_siblings_own_git_dir(tmp_path):
    absolute = _sibling(tmp_path / "abs")
    assert (
        sp.lease_dir(absolute)
        == tmp_path / "abs" / "repo" / ".git" / "worktrees" / "lake" / sp.LEASES
    )
    relative = _sibling(tmp_path / "rel", relative=True)
    assert (
        sp.lease_dir(relative).resolve()
        == (tmp_path / "rel" / "repo" / ".git" / "worktrees" / "lake" / sp.LEASES).resolve()
    )
    own = tmp_path / "own"
    (own / ".git").mkdir(parents=True)
    assert sp.lease_dir(own) is None, "a checkout of its own is a person's: no lease"
    assert sp.lease_dir(tmp_path / "absent") is None


def test_only_a_live_fresh_well_formed_lease_counts(tmp_path):
    directory = tmp_path / "leases"
    sp.hold(directory, A, Path("t1"), OTHER)
    sp.hold(directory, B, Path("t2"), 77)
    old = sp.hold(directory, A, Path("t3"), 78)
    os.utime(old, (time.time() - sp.MAX_AGE - 1,) * 2)
    (directory / f"junk{sp.LEASE_SUFFIX}").write_text(A, encoding="utf-8")
    (directory / f"79{sp.LEASE_SUFFIX}").write_text("", encoding="utf-8")
    live = sp.live_leases(directory, time.time(), alive=lambda pid: pid != 77)
    assert live == [sp.Lease(OTHER, A, "t1", directory / f"{OTHER}{sp.LEASE_SUFFIX}")]
    assert live[0].line == f"a run in t1 (pid {OTHER}) building against {A[:7]}"
    assert sp.live_leases(tmp_path / "none", time.time()) == []


def test_only_another_runs_lease_at_another_commit_blocks_a_move(tmp_path):
    directory = tmp_path / "leases"
    sp.hold(directory, A, Path("same"), 11)
    sp.hold(directory, B, Path("mine"), 12)
    sp.hold(directory, B, Path("other"), 13)
    found = sp.blockers(directory, A, 12, time.time(), alive=lambda pid: True)
    assert [lease.pid for lease in found] == [13], "a lease at the same commit coexists"


def test_a_sibling_already_at_the_pin_is_leased_without_moving(tmp_path):
    target = _sibling(tmp_path)
    state = State(head=A, want=A)
    sp.hold(sp.lease_dir(target), B, Path("stale-claim"), OTHER)
    done, said = _claim(target, state)
    assert done.held is not None and not done.moved and state.moves == 0
    assert done.held.read_text(encoding="utf-8").startswith(A)
    assert said == []


def test_a_free_sibling_is_moved_then_leased(tmp_path):
    target = _sibling(tmp_path)
    state = State(head=B, want=A)
    done, _ = _claim(target, state)
    assert done.moved and state.at == A and state.moves == 1
    assert done.held == sp.lease_dir(target) / f"{os.getpid()}{sp.LEASE_SUFFIX}"
    assert not (sp.lease_dir(target) / sp.LOCK).exists(), "the move lock is let go"


def test_a_move_waits_for_a_run_at_another_commit_to_finish(tmp_path):
    """d32ffcc2: one tree's provision moved data-lake under another tree's lock check."""
    target = _sibling(tmp_path)
    sp.hold(sp.lease_dir(target), B, Path("tree-123"), OTHER)
    running = {OTHER: True}
    state = State(head=B, want=A)

    def sleep(_seconds):
        assert state.moves == 0, "never moved while the other run held it"
        running[OTHER] = False

    done, said = _claim(target, state, alive=lambda pid: running.get(pid, True), sleep=sleep)
    assert done.moved and state.at == A
    assert len(said) == 1 and "tree-123" in said[0] and B[:7] in said[0]


def test_a_move_gives_up_after_its_wait_and_names_who_holds_it(tmp_path):
    target = _sibling(tmp_path)
    sp.hold(sp.lease_dir(target), B, Path("tree-123"), OTHER)
    state = State(head=B, want=A)
    ticks = iter(range(100))
    done, said = _claim(target, state, wait=3, clock=lambda: next(ticks), sleep=lambda s: None)
    assert done.held is None and state.moves == 0 and state.at == B
    assert [lease.tree for lease in done.blocked] == ["tree-123"]
    assert len(said) == 1, "said once, not once per poll"


def test_a_failed_move_leases_nothing(tmp_path):
    target = _sibling(tmp_path)
    state = State(head=B, want=A, works=False)
    done, _ = _claim(target, state)
    assert done == sp.Claim(None)
    assert list(sp.lease_dir(target).glob(f"*{sp.LEASE_SUFFIX}")) == []


def test_a_sibling_with_nowhere_to_lease_is_moved_unguarded(tmp_path):
    own = tmp_path / "own"
    (own / ".git").mkdir(parents=True)
    state = State(head=B, want=A)
    done, _ = _claim(own, state)
    assert done == sp.Claim(None, moved=True) and state.at == A


def test_the_move_lock_is_exclusive_and_a_dead_holders_is_broken(tmp_path):
    directory = tmp_path / "leases"
    with sp.move_lock(directory, 1, alive=lambda pid: True) as first:
        with sp.move_lock(directory, 2, alive=lambda pid: True) as second:
            assert first and not second
        with sp.move_lock(directory, 3, alive=lambda pid: pid != 1) as third:
            assert third, "a holder that died mid-move does not hold it forever"
    assert not (directory / sp.LOCK).exists()
    (directory / sp.LOCK).write_text("1\n", encoding="utf-8")
    os.utime(directory / sp.LOCK, (time.time() - sp.LOCK_STALE - 1,) * 2)
    with sp.move_lock(directory, 2, alive=lambda pid: True) as late:
        assert late, "a lock past LOCK_STALE is a mover that died"


def test_pid_alive_tells_this_process_from_a_finished_one():
    assert sp.pid_alive(os.getpid())
    assert not sp.pid_alive(0)
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert not sp.pid_alive(child.pid)


def test_release_is_quiet_about_a_lease_already_gone(tmp_path):
    path = sp.hold(tmp_path / "leases", A, Path("t"), OTHER)
    sp.release(path)
    sp.release(path)
    assert not path.exists()
