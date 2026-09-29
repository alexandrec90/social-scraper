# TODO — Social Scraper

## Setup (needs you)

- [ ] Fill in `.env` from `.env.example` (it is gitignored; nothing works without it)
- [ ] Create a **dedicated** X account and run `python -m social_scraper login x` once.
      Without it, search (discovery) is skipped and only `X_SEED_ACCOUNTS` and already
      tracked accounts are read -- timelines work logged out.
- [ ] Put a handful of known-good finance accounts in `X_SEED_ACCOUNTS` to bootstrap
      discovery (they are also the only X source while logged out)
- [ ] Optionally `python -m social_scraper login reddit`; anonymous works (measured)
- [ ] Decide where it runs on a schedule (Windows Task Scheduler running
      `python -m social_scraper run`, or leave a terminal open) -- it needs the desktop:
      Reddit blocks headless Chrome
- [ ] Confirm `python scripts/sync-devkit.py --list` shows a stamped `DEVKIT_VERSION`
- [ ] Set `DEVKIT_DIR` in CI so the drift check actually gates
- [ ] Create the **private** object-storage bucket and fill the `ARCHIVE_S3_*` vars --
      today the lake is the pooled local tree (`../data-lake/data/archive`), which is
      what data-lake itself uses; no S3 upload is implemented yet

## Hand-off to the consumers (other repos -- not changed from here)

- [ ] data-lake: a restore step that reads `_catalog/social_scraper_posts.json` and
      upserts `social_scraper_posts` into `social_posts` on `(platform, external_id)`
      -- the columns already match (README, "Data contract"). Keep the scraper's
      `created_at` on conflict, like the PRAW connector does.
- [ ] data-lake: `lens.py` only builds views for its own layouts; add the four
      `social_scraper_*` datasets if research should see them there
- [ ] ibkr_trader: `social_scraper_mentions` already carries validated `symbols`, so
      the `extract_tickers` backfill in its TODO can read them instead
- [ ] devkit workspace: hoist "Scraper: Run Once" / "Scraper: Status" tasks there
      (`.vscode/tasks.json` must stay empty, see its header)

## Next improvements

- [ ] Alembic: tables are created with `create_all` (idempotent, never alters). The
      first schema change needs a migration tool
- [ ] S3 mirror of the local lake with verify-before-delete (`.claude/rules/data-lake.md`)
- [ ] Company-name matching ("Nvidia" -> NVDA) from the directory's security names;
      today only cashtags and upper-case tickers count
- [ ] X: read threads (`TweetDetail`) for high-engagement tweets; replies carry a lot of
      the disagreement a sentiment model wants
- [ ] Reddit: open the day's top threads for their full comment trees (the
      subreddit-wide comment stream samples them, it does not exhaust them)

## Archive

- [x] Replace the placeholder in `social_scraper/` with something that does the job
- [x] Delete `tests/test_smoke.py` once real tests exist
- [x] Write `.claude/rules/data-lake.md`'s "what must never be sent" list before the
      first byte ships (the rule had it; `privacy.py` enforces it, with the negative test)
- [x] Fix `docker-compose.yml` for postgres:18 (volume at `/var/lib/postgresql`)
