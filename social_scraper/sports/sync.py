"""Keep `sports_entities` current: each league re-read from Wikidata once it is stale.

Staleness is per league (`sports:wikidata:<key>` in `crawl_state`), so a league added
to the TOML is fetched on the next cycle and the rest wait for their week. A cycle
syncs at most `SPORTS_SYNC_LEAGUES_PER_CYCLE` leagues: one took 35-110 s live
(2026-10-04; the whole list, ten minutes), so the first sync is spread over a few cycles
instead of holding one up, and the weekly refreshes stay spread the same way.

A league that fails keeps its previous entities -- an unreachable Wikidata degrades the
matcher's freshness, never the crawl -- and is retried next cycle. A 429 stops the
sync for the cycle.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.engine import Engine

from social_scraper.db import repository
from social_scraper.sports import discover, registry
from social_scraper.sports.leagues import League
from social_scraper.sports.wikidata import (
    WikidataClient,
    WikidataError,
    WikidataRateLimited,
    fetch_league,
)

log = logging.getLogger(__name__)

SYNCED_PREFIX = "sports:wikidata:"
# A discovered athlete nobody has named for this long leaves the matcher.
DISCOVERED_RETENTION = timedelta(days=60)


@dataclass
class SyncStats:
    synced: dict[str, int] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    retired: int = 0


def stale_leagues(
    leagues: tuple[League, ...], synced: dict[str, str], now: datetime, max_age: timedelta
) -> list[League]:
    """Leagues never synced, or synced longer than `max_age` ago, never-synced first. Pure."""

    def age(league: League) -> timedelta:
        last = synced.get(league.key)
        return now - datetime.fromisoformat(last) if last else timedelta.max

    due = [lg for lg in leagues if age(lg) >= max_age]
    return sorted(due, key=age, reverse=True)


@dataclass(frozen=True)
class SyncPolicy:
    """How stale a league may get, how many athletes per team, how many leagues a call."""

    max_age: timedelta
    players_per_team: int
    # None syncs every stale league (`sports-sync`); a cycle passes its per-cycle cap.
    limit: int | None = None


def sync_leagues(
    engine: Engine,
    client: WikidataClient,
    leagues: tuple[League, ...],
    now: datetime,
    policy: SyncPolicy,
) -> SyncStats:
    """Re-read up to `policy.limit` stale leagues, stalest first."""
    stats = SyncStats()
    with engine.connect() as conn:
        synced = repository.states_with_prefix(conn, SYNCED_PREFIX)
    for league in stale_leagues(leagues, synced, now, policy.max_age)[: policy.limit]:
        per_team = league.players if league.players is not None else policy.players_per_team
        try:
            fetched = fetch_league(client, league, per_team, now.year)
        except WikidataRateLimited as exc:
            stats.errors[league.key] = f"{exc}; the rest wait for the next cycle"
            log.warning("wikidata rate-limited the sync at %s; stopping", league.key)
            break
        except WikidataError as exc:
            stats.errors[league.key] = str(exc)
            log.warning("wikidata sync of %s failed: %s", league.key, exc)
            continue
        with engine.begin() as conn:
            stats.synced[league.key] = registry.replace_league(
                conn, league.key, fetched.entities, now
            )
            facts = discover.LeagueFacts(league.wikidata, frozenset(fetched.sports))
            discover.save_league_facts(conn, league.key, facts, now)
            if fetched.x_handle:
                repository.set_state(conn, f"sports:league_x:{league.key}", fetched.x_handle, now)
            repository.set_state(conn, SYNCED_PREFIX + league.key, now.isoformat(), now)
    with engine.begin() as conn:
        stats.retired = registry.retire_discovered(conn, now - DISCOVERED_RETENTION, now)
    return stats


def league_handles(engine: Engine, leagues: tuple[League, ...]) -> list[str]:
    """The listed leagues' own X accounts, as Wikidata gives them: always-trusted seeds."""
    keys = {lg.key for lg in leagues}
    with engine.connect() as conn:
        handles = repository.states_with_prefix(conn, "sports:league_x:")
    return sorted({h for key, h in handles.items() if key in keys})
