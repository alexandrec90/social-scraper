from datetime import UTC, datetime

from conftest import x_search_payload, x_tweet_2026, x_tweet_classic, x_user_classic

from social_scraper.x.parse import (
    iter_tweet_results,
    parse_author,
    parse_payloads,
    parse_time,
    parse_tweet,
)

USER = x_user_classic("111", "trader_a", followers=5000)


def test_parse_time_both_formats():
    assert parse_time("Mon Sep 28 14:00:00 +0000 2026") == datetime(2026, 9, 28, 14, tzinfo=UTC)
    assert parse_time(1790611200000) == datetime(2026, 9, 28, 16, tzinfo=UTC)
    assert parse_time("1790611200000") == datetime(2026, 9, 28, 16, tzinfo=UTC)
    assert parse_time("garbage") is None
    assert parse_time(None) is None
    assert parse_time(True) is None


def test_classic_tweet():
    post = parse_tweet(x_tweet_classic("9", "Buying $NVDA", USER, ("NVDA",)), "search")
    assert post.external_id == "9" and post.kind == "tweet" and post.channel == "search"
    assert post.author.user_id == "111" and post.author.name == "trader_a"
    assert post.author.followers == 5000 and post.author.verified is True
    assert post.author.created_at == datetime(2016, 3, 1, 10, tzinfo=UTC)
    e = post.engagement
    assert (e.likes, e.reposts, e.comments, e.quotes, e.views, e.bookmarks) == (
        50,
        5,
        7,
        1,
        1234,
        2,
    )
    assert e.score == 50
    assert post.tagged_symbols == ("NVDA",)
    assert post.url == "https://x.com/i/status/9"  # no handle in the URL
    assert post.raw == {"conversation_id": "9"}


def test_2026_web_app_shape():
    # Shape captured live from a logged-out profile page on 2026-09-28.
    tweet = x_tweet_2026(
        "2104602031319073137",
        "Built in Texas",
        "17033995",
        "Nasdaq",
    )
    tweet["details"]["cashtag_entities"] = [{"text": "NDAQ"}, "QQQ"]
    post = parse_tweet(tweet, "timeline")
    assert post.created_at == datetime(2026, 9, 28, 16, tzinfo=UTC)
    assert post.body == "Built in Texas"
    assert post.author.user_id == "17033995" and post.author.name == "Nasdaq"
    assert post.author.followers == 233266 and post.author.verified is True
    assert post.engagement.likes == 27 and post.engagement.views == 36978
    assert post.lang == "en"
    assert post.tagged_symbols == ("NDAQ", "QQQ")
    assert post.author.affiliated is True  # verified_type "Business": an organisation


def test_affiliation_from_the_label_or_an_organisation_check():
    plain = x_user_classic("1", "fan")
    assert parse_author(x_tweet_classic("1", "t", plain)).affiliated is False
    labelled = {
        **plain,
        "affiliates_highlighted_label": {"label": {"description": "The Athletic"}},
    }
    assert parse_author(x_tweet_classic("2", "t", labelled)).affiliated is True
    government = {**plain, "legacy": {"verified_type": "Government"}}
    assert parse_author(x_tweet_classic("3", "t", government)).affiliated is True
    personal = {**plain, "verification": {"is_blue_verified": True, "verified_type": None}}
    assert parse_author(x_tweet_classic("4", "t", personal)).affiliated is False


def test_note_tweet_full_text_wins():
    tweet = x_tweet_classic("9", "truncated…", USER)
    tweet["note_tweet"] = {
        "note_tweet_results": {
            "result": {
                "text": "the full long text $AMD",
                "entity_set": {"symbols": [{"text": "AMD"}]},
            }
        }
    }
    post = parse_tweet(tweet, "search")
    assert post.body == "the full long text $AMD"
    assert "AMD" in post.tagged_symbols


def test_reply_and_quote_kinds():
    reply = parse_tweet(x_tweet_classic("9", "yes", USER, in_reply_to_status_id_str="8"), "s")
    assert reply.kind == "reply" and reply.parent_id == "8"
    quote = parse_tweet(x_tweet_classic("9", "look", USER, is_quote_status=True), "s")
    assert quote.kind == "quote"


def test_retweet_wrapper_skipped_but_original_found():
    original = x_tweet_classic("1", "original $TSLA take", x_user_classic("222", "orig"))
    wrapper = x_tweet_classic(
        "2", "RT @orig: original", USER, retweeted_status_result={"result": original}
    )
    assert parse_tweet(wrapper, "timeline") is None
    posts = parse_payloads([x_search_payload(wrapper)], "timeline")
    assert [(p.external_id, p.author.name) for p in posts] == [("1", "orig")]


def test_visibility_wrapper_is_unwrapped():
    inner = x_tweet_classic("5", "hidden-ish", USER)
    inner.pop("__typename")
    wrapped = {"__typename": "TweetWithVisibilityResults", "tweet": inner}
    assert [t["rest_id"] for t in iter_tweet_results(x_search_payload(wrapped))] == ["5"]


def test_empty_shells_are_skipped():
    assert parse_tweet({"__typename": "Tweet", "rest_id": "1", "legacy": {}}, "s") is None
    no_text = x_tweet_classic("1", "", USER)
    assert parse_tweet(no_text, "s") is None


def test_unavailable_author_is_anonymous():
    tweet = x_tweet_classic("1", "hi", {"__typename": "UserUnavailable"})
    author = parse_author(tweet)
    assert author.name is None and author.user_id is None and author.hash is None


def test_parse_payloads_dedupes_across_payloads():
    t = x_tweet_classic("1", "one", USER)
    posts = parse_payloads([x_search_payload(t), [t], {"unrelated": True}], "search")
    assert len(posts) == 1
