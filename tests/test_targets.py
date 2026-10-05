from datetime import timedelta

from conftest import NOW

from social_scraper.sports.targets import (
    LEAGUE_WEIGHT,
    TEAM_FLOOR,
    SubredditTarget,
    entity_query,
    pick_subreddits,
    subreddit_targets,
)


def test_subreddit_targets_weights_and_dedupes():
    targets = subreddit_targets(
        ["nhl", "hockey", "NHL"],
        {"Habs": "Q1", "leafs": "Q2", "nhl": "Q9"},
        {"Q1": 20},
    )
    by_name = {t.name: t.weight for t in targets}
    assert set(by_name) == {"nhl", "hockey", "Habs", "leafs"}
    assert by_name["nhl"] == LEAGUE_WEIGHT  # the league's own wins over a team's
    assert by_name["leafs"] == TEAM_FLOOR
    assert TEAM_FLOOR < by_name["Habs"] < LEAGUE_WEIGHT + 3


def test_pick_subreddits_prefers_unvisited_then_weight_times_staleness():
    targets = [
        SubredditTarget("nhl", LEAGUE_WEIGHT),
        SubredditTarget("Habs", 1.0),
        SubredditTarget("quiet", TEAM_FLOOR),
        SubredditTarget("new", TEAM_FLOOR),
    ]
    visited = {
        "nhl": NOW - timedelta(hours=1),  # 3 x 1 = 3
        "habs": NOW - timedelta(hours=10),  # 1 x 10 = 10
        "quiet": NOW - timedelta(hours=20),  # 0.1 x 20 = 2
    }
    # never visited counts as a week: 0.1 x 168 = 16.8
    assert pick_subreddits(targets, visited, NOW, 3) == ["new", "Habs", "nhl"]
    assert pick_subreddits(targets, visited, NOW, 0) == []
    assert pick_subreddits([], visited, NOW, 4) == []


def test_a_quiet_team_still_comes_round():
    targets = [SubredditTarget("nhl", LEAGUE_WEIGHT), SubredditTarget("quiet", TEAM_FLOOR)]
    visited = {"nhl": NOW - timedelta(hours=1), "quiet": NOW - timedelta(hours=40)}
    assert pick_subreddits(targets, visited, NOW, 1) == ["quiet"]


def test_entity_query():
    assert entity_query("Montreal Canadiens") == '"Montreal Canadiens" (lang:en OR lang:fr)'
