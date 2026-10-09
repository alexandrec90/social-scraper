"""Rows mirror the NYU StockTwits dataset's CSVs (s3://stocktwits-nyu, checked 2026-10-08)
-- float-written reply ids, list-literal symbols, multi-line quoted bodies, files not in
id order -- with invented content: no real message or user id is committed."""

import json
from datetime import UTC, datetime

import pyarrow.parquet as pq
import pytest
from conftest import NOW

from social_scraper import backfill
from social_scraper.archive import datasets
from social_scraper.archive.store import LocalStore
from social_scraper.backfill import (
    NO_SYMBOL,
    OUT_OF_RANGE,
    UNPARSABLE,
    ArchiveFilter,
    Converted,
    ImportReport,
    Spool,
)
from social_scraper.models import stable_hash
from social_scraper.stocktwits import dumps
from social_scraper.stocktwits.dumps import (
    NO_METADATA,
    convert_message,
    import_folder,
    joined,
    load_features,
    message_post,
    spool_messages,
)

FEATURE_HEADER = (
    "message_id,user_id,created_at,sentiment,parent_message_id,in_reply_to_message_id,symbol_list"
)
FLT = ArchiveFilter()
CREATED = datetime(2021, 3, 4, 22, 53, 15, tzinfo=UTC)


def joined_row(**over):
    base = {
        "id": 100,
        "body": "$TSLA to the moon, isn&#39;t it",
        "has_meta": True,
        "user_id": "77",
        "created_at": CREATED,
        "sentiment": "Bullish",
        "conversation_id": None,
        "reply_to": None,
        "symbol_list": "['TSLA', 'AMD']",
    }
    return base | over


def dataset(folder, features, messages):
    """`features`/`messages`: {file name: CSV body without its header}."""
    for sub, header, files in (
        ("feature_wo_messages", FEATURE_HEADER, features),
        ("messages", "message_id,message_body", messages),
    ):
        (folder / sub).mkdir(parents=True, exist_ok=True)
        for name, body in files.items():
            # Bytes: text mode on Windows would turn a body's `\n` into `\r\n`.
            (folder / sub / name).write_bytes(f"{header}\n{body}".encode())
    return folder


def standard(folder):
    return dataset(
        folder,
        {
            "feature_wo_messages_000.csv": (
                "100,77,2021-03-04T22:53:15Z,Bullish,,,\"['TSLA', 'AMD']\"\n"
                "102,78,2021-03-04T22:54:00Z,,100.0,100.0,[]\n"
            ),
            # Out of order across files, as the real ones are.
            "feature_wo_messages_001.csv": "101,79,2021-03-04T22:53:30Z,Bearish,,,['GME']\n",
        },
        {
            "msg_000.csv": (
                '100,"$TSLA to the moon,\nmulti-line"\n'
                "102,@someone agreed $AMD\n"
                "101,$GME short squeeze\n"
                "103,orphan $NVDA without metadata\n"
            ),
        },
    )


def test_message_post_is_shaped_like_an_archived_post():
    post = message_post(joined_row())
    assert (post.platform, post.external_id, post.kind, post.channel) == (
        "stocktwits",
        "100",
        "message",
        "archive",
    )
    assert post.created_at == CREATED
    assert post.url == "https://stocktwits.com/message/100"
    assert post.author.hash == stable_hash("77") and post.author.name is None
    assert post.body == "$TSLA to the moon, isn't it"  # HTML entities unescaped
    assert post.tagged_symbols == ("TSLA", "AMD")
    assert post.raw == {}


def test_a_reply_points_at_its_parent_and_its_thread():
    post = message_post(joined_row(reply_to=99, conversation_id=90))
    assert post.kind == "reply" and post.parent_id == "99"
    assert post.raw == {"conversation_id": "90"}


@pytest.mark.parametrize(
    ("over", "reason"),
    [
        ({"id": None}, UNPARSABLE),
        ({"body": "  "}, UNPARSABLE),
        ({"has_meta": False}, NO_METADATA),
        ({"created_at": None}, UNPARSABLE),
    ],
)
def test_message_post_says_why_a_row_is_not_a_post(over, reason):
    assert message_post(joined_row(**over)) == reason


def test_convert_message_keeps_the_authors_sentiment_label(universe):
    result = convert_message(joined_row(), universe, FLT, "msg_000.csv")
    assert json.loads(result.post["extra"]) == {"backfill": "msg_000.csv", "sentiment": "bullish"}
    assert result.post["part"] == "social_scraper/posts/platform=stocktwits/dt=2021-03-04"
    assert {m["symbol"]: m["match_type"] for m in result.mentions} == {
        "TSLA": "tagged",
        "AMD": "tagged",
    }
    unlabelled = convert_message(joined_row(sentiment=None), universe, FLT, "s")
    assert "sentiment" not in json.loads(unlabelled.post["extra"])


