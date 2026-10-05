"""Teams and athletes per league, from Wikidata. No key, no LLM, refreshed weekly.

What each query returns, and how well it held up when run live (2026-10-04):

| query           | source                                              | quality              |
| --------------- | --------------------------------------------------- | -------------------- |
| `league_query`  | the league's X handle (P2002) and sport (P641)      | exact                |
| `teams_query`   | teams whose "league" (P118) has no end date         | NHL clean; the EPL   |
|                 | (P582), not dissolved (P576), a sports team/club    | lagged a promotion;  |
|                 | within two subclass steps, not a human              | FBS 106 of ~134      |

The team query's shape is measured, not stylistic. Players carry "league" claims too
(thousands for the NFL), and an unbounded `wdt:P279*` walk over each of them made the
NFL and NBA queries hit the endpoint's 60 s limit (HTTP 504). Excluding humans first and
bounding the walk at two steps answered every listed league, the slowest (NBA) in ~20 s;
three steps timed the NBA out again.
| `roster_query`  | athletes whose "member of sports team" (P54) is one | stints often lack an |
|                 | of those teams, alive, born in the last 40 years    | end date, see below  |
| `athletes_query`| individual sports: top athletes of a sport (P641)   | some celebrity noise |
|                 | by sitelink count                                   | (a singer who golfs) |

A team's names come with it: English and French labels and aliases ("Habs",
"Glorieux", "Sainte-Flanelle"), its subreddit (P3984) and X handle (P2002).

**Stints without an end date** put a veteran on every team they ever played for, so
`pick_rosters` keeps each athlete on the team of their *latest* stint (by start date,
P580), then the `per_team` most-linked athletes of each team. Season items would be the
exact source, but on 2026-10-04 none of the NHL, NBA, NFL, CFL, UCL or KHL seasons
listed their teams (P1923), so they are not used. What the queries miss, discovery
(`sports.discover`) and the league's `extra` list in the TOML fill in.

Every function but `WikidataClient`'s transport is pure: queries are strings, results
are parsed from the SPARQL JSON bindings.
"""

from __future__ import annotations

import http.client
import json
import logging
import re
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from social_scraper.sports.leagues import AthleteQuery, League

log = logging.getLogger(__name__)

SPARQL_HOST = "query.wikidata.org"
API_HOST = "www.wikidata.org"
# Wikimedia's policy wants a client name plus a way to reach its operator, and enforces
# it: without the URL every query came back 429 (measured 2026-10-04), with it they
# passed. The project URL is the contact; no personal address is sent.
USER_AGENT = (
    "social-scraper/0.1 (https://github.com/alexandrec90/social-scraper; weekly sports sync)"
)
TIMEOUT_S = 90
# Teams per roster query: 16 NHL teams answered in 3 s live; a whole league at once
# timed out at the endpoint's 60 s limit.
ROSTER_BATCH = 15
# Between queries, so a weekly sync of every league is a trickle, not a burst.
PAUSE_S = 1.0
# Athletes older than this are retired far more often than not.
MAX_ATHLETE_AGE = 40
TEAM = "team"
ATHLETE = "athlete"
_ENTITY_URI = re.compile(r"/entity/(Q\d+)$")
_LATIN = re.compile(r"[A-Za-z]")


class WikidataError(OSError):
    """Wikidata answered with an error, or not at all."""


class WikidataRateLimited(WikidataError):
    """HTTP 429: stop asking for this cycle rather than try the next league."""


@dataclass(frozen=True)
class EntityRecord:
    """One team or athlete as Wikidata describes it."""

    entity_id: str
    kind: str
    name: str
    aliases: tuple[str, ...] = ()
    subreddit: str | None = None
    x_handle: str | None = None
    team_id: str | None = None
    popularity: int = 0


@dataclass
class LeagueFetch:
    league: str
    entities: list[EntityRecord] = field(default_factory=list)
    x_handle: str | None = None
    sports: tuple[str, ...] = ()


# --- transport ------------------------------------------------------------------------


