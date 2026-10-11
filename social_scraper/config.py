"""Runtime settings, read from the environment and `.env`.

List-valued settings are comma-separated strings in the environment (`a,b,c`), exposed
as tuples through the `*_list` properties -- a `.env` line is easier to edit that way
than a JSON array, and it is what the sibling projects already use.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from social_scraper.models import PLATFORMS

DEFAULT_SUBREDDITS = (
    "wallstreetbets,stocks,investing,options,StockMarket,pennystocks,Daytrading,"
    "ValueInvesting,smallstreetbets,thetagang,CanadianInvestor"
)

# Generic X searches that surface finance accounts independently of any one symbol.
# Rotated one per discovery slot; `min_faves` keeps them to posts other people read.
DEFAULT_X_QUERIES = (
    "(bullish OR bearish) lang:en min_faves:20;"
    "(earnings OR guidance) (beat OR miss) lang:en min_faves:20;"
    "(calls OR puts) (expiry OR strike) lang:en min_faves:10;"
    "(breakout OR squeeze OR short interest) lang:en min_faves:20;"
    '("price target" OR upgrade OR downgrade) lang:en min_faves:20'
)


STOCKS = "stocks"
SPORTS = "sports"
DOMAINS = (STOCKS, SPORTS)


def csv_list(value: str, sep: str = ",") -> tuple[str, ...]:
    """Split a separated setting into trimmed, non-empty items."""
    return tuple(item.strip() for item in value.split(sep) if item.strip())


class Settings(BaseSettings):
    """Every knob the scraper reads. Defaults are the conservative, ban-averse ones."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = (
        "postgresql+psycopg://social_scraper:social_scraper_local_dev"  # pragma: allowlist secret
        "@127.0.0.1:5434/social_scraper"
    )

    # --- where things live -------------------------------------------------------------
    data_dir: Path = Path("data")
    # The local lake tree: always written, the staging buffer and what `lake` reads.
    archive_root: Path = Path("../data-lake/data/archive")
    logs_dir: Path = Path("logs")

    # --- the lake bucket (`archive/store.py`) --------------------------------------------
    # The names and meanings of data-lake's `ArchiveSettings`, which is how ibkr_trader
    # reads the bucket, so one block of R2 settings means the same thing in both repos.
    # "s3" uploads every exported partition and catalog to the bucket as well as
    # ARCHIVE_ROOT; "local" (the default) writes ARCHIVE_ROOT only.
    archive_backend: Literal["local", "s3"] = "local"
    archive_s3_bucket: str = ""
    archive_s3_endpoint_url: str = ""
    # Cloudflare R2 wants "auto".
    archive_s3_region: str = ""
    archive_s3_access_key_id: str = ""
    archive_s3_secret_access_key: SecretStr = SecretStr("")
    # Key prefix inside the bucket; empty puts `_catalog/` at the bucket root.
    archive_s3_prefix: str = ""

    # --- browser -----------------------------------------------------------------------
    # "chrome" drives the installed Google Chrome (a real fingerprint, no download);
    # empty uses Playwright's bundled Chromium (`playwright install chromium`).
    browser_channel: str = "chrome"
    # Headless with the `HeadlessChrome` UA token renamed gets through Reddit and X
    # (measured, see `browser.py`), and opens no window on the desktop. `login` is always
    # visible; `scrape --headed` / `run --headed` show the window for debugging.
    browser_headless: bool = True
    # When headed (BROWSER_HEADLESS=false), launch the window off-screen and minimize it,
    # so a cycle never takes the keyboard focus from whoever is using the desktop
    # (`browser.py`). Ignored headless; `login` and `--headed` always show the window.
    browser_minimized: bool = True
    # IANA zone for the active-hours windows; a human account does not post at 04:00.
    scraper_timezone: str = "America/Toronto"

    # --- symbols -----------------------------------------------------------------------
    symbol_universe_url: str = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqtraded.txt"
    symbol_universe_max_age_hours: float = 24 * 7
    # One-symbol-per-line files merged into the universe, e.g. ibkr_trader's tickers.txt
    # for TSX names the NASDAQ directory does not list.
    symbol_extra_files: str = ""
    # Symbols always searched on X, whatever is trending.
    watchlist: str = ""

    # --- reddit ------------------------------------------------------------------------
    reddit_subreddits: str = DEFAULT_SUBREDDITS
    reddit_ignore_authors: str = "AutoModerator,VisualMod"
    # Paced for a machine that is on around the clock: ~25 loads a cycle at 15-40 s
    # apart, well under the budgets, which are the ceiling rather than the pace.
    reddit_min_delay_s: float = 15.0
    reddit_max_delay_s: float = 40.0
    reddit_hourly_budget: int = 90
    reddit_daily_budget: int = 1000
    # Reddit rests overnight too: a browser that never sleeps is not a person.
    reddit_active_hours: str = "6-24"
    reddit_max_pages: int = 3
    reddit_refresh_hours: float = 6.0

    # --- x -----------------------------------------------------------------------------
    # 3 pages a cycle x 2 cycles an hour x 15 active hours = 90: the loop's own pace fits
    # the daily budget, so it never runs dry mid-afternoon and bursts again at the reset.
    x_min_delay_s: float = 60.0
    x_max_delay_s: float = 180.0
    x_hourly_budget: int = 8
    x_daily_budget: int = 90
    x_pages_per_cycle: int = 3
    x_scrolls_per_page: int = 3
    x_active_hours: str = "8-23"
    x_discovery_queries: str = DEFAULT_X_QUERIES
    x_seed_accounts: str = ""
    x_max_tracked_accounts: int = 150
    x_min_followers: int = 500
    x_search_share: float = 0.4

    # --- the platforms added beside Reddit and X ------------------------------------------
    # Each has its own browser profile (or none, for an API), throttle, budgets and active
    # hours, named `<platform>_*` like Reddit's and X's -- `runner.policy_for` reads them
    # by that name. Budgets are a ceiling about twice what the per-cycle settings spend.

    # StockTwits (`stocktwits/`): one symbol stream is one load of its JSON API through
    # the browser, the 30 newest messages; trending symbols are read every few hours.
    stocktwits_min_delay_s: float = 6.0
    stocktwits_max_delay_s: float = 15.0
    stocktwits_hourly_budget: int = 30
    stocktwits_daily_budget: int = 400
    stocktwits_active_hours: str = "6-24"
    stocktwits_symbols_per_cycle: int = 6
    stocktwits_trending_hours: float = 3.0

    # TradingView (`tradingview/`): one symbol's ideas page as JSON, 24 ideas a page.
    tradingview_min_delay_s: float = 10.0
    tradingview_max_delay_s: float = 30.0
    tradingview_hourly_budget: int = 12
    tradingview_daily_budget: int = 150
    tradingview_active_hours: str = "7-23"
    tradingview_symbols_per_cycle: int = 3

    # HFBoards (`hfboards/`, sports only): a forum's thread list, then the newest page of
    # each thread with new posts. Which forums: the leagues' `hfboards_forums`.
    hfboards_min_delay_s: float = 8.0
    hfboards_max_delay_s: float = 20.0
    hfboards_hourly_budget: int = 30
    hfboards_daily_budget: int = 450
    hfboards_active_hours: str = "7-24"
    hfboards_forums_per_cycle: int = 2
    hfboards_threads_per_forum: int = 3

    # Bluesky (`bluesky/live.py`): `searchPosts` on the public AppView, no account.
    bluesky_min_delay_s: float = 2.0
    bluesky_max_delay_s: float = 6.0
    bluesky_hourly_budget: int = 40
    bluesky_daily_budget: int = 800
    bluesky_active_hours: str = ""
    bluesky_searches_per_cycle: int = 6
    bluesky_sports_searches_per_cycle: int = 3

    # Polymarket (`polymarket/`): market prices from the Gamma API, no account. Stock
    # tags here; sports tags come from the leagues' `polymarket_tags`.
    polymarket_min_delay_s: float = 1.0
    polymarket_max_delay_s: float = 3.0
    polymarket_hourly_budget: int = 150
    polymarket_daily_budget: int = 2500
    polymarket_active_hours: str = ""
    polymarket_stock_tags: str = "stocks,earnings,equities,ipos,fed,economy"
    # Tags read a cycle, the longest unread first (0 reads all). A page of the `nfl` tag
    # was 33.6 MB at 100 events (2026-10-10): with ~40 tags, all of them every cycle was
    # gigabytes a day. 12 reads each sports tag every 1.5-2 h, stock tags every cycle.
    polymarket_tags_per_cycle: int = 12
    polymarket_pages_per_tag: int = 1
    # A market is stored only past this 24-hour volume (USD); 0 keeps any that traded.
    # 9,472 of the nfl tag's 10,371 open markets had not traded that day.
    polymarket_min_volume_24h: float = 0.0
    # Events past their end, re-read by id until they resolve, at most this many a cycle.
    polymarket_resolve_per_cycle: int = 40

    # --- the cycle ------------------------------------------------------------------------
    # The platforms `scrape all` crawls (comma-separated): a subset of models.PLATFORMS.
    scrape_platforms: str = "reddit,x,stocktwits,tradingview,hfboards,bluesky,polymarket"
    # Platforms crawled at once, each in its own thread with its own browser. Every
    # platform has its own throttle, so running them side by side changes no pace; it is
    # what lets seven fit a cycle that Reddit and X alone filled 20-25 minutes of. 1 runs
    # them one after another; each browser is a Chrome, so mind the memory.
    crawl_workers: int = 3

    # --- domains -------------------------------------------------------------------------
    # What a cycle collects: `stocks`, `sports`, or both (comma-separated). Both domains
    # draw on the same per-platform budgets above -- raise those when adding one.
    domains: str = "stocks"

    # --- sports (`social_scraper/sports/`) -------------------------------------------------
    sports_leagues_file: Path = Path("sports-leagues.toml")
    # How old a league's Wikidata teams and rosters may get before a cycle re-reads them.
    sports_refresh_hours: float = 24 * 7
    # Leagues a cycle re-reads at most (35-110 s each live); `sports-sync` does them all.
    sports_sync_leagues_per_cycle: int = 2
    # Best-known athletes kept per team (a league's `players` overrides it).
    sports_players_per_team: int = 6
    # Subreddits a cycle visits, chosen by priority (`sports/targets.py`), and pages per
    # listing: a rarely visited team subreddit would otherwise page back three deep.
    sports_reddit_subs_per_cycle: int = 4
    sports_reddit_max_pages: int = 1
    sports_x_pages_per_cycle: int = 2
    # Accounts always followed for sports, on top of the leagues' own (from Wikidata).
    sports_x_seed_accounts: str = ""
    sports_x_max_tracked_accounts: int = 150
    sports_x_min_followers: int = 1000
    # Wikidata lookups a cycle spends confirming names that keep coming up.
    sports_lookups_per_cycle: int = 5

    # --- shared throttle behaviour ------------------------------------------------------
    backoff_base_minutes: float = 30.0
    backoff_max_minutes: float = 720.0
    block_cooldown_hours: float = 24.0
    # Each consecutive challenge doubles the cooldown, up to this.
    block_cooldown_max_hours: float = 96.0

    # --- loop --------------------------------------------------------------------------
    cycle_minutes: float = 30.0

    @property
    def subreddits(self) -> tuple[str, ...]:
        return csv_list(self.reddit_subreddits)

    @property
    def ignored_reddit_authors(self) -> frozenset[str]:
        return frozenset(a.lower() for a in csv_list(self.reddit_ignore_authors))

    @property
    def watchlist_symbols(self) -> tuple[str, ...]:
        return tuple(s.upper().lstrip("$") for s in csv_list(self.watchlist))

    @property
    def x_queries(self) -> tuple[str, ...]:
        return csv_list(self.x_discovery_queries, sep=";")

    @property
    def x_seeds(self) -> tuple[str, ...]:
        return tuple(h.lstrip("@") for h in csv_list(self.x_seed_accounts))

    @property
    def enabled_domains(self) -> tuple[str, ...]:
        """`DOMAINS` as a tuple; an unknown name is a configuration error, not a skip."""
        names = tuple(d.lower() for d in csv_list(self.domains))
        unknown = sorted(set(names) - set(DOMAINS))
        if unknown:
            raise ValueError(f"DOMAINS: unknown {', '.join(unknown)} (known: {', '.join(DOMAINS)})")
        return names

    @property
    def sports_x_seeds(self) -> tuple[str, ...]:
        return tuple(h.lstrip("@") for h in csv_list(self.sports_x_seed_accounts))

    @property
    def extra_symbol_files(self) -> tuple[Path, ...]:
        return tuple(Path(p) for p in csv_list(self.symbol_extra_files))

    @property
    def x_hours(self) -> tuple[int, int] | None:
        return parse_hours(self.x_active_hours)

    @property
    def reddit_hours(self) -> tuple[int, int] | None:
        return parse_hours(self.reddit_active_hours)

    def hours_of(self, platform: str) -> tuple[int, int] | None:
        """`<PLATFORM>_ACTIVE_HOURS`, parsed."""
        return parse_hours(getattr(self, f"{platform}_active_hours"))

    @property
    def enabled_platforms(self) -> tuple[str, ...]:
        """`SCRAPE_PLATFORMS` in crawl order; an unknown name is a configuration error."""
        names = {p.lower() for p in csv_list(self.scrape_platforms)}
        unknown = sorted(names - set(PLATFORMS))
        if unknown:
            raise ValueError(
                f"SCRAPE_PLATFORMS: unknown {', '.join(unknown)} (known: {', '.join(PLATFORMS)})"
            )
        return tuple(p for p in PLATFORMS if p in names)

    @property
    def polymarket_stock_tag_list(self) -> tuple[str, ...]:
        return tuple(t.lower() for t in csv_list(self.polymarket_stock_tags))

    def profile_dir(self, platform: str) -> Path:
        """One browser profile per platform, so an X ban cannot touch the Reddit login."""
        return self.data_dir / "browser-profiles" / platform


def parse_hours(value: str) -> tuple[int, int] | None:
    """`"8-23"` -> `(8, 23)`: allowed from 08:00 until 22:59. Empty means always."""
    value = value.strip()
    if not value:
        return None
    start, _, end = value.partition("-")
    lo, hi = int(start), int(end)
    if not (0 <= lo <= 23 and 1 <= hi <= 24 and lo != hi):
        raise ValueError(f"active hours must look like '8-23', got {value!r}")
    return lo, hi
