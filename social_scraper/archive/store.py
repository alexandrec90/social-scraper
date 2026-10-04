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

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from social_scraper.config import Settings

# S3 error codes that mean "no such object" (R2 and AWS answer GET with NoSuchKey,
# HEAD with a bare 404).
_MISSING_CODES = frozenset({"404", "NoSuchKey", "NotFound"})


class StoreError(OSError):
    """The object store refused a request or could not be reached.

    An `OSError`, so an export already treats it like a full disk: the dataset fails,
    its watermark stays put, and the next run retries.
    """


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
        tmp.replace(path)

    def promote(self, staged: str, key: str) -> None:
        self.path(staged).replace(self.path(key))

    def delete(self, key: str) -> None:
        self.path(key).unlink(missing_ok=True)


def _is_missing(exc: ClientError) -> bool:
    return str(exc.response.get("Error", {}).get("Code")) in _MISSING_CODES


class S3Store:
    """An S3-compatible bucket (Cloudflare R2 in production). Takes a built client."""

    def __init__(self, client: Any, bucket: str, prefix: str = "") -> None:
        if not bucket:
            raise StoreError("ARCHIVE_BACKEND=s3 needs a bucket name (ARCHIVE_S3_BUCKET)")
        self.client = client
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.name = f"s3://{bucket}/{self.prefix}".rstrip("/")

    def object_key(self, key: str) -> str:
        """`key` as stored in the bucket: under the prefix, exactly as data-lake reads it."""
        return f"{self.prefix}/{key}" if self.prefix else key

    def _fail(self, action: str, key: str, exc: Exception) -> StoreError:
        return StoreError(f"{self.name}: {action} {key} failed: {exc}")

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
