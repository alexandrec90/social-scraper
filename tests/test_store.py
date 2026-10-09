import pytest
from botocore.exceptions import (
    ClientError,
    EndpointConnectionError,
    NoCredentialsError,
    ReadTimeoutError,
)
from conftest import LAKE_BUCKET, IsolatedSettings, bucket_keys

from social_scraper.archive import store
from social_scraper.archive.store import (
    LakeUnavailable,
    LocalStore,
    ObjectStore,
    S3Store,
    StoreError,
    make_s3_client,
    stores_for,
)


def s3_settings(**over):
    return IsolatedSettings(archive_backend="s3", archive_s3_bucket=LAKE_BUCKET, **over)


# --- LocalStore -----------------------------------------------------------------------


def test_local_store_round_trip(tmp_path):
    s = LocalStore(tmp_path)
    s.put_bytes("a/b/c.json", b"one")
    assert s.get_bytes("a/b/c.json") == b"one"
    assert s.path("a/b/c.json") == tmp_path / "a" / "b" / "c.json"
    assert [p.name for p in (tmp_path / "a" / "b").iterdir()] == ["c.json"]  # no .tmp left
    assert s.name == str(tmp_path)


def test_local_store_missing_is_key_error(tmp_path):
    with pytest.raises(KeyError):
        LocalStore(tmp_path).get_bytes("nope")


def test_local_store_promote_and_delete(tmp_path):
    s = LocalStore(tmp_path)
    s.put_bytes("k", b"old")
    s.put_bytes("k.tmp", b"new")
    s.promote("k.tmp", "k")
    assert s.get_bytes("k") == b"new"
    assert not s.path("k.tmp").exists()
    s.delete("k")
    s.delete("k")  # absent is not an error
    assert not s.path("k").exists()


def _refuse_replace(monkeypatch, times):
    """Make `Path.replace` raise WinError 32's `PermissionError` `times` times, then work."""
    real = store.Path.replace
    calls = []

    def flaky(self, target):
        calls.append(self.name)
        if len(calls) <= times:
            raise PermissionError(13, "being used by another process", str(self))
        return real(self, target)

    monkeypatch.setattr(store.Path, "replace", flaky)
    return calls


def test_replace_with_retry_outlasts_a_target_held_open(tmp_path, monkeypatch):
    # 2026-10-06: a reader holding `_catalog/social_scraper_posts.json` open failed the
    # rename once, which failed the dataset and the whole scheduled scrape.
    source, target = tmp_path / "t.tmp", tmp_path / "t"
    source.write_bytes(b"new")
    target.write_bytes(b"old")
    calls = _refuse_replace(monkeypatch, times=2)
    slept = []
    store.replace_with_retry(source, target, delays=(0.1, 0.2, 0.3), sleep=slept.append)
    assert target.read_bytes() == b"new"
    assert not source.exists()
    assert len(calls) == 3
    assert slept == [0.1, 0.2]


def test_replace_with_retry_gives_up_and_raises(tmp_path, monkeypatch):
    source, target = tmp_path / "t.tmp", tmp_path / "t"
    source.write_bytes(b"new")
    calls = _refuse_replace(monkeypatch, times=99)
    slept = []
    with pytest.raises(PermissionError):
        store.replace_with_retry(source, target, delays=(0.1, 0.2), sleep=slept.append)
    assert len(calls) == 3  # one per pause, then the last word
    assert slept == [0.1, 0.2]
    assert source.read_bytes() == b"new"  # staged bytes kept for the next run


def test_replace_with_retry_does_not_retry_other_errors(tmp_path):
    slept = []
    with pytest.raises(FileNotFoundError):
        store.replace_with_retry(
            tmp_path / "absent", tmp_path / "t", delays=(0.1,), sleep=slept.append
        )
    assert slept == []


def test_replace_with_retry_default_delays_are_read_at_call_time(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "REPLACE_RETRY_DELAYS_S", (0.0,))
    calls = _refuse_replace(monkeypatch, times=99)
    (tmp_path / "s").write_bytes(b"x")
    with pytest.raises(PermissionError):
        store.replace_with_retry(tmp_path / "s", tmp_path / "t")
    assert len(calls) == 2


