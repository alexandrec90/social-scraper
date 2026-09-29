import pytest
from conftest import reddit_comment, reddit_listing, reddit_submission

from social_scraper.reddit.parse import REDDIT_BASE, parse_listing, parse_thing


def test_submission_maps_to_data_lake_ids():
    post = parse_thing(reddit_submission("1wsuydk", 1790640000.0, selftext="body text"))
    assert post.external_id == "1wsuydk"  # bare id, like data-lake's PRAW connector
    assert post.kind == "submission"
    assert post.channel == "wallstreetbets"
    assert post.author.name == "someone" and post.author.user_id is None
    assert post.url == REDDIT_BASE + "/r/wallstreetbets/comments/1wsuydk/post/"
    assert post.engagement.score == 10 and post.engagement.comments == 2
    assert post.engagement.upvote_ratio == 0.9
    assert post.flair == "Discussion"
    assert post.raw["is_self"] is True
    assert post.created_at.tzinfo is not None


def test_comment_uses_fullname_and_thread_parent():
    post = parse_thing(reddit_comment("pcpft3j", 1790640000.0))
    assert post.external_id == "t1_pcpft3j"
    assert post.kind == "comment"
    assert post.parent_id == "abc123"
    assert post.title is None and post.body == "comment pcpft3j"
    assert post.raw["link_title"] == "Daily Discussion"


@pytest.mark.parametrize("gone", ["[deleted]", "[removed]", ""])
def test_removed_content_is_skipped(gone):
    assert parse_thing(reddit_comment("c1", 1.0, body=gone)) is None
    assert parse_thing(reddit_submission("s1", 1.0, title=gone, selftext=gone)) is None


def test_deleted_author_becomes_anonymous():
    post = parse_thing(reddit_submission("s1", 1.0, author="[deleted]"))
    assert post.author.name is None
    assert post.author.hash is None


def test_unknown_or_broken_children_are_skipped():
    assert parse_thing({"kind": "more", "data": {"children": []}}) is None
    assert parse_thing({"kind": "t3", "data": "nope"}) is None
    assert parse_thing({"kind": "t5", "data": {"created_utc": 1}}) is None


def test_boolean_scores_are_not_numbers():
    post = parse_thing(reddit_submission("s1", 1.0, score=True))
    assert post.engagement.score is None


def test_parse_listing_returns_posts_and_cursor_and_drops_bots():
    listing = reddit_listing(
        [
            reddit_submission("a", 3.0),
            reddit_submission("b", 2.0, author="AutoModerator"),
            reddit_comment("c", 1.0),
        ],
        after="t3_a",
    )
    posts, after = parse_listing(listing, frozenset({"automoderator"}))
    assert [p.external_id for p in posts] == ["a", "t1_c"]
    assert after == "t3_a"


def test_parse_listing_rejects_non_listings():
    with pytest.raises(ValueError):
        parse_listing({"error": 403})
    with pytest.raises(ValueError):
        parse_listing(None)
