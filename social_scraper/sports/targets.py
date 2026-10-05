"""What a sports cycle spends its page budget on. Pure.

**Reddit.** The leagues' own subreddits are a short, fixed list; the teams' are a
long one (every NHL, NFL, NBA team...), most of it quiet on any given day. Each cycle
visits `n` of them by priority = weight x hours since the last visit, so nothing
starves and the busy ones come round often:

| subreddit                     | weight                                      |
| ----------------------------- | ------------------------------------------- |
| a league's own                | `LEAGUE_WEIGHT`                             |
| a team's (`team_subreddits`)  | `TEAM_FLOOR` + log(1 + the team's mentions  |
|                               | over the last day, anywhere)                |

A team nobody mentions is still visited, at a tenth of a busy one's rate: the team
that is about to make news is not yet in the news.

**X.** Searches alternate between the most-mentioned entities of the last day
(`"Montreal Canadiens"`, newest first) and the leagues' generic queries (top first),
like cashtags and generic queries in the stock domain.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

LEAGUE_WEIGHT = 3.0
TEAM_FLOOR = 0.1
NEVER_VISITED_HOURS = 24 * 7


@dataclass(frozen=True)
class SubredditTarget:
    name: str
    weight: float


def subreddit_targets(
    league_subs: Sequence[str],
    team_subs: Mapping[str, str],
    mentions: Mapping[str, int],
) -> list[SubredditTarget]:
    """Every subreddit the domain may visit, with its weight; one entry per name.

    `team_subs` maps a team subreddit to its entity id, `mentions` an entity id to its
    recent mention count. A league's subreddit wins over a team's of the same name.
    """
    out: dict[str, SubredditTarget] = {}
    for sub in league_subs:
        out.setdefault(sub.lower(), SubredditTarget(sub, LEAGUE_WEIGHT))
    for sub, entity_id in team_subs.items():
        weight = TEAM_FLOOR + math.log1p(mentions.get(entity_id, 0))
        out.setdefault(sub.lower(), SubredditTarget(sub, weight))
    return list(out.values())


def pick_subreddits(
    targets: Sequence[SubredditTarget],
    visited: Mapping[str, datetime],
    now: datetime,
    n: int,
) -> list[str]:
    """The `n` targets with the highest weight x hours-since-visit. `visited` is lower-case."""

    def priority(t: SubredditTarget) -> tuple[float, str]:
        last = visited.get(t.name.lower())
        hours = NEVER_VISITED_HOURS if last is None else max((now - last).total_seconds() / 3600, 0)
        return (t.weight * hours, t.name.lower())

    ranked = sorted(targets, key=priority, reverse=True)
    return [t.name for t in ranked[: max(n, 0)]]


def entity_query(name: str) -> str:
    """An X search for one entity's chatter: the exact name, in either language."""
    return f'"{name}" (lang:en OR lang:fr)'
