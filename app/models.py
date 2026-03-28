from __future__ import annotations

from dataclasses import dataclass


@dataclass
class User:
    telegram_id: int
    username: str
    course_day: int = 1
