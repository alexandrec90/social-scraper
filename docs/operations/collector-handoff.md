# Moving the collector between PCs

The collector is devkit's scheduled `uv run social-scraper scrape`, every 30 minutes from
a Windows Scheduled Task, with the compose `db` started first. It runs on one of two
Windows PCs, and its Postgres lives in that PC's `pgdata` volume. Moving it moves the
database with it, through the bucket the lake export already writes. No new credentials
are needed: [`scripts/handoff.py`](../../scripts/handoff.py) builds its client the way
[`social_scraper/archive/store.py`](../../social_scraper/archive/store.py) does, from
`ARCHIVE_S3_*`, and refuses to run unless `ARCHIVE_BACKEND=s3`. The bucket is
data-lake's SeaweedFS pool on the tailnet, not R2.

**devkit drives it** with `collectors.py move social-scraper --to HOST`. On the source PC
it removes the scheduled task, waits for a scrape already running, and keeps `db` up. It
then runs `export`, and once that succeeds it stops this project's containers. The
receiving PC asks `pending` on each collectors pass. When a handoff is addressed to it,
that PC runs `import` and then registers the scheduled task there. devkit runs every verb
from the checkout on the host with `UV_FROZEN=1`. boto3 is a core dependency, so no
`--extra` is needed. To run it by hand:

```bash
# on PC-A, with the scheduled task removed and no scrape running
uv run python scripts/handoff.py export --from pc-a --to pc-b
# on PC-B
uv run python scripts/handoff.py pending --host pc-b   # the marker, or nothing
uv run python scripts/handoff.py import --host pc-b
```

Host names are compared case-insensitively. devkit spells them as `harness_events.host_name`
does, the lowercased hostname.

## What each step does

- **export**: refuses a marker another machine exported that has not been imported yet,
  because that dump is newer than this copy. Then it starts `db`, runs `pg_dump -Fc`
  inside it, copies the dump out to a temp file, and uploads it to
  `<prefix>/handoff/social-scraper/db.dump`. Only after the upload succeeds does it write
  `marker.json` beside the dump: `state: exported`, `from`, `to`, `at`, `key`, `sha256`,
  `bytes`, plus the source's `tables`, `posts` and `last_request_at` (its newest page
  load).
- **pending**: one GET of the marker, and no Docker. The client's timeouts are cut down
  (`PENDING_SOCKET`) so that an unreachable pool answers well inside devkit's 120 s. It
  prints the marker when it is `exported` and addressed to this host, and prints nothing
  otherwise.
- **import**: downloads the dump and checks its size and sha256 against the marker
  before touching Docker. Then it starts `db` and compares this machine's copy with the
  dump (see below). It drops the database, creates it empty and restores it **serially**
  (`pg_restore -Fc --no-owner`, never `-j`), then runs `ANALYZE`. It checks that the
  restored copy has the marker's table and post counts. Finally it marks the handoff
  `state: imported`, `by`, `imported_at`. It never registers the scheduled task: that is
  devkit's step.

The dump object stays in the bucket after an import, as the last handoff's backup. The
next export overwrites it.

## The dump replaces the receiver's copy

The receiver usually still holds an old copy of the database from the last time it ran
the collector. The import throws that copy away: `DROP DATABASE ... WITH (FORCE)`, then
`CREATE DATABASE`, then the restore. This is the same as ibkr_trader's handoff, and has
the effect of `pg_restore --clean` without its partial-object errors.

That is only right while the receiver's copy is the older one. When it has page loads
later than the dump's `last_request_at`, both machines scraped. Restoring would then
discard this machine's rows, so the import refuses, exits 1 and changes nothing. A person
decides which copy wins. To keep this machine's copy, hand off from here instead. To keep
the dump's copy, drop this one (the refusal prints the `dropdb` command) and import
again.

## What travels and what does not

