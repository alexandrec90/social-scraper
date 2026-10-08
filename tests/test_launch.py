from conftest import NOW

from social_scraper import launch
from social_scraper.db import repository
from social_scraper.models import REDDIT, X


def streak(engine, platform):
    with engine.connect() as conn:
        return repository.get_state(conn, launch.state_key(platform))


def test_launch_timeouts_skip_below_the_streak_and_error_at_it(engine):
    exc = TimeoutError("Timeout 180000ms exceeded.")
    results = [launch.timed_out(engine, REDDIT, exc, NOW) for _ in range(launch.LAUNCH_STREAK)]
    assert all("skipped" in r for r in results[:-1])
    assert "180000ms" in results[-1]["error"]
    assert streak(engine, REDDIT) == str(launch.LAUNCH_STREAK)


def test_the_streak_is_per_platform(engine):
    launch.timed_out(engine, REDDIT, TimeoutError("t"), NOW)
    assert streak(engine, X) is None
    assert launch.state_key(REDDIT) != launch.state_key(X)


def test_a_started_browser_ends_the_streak_and_writes_nothing_without_one(engine):
    launch.started(engine, X, NOW)
    assert streak(engine, X) is None
    launch.timed_out(engine, X, TimeoutError("t"), NOW)
    launch.started(engine, X, NOW)
    assert streak(engine, X) == "0"
    assert "skipped" in launch.timed_out(engine, X, TimeoutError("t"), NOW)
