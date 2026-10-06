import json
from datetime import UTC, datetime, timedelta

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import zstandard
from conftest import NOW, reddit_comment, reddit_submission

from social_scraper.archive import datasets, manifest
from social_scraper.archive.store import LocalStore, StoreError
from social_scraper.archive.writer import write_partition
from social_scraper.reddit import dumps
from social_scraper.reddit.dumps import (
    BACKFILL_KEY,
    HAS_CREDENTIAL,
    IGNORED_AUTHOR,
    NO_SYMBOL,
    OTHER_SUBREDDIT,
    OUT_OF_RANGE,
    UNPARSABLE,
    DumpFilter,
    ImportReport,
    convert,
    expand_inputs,
    import_dumps,
    observed_at,
    partition_runs,
    read_lines,
    thing_of,
)

AWS_KEY = "AKIAABCDEFGHIJKLMNOP"  # pragma: allowlist secret - fake key the filter must drop

CREATED = datetime(2021, 1, 27, 15, 0, tzinfo=UTC)
RETRIEVED = CREATED + timedelta(hours=3)
FLT = DumpFilter(subreddits=frozenset({"wallstreetbets"}), ignore_authors=frozenset({"automod"}))


def dumped(thing, retrieved):
    """A listing child as a dump line carries it: the bare `data` object."""
    data = thing["data"]
    if retrieved is not None:
        data["retrieved_on"] = int(retrieved.timestamp())
    return data


def sub(post_id="gme1", created=CREATED, retrieved=RETRIEVED, **over):
    over.setdefault("title", "$GME to the moon")
    return dumped(reddit_submission(post_id, created.timestamp(), **over), retrieved)


def com(comment_id="c1", created=CREATED, retrieved=RETRIEVED, **over):
    over.setdefault("body", "holding NVDA calls")
    return dumped(reddit_comment(comment_id, created.timestamp(), **over), retrieved)


def write_jsonl(path, objs, compress=False):
    body = "".join(json.dumps(o) + "\n" for o in objs).encode("utf-8")
    if compress:
        body = zstandard.ZstdCompressor().compress(body)
    path.write_bytes(body)
    return path


def lake_rows_of(root, kind):
    rows = []
    for f in sorted((root / datasets.PREFIX / kind).rglob("part.parquet")):
        rows.extend(pq.read_table(f).to_pylist())
    return rows


# --- one object -------------------------------------------------------------------------


def test_thing_of_tells_the_kind_from_the_object():
    assert thing_of({"title": "t"})["kind"] == "t3"
    assert thing_of({"body": "b"})["kind"] == "t1"
    assert thing_of({"id": "x"}) is None


def test_observed_at_reads_either_retrieval_field_in_any_numeric_form():
    assert observed_at({"retrieved_on": RETRIEVED.timestamp()}) == RETRIEVED
    assert observed_at({"retrieved_utc": str(int(RETRIEVED.timestamp()))}) == RETRIEVED
    # Arctic Shift re-fetches ~36 h later and keeps that score; its first `retrieved_on`
    # stays seconds after posting (live sample, 2026-10-04: score 694, retrieved at +15 s).
    second = RETRIEVED + timedelta(hours=36)
    arctic = {
        "retrieved_on": RETRIEVED.timestamp(),
        "_meta": {"retrieved_2nd_on": second.timestamp()},
    }
    assert observed_at(arctic) == second
    assert observed_at({"retrieved_on": RETRIEVED.timestamp(), "_meta": "?"}) == RETRIEVED
    assert observed_at({"retrieved_on": True}) is None
    assert observed_at({"retrieved_on": "soon"}) is None
    assert observed_at({}) is None


def test_convert_keeps_a_submission_naming_a_symbol(universe):
    result = convert(sub(), universe, FLT, "wallstreetbets_submissions.zst")
    assert result.skip is None
    post = result.post
    assert post["external_id"] == "gme1" and post["kind"] == "submission"
    assert post["channel"] == "wallstreetbets" and post["symbols"] == ["GME"]
    assert post["created_at"] == CREATED
    # fetched_at is the archive's capture, never the import's wall clock.
    assert post["fetched_at"] == post["first_seen_at"] == RETRIEVED
    assert json.loads(post["extra"])[BACKFILL_KEY] == "wallstreetbets_submissions.zst"
    assert post["part"] == "social_scraper/posts/platform=reddit/dt=2021-01-27"
    assert post["author_hash"] and "someone" not in json.dumps(post, default=str)
    assert [(m["symbol"], m["match_type"]) for m in result.mentions] == [("GME", "cashtag")]
    assert result.mentions[0]["part"] == "social_scraper/mentions/platform=reddit/dt=2021-01-27"


