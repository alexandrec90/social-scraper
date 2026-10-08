"""Rows mirror the Cashtag Piggybacking dataset's columns (Zenodo 2686862, checked
2026-10-06) with invented content -- no real tweet, user or handle is committed."""

import csv
import io
import json
import zipfile
from datetime import UTC, datetime

import pyarrow.parquet as pq
import pytest
from conftest import NOW

from social_scraper.archive import datasets
from social_scraper.archive.store import LocalStore
from social_scraper.backfill import NO_SYMBOL, OUT_OF_RANGE, UNPARSABLE, Converted, Spool
from social_scraper.models import stable_hash
from social_scraper.x import tweet_dumps
from social_scraper.x.tweet_dumps import (
    CHANNEL,
    RETWEET,
    Author,
    TweetFilter,
    convert_tweet,
    find_file,
    import_folder,
    load_authors,
    open_csv,
    spool_tweets,
    tweet_post,
)

TWEET_COLUMNS = [
    "id",
    "text",
    "user_id",
    "in_reply_to_status_id",
    "in_reply_to_user_id",
    "retweeted_status_id",
    "retweeted_user_id",
    "lang",
    "source",
    "created_at",
]
USER_COLUMNS = [
    "id",
    "name",
    "screen_name",
    "statuses_count",
    "followers_count",
    "friends_count",
    "favourites_count",
    "listed_count",
    "url",
    "lang",
    "time_zone",
    "location",
    "verified",
    "description",
    "created_at",
    "bot",
]
FLT = TweetFilter()


def row(tweet_id="900", text="Loading up on $NVDA, multi-line\nthesis", user="7", **over):
    base = {
        "id": tweet_id,
        "text": text,
        "user_id": user,
        "in_reply_to_status_id": "0",
        "in_reply_to_user_id": "0",
        "retweeted_status_id": "0",
        "retweeted_user_id": "0",
        "lang": "en",
        "source": "<a href='x'>web</a>",
        "created_at": "Thu Jun 01 14:00:00 +0000 2017",
    }
    return {**base, **over}


def user(user_id="7", followers="1200", verified="", bot="0"):
    return {c: "" for c in USER_COLUMNS} | {
        "id": user_id,
        "name": "Invented",
        "screen_name": "invented_handle",
        "followers_count": followers,
        "verified": verified,
        "bot": bot,
    }


def csv_text(columns, rows):
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=columns, quoting=csv.QUOTE_ALL)
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def zipped(path, member, text):
    """A zip the way the dataset ships it: the CSV plus macOS resource-fork junk."""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"__MACOSX/._{member}", b"\x00\x05\x16\x07junk")
        zf.writestr(member, text)
    return path


def dataset(folder, tweets, users=None):
    folder.mkdir(parents=True, exist_ok=True)
    zipped(folder / "tweets.csv.zip", "tweets.csv", csv_text(TWEET_COLUMNS, tweets))
    if users is not None:
        zipped(folder / "users.csv.zip", "users.csv", csv_text(USER_COLUMNS, users))
    return folder


def test_tweet_post_is_shaped_like_a_crawled_tweet():
    post = tweet_post(row(), Author(followers=1200, verified=True))
    assert (post.platform, post.external_id, post.kind, post.channel) == (
        "x",
        "900",
        "tweet",
        CHANNEL,
    )
    assert post.created_at == datetime(2017, 6, 1, 14, 0, tzinfo=UTC)
    assert post.url == "https://x.com/i/status/900"
    # Hashed from the numeric id, exactly as the crawler hashes X authors; no handle kept.
    assert post.author.hash == stable_hash("7") and post.author.name is None
    assert (post.author.followers, post.author.verified) == (1200, True)
    assert post.body.endswith("multi-line\nthesis")


def test_a_reply_points_at_its_parent():
    post = tweet_post(row(in_reply_to_status_id="899"), None)
    assert post.kind == "reply" and post.parent_id == "899"


@pytest.mark.parametrize(
    ("over", "reason"),
    [
        ({"retweeted_status_id": "123"}, RETWEET),
        ({"created_at": "yesterday"}, UNPARSABLE),
        ({"id": "not-a-number"}, UNPARSABLE),
        ({"text": "   "}, UNPARSABLE),
    ],
)
def test_tweet_post_says_why_a_row_is_not_a_post(over, reason):
    assert tweet_post(row(**over), None) == reason


def test_convert_tweet_keeps_the_bot_label(universe):
    result = convert_tweet(row(), {"7": Author(bot=1)}, universe, FLT, "tweets.csv.zip")
    extra = json.loads(result.post["extra"])
    assert extra == {"backfill": "tweets.csv.zip", "bot": 1}
    assert result.post["part"] == "social_scraper/posts/platform=x/dt=2017-06-01"
    assert [m["symbol"] for m in result.mentions] == ["NVDA"]
    unlabelled = convert_tweet(row(), {"7": Author()}, universe, FLT, "t")
    assert "bot" not in json.loads(unlabelled.post["extra"])


