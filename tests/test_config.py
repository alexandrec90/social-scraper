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
    assert s.browser_headless is False  # headless Chrome is blocked by Reddit (measured)
    assert s.browser_minimized is True  # out of the way of whoever is using the desktop
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


def test_profile_dir_is_per_platform(tmp_path):
    s = settings(data_dir=tmp_path)
    assert s.profile_dir("x") != s.profile_dir("reddit")
    assert s.profile_dir("x").parent == tmp_path / "browser-profiles"
