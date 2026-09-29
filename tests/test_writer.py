from datetime import timedelta

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from conftest import NOW

from social_scraper.archive import manifest, writer
from social_scraper.archive.manifest import DatasetSpec
from social_scraper.archive.writer import (
    PART_FILE,
    PartitionResult,
    VerifyError,
    merge,
    write_partition,
)

TS = pa.timestamp("us", tz="UTC")
SPEC = DatasetSpec(
    name="social_scraper_demo",
    ts_column="created_at",
    key_columns=("id",),
    schema=pa.schema([("id", pa.string()), ("created_at", TS), ("score", pa.int64())]),
    partition_of=lambda row: "demo/dt=x",
)


def rows(*items):
    return [
        {"id": i, "created_at": NOW + timedelta(minutes=n), "score": s}
        for n, (i, s) in enumerate(items)
    ]


def test_merge_new_rows_win():
    old = pa.Table.from_pylist(rows(("a", 1), ("b", 1)), schema=SPEC.schema)
    new = pa.Table.from_pylist(rows(("b", 9), ("c", 9)), schema=SPEC.schema)
    merged = merge(old, new, ("id",))
    got = {r["id"]: r["score"] for r in merged.to_pylist()}
    assert got == {"a": 1, "b": 9, "c": 9}
    assert merge(None, new, ("id",)) is new


def test_write_partition_writes_merges_and_catalogs(tmp_path):
    first = write_partition(tmp_path, SPEC, "demo/dt=x", rows(("a", 1), ("b", 2)), NOW)
    assert isinstance(first, PartitionResult)
    assert first.key == f"demo/dt=x/{PART_FILE}" and first.rows == 2
    second = write_partition(tmp_path, SPEC, "demo/dt=x", rows(("b", 5), ("c", 6)), NOW)
    assert second.rows == 3
    table = pq.read_table(tmp_path / second.key)
    assert {r["id"]: r["score"] for r in table.to_pylist()} == {"a": 1, "b": 5, "c": 6}
    entry = manifest.load(tmp_path, SPEC.name)["partitions"][second.key]
    assert entry["rows"] == 3
    assert entry["min_ts"] == NOW.isoformat()


def test_duplicate_keys_in_one_batch_are_refused(tmp_path):
    with pytest.raises(VerifyError):
        write_partition(tmp_path, SPEC, "demo/dt=x", rows(("a", 1), ("a", 2)), NOW)
    assert not (tmp_path / "demo/dt=x" / PART_FILE).exists()


def test_failed_read_back_leaves_the_old_file(tmp_path, monkeypatch):
    write_partition(tmp_path, SPEC, "demo/dt=x", rows(("a", 1)), NOW)
    real_read = writer.pq.read_table

    def corrupt(path, *a, **kw):
        table = real_read(path, *a, **kw)
        return table.slice(0, 0) if str(path).endswith(".tmp") else table

    monkeypatch.setattr(writer.pq, "read_table", corrupt)
    with pytest.raises(VerifyError):
        write_partition(tmp_path, SPEC, "demo/dt=x", rows(("b", 2)), NOW)
    monkeypatch.undo()
    assert pq.read_table(tmp_path / "demo/dt=x" / PART_FILE).num_rows == 1
    assert not (tmp_path / "demo/dt=x" / (PART_FILE + ".tmp")).exists()


def test_older_file_without_a_new_column_still_merges(tmp_path):
    narrow = pa.schema([("id", pa.string()), ("created_at", TS)])
    path = tmp_path / "demo/dt=x" / PART_FILE
    path.parent.mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist([{"id": "old", "created_at": NOW}], schema=narrow), path)
    result = write_partition(tmp_path, SPEC, "demo/dt=x", rows(("new", 1)), NOW)
    table = pq.read_table(path).to_pylist()
    assert result.rows == 2
    assert {r["id"]: r["score"] for r in table} == {"old": None, "new": 1}


def test_result_and_error_types():
    result = writer.PartitionResult("k", 1, NOW, NOW)
    assert result.rows == 1
    assert issubclass(writer.VerifyError, RuntimeError)
