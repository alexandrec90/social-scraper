"""Wikidata parsing and the per-league fetch, against bindings shaped like the live ones.

Row shapes mirror what `query.wikidata.org` returned on 2026-10-04 for the NHL and
Premier League queries (URIs as `http://www.wikidata.org/entity/Q...`, GROUP_CONCAT
fields `|`-joined), with invented ids and names.
"""

import json
from typing import ClassVar

import pytest

from social_scraper.sports import wikidata
from social_scraper.sports.leagues import AthleteQuery, League
from social_scraper.sports.wikidata import (
    ATHLETE,
    TEAM,
    EntityRecord,
    LeagueFetch,
    WikidataClient,
    WikidataError,
    WikidataRateLimited,
    athletes_query,
    bindings,
    fetch_league,
    get_entities,
    league_query,
    parse_athletes,
    parse_entity_doc,
    parse_league,
    parse_teams,
    pick_rosters,
    qid_of,
    roster_query,
    search_ids,
    teams_query,
    usable_alias,
)

E = "http://www.wikidata.org/entity/"


def team_row(qid, en, links="40", **over):
    return {"team": E + qid, "en": en, "links": links, **over}


def player_row(qid, team, en, links="20", start="", **over):
    return {"p": E + qid, "team": E + team, "en": en, "links": links, "start": start, **over}


class FakeClient:
    """Answers SPARQL by which query template it recognises; records every query."""

    def __init__(self, league=None, teams=None, rosters=None, athletes=None, docs=None):
        self.answers = {
            "wdt:P641 ?sport": league or [],
            "ps:P118": teams or [],
            "p:P54": rosters or [],
            "LIMIT": athletes or [],
        }
        self.docs = docs or {}
        self.queries: list[str] = []

    def sparql(self, query):
        self.queries.append(query)
        for marker, rows in self.answers.items():
            if marker in query:
                return rows
        raise AssertionError(f"unexpected query {query[:80]}")

    def api(self, **params):
        if params["action"] == "wbgetentities":
            return {"entities": {q: self.docs[q] for q in params["ids"].split("|")}}
        return {"search": [{"id": q} for q in self.docs]}


def test_qid_of_and_bindings():
    assert qid_of(E + "Q188143") == "Q188143"
    assert qid_of("not a uri") is None
    data = {"results": {"bindings": [{"x": {"type": "literal", "value": "1"}}]}}
    assert bindings(data) == [{"x": "1"}]
    assert bindings(None) == []


def test_usable_alias():
    assert usable_alias("Habs")
    assert usable_alias("Sainte-Flanelle")
    assert not usable_alias("CH")  # too short to match safely
    assert not usable_alias("Александр Овечкин")
    assert not usable_alias("Club de hockey Canadien inc 1909")
    assert not usable_alias("one two three four five six seven")


def test_parse_teams_merges_labels_aliases_and_links():
    rows = [
        team_row(
            "Q1",
            "Montreal Canadiens",
            fr="Canadiens de Montréal",
            aliases="Habs|Habs|Montreal Canadiens|Les Glorieux|CH",
            subs="Habs",
            xs="CanadiensMTL|canadiensmtl",
        ),
        team_row("Q1", "duplicate row ignored"),
        {"team": E + "Q2"},  # no label in either language
    ]
    (habs,) = parse_teams(rows)
    assert habs == EntityRecord(
        entity_id="Q1",
        kind=TEAM,
        name="Montreal Canadiens",
        aliases=("Canadiens de Montréal", "Habs", "Les Glorieux"),
        subreddit="Habs",
        x_handle="CanadiensMTL",
        popularity=40,
    )


def test_parse_teams_falls_back_to_the_french_label():
    (team,) = parse_teams([team_row("Q3", "", fr="Alouettes de Montréal", links="x")])
    assert team.name == "Alouettes de Montréal" and team.popularity == 0


def test_pick_rosters_keeps_the_latest_stint_and_the_best_known():
    rows = [
        # A veteran with an undated old stint and a dated recent one: the recent wins.
        player_row("Q10", "Q1", "Veteran Player", links="30"),
        player_row("Q10", "Q2", "Veteran Player", links="30", start="2025-07-01T00:00:00Z"),
        player_row("Q11", "Q1", "Star Player", links="50", start="2020-01-01T00:00:00Z"),
        player_row("Q12", "Q1", "Depth Player", links="5", start="2024-01-01T00:00:00Z"),
        player_row("Q13", "Q2", "", links="9"),  # unnamed
    ]
    picked = pick_rosters(rows, per_team=1)
    assert [(p.entity_id, p.team_id) for p in picked] == [("Q11", "Q1"), ("Q10", "Q2")]
    assert all(p.kind == ATHLETE for p in picked)
    assert pick_rosters(rows, per_team=0) == []


