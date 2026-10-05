from datetime import timedelta

from conftest import NOW, Clock
from sqlalchemy import select

from social_scraper.db import schema
from social_scraper.models import REDDIT, X, AuthorInfo, Engagement, Post
from social_scraper.sports.ingest import SportsIngestor, build_row
from social_scraper.sports.matcher import CONTEXT, NAME, Entity, Matcher

HABS = Entity("Q1", "team", "Montreal Canadiens", ("Habs",), frozenset({"nhl"}))
LEAFS = Entity("Q2", "team", "Toronto Maple Leafs", (), frozenset({"nhl"}))
MATCHER = Matcher([HABS, LEAFS], {"nhl": frozenset({"nhl"}), "habs": frozenset({"nhl"})})


def post(external_id, text, channel="nhl", platform=REDDIT, author="fan"):
    return Post(
        platform=platform,
        external_id=external_id,
        kind="comment",
        channel=channel,
        created_at=NOW - timedelta(hours=1),
        author=AuthorInfo(name=author),
        body=text,
        engagement=Engagement(score=4),
        raw={"permalink": "/r/nhl/x", "author_fullname": "t2_dropped"},
    )


def test_build_row_matches_scrubs_and_tags_leagues():
    row, found, leagues = build_row(post("a", "Habs beat Toronto Maple Leafs, @someone"), MATCHER)
    assert {m.entity_id: m.match_type for m in found} == {"Q1": CONTEXT, "Q2": NAME}
    assert row["entities"] == ["Q1", "Q2"] and row["leagues"] == ["nhl"]
    assert "@someone" not in row["body"]
    assert row["raw"] == {"permalink": "/r/nhl/x"}
    assert row["author_hash"] and "fan" not in row.values()
    assert leagues == frozenset({"nhl"})


def test_build_row_on_x_ignores_the_channel_but_takes_the_search_context():
    tweet = post("t", "Habs tonight", channel="search", platform=X)
    assert build_row(tweet, MATCHER)[1] == []
    _row, found, leagues = build_row(tweet, MATCHER, context=["nhl"])
    assert [m.entity_id for m in found] == ["Q1"] and leagues == frozenset({"nhl"})


def test_ingest_keeps_only_posts_about_a_league(engine):
    ingestor = SportsIngestor(engine, MATCHER, Clock())
    batch = [
        post("on", "great game tonight"),  # a league subreddit: on-topic without a name
        post("named", "Montreal Canadiens trade rumour", channel="AskReddit"),
        post("off", "my cat", channel="AskReddit"),
        post("named", "Montreal Canadiens trade rumour", channel="AskReddit"),  # duplicate
    ]
    stats = ingestor.ingest(batch)
    assert (stats.seen, stats.stored, stats.new_mentions, stats.snapshots) == (4, 2, 1, 2)
    with engine.connect() as conn:
        stored = {r.external_id for r in conn.execute(select(schema.sports_posts))}
        assert stored == {"on", "named"}
        assert conn.execute(select(schema.posts)).first() is None  # stock tables untouched


def test_ingest_counts_unmatched_names_for_discovery(engine):
    text = "Ivan Newguy scored and Ivan Newguy again"
    SportsIngestor(engine, MATCHER, Clock()).ingest([post("a", text)])
    with engine.connect() as conn:
        rows = conn.execute(select(schema.sports_name_candidates)).all()
    assert [(r.name, r.league, r.sightings) for r in rows] == [("Ivan Newguy", "nhl", 1)]


def test_ingest_without_name_collection(engine):
    SportsIngestor(engine, MATCHER, Clock(), collect_names=False).ingest(
        [post("a", "Ivan Newguy scored")]
    )
    with engine.connect() as conn:
        assert conn.execute(select(schema.sports_name_candidates)).first() is None


def test_is_relevant_uses_the_current_context():
    ingestor = SportsIngestor(None, MATCHER)
    tweet = post("t", "Habs win", channel="timeline", platform=X)
    assert not ingestor.is_relevant(tweet)
    ingestor.context = frozenset({"nhl"})
    assert ingestor.is_relevant(tweet)