def test_convert_keeps_a_comment_with_its_thread(universe):
    result = convert(com(), universe, FLT, "c.zst")
    assert result.post["external_id"] == "t1_c1" and result.post["parent_id"] == "abc123"
    assert [m["symbol"] for m in result.mentions] == ["NVDA"]


def test_convert_falls_back_to_created_when_the_archive_kept_no_retrieval_time(universe):
    assert convert(sub(retrieved=None), universe, FLT, "s").post["fetched_at"] == CREATED


def test_convert_keeps_a_cashtag_outside_the_directory(universe):
    """A ticker delisted since is not in today's directory; its cashtag still counts."""
    result = convert(sub(title="$BBBY squeeze"), universe, FLT, "s")
    assert result.post["symbols"] == []
    assert [(m["symbol"], m["in_universe"]) for m in result.mentions] == [("BBBY", False)]


@pytest.mark.parametrize(
    ("obj", "reason"),
    [
        (sub(subreddit="pics"), OTHER_SUBREDDIT),
        (sub(created=datetime(2014, 12, 31, tzinfo=UTC)), OUT_OF_RANGE),
        (sub(created_utc="never"), UNPARSABLE),
        (com(body="[deleted]"), UNPARSABLE),
        ({"id": "x", "subreddit": "wallstreetbets", "created_utc": 1.6e9}, UNPARSABLE),
        (["not", "an", "object"], UNPARSABLE),
        (sub(author="AutoMod"), IGNORED_AUTHOR),
        (sub(title="what a day", selftext="no tickers here"), NO_SYMBOL),
        # The negative test `.claude/rules/data-lake.md` requires: never written.
        (sub(selftext=f"leaked {AWS_KEY}"), HAS_CREDENTIAL),
    ],
)
def test_convert_skips_with_a_reason(universe, obj, reason):
    assert convert(obj, universe, FLT, "s") == dumps.Converted(reason)


def test_filter_window_is_since_inclusive_until_exclusive():
    flt = DumpFilter(frozenset(), since=CREATED, until=CREATED + timedelta(days=1))
    assert flt.in_range(CREATED) and not flt.in_range(CREATED + timedelta(days=1))
    assert not flt.in_range(CREATED - timedelta(seconds=1))
    assert DumpFilter(frozenset(), since=None).in_range(datetime(2006, 1, 1, tzinfo=UTC))


def test_all_posts_and_any_subreddit(universe):
    flt = DumpFilter(subreddits=frozenset(), all_posts=True)
    result = convert(sub(subreddit="pics", title="no tickers"), universe, flt, "s")
    assert result.skip is None and result.mentions == ()


# --- files ------------------------------------------------------------------------------


def test_read_lines_decompresses_zst_and_reads_plain_jsonl(tmp_path):
    objs = [{"a": 1}, {"b": "é"}]
    zst = write_jsonl(tmp_path / "x.zst", objs, compress=True)
    plain = write_jsonl(tmp_path / "x.jsonl", objs)
    for path in (zst, plain):
        assert [json.loads(line) for line in read_lines(path)] == objs


def test_expand_inputs_lists_dump_files_in_a_folder(tmp_path):
    (tmp_path / "b_comments.zst").write_bytes(b"")
    (tmp_path / "a_submissions.jsonl").write_bytes(b"")
    (tmp_path / "notes.txt").write_bytes(b"")
    single = tmp_path / "elsewhere.zst"
    assert expand_inputs([tmp_path, single]) == [
        tmp_path / "a_submissions.jsonl",
        tmp_path / "b_comments.zst",
        single,
    ]


