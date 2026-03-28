from app.services.t1_runtime import evening_breath_duration_sec, parse_rhythm_seconds, schedule_day_index
from datetime import date, timedelta


def test_parse_rhythm() -> None:
    assert parse_rhythm_seconds("4:2:4:2") == 12
    assert parse_rhythm_seconds("4:4:6:4") == 18


def test_evening_duration_includes_30s_cooldown() -> None:
    assert evening_breath_duration_sec("4:2:4:2", 5) == 12 * 5 + 30


def test_schedule_day_index() -> None:
    start = date(2025, 1, 1)
    assert schedule_day_index(start, start, 29) == 0
    assert schedule_day_index(start, start + timedelta(days=29), 29) == 29
    assert schedule_day_index(start, start + timedelta(days=100), 29) == 29
