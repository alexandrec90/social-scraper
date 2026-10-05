from pathlib import Path

import pytest

from social_scraper.sports.leagues import (
    AthleteQuery,
    League,
    LeagueConfigError,
    load_leagues,
    parse_athletes,
    parse_league,
    parse_leagues,
    subreddit_leagues,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

TEAM_LEAGUE = """
[[league]]
key = "nhl"
name = "NHL"
sport = "hockey"
wikidata = "Q1215892"
mise_o_jeu = ["ICE_HOCKEY|NHL"]
subreddits = ["nhl", "hockey"]
team_subreddits = true
x_queries = ["NHL injury"]
players = 4
extra = ["Q42"]
"""


def test_parse_leagues_reads_every_field():
    (nhl,) = parse_leagues(TEAM_LEAGUE)
    assert nhl == League(
        key="nhl",
        name="NHL",
        sport="hockey",
        wikidata="Q1215892",
        mise_o_jeu=("ICE_HOCKEY|NHL",),
        subreddits=("nhl", "hockey"),
        team_subreddits=True,
        x_queries=("NHL injury",),
        players=4,
        extra=("Q42",),
    )
    assert nhl.context == frozenset({"nhl"})


def test_athlete_league_and_defaults():
    (atp,) = parse_leagues(
        '[[league]]\nkey = "atp"\nname = "ATP"\nsport = "tennis"\n'
        'athletes = { sport = "Q847", sex = "Q6581097" }\n'
    )
    assert atp.athletes == AthleteQuery("Q847", "Q6581097", 150)
    assert (atp.wikidata, atp.subreddits, atp.team_subreddits, atp.players) == (
        None,
        (),
        False,
        None,
    )


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"key": "NHL", "name": "n", "sport": "s"}, "`key` must match"),
        ({"key": "nhl", "sport": "s"}, "`name` is required"),
        ({"key": "nhl", "name": "n", "sport": "s", "wikidata": "1215892"}, "Wikidata id"),
        ({"key": "x", "name": "n", "sport": "s", "subreddits": ["r/nhl"]}, "not subreddit"),
        ({"key": "x", "name": "n", "sport": "s", "subreddits": "nhl"}, "list of non-empty"),
        ({"key": "x", "name": "n", "sport": "s", "players": -1}, "non-negative"),
        ({"key": "x", "name": "n", "sport": "s", "team_subreddits": "yes"}, "true or false"),
        ({"key": "x", "name": "n", "sport": "s", "athletes": "Q847"}, "must be a table"),
        (
            {"key": "x", "name": "n", "sport": "s", "athletes": {"sport": "Q1", "limit": 0}},
            "1-1000",
        ),
        ({"key": "x", "name": "n", "sport": "s", "athletes": {"sport": "Q1", "sex": "m"}}, "sex"),
        ({"key": "x", "name": "n", "sport": "s", "extra": ["Q1", "nope"]}, "extra"),
    ],
)
def test_parse_league_rejects_malformed_tables(raw, message):
    with pytest.raises(LeagueConfigError, match=message):
        parse_league(raw, 0)


def test_parse_athletes():
    assert parse_athletes(None, "w") is None
    assert parse_athletes({"sport": "Q5377", "limit": 9}, "w") == AthleteQuery("Q5377", None, 9)
    with pytest.raises(LeagueConfigError, match=r"athletes\.sport"):
        parse_athletes({}, "w")


def test_parse_leagues_rejects_duplicates_bad_toml_and_unknown_covers():
    one = '[[league]]\nkey = "a"\nname = "A"\nsport = "s"\n'
    with pytest.raises(LeagueConfigError, match="duplicate"):
        parse_leagues(one + one)
    with pytest.raises(LeagueConfigError, match="not valid TOML"):
        parse_leagues("[[league]\n")
    with pytest.raises(LeagueConfigError, match="covers unknown"):
        parse_leagues(one + 'covers = ["b"]\n')
    with pytest.raises(LeagueConfigError, match=r"\[\[league\]\]"):
        parse_leagues('league = "a"\n')
    assert parse_leagues("") == ()


def test_load_leagues_names_the_file(tmp_path):
    bad = tmp_path / "leagues.toml"
    bad.write_text('[[league]]\nkey = "A"\n', encoding="utf-8")
    with pytest.raises(LeagueConfigError, match=r"leagues\.toml"):
        load_leagues(bad)
    with pytest.raises(LeagueConfigError, match="cannot read"):
        load_leagues(tmp_path / "missing.toml")


def test_the_committed_league_list_parses():
    leagues = load_leagues(REPO_ROOT / "sports-leagues.toml")
    keys = {lg.key for lg in leagues}
    assert {"nhl", "nfl", "nba", "mlb", "cfl", "epl", "atp", "wta", "mma"} <= keys
    assert all(lg.subreddits for lg in leagues)


def test_subreddit_leagues_unions_shared_subreddits_and_covers():
    leagues = parse_leagues(
        '[[league]]\nkey = "epl"\nname = "E"\nsport = "s"\nsubreddits = ["soccer"]\n'
        '[[league]]\nkey = "seriea"\nname = "S"\nsport = "s"\nsubreddits = ["Soccer"]\n'
        '[[league]]\nkey = "ucl"\nname = "U"\nsport = "s"\nsubreddits = ["championsleague"]\n'
        'covers = ["epl"]\n'
    )
    subs = subreddit_leagues(leagues)
    assert subs["soccer"] == frozenset({"epl", "seriea"})
    assert subs["championsleague"] == frozenset({"ucl", "epl"})
