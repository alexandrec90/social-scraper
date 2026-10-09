"""Records mirror Zenodo 14669616's `user_posts/<user>.jsonl` lines (checked 2026-10-08)
with invented content and ids -- no real post or account is committed."""

import io
import json
import tarfile
from datetime import UTC, datetime

import pyarrow.parquet as pq
import pytest
from conftest import NOW

from social_scraper.archive import datasets
from social_scraper.archive.store import LocalStore
from social_scraper.backfill import (
    NO_CASHTAG,
    OUT_OF_RANGE,
    UNPARSABLE,
    ArchiveFilter,
    Converted,
    ImportReport,
    Spool,
)
from social_scraper.bluesky import dumps
from social_scraper.bluesky.dumps import (
    REPOST,
    bluesky_post,
    convert_line,
    expand_inputs,
    import_files,
    is_input,
    parse_date,
    read_lines,
    spool_posts,
)
from social_scraper.models import stable_hash

FLT = ArchiveFilter()


def record(post_id=5001, text="Added to my $NVDA position today", **over):
    base = {
        "post_id": post_id,
        "user_id": 42,
        "instance": "bsky.social",
        "date": 202403101740,
        "text": text,
        "langs": ["eng"],
        "like_count": 7,
        "reply_count": 2,
        "repost_count": 1,
        "reply_to": None,
        "replied_author": None,
        "thread_root": None,
        "thread_root_author": None,
        "repost_from": None,
        "reposted_author": None,
        "quotes": None,
        "quoted_author": None,
        "labels": None,
        "sent_label": None,
        "sent_score": None,
    }
    return base | over


def line(**over):
    return json.dumps(record(**over)).encode() + b"\n"


def tarball(path, members):
    """A `.tar.gz` the way the record ships it: a `user_posts/` folder of `.jsonl`."""
    with tarfile.open(path, "w:gz") as tf:
        for name, body in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tf.addfile(info, io.BytesIO(body))
    return path


def test_bluesky_post_is_shaped_like_an_archived_post():
    post = bluesky_post(record())
    assert (post.platform, post.external_id, post.kind, post.channel) == (
        "bluesky",
        "5001",
        "post",
        "archive",
    )
    assert post.created_at == datetime(2024, 3, 10, 17, 40, tzinfo=UTC)
    # Namespaced: a dataset id must never hash like a real account's.
    assert post.author.hash == stable_hash("zenodo-14669616:42")
    assert post.url is None and post.lang == "eng"
    assert (post.engagement.likes, post.engagement.score, post.engagement.comments) == (7, 7, 2)
    assert post.engagement.reposts == 1


def test_a_reply_points_at_its_parent_and_its_thread():
    post = bluesky_post(record(reply_to=4000, thread_root=3999))
    assert post.kind == "reply" and post.parent_id == "4000"
    assert post.raw == {"conversation_id": "3999"}


@pytest.mark.parametrize(
    ("over", "reason"),
    [
        ({"repost_from": 77}, REPOST),
        ({"post_id": None}, UNPARSABLE),
        ({"post_id": True}, UNPARSABLE),
        ({"text": " "}, UNPARSABLE),
        ({"date": "yesterday"}, UNPARSABLE),
    ],
)
def test_bluesky_post_says_why_a_record_is_not_a_post(over, reason):
    assert bluesky_post(record(**over)) == reason


def test_parse_date_reads_ints_and_strings():
    assert parse_date("202312060142") == datetime(2023, 12, 6, 1, 42, tzinfo=UTC)
    assert parse_date(None) is None


def test_convert_line_keeps_only_explicit_listed_cashtags(universe):
    kept = convert_line(line(), universe, FLT, "user_posts.tar.gz")
    assert kept.skip is None
    assert kept.post["part"] == "social_scraper/posts/platform=bluesky/dt=2024-03-10"
    assert json.loads(kept.post["extra"]) == {"backfill": "user_posts.tar.gz"}
    # A bare ticker on a general-topic platform is not enough, nor is a `$` alone.
    for text in ("AMD earnings were great", "lunch cost $12", "echo $PATH"):
        assert convert_line(line(text=text), universe, FLT, "s") == Converted(NO_CASHTAG)


