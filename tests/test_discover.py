from datetime import timedelta

from conftest import NOW
from sqlalchemy import select

from social_scraper.db import schema
from social_scraper.sports import discover, registry
from social_scraper.sports.discover import (
    CONFIRMED,
    MIN_SIGHTINGS,
    PENDING,
    REJECTED,
    DiscoveryStats,
    LeagueFacts,
    accepts,
    candidate_names,
    count_candidates,
    due_lookups,
    load_league_facts,
    resolve,
    run_discovery,
    save_league_facts,
)
from social_scraper.sports.matcher import Entity, Matcher
from social_scraper.sports.wikidata import (
    ATHLETE,
    TEAM,
    EntityFacts,
    EntityRecord,
    WikidataRateLimited,
)

MATCHER = Matcher(
    [Entity("Q1", "athlete", "Connor McDavid", (), frozenset({"nhl"}))],
    {"nhl": frozenset({"nhl"})},
)
NHL = LeagueFacts("Q1215892", frozenset({"Q41466"}))


def test_candidate_names_takes_two_and_three_word_runs_only():
    text = (
        "Ivan Demidov scored. Lane Hutson Junior assisted. "
        "Game Thread Canadiens At Maple Leafs Tonight. The Rookie played. "
        "Connor McDavid and McDavid's linemate Zach Hyman"
    )
    assert candidate_names(text, MATCHER) == ["Ivan Demidov", "Lane Hutson Junior", "Zach Hyman"]


def test_candidate_names_breaks_runs_at_punctuation():
    assert candidate_names("Late winner, Arsenal. Bukayo Saka again", MATCHER) == ["Bukayo Saka"]


def test_candidate_names_skips_known_names_and_caps_the_count():
    assert candidate_names("Some Other McDavid", MATCHER) == []
    many = ". ".join(f"Name{chr(65 + i)} Person" for i in range(15))
    assert len(candidate_names(many, MATCHER)) == discover.MAX_NAMES_PER_POST


def test_count_candidates_and_due_lookups(engine):
    with engine.begin() as conn:
        for _ in range(MIN_SIGHTINGS):
            count_candidates(conn, ["Ivan Demidov"], ["nhl", "khl"], NOW)
        count_candidates(conn, ["Rare Name"], ["nhl"], NOW)
        count_candidates(conn, ["Wide Name"], ["a", "b", "c", "d"], NOW)
        due = due_lookups(conn, 10)
    assert due == [("Ivan Demidov", ["khl", "nhl"])]
    with engine.connect() as conn:
        wide = conn.execute(
            select(schema.sports_name_candidates.c.league).where(
                schema.sports_name_candidates.c.name == "Wide Name"
            )
        ).scalars()
        assert sorted(wide) == ["a", "b", "c"]  # MAX_LEAGUES_PER_NAME


def test_league_facts_round_trip(engine):
    with engine.begin() as conn:
        save_league_facts(conn, "nhl", NHL, NOW)
        save_league_facts(conn, "lol", LeagueFacts(None, frozenset()), NOW)
        assert load_league_facts(conn) == {"nhl": NHL, "lol": LeagueFacts(None, frozenset())}


def record(qid, kind=ATHLETE, name="Ivan Demidov"):
    return EntityRecord(qid, kind, name)


def test_accepts_by_wikidata_claims():
    human = EntityFacts(classes=frozenset({"Q5"}), sports=frozenset({"Q41466"}))
    golfer = EntityFacts(classes=frozenset({"Q5"}), sports=frozenset({"Q5377"}))
    club = EntityFacts(classes=frozenset({"Q4498974"}), leagues=frozenset({"Q1215892"}))
    other_club = EntityFacts(classes=frozenset({"Q4498974"}), leagues=frozenset({"Q190001"}))
    assert accepts(record("Q1"), human, NHL)
    assert not accepts(record("Q1"), golfer, NHL)
    assert accepts(record("Q2", TEAM), club, NHL)
    assert not accepts(record("Q2", TEAM), other_club, NHL)
    assert not accepts(record("Q2", TEAM), club, LeagueFacts(None, frozenset()))


