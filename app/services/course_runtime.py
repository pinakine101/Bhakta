"""
Рассылка заданий Т1–Т4 по расписанию (локальное время пользователя).
Окна: Т1 утро 9:00–10:00, Т1 вечер 21:00–22:00, Т2 12:00–21:00,
Т3 с момента отправки до 22:00, Т4 — 1 час с момента отправки.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import aiosqlite
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from app.db.repository import Repository
from app.services.schedule_loader import (
    get_schedule,
    get_tasks,
    get_t1_evening_text,
    get_t1_morning_text,
    get_t2_art_text,
    get_t2_choice_text,
    get_t2_final_text,
    get_t2_life_theme_text,
    get_t3_text,
    get_t4_challenge_text,
    t2_art_pool,
    t2_choice_sets_pool,
    t2_life_themes_pool,
)

TASK_POINTS = {
    "T1_morning": 1,
    "T1_evening": 1,
    "T2": 2,
    "T3": 3,
    "T4": 4,
}


def _local_now(tz_name: str) -> datetime:
    return datetime.now(ZoneInfo(tz_name))


def _iso(dt: datetime) -> str:
    return dt.isoformat(sep=" ", timespec="seconds")


def _pick_t4_id(tasks: dict, age_group: str | None, assignment_id: int) -> dict:
    t4_list = tasks.get("t4_tasks", [])
    if not t4_list:
        return {"id": 0, "description": "Короткое телесное задание"}
    ag = (age_group or "").strip()
    pool = [t for t in t4_list if str(t.get("age_group", "")).strip() == ag]
    if not pool:
        pool = t4_list
    idx = (assignment_id + hash(ag)) % len(pool)
    return pool[idx]


def build_done_skip_keyboard(row_id: int, week_num: int, show_skip: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="Выполнил", callback_data=f"cv2:done:{row_id}")]]
    if show_skip and week_num >= 2:
        rows[0].append(InlineKeyboardButton(text="Пропустить", callback_data=f"cv2:skip:{row_id}"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_accept_keyboard(row_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Принять", callback_data=f"cv2:accept:{row_id}")]]
    )


def _parse_hhmm(s: str | None, default_h: int, default_m: int = 0) -> tuple[int, int]:
    if not s or ":" not in s:
        return default_h, default_m
    try:
        parts = s.strip().split(":")
        return int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        return default_h, default_m


async def ensure_scheduled_rows_for_today(repo: Repository, user_id: int, tz_name: str) -> None:
    """Создаёт строки user_scheduled_tasks на сегодня с окнами (без отправки)."""
    v2 = await repo.get_user_v2(user_id)
    if not v2 or not v2.get("age_group") or not v2.get("birth_date"):
        return
    sched = get_schedule(v2["age_group"])
    day_num = min(30, max(1, v2.get("course_day", 1)))
    today = _local_now(tz_name).date()
    d = today.isoformat()

    morning_targets = sched.get("t1_morning_targets", [30] * 30)
    rhythms = sched.get("t1_evening_rhythms", ["4:2:4:2"] * 30)
    cycles = sched.get("t1_evening_cycles", [5] * 30)
    idx = day_num - 1
    target = morning_targets[idx] if idx < len(morning_targets) else 30
    rhythm = rhythms[idx] if idx < len(rhythms) else "4:2:4:2"
    n_cycles = cycles[idx] if idx < len(cycles) else 5

    start = datetime.combine(today, datetime.min.time()).replace(tzinfo=ZoneInfo(tz_name))
    t1m_start = start.replace(hour=9, minute=0, second=0)
    t1m_end = start.replace(hour=10, minute=0, second=0)
    t1e_start = start.replace(hour=21, minute=0, second=0)
    t1e_end = start.replace(hour=22, minute=0, second=0)
    settings = await repo.get_user_settings(user_id)
    t2_h, t2_m = _parse_hhmm(settings.get("t2_time"), 12, 0)
    t2_start = start.replace(hour=t2_h, minute=t2_m, second=0)
    t2_end = start.replace(hour=21, minute=0, second=0)

    await repo.upsert_scheduled_task(
        user_id, "T1_morning", d, "morning", None, _iso(t1m_start), _iso(t1m_end), None
    )
    await repo.upsert_scheduled_task(
        user_id, "T1_evening", d, "evening", None, _iso(t1e_start), _iso(t1e_end), None
    )

    t2_assign = next((x for x in sched.get("t2_assignments", []) if x["day"] == day_num), None)
    week1_block = v2.get("week_num", 1) == 1
    if t2_assign and not (week1_block and not await repo.t1_morning_completed_today(user_id, d)):
        await repo.upsert_scheduled_task(
            user_id, "T2", d, "", t2_assign.get("id"), _iso(t2_start), _iso(t2_end), None
        )

    t3_assign = next((x for x in sched.get("t3_assignments", []) if x["day"] == day_num), None)
    if t3_assign:
        if settings.get("t3_time"):
            th, tm = _parse_hhmm(settings.get("t3_time"), 15, 0)
            t3_send = start.replace(hour=th, minute=tm, second=0)
        else:
            offset_min = (user_id % 240)
            t3_send = start.replace(hour=14, minute=0, second=0) + timedelta(minutes=offset_min)
            if t3_send.hour >= 18:
                t3_send = start.replace(hour=17, minute=30, second=0)
        t3_end = start.replace(hour=22, minute=0, second=0)
        await repo.upsert_scheduled_task(
            user_id, "T3", d, "", t3_assign.get("id"), _iso(t3_send), _iso(t3_end), None
        )

    t4_ids = sched.get("t4_assignments", [1] * 30)
    t4_id = t4_ids[idx] if idx < len(t4_ids) else 1
    if settings.get("t4_time"):
        t4h, t4m = _parse_hhmm(settings.get("t4_time"), 14, 0)
        t4_send = start.replace(hour=t4h, minute=t4m, second=0)
    else:
        t4_offset = (user_id * 7) % 600
        t4_send = start.replace(hour=10, minute=0, second=0) + timedelta(minutes=t4_offset % 360)
        if t4_send.hour >= 20:
            t4_send = start.replace(hour=19, minute=0, second=0)
    t4_end = t4_send + timedelta(hours=1)
    await repo.upsert_scheduled_task(user_id, "T4", d, "", t4_id, _iso(t4_send), _iso(t4_end), None)


async def dispatch_due_tasks(bot: Bot, repo: Repository, user_id: int, tz_name: str) -> None:
    """Отправляет задания, у которых наступило время отправки и ещё не было sent_at."""
    await ensure_scheduled_rows_for_today(repo, user_id, tz_name)
    now = _local_now(tz_name)
    now_s = _iso(now)
    await repo.expire_pending_tasks(user_id, now_s)

    v2 = await repo.get_user_v2(user_id)
    if not v2 or not v2.get("age_group"):
        return
    week_num = int(v2.get("week_num") or 1)
    sched = get_schedule(v2["age_group"])
    tasks = get_tasks()
    day_num = min(30, max(1, v2.get("course_day", 1)))
    today_s = now.date().isoformat()
    idx = day_num - 1
    morning_targets = sched.get("t1_morning_targets", [30] * 30)
    rhythms = sched.get("t1_evening_rhythms", ["4:2:4:2"] * 30)
    cycles = sched.get("t1_evening_cycles", [5] * 30)
    target = morning_targets[idx] if idx < len(morning_targets) else 30
    rhythm = rhythms[idx] if idx < len(rhythms) else "4:2:4:2"
    n_cycles = cycles[idx] if idx < len(cycles) else 5

    async with aiosqlite.connect(repo.db_path) as db:
        cursor = await db.execute(
            """
            SELECT id, task_type, task_id, window_start, window_end, sent_at, completed, skipped
            FROM user_scheduled_tasks
            WHERE user_id = ? AND task_date = ? AND sent_at IS NULL AND completed = 0 AND skipped = 0
            """,
            (user_id, today_s),
        )
        rows = await cursor.fetchall()

    for row_id, task_type, task_id, ws, we, sent_at, comp, skip in rows:
        if sent_at or comp or skip:
            continue
        try:
            wstart = datetime.fromisoformat(ws.replace("Z", "+00:00"))
            if wstart.tzinfo is None:
                wstart = wstart.replace(tzinfo=ZoneInfo(tz_name))
        except Exception:
            continue
        if now < wstart:
            continue

        text = ""
        need_word = True
        kb = build_done_skip_keyboard(row_id, week_num, True)

        if task_type == "T1_morning":
            text = get_t1_morning_text(day_num, target)
            need_word = False
        elif task_type == "T1_evening":
            text = get_t1_evening_text(day_num, rhythm, n_cycles)
        elif task_type == "T2":
            t2 = next((x for x in sched.get("t2_assignments", []) if x["day"] == day_num), None)
            if not t2:
                continue
            typ = t2.get("type")
            if typ == "art":
                arts = t2_art_pool(tasks)
                art = next((a for a in arts if a["id"] == t2.get("id")), arts[0] if arts else {"id": 0, "duration_min": 2})
                text = get_t2_art_text(art)
            elif typ == "life_theme":
                themes = t2_life_themes_pool(tasks)
                th = next((t for t in themes if t["id"] == t2.get("id")), None)
                text = get_t2_life_theme_text(th or (themes[0] if themes else {"title": "Тема", "prompt": "Напиши текст."}))
            elif typ == "choice":
                sets = t2_choice_sets_pool(tasks)
                cs = next((c for c in sets if c["id"] == t2.get("id")), None)
                text = get_t2_choice_text(cs["image_ids"], tasks) if cs else ""
            elif typ == "final":
                text = get_t2_final_text(tasks)
                need_word = True
        elif task_type == "T3":
            t3 = next((x for x in sched.get("t3_assignments", []) if x["day"] == day_num), None)
            if not t3:
                continue
            key = {"text": "t3_texts", "skill": "t3_skills", "creativity": "t3_creativity", "practice": "t3_practices"}.get(
                t3.get("type"), "t3_texts"
            )
            pool = tasks.get(key, [])
            item = next((x for x in pool if x["id"] == t3.get("id")), pool[0] if pool else {"text": "Задание"})
            text = get_t3_text(item)
            if week_num >= 2:
                need_word = False
        elif task_type == "T4":
            t4 = _pick_t4_id(tasks, v2.get("age_group"), int(task_id or 1))
            text = get_t4_challenge_text(t4.get("description", ""))
            kb = build_accept_keyboard(row_id)

        if not text:
            continue

        await repo.mark_scheduled_sent(row_id, now_s)
        try:
            await bot.send_message(chat_id=user_id, text=text, reply_markup=kb)
        except Exception:
            pass