def test_convert_line_skips(universe):
    assert convert_line(b"{not json $\n", universe, FLT, "s") == Converted(UNPARSABLE)
    assert convert_line(b'["$x"]\n', universe, FLT, "s") == Converted(UNPARSABLE)
    assert convert_line(line(repost_from=9), universe, FLT, "s") == Converted(REPOST)
    window = ArchiveFilter(until=datetime(2024, 1, 1, tzinfo=UTC))
    assert convert_line(line(), universe, window, "s") == Converted(OUT_OF_RANGE)


def test_convert_line_with_all_posts_keeps_a_post_without_a_symbol(universe):
    kept = convert_line(line(text="lovely day"), universe, ArchiveFilter(all_posts=True), "s")
    assert kept.skip is None and kept.mentions == ()


def test_read_lines_streams_a_tarball_or_a_jsonl_file(tmp_path):
    archive = tarball(
        tmp_path / "user_posts.tar.gz",
        {"user_posts/1.jsonl": line() + line(post_id=5002), "user_posts/README": b"skip me"},
    )
    assert len(list(read_lines(archive))) == 2
    plain = tmp_path / "1.jsonl"
    plain.write_bytes(line())
    assert list(read_lines(plain)) == [line()]


def test_is_input_knows_the_archive_and_jsonl_suffixes(tmp_path):
    for name in ("user_posts.tar.gz", "u.TGZ", "u.tar", "1.jsonl"):
        assert is_input(tmp_path / name), name
    assert not is_input(tmp_path / "scripts.txt")


def test_expand_inputs_finds_the_archives_in_a_folder(tmp_path):
    for name in ("user_posts.tar.gz", "a.jsonl", "scripts.txt"):
        (tmp_path / name).write_bytes(b"")
    assert [p.name for p in expand_inputs([tmp_path])] == ["a.jsonl", "user_posts.tar.gz"]
    assert expand_inputs([tmp_path / "x.tar"]) == [tmp_path / "x.tar"]


def test_spool_posts_counts_every_outcome(tmp_path, universe):
    archive = tarball(
        tmp_path / "user_posts.tar.gz",
        {"user_posts/1.jsonl": line() + line(post_id=5002, text="hi") + line(repost_from=3)},
    )
    with Spool(tmp_path) as spool:
        stats = spool_posts(archive, spool, universe, FLT)
        assert spool.summary()[:2] == (1, 1)
    assert (stats.lines, stats.kept) == (3, 1)
    assert stats.skipped == {NO_CASHTAG: 1, REPOST: 1} and stats.error is None


def test_spool_posts_reports_a_truncated_download(tmp_path, universe):
    whole = tarball(tmp_path / "whole.tar.gz", {"user_posts/1.jsonl": line() * 2000})
    cut = tmp_path / "cut.tar.gz"
    cut.write_bytes(whole.read_bytes()[:200])
    with Spool(tmp_path) as spool:
        assert spool_posts(cut, spool, universe, FLT).error is not None


def test_import_files_writes_bluesky_rows_to_the_lake(tmp_path, universe):
    (tmp_path / "in").mkdir()
    tarball(
        tmp_path / "in" / "user_posts.tar.gz",
        {"user_posts/1.jsonl": line(), "user_posts/2.jsonl": line(post_id=5003, user_id=43)},
    )
    lake = tmp_path / "lake"
    report = import_files([tmp_path / "in"], universe, FLT, [LocalStore(lake)], tmp_path, NOW)
    assert report.ok and report.posts == 2
    [part] = (lake / datasets.PREFIX / "posts" / "platform=bluesky").rglob("part.parquet")
    assert {r["external_id"] for r in pq.read_table(part).to_pylist()} == {"5001", "5003"}


def test_import_files_reports_a_folder_with_nothing_to_read(tmp_path, universe):
    report = import_files([tmp_path], universe, FLT, None, tmp_path, NOW)
    assert not report.ok and "no .tar.gz" in report.errors[0]


def test_write_report_uses_its_own_file(tmp_path):
    path = dumps.write_report(ImportReport(started_at="t"), tmp_path)
    assert path.name == dumps.REPORT_FILE == "bluesky-import.json"
