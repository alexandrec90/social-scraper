"""Runtime settings, read from the environment and `.env`.

List-valued settings are comma-separated strings in the environment (`a,b,c`), exposed
as tuples through the `*_list` properties -- a `.env` line is easier to edit that way
than a JSON array, and it is what the sibling projects already use.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

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
    # The pooled data-lake tree: data-lake's `load_catalog` reads `_catalog/` from here.
    archive_root: Path = Path("../data-lake/data/archive")
    logs_dir: Path = Path("logs")

    # --- browser -----------------------------------------------------------------------
    # "chrome" drives the installed Google Chrome (a real fingerprint, no download);
    # empty uses Playwright's bundled Chromium (`playwright install chromium`).
    browser_channel: str = "chrome"
    browser_headless: bool = False
    # IANA zone for the X active-hours window; a human account does not post at 04:00.
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
    reddit_min_delay_s: float = 6.0
    reddit_max_delay_s: float = 14.0
    reddit_hourly_budget: int = 240
    reddit_daily_budget: int = 2000
    reddit_max_pages: int = 3
    reddit_refresh_hours: float = 6.0

    # --- x -----------------------------------------------------------------------------
    x_min_delay_s: float = 35.0
    x_max_delay_s: float = 90.0
    x_hourly_budget: int = 20
    x_daily_budget: int = 150
    x_pages_per_cycle: int = 8
    x_scrolls_per_page: int = 3
    x_active_hours: str = "8-23"
    x_discovery_queries: str = DEFAULT_X_QUERIES
    x_seed_accounts: str = ""
    x_max_tracked_accounts: int = 150
    x_min_followers: int = 500
    x_search_share: float = 0.4

    # --- shared throttle behaviour ------------------------------------------------------
    backoff_base_minutes: float = 15.0
    backoff_max_minutes: float = 360.0
    block_cooldown_hours: float = 12.0

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
    def extra_symbol_files(self) -> tuple[Path, ...]:
        return tuple(Path(p) for p in csv_list(self.symbol_extra_files))

    @property
    def x_hours(self) -> tuple[int, int] | None:
        return parse_hours(self.x_active_hours)

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
