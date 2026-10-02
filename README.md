# Social Scraper

Browser-driven social ingestion (Reddit, X) into data-lake

Generated from [devkit](https://github.com/alexandrec90/devkit)'s project
template. The agent harness in `scripts/hooks/` is vendored from there — see
`CLAUDE.md`, "Vendored agent harness".

## What it does

Collects stock-related sentiment material from Reddit and X without API keys, by
driving a real Chrome through Playwright, slowly enough that the accounts involved stay
healthy, and publishes it to the pooled data lake for ibkr_trader.

- **Reddit** -- a fixed set of subreddits (`REDDIT_SUBREDDITS`). Per subreddit and cycle:
  new submissions, the subreddit-wide newest-comments stream (where the daily-thread
  sentiment is), and every `REDDIT_REFRESH_HOURS` a `top?t=day` pass that re-observes
  posts so their engagement snapshots show how they matured. Anonymous browsing works;
  signing in is optional.
- **X** -- discovery, then following. Cashtag searches for your `WATCHLIST` and for
  whatever is trending across both platforms, plus rotating generic finance queries,
  surface authors; those seen repeatedly with reach become *tracked*, and tracked
  accounts' timelines are read. Accounts whose timelines turn out not to be about
  markets are dropped; anyone a tracked account retweets or quotes becomes a candidate.
  Search needs a signed-in profile; timelines do not.
- **Symbols are discovered, not listed** -- every `$CASHTAG`, X-tagged symbol, and
  upper-case word that is a real listed security (NASDAQ Trader's directory of every
  US-listed security, refreshed weekly, plus `SYMBOL_EXTRA_FILES`) is recorded. A
  cashtag outside the directory is kept and flagged `in_universe = false` -- that is
  how a fresh IPO shows up. A stoplist, tuned on live data, removes jargon that happens
  to be a ticker (`ITM`, `API`, `DD`, `CEO`, ...).
- **Sentiment material** -- text, engagement (score, comments, upvote ratio; likes,
  reposts, quotes, views, bookmarks), engagement snapshots over time, Reddit flair,
  author reach (followers) and verification, and a trading-slang stance feature
  (`stance` in [-1, 1] from "calls/puts/moon/drilling/🚀"...). The model sentiment score
  is deliberately left to ibkr_trader, which scores rows where `sentiment` is NULL.

## Staying unbanned

Every page load goes through one throttle per platform (`social_scraper/throttle.py`):

| Guard | Reddit default | X default |
| --- | --- | --- |
| Jittered gap between page loads | 6-14 s | 35-90 s |
| Hourly / daily budget (persisted in Postgres, survives restarts) | 240 / 2000 | 20 / 150 |
| Active hours (local time) | always | 08:00-23:00 |
| Page loads per cycle | all subreddits | 8, with slow jittered scrolls |
| Rate limit (429, "try reloading") | back off 15 min, doubling, capped at 6 h | same |
| Challenge (captcha, locked account, network block) | stop for 12 h | same |

Pages are read from the data the site loads for itself -- Reddit's listing JSON, X's
GraphQL responses and server-streamed first page -- never scraped from the DOM. Each
platform has its own browser profile under `data/browser-profiles/`, so a problem on
one never touches the other's cookies.

## Quick start

```bash
cp .env.example .env              # then fill in the placeholders
docker compose up -d --wait db    # Postgres: the scraper's operational state
uv run social-scraper init-db
uv run social-scraper login x     # optional: sign in once in the window, then close it
uv run social-scraper scrape      # one cycle of both platforms, then export
uv run social-scraper run         # forever, every CYCLE_MINUTES
```

Run these from the checkout root in any shell, with no venv activated: `uv run` creates
`.venv` from `uv.lock` on Python 3.12 (`.python-version`) the first time, and resyncs it
when the lock changes. A bare `python -m social_scraper` uses whatever `python` is on
PATH, which has none of the dependencies.

Run it on the desktop, not in the container: it needs a visible Chrome (Reddit blocks
headless) and the signed-in profiles in `data/`. Use a dedicated X account rather than
your personal one.

| Command | What it does |
| --- | --- |
| `scrape [reddit\|x\|all] [--no-export]` | one cycle now; report in `logs/scrape-run.json` |
| `run [reddit\|x\|all] [--cycles N]` | repeat forever (or N times) |
| `status` | budgets used, pauses, row counts, trending symbols, X accounts |
| `export` | copy new rows to the lake |
| `lake [--days N]` | per-symbol daily mentions/authors/stance from the lake |
| `universe` | refresh the symbol directory |

Every run overwrites `logs/scrape-run.json` with what each platform did, every error,
and every notice -- on success too. The terminal gets a status line and that path.

## Data contract (data-lake / ibkr_trader)

`export` writes Hive-style Parquet plus `_catalog/<dataset>.json` into `ARCHIVE_ROOT`,
the pooled tree data-lake's `load_catalog` reads -- no shared database, no shared
code. Verified against data-lake's own `DatasetManifest.from_dict`.

| Dataset | Grain | Key | Partition |
| --- | --- | --- | --- |
| `social_scraper_posts` | post, comment or tweet | platform, external_id | `social_scraper/posts/platform=*/dt=<created>/part.parquet` |
| `social_scraper_mentions` | (post, symbol) | platform, external_id, symbol | `.../mentions/platform=*/dt=<created>/` |
| `social_scraper_engagement` | post at one observation | platform, external_id, observed_at | `.../engagement/platform=*/dt=<observed>/` |
| `social_scraper_symbols` | every symbol ever mentioned | symbol | `.../symbols/` |

`social_scraper_posts` uses data-lake's `social_posts` column names (`platform`,
`channel`, `external_id`, `created_at`, `author_hash`, `title`, `body`, `score`,
`num_comments`, `symbols`, `fetched_at`), so restoring it into that table is a
column-for-column copy upserted on `(platform, external_id)`. Reddit ids match
data-lake's PRAW connector (bare base36 for submissions; comments are `t1_<id>`), and
`author_hash` is the same unsalted `sha256(username)`, so rows from both sources
dedupe and join. X authors hash their stable numeric id. Handles, e-mail addresses and
phone numbers are scrubbed from text; a post matching a credential pattern is dropped,
never exported.

