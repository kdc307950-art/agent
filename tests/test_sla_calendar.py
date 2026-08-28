from datetime import date, datetime, time, timezone

import pytest

from backend.tickets import BusinessCalendar, sla_policy_candidates


def calendar(**overrides):
    values = {
        "timezone_name": "Asia/Shanghai",
        "business_days": frozenset({0, 1, 2, 3, 4}),
        "work_start": time(9, 0),
        "work_end": time(18, 0),
        "holidays": frozenset(),
    }
    values.update(overrides)
    return BusinessCalendar(**values)


def test_business_minutes_cross_day_and_weekend_in_tenant_timezone():
    policy = calendar()
    # Friday 17:30 Asia/Shanghai, expressed as UTC.
    started = datetime(2026, 3, 6, 9, 30, tzinfo=timezone.utc)

    due = policy.add_business_minutes(started, 120)

    # 30 minutes Friday + 90 minutes Monday = Monday 10:30 CST.
    assert due == datetime(2026, 3, 9, 2, 30, tzinfo=timezone.utc)


def test_holiday_is_skipped():
    policy = calendar(holidays=frozenset({date(2026, 3, 9)}))
    started = datetime(2026, 3, 6, 9, 30, tzinfo=timezone.utc)

    due = policy.add_business_minutes(started, 120)

    assert due == datetime(2026, 3, 10, 2, 30, tzinfo=timezone.utc)


def test_before_work_and_after_work_move_to_next_valid_instant():
    policy = calendar()
    before = datetime(2026, 3, 2, 0, 0, tzinfo=timezone.utc)  # Monday 08:00 CST
    after = datetime(2026, 3, 2, 11, 0, tzinfo=timezone.utc)  # Monday 19:00 CST

    assert policy.add_business_minutes(before, 30) == datetime(2026, 3, 2, 1, 30, tzinfo=timezone.utc)
    assert policy.add_business_minutes(after, 30) == datetime(2026, 3, 3, 1, 30, tzinfo=timezone.utc)


def test_calendar_rejects_naive_time_unknown_zone_and_invalid_hours():
    policy = calendar()
    with pytest.raises(ValueError, match="时区"):
        policy.add_business_minutes(datetime(2026, 3, 2, 9, 0), 30)
    with pytest.raises(ValueError, match="未知 SLA 时区"):
        calendar(timezone_name="Mars/Olympus")
    with pytest.raises(ValueError, match="结束时间"):
        calendar(work_start=time(18, 0), work_end=time(9, 0))


def test_sla_policy_candidates_fall_back_by_parent_category():
    assert sla_policy_candidates("it.vpn") == ("it.vpn", "it")
    assert sla_policy_candidates("it.account") == ("it.account", "it")
    assert sla_policy_candidates("it") == ("it",)
    assert sla_policy_candidates("it.vpn.wifi") == ("it.vpn.wifi", "it.vpn", "it")
    assert sla_policy_candidates(None) == ()
    assert sla_policy_candidates("") == ()
