from datetime import timedelta

from conftest import NOW

from social_scraper.db import repository
from social_scraper.sports import registry
from social_scraper.sports.discover import load_league_facts
from social_scraper.sports.leagues import League
from social_scraper.sports.sync import (
    SYNCED_PREFIX,
    SyncPolicy,
    league_handles,
    stale_leagues,
    sync_leagues,
)
from social_scraper.sports.wikidata import WikidataError, WikidataRateLimited

E = "http://www.wikidata.org/entity/"
NHL = League("nhl", "NHL", "hockey", wikidata="Q1215892")
KHL = League("khl", "KHL", "hockey", wikidata="Q190001")
LOL = League("lol", "LoL", "esports")
WEEK = timedelta(days=7)
WEEKLY = SyncPolicy(max_age=WEEK, players_per_team=0)


class FakeClient:
    """One team per league; a league QID listed in `fail` raises that error."""

    def __init__(self, fail=None):
        self.fail = fail or {}
        self.leagues_asked: list[str] = []

    def sparql(self, query):
        qid = next((q for q in ("Q1215892", "Q190001") if q in query), None)
        if qid in self.fail:
            raise self.fail[qid]
        if "wdt:P641 ?sport" in query:
            self.leagues_asked.append(qid)
            return [{"xs": f"handle_{qid}", "sports": E + "Q41466"}]
        if "ps:P118" in query:
            return [{"team": E + f"{qid}0", "en": f"Team of {qid}"}]
        return []


def test_stale_leagues_never_synced_first_then_oldest():
    synced = {"nhl": (NOW - timedelta(days=8)).isoformat(), "khl": NOW.isoformat()}
    assert [lg.key for lg in stale_leagues((NHL, KHL, LOL), synced, NOW, WEEK)] == ["lol", "nhl"]
    assert stale_leagues((KHL,), synced, NOW, WEEK) == []
    assert [lg.key for lg in stale_leagues((KHL,), synced, NOW, timedelta(0))] == ["khl"]


def test_sync_leagues_stores_entities_facts_handles_and_the_stamp(engine):
    stats = sync_leagues(engine, FakeClient(), (NHL, LOL), NOW, WEEKLY)
    assert stats.synced == {"nhl": 1, "lol": 0} and stats.errors == {}
    with engine.connect() as conn:
        assert [e.name for e in registry.load_entities(conn)] == ["Team of Q1215892"]
        assert load_league_facts(conn)["nhl"].sports == frozenset({"Q41466"})
        assert set(repository.states_with_prefix(conn, SYNCED_PREFIX)) == {"nhl", "lol"}
    assert league_handles(engine, (NHL,)) == ["handle_Q1215892"]
    assert league_handles(engine, (KHL,)) == []
    # Fresh now: a second cycle asks Wikidata nothing.
    client = FakeClient()
    assert sync_leagues(engine, client, (NHL, LOL), NOW, WEEKLY).synced == {}
    assert client.leagues_asked == []


def test_sync_leagues_honours_the_per_cycle_limit(engine):
    capped = SyncPolicy(max_age=WEEK, players_per_team=0, limit=2)
    assert len(sync_leagues(engine, FakeClient(), (NHL, KHL, LOL), NOW, capped).synced) == 2
    assert len(sync_leagues(engine, FakeClient(), (NHL, KHL, LOL), NOW, capped).synced) == 1


def test_a_failed_league_keeps_its_entities_and_the_others_still_sync(engine):
    sync_leagues(engine, FakeClient(), (NHL,), NOW, WEEKLY)
    later = NOW + timedelta(days=8)
    client = FakeClient(fail={"Q1215892": WikidataError("HTTP 500")})
    stats = sync_leagues(engine, client, (NHL, KHL), later, WEEKLY)
    assert stats.errors == {"nhl": "HTTP 500"} and stats.synced == {"khl": 1}
    with engine.connect() as conn:
        names = sorted(e.name for e in registry.load_entities(conn))
    assert names == ["Team of Q1215892", "Team of Q190001"]


def test_a_rate_limit_stops_the_sync_for_the_cycle(engine):
    client = FakeClient(fail={"Q1215892": WikidataRateLimited("HTTP 429")})
    stats = sync_leagues(engine, client, (NHL, KHL), NOW, WEEKLY)
    assert list(stats.errors) == ["nhl"] and "next cycle" in stats.errors["nhl"]
    assert stats.synced == {} and client.leagues_asked == []
