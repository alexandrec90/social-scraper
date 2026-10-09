"""Where the lake's objects live: the local tree under `ARCHIVE_ROOT`, and the bucket.

Keys are POSIX paths relative to the archive root -- `_catalog/<dataset>.json`,
`social_scraper/posts/platform=reddit/dt=2026-10-01/part.parquet` -- the same keys
data-lake's `ObjectStore` reads (under its `ARCHIVE_S3_PREFIX`, when one is set). One
layout serves both backends, so data-lake's `load_catalog` reads the bucket with no
producer-specific code and no shared code.

An export always writes the local tree and, with `ARCHIVE_BACKEND=s3`, the bucket as
well. The local copy is the staging buffer `.claude/rules/data-lake.md` names and what
`social-scraper lake` reads; the bucket is what ibkr_trader reads from its container.
"""

from __future__ import annotations

import hashlib
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import boto3
from botocore.exceptions import BotoCoreError, ClientError, HTTPClientError
from botocore.exceptions import ConnectionError as BotoConnectionError

from social_scraper.archive.lock import LOCK_SUFFIX
from social_scraper.config import Settings

# S3 error codes that mean "no such object" (R2 and AWS answer GET with NoSuchKey,
# HEAD with a bare 404).
_MISSING_CODES = frozenset({"404", "NoSuchKey", "NotFound"})

# Pauses between attempts at a local rename the OS refused, about 8 s in all. On Windows
# a rename onto a file another process has open -- data-lake reading the catalog, the
# virus scanner on a part it just saw written -- fails with WinError 32 until that
# handle closes, which is a moment, not a fault. Failing the dataset on it failed the
# whole scheduled scrape (2026-10-06, `_catalog/social_scraper_posts.json`).
REPLACE_RETRY_DELAYS_S: tuple[float, ...] = (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 4.0)


