import io
from datetime import UTC, datetime, timedelta

import pyarrow.parquet as pq
from botocore.exceptions import ClientError
from conftest import LAKE_BUCKET, NOW, Clock, IsolatedSettings, bucket_keys
from sqlalchemy import select

from social_scraper import export as export_mod
from social_scraper.archive import datasets, manifest
from social_scraper.archive import store as store_mod
from social_scraper.archive.lock import TIMEOUT_S
from social_scraper.archive.store import LocalStore, S3Store
from social_scraper.archive.writer import from_parquet, to_parquet
from social_scraper.db import repository, schema
from social_scraper.export import (
    NO_DEADLINE,
    DatasetExport,
    Deadline,
    ExportResult,
    export_all,
    export_dataset,
    export_to_lake,
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
    result = export_all(engine, [LocalStore(tmp_path)], NOW)
    assert isinstance(result, ExportResult) and result.ok
    by_name = {d.dataset: d for d in result.datasets}
    assert by_name["social_scraper_posts"].rows == 1
    assert by_name["social_scraper_posts"].dropped_secrets == 1
    assert by_name["social_scraper_mentions"].rows == 2
    assert by_name["social_scraper_symbols"].rows == 1
    for f in tmp_path.rglob("*.parquet"):
        assert AWS_KEY.encode() not in f.read_bytes()
    for spec in datasets.STOCK:
        assert manifest.load(LocalStore(tmp_path), spec.name) is not None
    # No sports rows, so no sports dataset: an empty table writes nothing.
    for spec in datasets.SPORTS:
        assert manifest.load(LocalStore(tmp_path), spec.name) is None
        assert by_name[spec.name].rows == 0


def test_sports_rows_export_to_their_own_datasets_and_never_the_secret(engine, tmp_path):
    from social_scraper.sports import registry
    from social_scraper.sports.ingest import SportsIngestor
    from social_scraper.sports.matcher import Matcher
    from social_scraper.sports.wikidata import TEAM, EntityRecord

    with engine.begin() as conn:
        registry.replace_league(conn, "nhl", [EntityRecord("Q1", TEAM, "Montreal Canadiens")], NOW)
        matcher = Matcher(registry.load_entities(conn), {"nhl": frozenset({"nhl"})})
    posts = [post("s", "Montreal Canadiens win"), post("leak", f"Habs {AWS_KEY}")]
    posts = [Post(**{**p.__dict__, "channel": "nhl", "title": None}) for p in posts]
    SportsIngestor(engine, matcher, Clock()).ingest(posts)
    result = export_all(engine, [LocalStore(tmp_path)], NOW)
    by_name = {d.dataset: d for d in result.datasets}
    assert result.ok
    assert by_name["social_scraper_sports_posts"].rows == 1
    assert by_name["social_scraper_sports_posts"].dropped_secrets == 1
    assert by_name["social_scraper_sports_mentions"].rows == 1
    assert by_name["social_scraper_sports_entities"].rows == 1
    assert by_name["social_scraper_sports_entity_leagues"].rows == 1
    assert by_name["social_scraper_posts"].rows == 0  # nothing leaks into the stock datasets
    for f in tmp_path.rglob("*.parquet"):
        assert AWS_KEY.encode() not in f.read_bytes()
    for spec in datasets.SPORTS:
        assert manifest.load(LocalStore(tmp_path), spec.name) is not None


def test_lake_rows_scans_sports_posts_for_secrets():
    rows = [{"platform": "x", "external_id": "bad", "body": f"key {AWS_KEY}"}]
    assert lake_rows(datasets.SPORTS_POSTS, rows)[1] == 1
    assert lake_rows(datasets.SPORTS_MENTIONS, rows)[1] == 0


def test_export_is_incremental_and_merges_updates(engine, universe, tmp_path):
    clock = Clock()
    stores = [LocalStore(tmp_path)]
    seed(engine, universe, clock, post("a", "calls"))
    export_dataset(engine, stores, datasets.POSTS, NOW)
    assert export_dataset(engine, stores, datasets.POSTS, NOW).rows == 0
    clock.now = NOW + timedelta(hours=1)
    seed(engine, universe, clock, post("a", "calls edited"), post("b", "puts"))
    report = export_dataset(engine, stores, datasets.POSTS, NOW)
    assert report.rows == 2
    stored = {r["external_id"]: r["body"] for r in read_all(tmp_path, datasets.POSTS)}
    assert stored == {"a": "calls edited", "b": "puts"}


def test_export_batches_advance_the_watermark(engine, universe, tmp_path):
    clock = Clock()
    for i in range(3):
        clock.now = NOW + timedelta(minutes=i)
        seed(engine, universe, clock, post(f"p{i}", "x"))
    report = export_dataset(engine, [LocalStore(tmp_path)], datasets.POSTS, NOW, batch=2)
    assert report.rows == 3
    with engine.connect() as conn:
        assert repository.get_watermark(conn, datasets.POSTS.name) == NOW + timedelta(minutes=2)


def test_unwritable_lake_degrades_and_keeps_the_watermark(engine, universe, tmp_path, monkeypatch):
    seed(engine, universe, Clock(), post("a", "calls"))

    def broken(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(export_mod, "write_partition", broken)
    result = export_all(engine, [LocalStore(tmp_path)], NOW)
    assert not result.ok
    assert all("disk full" in d.error for d in result.datasets if d.error)
    with engine.connect() as conn:
        assert repository.get_watermark(conn, datasets.POSTS.name) is None
        assert conn.execute(select(schema.posts)).first() is not None


# --- a deadline: what is left waits for the next run ------------------------------------


def test_past_its_deadline_the_export_defers_every_dataset_and_keeps_the_watermarks(
    engine, universe, tmp_path
):
    seed(engine, universe, Clock(), post("a", "calls"))
    result = export_all(engine, [LocalStore(tmp_path)], NOW, Deadline(NOW, lambda: NOW))
    assert result.ok, "a deferral is not an error"
    by_name = {d.dataset: d for d in result.datasets}
    assert "out of export time" in by_name["social_scraper_posts"].deferred
    assert "social_scraper_posts" in result.deferred
    assert not list(tmp_path.rglob("*.parquet"))
    with engine.connect() as conn:
        assert repository.get_watermark(conn, datasets.POSTS.name) is None
    # The next run, with time to spare, exports the same rows.
    in_time = Deadline(NOW + timedelta(hours=1), lambda: NOW)
    later = export_all(engine, [LocalStore(tmp_path)], NOW, in_time)
    assert {d.dataset: d.rows for d in later.datasets}["social_scraper_posts"] == 1
    assert not later.deferred


def test_a_partition_lock_held_past_the_deadline_defers_and_leaves_the_holders_lock(
    engine, universe, tmp_path
):
    """2026-10-07: a `backfill export-full` held the lake's locks while the 20:30 scrape
    exported; each wait could take the lock's whole ten minutes, and the export ran 35
    minutes past its crawl. With a deadline, the wait ends there."""
    seed(engine, universe, Clock(), post("a", "calls"))
    store = LocalStore(tmp_path)
    with engine.connect() as conn:
        row = repository.rows_since(conn, schema.posts, "updated_at", None, 1)[0]
    (lake_row,), _ = lake_rows(datasets.POSTS, [row])
    lock = store.lock_path(f"{datasets.POSTS.partition_of(lake_row)}/part.parquet")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("a backfill")
    until = datetime.now(UTC) + timedelta(seconds=0.3)
    report = export_dataset(engine, [store], datasets.POSTS, NOW, deadline=Deadline(until))
    assert report.deferred and "held by another writer" in report.deferred
    assert report.rows == 0
    assert lock.read_text() == "a backfill"
    with engine.connect() as conn:
        assert repository.get_watermark(conn, datasets.POSTS.name) is None


def test_lock_wait_is_the_time_left_capped_at_the_locks_own_timeout():
    def at(seconds):
        return Deadline(NOW + timedelta(seconds=seconds), lambda: NOW)

    assert NO_DEADLINE.lock_wait_s() == TIMEOUT_S and not NO_DEADLINE.passed()
    assert at(30).lock_wait_s() == 30 and not at(30).passed()
    assert at(3600).lock_wait_s() == TIMEOUT_S
    assert at(-5).lock_wait_s() == 0 and at(-5).passed()
    assert at(0).passed(), "the deadline itself is past it"
    assert at(0).deferral("held") == f"out of export time at {NOW:%H:%M:%S}Z (held)"


# --- export_to_lake: the stores the settings configure ---------------------------------


def s3_settings(tmp_path, **over):
    fields = {
        "archive_root": tmp_path / "lake",
        "archive_backend": "s3",
        "archive_s3_bucket": LAKE_BUCKET,
        **over,
    }
    return IsolatedSettings(**fields)


def local_keys(root):
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


class CorruptReadBack:
    """An S3 client whose staged objects read back empty -- a write that did not land."""

    def __init__(self, client):
        self._client = client

    def __getattr__(self, name):
        return getattr(self._client, name)

    def get_object(self, **kw):
        response = self._client.get_object(**kw)
        if kw["Key"].endswith(".tmp"):
            empty = from_parquet(response["Body"].read()).slice(0, 0)
            return {"Body": io.BytesIO(to_parquet(empty))}
        return response


def test_settings_unset_export_to_the_local_tree_only(engine, universe, tmp_path, monkeypatch):
    def never(settings):
        raise AssertionError("no S3 client without ARCHIVE_BACKEND=s3")

    monkeypatch.setattr(store_mod, "make_s3_client", never)
    seed(engine, universe, Clock(), post("a", "calls"))
    settings = IsolatedSettings(archive_root=tmp_path / "lake")
    assert export_to_lake(engine, settings, NOW).ok
    for spec in datasets.STOCK:
        assert manifest.load(LocalStore(settings.archive_root), spec.name) is not None


def test_bucket_gets_the_same_keys_as_the_local_tree(engine, universe, tmp_path, s3_client):
    seed(engine, universe, Clock(), post("a", "calls"), post("leak", f"here is {AWS_KEY}"))
    settings = s3_settings(tmp_path)
    assert export_to_lake(engine, settings, NOW, client=s3_client).ok
    keys = bucket_keys(s3_client)
    # Root-relative and identical to the local layout, so data-lake's load_catalog reads
    # the bucket unchanged; no staged `.tmp` object is left behind in either.
    assert keys == local_keys(settings.archive_root)
    assert {f"_catalog/{spec.name}.json" for spec in datasets.STOCK} <= keys
    assert "social_scraper/posts/platform=reddit/dt=2026-09-28/part.parquet" in keys
    assert not any(k.endswith(".tmp") for k in keys)
    bucket = S3Store(s3_client, LAKE_BUCKET)
    for spec in datasets.STOCK:
        catalog = manifest.load(bucket, spec.name)
        manifest.validate(catalog)
        assert set(catalog["partitions"]) <= keys
    posts = from_parquet(
        bucket.get_bytes("social_scraper/posts/platform=reddit/dt=2026-09-28/part.parquet")
    )
    assert posts.column("external_id").to_pylist() == ["a"]  # the secret never uploaded


def test_bucket_keys_sit_under_the_prefix(engine, universe, tmp_path, s3_client):
    seed(engine, universe, Clock(), post("a", "calls"))
    settings = s3_settings(tmp_path, archive_s3_prefix="lake")
    assert export_to_lake(engine, settings, NOW, client=s3_client).ok
    expected = {f"lake/{k}" for k in local_keys(settings.archive_root)}
    assert bucket_keys(s3_client) == expected
    catalog = manifest.load(S3Store(s3_client, LAKE_BUCKET, "lake"), "social_scraper_posts")
    # Partition keys stay root-relative: data-lake's store adds its prefix itself.
    assert all(not k.startswith("lake/") for k in catalog["partitions"])


def test_bucket_partition_is_merged_with_new_rows_winning(engine, universe, tmp_path, s3_client):
    clock = Clock()
    bucket = S3Store(s3_client, LAKE_BUCKET)
    # Archived to the bucket alone: the local tree never sees these rows, so the merge
    # below can only have read them back from the bucket.
    seed(engine, universe, clock, post("old", "kept"), post("a", "stale"))
    assert export_all(engine, [bucket], NOW).ok
    clock.now = NOW + timedelta(hours=1)
    seed(engine, universe, clock, post("a", "fresh"))
    settings = s3_settings(tmp_path)
    assert export_to_lake(engine, settings, NOW, client=s3_client).ok
    key = "social_scraper/posts/platform=reddit/dt=2026-09-28/part.parquet"
    in_bucket = {
        r["external_id"]: r["body"] for r in from_parquet(bucket.get_bytes(key)).to_pylist()
    }
    assert in_bucket == {"old": "kept", "a": "fresh"}
    local = {r["external_id"]: r["body"] for r in read_all(settings.archive_root, datasets.POSTS)}
    assert local == {"a": "fresh"}
    assert manifest.load(bucket, "social_scraper_posts")["partitions"][key]["rows"] == 2


def test_bucket_read_back_failure_fails_the_export(engine, universe, tmp_path, s3_client):
    seed(engine, universe, Clock(), post("a", "calls"))
    settings = s3_settings(tmp_path)
    result = export_to_lake(engine, settings, NOW, client=CorruptReadBack(s3_client))
    assert not result.ok
    errors = [d.error for d in result.datasets if d.error]
    assert errors and all("VerifyError" in e and "read-back" in e for e in errors)
    # Nothing replaced, nothing catalogued, nothing staged left; the rows wait for a retry.
    assert bucket_keys(s3_client) == set()
    with engine.connect() as conn:
        assert repository.get_watermark(conn, datasets.POSTS.name) is None


def test_unreachable_bucket_fails_the_export_and_keeps_the_watermark(
    engine, universe, tmp_path, s3_client
):
    seed(engine, universe, Clock(), post("a", "calls"))
    settings = s3_settings(tmp_path, archive_s3_bucket="no-such-bucket")
    result = export_to_lake(engine, settings, NOW, client=s3_client)
    assert not result.ok
    assert all("StoreError" in d.error for d in result.datasets if d.error)
    with engine.connect() as conn:
        assert repository.get_watermark(conn, datasets.POSTS.name) is None
    # The next run, with the bucket back, sends the same rows.
    s3_client.create_bucket(Bucket="no-such-bucket")
    assert export_to_lake(engine, settings, NOW, client=s3_client).ok
    assert bucket_keys(s3_client, "no-such-bucket")


class RefusesPuts:
    """An S3 client whose server fails every put with a 500, as the storage pool's gateway
    does while one of its two PCs is off; counts the puts it was sent."""

    def __init__(self, client):
        self._client = client
        self.puts = 0

    def __getattr__(self, name):
        return getattr(self._client, name)

    def put_object(self, **kw):
        self.puts += 1
        error = {"Code": "InternalError", "Message": "We encountered an internal error"}
        raise ClientError(
            {"Error": error, "ResponseMetadata": {"HTTPStatusCode": 500}}, "PutObject"
        )


def test_a_lake_refusing_writes_defers_every_dataset_after_one_try(
    engine, universe, tmp_path, s3_client
):
    """5354e2f6: the 23:00 scrape on 2026-10-08 spent three minutes of botocore retries on
    each of four datasets against a pool with a PC off, and failed. The first refusal now
    defers that dataset and every one after it, untried, and the run is ok."""
    seed(engine, universe, Clock(), post("a", "calls"))
    settings = s3_settings(tmp_path)
    down = RefusesPuts(s3_client)
    result = export_to_lake(engine, settings, NOW, client=down)
    assert result.ok, "a lake that is down is a deferral, not an error"
    assert result.lake_down and result.lake_down.startswith(export_mod.LAKE_DOWN)
    assert "InternalError" in result.lake_down
    assert result.deferred == [spec.name for spec in datasets.ALL]
    assert down.puts == 1, "no dataset after the first refusal is tried"
    with engine.connect() as conn:
        assert repository.get_watermark(conn, datasets.POSTS.name) is None
    # The pool back, the next run sends the same rows.
    later = export_to_lake(engine, settings, NOW, client=s3_client)
    assert later.ok and later.lake_down is None and not later.deferred
    assert "social_scraper/posts/platform=reddit/dt=2026-09-28/part.parquet" in bucket_keys(
        s3_client
    )


def test_unconfigured_bucket_fails_every_dataset_without_raising(engine, tmp_path):
    settings = IsolatedSettings(archive_root=tmp_path / "lake", archive_backend="s3")
    result = export_to_lake(engine, settings, NOW)
    assert not result.ok
    assert [d.dataset for d in result.datasets] == [s.name for s in datasets.ALL]
    assert all("ARCHIVE_S3_BUCKET" in d.error for d in result.datasets)


def test_dataset_export_defaults():
    assert DatasetExport("d") == DatasetExport("d", 0, 0, 0, None)


def test_export_result_ok_reflects_every_dataset():
    assert export_mod.ExportResult().ok
    assert not export_mod.ExportResult([DatasetExport("a"), DatasetExport("b", error="x")]).ok