class WikidataClient:
    """The two endpoints the sync needs, over HTTPS only. `sleep` paces the calls."""

    def __init__(self, sleep: Callable[[float], None] = time.sleep, pause_s: float = PAUSE_S):
        self.sleep = sleep
        self.pause_s = pause_s
        self.calls = 0

    def _get(self, host: str, path: str) -> Any:
        if self.calls:
            self.sleep(self.pause_s)
        self.calls += 1
        conn = http.client.HTTPSConnection(host, timeout=TIMEOUT_S)
        try:
            conn.request(
                "GET", path, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
            )
            resp = conn.getresponse()
            body = resp.read()
        except (OSError, http.client.HTTPException) as exc:
            raise WikidataError(f"{host}: {type(exc).__name__}: {exc}") from exc
        finally:
            conn.close()
        if resp.status == 429:
            raise WikidataRateLimited(f"{host} answered HTTP 429")
        if resp.status != 200:
            raise WikidataError(f"{host} answered HTTP {resp.status}")
        try:
            return json.loads(body)
        except ValueError as exc:
            raise WikidataError(f"{host} answered non-JSON") from exc

    def sparql(self, query: str) -> list[dict[str, str]]:
        """Rows of `{variable: value}`; URIs and literals alike are plain strings."""
        data = self._get(SPARQL_HOST, "/sparql?" + urlencode({"query": query, "format": "json"}))
        return bindings(data)

    def api(self, **params: str) -> Any:
        return self._get(API_HOST, "/w/api.php?" + urlencode({**params, "format": "json"}))


def bindings(data: Any) -> list[dict[str, str]]:
    rows = (data or {}).get("results", {}).get("bindings", [])
    return [{k: v.get("value", "") for k, v in row.items()} for row in rows]


# --- queries --------------------------------------------------------------------------


def league_query(qid: str) -> str:
    return f"""
SELECT (GROUP_CONCAT(DISTINCT ?x; separator="|") AS ?xs)
       (GROUP_CONCAT(DISTINCT ?sport; separator="|") AS ?sports)
WHERE {{
  OPTIONAL {{ wd:{qid} wdt:P2002 ?x }}
  OPTIONAL {{ wd:{qid} wdt:P641 ?sport }}
}}"""


def teams_query(qid: str) -> str:
    return f"""
SELECT ?team ?en ?fr ?links
  (GROUP_CONCAT(DISTINCT ?sub; separator="|") AS ?subs)
  (GROUP_CONCAT(DISTINCT ?x; separator="|") AS ?xs)
  (GROUP_CONCAT(DISTINCT ?alias; separator="|") AS ?aliases)
WHERE {{
  ?team p:P118 ?st . ?st ps:P118 wd:{qid} .
  FILTER NOT EXISTS {{ ?st pq:P582 ?end }}
  FILTER NOT EXISTS {{ ?team wdt:P576 ?dissolved }}
  MINUS {{ ?team wdt:P31 wd:Q5 }}
  ?team wdt:P31/wdt:P279?/wdt:P279? ?class .
  VALUES ?class {{ wd:Q12973014 wd:Q847017 }}
  ?team wikibase:sitelinks ?links .
  OPTIONAL {{ ?team rdfs:label ?en FILTER(LANG(?en) = "en") }}
  OPTIONAL {{ ?team rdfs:label ?fr FILTER(LANG(?fr) = "fr") }}
  OPTIONAL {{ ?team wdt:P3984 ?sub }}
  OPTIONAL {{ ?team wdt:P2002 ?x }}
  OPTIONAL {{ ?team skos:altLabel ?alias FILTER(LANG(?alias) IN ("en", "fr")) }}
}}
GROUP BY ?team ?en ?fr ?links"""


def _athlete_fields() -> str:
    return """
  ?p wdt:P31 wd:Q5 .
  FILTER NOT EXISTS { ?p wdt:P570 ?died }
  ?p wdt:P569 ?born . FILTER(?born >= ?cutoff)
  ?p wikibase:sitelinks ?links .
  OPTIONAL { ?p rdfs:label ?en FILTER(LANG(?en) = "en") }
  OPTIONAL { ?p rdfs:label ?fr FILTER(LANG(?fr) = "fr") }
  OPTIONAL { ?p wdt:P2002 ?x }
  OPTIONAL { ?p skos:altLabel ?alias FILTER(LANG(?alias) IN ("en", "fr")) }"""


