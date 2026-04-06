import asyncio
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class T1State:
    morning_start: dict[int, tuple[int, datetime, int]] = field(default_factory=dict)
    evening_timers: dict[int, asyncio.Task] = field(default_factory=dict)
    evening_phase: dict[int, int] = field(default_factory=dict)
    pending_evening_word: dict[int, int] = field(default_factory=dict)
    pending_morning_word: dict[int, dict] = field(default_factory=dict)

    def clear_user(self, user_id: int) -> None:
        self.morning_start.pop(user_id, None)
        self.evening_phase.pop(user_id, None)
        self.pending_evening_word.pop(user_id, None)
        self.pending_morning_word.pop(user_id, None)
        t = self.evening_timers.pop(user_id, None)
        if t is not None:
            t.cancel()


@dataclass
class T2Pending:
    row_id: int
    step: str
    art_seconds: int | None = None
    response_draft: str | None = None
    selected_image_id: int | None = None


@dataclass
class T2State:
    pending: dict[int, T2Pending] = field(default_factory=dict)
    art_session: dict[int, tuple[int, datetime]] = field(default_factory=dict)
    art_timers: dict[int, asyncio.Task] = field(default_factory=dict)

    def clear_user(self, user_id: int) -> None:
        self.pending.pop(user_id, None)
        self.art_session.pop(user_id, None)
        t = self.art_timers.pop(user_id, None)
        if t is not None:
            t.cancel()


t1_state = T1State()
t2_state = T2State()