def test_parse_athletes_and_league():
    (a,) = parse_athletes([{"p": E + "Q20", "en": "Ace Server", "links": "70", "xs": "ace"}])
    assert (a.entity_id, a.kind, a.popularity, a.x_handle, a.team_id) == (
        "Q20",
        ATHLETE,
        70,
        "ace",
        None,
    )
    handle, sports = parse_league([{"xs": "NHL", "sports": E + "Q41466"}])
    assert (handle, sports) == ("NHL", ("Q41466",))
    assert parse_league([]) == (None, ())


def test_queries_name_their_inputs():
    assert "wd:Q1215892" in league_query("Q1215892")
    assert "ps:P118 wd:Q1215892" in teams_query("Q1215892")
    roster = roster_query(["Q1", "Q2"], 1986)
    assert "VALUES ?team { wd:Q1 wd:Q2 }" in roster and '"1986-01-01' in roster
    q = athletes_query(AthleteQuery("Q847", "Q6581097", 25), 1986)
    assert "wdt:P641 wd:Q847" in q and "wdt:P21 wd:Q6581097" in q and "LIMIT 25" in q
    assert "P21" not in athletes_query(AthleteQuery("Q5377"), 1986)


def test_fetch_league_reads_teams_rosters_in_batches_and_athletes(monkeypatch):
    monkeypatch.setattr(wikidata, "ROSTER_BATCH", 2)
    client = FakeClient(
        league=[{"xs": "NHL", "sports": E + "Q41466"}],
        teams=[team_row(f"Q{i}", f"Team {i}") for i in range(1, 4)],
        rosters=[player_row("Q10", "Q1", "Some Player", start="2025-01-01T00:00:00Z")],
    )
    league = League("nhl", "NHL", "hockey", wikidata="Q1215892")
    fetched = fetch_league(client, league, per_team=3, this_year=2026)
    assert fetched.x_handle == "NHL" and fetched.sports == ("Q41466",)
    # Three teams, then the same player row from each of the two roster batches: once.
    assert [e.entity_id for e in fetched.entities] == ["Q1", "Q2", "Q3", "Q10"]
    assert sum("p:P54" in q for q in client.queries) == 2
    assert '"1986-01-01' in client.queries[-1]


class TimingOutClient:
    """Roster queries over `max_teams` teams time out, as the NFL's did live."""

    def __init__(self, max_teams, error=None):
        self.max_teams, self.error = max_teams, error or WikidataError("HTTP 504")
        self.sizes: list[int] = []

    def sparql(self, query):
        teams = query.split("VALUES ?team {")[1].split("}")[0].split()
        self.sizes.append(len(teams))
        if len(teams) > self.max_teams:
            raise self.error
        return [player_row(f"Q9{t[4:]}", t[3:], f"Player {t}") for t in teams]


def test_fetch_rosters_halves_a_batch_that_times_out():
    client = TimingOutClient(max_teams=2)
    picked = wikidata.fetch_rosters(client, ["Q1", "Q2", "Q3", "Q4", "Q5"], 1986, 1)
    assert sorted(p.team_id for p in picked) == ["Q1", "Q2", "Q3", "Q4", "Q5"]
    assert client.sizes == [5, 2, 3, 1, 2]


def test_fetch_rosters_gives_up_on_a_single_team_and_never_splits_a_429():
    with pytest.raises(WikidataError):
        wikidata.fetch_rosters(TimingOutClient(max_teams=0), ["Q1", "Q2"], 1986, 1)
    client = TimingOutClient(max_teams=1, error=WikidataRateLimited("429"))
    with pytest.raises(WikidataRateLimited):
        wikidata.fetch_rosters(client, ["Q1", "Q2"], 1986, 1)
    assert client.sizes == [2]


def test_fetch_league_without_players_skips_rosters():
    client = FakeClient(teams=[team_row("Q1", "Team")])
    fetched = fetch_league(client, League("x", "X", "s", wikidata="Q9"), 0, 2026)
    assert isinstance(fetched, LeagueFetch) and fetched.league == "x"
    assert not any("p:P54" in q for q in client.queries)
    assert [e.entity_id for e in fetched.entities] == ["Q1"]


def test_fetch_league_for_an_individual_sport_and_extras():
    doc = {
        "id": "Q30",
        "labels": {"en": {"value": "Extra Club"}},
        "claims": {"P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q476028"}}}}]},
    }
    client = FakeClient(athletes=[{"p": E + "Q20", "en": "Ace Server"}], docs={"Q30": doc})
    league = League("atp", "ATP", "tennis", athletes=AthleteQuery("Q847"), extra=("Q30",))
    fetched = fetch_league(client, league, 6, 2026)
    assert fetched.sports == ("Q847",)
    assert [(e.entity_id, e.kind) for e in fetched.entities] == [("Q20", ATHLETE), ("Q30", TEAM)]


