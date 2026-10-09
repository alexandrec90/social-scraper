import io
import threading
from datetime import UTC, datetime, timedelta

from social_scraper.watchdog import Watchdog, print_stacks

NOW = datetime(2026, 10, 9, 19, 0, tzinfo=UTC)


def test_a_cycle_that_finishes_first_is_never_ended():
    ended = []
    guard = Watchdog(NOW + timedelta(hours=1), lambda: 7, ended.append, now=NOW).start()
    assert guard.finish() is True
    assert ended == []


def test_a_cycle_still_going_at_the_deadline_is_ended_with_the_expiry_code():
    ended = threading.Event()
    codes = []

    def end(code):
        codes.append(code)
        ended.set()

    guard = Watchdog(NOW - timedelta(seconds=1), lambda: 3, end, now=NOW).start()
    assert ended.wait(5), "a deadline already past fires at once"
    assert codes == [3]
    assert guard.finish() is False, "the watchdog has claimed the ending"
    assert guard.outcome() == 3


def test_a_cycle_that_finishes_first_stops_a_late_expiry():
    expired = []
    guard = Watchdog(NOW + timedelta(seconds=30), lambda: expired.append(1) or 1, print, now=NOW)
    guard.start()
    guard.finish()
    guard._fire()
    assert expired == [], "only one of the two ends the run"


def test_print_stacks_shows_where_a_blocked_thread_is():
    release = threading.Event()

    def blocked_on_the_database():
        release.wait(5)

    worker = threading.Thread(target=blocked_on_the_database)
    worker.start()
    try:
        out = io.StringIO()
        print_stacks(out)
    finally:
        release.set()
        worker.join()
    assert "blocked_on_the_database" in out.getvalue()
    assert f"thread {threading.get_ident()} was in" not in out.getvalue(), "not its own"
