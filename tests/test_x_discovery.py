from datetime import timedelta

from conftest import NOW

from social_scraper.x.discovery import (
    CANDIDATE,
    DROPPED,
    MIN_TIMELINE_SAMPLE,
    SEED,
    TRACKED,
    AccountRow,
    finance_ratio,
    pick_symbols,
    pick_timelines,
    reconcile,
    score_account,
    split_budget,
)


def row(uid, status=CANDIDATE, **kw):
    return AccountRow(user_id=uid, handle=f"h{uid}", status=status, **kw)


def test_finance_ratio_uses_prior_until_sampled():
    assert finance_ratio(row("a", timeline_tweets=3, timeline_finance_tweets=3)) == 0.5
    sampled = row("a", timeline_tweets=18, timeline_finance_tweets=18)
    assert finance_ratio(sampled) == 19 / 20


def test_score_rewards_activity_reach_and_engagement():
    quiet = row("a", followers=100, search_hits=1, search_engagement=1)
    loud = row("b", followers=100_000, search_hits=10, search_engagement=5000)
    assert score_account(loud) > score_account(quiet) > 0


def test_score_penalises_off_topic_timelines():
    on = row("a", followers=1000, search_hits=5, timeline_tweets=20, timeline_finance_tweets=18)
    off = row("b", followers=1000, search_hits=5, timeline_tweets=20, timeline_finance_tweets=1)
    assert score_account(on) > score_account(off)


def test_reconcile_promotes_best_eligible_candidates_up_to_cap():
    rows = [
        row("big", followers=50_000, search_hits=5, search_engagement=500),
        row("mid", followers=5_000, search_hits=3, search_engagement=100),
        row("tiny", followers=10, search_hits=9),  # under min_followers
        row("once", followers=90_000, search_hits=1),  # seen once only
    ]
    out = reconcile(rows, max_tracked=1, min_followers=500)
    assert out["big"][0] == TRACKED
    assert out["mid"][0] == CANDIDATE
    assert out["tiny"][0] == CANDIDATE
    assert out["once"][0] == CANDIDATE


def test_reconcile_counts_seeds_against_the_cap_and_never_drops_them():
    rows = [
        row("seed", status=SEED, timeline_tweets=50, timeline_finance_tweets=0),
        row("c1", followers=5_000, search_hits=4),
    ]
    out = reconcile(rows, max_tracked=1, min_followers=500)
    assert out["seed"][0] == SEED
    assert out["c1"][0] == CANDIDATE


def test_reconcile_drops_tracked_accounts_that_are_off_topic():
    off = row(
        "off",
        status=TRACKED,
        timeline_tweets=MIN_TIMELINE_SAMPLE + 10,
        timeline_finance_tweets=0,
    )
    assert reconcile([off], 10, 500)["off"][0] == DROPPED


def test_reconcile_keeps_dropped_dropped():
    assert reconcile([row("d", status=DROPPED, search_hits=50)], 10, 0)["d"][0] == DROPPED


def test_tracked_accounts_keep_their_place_against_a_marginally_better_candidate():
    incumbent = row("inc", status=TRACKED, followers=10_000, search_hits=5, search_engagement=100)
    challenger = row("new", followers=11_000, search_hits=5, search_engagement=100)
    out = reconcile([incumbent, challenger], max_tracked=1, min_followers=500)
    assert out["inc"][0] == TRACKED and out["new"][0] == CANDIDATE


def test_pick_timelines_prefers_never_visited_then_stalest():
    rows = [
        row("fresh", status=TRACKED, last_timeline_at=NOW - timedelta(hours=1)),
        row("stale", status=TRACKED, last_timeline_at=NOW - timedelta(days=2)),
        row("never", status=SEED),
        row("cand"),
    ]
    picked = pick_timelines(rows, NOW, 2)
    assert [r.user_id for r in picked] == ["never", "stale"]


def test_pick_symbols_skips_recently_searched_and_dedupes():
    last = {"NVDA": NOW - timedelta(hours=1), "AMD": NOW - timedelta(hours=10)}
    assert pick_symbols(["NVDA", "AMD", "TSLA", "AMD", "GME"], last, NOW, 2) == ["AMD", "TSLA"]
    assert pick_symbols([], {}, NOW, 3) == []


def test_split_budget():
    assert split_budget(8, 0.4, have_timelines=True) == (3, 5)
    assert split_budget(8, 0.4, have_timelines=False) == (8, 0)
    assert split_budget(1, 0.1, have_timelines=True) == (1, 0)
    assert split_budget(0, 0.5, have_timelines=True) == (0, 0)
