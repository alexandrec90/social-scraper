import json
from datetime import timedelta

import pyarrow as pa
import pytest
from conftest import LAKE_BUCKET, NOW, bucket_keys

from social_scraper.archive import manifest
from social_scraper.archive.manifest import (
    REQUIRED_KEYS,
    DatasetSpec,
    ManifestError,
    catalog_key,
    load,
    partition_entry,
    validate,
    write,
)
from social_scraper.archive.store import LocalStore, S3Store

SPEC = DatasetSpec(
    name="social_scraper_demo",
    ts_column="created_at",
    key_columns=("id",),
    schema=pa.schema([("id", pa.string()), ("created_at", pa.timestamp("us", tz="UTC"))]),
    partition_of=lambda row: "demo",
)


def test_schema_strings_use_arrow_type_names():
    assert SPEC.schema_strings() == {"id": "string", "created_at": "timestamp[us, tz=UTC]"}


def test_catalog_key_is_data_lakes_manifest_key():
    # data-lake's `manifest_key`: f"{CATALOG_PREFIX}{dataset}.json" with "_catalog/".
    assert catalog_key("d") == "_catalog/d.json"


def test_partition_entry_is_iso():
    e = partition_entry(3, NOW, NOW + timedelta(hours=1), NOW)
    assert e == {
        "rows": 3,
        "min_ts": NOW.isoformat(),
        "max_ts": (NOW + timedelta(hours=1)).isoformat(),
        "updated_at": NOW.isoformat(),
    }


def test_write_produces_what_data_lake_reads(tmp_path):
    store = LocalStore(tmp_path)
    parts = {"demo/part.parquet": partition_entry(1, NOW, NOW, NOW)}
    written = write(store, SPEC, parts, NOW)
    path = tmp_path / "_catalog" / f"{SPEC.name}.json"
    on_disk = json.loads(path.read_text())
    assert on_disk == written == load(store, SPEC.name)
    assert REQUIRED_KEYS <= on_disk.keys()
    # The key data-lake's DatasetManifest.from_dict requires (sports_betting once wrote
    # `timestamp_column` and the lake raised KeyError on it).
    assert on_disk["ts_column"] == "created_at"
    assert on_disk["schema_version"] == manifest.SCHEMA_VERSION == 1
    assert on_disk["key_columns"] == ["id"]
    assert [p.name for p in path.parent.iterdir()] == [path.name]  # no .tmp left


def test_write_and_load_through_the_bucket(s3_client):
    bucket = S3Store(s3_client, LAKE_BUCKET)
    written = write(bucket, SPEC, {"demo/part.parquet": partition_entry(1, NOW, NOW, NOW)}, NOW)
    assert bucket_keys(s3_client) == {f"_catalog/{SPEC.name}.json"}
    assert load(bucket, SPEC.name) == written


def test_load_missing_is_none(tmp_path):
    assert load(LocalStore(tmp_path), "nope") is None


def test_load_damaged_manifest_is_an_error_not_absence(tmp_path):
    store = LocalStore(tmp_path)
    store.put_bytes(catalog_key("d"), b"{half a manifest")
    with pytest.raises(ManifestError, match="not JSON"):
        load(store, "d")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m.pop("ts_column"),
        lambda m: m.update(dataset=""),
        lambda m: m.update(key_columns=[]),
        lambda m: m["partitions"]["p"].pop("rows"),
        lambda m: m["partitions"]["p"].update(min_ts="yesterday"),
    ],
)
def test_validate_rejects_what_the_lake_would_reject(mutate):
    m = {
        "dataset": "d",
        "schema_version": 1,
        "ts_column": "t",
        "key_columns": ["k"],
        "schema": {},
        "updated_at": NOW.isoformat(),
        "partitions": {"p": partition_entry(1, NOW, NOW, NOW)},
    }
    validate(m)
    mutate(m)
    with pytest.raises(ManifestError):
        validate(m)


def test_manifest_error_is_a_value_error():
    assert issubclass(manifest.ManifestError, ValueError)
