import hashlib
from datetime import UTC, datetime, timedelta, timezone

from social_scraper.models import AuthorInfo, Engagement, Post, as_utc, stable_hash


def test_stable_hash_is_plain_sha256_like_data_lake():
    assert stable_hash("someone") == hashlib.sha256(b"someone").hexdigest()


def test_as_utc_handles_naive_and_aware():
    naive = datetime(2026, 1, 1, 12)
    assert as_utc(naive) == datetime(2026, 1, 1, 12, tzinfo=UTC)
    east = datetime(2026, 1, 1, 12, tzinfo=timezone(timedelta(hours=5)))
    assert as_utc(east) == datetime(2026, 1, 1, 7, tzinfo=UTC)


def test_engagement_as_dict_uses_social_posts_names():
    d = Engagement(score=1, comments=2, likes=3).as_dict()
    assert d["score"] == 1
    assert d["num_comments"] == 2
    assert d["likes"] == 3
    assert d["views"] is None


def test_author_hash_prefers_stable_id():
    assert AuthorInfo(name="handle", user_id="42").hash == stable_hash("42")
    assert AuthorInfo(name="someone").hash == stable_hash("someone")
    assert AuthorInfo(name=None).hash is None


def test_post_text_joins_title_and_body():
    base = {
        "platform": "reddit",
        "external_id": "1",
        "kind": "submission",
        "channel": "stocks",
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "author": AuthorInfo(name=None),
    }
    assert Post(**base, title="T", body="B").text == "T\nB"
    assert Post(**base, title=None, body="B").text == "B"
    assert Post(**base).text == ""
