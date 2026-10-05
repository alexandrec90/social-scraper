from datetime import timedelta

from conftest import NOW, Clock

from social_scraper.archive import lens
from social_scraper.archive.lens import connect, symbol_daily
from social_scraper.archive.store import LocalStore
from social_scraper.export import export_all
from social_scraper.models import AuthorInfo, Post
from social_scraper.pipeline import Ingestor


def test_connect_on_an_empty_lake_has_no_views(tmp_path):
    con = connect(tmp_path)
    try:
        assert (
            con.execute("SELECT count(*) FROM duckdb_views() WHERE NOT internal").fetchone()[0] == 0
        )
    finally:
        con.close()


def test_symbol_daily_over_exported_data(engine, universe, tmp_path):
    posts = [
        Post(
            platform="reddit",
            external_id=f"p{i}",
            kind="submission",
            channel="stocks",
            created_at=NOW - timedelta(hours=1),
            author=AuthorInfo(name=f"user{i % 2}"),
            body=body,
        )
        for i, body in enumerate(["$NVDA calls", "$NVDA puts", "$NVDA to the moon", "$AMD"])
    ]
    Ingestor(engine, universe, Clock()).ingest(posts)
    export_all(engine, [LocalStore(tmp_path)], NOW)
    con = connect(tmp_path)
    try:
        views = {r[0] for r in con.execute("SELECT view_name FROM duckdb_views()").fetchall()}
        # No post had engagement numbers, so no snapshots -- and no engagement view.
        assert set(lens.VIEWS) - views == {"social_engagement"}
        rows = symbol_daily(con, NOW - timedelta(days=1))
    finally:
        con.close()
    top = rows[0]
    assert top[0] == "NVDA" and top[2] == 3 and top[3] == 2
    assert top[4] is not None  # mean stance over the three NVDA posts


def test_a_timestamp_column_reads_back_through_the_lens(engine, universe, tmp_path):
    """DuckDB hands a TIMESTAMPTZ to Python through pytz, which it does not declare.

    Without it, any verification query that selects ``created_at`` raises "Required module
    'pytz' failed to import" -- a session reading back its own import hit exactly that.
    """
    post = Post(
        platform="reddit",
        external_id="p1",
        kind="submission",
        channel="stocks",
        created_at=NOW - timedelta(hours=1),
        author=AuthorInfo(name="user"),
        body="$NVDA calls",
    )
    Ingestor(engine, universe, Clock()).ingest([post])
    export_all(engine, [LocalStore(tmp_path)], NOW)
    con = connect(tmp_path)
    try:
        (created_at,) = con.execute("SELECT created_at FROM social_posts").fetchone()
    finally:
        con.close()
    assert created_at == NOW - timedelta(hours=1)
