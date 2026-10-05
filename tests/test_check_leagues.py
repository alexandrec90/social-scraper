"""`scripts/check-leagues.py`: the league list against what Mise-o-jeu+ showed."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

LEAGUES = """
[[league]]
key = "nhl"
name = "NHL"
sport = "hockey"
mise_o_jeu = ["ICE_HOCKEY|NHL"]

[[league]]
key = "epl"
name = "Premier League"
sport = "soccer"
mise_o_jeu = ["English Premier League", "FOOTBALL|FA Cup"]

[[league]]
key = "lol"
name = "LoL"
sport = "esports"
"""

# The shape sports_betting's `overlay/coverage.py` writes: days -> "SPORT|League" -> counts.
COVERAGE = {
    "days": {
        "2026-10-02": {
            "ICE_HOCKEY|NHL": {"events": 12, "priced": 10},
            "FOOTBALL|English Premier League": {"events": 10, "priced": 9},
            "BASKETBALL|EuroLeague": {"events": 3, "priced": 0},
        },
        "2026-10-03": {
            "BASKETBALL|EuroLeague": {"events": 4, "priced": 1},
            "TENNIS|ATP Shanghai": {"events": 20, "priced": 20},
        },
    }
}


def _load():
    spec = importlib.util.spec_from_file_location(
        "check_leagues_script", REPO_ROOT / "scripts" / "check-leagues.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def check():
    return _load()


def test_coverage_labels_sums_days(check):
    assert check.coverage_labels(COVERAGE)["BASKETBALL|EuroLeague"] == 7
    with pytest.raises(ValueError, match="days"):
        check.coverage_labels({"rows": []})
    assert check.coverage_labels({"days": {"d": {"X|Y": "garbage"}}}) == {"X|Y": 0}


def test_claims_full_label_or_league_part_any_case(check):
    assert check.claims("ICE_HOCKEY|NHL", ["ice_hockey|nhl"])
    assert check.claims("FOOTBALL|English Premier League", ["English Premier League"])
    assert not check.claims("FOOTBALL|FA Cup", ["FA"])
    assert not check.claims("NOLEAGUE", ["NOLEAGUE|x"])


def test_compare_reports_unmapped_unseen_and_unlabelled(check):
    seen = check.coverage_labels(COVERAGE)
    leagues = {"nhl": ("ICE_HOCKEY|NHL",), "epl": ("English Premier League", "FOOTBALL|FA Cup")}
    result = check.compare(seen, {**leagues, "lol": ()})
    assert result["ok"] is False
    assert result["unmapped"] == [
        {"label": "TENNIS|ATP Shanghai", "events": 20},
        {"label": "BASKETBALL|EuroLeague", "events": 7},
    ]
    assert result["unseen"] == ["epl: FOOTBALL|FA Cup"]
    assert result["leagues_without_labels"] == ["lol"]
    assert check.compare({"ICE_HOCKEY|NHL": 1}, leagues)["ok"] is True


def test_write_artifact_creates_its_folder_and_overwrites(check, tmp_path):
    path = tmp_path / "logs" / "league-check.json"
    check.write_artifact({"ok": False}, path)
    assert check.write_artifact({"ok": True}, path) == path
    assert json.loads(path.read_text(encoding="utf-8")) == {"ok": True}


def write_inputs(tmp_path, coverage=COVERAGE):
    leagues = tmp_path / "leagues.toml"
    leagues.write_text(LEAGUES, encoding="utf-8")
    cov = tmp_path / "coverage.json"
    cov.write_text(json.dumps(coverage), encoding="utf-8")
    return ["--leagues", str(leagues), "--coverage", str(cov)]


def test_main_fails_on_unmapped_and_writes_the_artifact(check, tmp_path, capsys):
    artifact = tmp_path / "logs" / "league-check.json"
    assert check.main([*write_inputs(tmp_path), "--artifact", str(artifact)]) == 1
    body = json.loads(artifact.read_text(encoding="utf-8"))
    assert body["ok"] is False and len(body["unmapped"]) == 2
    assert "2 unmapped" in capsys.readouterr().out


def test_main_passes_when_everything_is_claimed(check, tmp_path):
    artifact = tmp_path / "out.json"
    coverage = {"days": {"d": {"ICE_HOCKEY|NHL": {"events": 1}}}}
    assert check.main([*write_inputs(tmp_path, coverage), "--artifact", str(artifact)]) == 0
    assert json.loads(artifact.read_text(encoding="utf-8"))["ok"] is True


def test_main_reports_unreadable_inputs_in_the_artifact(check, tmp_path):
    artifact = tmp_path / "out.json"
    args = ["--coverage", str(tmp_path / "missing.json"), "--artifact", str(artifact)]
    assert check.main([*write_inputs(tmp_path)[:2], *args]) == 2
    assert "missing.json" in json.loads(artifact.read_text(encoding="utf-8"))["error"]
