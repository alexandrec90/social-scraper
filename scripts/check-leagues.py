"""Is every league the operator sees on Mise-o-jeu+ in `sports-leagues.toml`?

The league list is maintained by hand, and Mise-o-jeu+ is never scraped. What *is*
recorded is sports_betting's overlay coverage file (`logs/overlay-coverage.json` in that
repo): per day, how many events of each `SPORT|League` the operator's own browsing
showed, counts only. This compares those labels with the `mise_o_jeu` labels in the
league list and reports:

- **unmapped** -- seen on Mise-o-jeu+, claimed by no league. Each one is a league to add
  to the TOML (or a label to add to an existing league's `mise_o_jeu`). Fails the check.
- **unseen** -- a label in the TOML the coverage file has never shown. Informational: the
  operator may simply not have browsed that sport.

A `mise_o_jeu` entry is either the full `SPORT|League` label or just the league part.
The result goes to `logs/league-check.json`, on failure too, overwritten each run.

    uv run python scripts/check-leagues.py [--coverage PATH] [--leagues PATH]

Exit 0 when nothing is unmapped, 1 when something is, 2 when a file cannot be read.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_COVERAGE = REPO_ROOT.parent / "sports_betting" / "logs" / "overlay-coverage.json"
DEFAULT_LEAGUES = REPO_ROOT / "sports-leagues.toml"
ARTIFACT = REPO_ROOT / "logs" / "league-check.json"


def coverage_labels(payload: Any) -> dict[str, int]:
    """`{"SPORT|League": events seen}` summed over every day in the coverage file."""
    totals: dict[str, int] = defaultdict(int)
    days = payload.get("days") if isinstance(payload, dict) else None
    if not isinstance(days, dict):
        raise ValueError("not an overlay coverage file (no `days` object)")
    for rows in days.values():
        for label, row in (rows or {}).items():
            events = row.get("events", 0) if isinstance(row, dict) else 0
            totals[label] += int(events) if isinstance(events, int) else 0
    return dict(totals)


def claims(label: str, wanted: Iterable[str]) -> bool:
    """A league's `mise_o_jeu` entries claim `label` (full label or league part)."""
    league = label.partition("|")[2]
    lowered = {w.casefold() for w in wanted}
    return label.casefold() in lowered or (bool(league) and league.casefold() in lowered)


def compare(seen: dict[str, int], leagues: dict[str, tuple[str, ...]]) -> dict[str, Any]:
    """The check's result for coverage `seen` against `{league key: mise_o_jeu labels}`."""
    every = [w for labels in leagues.values() for w in labels]
    unmapped = [
        {"label": label, "events": n}
        for label, n in sorted(seen.items(), key=lambda kv: (-kv[1], kv[0]))
        if not claims(label, every)
    ]
    unseen = sorted(
        f"{key}: {w}"
        for key, labels in leagues.items()
        for w in labels
        if not any(claims(label, [w]) for label in seen)
    )
    return {
        "ok": not unmapped,
        "labels_seen": len(seen),
        "unmapped": unmapped,
        "unseen": unseen,
        "leagues_without_labels": sorted(k for k, labels in leagues.items() if not labels),
    }


def write_artifact(body: dict[str, Any], path: Path = ARTIFACT) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, indent=2), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--coverage", type=Path, default=DEFAULT_COVERAGE)
    parser.add_argument("--leagues", type=Path, default=DEFAULT_LEAGUES)
    parser.add_argument("--artifact", type=Path, default=ARTIFACT)
    args = parser.parse_args(argv)
    # Imported here so `--help` works before the venv is provisioned.
    from social_scraper.sports.leagues import LeagueConfigError, load_leagues

    try:
        leagues = {lg.key: lg.mise_o_jeu for lg in load_leagues(args.leagues)}
        seen = coverage_labels(json.loads(args.coverage.read_text(encoding="utf-8")))
    except (OSError, ValueError, LeagueConfigError) as exc:
        error = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        path = write_artifact(error, args.artifact)
        print(f"FAILED -- cannot read inputs: {exc} (details in {path})")
        return 2
    result = {
        "coverage": str(args.coverage),
        "leagues": str(args.leagues),
        **compare(seen, leagues),
    }
    path = write_artifact(result, args.artifact)
    status = "ok" if result["ok"] else f"FAILED -- {len(result['unmapped'])} unmapped league(s)"
    print(f"{status} -- details in {path}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
