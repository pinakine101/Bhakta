"""Т1 утро/вечер: рассылка в окнах, inline-кнопки, таймер вечера, слово."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardRemove,
)

from app.config import Settings
from app.db.repository import Repository
from app.t2_bot import deliver_t2_slot
from app.timer import DONE_SOUND, START_SOUND, play_sound, start_task_timer
from app.services.t1_runtime import (
    T1_EVENING_BODY_TEMPLATE,
    T1_EVENING_TITLE,
    T1_MORNING_BODY,
    T1_MORNING_TITLE,
    evening_breath_duration_sec,
    load_schedule,
    local_window_to_utc_iso,
    resolve_tz_name,
    schedule_day_index,
    t1_evening_params_for_day,
)
from app.services.schedule_loader import (
    course_calendar_day,
    get_schedule,
    map_schedule_t2_type_to_subtype,
    t2_assignment_for_calendar_day,
)


@dataclass
class T1State:
    """Состояние сессий Т1 в памяти процесса."""

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

def continue_keyboard() -> InlineKeyboardMarkup:
    # Same callback used in app.main.py
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Продолжить", callback_data="tasks:today")]]
    )


_T1_IMAGE_PATH = Path(__file__).resolve().parent.parent / "images" / "Cont_1.jpg"


async def _send_t1_image_if_exists(bot: Bot, chat_id: int) -> None:
    if _T1_IMAGE_PATH.is_file():
        with contextlib.suppress(Exception):
            await bot.send_photo(chat_id, FSInputFile(_T1_IMAGE_PATH))


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _within_window_utc(window_end_iso: str) -> bool:
    return _now_utc_iso() <= window_end_iso


def _within_window_utc_range(window_start_iso: str, window_end_iso: str) -> bool:
    now_iso = _now_utc_iso()
    return window_start_iso <= now_iso <= window_end_iso


def _t4_daily_start_time(uid: int, today: date) -> time:
    # Deterministic pseudo-random start in 09:00..20:00 (1 hour window to max 21:00).
    seed = f"{uid}:{today.isoformat()}".encode("utf-8")
    offset = int(hashlib.sha256(seed).hexdigest()[:8], 16) % 661
    total_minutes = 9 * 60 + offset
    hour, minute = divmod(total_minutes, 60)
    return time(hour, minute)


async def _cancel_timer_safe(t: asyncio.Task | None) -> None:
    if t is None:
        return
    t.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await t


def register_t1_handlers(dp: Dispatcher, repo: Repository, settings: Settings, state: T1State) -> None:
    @dp.callback_query(F.data.startswith("t1:"))
    async def t1_callback(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":")
        if len(parts) != 3 or not callback.from_user or not callback.message:
            await callback.answer("Ошибка", show_alert=True)
            return
        _, kind, sid = parts
        try:
            row_id = int(sid)
        except ValueError:
            await callback.answer("Ошибка", show_alert=True)
            return
        uid = callback.from_user.id
        row = await repo.get_scheduled_task_by_id(row_id)
        if not row or row["user_id"] != uid:
            await callback.answer("Задание не найдено", show_alert=True)
            return
        if row["completed"] or row["skipped"]:
            await callback.answer("Уже обработано")
            return

        if kind == "mo":
            if not _within_window_utc(row["window_end"]):
                await callback.answer("Окно задания закончилось — задание удалено.", show_alert=True)
                return
            await _send_t1_image_if_exists(callback.bot, callback.message.chat.id)
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="Начать", callback_data=f"t1:ms:{row_id}"),
                        InlineKeyboardButton(text="Готово", callback_data=f"t1:md:{row_id}"),
                    ]
                ]
            )
            await callback.message.answer(T1_MORNING_BODY, reply_markup=kb)
            await callback.answer()
            return

        if kind == "ms":
            if row["task_type"] != "T1_morning":
                await callback.answer("Ошибка", show_alert=True)
                return
            if not _within_window_utc(row["window_end"]):
                await callback.answer("Окно задания закончилось — задание удалено.", show_alert=True)
                return
            pr = await repo.get_t1_progress(uid)
            target = int(pr["morning_seconds"]) if pr else 20
            play_sound(START_SOUND)
            state.morning_start[uid] = (row_id, datetime.now(timezone.utc), target)
            mm, ss = divmod(target, 60)
            await callback.message.answer(
                f"<i>⏱ Таймер: {mm:02d}:{ss:02d}</i>",
                parse_mode="HTML",
            )
            await callback.answer("Время пошло")
            return

        if kind == "md":
            if row["task_type"] != "T1_morning":
                await callback.answer("Ошибка", show_alert=True)
                return
            ctx = state.morning_start.pop(uid, None)
            if not ctx:
                await callback.answer("Сначала нажми «Начать»", show_alert=True)
                return
            rid, started, target = ctx
            if rid != row_id:
                await callback.answer("Другое задание", show_alert=True)
                return
            play_sound(DONE_SOUND)
            actual = int((datetime.now(timezone.utc) - started).total_seconds())
            if actual < 0:
                actual = 0
            reached = actual >= target
            # После Т1 утро просим одно слово (как и для вечерней практики).
            state.pending_morning_word[uid] = {
                "row_id": row_id,
                "actual": actual,
                "target": target,
                "task_date": row["task_date"],
                "reached": reached,
            }
            await callback.message.answer("Напишите одно слово.", reply_markup=ReplyKeyboardRemove())
            await callback.answer()
            return

        if kind == "eo":
            if not _within_window_utc(row["window_end"]):
                await callback.answer("Окно задания закончилось — задание удалено.", show_alert=True)
                return
            await _send_t1_image_if_exists(callback.bot, callback.message.chat.id)
            rhythm = row.get("evening_rhythm") or ""
            cycles = int(row.get("evening_cycles") or 0)
            body = T1_EVENING_BODY_TEMPLATE.format(rhythm=rhythm, cycles=cycles)
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="Начать", callback_data=f"t1:es:{row_id}"),
                        InlineKeyboardButton(text="Готово", callback_data=f"t1:ed:{row_id}"),
                    ]
                ]
            )
            await callback.message.answer(body, reply_markup=kb)
            await callback.answer()
            return

        if kind == "es":
            if row["task_type"] != "T1_evening":
                await callback.answer("Ошибка", show_alert=True)
                return
            if not _within_window_utc(row["window_end"]):
                await callback.answer("Окно задания закончилось — задание удалено.", show_alert=True)
                return
            rhythm = row.get("evening_rhythm") or "4:4:6:4"
            cycles = int(row.get("evening_cycles") or 5)
            duration = evening_breath_duration_sec(rhythm, cycles)
            await _cancel_timer_safe(state.evening_timers.pop(uid, None))

            state.evening_phase[uid] = row_id
            state.evening_timers[uid] = asyncio.create_task(
                start_task_timer(
                    chat_id=callback.message.chat.id,
                    bot=callback.bot,
                    duration_seconds=duration,
                    task_name=f"T1 вечер #{row_id}",
                    notify_countdown=False,
                    send_start_message=False,
                )
            )
            mm, ss = divmod(duration, 60)
            await callback.message.answer(
                f"<i>⏱ Таймер: {mm:02d}:{ss:02d}</i>",
                parse_mode="HTML",
            )
            await callback.answer("Таймер запущен")
            return

        if kind == "ed":
            if row["task_type"] != "T1_evening":
                await callback.answer("Ошибка", show_alert=True)
                return
            if state.evening_phase.get(uid) != row_id:
                await callback.answer("Сначала нажми «Начать»", show_alert=True)
                return
            await _cancel_timer_safe(state.evening_timers.pop(uid, None))
            state.evening_phase.pop(uid, None)
            play_sound(DONE_SOUND)
            state.pending_evening_word[uid] = row_id
            await callback.message.answer(
                "Напишите одно слово.",
                reply_markup=ReplyKeyboardRemove(),
            )
            await callback.answer()
            return

        await callback.answer("Неизвестное действие", show_alert=True)

    @dp.message(
        lambda m: m.from_user is not None and m.from_user.id in state.pending_evening_word,
        F.text,
    )
    async def t1_evening_word_message(message: Message) -> None:
        uid = message.from_user.id
        row_id = state.pending_evening_word.pop(uid, None)
        if row_id is None:
            return
        word = (message.text or "").strip()
        if not word or len(word) > 50 or word.startswith("/"):
            state.pending_evening_word[uid] = row_id
            await message.answer("Напиши одно слово (1–50 символов).")
            return
        row = await repo.get_scheduled_task_by_id(row_id)
        if not row or row["user_id"] != uid:
            await message.answer("Задание не найдено.")
            return
        urow = await repo.get_user_row_for_t1(uid)
        ag = (urow or {}).get("age_group")
        now_s = datetime.now().isoformat(sep=" ", timespec="seconds")
        await repo.complete_t1_evening_task(row_id, word, now_s)
        await repo.save_user_word(
            user_id=uid,
            word=word,
            task_type="T1_evening",
            task_id=str(row_id),
            age_group=ag,
        )
        await message.answer("Ваши данные внесены", reply_markup=continue_keyboard())

    @dp.message(
        lambda m: m.from_user is not None and m.from_user.id in state.pending_morning_word,
        F.text,
    )
    async def t1_morning_word_message(message: Message) -> None:
        uid = message.from_user.id
        ctx = state.pending_morning_word.pop(uid, None)
        if not ctx:
            return
        word = (message.text or "").strip()
        if not word or len(word) > 50 or word.startswith("/"):
            state.pending_morning_word[uid] = ctx
            await message.answer("Напиши одно слово (1–50 символов).")
            return
        row_id = int(ctx["row_id"])
        row = await repo.get_scheduled_task_by_id(row_id)
        if not row or row["user_id"] != uid:
            await message.answer("Задание не найдено.")
            return
        urow = await repo.get_user_row_for_t1(uid)
        ag = (urow or {}).get("age_group")
        now_s = datetime.now().isoformat(sep=" ", timespec="seconds")
        await repo.complete_t1_morning_task(
            row_id,
            int(ctx.get("actual") or 0),
            int(ctx.get("target") or 0),
            now_s,
            word=word,
        )
        await repo.update_t1_morning_after_session(uid, str(ctx.get("task_date") or row["task_date"]), bool(ctx.get("reached")))
        await repo.save_user_word(
            user_id=uid,
            word=word,
            task_type="T1_morning",
            task_id=str(row_id),
            age_group=ag,
        )
        await message.answer("Ваши данные внесены", reply_markup=continue_keyboard())


async def deliver_t1_morning_slot(
    bot: Bot, repo: Repository, settings: Settings, uid: int, tz: str, _age_group: str, today: date
) -> None:
    now_iso = _now_utc_iso()
    ws, we = local_window_to_utc_iso(today, time(0, 0), time(23, 59), tz)
    task_date = today.isoformat()
    await repo.upsert_t1_daily_task(uid, "T1_morning", task_date, ws, we, 0)
    meta = await repo.get_t1_row_meta(uid, "T1_morning", task_date)
    if not meta or meta["completed"] or meta["skipped"]:
        return
    tz_name = resolve_tz_name(tz, settings.timezone)
    now_local = datetime.now(ZoneInfo(tz_name))
    if now_local.date() != today:
        return
    if meta["sent_at"]:
        return
    row_id = meta["id"]
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=T1_MORNING_TITLE, callback_data=f"t1:mo:{row_id}")]
        ]
    )
    try:
        await bot.send_message(uid, "Нажми кнопку ниже, чтобы открыть утреннее задание.", reply_markup=kb)
        await repo.mark_scheduled_sent(row_id, now_iso)
    except Exception:
        pass


async def deliver_t1_evening_slot(
    bot: Bot, repo: Repository, settings: Settings, uid: int, tz: str, age_group: str, today: date
) -> None:
    now_iso = _now_utc_iso()
    sched = load_schedule(age_group)
    max_ix = len(sched["t1_evening_rhythms"]) - 1
    urow = await repo.get_user_row_for_t1(uid)
    cs = (urow or {}).get("course_start_date")
    try:
        course_start = date.fromisoformat(cs) if cs else today
    except ValueError:
        course_start = today
    day_ix = schedule_day_index(course_start, today, max_ix)
    rhythm, cycles = t1_evening_params_for_day(sched, day_ix)
    ws, we = local_window_to_utc_iso(today, time(0, 0), time(23, 59), tz)
    task_date = today.isoformat()
    await repo.upsert_t1_daily_task(
        uid, "T1_evening", task_date, ws, we, 0, rhythm, cycles
    )
    meta = await repo.get_t1_row_meta(uid, "T1_evening", task_date)
    if not meta or meta["completed"] or meta["skipped"]:
        return
    tz_name = resolve_tz_name(tz, settings.timezone)
    now_local = datetime.now(ZoneInfo(tz_name))
    if now_local.date() != today:
        return
    if meta["sent_at"]:
        return
    row_id = meta["id"]
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=T1_EVENING_TITLE, callback_data=f"t1:eo:{row_id}")]
        ]
    )
    try:
        await bot.send_message(uid, "Нажми кнопку ниже, чтобы открыть вечернее задание.", reply_markup=kb)
        await repo.mark_scheduled_sent(row_id, now_iso)
    except Exception:
        pass


async def _send_daily_tasks_digest(
    bot: Bot,
    repo: Repository,
    settings: Settings,
    uid: int,
    tz: str,
    age_group: str,
    today: date,
    include_already_sent: bool = False,
) -> None:
    now_iso = _now_utc_iso()
    tz_name = resolve_tz_name(tz, settings.timezone)
    today_s = today.isoformat()
    buttons: list[list[InlineKeyboardButton]] = []
    mark_rows: list[int] = []

    sched = load_schedule(age_group)
    max_ix = max(0, len(sched["t1_evening_rhythms"]) - 1)
    urow = await repo.get_user_row_for_t1(uid)
    cs = (urow or {}).get("course_start_date")
    try:
        course_start = date.fromisoformat(cs) if cs else today
    except ValueError:
        course_start = today
    day_ix = schedule_day_index(course_start, today, max_ix)
    rhythm, cycles = t1_evening_params_for_day(sched, day_ix)

    # T1 (утро): 06:00–09:00
    t1_ws, t1_we = local_window_to_utc_iso(today, time(6, 0), time(9, 0), tz_name)
    await repo.upsert_t1_daily_task(uid, "T1_morning", today_s, t1_ws, t1_we, 0)

    # T1 (вечер): 21:00–00:00 (до конца суток).
    t1e_ws, t1e_we = local_window_to_utc_iso(today, time(21, 0), time(23, 59), tz_name)
    await repo.upsert_t1_daily_task(uid, "T1_evening", today_s, t1e_ws, t1e_we, 0, rhythm, cycles)

    schedule = get_schedule(age_group)
    cal_day = course_calendar_day(course_start, today)

    # T2: 12:00–21:00
    assign = t2_assignment_for_calendar_day(schedule, cal_day)
    if assign:
        subtype = map_schedule_t2_type_to_subtype(str(assign["type"]))
        tid = assign.get("id")
        task_id_val = int(tid) if tid is not None else 0
        t2_ws, t2_we = local_window_to_utc_iso(today, time(12, 0), time(21, 0), tz_name)
        await repo.upsert_t2_daily_task(uid, today_s, t2_ws, t2_we, task_id_val, subtype)

    # T3: 15:00–21:00
    t3_assign = next((x for x in schedule.get("t3_assignments", []) if int(x.get("day") or 0) == cal_day), None)
    if t3_assign:
        t3_id = int(t3_assign.get("id") or 0)
        t3_ws, t3_we = local_window_to_utc_iso(today, time(15, 0), time(21, 0), tz_name)
        await repo.upsert_scheduled_task(uid, "T3", today_s, "", t3_id, t3_ws, t3_we, None)

    # T4: случайный старт 09:00..20:00, окно 1 час (до 21:00)
    t4_list = schedule.get("t4_assignments", [])
    t4_id = int(t4_list[cal_day - 1]) if 0 < cal_day <= len(t4_list) else 0
    if t4_id:
        t4_start_local = datetime.combine(today, _t4_daily_start_time(uid, today))
        t4_end_local = t4_start_local + timedelta(hours=1)
        t4_ws, t4_we = local_window_to_utc_iso(today, t4_start_local.time(), t4_end_local.time(), tz_name)
        await repo.upsert_scheduled_task(uid, "T4", today_s, "", t4_id, t4_ws, t4_we, None)

    # Берем актуальные строки из БД и показываем/шлем только те, чье окно уже открыто.
    rows = await repo.list_scheduled_for_date(uid, today_s)
    seen_button_titles: set[str] = set()
    for r in rows:
        if r["completed"] or r["skipped"]:
            continue
        if not _within_window_utc_range(str(r["window_start"]), str(r["window_end"])):
            continue
        if not include_already_sent and r["sent_at"]:
            continue

        t = str(r["task_type"])
        rid = int(r["id"])
        if t == "T1_morning":
            title = T1_MORNING_TITLE
            cb = f"t1:mo:{rid}"
        elif t == "T1_evening":
            title = T1_EVENING_TITLE
            cb = f"t1:eo:{rid}"
        elif t == "T2":
            title = "Т2"
            cb = f"t2:o:{rid}"
        elif t == "T3":
            title = "Т3"
            cb = f"t3:o:{rid}"
        elif t == "T4":
            title = "Т4"
            cb = f"t4:o:{rid}"
        else:
            continue

        if title not in seen_button_titles:
            buttons.append([InlineKeyboardButton(text=title, callback_data=cb)])
            seen_button_titles.add(title)
        if not r["sent_at"]:
            mark_rows.append(rid)

    if not buttons:
        return

    text = (
        "Список активных заданий.\n"
        "После окончания окна задание удаляется и открыть его уже нельзя."
    )
    await bot.send_message(uid, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    for row_id in mark_rows:
        await repo.mark_scheduled_sent(row_id, now_iso)


async def send_daily_plan_now(bot: Bot, repo: Repository, settings: Settings, uid: int) -> None:
    u = await repo.get_user_row_for_t1(uid)
    if not u or not u.get("age_group"):
        return
    tz = resolve_tz_name(u.get("timezone"), settings.timezone)
    today = datetime.now(ZoneInfo(tz)).date()
    await _send_daily_tasks_digest(
        bot, repo, settings, uid, tz, u["age_group"], today, include_already_sent=True
    )


def _last_day_of_month(d: date) -> date:
    if d.month == 12:
        return date(d.year, 12, 31)
    return date(d.year, d.month + 1, 1) - timedelta(days=1)


async def _maybe_send_weekly_report(
    bot: Bot,
    repo: Repository,
    uid: int,
    now_local: datetime,
) -> None:
    profile = await repo.get_analysis_profile(uid)
    if not profile.get("reports_enabled", True):
        return

    # End-of-week report: Sunday after 21:00 local time.
    if now_local.weekday() != 6 or now_local.time() < time(21, 0):
        return

    week_start = now_local.date() - timedelta(days=now_local.weekday())  # Monday
    week_key = week_start.isoformat()
    if str(profile.get("last_week_key") or "") == week_key:
        return

    earned, max_pts = await repo.weekly_points_and_max(uid, week_start)
    pct = int(round((earned / max_pts) * 100)) if max_pts > 0 else 0
    pass_week = earned >= 45

    text = (
        "Итоги недели:\n"
        f"Баллы: {earned} из {max_pts} ({pct}%).\n"
        f"Проходной порог: 45 — {'пройден' if pass_week else 'не пройден'}."
    )
    await bot.send_message(uid, text)
    await repo.set_last_week_key(uid, week_key)


async def _maybe_send_monthly_stage_notice(
    bot: Bot,
    repo: Repository,
    uid: int,
    now_local: datetime,
) -> None:
    profile = await repo.get_analysis_profile(uid)
    if not profile.get("reports_enabled", True):
        return

    # End-of-month notice: last day of month after 21:00 local time.
    today = now_local.date()
    if today != _last_day_of_month(today) or now_local.time() < time(21, 0):
        return

    month_key = f"{today.year:04d}-{today.month:02d}"
    if str(profile.get("last_month_key") or "") == month_key:
        return

    month_start = date(today.year, today.month, 1)
    earned = await repo.month_points(uid, month_start)
    if earned >= 180:
        await bot.send_message(
            uid,
            "Месяц завершён: первая ступень ПРАСАДА пройдена.\n"
            "Готовность к ступени МАХЕША подтверждена.",
        )
    await repo.set_last_month_key(uid, month_key)


async def t1_scheduler_tick(bot: Bot, repo: Repository, settings: Settings) -> None:
    now_iso = _now_utc_iso()
    for uid, tz_raw, ag, course_start_s in await repo.list_users_active_course():
        tz = resolve_tz_name(tz_raw, settings.timezone)
        now_local = datetime.now(ZoneInfo(tz))
        today = now_local.date()
        today_s = today.isoformat()
        if not course_start_s:
            await repo.ensure_course_start_date_today(uid, today_s)
            course_start_s = today_s
        try:
            date.fromisoformat(course_start_s)
        except ValueError:
            await repo.ensure_course_start_date_today(uid, today_s)

        await repo.expire_pending_tasks(uid, now_iso)
        await _send_daily_tasks_digest(bot, repo, settings, uid, tz, ag, today)
        await _maybe_send_weekly_report(bot, repo, uid, now_local)
        await _maybe_send_monthly_stage_notice(bot, repo, uid, now_local)


async def t1_background_loop(bot: Bot, repo: Repository, settings: Settings) -> None:
    while True:
        try:
            await t1_scheduler_tick(bot, repo, settings)
        except Exception:
            pass
        await asyncio.sleep(30)