Partitions are rewritten by merge (new rows win), verified by read-back, then
catalogued -- a late comment or refreshed score lands in the right day's file.

In VS Code, `Ctrl+Shift+B` runs the default build task and the task quick-pick
(`Ctrl+Shift+P` → "Run Task") lists everything else, each with a one-line `detail`
explaining what it costs and what it touches.

## Host ports

This checkout is **slot 2** in devkit's `ports.toml`. Every published port
is its conventional base plus the slot:

| Service | Host port |
| --- | --- |
| postgres | 5434 |

Regenerate the `.env` block for any checkout with
`python <devkit>/scripts/devkit_ports.py <checkout-name>`.


## Layout

```text
social_scraper/
  cli.py, runner.py       commands; one cycle across platforms + the run report
  browser.py              Playwright transport (the only code that touches Chrome)
  fetch.py                the Browser protocol scrapers are written (and tested) against
  throttle.py             pacing, budgets, active hours, circuit breaker
  reddit/                 listing parser + incremental subreddit crawler
  x/                      GraphQL/SSR tweet parser, account discovery, crawler
  symbols/                NASDAQ symbol universe + mention extraction
  pipeline.py             scrub, extract, score, upsert
  stance.py, privacy.py   trading-slang stance; PII scrubbing and secret filter
  db/                     operational tables (Postgres; SQLite in tests)
  export.py, archive/     lake writer, catalog, DuckDB read lens
tests/                    tests (SQLite always; Postgres too when DATABASE_URL is one)
scripts/                  project scripts (Python, each with tests)
scripts/hooks/            vendored agent harness — edit upstream in devkit
.devkit.toml              the per-project harness seam (NOT vendored)
docker-compose.yml        the local stack
```

## CI

`.github/workflows/pr-gate.yml` runs lint, tests, and the harness drift check on
every PR. The drift check is only meaningful when it can see devkit — if it prints
"nothing to do (skipping)", the gate is inert and the wiring needs fixing. Locally,
where `DEVKIT_DIR` is usually unset, `pre-commit run devkit-drift --all-files` is the
same check against the devkit rev pinned in `.pre-commit-config.yaml`.

`.github/dependabot.yml` opens weekly dependency PRs, and
`.github/workflows/dependabot-automerge.yml` merges them once the gate passes —
patch/minor bumps of anything, plus majors confined to dev tooling. A major that
touches a runtime dependency is labelled `needs-manual-merge` and waits for you.

That auto-merge workflow is **vendored** from devkit, so `sync-devkit.py --check`
compares it byte-for-byte and editing it here shows up as drift. Keep the gate's title
`PR Gate`: the auto-merge workflow waits on it by name, and a rename makes the merge job
inert without turning anything red.

`.github/actions/setup-python-env/` is **yours**, by contrast. devkit shipped it through
that same channel for one release and pulled it back out when two projects turned out to
install in ways `uv sync` cannot express, so it now arrives from `templates/` as a
one-shot copy: nothing compares it, and rewriting its steps to match how this project
installs is the supported move rather than drift. What owning it costs is the other
direction — later devkit fixes to that file never reach here.