def roster_query(team_ids: Sequence[str], born_after: int) -> str:
    values = " ".join(f"wd:{t}" for t in team_ids)
    return f"""
SELECT ?p ?team ?start ?en ?fr ?links
  (GROUP_CONCAT(DISTINCT ?x; separator="|") AS ?xs)
  (GROUP_CONCAT(DISTINCT ?alias; separator="|") AS ?aliases)
WHERE {{
  VALUES ?team {{ {values} }}
  BIND("{born_after}-01-01T00:00:00Z"^^xsd:dateTime AS ?cutoff)
  ?p p:P54 ?ms . ?ms ps:P54 ?team .
  FILTER NOT EXISTS {{ ?ms pq:P582 ?left }}
  OPTIONAL {{ ?ms pq:P580 ?start }}{_athlete_fields()}
}}
GROUP BY ?p ?team ?start ?en ?fr ?links"""


def athletes_query(spec: AthleteQuery, born_after: int) -> str:
    sex = f"?p wdt:P21 wd:{spec.sex} ." if spec.sex else ""
    return f"""
SELECT ?p ?en ?fr ?links
  (GROUP_CONCAT(DISTINCT ?x; separator="|") AS ?xs)
  (GROUP_CONCAT(DISTINCT ?alias; separator="|") AS ?aliases)
WHERE {{
  {{
    SELECT ?p ?links WHERE {{
      BIND("{born_after}-01-01T00:00:00Z"^^xsd:dateTime AS ?cutoff)
      ?p wdt:P641 wd:{spec.sport} .
      {sex}
      ?p wdt:P31 wd:Q5 .
      FILTER NOT EXISTS {{ ?p wdt:P570 ?died }}
      ?p wdt:P569 ?born . FILTER(?born >= ?cutoff)
      ?p wikibase:sitelinks ?links . FILTER(?links >= 5)
    }}
    ORDER BY DESC(?links)
    LIMIT {spec.limit}
  }}
  OPTIONAL {{ ?p rdfs:label ?en FILTER(LANG(?en) = "en") }}
  OPTIONAL {{ ?p rdfs:label ?fr FILTER(LANG(?fr) = "fr") }}
  OPTIONAL {{ ?p wdt:P2002 ?x }}
  OPTIONAL {{ ?p skos:altLabel ?alias FILTER(LANG(?alias) IN ("en", "fr")) }}
}}
GROUP BY ?p ?en ?fr ?links"""


# --- parsing --------------------------------------------------------------------------


def qid_of(uri: str) -> str | None:
    m = _ENTITY_URI.search(uri or "")
    return m.group(1) if m else None


def _split(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split("|") if v.strip()]


def usable_alias(alias: str) -> bool:
    """A name worth matching: Latin script, 3-60 characters, at most six words.

    Drops the Cyrillic spellings, corporate names and full legal names Wikidata keeps
    as aliases, which nobody types in a game thread.
    """
    return (
        3 <= len(alias) <= 60
        and bool(_LATIN.search(alias))
        and len(alias.split()) <= 6
        and not any(ch.isdigit() for ch in alias)
    )


def _names(row: dict[str, str]) -> tuple[str, tuple[str, ...]] | None:
    """`(display name, aliases)`: English label first, French label as an alias."""
    name = row.get("en") or row.get("fr")
    if not name:
        return None
    seen = {name.casefold()}
    aliases: list[str] = []
    for alias in [row.get("fr") or "", *_split(row.get("aliases"))]:
        if alias and usable_alias(alias) and alias.casefold() not in seen:
            seen.add(alias.casefold())
            aliases.append(alias)
    return name, tuple(aliases)


def _first(values: list[str]) -> str | None:
    return sorted(values, key=str.lower)[0] if values else None


def _int(value: str | None) -> int:
    try:
        return int(value or 0)
    except ValueError:
        return 0