def test_partition_runs_joins_a_partition_split_across_batches():
    schema = pa.schema([("id", pa.string()), ("part", pa.string())])

    def batch(*rows):
        return pa.RecordBatch.from_pylist([{"id": i, "part": p} for i, p in rows], schema=schema)

    batches = [batch(("1", "a"), ("2", "a")), batch(("3", "a"), ("4", "b")), batch(("5", "c"))]
    runs = [(part, table.column("id").to_pylist()) for part, table in partition_runs(batches)]
    assert runs == [("a", ["1", "2", "3"]), ("b", ["4"]), ("c", ["5"])]
    assert list(partition_runs([])) == []


def test_precheck_rejects_from_raw_fields_alone():
    assert dumps.precheck(sub(), FLT) is None
    assert dumps.precheck(sub(subreddit="WallStreetBets"), FLT) is None  # case-blind
    assert dumps.precheck(sub(subreddit="pics"), FLT) == OTHER_SUBREDDIT
    assert dumps.precheck({"subreddit": "wallstreetbets"}, FLT) == UNPARSABLE
    assert dumps.precheck("a string", FLT) == UNPARSABLE


def test_mentions_of_partitions_each_mention_by_its_post_day(universe):
    row = {"platform": "reddit", "external_id": "p", "channel": "stocks", "created_at": CREATED}
    found = [dumps.Mention("GME", "cashtag", True), dumps.Mention("XYZQ", "cashtag", False)]
    rows = dumps.mentions_of(row, found)
    assert [(m["symbol"], m["in_universe"]) for m in rows] == [("GME", True), ("XYZQ", False)]
    assert {m["part"] for m in rows} == {"social_scraper/mentions/platform=reddit/dt=2021-01-27"}
    assert dumps.mentions_of(row, []) == ()


UNITS = {"KiB": 2**10, "MiB": 2**20, "GiB": 2**30, "TiB": 2**40}


def test_the_spool_caps_duckdb_memory(tmp_path):
    """DuckDB defaults to 80% of RAM; a full backfill's spool got the run killed for low
    memory on a 16 GB desktop (2026-10-05). The cap makes it spill to disk instead."""
    with dumps.Spool(tmp_path) as spool:
        [(value,)] = spool.con.execute("SELECT current_setting('memory_limit')").fetchall()
        settings = dict(spool.con.execute("SELECT name, value FROM duckdb_settings()").fetchall())
    number, unit = value.split()
    assert float(number) * UNITS[unit] <= 2 * 10**9
    assert int(settings["threads"]) <= dumps.SPOOL_THREADS


def test_spool_file_fills_the_spool_and_its_file_stats(tmp_path, universe):
    path = write_jsonl(tmp_path / "s.zst", [sub(), sub("x", subreddit="pics")], compress=True)
    with dumps.Spool(tmp_path) as spool:
        stats = dumps.spool_file(path, spool, universe, FLT, log_every=1)
        assert isinstance(stats, dumps.FileStats)
        assert (stats.lines, stats.kept, stats.mentions) == (2, 1, 1)
        assert stats.skipped == {OTHER_SUBREDDIT: 1} and stats.error is None
        assert spool.summary()[:2] == (1, 1)


def test_line_filter_passes_every_spelling_of_a_wanted_subreddit():
    pattern = dumps.line_filter(frozenset({"wallstreetbets", "stockmarket"}))
    assert pattern.search(b'{"id":"a","subreddit":"wallstreetbets","body":"x"}')
    assert pattern.search(b'{"subreddit": "StockMarket"}')  # spaced, display case
    assert not pattern.search(b'{"subreddit":"pics","body":"wallstreetbets is wild"}')
    assert not pattern.search(b'{"subreddit":"wallstreetbetsELITE"}')  # a prefix is not it
    assert not pattern.search(b'{"subreddit_name_prefixed":"r/wallstreetbets"}')
    assert dumps.line_filter(frozenset()) is None  # every subreddit wanted: no filter


