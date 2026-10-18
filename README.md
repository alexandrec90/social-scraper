# Social Scraper

Browser-driven social ingestion (Reddit, X, StockTwits, TradingView, HFBoards, Bluesky,
Polymarket) into data-lake

Generated from [devkit](https://github.com/alexandrec90/devkit)'s project
template. The agent harness in `scripts/hooks/` is vendored from there — see
`CLAUDE.md`, "Vendored agent harness".

## What it does

Collects stock-related sentiment material from Reddit, X, StockTwits, TradingView and
Bluesky, and prediction-market prices from Polymarket, without API keys -- by driving a
real Chrome through Playwright where a site has no open API, slowly enough that the
accounts involved stay healthy -- and publishes it to the pooled data lake for
ibkr_trader. None of the added platforms needs an account or a key (see
[Platforms](#platforms)).

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
- **StockTwits, TradingView, Bluesky** -- per cycle, a few symbols (the watchlist, then
  whatever is trending everywhere): a StockTwits symbol stream, with each author's own
  Bullish/Bearish label in `extra.sentiment`; a symbol's newest TradingView ideas, each
  `extra.direction` long or short; a Bluesky `$TICKER` search, kept only where the post
  writes a listed `$TICKER` or `#TICKER` (Bluesky is general-topic).
- **Polymarket** -- open prediction markets under the stock tags (`stocks`, `earnings`,
  `fed`, ...): what each asks, the tickers it names, and its prices over time.
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

## The sports domain (`DOMAINS=stocks,sports`)

A second domain, off by default, collects chatter about what Mise-o-jeu+ lets you bet
on. It shares the crawlers and the per-platform throttles with stocks, and has its own
tables (`sports_*`) and lake datasets (`social_scraper_sports_*`), so no sports row ever
reaches what ibkr_trader reads. No LLM is involved.

- **Scope is a hand-kept league list**, `sports-leagues.toml`: per league, its
  subreddits, its generic X searches, and where its teams and athletes come from.
  Mise-o-jeu+ itself is never contacted (its terms forbid storing its data; see
  sports_betting's `docs/data-sources.md`). `scripts/check-leagues.py` compares the
  list's `mise_o_jeu` labels with sports_betting's overlay coverage file -- the leagues
  you have actually seen on Mise-o-jeu+ -- and fails on any league the list lacks.
- **Entities come from Wikidata**, weekly per league: each team's English and French
  names and aliases ("Habs", "Glorieux"), its subreddit and X handle, and the
  best-known athletes of each roster; top athletes by sport for tennis, golf, MMA,
  boxing and F1. Names that keep appearing in a league's posts are looked up on Wikidata
  and become entities when Wikidata says they play that sport or in that league.
- **Matching** -- a full name counts anywhere; a one-word name ("Habs", "Panthers",
  "McDavid") only when the post is already about a league that one entity plays in
  (its subreddit, its X search, or another name in the post).
- **Reddit** -- league subreddits plus each team's (for leagues with
  `team_subreddits`), `SPORTS_REDDIT_SUBS_PER_CYCLE` a cycle, the busiest and stalest
  first.
- **X** -- searches for the most-mentioned teams and athletes of the day, plus the
  leagues' generic queries. Accounts are scored as for stocks, plus: an organisation's
  account (X's affiliate label or a Business/Government check) scores higher, and one
  whose own tweets keep selling picks is dropped. The leagues' own accounts are seeds.
- **HFBoards** -- the hockey forum: each league's `hfboards_forums` (the NHL's lists all
  32 team boards, the main NHL board, trade rumours and the sportsbook board), two
  forums a cycle, the least recently read first; in each, the newest page of up to three
  threads with new posts. Quotes are stripped from a post, so it carries no one else's
  words or name.
- **Bluesky** -- the day's most-mentioned teams and athletes, and each league's
  `bluesky_queries`, as for X.
- **Polymarket** -- every open market under a league's `polymarket_tags`: season
  futures, and games, with the teams, moneyline/totals/spread prices, the live score and
  period, and, once it is over, the result (`winner`). This is the one source of
  *event* data here -- schedules, scores, results and implied probabilities; the rest is
  chatter about the events.

Both domains draw on the same Reddit and X budgets: the defaults are sized for stocks
alone, so raise `REDDIT_DAILY_BUDGET` / `X_DAILY_BUDGET` (or lower the per-cycle pages)
when turning sports on. `sports-sync` refreshes every league from Wikidata at once
instead of the cycle's two at a time.

## Platforms

| Platform | How it is read | Domains | Account or key |
| --- | --- | --- | --- |
| Reddit | listing JSON, through Chrome | stocks, sports | optional (`login reddit`) |
| X | GraphQL and server-streamed tweets, through Chrome | stocks, sports | search needs `login x` |
| StockTwits | `api.stocktwits.com/api/2/streams/symbol/<SYM>.json`, through Chrome | stocks | none |
| TradingView | `/symbols/<SYM>/ideas/?component-data-only=1` (JSON), through Chrome | stocks | none |
| HFBoards | forum and thread HTML (XenForo; no JSON), through Chrome | sports | none |
| Bluesky | `api.bsky.app` `searchPosts`, plain HTTPS | stocks, sports | none |
| Polymarket | `gamma-api.polymarket.com/events`, plain HTTPS | stocks, sports | none |

Measured 2026-10-10. StockTwits, TradingView and HFBoards have no open API -- HFBoards
answers a plain HTTP client with 403 -- so they go through Chrome like Reddit and X, each
in its own profile. Bluesky's public AppView and Polymarket's Gamma API answer any
client, so they are read with plain HTTPS (`apiclient.py`), no browser.
`public.api.bsky.app` refuses `searchPosts` signed out; `api.bsky.app` serves it.

**Privacy.** A Bluesky post's URI and link contain its author's DID, so a Bluesky row's
`external_id` is the hash of its URI and it carries no `url`. Polymarket rows carry no
wallet address. Authors are hashed on every platform, as before -- StockTwits on its
numeric user id, so live rows join the NYU archive's.

**Platforms run side by side.** Each has its own throttle, so `CRAWL_WORKERS` (default
3) of them are crawled at once, each in its own thread and browser: a cycle lasts as long
as its slowest platform rather than all of them added up, which seven platforms need --
Reddit and X alone filled 20-25 minutes of a 30-minute cycle. `SCRAPE_PLATFORMS` narrows
what `scrape all` crawls.

## Staying unbanned

Every page load goes through one throttle per platform (`social_scraper/throttle.py`):

| Guard | Reddit default | X default |
| --- | --- | --- |
| Jittered gap between page loads | 15-40 s | 60-180 s |
| Hourly / daily budget (persisted in Postgres, survives restarts) | 90 / 1000 | 8 / 90 |
| Active hours (local time) | 06:00-23:59 | 08:00-22:59 |
| Page loads per cycle | all subreddits | 3, with slow jittered scrolls |
| Rate limit (429, "try reloading") | back off 30 min, doubling, capped at 12 h | same |
| Challenge (captcha, locked account, network block) | stop for 24 h, doubling to 96 h | same |

The added platforms have the same guards with their own defaults (`config.py`,
`.env.example`): StockTwits 6-15 s apart, 30/h and 400/day; TradingView 10-30 s, 12/h,
150/day; HFBoards 8-20 s, 30/h, 450/day; Bluesky 2-6 s, 40/h, 800/day; Polymarket
1-3 s, 150/h, 2500/day. A Cloudflare interstitial ("Just a moment...") is a challenge.

Every platform but X is read anonymously, and on those a first challenge is a notice in
the run report, not a failed cycle: the pause is the whole response, and nobody can lift the
block sooner. A challenge again on the first load after the pause fails the cycle. On X
the first one fails it, because a locked account waits for a person to unlock it.

A rate limit is a notice on every platform, X included, when it is the only failed load
in the last 24 hours: the backoff is the response. One more failed load within the day
fails the cycle, because pacing that is too fast hits the limit again.

Pages are read from the data the site loads for itself -- Reddit's listing JSON, X's
GraphQL responses and server-streamed first page -- never scraped from the DOM. Each
platform has its own browser profile under `data/browser-profiles/`, so a problem on
one never touches the other's cookies.

## Quick start

```bash
cp .env.example .env                # then fill in the placeholders
docker compose up -d --wait db      # Postgres: the scraper's operational state
uv run social-scraper init-db
uv run social-scraper login x       # optional: sign in once in the window, then close it
uv run social-scraper scrape --now  # one cycle of both platforms, then export
uv run social-scraper run           # forever, every CYCLE_MINUTES
```

Run these from the checkout root in any shell, with no venv activated: `uv run` creates
`.venv` from `uv.lock` on Python 3.12 (`.python-version`) the first time, and resyncs it
when the lock changes. A bare `python -m social_scraper` uses whatever `python` is on
PATH, which has none of the dependencies.

Run it on the desktop, not in the container: it needs the installed Chrome and the
signed-in profiles in `data/`. Chrome runs headless by default (`BROWSER_HEADLESS`), so a
scheduled run opens no window. Headless gets through Reddit and X only because the
`HeadlessChrome` user-agent token is renamed; both block it otherwise (measured; see
`social_scraper/browser.py`). `login` always opens a visible window, and
`scrape --headed` / `run --headed` show one for debugging. Use a dedicated X account rather than
your personal one.

| Command | What it does |
| --- | --- |
| `scrape [PLATFORM\|all] [--no-export] [--now]` | one cycle (`all` is `SCRAPE_PLATFORMS`); report in `logs/scrape-run.json` |
| `run [reddit\|x\|all] [--cycles N]` | repeat forever (or N times) |
| `status` | budgets used, pauses, row counts, trending symbols, X accounts |
| `export [--full]` | copy new rows to the lake; `--full` resends every row |
| `lake [--days N]` | per-symbol daily mentions/authors/stance from the lake |
| `universe` | refresh the symbol directory |
| `reddit-import PATH... [--since D] [--until D] [--all-posts] [--local-only] [--dry-run]` | backfill Reddit history from archive dumps; report in `logs/reddit-import.json` |
| `x-import FOLDER... [same options]` | backfill X history from tweet CSV datasets; report in `logs/x-import.json` |
| `stocktwits-import FOLDER... [same options]` | backfill StockTwits 2008-2022 from the NYU dataset; report in `logs/stocktwits-import.json` |
| `bluesky-import PATH... [same options]` | backfill Bluesky 2023-2024 from Zenodo 14669616; report in `logs/bluesky-import.json` |
| `hn-import PATH... [same options]` | backfill Hacker News from `open-index/hacker-news`; report in `logs/hn-import.json` |
| `sports-sync [KEY...]` | re-read leagues from Wikidata now; outcome in `logs/sports-sync.json` |

Every run overwrites `logs/scrape-run.json` with what each platform did, every error,
and every notice -- on success too. The terminal gets a status line and that path.

`scrape` is what a scheduler fires (devkit runs it every `CYCLE_MINUTES`). It exits 1
when the cycle failed -- database unreachable, browser crash, a platform erroring, a
failed export -- and 0 when a platform was deliberately left alone (outside X active
hours, budget spent, circuit breaker open). It first waits a random 0-20% of
`CYCLE_MINUTES`, so a fixed scheduler grid does not become a fixed request interval;
`--now` skips the wait for a run by hand. No page load starts after three quarters of a
cycle, counted from the scheduler's fire when devkit names it (`DEVKIT_FIRED_AT`) and
from the command's own start otherwise, so a run ends before the next fire is due.

The scheduled collector runs on one PC at a time, and moves to the other one with its
database: run devkit's `collectors.py move social-scraper --to HOST`, which calls
`scripts/handoff.py` to carry the dump through the lake bucket. Browser logins do not
travel, so redo `login x` on the receiving PC. See
[`docs/operations/collector-handoff.md`](docs/operations/collector-handoff.md).

## Backfilling Reddit history

The crawler only sees what a listing still shows, so on its own the lake starts the day
the scraper did. `reddit-import` loads the years before that from the community
archives, which are free to download and need no account:

- **Up to the end of 2025:** Watchful1's
  [subreddit torrents](https://academictorrents.com/details/3e3f64dee22dc304cdd2546254ca1f8e8ae542b4)
  (built from Pushshift). In the torrent client, select only
  `<sub>_submissions.zst` and `<sub>_comments.zst` for the subreddits in
  `REDDIT_SUBREDDITS` -- the full torrent is terabytes.
- **2026 onwards:** [Arctic Shift](https://github.com/ArthurHeitmann/arctic_shift)'s
  monthly torrents (`RC_<month>.zst` comments, `RS_<month>.zst` submissions; all of
  Reddit, 60-75 GiB a month) from its
  [download links](https://github.com/ArthurHeitmann/arctic_shift/blob/master/download_links.md),
  published a few weeks after each month ends, or its per-subreddit
  [download tool](https://arctic-shift.photon-reddit.com/download-tool) for a few
  subreddits. A whole-Reddit month imports in about an hour: lines from other
  subreddits are dropped from the raw bytes, before any parsing (142k lines/s, measured).

Put the files in one folder and import it (a folder, because PowerShell hands a native
command its `*.zst` unexpanded):

```bash
uv run social-scraper reddit-import D:/reddit-dumps --dry-run   # counts per year, writes nothing
uv run social-scraper reddit-import D:/reddit-dumps             # writes the lake
```

Each line goes through the crawler's own parser and pipeline -- the same ids, author
hash, scrubbing, symbol extraction and stance -- and lands in `social_scraper_posts` and
`social_scraper_mentions`, written straight to the lake (`ARCHIVE_ROOT`, plus the
bucket unless `--local-only`), not through the database. By default it keeps posts in
`REDDIT_SUBREDDITS`, created since 2015-01-01, that mention at least one symbol; a
cashtag outside today's symbol directory counts, so a ticker delisted since keeps its
history. `--all-posts` keeps the rest.

A backfill never overwrites a row the live crawler exported, and re-running it changes
nothing. What it does not carry: engagement snapshots (an archive kept one observation
per post -- Arctic Shift's about 36 hours after posting -- which stays on the row with
`fetched_at` set to its time; `extra.backfill` names the dump a row came from) and the
symbol registry, which the live export rewrites from the database. The spool goes under `DATA_DIR` and can reach gigabytes for
r/wallstreetbets; `--dry-run` first says how much is coming. Use of the archives is
governed by Reddit's terms, which the archives do not restate -- check them before use.

### X history from tweet datasets

X has no free bulk archive, and id-only datasets can no longer be turned back into text,
so only datasets that shipped the text help. `x-import` reads them; first supported is
the [Cashtag Piggybacking dataset](https://zenodo.org/record/2686862) (CNR, CC BY 4.0,
research use only): about 9M cashtag tweets from May to September 2017, with each
author's bot label. Download `tweets.csv.zip` and `users.csv.zip` into one folder:

```bash
uv run social-scraper x-import D:/social-dumps/cashtag-2017 --local-only
```

Tweets land as `platform=x`, `channel=archive`, authors hashed from their numeric id like
the crawler's (no handle stored); retweets are skipped, as the crawler skips them; the
bot label is `extra.bot`. Report in `logs/x-import.json`. Every importer shares
`backfill.py`: the spool, the lake write and the never-over-a-live-row rule.

### StockTwits, Bluesky and Hacker News

Three more free archives of people talking about stocks, each with its own `platform`
in the lake and `channel=archive` (StockTwits and Bluesky are now crawled live as well;
Hacker News is not):

| Command | Archive | Download | What is kept |
| --- | --- | --- | --- |
| `stocktwits-import` | NYU's StockTwits records, May 2008 to 2022 | the `messages/` and `feature_wo_messages/` folders of `s3://stocktwits-nyu/dataset/v1/data/csv/` (public, ~80 GiB) | messages naming a symbol, as on Reddit; the symbols StockTwits tagged count, and the author's Bullish/Bearish label is `extra.sentiment` |
| `bluesky-import` | [Zenodo 14669616](https://zenodo.org/records/14669616), Feb 2023 to Mar 2024 | `user_posts.tar.gz` (18 GiB), imported as it is | posts with an explicit, listed `$TICKER`; reposts skipped |
| `hn-import` | [`open-index/hacker-news`](https://huggingface.co/datasets/open-index/hacker-news), 2006 on | `data/**/*.parquet` (12 GiB) | items with an explicit, listed `$TICKER`; deleted and dead items skipped |

```bash
uv run social-scraper stocktwits-import D:/social-dumps/stocktwits-2008-2022
uv run social-scraper bluesky-import D:/social-dumps/bluesky-2024/user_posts.tar.gz
uv run social-scraper hn-import D:/social-dumps/hackernews/data
```

Bluesky and Hacker News are general-topic, so a bare upper-case word is not evidence
there: only a `$TICKER` in upper case, of two letters or more, that is listed and is
neither a stoplist word nor a shell variable (`$PATH` is UiPath) or placeholder (`$XX`).
Expect little: 578 Hacker News items in all of 2021 (of 4.2M), and one Bluesky post in
240k in a 5M-line sample (measured 2026-10-08). A crypto token sharing a ticker (`$FET`) still gets through.

StockTwits joins two tables in DuckDB before reading a message: it loads the 29 GiB of
metadata into a sorted table under `DATA_DIR` first, so leave it disk room. The Bluesky
dataset replaced every account and post id with its own integer, so its rows link to no
bsky.app page and its author hashes join with nothing else.

## Data contract (data-lake / ibkr_trader)

`export` writes Hive-style Parquet plus `_catalog/<dataset>.json` to two places:

- **`ARCHIVE_ROOT`**, the local tree -- always. It is the staging buffer, and what
  `lake` reads.
- **The private S3-compatible bucket ibkr_trader reads** (Cloudflare R2, `data-lake`)
  -- when `ARCHIVE_BACKEND=s3`. The `ARCHIVE_S3_*` settings are data-lake's own names
  (`BUCKET`, `ENDPOINT_URL`, `REGION`, `ACCESS_KEY_ID`, `SECRET_ACCESS_KEY`, `PREFIX`),
  so ibkr_trader's R2 block is copied over unchanged. Each object lands at the same key
  relative to the archive root as in the local tree (under `ARCHIVE_S3_PREFIX`, if
  set), so data-lake's `load_catalog` reads the bucket as it is -- no shared database,
  no shared code. After switching the bucket on, `export --full` sends it the history.

The catalog is verified against data-lake's own `DatasetManifest.from_dict`.

| Dataset | Grain | Key | Partition |
| --- | --- | --- | --- |
| `social_scraper_posts` | post, comment or tweet | platform, external_id | `social_scraper/posts/platform=*/dt=<created>/part.parquet` |
| `social_scraper_mentions` | (post, symbol) | platform, external_id, symbol | `.../mentions/platform=*/dt=<created>/` |
| `social_scraper_engagement` | post at one observation | platform, external_id, observed_at | `.../engagement/platform=*/dt=<observed>/` |
| `social_scraper_symbols` | every symbol ever mentioned | symbol | `.../symbols/` |
| `social_scraper_sports_posts` | sports post, with `entities` and `leagues` | platform, external_id | `.../sports_posts/platform=*/dt=<created>/` |
| `social_scraper_sports_mentions` | (post, entity) | platform, external_id, entity_id | `.../sports_mentions/platform=*/dt=<created>/` |
| `social_scraper_sports_engagement` | sports post at one observation | platform, external_id, observed_at | `.../sports_engagement/platform=*/dt=<observed>/` |
| `social_scraper_sports_entities` | team or athlete (Wikidata QID) | entity_id | `.../sports_entities/` |
| `social_scraper_sports_entity_leagues` | (entity, league) | entity_id, league | `.../sports_entity_leagues/` |
| `social_scraper_polymarket_markets` | prediction market, with `domains`, `leagues`, `symbols`, game state, `winner` | market_id | `.../polymarket_markets/platform=polymarket/dt=<created>/` |
| `social_scraper_polymarket_prices` | market at one observation (prices, bid/ask, volume, live score) | market_id, observed_at | `.../polymarket_prices/platform=polymarket/dt=<observed>/` |

Posts from the added platforms land in the same post datasets as Reddit's and X's, with
their own `platform` (`stocktwits`, `tradingview`, `bluesky` in the stock ones;
`hfboards`, `bluesky` in the sports ones). The Polymarket datasets are neither domain's:
a market's `domains` says which it is for, so nothing ibkr_trader reads changes shape.

Sports entities ship without their subreddit or X handle: an athlete's handle
identifies a person, and the lake takes no identifier unhashed.

`social_scraper_posts` uses data-lake's `social_posts` column names (`platform`,
`channel`, `external_id`, `created_at`, `author_hash`, `title`, `body`, `score`,
`num_comments`, `symbols`, `fetched_at`), so restoring it into that table is a
column-for-column copy upserted on `(platform, external_id)`. Reddit ids match
data-lake's PRAW connector (bare base36 for submissions; comments are `t1_<id>`), and
`author_hash` is the same unsalted `sha256(username)`, so rows from both sources
dedupe and join. X authors hash their stable numeric id. Handles, e-mail addresses and
phone numbers are scrubbed from text; a post matching a credential pattern is dropped,
never exported.

Partitions are rewritten by merge (new rows win; for `reddit-import`, stored rows win),
verified by read-back, then catalogued -- a late comment or refreshed score lands in the
right day's file. A backfilled post's `fetched_at` is when the archive captured it, so a
restore that refreshes only from a later `fetched_at` keeps the live observation. Each
store does this against its own copy: the bucket's merge reads the partition back from
the bucket. A store that fails (a refused or unreachable bucket included) fails the
export, which leaves the watermark where it was so the next run resends the same rows.

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
  cli.py, runner.py       commands; one cycle across platforms (in parallel) + the report
  browser.py              Playwright transport (the only code that touches Chrome)
  apiclient.py            plain-HTTPS transport for the public APIs (Bluesky, Polymarket)
  fetch.py                the Browser protocol scrapers are written (and tested) against
  feeds.py                what the newer crawlers share: one throttled load, failure rules
  throttle.py             pacing, budgets, active hours, circuit breaker
  reddit/                 listing parser + incremental subreddit crawler; dumps.py
                          backfills history from Pushshift / Arctic Shift dumps
  x/                      GraphQL/SSR tweet parser, account discovery, crawler;
                          tweet_dumps.py backfills history from tweet CSV datasets
  stocktwits/             live symbol streams (scraper.py); dumps.py the NYU archive
  tradingview/            symbol ideas
  hfboards/               XenForo forum and thread parser + crawler (sports)
  bluesky/                live searches (live.py); dumps.py the Zenodo archive
  polymarket/             Gamma events parser, market store, crawl
  hackernews/             archive importer only (dumps.py): never crawled
  backfill.py             what every archive importer shares: spool, lake write, report
  symbols/                NASDAQ symbol universe + mention extraction
  sports/                 the sports domain: league list, Wikidata sync, matcher, crawl plans
  pipeline.py             scrub, extract, score, upsert
  stance.py, privacy.py   trading-slang stance; PII scrubbing and secret filter
  db/                     operational tables (Postgres; SQLite in tests)
  export.py, archive/     lake writer, catalog, local/S3 stores, DuckDB read lens
tests/                    tests (SQLite always; Postgres too when DATABASE_URL is one)
scripts/                  project scripts (Python, each with tests); handoff.py moves the
                          collector's database to the other PC
docs/operations/          runbooks (collector-handoff.md)
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