class FakeClient:
    def __init__(self, docs, fail_after=None):
        self.docs = docs  # qid -> wbgetentities document
        self.searches = []
        self.fail_after = fail_after

    def api(self, **params):
        if params["action"] == "wbsearchentities":
            if self.fail_after is not None and len(self.searches) >= self.fail_after:
                raise WikidataRateLimited("429")
            self.searches.append(params["search"])
            return {"search": [{"id": q} for q in self.docs if params["search"] in q_names(q)]}
        return {"entities": {q: self.docs[q] for q in params["ids"].split("|")}}


NAMES = {"Q100": "Ivan Demidov", "Q101": "Ivan Demidov", "Q200": "Game Thread"}


def q_names(qid):
    return NAMES[qid]


def doc(qid, *classes, sports=(), leagues=()):
    def claims(values):
        return [{"mainsnak": {"datavalue": {"value": {"id": v}}}} for v in values]

    return {
        "id": qid,
        "labels": {"en": {"value": NAMES[qid]}},
        "claims": {"P31": claims(classes), "P641": claims(sports), "P118": claims(leagues)},
        "sitelinks": {"enwiki": {}},
    }


DOCS = {
    # A namesake first in the search results: a painter, not a hockey player.
    "Q100": doc("Q100", "Q5", sports=()),
    "Q101": doc("Q101", "Q5", sports=("Q41466",)),
    "Q200": doc("Q200", "Q1"),
}


def test_resolve_skips_namesakes_and_checks_every_league():
    client = FakeClient(DOCS)
    found = resolve(client, "Ivan Demidov", ["khl", "nhl"], {"nhl": NHL})
    assert found is not None and found[0].entity_id == "Q101" and found[1] == "nhl"
    assert resolve(client, "Nobody Known", ["nhl"], {"nhl": NHL}) is None


def seed_candidates(engine, *names):
    with engine.begin() as conn:
        save_league_facts(conn, "nhl", NHL, NOW)
        for name in names:
            for _ in range(MIN_SIGHTINGS):
                count_candidates(conn, [name], ["nhl"], NOW)


def statuses(engine):
    t = schema.sports_name_candidates
    with engine.connect() as conn:
        return {r.name: (r.status, r.entity_id) for r in conn.execute(select(t))}


def test_discovery_stats_start_empty():
    stats = DiscoveryStats()
    assert (stats.looked_up, stats.confirmed, stats.rejected, stats.pruned) == (0, [], 0, 0)
    assert stats.error is None


def test_run_discovery_confirms_rejects_and_adds_the_entity(engine):
    seed_candidates(engine, "Ivan Demidov", "Game Thread")
    stats = run_discovery(engine, FakeClient(DOCS), NOW, limit=5)
    assert (stats.looked_up, stats.rejected, stats.confirmed) == (2, 1, ["Ivan Demidov (nhl)"])
    assert statuses(engine) == {
        "Ivan Demidov": (CONFIRMED, "Q101"),
        "Game Thread": (REJECTED, None),
    }
    with engine.connect() as conn:
        (entity,) = registry.load_entities(conn)
    assert (entity.entity_id, entity.leagues) == ("Q101", frozenset({"nhl"}))
    # Nothing left pending: a second run looks nothing up.
    assert run_discovery(engine, FakeClient(DOCS), NOW, limit=5).looked_up == 0


def test_run_discovery_stops_at_a_wikidata_error_and_prunes(engine):
    seed_candidates(engine, "Ivan Demidov", "Game Thread")
    stats = run_discovery(engine, FakeClient(DOCS, fail_after=0), NOW, limit=5)
    assert stats.looked_up == 0 and "429" in stats.error
    assert {s for s, _ in statuses(engine).values()} == {PENDING}
    pruned = run_discovery(engine, FakeClient(DOCS), NOW + timedelta(days=31), limit=0)
    assert pruned.pruned == 2 and statuses(engine) == {}
