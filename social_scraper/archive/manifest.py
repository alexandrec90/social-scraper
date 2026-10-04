"""`_catalog/<dataset>.json` -- the reuse contract with data-lake.

data-lake's `archive.catalog.DatasetManifest.from_dict` is the reader, and this module
writes exactly what it requires (schema version 1):

    {"dataset", "schema_version", "ts_column", "key_columns", "schema", "updated_at",
     "partitions": {"<root-relative path>": {"rows", "min_ts", "max_ts", "updated_at"}}}

Timestamps are ISO-8601 UTC. Extra top-level keys are ignored by the reader, so
`producer` is safe to add. Note the name is `ts_column`: sports_betting once wrote
`timestamp_column` and the lake raised `KeyError` on it.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pyarrow as pa

from social_scraper.archive.store import ObjectStore

CATALOG_DIR = "_catalog"
SCHEMA_VERSION = 1
PRODUCER = "social-scraper"
REQUIRED_KEYS = frozenset(
    {"dataset", "schema_version", "ts_column", "key_columns", "schema", "partitions", "updated_at"}
)
REQUIRED_PARTITION_KEYS = frozenset({"rows", "min_ts", "max_ts", "updated_at"})


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class DatasetSpec:
    """One dataset: its catalog identity, its Arrow schema, and where a row lands.

    `partition_of(row)` returns the root-relative directory of the row's partition;
    the file inside it is always `part.parquet`, one per partition.
    """

    name: str
    ts_column: str
    key_columns: tuple[str, ...]
    schema: pa.Schema
    partition_of: Callable[[dict[str, Any]], str]

    def schema_strings(self) -> dict[str, str]:
        return {f.name: str(f.type) for f in self.schema}


def catalog_key(dataset: str) -> str:
    """The manifest's root-relative key -- data-lake's `manifest_key`."""
    return f"{CATALOG_DIR}/{dataset}.json"


def partition_entry(rows: int, min_ts: datetime, max_ts: datetime, updated_at: datetime) -> dict:
    return {
        "rows": rows,
        "min_ts": min_ts.isoformat(),
        "max_ts": max_ts.isoformat(),
        "updated_at": updated_at.isoformat(),
    }


def validate(manifest: dict[str, Any]) -> None:
    """Raise `ManifestError` unless data-lake's reader would accept `manifest`."""
    missing = REQUIRED_KEYS - manifest.keys()
    if missing:
        raise ManifestError(f"manifest missing {sorted(missing)}")
    if not isinstance(manifest["dataset"], str) or not manifest["dataset"]:
        raise ManifestError("dataset must be a non-empty string")
    if not manifest["key_columns"]:
        raise ManifestError("key_columns must not be empty")
    for key, entry in manifest["partitions"].items():
        gap = REQUIRED_PARTITION_KEYS - entry.keys()
        if gap:
            raise ManifestError(f"partition {key} missing {sorted(gap)}")
        for field in ("min_ts", "max_ts", "updated_at"):
            try:
                datetime.fromisoformat(entry[field])
            except (TypeError, ValueError) as exc:
                raise ManifestError(f"partition {key}.{field} is not ISO-8601") from exc


def load(store: ObjectStore, dataset: str) -> dict[str, Any] | None:
    """The stored manifest, or `None` if there is none.

    A manifest that exists but is not JSON raises `ManifestError`, never `None`: the
    bucket is shared, and reporting damage as absence would let the next write replace
    the record of every partition with a record of one.
    """
    try:
        raw = store.get_bytes(catalog_key(dataset))
    except KeyError:
        return None
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:  # JSONDecodeError is a ValueError
        raise ManifestError(f"{store.name}: {catalog_key(dataset)} is not JSON: {exc}") from exc


def write(
    store: ObjectStore, spec: DatasetSpec, partitions: dict[str, dict], updated_at: datetime
) -> dict[str, Any]:
    """Validate and write the manifest (atomically: a whole-object put). Returns it."""
    manifest = {
        "dataset": spec.name,
        "schema_version": SCHEMA_VERSION,
        "ts_column": spec.ts_column,
        "key_columns": list(spec.key_columns),
        "schema": spec.schema_strings(),
        "partitions": dict(sorted(partitions.items())),
        "updated_at": updated_at.isoformat(),
        "producer": PRODUCER,
    }
    validate(manifest)
    body = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
    store.put_bytes(catalog_key(spec.name), body)
    return manifest
