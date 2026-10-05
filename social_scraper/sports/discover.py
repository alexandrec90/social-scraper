"""Teams and athletes the weekly sync missed, found from what people write.

1. **Count.** Every on-topic post contributes its unmatched capitalised two- and
   three-word runs ("Ivan Demidov", "West Ham") to `sports_name_candidates`, per league.
2. **Look up.** A name seen `MIN_SIGHTINGS` times is searched on Wikidata
   (`wbsearchentities`), a few names per cycle.
3. **Confirm or reject** by Wikidata's own claims, never by guesswork: an athlete is a
   human whose sport (P641) is one of the league's sports; a team is a non-human whose
   league (P118, at any time) is the league. A confirmed name becomes an entity with a
   `discovered` membership; a rejected one is remembered so it is not searched again.
   A name seen in several leagues' posts joins the first (alphabetically) that
   accepts it -- for r/soccer, possibly not the athlete's own; the matcher only needs
   *a* league to resolve one-word mentions in context.

Candidates unseen for `CANDIDATE_RETENTION` are deleted, confirmed or not: they are
names out of other people's posts, kept only as long as they are useful.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.engine import Connection, Engine

from social_scraper.db import repository, schema
from social_scraper.db.repository import dialect_insert
from social_scraper.sports import registry
from social_scraper.sports.matcher import STOP, Matcher, tokens
from social_scraper.sports.wikidata import (
    EntityFacts,
    EntityRecord,
    WikidataClient,
    WikidataError,
    get_entities,
    search_ids,
)

log = logging.getLogger(__name__)

PENDING = "pending"
CONFIRMED = "confirmed"
REJECTED = "rejected"
MIN_SIGHTINGS = 5
CANDIDATE_RETENTION = timedelta(days=30)
MAX_NAMES_PER_POST = 10
# A post is counted toward at most this many leagues (r/soccer maps to a dozen).
MAX_LEAGUES_PER_NAME = 3
SEARCH_RESULTS = 5
LEAGUE_STATE_PREFIX = "sports:league:"
# Punctuation ends a name: "Arsenal. Bukayo Saka" is two runs, not one of three.
_CLAUSE_BREAK = re.compile(r"[.!?,;:()\[\]\"\n|/]+")


def _capitalised(tok: str) -> bool:
    return len(tok) >= 2 and tok[0].isupper() and any(c.islower() for c in tok[1:])


def candidate_names(text: str, matcher: Matcher) -> list[str]:
    """Two- and three-word capitalised runs no entity already answers to. Pure.

    Longer runs are title case ("Game Thread Canadiens At Leafs"), not names, and are
    skipped whole. A run whose last word is a known one-word name (a surname, a
    nickname) is a known entity spelled out, so it is not a candidate either.
    """
    out: dict[str, None] = {}
    for clause in _CLAUSE_BREAK.split(text):
        run: list[str] = []
        for tok in [*tokens(clause), ""]:
            if tok and _capitalised(tok) and tok.isalpha():
                run.append(tok)
                continue
            if 2 <= len(run) <= 3 and run[0].lower() not in STOP:
                if not (matcher.knows(run) or matcher.knows(run[-1:])):
                    out[" ".join(run)] = None
            run = []
    return list(out)[:MAX_NAMES_PER_POST]


def count_candidates(
    conn: Connection, names: Iterable[str], leagues: Iterable[str], now: datetime
) -> None:
    t = schema.sports_name_candidates
    for league in sorted(leagues)[:MAX_LEAGUES_PER_NAME]:
        for name in names:
            stmt = dialect_insert(conn, t).values(
                name=name[:128],
                league=league,
                sightings=1,
                status=PENDING,
                first_seen_at=now,
                last_seen_at=now,
            )
            conn.execute(
                stmt.on_conflict_do_update(
                    index_elements=["name", "league"],
                    set_={"sightings": t.c.sightings + 1, "last_seen_at": now},
                )
            )


def due_lookups(conn: Connection, limit: int) -> list[tuple[str, list[str]]]:
    """`(name, leagues)` of the most-seen pending names, at most `limit` names."""
    t = schema.sports_name_candidates
    total = func.sum(t.c.sightings).label("total")
    names = [
        r.name
        for r in conn.execute(
            select(t.c.name, total)
            .where(t.c.status == PENDING)
            .group_by(t.c.name)
            .having(total >= MIN_SIGHTINGS)
            .order_by(total.desc(), t.c.name)
            .limit(limit)
        )
    ]
    out = []
    for name in names:
        leagues: list[str] = sorted(
            conn.execute(
                select(t.c.league).where(and_(t.c.name == name, t.c.status == PENDING))
            ).scalars()
        )
        out.append((name, leagues))
    return out


@dataclass(frozen=True)
class LeagueFacts:
    """What confirmation checks a candidate against: the league's QID and sports."""

    qid: str | None
    sports: frozenset[str]


