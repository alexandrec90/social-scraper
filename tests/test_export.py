from datetime import timedelta

import pyarrow.parquet as pq
from conftest import NOW, Clock
from sqlalchemy import select

from social_scraper import export as export_mod
from social_scraper.archive import datasets, manifest
from social_scraper.db import repository, schema
from social_scraper.export import (
    DatasetExport,
    ExportResult,
    export_all,
    export_dataset,
    fetch_batch,
    lake_rows,
)
from social_scraper.models import AuthorInfo, Engagement, Post
from social_scraper.pipeline import Ingestor

AWS_KEY = "AKIAABCDEFGHIJKLMNOP"  # pragma: allowlist secret - fake key the filter must drop


def post(external_id, body, created=NOW - timedelta(hours=2)):
    return Post(
        platform="reddit",
        external_id=external_id,
        kind="submission",
        channel="stocks",
        created_at=created,
        author=AuthorInfo(name="someone"),
        title="$NVDA",
        body=body,
        engagement=Engagement(score=3),
    )


def seed(engine, universe, clock, *posts):
    Ingestor(engine, universe, clock).ingest(posts)


def read_all(root, spec):
    files = sorted((root / datasets.PREFIX).rglob("part.parquet"))
    rows = []
    for f in files:
        if f"/{spec.name.removeprefix('social_scraper_')}/" in f.as_posix():
            rows.extend(pq.read_table(f).to_pylist())
    return rows


def test_fetch_batch_never_splits_a_timestamp(engine):
    with engine.begin() as conn:
        for i in range(3):
            repository.set_state(conn, f"a{i}", "v", NOW)
        repository.set_state(conn, "b", "v", NOW + timedelta(seconds=1))
        repository.set_state(conn, "c", "v", NOW + timedelta(seconds=2))
    with engine.connect() as conn:
        # A full page ending mid-timestamp holds that timestamp back.
        page = fetch_batch(conn, schema.crawl_state, "updated_at", None, 4)
        assert [r["key"] for r in page] == ["a0", "a1", "a2"]
        # A page that is one timestamp returns all of it, whatever the limit.
        assert len(fetch_batch(conn, schema.crawl_state, "updated_at", None, 2)) == 3
        # A short page is returned as is.
        assert len(fetch_batch(conn, schema.crawl_state, "updated_at", NOW, 10)) == 2


def test_lake_rows_drops_posts_carrying_a_secret():
    """The negative test `.claude/rules/data-lake.md` requires: never uploaded."""
    rows = [
        {"platform": "reddit", "external_id": "ok", "title": "t", "body": "fine"},
        {"platform": "reddit", "external_id": "bad", "title": "t", "body": f"key {AWS_KEY}"},
    ]
    out, dropped = lake_rows(datasets.POSTS, rows)
    assert [r["external_id"] for r in out] == ["ok"] and dropped == 1
    # Mentions carry no text, so nothing is scanned or dropped there.
    assert lake_rows(datasets.MENTIONS, [{"body": AWS_KEY}])[1] == 0


def test_export_writes_every_dataset_and_never_the_secret(engine, universe, tmp_path):
    clock = Clock()
    seed(engine, universe, clock, post("a", "calls"), post("leak", f"here is {AWS_KEY}"))
    result = export_all(engine, tmp_path, NOW)
    assert isinstance(result, ExportResult) and result.ok
    by_name = {d.dataset: d for d in result.datasets}
    assert by_name["social_scraper_posts"].rows == 1
    assert by_name["social_scraper_posts"].dropped_secrets == 1
    assert by_name["social_scraper_mentions"].rows == 2
    assert by_name["social_scraper_symbols"].rows == 1
    for f in tmp_path.rglob("*.parquet"):
        assert AWS_KEY.encode() not in f.read_bytes()
    for spec in datasets.ALL:
        assert manifest.load(tmp_path, spec.name) is not None


def test_export_is_incremental_and_merges_updates(engine, universe, tmp_path):
    clock = Clock()
    seed(engine, universe, clock, post("a", "calls"))
    export_dataset(engine, tmp_path, datasets.POSTS, NOW)
    assert export_dataset(engine, tmp_path, datasets.POSTS, NOW).rows == 0
    clock.now = NOW + timedelta(hours=1)
    seed(engine, universe, clock, post("a", "calls edited"), post("b", "puts"))
    report = export_dataset(engine, tmp_path, datasets.POSTS, NOW)
    assert report.rows == 2
    stored = {r["external_id"]: r["body"] for r in read_all(tmp_path, datasets.POSTS)}
    assert stored == {"a": "calls edited", "b": "puts"}


def test_export_batches_advance_the_watermark(engine, universe, tmp_path):
    clock = Clock()
    for i in range(3):
        clock.now = NOW + timedelta(minutes=i)
        seed(engine, universe, clock, post(f"p{i}", "x"))
    report = export_dataset(engine, tmp_path, datasets.POSTS, NOW, batch=2)
    assert report.rows == 3
    with engine.connect() as conn:
        assert repository.get_watermark(conn, datasets.POSTS.name) == NOW + timedelta(minutes=2)


def test_unwritable_lake_degrades_and_keeps_the_watermark(engine, universe, tmp_path, monkeypatch):
    seed(engine, universe, Clock(), post("a", "calls"))

    def broken(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(export_mod, "write_partition", broken)
    result = export_all(engine, tmp_path, NOW)
    assert not result.ok
    assert all("disk full" in d.error for d in result.datasets if d.error)
    with engine.connect() as conn:
        assert repository.get_watermark(conn, datasets.POSTS.name) is None
        assert conn.execute(select(schema.posts)).first() is not None


def test_dataset_export_defaults():
    assert DatasetExport("d") == DatasetExport("d", 0, 0, 0, None)


def test_export_result_ok_reflects_every_dataset():
    assert export_mod.ExportResult().ok
    assert not export_mod.ExportResult([DatasetExport("a"), DatasetExport("b", error="x")]).ok