def test_local_store_writes_and_promotes_through_a_held_target(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "REPLACE_RETRY_DELAYS_S", (0.0, 0.0))
    s = LocalStore(tmp_path)
    _refuse_replace(monkeypatch, times=1)
    s.put_bytes("_catalog/d.json", b"{}")
    assert s.get_bytes("_catalog/d.json") == b"{}"
    assert not s.path("_catalog/d.json.tmp").exists()
    s.put_bytes("k.tmp", b"new")
    _refuse_replace(monkeypatch, times=1)
    s.promote("k.tmp", "k")
    assert s.get_bytes("k") == b"new"


# --- S3Store --------------------------------------------------------------------------


def test_s3_store_round_trip_under_a_prefix(s3_client):
    s = S3Store(s3_client, LAKE_BUCKET, prefix="/lake/")
    s.put_bytes("_catalog/d.json", b"{}")
    assert s.get_bytes("_catalog/d.json") == b"{}"
    # Stored under the prefix, as data-lake's S3ObjectStore._key reads it.
    assert bucket_keys(s3_client) == {"lake/_catalog/d.json"}
    assert s.object_key("x") == "lake/x"
    assert s.name == f"s3://{LAKE_BUCKET}/lake"


def test_s3_store_without_a_prefix_writes_at_the_bucket_root(s3_client):
    s = S3Store(s3_client, LAKE_BUCKET)
    s.put_bytes("_catalog/d.json", b"{}")
    assert bucket_keys(s3_client) == {"_catalog/d.json"}
    assert s.name == f"s3://{LAKE_BUCKET}"


def test_s3_store_missing_object_is_key_error(s3_client):
    with pytest.raises(KeyError):
        S3Store(s3_client, LAKE_BUCKET).get_bytes("nope")


def test_s3_store_promote_replaces_and_removes_the_staged_object(s3_client):
    s = S3Store(s3_client, LAKE_BUCKET)
    s.put_bytes("p/part.parquet", b"old")
    s.put_bytes("p/part.parquet.tmp", b"new")
    s.promote("p/part.parquet.tmp", "p/part.parquet")
    assert s.get_bytes("p/part.parquet") == b"new"
    assert bucket_keys(s3_client) == {"p/part.parquet"}
    s.delete("p/part.parquet")
    s.delete("p/part.parquet")  # absent is not an error
    assert bucket_keys(s3_client) == set()


def test_s3_store_failures_are_store_errors(s3_client):
    s = S3Store(s3_client, "no-such-bucket")
    for call in (
        lambda: s.get_bytes("k"),
        lambda: s.put_bytes("k", b"x"),
        lambda: s.promote("k.tmp", "k"),
        lambda: s.delete("k"),
    ):
        with pytest.raises(StoreError, match="no-such-bucket") as caught:
            call()
        # A 4xx refuses this request; the server is fine, so nothing is deferred for it.
        assert not isinstance(caught.value, LakeUnavailable)
    assert issubclass(StoreError, OSError)  # an export treats it like a full disk


def internal_error() -> ClientError:
    """What the SeaweedFS gateway answers a put once botocore's retries run out, while a
    `001` bucket has no second volume server to place a replica on (2026-10-08)."""
    response = {
        "Error": {"Code": "InternalError", "Message": "We encountered an internal error"},
        "ResponseMetadata": {"HTTPStatusCode": 500},
    }
    return ClientError(response, "PutObject")