| What | Travels | Why |
| --- | --- | --- |
| The Postgres `db`: posts, mentions, engagement, symbols, `x_accounts`, `crawl_state` cursors, `request_log` budgets, `throttle_state`, `export_watermarks`, the sports and Polymarket tables | yes | it is the source of truth. Budgets and circuit breakers follow the accounts, not the machine |
| `browser:<platform>:launch_timeouts` in `crawl_state` | cleared on import | a slow Chrome start measures the machine (`launch.py`), and this is a different machine |
| `logs/scrape-run.json` | no; the receiver's own is moved to `logs/scrape-run.before-handoff.json` | see below |
| `data/browser-profiles/` (about 3.2 GB) | no | Chrome encrypts its cookies with DPAPI, per Windows user, so they cannot decrypt on another PC |
| `data/symbols/` | no | the first scrape downloads the symbol directory again when it is missing or older than `SYMBOL_UNIVERSE_MAX_AGE_HOURS` (`symbols/universe.py`) |

**`logs/scrape-run.json` starts clean.** The next run reads three things from it.
`timeout_streak` counts crawl timeouts, which measure the machine's load, not the
collector, and `offline_streak` counts cycles that got no answer from the network, which
measure the sender's connection. `lake_down_since` dates a bucket outage, and a
successful handoff has just written to that bucket, so the sender's value is stale by
construction. Carrying the
sender's report would therefore carry nothing true. The receiver's own report is worse:
it dates from the last time that machine ran, perhaps weeks ago. With it, the first slow
load would count as a long streak, and the first bucket refusal as an outage of weeks,
which fails the scrape at once (`runner.judge_lake`). So the import moves it aside rather
than deleting it.

**Logins have to be redone on the receiver.** The import's status line and the `logins`
list in `logs/handoff.json` name, for each of `models.LOGIN_PLATFORMS`, whether this
machine has a profile of its own and what the login command is:

- `uv run social-scraper login x`: until then, X search is skipped and only the seed
  and tracked accounts' timelines are read, since timelines work logged out.
- `uv run social-scraper login reddit`: optional, because Reddit is read anonymously.

A profile left from this machine's last turn may still be signed in. If X search shows
as skipped in `logs/scrape-run.json`, that session has expired. Running logged out is a
supported mode, not a failure: X's login wall on a search is a notice, and the scrape
still exits 0. `tests/test_runner.py` holds that the scheduled verdict stays green while
nobody has logged in yet.

## One machine at a time

The lake's S3 locks are machine-local files under `%TEMP%`
(`S3Store.lock_path`). They keep this machine's writers in turn, but do nothing about a
writer on another machine. That is safe only because exactly one machine runs the
collector. The handoff is what keeps it so: the task is removed on the source before the
export, and registered on the receiver only after the import. Never run `scrape` or an
`*-import` backfill against the bucket on both PCs at once.

**An export needs both storage PCs on.** The pool behind the bucket refuses every write
while either of its two storage PCs is off (`LakeUnavailable` in `archive/store.py`). An
export started then fails after botocore's retries, says so, and writes no marker. Run it
again once both are up.

The dump holds the operational database as it is, which is not lake data. In particular,
`x_accounts.handle` and `sports_x_accounts.handle` are raw, and only `author_hash` ever
reaches the lake. The dump sits under `handoff/`, outside `_catalog/`, so no lake
consumer lists or reads it. Like everything else in the bucket, it relies on the bucket
staying private (`.claude/rules/data-lake.md`).

## When it fails

Every run overwrites `logs/handoff.json`. It is written first as a "running" account, so
that a run devkit's bound cut short says so. When the run ends, it is rewritten with
`command`, `ok`, `exit_code` and `detail`, on failure too. Exit codes:

- 0: done.
- 1: failed, with the reason in `detail`. This includes a bucket that is not configured,
  which devkit reports.
- 2: cannot tell. Only `pending` uses it, for a bucket it could not read, and devkit
  retries on its next pass.

Each verb keeps inside devkit's bound with its own budget: export 85 of 90 minutes,
import 55 of 60. Each long step gets what is left of that budget as its timeout, so a
slow run fails here and writes why.

- A failed export writes no marker, so the other PC never sees a half-uploaded dump.
- A failed import leaves the marker `exported`, so the next pass retries it. The local
  database may already be dropped by then. That is safe only because the dump in the
  bucket is intact and was verified before anything was dropped.
- `pg_restore` exits 1 on any failed statement. The import accepts that only when every
  error is on the allowlist (`IGNORABLE_RESTORE_ERRORS`), which covers the default
  `public` schema existing already. Any other error fails the import.
