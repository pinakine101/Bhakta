from datetime import date, timedelta

from app.services.schedule_loader import (
    course_calendar_day,
    get_schedule,
    parse_t2_start_clock,
    t2_assignment_for_calendar_day,
)


def test_course_calendar_day_bounds() -> None:
    start = date(2025, 3, 1)
    assert course_calendar_day(start, start) == 1
    assert course_calendar_day(start, start + timedelta(days=29)) == 30
    assert course_calendar_day(start, start + timedelta(days=100)) == 30


def test_t2_assignment_day1() -> None:
    sched = get_schedule("15-25")
    a = t2_assignment_for_calendar_day(sched, 1)
    assert a is not None
    assert a["type"] == "art"
    assert a["id"] == 1


def test_parse_t2_start_clock() -> None:
    assert parse_t2_start_clock(None, 1) == (12, 0)
    assert parse_t2_start_clock("14:30", 3) == (14, 30)
    assert parse_t2_start_clock("13:15", 1, course_cal_day=20) == (13, 15)