def test_spool_file_skips_other_subreddits_without_parsing_them(tmp_path, universe, monkeypatch):
    """A monthly dump is all of Reddit: ~600M lines, nearly all of them unwanted. Parsing
    each before looking at its subreddit ran at 7,500 lines/s -- weeks for eight months."""
    objs = [sub(), sub("x", subreddit="pics"), sub("y", subreddit="aww")]
    path = write_jsonl(tmp_path / "RS_2026-01.zst", objs, compress=True)
    real_loads = json.loads
    parsed = []

    def counting_loads(text, *args, **kwargs):
        parsed.append(text)
        return real_loads(text, *args, **kwargs)

    monkeypatch.setattr(dumps.json, "loads", counting_loads)
    with dumps.Spool(tmp_path) as spool:
        stats = dumps.spool_file(path, spool, universe, FLT)
    assert (stats.lines, stats.kept) == (3, 1)
    assert stats.skipped == {OTHER_SUBREDDIT: 2}
    assert len(parsed) == 1  # only the wallstreetbets line was ever parsed


def test_spool_file_keeps_a_post_with_invalid_bytes(tmp_path, universe):
    line = json.dumps(sub()).encode().replace(b"to the moon", b"to the \xff moon")
    path = tmp_path / "s.jsonl"
    path.write_bytes(line + b"\n")
    with dumps.Spool(tmp_path) as spool:
        stats = dumps.spool_file(path, spool, universe, FLT)
    assert (stats.kept, stats.bad_lines) == (1, 0)


def test_spool_file_flushes_in_batches(tmp_path, universe, monkeypatch):
    monkeypatch.setattr(dumps, "SPOOL_BATCH", 2)
    objs = [sub(f"p{i}") for i in range(5)]
    path = write_jsonl(tmp_path / "s.jsonl", objs)
    with dumps.Spool(tmp_path) as spool:
        assert dumps.spool_file(path, spool, universe, FLT).kept == 5
        assert spool.summary()[0] == 5


class CountingStore(LocalStore):
    def __init__(self, root):
        super().__init__(root)
        self.puts = []

    def put_bytes(self, key, data):
        self.puts.append(key)
        super().put_bytes(key, data)


def test_write_spool_writes_each_partition_once_into_every_store(tmp_path, universe):
    path = write_jsonl(tmp_path / "s.jsonl", [sub(), sub("gme2"), com()])
    stores = [CountingStore(tmp_path / "a"), CountingStore(tmp_path / "b")]
    report = ImportReport(started_at="t")
    with dumps.Spool(tmp_path) as spool:
        dumps.spool_file(path, spool, universe, FLT)
        dumps.write_spool(spool, stores, NOW, report)
    assert report.partitions == {"social_scraper_posts": 1, "social_scraper_mentions": 1}
    for store in stores:
        staged = [k for k in store.puts if k.endswith(".tmp")]
        assert len(staged) == 2  # one posts file, one mentions file -- not one per row


# --- a whole import ---------------------------------------------------------------------


LAKE = object()  # `run`'s default: the local lake under tmp_path


def run(tmp_path, universe, paths, stores=LAKE):
    spool = tmp_path / "spool"
    spool.mkdir(exist_ok=True)
    if stores is LAKE:
        stores = [LocalStore(tmp_path / "lake")]
    return import_dumps(paths, universe, FLT, stores, spool, NOW)


def test_import_writes_posts_and_mentions_partitions_with_their_catalog(tmp_path, universe):
    day2 = CREATED + timedelta(days=1)
    subs = write_jsonl(
        tmp_path / "wsb_submissions.zst", [sub(), sub("gme2", created=day2)], compress=True
    )
    coms = tmp_path / "wsb_comments.jsonl"
    write_jsonl(coms, [com(), com("c2", body="nothing to see")])
    with coms.open("a", encoding="utf-8") as fh:
        fh.write('{"subreddit":"wallstreetbets","body":"cut off mid-li\n')  # truncated
    report = run(tmp_path, universe, [subs, coms])
    assert report.ok, report.errors
    assert (report.posts, report.mentions) == (3, 3)
    assert report.partitions == {"social_scraper_posts": 2, "social_scraper_mentions": 2}
    assert report.by_year == [{"year": "2021", "posts": 3, "comments": 1, "symbols": 2}]
    stats = {s.path: s for s in report.files}
    assert stats[str(coms)].bad_lines == 1 and stats[str(coms)].skipped == {NO_SYMBOL: 1}
    lake = tmp_path / "lake"
    assert {r["external_id"] for r in lake_rows_of(lake, "posts")} == {"gme1", "gme2", "t1_c1"}
    assert len(lake_rows_of(lake, "mentions")) == 3
    catalog = manifest.load(LocalStore(lake), "social_scraper_posts")
    entry = catalog["partitions"]["social_scraper/posts/platform=reddit/dt=2021-01-27/part.parquet"]
    assert entry["rows"] == 2 and entry["updated_at"] == NOW.isoformat()


