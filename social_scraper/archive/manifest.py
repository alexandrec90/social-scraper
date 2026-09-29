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
from pathlib import Path
from typing import Any

import pyarrow as pa

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


def catalog_path(root: Path, dataset: str) -> Path:
    return root / CATALOG_DIR / f"{dataset}.json"


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


def load(root: Path, dataset: str) -> dict[str, Any] | None:
    path = catalog_path(root, dataset)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write(
    root: Path, spec: DatasetSpec, partitions: dict[str, dict], updated_at: datetime
) -> dict[str, Any]:
    """Validate and atomically write the manifest. Returns what was written."""
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
    path = catalog_path(root, spec.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    return manifest
