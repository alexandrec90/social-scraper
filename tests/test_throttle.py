import secrets
from datetime import timedelta

import pytest
from conftest import NOW, Clock

from social_scraper import throttle as throttle_mod
from social_scraper.throttle import (
    CHALLENGED,
    ERROR,
    ERROR_STREAK,
    LOGIN_REQUIRED,
    OK,
    RATE_LIMITED,
    TIMED_OUT,
    Block,
    BudgetExhausted,
    CircuitOpen,
    MemoryThrottleStore,
    OutOfTime,
    OutsideActiveHours,
    Policy,
    Throttle,
    in_active_hours,
)


def make(clock=None, **over):
    policy = Policy(
        **{
            "platform": "x",
            "min_delay_s": 10,
            "max_delay_s": 20,
            "hourly_budget": 3,
            "daily_budget": 5,
            **over,
        }
    )
    slept: list[float] = []
    store = MemoryThrottleStore()
    t = Throttle(
        policy, store, clock=clock or Clock(), sleep=slept.append, rng=secrets.SystemRandom()
    )
    return t, store, slept


@pytest.mark.parametrize(
    ("hours", "hour", "allowed"),
    [
        (None, 3, True),
        ((8, 23), 8, True),
        ((8, 23), 22, True),
        ((8, 23), 23, False),
        ((8, 23), 7, False),
        ((22, 6), 23, True),
        ((22, 6), 5, True),
        ((22, 6), 12, False),
    ],
)
def test_in_active_hours(hours, hour, allowed):
    assert in_active_hours(hours, hour) is allowed


def test_first_request_does_not_wait():
    t, _, slept = make()
    t.acquire()
    assert slept == []


def test_spacing_waits_the_jittered_gap():
    clock = Clock()
    t, store, slept = make(clock)
    store.log_request("x", NOW - timedelta(seconds=4), "search", 200, True)
    t.acquire()
    assert len(slept) == 1
    assert 10 - 4 <= slept[0] <= 20 - 4


def test_no_wait_when_gap_already_elapsed():
    t, store, slept = make()
    store.log_request("x", NOW - timedelta(minutes=5), "search", 200, True)
    t.acquire()
    assert slept == []


def test_hourly_and_daily_budgets():
    t, store, _ = make()
    for i in range(3):
        store.log_request("x", NOW - timedelta(minutes=i + 1), "k", 200, True)
    with pytest.raises(BudgetExhausted, match="hourly"):
        t.check()
    store.requests.clear()
    for i in range(5):
        store.log_request("x", NOW - timedelta(hours=i + 2), "k", 200, True)
    with pytest.raises(BudgetExhausted, match="daily"):
        t.check()


def test_budgets_are_per_platform():
    t, store, _ = make()
    for i in range(10):
        store.log_request("reddit", NOW - timedelta(minutes=i), "k", 200, True)
    t.check()


def test_outside_active_hours():
    # NOW is 15:00 UTC = 11:00 in Toronto.
    t, _, _ = make(active_hours=(18, 22), timezone="America/Toronto")
    with pytest.raises(OutsideActiveHours):
        t.check()
    t2, _, _ = make(active_hours=(8, 23), timezone="America/Toronto")
    t2.check()


def test_no_page_load_starts_after_the_deadline():
    clock = Clock()
    t, _, slept = make(clock, deadline=NOW + timedelta(minutes=1))
    t.acquire()
    clock.now = NOW + timedelta(minutes=2)
    with pytest.raises(OutOfTime, match="before the next is due"):
        t.check()
    assert slept == []


def test_a_gap_that_would_end_past_the_deadline_is_not_waited():
    """2026-10-07: a 29-minute scrape on a 30-minute schedule made the scheduler skip
    the next fire. The last X gap is up to three minutes; it is not slept into the next run."""
    t, store, slept = make(deadline=NOW + timedelta(seconds=5))
    store.log_request("x", NOW - timedelta(seconds=1), "search", 200, True)
    with pytest.raises(OutOfTime):
        t.acquire()
    assert slept == []


def test_no_deadline_is_no_limit():
    clock = Clock()
    t, _, _ = make(clock)
    clock.now = NOW + timedelta(days=30)
    t.acquire()