def parse_teams(rows: Iterable[dict[str, str]]) -> list[EntityRecord]:
    out: dict[str, EntityRecord] = {}
    for row in rows:
        qid, names = qid_of(row.get("team", "")), _names(row)
        if qid is None or names is None or qid in out:
            continue
        out[qid] = EntityRecord(
            entity_id=qid,
            kind=TEAM,
            name=names[0],
            aliases=names[1],
            subreddit=_first(_split(row.get("subs"))),
            x_handle=_first(_split(row.get("xs"))),
            popularity=_int(row.get("links")),
        )
    return list(out.values())


def parse_athletes(rows: Iterable[dict[str, str]]) -> list[EntityRecord]:
    out: dict[str, EntityRecord] = {}
    for row in rows:
        qid, names = qid_of(row.get("p", "")), _names(row)
        if qid is None or names is None or qid in out:
            continue
        out[qid] = EntityRecord(
            entity_id=qid,
            kind=ATHLETE,
            name=names[0],
            aliases=names[1],
            x_handle=_first(_split(row.get("xs"))),
            popularity=_int(row.get("links")),
        )
    return list(out.values())


def pick_rosters(rows: Iterable[dict[str, str]], per_team: int) -> list[EntityRecord]:
    """Each athlete on their latest stint's team, then the `per_team` best-known per team.

    A stint with no start date ranks below any dated one: an undated stint is usually an
    old one nobody back-filled.
    """
    latest: dict[str, tuple[str, dict[str, str]]] = {}
    for row in rows:
        qid, team = qid_of(row.get("p", "")), qid_of(row.get("team", ""))
        if qid is None or team is None:
            continue
        start = row.get("start") or ""
        if qid not in latest or start > latest[qid][0]:
            latest[qid] = (start, row)
    by_team: dict[str, list[EntityRecord]] = {}
    for qid, (_, row) in latest.items():
        names = _names(row)
        team = qid_of(row["team"])
        if names is None or team is None:
            continue
        by_team.setdefault(team, []).append(
            EntityRecord(
                entity_id=qid,
                kind=ATHLETE,
                name=names[0],
                aliases=names[1],
                x_handle=_first(_split(row.get("xs"))),
                team_id=team,
                popularity=_int(row.get("links")),
            )
        )
    out: list[EntityRecord] = []
    for team in sorted(by_team):
        ranked = sorted(by_team[team], key=lambda e: (-e.popularity, e.entity_id))
        out.extend(ranked[:per_team])
    return out


def parse_league(rows: list[dict[str, str]]) -> tuple[str | None, tuple[str, ...]]:
    """`(X handle, sport QIDs)` of the league itself."""
    row = rows[0] if rows else {}
    sports = tuple(q for q in (qid_of(s) for s in _split(row.get("sports"))) if q)
    return _first(_split(row.get("xs"))), sports


# --- single entities (the `extra` list, and discovery) ------------------------------------

# `wbgetentities` takes at most 50 ids per call.
ENTITY_BATCH = 50
HUMAN = "Q5"


@dataclass(frozen=True)
class EntityFacts:
    """The claims discovery decides on: what the item is, its sports, its leagues."""

    classes: frozenset[str] = frozenset()
    sports: frozenset[str] = frozenset()
    leagues: frozenset[str] = frozenset()

    @property
    def is_human(self) -> bool:
        return HUMAN in self.classes


