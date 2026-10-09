"""Items mirror `open-index/hacker-news`'s Parquet columns and types (checked 2026-10-08)
with invented content and usernames -- no real item is committed."""

import json
from datetime import UTC, datetime

import pyarrow as pa
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
from social_scraper.hackernews import dumps
from social_scraper.hackernews.dumps import (
    POLL_OPTION,
    REMOVED,
    convert_item,
    expand_inputs,
    import_files,
    item_post,
    plain_text,
    spool_items,
    with_a_dollar,
)
from social_scraper.models import stable_hash

FLT = ArchiveFilter()
TIME = datetime(2021, 1, 1, 0, 0, 1, tzinfo=UTC)
SCHEMA = pa.schema(
    [
        ("id", pa.uint32()),
        ("deleted", pa.uint8()),
        ("type", pa.int8()),
        ("by", pa.string()),
        ("time", pa.timestamp("ms", tz="UTC")),
        ("text", pa.string()),
        ("dead", pa.uint8()),
        ("parent", pa.uint32()),
        ("url", pa.string()),
        ("score", pa.int32()),
        ("title", pa.string()),
        ("descendants", pa.int32()),
    ]
)


def item(item_id=900, kind=2, text="I sold my $TSLA calls<p>Bought &#x27;more&#x27;", **over):
    base = {
        "id": item_id,
        "deleted": 0,
        "type": kind,
        "by": "invented_user",
        "time": TIME,
        "text": text,
        "dead": 0,
        "parent": 899 if kind == 2 else 0,
        "url": "",
        "score": 0,
        "title": "",
        "descendants": 0,
    }
    return base | over


def story(**over):
    return item(
        901,
        1,
        "",
        title="Ask HN: is $NVDA overvalued?",
        url="https://example.com/a",
        score=42,
        descendants=17,
        **over,
    )


def parquet(path, items):
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(items, schema=SCHEMA), path)
    return path


def test_plain_text_drops_markup_and_code():
    markup = (
        "Buy <i>$AMD</i>?<p>Run this:<pre><code>export PATH=$HOME&#x2F;bin</code></pre>"
        '<a href="https:&#x2F;&#x2F;x.test">https:&#x2F;&#x2F;x.test</a>'
    )
    assert plain_text(markup) == "Buy $AMD?\n\nRun this:https://x.test"


def test_a_comment_is_shaped_like_an_archived_post():
    post = item_post(item())
    assert (post.platform, post.external_id, post.kind, post.channel) == (
        "hackernews",
        "900",
        "comment",
        "archive",
    )
    assert post.created_at == TIME and post.parent_id == "899"
    assert post.url == "https://news.ycombinator.com/item?id=900"
    assert post.author.hash == stable_hash("invented_user")
    assert post.body == "I sold my $TSLA calls\n\nBought 'more'"
    # HN publishes no comment points; the dataset's 0 is not stored as one.
    assert post.engagement.score is None and post.engagement.comments is None


def test_a_story_keeps_its_title_points_and_comment_count():
    post = item_post(story())
    assert post.kind == "story" and post.parent_id is None
    assert post.title == "Ask HN: is $NVDA overvalued?" and post.body is None
    assert (post.engagement.score, post.engagement.comments) == (42, 17)


@pytest.mark.parametrize(
    ("over", "reason"),
    [
        ({"deleted": 1}, REMOVED),
        ({"dead": 1}, REMOVED),
        ({"type": 4}, POLL_OPTION),
        ({"type": 9}, UNPARSABLE),
        ({"text": "<pre>only code</pre>"}, UNPARSABLE),
    ],
)
def test_item_post_says_why_an_item_is_not_a_post(over, reason):
    assert item_post(item(**over)) == reason


def test_convert_item_keeps_only_explicit_listed_cashtags(universe):
    kept = convert_item(story(), universe, FLT, "2021-01.parquet")
    assert kept.skip is None
    assert kept.post["part"] == "social_scraper/posts/platform=hackernews/dt=2021-01-01"
    extra = json.loads(kept.post["extra"])
    assert extra == {"backfill": "2021-01.parquet", "link": "https://example.com/a"}
    for text in ("AMD is fine", "costs $5", "set $PATH", "<pre>echo $TSLA</pre> nothing"):
        assert convert_item(item(text=text), universe, FLT, "s") == Converted(NO_CASHTAG)


def test_convert_item_skips(universe):
    window = ArchiveFilter(since=datetime(2022, 1, 1, tzinfo=UTC))
    assert convert_item(item(), universe, window, "s") == Converted(OUT_OF_RANGE)
    assert convert_item(item(dead=1), universe, FLT, "s") == Converted(REMOVED)


def test_with_a_dollar_filters_in_arrow():
    batch = pa.RecordBatch.from_pylist(
        [item(1, text="no money"), item(2, text="$x"), story()], schema=SCHEMA
    )
    assert with_a_dollar(batch).column("id").to_pylist() == [2, 901]


def test_expand_inputs_walks_year_folders(tmp_path):
    parquet(tmp_path / "data" / "2007" / "2007-01.parquet", [])
    parquet(tmp_path / "data" / "2006" / "2006-10.parquet", [])
    assert [p.name for p in expand_inputs([tmp_path])] == ["2006-10.parquet", "2007-01.parquet"]


def test_spool_items_counts_every_outcome(tmp_path, universe):
    path = parquet(
        tmp_path / "m.parquet",
        [item(), item(902, text="nothing here"), item(903, dead=1, text="$TSLA"), story()],
    )
    with Spool(tmp_path) as spool:
        stats = spool_items(path, spool, universe, FLT)
        assert spool.summary()[0] == 2
    assert (stats.lines, stats.kept) == (4, 2)
    assert stats.skipped == {NO_CASHTAG: 1, REMOVED: 1} and stats.error is None


def test_spool_items_reports_an_unreadable_file(tmp_path, universe):
    bad = tmp_path / "bad.parquet"
    bad.write_bytes(b"not parquet")
    with Spool(tmp_path) as spool:
        assert spool_items(bad, spool, universe, FLT).error is not None


def test_import_files_writes_hn_rows_to_the_lake(tmp_path, universe):
    parquet(tmp_path / "hn" / "data" / "2021" / "2021-01.parquet", [item(), story()])
    lake = tmp_path / "lake"
    report = import_files([tmp_path / "hn"], universe, FLT, [LocalStore(lake)], tmp_path, NOW)
    assert report.ok and report.posts == 2
    [part] = (lake / datasets.PREFIX / "posts" / "platform=hackernews").rglob("part.parquet")
    rows = {r["external_id"]: r for r in pq.read_table(part).to_pylist()}
    assert rows["901"]["score"] == 42 and rows["900"]["score"] is None


def test_import_files_reports_a_folder_with_nothing_to_read(tmp_path, universe):
    (tmp_path / "empty").mkdir()
    report = import_files([tmp_path / "empty"], universe, FLT, None, tmp_path, NOW)
    assert not report.ok and "no .parquet" in report.errors[0]


def test_write_report_uses_its_own_file(tmp_path):
    path = dumps.write_report(ImportReport(started_at="t"), tmp_path)
    assert path.name == dumps.REPORT_FILE == "hn-import.json"
