from pathlib import Path

import pytest

from conftest import IsolatedSettings

from social_scraper.config import Settings, csv_list, parse_hours


def settings(**over) -> Settings:
    return IsolatedSettings(**over)


def test_csv_list_trims_and_drops_empties():
    assert csv_list(" a, b ,,c ") == ("a", "b", "c")
    assert csv_list("") == ()
    assert csv_list("x;y", sep=";") == ("x", "y")


@pytest.mark.parametrize(
    ("value", "expected"),
    [("8-23", (8, 23)), ("22-6", (22, 6)), ("0-24", (0, 24)), ("", None), ("  ", None)],
)
def test_parse_hours(value, expected):
    assert parse_hours(value) == expected


@pytest.mark.parametrize("bad", ["5-5", "25-3", "3-25", "abc"])
def test_parse_hours_rejects_nonsense(bad):
    with pytest.raises(ValueError):
        parse_hours(bad)


def test_list_properties_parse_their_strings():
    s = settings(
        reddit_subreddits="stocks, investing",
        reddit_ignore_authors="AutoModerator,VisualMod",
        watchlist="$nvda, tsla",
        x_discovery_queries="a b; c d",
        x_seed_accounts="@one,two",
        symbol_extra_files="a.txt,b.txt",
        x_active_hours="9-17",
        reddit_active_hours="",
    )
    assert s.subreddits == ("stocks", "investing")
    assert s.ignored_reddit_authors == frozenset({"automoderator", "visualmod"})
    assert s.watchlist_symbols == ("NVDA", "TSLA")
    assert s.x_queries == ("a b", "c d")
    assert s.x_seeds == ("one", "two")
    assert s.extra_symbol_files == (Path("a.txt"), Path("b.txt"))
    assert s.x_hours == (9, 17)
    assert s.reddit_hours is None


def test_domains_default_to_stocks_and_reject_unknown_names():
    assert settings().enabled_domains == ("stocks",)
    assert settings(domains="Stocks, sports").enabled_domains == ("stocks", "sports")
    with pytest.raises(ValueError, match="crypto"):
        _ = settings(domains="sports,crypto").enabled_domains


def test_sports_settings():
    s = settings(sports_x_seed_accounts="@NHL, CFL")
    assert s.sports_x_seeds == ("NHL", "CFL")
    assert s.sports_leagues_file == Path("sports-leagues.toml")
    # A rarely visited team subreddit would otherwise page back three deep.
    assert s.sports_reddit_max_pages == 1


def test_defaults_are_conservative():
    # Sized for a machine that runs `social-scraper run` around the clock.
    s = settings()
    assert "wallstreetbets" in s.subreddits
    assert s.reddit_min_delay_s >= 15
    assert s.reddit_hourly_budget <= 90
    assert s.reddit_daily_budget <= 1000
    assert s.reddit_hours is not None  # Reddit rests overnight too
    assert s.x_daily_budget <= 90
    assert s.x_hourly_budget <= 8
    assert s.x_min_delay_s >= 60
    assert s.block_cooldown_hours >= 24
    assert s.block_cooldown_max_hours >= s.block_cooldown_hours
    assert s.browser_headless is True  # passes Reddit and X once the UA is renamed (measured)
    assert s.browser_minimized is True  # a headed run stays out of the desktop user's way
    assert len(s.x_queries) >= 3


def _active_hours_per_day(hours: tuple[int, int] | None) -> int:
    if hours is None:
        return 24
    start, end = hours
    return end - start if start < end else 24 - start + end


def test_default_x_pace_fits_its_budgets():
    # Budgets are a ceiling, not the pacing: a loop that hits the daily budget at 15:00
    # goes quiet for the rest of the window and then bursts again at the reset.
    s = settings()
    cycles_per_hour = 60 / s.cycle_minutes
    hourly = s.x_pages_per_cycle * cycles_per_hour
    assert hourly <= s.x_hourly_budget
    assert hourly * _active_hours_per_day(s.x_hours) <= s.x_daily_budget


def test_the_lake_is_local_only_by_default():
    s = settings()
    assert s.archive_backend == "local"
    assert s.archive_s3_bucket == s.archive_s3_endpoint_url == s.archive_s3_prefix == ""
    assert s.archive_s3_secret_access_key.get_secret_value() == ""


FAKE_SECRET = "fake-secret"  # pragma: allowlist secret - invented value, never a credential


def test_bucket_settings_read_data_lakes_names(monkeypatch):
    """One block of R2 settings means the same thing here and in ibkr_trader/data-lake."""
    env = {
        "ARCHIVE_BACKEND": "s3",
        "ARCHIVE_S3_BUCKET": "data-lake",
        "ARCHIVE_S3_ENDPOINT_URL": "https://acct.r2.cloudflarestorage.com",
        "ARCHIVE_S3_REGION": "auto",
        "ARCHIVE_S3_ACCESS_KEY_ID": "fake-id",
        "ARCHIVE_S3_SECRET_ACCESS_KEY": FAKE_SECRET,
        "ARCHIVE_S3_PREFIX": "",
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    s = settings()
    assert s.archive_backend == "s3"
    assert s.archive_s3_bucket == "data-lake"
    assert s.archive_s3_endpoint_url == "https://acct.r2.cloudflarestorage.com"
    assert s.archive_s3_region == "auto"
    assert s.archive_s3_access_key_id == "fake-id"
    assert s.archive_s3_secret_access_key.get_secret_value() == FAKE_SECRET
    assert s.archive_s3_prefix == ""
    # The secret never reaches a repr, a log line or a run report by accident.
    assert FAKE_SECRET not in repr(s)


def test_an_unknown_archive_backend_is_refused(monkeypatch):
    monkeypatch.setenv("ARCHIVE_BACKEND", "gcs")
    with pytest.raises(ValueError, match="archive_backend"):
        settings()


def test_profile_dir_is_per_platform(tmp_path):
    s = settings(data_dir=tmp_path)
    assert s.profile_dir("x") != s.profile_dir("reddit")
    assert s.profile_dir("x").parent == tmp_path / "browser-profiles"