def _claim_values(claims: dict[str, Any], prop: str) -> list[Any]:
    out = []
    for claim in claims.get(prop) or []:
        value = ((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value")
        if isinstance(value, dict):
            value = value.get("id")
        if isinstance(value, str) and value:
            out.append(value)
    return out


def parse_entity_doc(doc: dict[str, Any]) -> tuple[EntityRecord, EntityFacts] | None:
    """One `wbgetentities` document -> record and facts, or None if missing/unnamed."""
    qid = doc.get("id")
    if not isinstance(qid, str) or "missing" in doc:
        return None
    labels, aliases = doc.get("labels") or {}, doc.get("aliases") or {}
    row = {
        "en": (labels.get("en") or {}).get("value", ""),
        "fr": (labels.get("fr") or {}).get("value", ""),
        "aliases": "|".join(
            a.get("value", "") for lang in ("en", "fr") for a in aliases.get(lang) or []
        ),
    }
    names = _names(row)
    if names is None:
        return None
    claims = doc.get("claims") or {}
    facts = EntityFacts(
        classes=frozenset(_claim_values(claims, "P31")),
        sports=frozenset(_claim_values(claims, "P641")),
        leagues=frozenset(_claim_values(claims, "P118")),
    )
    record = EntityRecord(
        entity_id=qid,
        kind=ATHLETE if facts.is_human else TEAM,
        name=names[0],
        aliases=names[1],
        subreddit=_first(_claim_values(claims, "P3984")) if not facts.is_human else None,
        x_handle=_first(_claim_values(claims, "P2002")),
        popularity=len(doc.get("sitelinks") or {}),
    )
    return record, facts


def get_entities(
    client: WikidataClient, qids: Sequence[str]
) -> dict[str, tuple[EntityRecord, EntityFacts]]:
    out: dict[str, tuple[EntityRecord, EntityFacts]] = {}
    for i in range(0, len(qids), ENTITY_BATCH):
        data = client.api(
            action="wbgetentities",
            ids="|".join(qids[i : i + ENTITY_BATCH]),
            props="labels|aliases|claims|sitelinks",
            languages="en|fr",
        )
        for doc in ((data or {}).get("entities") or {}).values():
            parsed = parse_entity_doc(doc)
            if parsed is not None:
                out[parsed[0].entity_id] = parsed
    return out


def search_ids(client: WikidataClient, name: str, limit: int = 5) -> list[str]:
    """QIDs whose label or alias matches `name`, best first."""
    data = client.api(
        action="wbsearchentities", search=name, language="en", type="item", limit=str(limit)
    )
    return [h["id"] for h in (data or {}).get("search") or [] if isinstance(h.get("id"), str)]


# --- the per-league fetch ---------------------------------------------------------------


def fetch_rosters(
    client: WikidataClient, team_ids: Sequence[str], born_after: int, per_team: int
) -> list[EntityRecord]:
    """`pick_rosters` for `team_ids`, halving the batch whenever a query times out.

    NFL teams carry ~300 player stints each with no end date: 8 teams took 43 s live and
    15 hit the 60 s limit (2026-10-04), while 4 took 9 s. A 429 is not split: it means
    stop, not smaller.
    """
    try:
        rows = client.sparql(roster_query(team_ids, born_after))
    except WikidataRateLimited:
        raise
    except WikidataError:
        if len(team_ids) == 1:
            raise
        mid = len(team_ids) // 2
        return [
            *fetch_rosters(client, team_ids[:mid], born_after, per_team),
            *fetch_rosters(client, team_ids[mid:], born_after, per_team),
        ]
    return pick_rosters(rows, per_team)


def fetch_league(
    client: WikidataClient, league: League, per_team: int, this_year: int
) -> LeagueFetch:
    """Everything Wikidata knows about `league`'s entities. Raises `WikidataError`."""
    born_after = this_year - MAX_ATHLETE_AGE
    out = LeagueFetch(league.key)
    if league.wikidata:
        out.x_handle, out.sports = parse_league(client.sparql(league_query(league.wikidata)))
        teams = parse_teams(client.sparql(teams_query(league.wikidata)))
        out.entities.extend(teams)
        ids = [t.entity_id for t in teams]
        if per_team > 0:
            for i in range(0, len(ids), ROSTER_BATCH):
                batch = ids[i : i + ROSTER_BATCH]
                out.entities.extend(fetch_rosters(client, batch, born_after, per_team))
    if league.athletes:
        out.sports = tuple(dict.fromkeys((*out.sports, league.athletes.sport)))
        out.entities.extend(
            parse_athletes(client.sparql(athletes_query(league.athletes, born_after)))
        )
    if league.extra:
        out.entities.extend(record for record, _ in get_entities(client, league.extra).values())
    seen: set[str] = set()
    unique = []
    for e in out.entities:
        if e.entity_id not in seen:
            seen.add(e.entity_id)
            unique.append(e)
    out.entities = unique
    return out
