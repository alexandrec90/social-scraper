---
description: Data-lake storage, catalog, privacy, and restoration constraints
paths:
  - "social_scraper/archive/**/*.py"
  - "social_scraper/export.py"
  - "social_scraper/privacy.py"
---

# Rule: Data lake

`social_scraper/archive/` writes this project's raw data into an S3-compatible bucket as
Hive-partitioned Parquet, and reads it back through a read-only DuckDB lens. The
difference between a lake and a swamp is a schema, a partition convention, and a
catalog — all three are mandatory here.

## The two rules that never relax

**1. Bronze, not source of truth.** Parquet in object storage is immutable raw/bronze
data. The operational database stays the source of truth for anything the hot path
reads. Consumers **restore-then-use**; no request path queries the lake directly. A
lake read on a hot path turns an object-store outage into an application outage.

**2. The bucket is private. Always.** Not "private for now". Scraped or third-party
content carries licensing and privacy obligations (in Québec, Law 25) that a public
bucket violates the moment it is flipped. Never a public bucket, never a public
dataset host.

## The catalog is the reuse contract

Every dataset declares itself in `_catalog/<dataset>.json`: its natural key, its
timestamp column, its column schema, and per-partition row counts and min/max
timestamps. A second project consumes this lake by pointing DuckDB at the same
bucket and reading the catalog — **no shared database and no shared code**.

This is what makes the catalog non-optional. A dataset written without a manifest
entry is invisible to every consumer but the writer, which is precisely the swamp.
Write the manifest in the same operation that writes the partition, immediately
after the verify step — never as a later backfill.

## What may be written

- Non-PII, non-account data: public content, market/reference data, derived features.
- Author or user identifiers must be **hashed before they are written**, never stored raw.

## What must never be written

Write this list down before the first byte ships, not after an incident:

- Credentials of any kind — `.env` values, API keys, tokens, session secrets
- Account, order, execution, or payment records
- Personal contact data
- Raw prompt/response bodies from agent sessions
- Raw log lines. Log **signatures** only: a raw line can contain any of the above.

The repo's secret scanning covers the repo. It does **not** cover an exporter — the
allowlist here is what covers that, and it needs its own negative test asserting that
a payload containing a known secret pattern is dropped rather than uploaded.

## Failure behaviour

An unreachable lake **degrades, never blocks**. A failed archive write must not fail a
build, a test run, or an agent session. Log it and move on; the local staging path
under `ARCHIVE_ROOT` is the buffer.

## Verify before delete

Never delete a local partition until the remote copy has been read back and its row
count matched. "Uploaded" is not "durable".