class ServerDown:
    """An S3 client whose server fails every request with `error`."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    def __getattr__(self, name):
        def fail(**kw):
            raise self.error

        return fail


@pytest.mark.parametrize(
    "error",
    [
        internal_error(),
        EndpointConnectionError(endpoint_url="http://100.76.121.58:8333"),
        ReadTimeoutError(endpoint_url="http://100.76.121.58:8333"),
    ],
    ids=["5xx", "unreachable", "read-timeout"],
)
def test_a_server_that_fails_or_cannot_be_reached_is_lake_unavailable(error):
    s = S3Store(ServerDown(error), LAKE_BUCKET)
    for call in (
        lambda: s.get_bytes("k"),
        lambda: s.put_bytes("k", b"x"),
        lambda: s.promote("k.tmp", "k"),
        lambda: s.delete("k"),
    ):
        with pytest.raises(LakeUnavailable, match=LAKE_BUCKET):
            call()
    assert issubclass(LakeUnavailable, StoreError)


def test_is_unavailable_reads_the_status_not_the_code():
    assert store.is_unavailable(internal_error())
    slow = ClientError(
        {"Error": {"Code": "SlowDown"}, "ResponseMetadata": {"HTTPStatusCode": 503}}, "PutObject"
    )
    assert store.is_unavailable(slow)
    denied = ClientError(
        {"Error": {"Code": "AccessDenied"}, "ResponseMetadata": {"HTTPStatusCode": 403}},
        "PutObject",
    )
    assert not store.is_unavailable(denied)
    assert not store.is_unavailable(ClientError({"Error": {"Code": "InternalError"}}, "PutObject"))
    assert not store.is_unavailable(NoCredentialsError())


def test_s3_store_needs_a_bucket(s3_client):
    with pytest.raises(StoreError, match="ARCHIVE_S3_BUCKET"):
        S3Store(s3_client, "")


# --- building the stores from settings -------------------------------------------------


def test_make_s3_client_uses_the_r2_settings(aws_env, monkeypatch):
    # No request is made: building a boto3 client is offline.
    monkeypatch.setenv("ARCHIVE_S3_SECRET_ACCESS_KEY", "fake-secret")
    client = make_s3_client(
        s3_settings(
            archive_s3_endpoint_url="https://acct.r2.cloudflarestorage.com",
            archive_s3_region="auto",
            archive_s3_access_key_id="fake-id",
        )
    )
    assert client.meta.endpoint_url == "https://acct.r2.cloudflarestorage.com"
    assert client.meta.region_name == "auto"
    creds = client._request_signer._credentials
    assert (creds.access_key, creds.secret_key) == ("fake-id", "fake-secret")


def test_make_s3_client_rejects_a_malformed_endpoint(aws_env):
    with pytest.raises(StoreError, match="S3 client"):
        make_s3_client(s3_settings(archive_s3_endpoint_url="not a url"))


def test_stores_for_defaults_to_the_local_tree_only(tmp_path, monkeypatch):
    def never(settings):
        raise AssertionError("no S3 client without ARCHIVE_BACKEND=s3")

    monkeypatch.setattr(store, "make_s3_client", never)
    # A bucket name alone does not switch the bucket on: ARCHIVE_BACKEND does.
    stores = stores_for(IsolatedSettings(archive_root=tmp_path, archive_s3_bucket=LAKE_BUCKET))
    assert len(stores) == 1 and isinstance(stores[0], LocalStore)
    assert stores[0].root == tmp_path


def test_stores_for_s3_writes_local_first_then_the_bucket(tmp_path, s3_client, monkeypatch):
    monkeypatch.setattr(store, "make_s3_client", lambda settings: s3_client)
    local, bucket = stores_for(s3_settings(archive_root=tmp_path, archive_s3_prefix="p"))
    assert isinstance(local, LocalStore) and local.root == tmp_path
    assert isinstance(bucket, S3Store)
    assert (bucket.client, bucket.bucket, bucket.prefix) == (s3_client, LAKE_BUCKET, "p")


def test_stores_for_prefers_an_injected_client(tmp_path, s3_client, monkeypatch):
    monkeypatch.setattr(store, "make_s3_client", lambda settings: pytest.fail("built a client"))
    bucket = stores_for(s3_settings(archive_root=tmp_path), client=s3_client)[1]
    assert isinstance(bucket, S3Store) and bucket.client is s3_client


def test_both_stores_are_object_stores(tmp_path, s3_client):
    assert isinstance(LocalStore(tmp_path), ObjectStore)
    assert isinstance(S3Store(s3_client, LAKE_BUCKET), ObjectStore)
    assert not isinstance(object(), ObjectStore)


def test_a_local_lock_sits_beside_its_object(tmp_path):
    key = "_catalog/social_scraper_posts.json"
    assert (
        LocalStore(tmp_path).lock_path(key) == tmp_path / "_catalog/social_scraper_posts.json.lock"
    )


def test_a_bucket_lock_is_machine_local_and_named_for_the_object(s3_client):
    same = S3Store(s3_client, LAKE_BUCKET, "p").lock_path("_catalog/a.json")
    assert same == S3Store(s3_client, LAKE_BUCKET, "p").lock_path("_catalog/a.json")
    assert same.parent.name == "social-scraper-locks" and same.suffix == ".lock"
    assert same != S3Store(s3_client, LAKE_BUCKET, "q").lock_path("_catalog/a.json")
    assert same != S3Store(s3_client, LAKE_BUCKET, "p").lock_path("_catalog/b.json")


def test_stores_for_s3_without_a_bucket_is_a_store_error(tmp_path, s3_client):
    with pytest.raises(StoreError, match="ARCHIVE_S3_BUCKET"):
        stores_for(IsolatedSettings(archive_backend="s3"), client=s3_client)
