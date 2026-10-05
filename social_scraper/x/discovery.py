"""Which X accounts are worth following, and what to search for next. All pure.

The loop is the same for every domain (stocks, sports); only what counts as an
on-topic tweet differs:

1. **Search** the domain's subjects (cashtags for stocks, teams and athletes for sports)
   and a rotating set of generic queries. Every author found is a *candidate*, and so
   is anyone a tracked account retweets or quotes ("amplified").
2. **Score** candidates on activity, reach and engagement. A search sample is biased
   -- every tweet a `$NVDA` search returns mentions a ticker -- so an account's
   *relevance ratio* is only trusted once its own timeline has been read. An account
   that is an organisation's (X's affiliate label, or a Business/Government
   verification) scores higher: it is a newsroom, a team, a league.
3. **Promote** the best candidates to *tracked*, up to `max_tracked`. Tracked accounts
   get their timelines visited, stalest-and-best first.
4. **Drop** a tracked account whose timeline turns out to be mostly off-topic, or --
   sports -- too often selling picks (`tout_tweets`; always 0 for stocks).

Seeds are always tracked and never dropped.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

CANDIDATE = "candidate"
TRACKED = "tracked"
DROPPED = "dropped"
SEED = "seed"

MIN_TIMELINE_SAMPLE = 8
DROP_BELOW_RATIO = 0.2
# Share of an account's own tweets that sell picks at which it is dropped. One promo
# in ten is a sponsor read; one in five is the business.
DROP_TOUT_RATIO = 0.2
MIN_SIGHTINGS = 2
# A tracked account keeps its place unless a candidate beats it by this factor, so
# the tracked set does not churn on noise.
TRACKED_BONUS = 1.15
# An organisation's account (affiliate label, Business/Government verification).
AFFILIATED_BONUS = 1.5


@dataclass(frozen=True)
class AccountRow:
    user_id: str
    handle: str
    status: str
    followers: int | None = None
    search_hits: int = 0
    search_engagement: int = 0
    timeline_tweets: int = 0
    timeline_relevant_tweets: int = 0
    mentioned_by_tracked: int = 0
    last_timeline_at: datetime | None = None
    affiliated: bool = False
    tout_tweets: int = 0


def relevance_ratio(row: AccountRow) -> float:
    """Share of the account's own tweets that are on-topic, smoothed toward 0.5.

    Before `MIN_TIMELINE_SAMPLE` timeline tweets it is the neutral prior -- the search
    sample says nothing about what else the account posts.
    """
    if row.timeline_tweets < MIN_TIMELINE_SAMPLE:
        return 0.5
    return (row.timeline_relevant_tweets + 1) / (row.timeline_tweets + 2)


def is_tout(row: AccountRow) -> bool:
    """Enough of the account's own timeline sells picks that it is a tipster, not a source."""
    return (
        row.timeline_tweets >= MIN_TIMELINE_SAMPLE
        and row.tout_tweets / row.timeline_tweets >= DROP_TOUT_RATIO
    )


def score_account(row: AccountRow) -> float:
    sightings = row.search_hits + row.mentioned_by_tracked
    activity = math.log1p(sightings)
    reach = math.log10((row.followers or 0) + 10)
    engagement = math.log1p(row.search_engagement / max(row.search_hits, 1))
    bonus = AFFILIATED_BONUS if row.affiliated else 1.0
    return round(bonus * relevance_ratio(row) * (activity + 0.5 * reach + 0.5 * engagement), 4)


def _eligible(row: AccountRow, min_followers: int) -> bool:
    if row.status in (SEED, DROPPED):
        return False
    if row.status == TRACKED:
        return True
    return (row.followers or 0) >= min_followers and (
        row.search_hits + row.mentioned_by_tracked >= MIN_SIGHTINGS
    )


def should_drop(row: AccountRow) -> bool:
    """A tracked account whose own timeline is off-topic, or sells picks."""
    if row.status != TRACKED:
        return False
    off_topic = (
        row.timeline_tweets >= MIN_TIMELINE_SAMPLE and relevance_ratio(row) < DROP_BELOW_RATIO
    )
    return off_topic or is_tout(row)


def reconcile(
    rows: Sequence[AccountRow], max_tracked: int, min_followers: int
) -> dict[str, tuple[str, float]]:
    """New `(status, score)` for every account whose status or score changed."""
    out: dict[str, tuple[str, float]] = {}
    scores = {r.user_id: score_account(r) for r in rows}
    ranked: list[tuple[float, str]] = []
    for r in rows:
        if should_drop(r):
            out[r.user_id] = (DROPPED, scores[r.user_id])
        elif _eligible(r, min_followers):
            bonus = TRACKED_BONUS if r.status == TRACKED else 1.0
            ranked.append((scores[r.user_id] * bonus, r.user_id))
    seeds = sum(1 for r in rows if r.status == SEED)
    ranked.sort(reverse=True)
    keep = {uid for _, uid in ranked[: max(0, max_tracked - seeds)]}
    for r in rows:
        if r.user_id in out:
            continue
        status = r.status
        if status in (CANDIDATE, TRACKED):
            status = TRACKED if r.user_id in keep else CANDIDATE
        out[r.user_id] = (status, scores[r.user_id])
    return out


def pick_timelines(rows: Sequence[AccountRow], now: datetime, n: int) -> list[AccountRow]:
    """The `n` tracked/seed accounts most overdue for a visit, weighted by score.

    Priority is `(score + 1) * hours since the last visit`; never visited is treated
    as a week, so new promotions go first.
    """
    active = [r for r in rows if r.status in (TRACKED, SEED)]

    def priority(r: AccountRow) -> float:
        last = r.last_timeline_at
        hours = 24 * 7 if last is None else max((now - last).total_seconds() / 3600, 0.0)
        return (score_account(r) + 1) * hours

    return sorted(active, key=priority, reverse=True)[:n]


def pick_symbols(
    candidates: Sequence[str],
    last_searched: dict[str, datetime],
    now: datetime,
    n: int,
    min_gap: timedelta = timedelta(hours=6),
) -> list[str]:
    """First `n` candidates (in priority order) not searched within `min_gap`."""
    out: list[str] = []
    for sym in dict.fromkeys(candidates):
        last = last_searched.get(sym)
        if last is None or now - last >= min_gap:
            out.append(sym)
        if len(out) == n:
            break
    return out


def split_budget(pages: int, search_share: float, have_timelines: bool) -> tuple[int, int]:
    """`(searches, timelines)` for a cycle. With nothing tracked yet, all search."""
    if pages <= 0:
        return 0, 0
    if not have_timelines:
        return pages, 0
    searches = max(1, round(pages * search_share))
    return min(searches, pages), pages - min(searches, pages)
