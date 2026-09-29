"""Read-only DuckDB lens over what this project wrote to the lake.

Research and verification only. Nothing on a hot path queries the lake -- consumers
restore into their own database first (`.claude/rules/data-lake.md`, rule 1).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import duckdb

from social_scraper.archive.datasets import PREFIX

VIEWS = {
    "social_posts": "posts",
    "social_mentions": "mentions",
    "social_engagement": "engagement",
    "social_symbols": "symbols",
}

# Per symbol and day: how much it was talked about, by how many distinct people, and
# which way the slang leaned. The shape a sentiment feature starts from.
SYMBOL_DAILY_SQL = """
SELECT m.symbol,
       CAST(m.created_at AS DATE) AS day,
       count(*) AS mentions,
       count(DISTINCT p.author_hash) AS authors,
       round(avg(p.stance), 3) AS mean_stance,
       sum(coalesce(p.score, 0)) AS engagement
FROM social_mentions m
JOIN social_posts p USING (platform, external_id)
WHERE m.in_universe AND m.created_at >= ?
GROUP BY ALL
ORDER BY day DESC, mentions DESC
LIMIT ?
"""


def connect(root: Path) -> duckdb.DuckDBPyConnection:
    """An in-memory DuckDB with one view per dataset that has data on disk."""
    con = duckdb.connect(":memory:")
    for view, folder in VIEWS.items():
        base = root / PREFIX / folder
        if not any(base.rglob("part.parquet")):
            continue
        pattern = (base / "**" / "part.parquet").as_posix()
        # The relational API, not an interpolated CREATE VIEW: no SQL is built from a path.
        con.read_parquet(pattern, union_by_name=True, hive_partitioning=False).create_view(view)
    return con


def symbol_daily(con: duckdb.DuckDBPyConnection, since: datetime, limit: int = 50) -> list[tuple]:
    return con.execute(SYMBOL_DAILY_SQL, [since, limit]).fetchall()
