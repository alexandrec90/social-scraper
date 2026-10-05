"""The league list: what the sports domain covers, maintained by hand in TOML.

One `[[league]]` table per league Mise-o-jeu+ offers. Each says where its chatter lives
and where its entities come from:

| key               | meaning                                                          |
| ----------------- | ---------------------------------------------------------------- |
| `key`             | short id, `[a-z0-9_]`, stored on every row (`nhl`, `epl`)        |
| `name`, `sport`   | labels for people                                                |
| `wikidata`        | the league's QID: its teams come from it, and their rosters     |
| `athletes`        | individual sports: `{ sport = "Q847", sex = "Q6581097", limit }` |
| `mise_o_jeu`      | Mise-o-jeu+'s own labels for it, `"SPORT|League"` or `"League"`  |
| `subreddits`      | league/sport subreddits, always in the rotation                  |
| `team_subreddits` | also rotate through each team's subreddit (Wikidata P3984)       |
| `x_queries`       | generic X searches for this league                               |
| `players`         | athletes kept per team (default `SPORTS_PLAYERS_PER_TEAM`)       |
| `extra`           | QIDs Wikidata's league query misses: always in the league        |
| `covers`          | league keys whose entities play here too: a cup's clubs come     |
|                   | from domestic leagues (`ucl` covers `epl`, `laliga`, ...)        |

`scripts/check-leagues.py` compares the `mise_o_jeu` labels against what the operator
has actually seen on Mise-o-jeu+, so a league missing here shows up as a failure.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_KEY = re.compile(r"^[a-z0-9_]{1,32}$")
_QID = re.compile(r"^Q\d+$")
_SUBREDDIT = re.compile(r"^[A-Za-z0-9_]{2,21}$")


class LeagueConfigError(ValueError):
    """The league file is unreadable or a league in it is malformed."""


@dataclass(frozen=True)
class AthleteQuery:
    """Top athletes of a sport on Wikidata, by how many Wikipedias have an article."""

    sport: str
    sex: str | None = None
    limit: int = 150


@dataclass(frozen=True)
class League:
    key: str
    name: str
    sport: str
    wikidata: str | None = None
    athletes: AthleteQuery | None = None
    mise_o_jeu: tuple[str, ...] = ()
    subreddits: tuple[str, ...] = ()
    team_subreddits: bool = False
    x_queries: tuple[str, ...] = ()
    players: int | None = None
    extra: tuple[str, ...] = ()
    covers: tuple[str, ...] = ()

    @property
    def context(self) -> frozenset[str]:
        """The leagues a post about this league may name entities of."""
        return frozenset({self.key, *self.covers})


def _strings(raw: dict[str, Any], field: str, where: str) -> tuple[str, ...]:
    value = raw.get(field, [])
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise LeagueConfigError(f"{where}: `{field}` must be a list of non-empty strings")
    return tuple(v.strip() for v in value)


def _qid(value: Any, field: str, where: str) -> str:
    if not isinstance(value, str) or not _QID.match(value):
        raise LeagueConfigError(f"{where}: `{field}` must be a Wikidata id like Q1215892")
    return value


def parse_athletes(spec: Any, where: str) -> AthleteQuery | None:
    """The `athletes = { sport, sex, limit }` table, or None when absent."""
    if spec is None:
        return None
    if not isinstance(spec, dict):
        raise LeagueConfigError(f"{where}: `athletes` must be a table")
    limit = spec.get("limit", 150)
    if not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise LeagueConfigError(f"{where}: `athletes.limit` must be 1-1000")
    sex = spec.get("sex")
    return AthleteQuery(
        sport=_qid(spec.get("sport"), "athletes.sport", where),
        sex=_qid(sex, "athletes.sex", where) if sex is not None else None,
        limit=limit,
    )


def _subreddits(raw: dict[str, Any], where: str) -> tuple[str, ...]:
    subreddits = _strings(raw, "subreddits", where)
    bad = [s for s in subreddits if not _SUBREDDIT.match(s)]
    if bad:
        raise LeagueConfigError(f"{where}: not subreddit names: {', '.join(bad)}")
    return subreddits


def _identity(raw: dict[str, Any], where: str) -> str:
    """The league's `key`, once `key`, `name` and `sport` are known to be valid."""
    key = raw.get("key")
    if not isinstance(key, str) or not _KEY.match(key):
        raise LeagueConfigError(f"{where}: `key` must match {_KEY.pattern}")
    for field in ("name", "sport"):
        if not isinstance(raw.get(field), str) or not raw[field].strip():
            raise LeagueConfigError(f"{where}: `{field}` is required")
    return key


def _options(raw: dict[str, Any], where: str) -> tuple[int | None, bool]:
    """`(players, team_subreddits)`, type-checked."""
    players = raw.get("players")
    if players is not None and (not isinstance(players, int) or players < 0):
        raise LeagueConfigError(f"{where}: `players` must be a non-negative integer")
    team_subs = raw.get("team_subreddits", False)
    if not isinstance(team_subs, bool):
        raise LeagueConfigError(f"{where}: `team_subreddits` must be true or false")
    return players, team_subs


def parse_league(raw: dict[str, Any], index: int) -> League:
    """One `[[league]]` table -> `League`. Raises `LeagueConfigError` naming the fault."""
    where = f"league #{index + 1} ({raw.get('key') or 'no key'})"
    key = _identity(raw, where)
    wikidata = raw.get("wikidata")
    players, team_subs = _options(raw, where)
    return League(
        key=key,
        name=raw["name"].strip(),
        sport=raw["sport"].strip(),
        wikidata=_qid(wikidata, "wikidata", where) if wikidata is not None else None,
        athletes=parse_athletes(raw.get("athletes"), where),
        mise_o_jeu=_strings(raw, "mise_o_jeu", where),
        subreddits=_subreddits(raw, where),
        team_subreddits=team_subs,
        x_queries=_strings(raw, "x_queries", where),
        players=players,
        extra=tuple(_qid(q, "extra", where) for q in _strings(raw, "extra", where)),
        covers=_strings(raw, "covers", where),
    )


def parse_leagues(text: str) -> tuple[League, ...]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise LeagueConfigError(f"not valid TOML: {exc}") from exc
    tables = data.get("league", [])
    if not isinstance(tables, list):
        raise LeagueConfigError("leagues must be `[[league]]` tables")
    leagues = tuple(parse_league(raw, i) for i, raw in enumerate(tables))
    seen: set[str] = set()
    for league in leagues:
        if league.key in seen:
            raise LeagueConfigError(f"duplicate league key {league.key!r}")
        seen.add(league.key)
    for league in leagues:
        unknown = sorted(set(league.covers) - seen)
        if unknown:
            raise LeagueConfigError(f"{league.key}: covers unknown league(s) {', '.join(unknown)}")
    return leagues


def load_leagues(path: Path) -> tuple[League, ...]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LeagueConfigError(f"cannot read {path}: {exc}") from exc
    try:
        return parse_leagues(text)
    except LeagueConfigError as exc:
        raise LeagueConfigError(f"{path}: {exc}") from exc


def subreddit_leagues(leagues: tuple[League, ...]) -> dict[str, frozenset[str]]:
    """`{subreddit (lower-case): league keys}` for the leagues' own subreddits.

    A subreddit listed by several leagues (r/soccer) is about all of them, and one of
    a league that `covers` others is about those too.
    """
    out: dict[str, set[str]] = {}
    for league in leagues:
        for sub in league.subreddits:
            out.setdefault(sub.lower(), set()).update(league.context)
    return {sub: frozenset(keys) for sub, keys in out.items()}