def test_rate_limit_backs_off_exponentially_and_caps():
    clock = Clock()
    t, store, _ = make(clock, backoff_base_minutes=15, backoff_max_minutes=40)
    t.record(RATE_LIMITED, "search", 429)
    assert store.get_block("x").blocked_until == NOW + timedelta(minutes=15)
    t.record(RATE_LIMITED, "search", 429)
    assert store.get_block("x").blocked_until == NOW + timedelta(minutes=30)
    t.record(RATE_LIMITED, "search", 429)
    assert store.get_block("x").blocked_until == NOW + timedelta(minutes=40)
    with pytest.raises(CircuitOpen):
        t.check()


def test_challenge_opens_the_circuit_for_the_cooldown():
    t, store, _ = make(block_cooldown_hours=12)
    t.record(CHALLENGED, "search", 403, "captcha")
    block = store.get_block("x")
    assert block.blocked_until == NOW + timedelta(hours=12)
    assert "captcha" in block.reason
    with pytest.raises(CircuitOpen):
        t.acquire()


def test_repeated_challenges_double_the_cooldown_up_to_the_cap():
    # The first request after a cooldown being challenged again means the platform still
    # has us flagged: retrying every `block_cooldown_hours` forever is how a flag
    # becomes a ban on a machine that never stops.
    t, store, _ = make(block_cooldown_hours=12, block_cooldown_max_hours=40)
    t.record(CHALLENGED, "search", 403)
    assert store.get_block("x").blocked_until == NOW + timedelta(hours=12)
    t.record(CHALLENGED, "search", 403)
    assert store.get_block("x").blocked_until == NOW + timedelta(hours=24)
    t.record(CHALLENGED, "search", 403)
    assert store.get_block("x").blocked_until == NOW + timedelta(hours=40)


def test_a_success_resets_the_challenge_escalation():
    t, store, _ = make(block_cooldown_hours=12)
    t.record(CHALLENGED, "search", 403)
    t.record(CHALLENGED, "search", 403)
    t.record(OK, "search", 200)
    t.record(CHALLENGED, "search", 403)
    assert store.get_block("x").blocked_until == NOW + timedelta(hours=12)


def test_plain_errors_pause_only_when_repeated():
    t, store, _ = make()
    t.record(ERROR, "k", 500)
    t.record(ERROR, "k", 500)
    assert store.get_block("x").blocked_until is None
    t.record(ERROR, "k", 500)
    assert store.get_block("x").blocked_until is not None


def test_a_timeout_is_a_plain_error_and_failing_is_the_pause_streak():
    t, store, _ = make()
    for _ in range(ERROR_STREAK - 1):
        t.record(TIMED_OUT, "k", None, "page load timed out")
    assert not t.failing() and store.get_block("x").blocked_until is None
    t.record(TIMED_OUT, "k", None, "page load timed out")
    assert t.failing() and store.get_block("x").blocked_until is not None
    t.record(OK, "k", 200)
    assert not t.failing()


def test_a_first_challenge_is_a_notice_and_one_after_the_cooldown_an_error():
    t, _, _ = make()
    t.record(CHALLENGED, "k", 403)
    assert t.notice(CHALLENGED)
    # The first load after the cooldown is challenged again: the block held.
    t.record(CHALLENGED, "k", 403)
    assert not t.notice(CHALLENGED)
    t.record(OK, "k", 200)
    t.record(CHALLENGED, "k", 403)
    assert t.notice(CHALLENGED)


def test_a_challenge_on_top_of_another_failure_is_an_error():
    t, _, _ = make()
    t.record(ERROR, "k", 502)
    t.record(CHALLENGED, "k", 403)
    assert not t.notice(CHALLENGED)


def test_notice_reads_timeouts_as_failing_does_and_nothing_else_as_one():
    t, _, _ = make()
    t.record(TIMED_OUT, "k", None)
    assert t.notice(TIMED_OUT)
    for _ in range(ERROR_STREAK - 1):
        t.record(TIMED_OUT, "k", None)
    assert not t.notice(TIMED_OUT)
    fresh, _, _ = make()
    fresh.record(ERROR, "k", 502)
    assert not fresh.notice(ERROR)


def test_the_only_failed_load_of_the_day_being_a_rate_limit_is_a_notice():
    # 2026-10-12: one X rate-limit page after a day of clean loads failed the scrape.
    clock = Clock(NOW)
    t, _, _ = make(clock)
    t.record(OK, "k", 200)
    t.record(RATE_LIMITED, "k", 200, "rate-limit page")
    assert t.notice(RATE_LIMITED)


def test_a_rate_limit_again_after_the_backoff_is_an_error():
    t, _, _ = make()
    t.record(RATE_LIMITED, "k", 429)
    t.record(RATE_LIMITED, "k", 429)
    assert not t.notice(RATE_LIMITED)