def replace_with_retry(
    source: Path,
    target: Path,
    *,
    delays: tuple[float, ...] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """`source.replace(target)`, retried while the OS refuses it as in use.

    Only `PermissionError` is retried -- what a sharing violation raises. A refusal that
    outlasts every pause is a real one and propagates, so the dataset still fails.
    `delays` defaults to `REPLACE_RETRY_DELAYS_S`, read at call time.
    """
    for delay in REPLACE_RETRY_DELAYS_S if delays is None else delays:
        try:
            source.replace(target)
            return
        except PermissionError:
            sleep(delay)
    source.replace(target)


class StoreError(OSError):
    """The object store refused a request or could not be reached.

    An `OSError`, so an export already treats it like a full disk: the dataset fails,
    its watermark stays put, and the next run retries.
    """


class LakeUnavailable(StoreError):
    """The bucket's server failed the request (a 5xx) or could not be reached at all.

    The store is at fault, not the request, so every other request fails the same way
    and the export defers the lot rather than failing it (`export.export_all`). The pool
    behind the bucket refuses every write while either of its two storage PCs is off
    (data-lake's `storage-node.py`, placement `001`), answering `InternalError` once
    botocore's retries run out -- about three minutes a dataset. 2026-10-08: the second
    PC went off at 22:31, and the 23:00 scrape spent twelve minutes on four datasets and
    failed, filed on devkit's ledger as a broken scheduled job (5354e2f6).
    """


def is_unavailable(exc: Exception) -> bool:
    """Whether `exc`, from botocore, says the server failed or was never reached --
    as against refusing this request (a 4xx: a bad key, a missing bucket, credentials)."""
    if isinstance(exc, ClientError):
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        return isinstance(status, int) and status >= 500
    return isinstance(exc, (BotoConnectionError, HTTPClientError))


@runtime_checkable
class ObjectStore(Protocol):
    """Whole-object blob access by root-relative key."""

    name: str

    def get_bytes(self, key: str) -> bytes:
        """The object's bytes. Raises `KeyError` when there is no such object."""
        ...

    def put_bytes(self, key: str, data: bytes) -> None: ...

    def promote(self, staged: str, key: str) -> None:
        """Replace `key` with the object at `staged`, and remove `staged`."""
        ...

    def delete(self, key: str) -> None:
        """Remove `key`; absent is not an error."""
        ...

    def lock_path(self, key: str) -> Path:
        """The local file `archive.lock.file_lock` takes to update `key` alone."""
        ...


class LocalStore:
    """The archive as a plain directory tree."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.name = str(self.root)

    def path(self, key: str) -> Path:
        return self.root / key

    def get_bytes(self, key: str) -> bytes:
        path = self.path(key)
        if not path.is_file():
            raise KeyError(key)
        return path.read_bytes()

    def put_bytes(self, key: str, data: bytes) -> None:
        # Write-then-rename: a crash mid-write never leaves a truncated object behind.
        path = self.path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        replace_with_retry(tmp, path)

    def promote(self, staged: str, key: str) -> None:
        replace_with_retry(self.path(staged), self.path(key))

    def delete(self, key: str) -> None:
        self.path(key).unlink(missing_ok=True)

    def lock_path(self, key: str) -> Path:
        # Beside the object, so any process writing this tree -- whatever its user, temp
        # dir or checkout -- finds the same lock. No reader globs `*.lock`.
        return self.path(key + LOCK_SUFFIX)


def _is_missing(exc: ClientError) -> bool:
    return str(exc.response.get("Error", {}).get("Code")) in _MISSING_CODES


class S3Store:
    """An S3-compatible bucket at `ARCHIVE_S3_ENDPOINT_URL` -- data-lake's SeaweedFS pool
    or Cloudflare R2, whichever it names. Takes a built client."""

    def __init__(self, client: Any, bucket: str, prefix: str = "") -> None:
        if not bucket:
            raise StoreError("ARCHIVE_BACKEND=s3 needs a bucket name (ARCHIVE_S3_BUCKET)")
        self.client = client
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.name = f"s3://{bucket}/{self.prefix}".rstrip("/")
        # What a failure is reported against. A bucket name says nothing about which
        # server holds it: 2026-10-08's `InternalError` read as an R2 fault until the
        # settings were probed, and the bucket was the pool on a home PC left off.
        endpoint = getattr(getattr(client, "meta", None), "endpoint_url", None)
        self.where = f"{self.name} at {endpoint}" if isinstance(endpoint, str) else self.name

    def object_key(self, key: str) -> str:
        """`key` as stored in the bucket: under the prefix, exactly as data-lake reads it."""
        return f"{self.prefix}/{key}" if self.prefix else key

    def _fail(self, action: str, key: str, exc: Exception) -> StoreError:
        kind = LakeUnavailable if is_unavailable(exc) else StoreError
        return kind(f"{self.where}: {action} {key} failed: {exc}")

    def get_bytes(self, key: str) -> bytes:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self.object_key(key))
            return response["Body"].read()
        except ClientError as exc:
            if _is_missing(exc):
                raise KeyError(key) from None
            raise self._fail("get", key, exc) from exc
        except BotoCoreError as exc:
            raise self._fail("get", key, exc) from exc

    def put_bytes(self, key: str, data: bytes) -> None:
        try:
            self.client.put_object(Bucket=self.bucket, Key=self.object_key(key), Body=data)
        except (ClientError, BotoCoreError) as exc:
            raise self._fail("put", key, exc) from exc

    def promote(self, staged: str, key: str) -> None:
        # S3 has no rename: a server-side copy, then the staged object goes. A failure
        # between the two leaves a `.tmp` object, which no reader lists as a partition.
        source = {"Bucket": self.bucket, "Key": self.object_key(staged)}
        try:
            self.client.copy_object(Bucket=self.bucket, Key=self.object_key(key), CopySource=source)
        except (ClientError, BotoCoreError) as exc:
            raise self._fail("copy", f"{staged} -> {key}", exc) from exc
        self.delete(staged)

    def delete(self, key: str) -> None:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=self.object_key(key))
        except (ClientError, BotoCoreError) as exc:
            raise self._fail("delete", key, exc) from exc

    def lock_path(self, key: str) -> Path:
        # S3 has no lock primitive, so the lock is a machine-local file named for the
        # bucket object: every writer on this machine -- the one data PC -- takes turns.
        # Writers on two machines would not; nothing runs that way today.
        digest = hashlib.sha256(f"{self.name}/{key}".encode()).hexdigest()[:32]
        return Path(tempfile.gettempdir()) / "social-scraper-locks" / (digest + LOCK_SUFFIX)


def make_s3_client(settings: Settings) -> Any:
    """A boto3 S3 client from the `ARCHIVE_S3_*` settings. Makes no request."""
    secret = settings.archive_s3_secret_access_key.get_secret_value()
    try:
        return boto3.client(
            "s3",
            endpoint_url=settings.archive_s3_endpoint_url or None,
            region_name=settings.archive_s3_region or None,
            aws_access_key_id=settings.archive_s3_access_key_id or None,
            aws_secret_access_key=secret or None,
        )
    except (BotoCoreError, ValueError) as exc:  # ValueError: a malformed endpoint URL
        raise StoreError(f"cannot build the S3 client: {exc}") from exc


def stores_for(settings: Settings, client: Any = None) -> list[ObjectStore]:
    """Every store an export writes, local first. `client` overrides the built one."""
    stores: list[ObjectStore] = [LocalStore(settings.archive_root)]
    if settings.archive_backend == "s3":
        stores.append(
            S3Store(
                client if client is not None else make_s3_client(settings),
                settings.archive_s3_bucket,
                settings.archive_s3_prefix,
            )
        )
    return stores
