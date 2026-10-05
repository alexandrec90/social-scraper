from datetime import timedelta

from conftest import NOW
from sqlalchemy import select

from social_scraper.db import schema
from social_scraper.sports import registry
from social_scraper.sports.matcher import CONTEXT, NAME, EntityMention
from social_scraper.sports.wikidata import ATHLETE, TEAM, EntityRecord

HABS = EntityRecord("Q1", TEAM, "Montreal Canadiens", ("Habs",), subreddit="Habs", x_handle="CH")
LEAFS = EntityRecord("Q2", TEAM, "Toronto Maple Leafs", subreddit="leafs")
STAR = EntityRecord("Q3", ATHLETE, "Some Star", team_id="Q1", popularity=30)


def entity_rows(db):
    with db.connect() as conn:
        return {r.entity_id: r for r in conn.execute(select(schema.sports_entities))}


def memberships(db):
    t = schema.sports_entity_leagues
    with db.connect() as conn:
        return {(r.entity_id, r.league): (r.source, r.active) for r in conn.execute(select(t))}


def test_replace_league_inserts_then_retires_what_wikidata_dropped(db):
    with db.begin() as conn:
        assert registry.replace_league(conn, "nhl", [HABS, LEAFS, STAR], NOW) == 3
    later = NOW + timedelta(days=7)
    renamed = EntityRecord("Q1", TEAM, "Canadiens", ("Habs", "Glorieux"), subreddit="Habs")
    with db.begin() as conn:
        registry.replace_league(conn, "nhl", [renamed, STAR], later)
    rows = entity_rows(db)
    assert rows["Q1"].name == "Canadiens" and rows["Q1"].aliases == ["Habs", "Glorieux"]
    assert rows["Q2"].active is False  # left the league, kept as a row
    assert rows["Q1"].first_seen_at.replace(tzinfo=None) == NOW.replace(tzinfo=None)
    assert memberships(db)[("Q2", "nhl")] == ("wikidata", False)


def test_an_entity_in_two_leagues_stays_active_while_one_holds_it(db):
    with db.begin() as conn:
        registry.replace_league(conn, "epl", [HABS], NOW)
        registry.replace_league(conn, "ucl", [HABS], NOW)
        registry.replace_league(conn, "ucl", [], NOW)
    assert entity_rows(db)["Q1"].active is True
    with db.connect() as conn:
        (habs,) = registry.load_entities(conn)
    assert habs.leagues == frozenset({"epl"})


def test_a_sync_never_retires_a_discovered_membership(db):
    with db.begin() as conn:
        registry.upsert_entity(conn, STAR, registry.DISCOVERED, NOW)
        registry.set_membership(conn, "Q3", "nhl", registry.DISCOVERED, NOW)
        registry.replace_league(conn, "nhl", [HABS], NOW)
    assert memberships(db)[("Q3", "nhl")] == ("discovered", True)
    # ...and Wikidata later listing it takes ownership.
    with db.begin() as conn:
        registry.replace_league(conn, "nhl", [HABS, STAR], NOW)
    assert memberships(db)[("Q3", "nhl")] == ("wikidata", True)
    assert entity_rows(db)["Q3"].source == "wikidata"


def test_refresh_active_follows_memberships(db):
    m = schema.sports_entity_leagues
    with db.begin() as conn:
        registry.replace_league(conn, "nhl", [HABS], NOW)
        conn.execute(m.update().values(active=False))
        registry.refresh_active(conn, NOW)
    assert entity_rows(db)["Q1"].active is False
    with db.begin() as conn:
        conn.execute(m.update().values(active=True))
        registry.refresh_active(conn, NOW)
    assert entity_rows(db)["Q1"].active is True


def test_retire_discovered_after_silence(db):
    with db.begin() as conn:
        registry.upsert_entity(conn, STAR, registry.DISCOVERED, NOW)
        registry.set_membership(conn, "Q3", "nhl", registry.DISCOVERED, NOW)
        assert registry.retire_discovered(conn, NOW - timedelta(days=1), NOW) == 0
        assert registry.retire_discovered(conn, NOW + timedelta(days=1), NOW) == 1
    assert entity_rows(db)["Q3"].active is False


def test_team_subreddits_and_official_handles(db):
    with db.begin() as conn:
        registry.replace_league(conn, "nhl", [HABS, LEAFS, STAR], NOW)
        registry.replace_league(conn, "epl", [HABS], NOW)
        assert registry.team_subreddits(conn, ["nhl"]) == {
            "Habs": ("Q1", "nhl"),
            "leafs": ("Q2", "nhl"),
        }
        assert registry.team_subreddits(conn, []) == {}
        assert registry.official_handles(conn, ["Q1", "Q2"]) == ["CH"]
        assert registry.official_handles(conn, []) == []


def post_row(external_id, created=NOW):
    return {
        "platform": "reddit",
        "external_id": external_id,
        "channel": "nhl",
        "created_at": created,
    }


def test_record_mentions_counts_new_ones_once_and_feeds_trending(db):
    with db.begin() as conn:
        registry.replace_league(conn, "nhl", [HABS, LEAFS], NOW)
        found = [EntityMention("Q1", NAME), EntityMention("Q2", CONTEXT)]
        assert registry.record_mentions(conn, post_row("a"), found, NOW) == 2
        assert registry.record_mentions(conn, post_row("a"), found, NOW) == 0
        registry.record_mentions(conn, post_row("b"), found[:1], NOW)
        registry.record_mentions(conn, post_row("old", NOW - timedelta(days=3)), found[1:], NOW)
        since = NOW - timedelta(hours=24)
        assert registry.trending(conn, since, 10) == [
            ("Q1", "Montreal Canadiens", 2),
            ("Q2", "Toronto Maple Leafs", 1),
        ]
        assert registry.trending(conn, since, 10, leagues=["epl"]) == []
        assert registry.trending(conn, since, 1) == [("Q1", "Montreal Canadiens", 2)]
    rows = entity_rows(db)
    assert rows["Q1"].mention_count == 2 and rows["Q2"].mention_count == 2


def test_counts(db):
    with db.connect() as conn:
        assert registry.counts(conn) == {
            "sports_posts": 0,
            "sports_mentions": 0,
            "sports_engagement_snapshots": 0,
            "sports_entities": 0,
        }