def test_import_keeps_the_latest_capture_of_a_post_found_in_two_dumps(tmp_path, universe):
    old = write_jsonl(tmp_path / "torrent.zst", [sub(score=1)], compress=True)
    later = RETRIEVED + timedelta(days=30)
    new = write_jsonl(tmp_path / "monthly.jsonl", [sub(score=500, retrieved=later)])
    report = run(tmp_path, universe, [new, old])
    assert report.posts == 1 and report.mentions == 1
    [row] = lake_rows_of(tmp_path / "lake", "posts")
    assert row["score"] == 500 and row["fetched_at"] == later


def test_import_never_overwrites_a_row_the_live_scrape_exported(tmp_path, universe):
    lake = LocalStore(tmp_path / "lake")
    live = convert(sub(score=900, retrieved=NOW), universe, FLT, "s").post
    live = {k: v for k, v in live.items() if k != "part"} | {"extra": None}
    write_partition(lake, datasets.POSTS, datasets.POSTS.partition_of(live), [live], NOW)
    path = write_jsonl(tmp_path / "s.zst", [sub(score=1), sub("gme9")], compress=True)
    assert run(tmp_path, universe, [path]).ok
    rows = {r["external_id"]: r for r in lake_rows_of(tmp_path / "lake", "posts")}
    assert rows["gme1"]["score"] == 900 and rows["gme1"]["extra"] is None
    assert "gme9" in rows


def test_reimporting_the_same_dump_changes_nothing(tmp_path, universe):
    path = write_jsonl(tmp_path / "s.zst", [sub(), com()], compress=True)
    run(tmp_path, universe, [path])
    first = sorted(r["external_id"] for r in lake_rows_of(tmp_path / "lake", "posts"))
    assert run(tmp_path, universe, [path]).ok
    assert sorted(r["external_id"] for r in lake_rows_of(tmp_path / "lake", "posts")) == first


def test_dry_run_counts_without_writing(tmp_path, universe):
    path = write_jsonl(tmp_path / "s.zst", [sub()], compress=True)
    report = run(tmp_path, universe, [path], stores=None)
    assert report.ok and report.dry_run and report.posts == 1
    assert report.partitions == {}
    assert not (tmp_path / "lake").exists()


def test_an_unreadable_file_is_reported_and_the_rest_still_lands(tmp_path, universe):
    good = write_jsonl(tmp_path / "good.zst", [sub()], compress=True)
    corrupt = tmp_path / "corrupt.zst"
    corrupt.write_bytes(b"this is not zstd at all")
    missing = tmp_path / "missing.zst"
    report = run(tmp_path, universe, [corrupt, missing, good])
    assert not report.ok and len(report.errors) == 2
    assert "ZstdError" in report.errors[0] and "missing.zst" in report.errors[1]
    assert [r["external_id"] for r in lake_rows_of(tmp_path / "lake", "posts")] == ["gme1"]


class RefusingStore(LocalStore):
    def put_bytes(self, key, data):
        raise StoreError("bucket refused the write")


def test_a_store_failure_is_reported_not_raised(tmp_path, universe):
    path = write_jsonl(tmp_path / "s.zst", [sub()], compress=True)
    report = run(tmp_path, universe, [path], stores=[RefusingStore(tmp_path / "lake")])
    assert not report.ok and report.errors == ["lake write: StoreError: bucket refused the write"]


def test_write_report_overwrites_the_artifact(tmp_path):
    report = ImportReport(started_at="t", errors=["boom"])
    path = dumps.write_report(report, tmp_path / "logs")
    assert path.name == dumps.REPORT_FILE
    body = json.loads(path.read_text())
    assert body["ok"] is False and body["errors"] == ["boom"]
    dumps.write_report(ImportReport(started_at="u"), tmp_path / "logs")
    assert json.loads(path.read_text())["ok"] is True