def test_a_second_rate_limit_in_the_day_after_good_loads_doubles_the_pause():
    # 2026-10-23: X limited a timeline at 15:38 and a search at 17:05 with a clean cycle
    # between. The success had reset the streak, so both got the 30-minute base pause and
    # the second failed the scrape, though only a longer wait was ever going to lift it.
    clock = Clock(NOW)
    t, store, _ = make(clock, backoff_base_minutes=30, backoff_max_minutes=360)
    t.record(RATE_LIMITED, "timeline", 200, "rate-limit page")
    assert store.get_block("x").blocked_until == NOW + timedelta(minutes=30)
    assert t.notice(RATE_LIMITED)
    clock.now += timedelta(minutes=58)
    t.record(OK, "search", 200)
    clock.now += timedelta(minutes=29)
    t.record(RATE_LIMITED, "search", 200, "rate-limit page")
    assert store.get_block("x").consecutive_failures == 1
    assert store.get_block("x").blocked_until == clock.now + timedelta(minutes=60)
    assert t.notice(RATE_LIMITED)


def test_a_rate_limit_the_longest_pause_answers_is_an_error():
    # A success resets the breaker's streak; the day's failures still see the pattern,
    # and once the pause is the longest the breaker has, waiting is not working.
    clock = Clock(NOW)
    t, store, _ = make(clock, backoff_base_minutes=30, backoff_max_minutes=240)
    pauses, notices = [], []
    for _ in range(4):
        t.record(RATE_LIMITED, "k", 429)
        pauses.append(store.get_block("x").blocked_until - clock.now)
        notices.append(t.notice(RATE_LIMITED))
        clock.now += timedelta(hours=1)
        t.record(OK, "k", 200)
    assert pauses == [timedelta(minutes=m) for m in (30, 60, 120, 240)]
    assert notices == [True, True, True, False]


def test_a_rate_limit_after_a_failure_outside_the_window_is_a_notice():
    clock = Clock(NOW)
    t, store, _ = make(clock, backoff_base_minutes=15)
    t.record(ERROR, "k", 502)
    t.record(OK, "k", 200)
    clock.now += throttle_mod.RATE_LIMIT_WINDOW + timedelta(minutes=1)
    t.record(RATE_LIMITED, "k", 429)
    assert store.get_block("x").blocked_until == clock.now + timedelta(minutes=15)
    assert t.notice(RATE_LIMITED)


def test_a_rate_limit_on_top_of_another_failed_load_is_an_error():
    t, _, _ = make()
    t.record(TIMED_OUT, "k", None)
    t.record(RATE_LIMITED, "k", 429)
    assert not t.notice(RATE_LIMITED)


def test_success_closes_the_circuit():
    t, store, _ = make()
    t.record(ERROR, "k", 500)
    t.record(OK, "k", 200)
    assert store.get_block("x") == Block(None, None, 0)


def test_login_required_is_logged_but_does_not_trip_the_breaker():
    t, store, _ = make()
    t.record(LOGIN_REQUIRED, "search", 200)
    assert store.get_block("x") == Block(None, None, 0)
    assert store.count_since("x", NOW - timedelta(minutes=1)) == 1


def test_expired_block_allows_requests():
    clock = Clock()
    t, _store, _ = make(clock)
    t.record(CHALLENGED, "k")
    clock.now = NOW + timedelta(hours=13)
    t.check()


def test_dwell_sleeps_within_bounds():
    t, _, slept = make()
    t.dwell(1, 2)
    assert 1 <= slept[0] <= 2


def test_default_dwell_reads_like_a_person():
    t, _, slept = make()
    for _ in range(50):
        t.dwell()
    assert all(4 <= s <= 12 for s in slept)


def test_memory_store_last_request():
    store = MemoryThrottleStore()
    assert store.last_request_at("x") is None
    store.log_request("x", NOW, "k", 200, True)
    store.log_request("x", NOW - timedelta(hours=1), "k", 200, True)
    assert store.last_request_at("x") == NOW


def test_stop_reasons_share_a_base_the_runner_catches():
    stops = (
        throttle_mod.BudgetExhausted,
        throttle_mod.CircuitOpen,
        throttle_mod.OutsideActiveHours,
        throttle_mod.OutOfTime,
    )
    for exc in stops:
        assert issubclass(exc, throttle_mod.ThrottleStop)


def test_memory_store_satisfies_the_store_protocol():
    store: throttle_mod.ThrottleStore = MemoryThrottleStore()
    assert store.get_block("x") == Block(None, None, 0)
