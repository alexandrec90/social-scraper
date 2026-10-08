import os
import time

import pytest

from social_scraper.archive.lock import LOCK_SUFFIX, STALE_S, LockTimeout, file_lock


def test_the_lock_exists_only_while_held(tmp_path):
    path = tmp_path / "sub" / f"cat.json{LOCK_SUFFIX}"
    with file_lock(path):
        assert path.is_file()
        assert path.read_text().strip() == str(os.getpid())
    assert not path.exists()


def test_a_held_lock_is_waited_for(tmp_path):
    path = tmp_path / "x.lock"
    path.write_text("other writer")
    sleeps = []

    def other_writer_finishes(seconds):
        sleeps.append(seconds)
        path.unlink()

    with file_lock(path, sleep=other_writer_finishes):
        assert path.read_text().strip() == str(os.getpid())
    assert len(sleeps) == 1


def test_a_dead_writers_lock_is_broken(tmp_path):
    path = tmp_path / "x.lock"
    path.write_text("crashed writer")
    old = time.time() - STALE_S - 60
    os.utime(path, (old, old))

    def never(seconds):
        raise AssertionError("a stale lock must not be waited on")

    with file_lock(path, sleep=never):
        assert path.read_text().strip() == str(os.getpid())


def test_a_long_wait_times_out_and_leaves_the_holders_lock(tmp_path):
    path = tmp_path / "x.lock"
    path.write_text("busy writer")
    clock = iter([0.0, 5.0, 11.0])
    with pytest.raises(LockTimeout, match="held by another writer"):
        with file_lock(path, timeout_s=10, sleep=lambda s: None, monotonic=lambda: next(clock)):
            pass
    assert path.read_text() == "busy writer"
    assert issubclass(LockTimeout, OSError)  # an export fails the dataset and retries


def test_the_lock_is_released_when_the_block_raises(tmp_path):
    path = tmp_path / "x.lock"
    with pytest.raises(RuntimeError):
        with file_lock(path):
            raise RuntimeError("write failed")
    assert not path.exists()
