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
    )
    assert s.subreddits == ("stocks", "investing")
    assert s.ignored_reddit_authors == frozenset({"automoderator", "visualmod"})
    assert s.watchlist_symbols == ("NVDA", "TSLA")
    assert s.x_queries == ("a b", "c d")
    assert s.x_seeds == ("one", "two")
    assert s.extra_symbol_files == (Path("a.txt"), Path("b.txt"))
    assert s.x_hours == (9, 17)


def test_defaults_are_conservative():
    s = settings()
    assert "wallstreetbets" in s.subreddits
    assert s.x_daily_budget <= 200
    assert s.x_min_delay_s >= 30
    assert s.browser_headless is True  # passes Reddit and X once the UA is renamed (measured)
    assert len(s.x_queries) >= 3


def test_profile_dir_is_per_platform(tmp_path):
    s = settings(data_dir=tmp_path)
    assert s.profile_dir("x") != s.profile_dir("reddit")
    assert s.profile_dir("x").parent == tmp_path / "browser-profiles"
