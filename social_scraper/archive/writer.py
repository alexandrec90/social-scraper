"""Write one Hive-style partition into one store: merge, verify, replace, catalog.

The same sequence sports_betting's `EventArchive` uses, so the pooled lake behaves one
way whoever wrote to it, and the same sequence whichever store it is written to (the
local tree or the bucket, `archive/store.py`):

1. Read the partition's existing object **from that store**, if any, and merge the new
   rows in on the natural key -- **the new row wins**, because a re-observed post
   carries fresher engagement than the copy already archived.
2. Write the result to `part.parquet.tmp`.
3. **Verify**: read the staged object back and check its row count and exact key set.
4. Replace `part.parquet` with it, then update `_catalog/<dataset>.json` in the same
   call -- never as a later backfill (`.claude/rules/data-lake.md`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from social_scraper.archive import manifest
from social_scraper.archive.manifest import DatasetSpec
from social_scraper.archive.store import ObjectStore
from social_scraper.models import as_utc

PART_FILE = "part.parquet"
STAGED_SUFFIX = ".tmp"


class VerifyError(RuntimeError):
    """The file read back is not the file we meant to write. Nothing was replaced."""


@dataclass(frozen=True)
class PartitionResult:
    key: str
    rows: int
    min_ts: datetime
    max_ts: datetime


def _keys(table: pa.Table, columns: tuple[str, ...]) -> list[tuple]:
    cols = [table.column(c).to_pylist() for c in columns]
    return list(zip(*cols, strict=True))


def merge(existing: pa.Table | None, new: pa.Table, key_columns: tuple[str, ...]) -> pa.Table:
    """`existing` minus rows whose key is in `new`, plus `new`. New rows win."""
    if existing is None or existing.num_rows == 0:
        return new
    incoming = set(_keys(new, key_columns))
    keep = pa.array([k not in incoming for k in _keys(existing, key_columns)], type=pa.bool_())
    return pa.concat_tables([existing.filter(keep), new])


def to_parquet(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink, compression="zstd")
    return sink.getvalue().to_pybytes()


def from_parquet(data: bytes) -> pa.Table:
    return pq.read_table(pa.BufferReader(data))


def _read_existing(store: ObjectStore, key: str, schema: pa.Schema) -> pa.Table | None:
    try:
        data = store.get_bytes(key)
    except KeyError:
        return None
    table = from_parquet(data)
    # Columns added to the spec since the file was written arrive as nulls.
    for field in schema:
        if field.name not in table.column_names:
            table = table.append_column(field, pa.nulls(table.num_rows, type=field.type))
    return table.select(schema.names).cast(schema)


def write_partition(
    store: ObjectStore,
    spec: DatasetSpec,
    partition_dir: str,
    rows: list[dict[str, Any]],
    now: datetime,
) -> PartitionResult:
    """Merge `rows` into one partition of `store` and record it in that store's catalog."""
    rel = f"{partition_dir}/{PART_FILE}"
    merged = merge(
        _read_existing(store, rel, spec.schema),
        pa.Table.from_pylist(rows, schema=spec.schema),
        spec.key_columns,
    )
    expected = set(_keys(merged, spec.key_columns))
    if len(expected) != merged.num_rows:
        raise VerifyError(f"{store.name}: {rel}: duplicate keys in the merged partition")
    staged = rel + STAGED_SUFFIX
    store.put_bytes(staged, to_parquet(merged))
    try:
        check = from_parquet(store.get_bytes(staged))
    except KeyError:
        raise VerifyError(f"{store.name}: {staged}: gone on read-back") from None
    if check.num_rows != merged.num_rows or set(_keys(check, spec.key_columns)) != expected:
        store.delete(staged)
        raise VerifyError(f"{store.name}: {rel}: read-back did not match what was written")
    store.promote(staged, rel)
    stamps = [as_utc(t) for t in merged.column(spec.ts_column).to_pylist() if t is not None]
    result = PartitionResult(rel, merged.num_rows, min(stamps), max(stamps))
    _record(store, spec, result, now)
    return result


def _record(store: ObjectStore, spec: DatasetSpec, result: PartitionResult, now: datetime) -> None:
    current = manifest.load(store, spec.name) or {}
    partitions = dict(current.get("partitions") or {})
    partitions[result.key] = manifest.partition_entry(
        result.rows, result.min_ts, result.max_ts, now
    )
    manifest.write(store, spec, partitions, now)
