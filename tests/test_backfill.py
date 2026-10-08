import json
from datetime import UTC, datetime, timedelta

import pyarrow.parquet as pq
from conftest import NOW

from social_scraper import backfill
from social_scraper.archive import datasets
from social_scraper.archive.store import LocalStore, StoreError
from social_scraper.backfill import (
    BACKFILL_KEY,
    HAS_CREDENTIAL,
    NO_SYMBOL,
    UNPARSABLE,
    Converted,
    FileStats,
    ImportReport,
    Spool,
    lake_post,
    run_import,
)
from social_scraper.models import X, AuthorInfo, Post

AWS_KEY = "AKIAABCDEFGHIJKLMNOP"  # pragma: allowlist secret - fake key the filter must drop
CREATED = datetime(2017, 6, 1, 14, 0, tzinfo=UTC)


def tweet(tweet_id="1", text="$NVDA breaking out", created=CREATED):
    return Post(
        platform=X,
        external_id=tweet_id,
        kind="tweet",
        channel="archive",
        created_at=created,
        author=AuthorInfo(name=None, user_id="42"),
        body=text,
    )


def test_lake_post_runs_the_crawlers_pipeline_and_marks_the_source(universe):
    seen = CREATED + timedelta(hours=1)
    result = lake_post(tweet(), universe, "tweets.csv.zip", seen, extra={"bot": 0})
    post = result.post
    assert result.skip is None and post["symbols"] == ["NVDA"]
    assert post["fetched_at"] == post["first_seen_at"] == seen
    assert json.loads(post["extra"]) == {BACKFILL_KEY: "tweets.csv.zip", "bot": 0}
    assert post["part"] == "social_scraper/posts/platform=x/dt=2017-06-01"
    assert [m["symbol"] for m in result.mentions] == ["NVDA"]


def test_lake_post_without_an_observation_uses_the_posts_time(universe):
    assert lake_post(tweet(), universe, "s").post["fetched_at"] == CREATED


def test_lake_post_skips_what_names_no_symbol_unless_all_posts(universe):
    plain = tweet(text="lovely weather")
    assert lake_post(plain, universe, "s") == Converted(NO_SYMBOL)
    kept = lake_post(plain, universe, "s", all_posts=True)
    assert kept.skip is None and kept.mentions == ()


def test_lake_post_never_writes_a_credential(universe):
    """The negative test `.claude/rules/data-lake.md` requires."""
    leaked = tweet(text=f"$NVDA {AWS_KEY}")
    assert lake_post(leaked, universe, "s") == Converted(HAS_CREDENTIAL)


def test_file_stats_count_skips_by_reason():
    stats = FileStats("f")
    stats.skip("a")
    stats.skip("a")
    stats.skip("b")
    assert stats.skipped == {"a": 2, "b": 1}


def test_spool_take_counts_skips_and_buffers_kept_rows(tmp_path, universe, monkeypatch):
    monkeypatch.setattr(backfill, "SPOOL_BATCH", 2)
    stats = FileStats("f")
    with Spool(tmp_path) as spool:
        spool.take(Converted(None), stats)  # a skip with no reason counts as unparsable
        spool.take(lake_post(tweet("1"), universe, "s"), stats)
        assert spool.summary()[0] == 0  # buffered, not yet written
        spool.take(lake_post(tweet("2"), universe, "s"), stats)
        assert spool.summary()[0] == 2  # the batch filled and flushed itself
        spool.take(lake_post(tweet("3"), universe, "s"), stats)
        spool.flush()
        assert spool.summary()[:2] == (3, 3)
    assert (stats.kept, stats.mentions, stats.skipped) == (3, 3, {UNPARSABLE: 1})


def spool_from(posts, universe):
    """A `SpoolOne` that takes `posts` for whichever path it is given."""

    def one(path, spool):
        stats = FileStats(str(path))
        for post in posts:
            spool.take(lake_post(post, universe, path.name), stats)
        return stats

    return one


def test_run_import_writes_the_lake_once_per_partition(tmp_path, universe):
    one = spool_from([tweet("1"), tweet("2", created=CREATED + timedelta(days=1))], universe)
    lake = tmp_path / "lake"
    report = run_import([tmp_path / "a.csv"], one, [LocalStore(lake)], tmp_path, NOW)
    assert report.ok and (report.posts, report.mentions) == (2, 2)
    assert report.partitions == {"social_scraper_posts": 2, "social_scraper_mentions": 2}
    assert report.by_year == [{"year": "2017", "posts": 2, "comments": 0, "symbols": 1}]
    files = sorted((lake / datasets.PREFIX / "posts").rglob("part.parquet"))
    assert sum(pq.read_metadata(f).num_rows for f in files) == 2


def test_run_import_dry_run_writes_nothing(tmp_path, universe):
    report = run_import([tmp_path / "a"], spool_from([tweet()], universe), None, tmp_path, NOW)
    assert report.dry_run and report.posts == 1 and report.partitions == {}


def test_run_import_reports_a_failing_file_and_a_refusing_store(tmp_path, universe):
    def broken(path, spool):
        stats = spool_from([tweet()], universe)(path, spool)
        stats.error = "OSError: disk on fire"
        return stats

    class Refusing(LocalStore):
        def put_bytes(self, key, data):
            raise StoreError("no")

    report = run_import([tmp_path / "a"], broken, [Refusing(tmp_path / "lake")], tmp_path, NOW)
    assert not report.ok
    assert report.errors[0].endswith("a: OSError: disk on fire")
    assert report.errors[1] == "lake write: StoreError: no"
    assert report.posts == 1  # what the broken file yielded before failing was kept


def test_write_report_names_its_file(tmp_path):
    path = backfill.write_report(ImportReport(started_at="t", errors=["e"]), tmp_path, "x.json")
    assert path == tmp_path / "x.json"
    assert json.loads(path.read_text())["ok"] is False