def claim(value):
    return {"mainsnak": {"datavalue": {"value": value}}}


def test_parse_entity_doc_reads_claims():
    doc = {
        "id": "Q50",
        "labels": {"en": {"value": "West Ham United F.C."}, "fr": {"value": "West Ham United"}},
        "aliases": {"en": [{"value": "Hammers"}], "fr": [{"value": "West Ham"}]},
        "claims": {
            "P31": [claim({"id": "Q476028"})],
            "P641": [claim({"id": "Q2736"})],
            "P118": [claim({"id": "Q9448"}), {"mainsnak": {"snaktype": "novalue"}}],
            "P3984": [claim("Hammers")],
            "P2002": [claim("WestHam")],
        },
        "sitelinks": {"enwiki": {}, "frwiki": {}},
    }
    record, facts = parse_entity_doc(doc)
    assert record == EntityRecord(
        entity_id="Q50",
        kind=TEAM,
        name="West Ham United F.C.",
        aliases=("West Ham United", "Hammers", "West Ham"),
        subreddit="Hammers",
        x_handle="WestHam",
        popularity=2,
    )
    assert facts.leagues == frozenset({"Q9448"}) and not facts.is_human
    assert parse_entity_doc({"id": "Q1", "missing": ""}) is None
    assert parse_entity_doc({"id": "Q1", "labels": {}}) is None


def test_parse_entity_doc_treats_humans_as_athletes_without_a_subreddit():
    doc = {
        "id": "Q60",
        "labels": {"en": {"value": "Ivan Rookie"}},
        "claims": {"P31": [claim({"id": "Q5"})], "P3984": [claim("fanclub")]},
    }
    record, facts = parse_entity_doc(doc)
    assert record.kind == ATHLETE and record.subreddit is None and facts.is_human


def test_get_entities_and_search_ids():
    doc = {"id": "Q70", "labels": {"en": {"value": "Some Club"}}}
    client = FakeClient(docs={"Q70": doc})
    assert list(get_entities(client, ["Q70"])) == ["Q70"]
    assert search_ids(client, "Some Club") == ["Q70"]


# --- transport --------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status, body):
        self.status, self.body = status, body

    def read(self):
        return self.body


class FakeConnection:
    # Class-level on purpose: the client builds a new connection per call, and the
    # `https` fixture resets both lists for each test.
    responses: ClassVar[list] = []
    requests: ClassVar[list] = []

    def __init__(self, host, timeout):
        self.host = host

    def request(self, method, path, headers):
        FakeConnection.requests.append((self.host, path, headers))

    def getresponse(self):
        answer = FakeConnection.responses.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def close(self):
        pass


@pytest.fixture
def https(monkeypatch):
    FakeConnection.responses, FakeConnection.requests = [], []
    monkeypatch.setattr(wikidata.http.client, "HTTPSConnection", FakeConnection)
    return FakeConnection


def test_client_sends_a_contactable_user_agent_and_paces(https):
    body = json.dumps({"results": {"bindings": [{"x": {"value": "1"}}]}}).encode()
    https.responses = [FakeResponse(200, body), FakeResponse(200, b'{"search": []}')]
    pauses = []
    client = WikidataClient(sleep=pauses.append, pause_s=2.0)
    assert client.sparql("SELECT 1") == [{"x": "1"}]
    assert client.api(action="wbsearchentities", search="a") == {"search": []}
    (host1, path1, headers), (host2, path2, _) = https.requests
    assert host1 == wikidata.SPARQL_HOST and path1.startswith("/sparql?query=SELECT")
    assert host2 == wikidata.API_HOST and "format=json" in path2
    # Wikimedia answered 429 to every query from a user agent without a URL (2026-10-04).
    assert "https://" in headers["User-Agent"]
    assert pauses == [2.0]  # the first call does not wait


def test_client_errors(https):
    https.responses = [
        FakeResponse(429, b""),
        FakeResponse(500, b""),
        FakeResponse(200, b"<html>"),
        OSError("reset"),
    ]
    client = WikidataClient(sleep=lambda s: None)
    with pytest.raises(WikidataRateLimited):
        client.sparql("q")
    with pytest.raises(WikidataError, match="HTTP 500"):
        client.sparql("q")
    with pytest.raises(WikidataError, match="non-JSON"):
        client.sparql("q")
    with pytest.raises(WikidataError, match="reset"):
        client.sparql("q")
