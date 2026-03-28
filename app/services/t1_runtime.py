"""Т1: расписание вечера из JSON, длительность ритма, стартовые цели утра."""

from __future__ import annotations

import json
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"


def resolve_tz_name(tz_name: str | None, fallback: str) -> str:
    name = (tz_name or fallback).strip()
    try:
        ZoneInfo(name)
        return name
    except Exception:
        return fallback

_SCHEDULE_FILES = {
    "15-25": "schedule_15_25.json",
    "25-35": "schedule_25_35.json",
    "35+": "schedule_35_plus.json",
}


def load_schedule(age_group: str) -> dict[str, Any]:
    name = _SCHEDULE_FILES.get(age_group, _SCHEDULE_FILES["25-35"])
    path = _DATA_DIR / name
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def starting_morning_seconds(age_group: str) -> int:
    return {"15-25": 10, "25-35": 20, "35+": 30}.get(age_group, 20)


def parse_rhythm_seconds(rhythm: str) -> int:
    return sum(int(x) for x in rhythm.strip().split(":"))


def evening_breath_duration_sec(rhythm: str, cycles: int) -> int:
    """Ритм × циклы + 30 сек обычного дыхания."""
    return parse_rhythm_seconds(rhythm) * int(cycles) + 30


def schedule_day_index(course_start: date, today: date, max_ix: int) -> int:
    d = (today - course_start).days
    if d < 0:
        return 0
    return min(d, max_ix)


def t1_evening_params_for_day(sched: dict[str, Any], day_ix: int) -> tuple[str, int]:
    rhythms: list[str] = sched["t1_evening_rhythms"]
    cycles_l: list[int] = sched["t1_evening_cycles"]
    ix = min(day_ix, len(rhythms) - 1, len(cycles_l) - 1)
    return rhythms[ix], int(cycles_l[ix])


def local_window_to_utc_iso(
    local_date: date, start_t: time, end_t: time, tz_name: str
) -> tuple[str, str]:
    tz = ZoneInfo(tz_name)
    ws = datetime.combine(local_date, start_t, tzinfo=tz).astimezone(timezone.utc)
    we = datetime.combine(local_date, end_t, tzinfo=tz).astimezone(timezone.utc)
    return ws.strftime("%Y-%m-%dT%H:%M:%SZ"), we.strftime("%Y-%m-%dT%H:%M:%SZ")


T1_MORNING_TITLE = "Т1 Утро"
T1_EVENING_TITLE = "Т1 Вечер"

T1_MORNING_BODY = (
    "Смотрите на точку в центре экрана. Не отводите взгляд. "
    "Нажмите «Начать», когда начнёте, и «Готово», когда отведёте взгляд."
)

T1_EVENING_BODY_TEMPLATE = (
    "Дышите в ритме {rhythm}, {cycles} циклов. После дыхания — 30 секунд обычного дыхания, "
    "внимание на сознание. Нажмите «Начать», когда начнёте, и «Готово» после завершения."
)

TIMER_DONE_TEXT = 'Время вышло. Нажмите «Готово», если завершили.'