def save_league_facts(conn: Connection, league: str, facts: LeagueFacts, now: datetime) -> None:
    value = "|".join([facts.qid or "", *sorted(facts.sports)])
    repository.set_state(conn, LEAGUE_STATE_PREFIX + league, value, now)


def load_league_facts(conn: Connection) -> dict[str, LeagueFacts]:
    out = {}
    for league, value in repository.states_with_prefix(conn, LEAGUE_STATE_PREFIX).items():
        qid, *sports = value.split("|")
        out[league] = LeagueFacts(qid or None, frozenset(s for s in sports if s))
    return out


def accepts(record: EntityRecord, facts: EntityFacts, league: LeagueFacts) -> bool:
    """Wikidata's claims say this item belongs in the league. Pure."""
    if facts.is_human:
        return bool(facts.sports & league.sports)
    return league.qid is not None and league.qid in facts.leagues


def resolve(
    client: WikidataClient, name: str, leagues: Sequence[str], facts: dict[str, LeagueFacts]
) -> tuple[EntityRecord, str] | None:
    """The first search hit some candidate league accepts, with that league."""
    hits = search_ids(client, name, SEARCH_RESULTS)
    if not hits:
        return None
    docs = get_entities(client, hits)
    for qid in hits:
        if qid not in docs:
            continue
        record, item = docs[qid]
        for league in leagues:
            if league in facts and accepts(record, item, facts[league]):
                return record, league
    return None


@dataclass
class DiscoveryStats:
    looked_up: int = 0
    confirmed: list[str] = field(default_factory=list)
    rejected: int = 0
    pruned: int = 0
    error: str | None = None


def run_discovery(
    engine: Engine, client: WikidataClient, now: datetime, limit: int
) -> DiscoveryStats:
    """Look up to `limit` names, record each verdict, prune stale candidates."""
    stats = DiscoveryStats()
    t = schema.sports_name_candidates
    with engine.begin() as conn:
        stats.pruned = int(
            conn.execute(delete(t).where(t.c.last_seen_at < now - CANDIDATE_RETENTION)).rowcount
            or 0
        )
        due = due_lookups(conn, limit) if limit > 0 else []
        facts = load_league_facts(conn)
    for name, leagues in due:
        try:
            found = resolve(client, name, leagues, facts)
        except WikidataError as exc:
            stats.error = str(exc)
            log.warning("discovery lookup failed, stopping for this cycle: %s", exc)
            break
        stats.looked_up += 1
        with engine.begin() as conn:
            where = and_(t.c.name == name, t.c.status == PENDING)
            if found is None:
                conn.execute(update(t).where(where).values(status=REJECTED, checked_at=now))
                stats.rejected += 1
                continue
            record, league = found
            registry.upsert_entity(conn, record, registry.DISCOVERED, now)
            registry.set_membership(conn, record.entity_id, league, registry.DISCOVERED, now)
            registry.refresh_active(conn, now)
            conn.execute(
                update(t)
                .where(where)
                .values(status=CONFIRMED, checked_at=now, entity_id=record.entity_id)
            )
            stats.confirmed.append(f"{record.name} ({league})")
    return stats
