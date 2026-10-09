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
- [ ] Set `ARCHIVE_BACKEND=s3` and copy ibkr_trader's `ARCHIVE_S3_*` R2 block into
      `.env` (same names, same meaning; the bucket is `data-lake`), then run
      `social-scraper export --full` once so the bucket gets the history the local
      tree already holds. Until then the export reaches only `ARCHIVE_ROOT`, which
      ibkr_trader does not read
- [ ] Backfill Reddit history (README, "Backfilling Reddit history"): download the
      `REDDIT_SUBREDDITS` files from the subreddit torrent (needs a torrent client) and
      Arctic Shift for 2026, run `reddit-import <folder> --dry-run` to size it, check the
      R2 bucket has room, then run it for real
- [ ] Backfill StockTwits, Bluesky and Hacker News (README, "StockTwits, Bluesky and
      Hacker News"): `stocktwits-import`, `bluesky-import`, `hn-import`. StockTwits
      first loads ~29 GiB of metadata into DuckDB under `DATA_DIR`; run it when no
      other import is using the memory

## Sports domain (needs you)

- [ ] Map Mise-o-jeu+ onto the league list: browse Mise-o-jeu+ with sports_betting's
      overlay on, then `uv run python scripts/check-leagues.py`; paste each unmapped
      `SPORT|League` label into the right league's `mise_o_jeu` in `sports-leagues.toml`
      (or add the league). Until then every label shows as unmapped
- [ ] Decide the budgets before `DOMAINS=stocks,sports`: stocks alone already uses
      ~900 of `REDDIT_DAILY_BUDGET=1000`, and X's 90/day fits 3 pages a cycle. Sports
      adds ~10 Reddit loads and `SPORTS_X_PAGES_PER_CYCLE` X pages a cycle
- [ ] `uv run social-scraper sports-sync` once (about ten minutes), so the first cycles
      do not trickle the leagues in two at a time
- [ ] Put a few known-good sports accounts (beat reporters, insiders) in
      `SPORTS_X_SEED_ACCOUNTS`
- [ ] Review `sports-leagues.toml`'s subreddit names: they were not checked against
      Reddit, and a wrong one shows as "not found" in `logs/scrape-run.json`
- [ ] Add QIDs to a league's `extra` where Wikidata's league data lags (the Premier
      League query missed promoted clubs on 2026-10-04; FBS returned 106 of ~134 teams)
- [ ] sports_betting: read `social_scraper_sports_*` from the lake if its models want
      the chatter (restore-then-use, per `.claude/rules/data-lake.md`)

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
