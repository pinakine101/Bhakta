"""Т1 утро/вечер: рассылка в окнах, inline-кнопки, таймер вечера, слово."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters, ContextTypes

from app.config import Settings
from app.db.repository import Repository
from app.t2_bot import deliver_t2_slot

repo: Repository = None  # set by register_t1_handlers
settings: Settings = None  # set by register_t1_handlers
from app.services.t1_runtime import (
    T1_EVENING_BODY_TEMPLATE,
    T1_EVENING_TITLE,
    T1_MORNING_BODY,
    T1_MORNING_TITLE,
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
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Продолжить", callback_data="tasks:today")]]
    )


_T1_IMAGE_PATH = Path(__file__).resolve().parent.parent / "images" / "Cont_1.jpg"


async def _send_t1_image_if_exists(bot: Bot, chat_id: int) -> None:
    if _T1_IMAGE_PATH.is_file():
        with contextlib.suppress(Exception):
            await bot.send_photo(chat_id, _T1_IMAGE_PATH)


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _within_window_utc(window_end_iso: str) -> bool:
    return _now_utc_iso() <= window_end_iso


def _within_window_utc_range(window_start_iso: str, window_end_iso: str) -> bool:
    now_iso = _now_utc_iso()
    return window_start_iso <= now_iso <= window_end_iso


def _t4_daily_start_time(uid: int, today: date) -> time:
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


async def _t1_message_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if uid in t1_state.pending_evening_word:
        await _t1_evening_word_message(update, context)
    elif uid in t1_state.pending_morning_word:
        await _t1_morning_word_message(update, context)


def register_t1_handlers(app: Application, _repo: Repository, _settings: Settings, state: T1State) -> None:
    global repo, settings
    repo = _repo
    settings = _settings
    app.add_handler(CallbackQueryHandler(_t1_callback_t1, pattern=re.compile(r"^t1:mo:(\d+)$")))
    app.add_handler(CallbackQueryHandler(_t1_callback_ms, pattern=re.compile(r"^t1:ms:(\d+)$")))
    app.add_handler(CallbackQueryHandler(_t1_callback_md, pattern=re.compile(r"^t1:md:(\d+)$")))
    app.add_handler(CallbackQueryHandler(_t1_callback_eo, pattern=re.compile(r"^t1:eo:(\d+)$")))
    app.add_handler(CallbackQueryHandler(_t1_callback_es, pattern=re.compile(r"^t1:es:(\d+)$")))
    app.add_handler(CallbackQueryHandler(_t1_callback_ed, pattern=re.compile(r"^t1:ed:(\d+)$")))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _t1_message_router))


async def _t1_callback_base(update: Update, row_id: int) -> dict | None:
    """Fetch task row and validate. Returns row dict or None."""
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    row = await repo.get_scheduled_task_by_id(row_id)
    if not row or row["user_id"] != uid:
        await query.message.reply_text("Задание не найдено", reply_markup=ReplyKeyboardRemove())
        return None
    if row["completed"] or row["skipped"]:
        await query.answer("Уже обработано")
        return None
    ws = str(row.get("window_start") or "")
    we = str(row.get("window_end") or "")
    now_iso = _now_utc_iso()
    if now_iso < ws:
        await query.answer("Дождитесь временного окна", show_alert=True)
        return None
    if now_iso > we:
        await query.answer("Окно задания закончилось — задание удалено.", show_alert=True)
        return None
    return row


def _t1_mk_filter(state_key: int):
    class DynamicFilter:
        def __init__(self, key: int):
            self.key = key
        def check(self, update: Update) -> bool:
            if not update.message or not update.message.from_user:
                return False
            uid = update.message.from_user.id
            return uid in _t1_pending_map.get(self.key, {})
    return DynamicFilter(state_key)


_t1_pending_map: dict[int, dict[int, int | dict]] = {}


async def _t1_callback_t1(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    match = re.match(r"^t1:mo:(\d+)$", query.data or "")
    if not match:
        return
    row_id = int(match.group(1))
    row = await _t1_callback_base(update, row_id)
    if not row:
        return
    if row["task_type"] != "T1_morning":
        await query.answer("Ошибка", show_alert=True)
        return
    await _send_t1_image_if_exists(context.bot, query.message.chat.id)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Начать", callback_data=f"t1:ms:{row_id}"),
                InlineKeyboardButton(text="Готово", callback_data=f"t1:md:{row_id}"),
            ]
        ]
    )
    await query.message.reply_text(T1_MORNING_BODY, reply_markup=kb)
    await query.answer()


async def _t1_callback_ms(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    match = re.match(r"^t1:ms:(\d+)$", query.data or "")
    if not match:
        return
    row_id = int(match.group(1))
    row = await _t1_callback_base(update, row_id)
    if not row:
        return
    if row["task_type"] != "T1_morning":
        await query.answer("Ошибка", show_alert=True)
        return
    uid = update.effective_user.id
    pr = await repo.get_t1_progress(uid)
    target = int(pr["morning_seconds"]) if pr else 20
    t1_state.morning_start[uid] = (row_id, datetime.now(timezone.utc), target)
    mm, ss = divmod(target, 60)
    await query.message.reply_text(f"<i>⏱ Таймер: {mm:02d}:{ss:02d}</i>", parse_mode="HTML")
    await query.answer("Время пошло")


async def _t1_callback_md(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    match = re.match(r"^t1:md:(\d+)$", query.data or "")
    if not match:
        return
    row_id = int(match.group(1))
    row = await _t1_callback_base(update, row_id)
    if not row:
        return
    if row["task_type"] != "T1_morning":
        await query.answer("Ошибка", show_alert=True)
        return
    uid = update.effective_user.id
    ctx = t1_state.morning_start.pop(uid, None)
    if not ctx:
        await query.answer("Сначала нажми «Начать»", show_alert=True)
        return
    rid, started, target = ctx
    if rid != row_id:
        await query.answer("Другое задание", show_alert=True)
        return
    actual = int((datetime.now(timezone.utc) - started).total_seconds())
    if actual < 0:
        actual = 0
    reached = actual >= target
    t1_state.pending_morning_word[uid] = {
        "row_id": row_id,
        "actual": actual,
        "target": target,
        "task_date": row["task_date"],
        "reached": reached,
    }
    await query.message.reply_text("Напишите одно слово.", reply_markup=ReplyKeyboardRemove())
    await query.answer()


async def _t1_callback_eo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    match = re.match(r"^t1:eo:(\d+)$", query.data or "")
    if not match:
        return
    row_id = int(match.group(1))
    row = await _t1_callback_base(update, row_id)
    if not row:
        return
    if row["task_type"] != "T1_evening":
        await query.answer("Ошибка", show_alert=True)
        return
    await _send_t1_image_if_exists(context.bot, query.message.chat.id)
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
    await query.message.reply_text(body, reply_markup=kb)
    await query.answer()


async def _t1_callback_es(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    match = re.match(r"^t1:es:(\d+)$", query.data or "")
    if not match:
        return
    row_id = int(match.group(1))
    row = await _t1_callback_base(update, row_id)
    if not row:
        return
    if row["task_type"] != "T1_evening":
        await query.answer("Ошибка", show_alert=True)
        return
    uid = update.effective_user.id
    t1_state.evening_phase[uid] = row_id
    await query.answer()


async def _t1_callback_ed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    match = re.match(r"^t1:ed:(\d+)$", query.data or "")
    if not match:
        return
    row_id = int(match.group(1))
    row = await _t1_callback_base(update, row_id)
    if not row:
        return
    if row["task_type"] != "T1_evening":
        await query.answer("Ошибка", show_alert=True)
        return
    uid = update.effective_user.id
    if t1_state.evening_phase.get(uid) != row_id:
        await query.answer("Сначала нажми «Начать»", show_alert=True)
        return
    await _cancel_timer_safe(t1_state.evening_timers.pop(uid, None))
    t1_state.evening_phase.pop(uid, None)
    t1_state.pending_evening_word[uid] = row_id
    await query.message.reply_text("Напишите одно слово.", reply_markup=ReplyKeyboardRemove())
    await query.answer()


async def _t1_evening_word_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    row_id = t1_state.pending_evening_word.pop(uid, None)
    if row_id is None:
        return
    message = update.message
    word = (message.text or "").strip()
    if not word or len(word) > 50 or word.startswith("/"):
        t1_state.pending_evening_word[uid] = row_id
        await message.reply_text("Напиши одно слово (1–50 символов).")
        return
    row = await repo.get_scheduled_task_by_id(row_id)
    if not row or row["user_id"] != uid:
        await message.reply_text("Задание не найдено.")
        return
    urow = await repo.get_user_row_for_t1(uid)
    ag = (urow or {}).get("age_group")
    now_s = datetime.now().isoformat(sep=" ", timespec="seconds")
    await repo.complete_t1_evening_task(row_id, word, now_s)
    await repo.save_user_word(
        user_id=uid, word=word, task_type="T1_evening", task_id=str(row_id), age_group=ag
    )
    await message.reply_text("Ваши данные внесены", reply_markup=continue_keyboard())


async def _t1_morning_word_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    ctx = t1_state.pending_morning_word.pop(uid, None)
    if not ctx:
        return
    message = update.message
    word = (message.text or "").strip()
    if not word or len(word) > 50 or word.startswith("/"):
        t1_state.pending_morning_word[uid] = ctx
        await message.reply_text("Напиши одно слово (1–50 символов).")
        return
    row_id = int(ctx["row_id"])
    row = await repo.get_scheduled_task_by_id(row_id)
    if not row or row["user_id"] != uid:
        await message.reply_text("Задание не найдено.")
        return
    urow = await repo.get_user_row_for_t1(uid)
    ag = (urow or {}).get("age_group")
    now_s = datetime.now().isoformat(sep=" ", timespec="seconds")
    await repo.complete_t1_morning_task(
        row_id, int(ctx.get("actual") or 0), int(ctx.get("target") or 0), now_s, word=word
    )
    await repo.update_t1_morning_after_session(uid, str(ctx.get("task_date") or row["task_date"]), bool(ctx.get("reached")))
    await repo.save_user_word(
        user_id=uid, word=word, task_type="T1_morning", task_id=str(row_id), age_group=ag
    )
    await message.reply_text("Ваши данные внесены", reply_markup=continue_keyboard())


t1_state = T1State()


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
    await repo.upsert_t1_daily_task(uid, "T1_evening", task_date, ws, we, 0, rhythm, cycles)
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

    t1_ws, t1_we = local_window_to_utc_iso(today, time(6, 0), time(9, 0), tz_name)
    await repo.upsert_t1_daily_task(uid, "T1_morning", today_s, t1_ws, t1_we, 0)

    t1e_ws, t1e_we = local_window_to_utc_iso(today, time(21, 0), time(23, 59), tz_name)
    await repo.upsert_t1_daily_task(uid, "T1_evening", today_s, t1e_ws, t1e_we, 0, rhythm, cycles)

    schedule = get_schedule(age_group)
    cal_day = course_calendar_day(course_start, today)

    assign = t2_assignment_for_calendar_day(schedule, cal_day)
    if assign:
        subtype = map_schedule_t2_type_to_subtype(str(assign["type"]))
        tid = assign.get("id")
        task_id_val = int(tid) if tid is not None else 0
        t2_ws, t2_we = local_window_to_utc_iso(today, time(12, 0), time(21, 0), tz_name)
        await repo.upsert_t2_daily_task(uid, today_s, t2_ws, t2_we, task_id_val, subtype)

    t3_assign = next((x for x in schedule.get("t3_assignments", []) if int(x.get("day") or 0) == cal_day), None)
    if t3_assign:
        t3_id = int(t3_assign.get("id") or 0)
        t3_ws, t3_we = local_window_to_utc_iso(today, time(15, 0), time(21, 0), tz_name)
        await repo.upsert_scheduled_task(uid, "T3", today_s, "", t3_id, t3_ws, t3_we, None)

    t4_list = schedule.get("t4_assignments", [])
    t4_id = int(t4_list[cal_day - 1]) if 0 < cal_day <= len(t4_list) else 0
    if t4_id:
        t4_start_local = datetime.combine(today, _t4_daily_start_time(uid, today))
        t4_end_local = t4_start_local + timedelta(hours=1)
        t4_ws, t4_we = local_window_to_utc_iso(today, t4_start_local.time(), t4_end_local.time(), tz_name)
        await repo.upsert_scheduled_task(uid, "T4", today_s, "", t4_id, t4_ws, t4_we, None)

    rows = await repo.list_scheduled_for_date(uid, today_s)
    buttons: list[list[InlineKeyboardButton]] = []
    mark_rows: list[int] = []

    for r in rows:
        t = str(r["task_type"])
        rid = int(r["id"])
        ws = str(r.get("window_start") or "")
        we = str(r.get("window_end") or "")
        if not include_already_sent and r.get("sent_at"):
            continue
        if not _within_window_utc_range(ws, we):
            continue
        if t == "T1_morning":
            buttons.append([InlineKeyboardButton(text="Т1 утро", callback_data=f"t1:mo:{rid}")])
            mark_rows.append(rid)
        elif t == "T1_evening":
            buttons.append([InlineKeyboardButton(text="Т1 вечер", callback_data=f"t1:eo:{rid}")])
            mark_rows.append(rid)
        elif t == "T2":
            buttons.append([InlineKeyboardButton(text="Т2", callback_data=f"t2:o:{rid}")])
            mark_rows.append(rid)
        elif t == "T3":
            buttons.append([InlineKeyboardButton(text="Т3", callback_data=f"t3:o:{rid}")])
            mark_rows.append(rid)
        elif t == "T4":
            buttons.append([InlineKeyboardButton(text="Т4", callback_data=f"t4:o:{rid}")])
            mark_rows.append(rid)

    if buttons:
        await bot.send_message(uid, "Активные задания:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        for row_id in mark_rows:
            await repo.mark_scheduled_sent(row_id, now_iso)


def _format_window_local(win_start: str, win_end: str, tz_name: str) -> str:
    try:
        ws = datetime.fromisoformat(win_start.replace("Z", "+00:00")).astimezone(ZoneInfo(tz_name))
        we = datetime.fromisoformat(win_end.replace("Z", "+00:00")).astimezone(ZoneInfo(tz_name))
        return f"{ws.strftime('%H:%M')}–{we.strftime('%H:%M')}"
    except Exception:
        return "спонтанно"


def _last_day_of_month(d: date) -> date:
    if d.month == 12:
        return date(d.year, 12, 31)
    return date(d.year, d.month + 1, 1) - timedelta(days=1)


async def send_daily_plan_now(bot: Bot, repo: Repository, settings: Settings, uid: int) -> None:
    u = await repo.get_user_row_for_t1(uid)
    if not u or not u.get("age_group"):
        return
    tz = resolve_tz_name(u.get("timezone"), settings.timezone)
    today = datetime.now(ZoneInfo(tz)).date()
    await _send_daily_tasks_digest(bot, repo, settings, uid, tz, u["age_group"], today, include_already_sent=True)


async def _maybe_send_weekly_report(
    bot: Bot,
    repo: Repository,
    uid: int,
    now_local: datetime,
) -> None:
    profile = await repo.get_analysis_profile(uid)
    if not profile.get("reports_enabled", True):
        return
    if now_local.weekday() != 6 or now_local.time() < time(21, 0):
        return
    week_start = now_local.date() - timedelta(days=now_local.weekday())
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
            "Месяц завершён: первая ступень ПРАСАДА пройдена.\nГотовность к ступени МАХЕША подтверждена.",
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
