from datetime import UTC, datetime, timedelta

from conftest import NOW

from social_scraper.db import schema
from social_scraper.models import X, AuthorInfo, Engagement, Post
from social_scraper.x import accounts
from social_scraper.x.accounts import (
    AMPLIFIED,
    SEARCH,
    TimelineSample,
    add_seeds,
    apply,
    engagement_of,
    load,
    observe,
    record_visit,
    resolve_seed,
)
from social_scraper.x.discovery import SEED, TRACKED


def tweet(uid="1", handle="alice", followers=1000, likes=10):
    return Post(
        platform=X,
        external_id=f"t{uid}{likes}",
        kind="tweet",
        channel="search",
        created_at=datetime(2026, 9, 28, tzinfo=UTC),
        author=AuthorInfo(name=handle, user_id=uid, followers=followers, verified=False),
        body="$NVDA",
        engagement=Engagement(likes=likes, reposts=2, comments=3, quotes=1),
    )


def by_id(db):
    with db.connect() as conn:
        return {r.user_id: r for r in load(conn)}


def test_engagement_of_weights_amplification():
    assert engagement_of(tweet(likes=10)) == 10 + 4 + 3 + 2


def test_observe_creates_then_accumulates(db):
    with db.begin() as conn:
        assert observe(conn, [tweet(), tweet(likes=20)], SEARCH, NOW) == 1
        assert observe(conn, [tweet(followers=None)], AMPLIFIED, NOW) == 0
    row = by_id(db)["1"]
    assert row.search_hits == 2
    assert row.search_engagement == 19 + 29
    assert row.mentioned_by_tracked == 1
    assert row.followers == 1000  # a missing follower count does not erase the known one
    assert row.status == "candidate"


def test_observe_ignores_authors_without_ids(db):
    anon = Post(
        platform=X,
        external_id="z",
        kind="tweet",
        channel="s",
        created_at=NOW,
        author=AuthorInfo(name=None),
    )
    with db.begin() as conn:
        assert observe(conn, [anon], SEARCH, NOW) == 0


def test_apply_and_record_visit(db):
    with db.begin() as conn:
        observe(conn, [tweet()], SEARCH, NOW)
        apply(conn, {"1": (TRACKED, 3.5)})
        record_visit(conn, "1", TimelineSample(10, 7), NOW)
        record_visit(conn, "1", TimelineSample(5, 1), NOW + timedelta(hours=1))
    row = by_id(db)["1"]
    assert row.status == TRACKED
    assert (row.timeline_tweets, row.timeline_relevant_tweets) == (15, 8)
    assert row.last_timeline_at == NOW + timedelta(hours=1)


def test_the_sports_table_keeps_affiliation_and_tout_evidence(db):
    t = schema.sports_x_accounts
    org = tweet(uid="9", handle="newsroom")
    org = Post(**{**org.__dict__, "author": AuthorInfo("newsroom", "9", 9000, True, None, True)})
    with db.begin() as conn:
        assert observe(conn, [org], SEARCH, NOW, t) == 1
        # A later sighting without the label (None) does not erase it.
        unknown = Post(**{**org.__dict__, "author": AuthorInfo("newsroom", "9", 9000)})
        observe(conn, [unknown], SEARCH, NOW, t)
        record_visit(conn, "9", TimelineSample(10, 9, touts=2), NOW, t)
        (row,) = load(conn, t)
    assert row.affiliated is True and row.search_hits == 2
    assert (row.timeline_tweets, row.timeline_relevant_tweets, row.tout_tweets) == (10, 9, 2)
    with db.connect() as conn:
        assert load(conn) == []  # the stock table is a separate list


def test_sports_seeds_and_resolution(db):
    t = schema.sports_x_accounts
    with db.begin() as conn:
        assert add_seeds(conn, ["NHL", "nhl", "CFL"], NOW, t) == 2
        assert resolve_seed(conn, "seed:nhl", "50", t) == "50"
        apply(conn, {"50": (SEED, 1.0)}, t)
        rows = {r.user_id: r for r in load(conn, t)}
    assert set(rows) == {"50", "seed:cfl"} and rows["50"].tout_tweets == 0


def test_add_seeds_is_idempotent_and_marks_known_handles(db):
    with db.begin() as conn:
        observe(conn, [tweet(handle="Known")], SEARCH, NOW)
        assert add_seeds(conn, ["known", "newbie"], NOW) == 1
        assert add_seeds(conn, ["newbie"], NOW) == 0
    rows = by_id(db)
    assert rows["1"].status == SEED
    assert rows[accounts.SEED_ID_PREFIX + "newbie"].status == SEED


def test_resolve_seed_renames_placeholder(db):
    with db.begin() as conn:
        add_seeds(conn, ["newbie"], NOW)
        assert resolve_seed(conn, "seed:newbie", "777") == "777"
        assert resolve_seed(conn, "777", "777") == "777"  # already real: no-op
    rows = by_id(db)
    assert "777" in rows and "seed:newbie" not in rows


def test_resolve_seed_merges_into_existing_account(db):
    with db.begin() as conn:
        observe(conn, [tweet(uid="555", handle="dup")], SEARCH, NOW)
        conn.execute(
            accounts.schema.x_accounts.insert().values(
                user_id="seed:dupe",
                handle="dupe",
                status=SEED,
                source=SEED,
                search_hits=0,
                search_engagement=0,
                timeline_tweets=0,
                timeline_relevant_tweets=0,
                mentioned_by_tracked=0,
                score=0.0,
                first_seen_at=NOW,
                last_seen_at=NOW,
            )
        )
        assert resolve_seed(conn, "seed:dupe", "555") == "555"
    rows = by_id(db)
    assert "seed:dupe" not in rows and rows["555"].status == SEED