def test_convert_tweet_skips(universe):
    window = TweetFilter(since=datetime(2017, 7, 1, tzinfo=UTC))
    assert convert_tweet(row(), {}, universe, window, "t") == Converted(OUT_OF_RANGE)
    assert convert_tweet(row(text="no ticker"), {}, universe, FLT, "t") == Converted(NO_SYMBOL)
    assert convert_tweet(row(retweeted_status_id="5"), {}, universe, FLT, "t") == Converted(RETWEET)


def test_filter_window():
    flt = TweetFilter(until=datetime(2017, 6, 1, tzinfo=UTC))
    assert flt.in_range(datetime(2017, 5, 31, tzinfo=UTC))
    assert not flt.in_range(datetime(2017, 6, 1, tzinfo=UTC))


def test_open_csv_reads_a_zip_member_or_a_plain_file(tmp_path):
    text = csv_text(TWEET_COLUMNS, [row()])
    plain = tmp_path / "tweets.csv"
    plain.write_text(text, encoding="utf-8")
    for path in (zipped(tmp_path / "t.zip", "tweets.csv", text), plain):
        with open_csv(path, "tweets.csv") as rows:
            assert [r["id"] for r in rows] == ["900"]
    with pytest.raises(FileNotFoundError, match=r"no users\.csv"):
        with open_csv(tmp_path / "t.zip", "users.csv"):
            pass


def test_load_authors_reads_reach_verification_and_bot_label(tmp_path):
    path = zipped(
        tmp_path / "users.csv.zip",
        "users.csv",
        csv_text(USER_COLUMNS, [user(), user("8", followers="", verified="1", bot="")]),
    )
    authors = load_authors(path)
    assert authors["7"] == Author(followers=1200, verified=False, bot=0)
    assert authors["8"] == Author(followers=None, verified=True, bot=None)


def test_find_file_prefers_the_plain_csv(tmp_path):
    assert find_file(tmp_path, "tweets.csv") is None
    (tmp_path / "tweets.csv.zip").write_bytes(b"")
    assert find_file(tmp_path, "tweets.csv") == tmp_path / "tweets.csv.zip"
    (tmp_path / "tweets.csv").write_text("")
    assert find_file(tmp_path, "tweets.csv") == tmp_path / "tweets.csv"


def test_spool_tweets_counts_every_outcome(tmp_path, universe):
    folder = dataset(
        tmp_path / "d", [row(), row("901", retweeted_status_id="900"), row("902", text="hi")]
    )
    with Spool(tmp_path) as spool:
        stats = spool_tweets(folder / "tweets.csv.zip", spool, universe, FLT, {})
        assert spool.summary()[:2] == (1, 1)
    assert (stats.lines, stats.kept) == (3, 1)
    assert stats.skipped == {RETWEET: 1, NO_SYMBOL: 1} and stats.error is None


def test_spool_tweets_reports_an_unreadable_file(tmp_path, universe):
    bad = tmp_path / "tweets.csv.zip"
    bad.write_bytes(b"not a zip")
    with Spool(tmp_path) as spool:
        assert "BadZipFile" in spool_tweets(bad, spool, universe, FLT, {}).error


def test_import_folder_writes_x_rows_to_the_lake(tmp_path, universe):
    folder = dataset(tmp_path / "cashtag", [row(), row("903", user="8")], [user(bot="1")])
    lake = tmp_path / "lake"
    report = import_folder([folder], universe, FLT, [LocalStore(lake)], tmp_path, NOW)
    assert report.ok and report.posts == 2
    [part] = (lake / datasets.PREFIX / "posts" / "platform=x").rglob("part.parquet")
    rows = {r["external_id"]: r for r in pq.read_table(part).to_pylist()}
    assert json.loads(rows["900"]["extra"])["bot"] == 1
    assert "bot" not in json.loads(rows["903"]["extra"])  # user 8 is not in users.csv
    assert rows["900"]["author_followers"] == 1200


def test_import_folder_reports_a_folder_with_no_tweets(tmp_path, universe):
    (tmp_path / "empty").mkdir()
    report = import_folder([tmp_path / "empty"], universe, FLT, None, tmp_path, NOW)
    assert not report.ok and "no tweets.csv" in report.errors[0]


def test_write_report_uses_its_own_file(tmp_path):
    from social_scraper.backfill import ImportReport

    path = tweet_dumps.write_report(ImportReport(started_at="t"), tmp_path)
    assert path.name == tweet_dumps.REPORT_FILE == "x-import.json"