def test_convert_message_skips(universe):
    window = ArchiveFilter(since=datetime(2022, 1, 1, tzinfo=UTC))
    assert convert_message(joined_row(), universe, window, "s") == Converted(OUT_OF_RANGE)
    plain = joined_row(body="nice weather", symbol_list="[]")
    assert convert_message(plain, universe, FLT, "s") == Converted(NO_SYMBOL)
    assert convert_message(joined_row(has_meta=False), universe, FLT, "s") == Converted(NO_METADATA)


def test_load_features_sorts_and_types_the_metadata(tmp_path):
    folder = standard(tmp_path / "st")
    con = backfill.connect(tmp_path / "f.duckdb", tmp_path / "tmp")
    try:
        files = sorted((folder / "feature_wo_messages").glob("*.csv"))
        assert load_features(con, files) == 3
        rows = con.execute("SELECT message_id, conversation_id, reply_to FROM features").fetchall()
        assert rows == [(100, None, None), (101, None, None), (102, 100, 100)]
    finally:
        con.close()


def test_joined_pairs_each_message_with_its_metadata(tmp_path):
    folder = standard(tmp_path / "st")
    con = backfill.connect(tmp_path / "f.duckdb", tmp_path / "tmp")
    try:
        load_features(con, sorted((folder / "feature_wo_messages").glob("*.csv")))
        rows = {r["id"]: r for r in joined(con, folder / "messages" / "msg_000.csv")}
    finally:
        con.close()
    assert rows[100]["body"] == "$TSLA to the moon,\nmulti-line"
    assert rows[100]["created_at"] == CREATED
    assert rows[101]["sentiment"] == "Bearish"  # its metadata is in another file
    assert rows[102]["reply_to"] == 100
    assert not rows[103]["has_meta"]


def test_spool_messages_counts_every_outcome(tmp_path, universe):
    folder = standard(tmp_path / "st")
    con = backfill.connect(tmp_path / "f.duckdb", tmp_path / "tmp")
    try:
        load_features(con, sorted((folder / "feature_wo_messages").glob("*.csv")))
        with Spool(tmp_path) as spool:
            stats = spool_messages(folder / "messages" / "msg_000.csv", spool, con, universe, FLT)
            assert spool.summary()[0] == 3
    finally:
        con.close()
    assert (stats.lines, stats.kept) == (4, 3)
    assert stats.skipped == {NO_METADATA: 1} and stats.error is None


def test_spool_messages_reports_an_unreadable_file(tmp_path, universe):
    con = backfill.connect(tmp_path / "f.duckdb", tmp_path / "tmp")
    try:
        with Spool(tmp_path) as spool:
            stats = spool_messages(tmp_path / "missing.csv", spool, con, universe, FLT)
    finally:
        con.close()
    assert stats.error is not None and stats.kept == 0


def test_import_folder_writes_stocktwits_rows_to_the_lake(tmp_path, universe):
    folder = standard(tmp_path / "st")
    lake = tmp_path / "lake"
    report = import_folder([folder], universe, FLT, [LocalStore(lake)], tmp_path, NOW)
    assert report.ok and report.posts == 3
    assert report.files[0].skipped == {NO_METADATA: 1}
    [part] = (lake / datasets.PREFIX / "posts" / "platform=stocktwits").rglob("part.parquet")
    rows = {r["external_id"]: r for r in pq.read_table(part).to_pylist()}
    assert json.loads(rows["101"]["extra"])["sentiment"] == "bearish"
    assert rows["102"]["kind"] == "reply" and rows["102"]["parent_id"] == "100"


def test_import_folder_reports_a_folder_without_both_tables(tmp_path, universe):
    (tmp_path / "half" / "messages").mkdir(parents=True)
    report = import_folder([tmp_path / "half"], universe, FLT, None, tmp_path, NOW)
    assert not report.ok and "no feature_wo_messages" in report.errors[0]


def test_import_folder_reports_a_metadata_load_that_fails(tmp_path, universe, monkeypatch):
    folder = standard(tmp_path / "st")

    def broken(con, files):
        raise dumps.duckdb.IOException("disk full")

    monkeypatch.setattr(dumps, "load_features", broken)
    report = import_folder([folder], universe, FLT, None, tmp_path, NOW)
    assert not report.ok and "disk full" in report.errors[0]


def test_write_report_uses_its_own_file(tmp_path):
    path = dumps.write_report(ImportReport(started_at="t"), tmp_path)
    assert path.name == dumps.REPORT_FILE == "stocktwits-import.json"
