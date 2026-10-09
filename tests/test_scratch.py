import logging
import os
import subprocess
import sys

import pytest

from social_scraper import scratch
from social_scraper.scratch import owner_pid, pid_alive, scratch_dir, sweep


def dead_pid() -> int:
    """The pid of a process that has exited (the OS may reuse it, not within a test)."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def make_scratch(parent, name, size=10):
    path = parent / name
    (path / "tmp").mkdir(parents=True)
    (path / "spool.duckdb").write_bytes(b"x" * size)
    return path


def test_the_scratch_dir_carries_its_owner_and_is_gone_after_the_block(tmp_path):
    with scratch_dir(tmp_path) as spool:
        assert spool.is_dir() and spool.parent == tmp_path
        assert owner_pid(spool.name) == os.getpid()
        (spool / "spool.duckdb").write_bytes(b"rows")
    assert list(tmp_path.iterdir()) == []


def test_the_scratch_dir_is_removed_when_the_run_raises(tmp_path):
    with pytest.raises(RuntimeError), scratch_dir(tmp_path):
        raise RuntimeError("the importer crashed")
    assert list(tmp_path.iterdir()) == []


def test_owner_pid_reads_only_this_modules_names():
    assert owner_pid("import-4242-ab_c12xy") == 4242
    assert owner_pid("import-ab_c12xy") is None  # the pre-pid `TemporaryDirectory` spelling
    assert owner_pid("symbols") is None
    assert owner_pid("import-12-") is None


def test_pid_alive_tells_a_running_process_from_an_exited_one():
    assert pid_alive(os.getpid())
    assert not pid_alive(dead_pid())
    assert not pid_alive(0)


def test_the_windows_probe_refuses_any_other_platform(monkeypatch):
    # The guard is what lets mypy check this module for Linux, where `ctypes` has no
    # `WinDLL`; at run time it stops a call that `pid_alive` never makes off Windows.
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(OSError, match="linux"):
        scratch._windows_pid_alive(os.getpid())


def test_a_killed_runs_scratch_is_removed_and_said_so(tmp_path, caplog):
    # 2026-10-08: a StockTwits dry run reaped for low memory left 29.7 GiB under data/.
    stale = make_scratch(tmp_path, "import-777-abcdefgh", size=2048)
    with caplog.at_level(logging.WARNING, logger=scratch.__name__):
        removed = sweep(tmp_path, alive=lambda pid: False)
    assert removed == [stale]
    assert not stale.exists()
    assert "import-777-abcdefgh" in caplog.text and "process 777" in caplog.text


def test_a_running_imports_scratch_is_left_alone(tmp_path, caplog):
    # The report's other half: a long Reddit import shared the machine with the dry run.
    live = make_scratch(tmp_path, "import-888-abcdefgh")
    with caplog.at_level(logging.INFO, logger=scratch.__name__):
        assert sweep(tmp_path, alive=lambda pid: pid == 888) == []
    assert (live / "spool.duckdb").is_file()
    assert "process 888" in caplog.text and "still running" in caplog.text


def test_an_ownerless_scratch_is_named_not_deleted(tmp_path, caplog):
    legacy = make_scratch(tmp_path, "import-k2j4_x9q")
    with caplog.at_level(logging.WARNING, logger=scratch.__name__):
        assert sweep(tmp_path, alive=lambda pid: False) == []
    assert legacy.is_dir()
    assert "import-k2j4_x9q" in caplog.text


def test_the_sweep_touches_nothing_else_in_data(tmp_path):
    (tmp_path / "symbols").mkdir()
    (tmp_path / "import-123-file.txt").write_text("a file, not a scratch dir")
    assert sweep(tmp_path, alive=lambda pid: False) == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["import-123-file.txt", "symbols"]
    assert sweep(tmp_path / "missing", alive=lambda pid: False) == []


def test_a_scratch_that_cannot_be_removed_does_not_stop_the_run(tmp_path, monkeypatch, caplog):
    make_scratch(tmp_path, "import-777-abcdefgh")

    def locked(path):
        raise PermissionError("in use by another process")

    monkeypatch.setattr(scratch.shutil, "rmtree", locked)
    with caplog.at_level(logging.WARNING, logger=scratch.__name__):
        assert sweep(tmp_path, alive=lambda pid: False) == []
    assert "could not remove" in caplog.text


def test_opening_a_scratch_dir_sweeps_first(tmp_path):
    stale = make_scratch(tmp_path, f"import-{dead_pid()}-abcdefgh")
    with scratch_dir(tmp_path) as spool:
        assert [p.name for p in tmp_path.iterdir()] == [spool.name]
    assert not stale.exists()
