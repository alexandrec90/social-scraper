"""The detect-secrets hook's `--exclude-lines` pattern covers lake partition keys, and
nothing wider: a key is all Base64 characters, so asserting one in a test could fail the
commit by chance (`platform=bluesky/dt=2024-03-10` did, 2026-10-08)."""

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from social_scraper.archive import datasets
from social_scraper.models import BLUESKY, HACKERNEWS, PLATFORMS, STOCKTWITS

CONFIG = Path(__file__).resolve().parents[1] / ".pre-commit-config.yaml"
# Read as text, not YAML: only this hook passes `--exclude-lines`, and a reshaped config
# finds no pattern here, which fails the first test rather than passing it.
_ARG_RE = re.compile(r"^\s*- --exclude-lines\n\s*- '(.+)'$", re.MULTILINE)


def exclude_lines() -> list[re.Pattern[str]]:
    return [re.compile(p) for p in _ARG_RE.findall(CONFIG.read_text(encoding="utf-8"))]


def excluded(line: str) -> bool:
    return any(p.search(line) for p in exclude_lines())


DATED = [
    s
    for s in datasets.ALL
    if "platform=" in s.partition_of({"platform": "x", s.ts_column: datetime.now(UTC)})
]


@pytest.mark.parametrize("spec", DATED, ids=lambda s: s.name)
@pytest.mark.parametrize("platform", [*PLATFORMS, STOCKTWITS, BLUESKY, HACKERNEWS])
def test_every_partition_key_is_excluded(spec, platform):
    key = spec.partition_of(
        {"platform": platform, spec.ts_column: datetime(2024, 3, 10, tzinfo=UTC)}
    )
    assert excluded(f'    assert kept.post["part"] == "{key}"')
    assert excluded(f'    key = "{key}/part.parquet"')


@pytest.mark.parametrize(
    "line",
    [
        'token = "aGVsbG8gd29ybGQgdGhpcyBpcyBhIHNlY3JldCB2YWx1ZQ=="',  # pragma: allowlist secret
        'key = "social_scraper/posts/platform=x/dt=2024-03-10/aGVsbG8gd29ybGQ="',  # pragma: allowlist secret
        'key = "posts/platform=bluesky/dt=2024-03-10"',
    ],
)
def test_other_strings_are_still_scanned(line):
    assert not excluded(line)
