"""Т2: арт, темы жизни, выбор, итог месяца — кнопки, фото, таймер, ввод текста."""

from __future__ import annotations

import asyncio
import contextlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, ReplyKeyboardRemove
from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters, ContextTypes

from app.config import Settings
from app.db.repository import Repository
from app.services.schedule_loader import (
    course_calendar_day,
    get_tasks,
    get_t2_final_text,
    map_schedule_t2_type_to_subtype,
    parse_t2_start_clock,
    t2_art_pool,
    t2_assignment_for_calendar_day,
    t2_choice_sets_pool,
    t2_life_themes_pool,
    get_schedule,
)
from app.services.t1_runtime import local_window_to_utc_iso, resolve_tz_name
from app.services.t2_media import art_image_path, choice_image_path

repo: Repository = None  # set by register_t2_handlers
settings: Settings = None  # set by register_t2_handlers


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def continue_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Продолжить", callback_data="tasks:today")]]
    )


def _within_window_utc(window_end_iso: str) -> bool:
    return _now_utc_iso() <= window_end_iso


def _window_status(window_start_iso: str, window_end_iso: str) -> str:
    now = _now_utc_iso()
    if now < window_start_iso:
        return "not_started"
    if now > window_end_iso:
        return "ended"
    return "open"


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


t2_state = T2State()


async def _cancel_timer_safe(t: asyncio.Task | None) -> None:
    if t is None:
        return
    t.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await t


def _art_by_id(tasks: dict, aid: int) -> dict | None:
    for a in t2_art_pool(tasks):
        if int(a["id"]) == int(aid):
            return a
    return None


def _theme_by_id(tasks: dict, tid: int) -> dict | None:
    for t in t2_life_themes_pool(tasks):
        if int(t["id"]) == tid:
            return t
    return None


def _choice_set_by_id(tasks: dict, sid: int) -> dict | None:
    for s in t2_choice_sets_pool(tasks):
        if int(s["id"]) == sid:
            return s
    return None


def _art_caption(art: dict) -> str:
    if art.get("description"):
        return str(art["description"])
    if art.get("title"):
        auth = art.get("author", "")
        return f"{auth} — {art['title']}".strip(" —")
    return "образ"


def _skip_keyboard(row_id: int, show_skip: bool) -> list[list[InlineKeyboardButton]]:
    row = [InlineKeyboardButton(text="Пропустить", callback_data=f"t2:sk:{row_id}")]
    return [row] if show_skip else []


def _t2_show_skip(week_num: int, cal_day: int) -> bool:
    return week_num >= 2 or cal_day >= 8


async def _send_t2_open_art(
    bot: Bot,
    chat_id: int,
    row: dict,
    tasks: dict,
    show_skip: bool,
) -> None:
    art = _art_by_id(tasks, int(row["task_id"]))
    if not art:
        await bot.send_message(chat_id, "Задание Т2 (арт) не найдено в библиотеке.")
        return
    dm = int(art.get("duration_min") or 2)
    path = art_image_path(int(art["id"]))
    cap = "Изображение:"
    if path.is_file():
        await bot.send_photo(chat_id, path, caption=cap)
    else:
        await bot.send_message(
            chat_id,
            f"{cap}\n(файл не найден: {path.name})\n{_art_caption(art)}",
        )
    body = (
        f"Смотри на это изображение {dm} минут. Не оценивай, не анализируй. Просто смотри.\n"
        "Нажми «Начать», когда начнёшь, и «Готово» после завершения. "
        "Затем напиши 2–3 предложения и одно слово."
    )
    rid = row["id"]
    rows = [
        [
            InlineKeyboardButton(text="Начать", callback_data=f"t2:s:{rid}"),
            InlineKeyboardButton(text="Готово", callback_data=f"t2:d:{rid}"),
        ]
    ]
    rows.extend(_skip_keyboard(rid, show_skip))
    await bot.send_message(chat_id, body, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def _send_t2_open_life(bot: Bot, chat_id: int, row: dict, tasks: dict, show_skip: bool) -> None:
    th = _theme_by_id(tasks, int(row["task_id"]))
    if not th:
        await bot.send_message(chat_id, "Тема жизни не найдена.")
        return
    text = (
        f"Тема жизни: {th['title']}.\n{th['prompt']}\n\n"
        "Нажми «Готово», когда будешь готов написать размышления."
    )
    rid = row["id"]
    rows = [[InlineKeyboardButton(text="Готово", callback_data=f"t2:ld:{rid}")]]
    rows.extend(_skip_keyboard(rid, show_skip))
    await bot.send_message(chat_id, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def _send_t2_open_choice(bot: Bot, chat_id: int, row: dict, tasks: dict, show_skip: bool) -> None:
    cs = _choice_set_by_id(tasks, int(row["task_id"]))
    if not cs:
        await bot.send_message(chat_id, "Набор выбора не найден.")
        return
    ids = [int(x) for x in cs.get("image_ids", [])][:4]
    if not ids:
        await bot.send_message(chat_id, "В наборе выбора нет изображений.")
        return
    rid = row["id"]
    media: list[InputMediaPhoto] = []
    for i, img_id in enumerate(ids):
        p = choice_image_path(int(row["task_id"]), img_id)
        cap = f"Вариант {i + 1}"
        if p.is_file():
            media.append(InputMediaPhoto(media=str(p), caption=cap))
        else:
            art = _art_by_id(tasks, img_id)
            desc = _art_caption(art) if art else str(img_id)
            await bot.send_message(chat_id, f"{cap}: (нет файла) {desc}")
    if len(media) == 1:
        m0 = media[0]
        await bot.send_photo(chat_id, m0.media, caption=m0.caption)
    elif len(media) >= 2:
        await bot.send_media_group(chat_id, media=media)
    intro = "Выбери одно изображение, которое больше откликается."
    btns = [
        InlineKeyboardButton(text=f"Изображение {i + 1}", callback_data=f"t2:c:{rid}:{i}")
        for i in range(len(ids))
    ]
    rows = [btns[i : i + 2] for i in range(0, len(btns), 2)]
    rows.extend(_skip_keyboard(rid, show_skip))
    await bot.send_message(chat_id, intro, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def _send_t2_open_final(bot: Bot, chat_id: int, row: dict, tasks: dict, show_skip: bool) -> None:
    text = get_t2_final_text(tasks).replace("«Выполнил»", "«Готово»")
    rid = row["id"]
    rows = [[InlineKeyboardButton(text="Готово", callback_data=f"t2:fd:{rid}")]]
    rows.extend(_skip_keyboard(rid, show_skip))
    await bot.send_message(chat_id, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


def register_t2_handlers(app: Application, _repo: Repository, _settings: Settings, state: T2State) -> None:
    global repo, settings
    repo = _repo
    settings = _settings
    app.add_handler(CallbackQueryHandler(_t2_callback_o, pattern=re.compile(r"^t2:o:(\d+)$")))
    app.add_handler(CallbackQueryHandler(_t2_callback_sk, pattern=re.compile(r"^t2:sk:(\d+)$")))
    app.add_handler(CallbackQueryHandler(_t2_callback_s, pattern=re.compile(r"^t2:s:(\d+)$")))
    app.add_handler(CallbackQueryHandler(_t2_callback_d, pattern=re.compile(r"^t2:d:(\d+)$")))
    app.add_handler(CallbackQueryHandler(_t2_callback_ld, pattern=re.compile(r"^t2:ld:(\d+)$")))
    app.add_handler(CallbackQueryHandler(_t2_callback_fd, pattern=re.compile(r"^t2:fd:(\d+)$")))
    app.add_handler(CallbackQueryHandler(_t2_callback_c, pattern=re.compile(r"^t2:c:(\d+):(\d+)$")))


async def _t2_base(update: Update, row_id: int) -> tuple[int, dict | None]:
    query = update.callback_query
    uid = update.effective_user.id
    data = query.data or ""
    parts = data.split(":")
    if len(parts) < 3:
        await query.answer("Ошибка", show_alert=True)
        return uid, None
    try:
        row_id = int(parts[2])
    except ValueError:
        await query.answer("Ошибка", show_alert=True)
        return uid, None
    row = await repo.get_scheduled_task_by_id(row_id)
    if not row or row["user_id"] != uid or row["task_type"] != "T2":
        await query.answer("Задание не найдено", show_alert=True)
        return uid, None
    if row["completed"] or row["skipped"]:
        await query.answer("Уже обработано")
        return uid, None
    ws = str(row.get("window_start") or "")
    we = str(row.get("window_end") or "")
    status = _window_status(ws, we)
    if status == "not_started":
        await query.answer("Дождитесь временного окна", show_alert=True)
        return uid, None
    if status == "ended":
        await query.answer("Окно задания закончилось — задание удалено.", show_alert=True)
        return uid, None
    return uid, row


async def _t2_get_skip(row: dict, uid: int) -> bool:
    urow = await repo.get_user_row_for_t1(uid)
    week_num = int((urow or {}).get("week_num") or 1)
    try:
        td = date.fromisoformat(str(row["task_date"]))
        csd = (urow or {}).get("course_start_date")
        cal_d = course_calendar_day(date.fromisoformat(str(csd)), td) if csd else 1
    except ValueError:
        cal_d = 1
    return _t2_show_skip(week_num, cal_d)


async def _t2_callback_o(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    match = re.match(r"^t2:o:(\d+)$", query.data or "")
    if not match:
        return
    row_id = int(match.group(1))
    uid, row = await _t2_base(update, row_id)
    if row is None:
        return
    st = row.get("t2_subtype") or ""
    chat_id = query.message.chat.id
    tasks = get_tasks()
    show_skip = await _t2_get_skip(row, uid)
    if st == "art":
        await _send_t2_open_art(context.bot, chat_id, row, tasks, show_skip)
    elif st == "life_theme":
        await _send_t2_open_life(context.bot, chat_id, row, tasks, show_skip)
    elif st == "choice":
        await _send_t2_open_choice(context.bot, chat_id, row, tasks, show_skip)
    elif st == "final":
        await _send_t2_open_final(context.bot, chat_id, row, tasks, show_skip)
    else:
        await query.answer("Неизвестный тип", show_alert=True)
        return
    await query.answer()


async def _t2_callback_sk(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    match = re.match(r"^t2:sk:(\d+)$", query.data or "")
    if not match:
        return
    row_id = int(match.group(1))
    uid, row = await _t2_base(update, row_id)
    if row is None:
        return
    show_skip = await _t2_get_skip(row, uid)
    if not show_skip:
        await query.answer("Пропуск со 2-й недели курса", show_alert=True)
        return
    await _cancel_timer_safe(t2_state.art_timers.pop(uid, None))
    t2_state.art_session.pop(uid, None)
    t2_state.pending.pop(uid, None)
    await repo.skip_scheduled_task(row_id, _now_utc_iso())
    await query.message.reply_text("Задание пропущено.", reply_markup=continue_keyboard())
    await query.answer()


async def _t2_callback_s(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    match = re.match(r"^t2:s:(\d+)$", query.data or "")
    if not match:
        return
    row_id = int(match.group(1))
    uid, row = await _t2_base(update, row_id)
    if row is None:
        return
    if row.get("t2_subtype") != "art":
        await query.answer("Ошибка", show_alert=True)
        return
    ws = str(row.get("window_start") or "")
    we = str(row.get("window_end") or "")
    status = _window_status(ws, we)
    if status == "not_started":
        await query.answer("Дождитесь временного окна", show_alert=True)
        return
    if status == "ended":
        await query.answer("Окно задания закончилось — задание удалено.", show_alert=True)
        return
    tasks = get_tasks()
    art = _art_by_id(tasks, int(row["task_id"]))
    dm = int((art or {}).get("duration_min") or 2)
    await _cancel_timer_safe(t2_state.art_timers.pop(uid, None))
    t2_state.art_session[uid] = (row_id, datetime.now(timezone.utc))
    mm, ss = divmod(dm * 60, 60)
    await query.message.reply_text(f"<i>⏱ Таймер: {mm:02d}:{ss:02d}</i>", parse_mode="HTML")
    asyncio.create_task(_t2_art_timer_countdown(context.bot, uid, query.message.chat.id, dm * 60))
    await query.answer()


async def _t2_art_timer_countdown(bot: Bot, uid: int, chat_id: int, target: int) -> None:
    await asyncio.sleep(target)
    if uid in t2_state.art_session:
        try:
            await bot.send_message(chat_id, "⏱ Время вышло! Нажми «Готово» и напиши свои впечатления.")
        except Exception:
            pass


async def _t2_callback_d(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    match = re.match(r"^t2:d:(\d+)$", query.data or "")
    if not match:
        return
    row_id = int(match.group(1))
    uid, row = await _t2_base(update, row_id)
    if row is None:
        return
    if row.get("t2_subtype") != "art":
        await query.answer("Ошибка", show_alert=True)
        return
    ctx = t2_state.art_session.pop(uid, None)
    await _cancel_timer_safe(t2_state.art_timers.pop(uid, None))
    if not ctx or ctx[0] != row_id:
        await query.answer("Сначала нажми «Начать»", show_alert=True)
        return
    actual = int((datetime.now(timezone.utc) - ctx[1]).total_seconds())
    if actual < 0:
        actual = 0
    t2_state.pending[uid] = T2Pending(row_id=row_id, step="art_sents", art_seconds=actual)
    await query.message.reply_text("Напиши 2–3 предложения о том, что чувствуешь, что заметил.", reply_markup=ReplyKeyboardRemove())
    await query.answer()


async def _t2_callback_ld(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    match = re.match(r"^t2:ld:(\d+)$", query.data or "")
    if not match:
        return
    row_id = int(match.group(1))
    uid, row = await _t2_base(update, row_id)
    if row is None:
        return
    if row.get("t2_subtype") != "life_theme":
        await query.answer("Ошибка", show_alert=True)
        return
    t2_state.pending[uid] = T2Pending(row_id=row_id, step="life_text")
    await query.message.reply_text("Напиши свои размышления.", reply_markup=ReplyKeyboardRemove())
    await query.answer()


async def _t2_callback_fd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    match = re.match(r"^t2:fd:(\d+)$", query.data or "")
    if not match:
        return
    row_id = int(match.group(1))
    uid, row = await _t2_base(update, row_id)
    if row is None:
        return
    if row.get("t2_subtype") != "final":
        await query.answer("Ошибка", show_alert=True)
        return
    t2_state.pending[uid] = T2Pending(row_id=row_id, step="final_word")
    await query.message.reply_text("Напиши одно слово.", reply_markup=ReplyKeyboardRemove())
    await query.answer()


async def _t2_callback_c(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    match = re.match(r"^t2:c:(\d+):(\d+)$", query.data or "")
    if not match:
        return
    row_id = int(match.group(1))
    idx = int(match.group(2))
    uid, row = await _t2_base(update, row_id)
    if row is None:
        return
    if row.get("t2_subtype") != "choice":
        await query.answer("Ошибка", show_alert=True)
        return
    ws = str(row.get("window_start") or "")
    we = str(row.get("window_end") or "")
    status = _window_status(ws, we)
    if status == "not_started":
        await query.answer("Дождитесь временного окна", show_alert=True)
        return
    if status == "ended":
        await query.answer("Окно задания закончилось — задание удалено.", show_alert=True)
        return
    tasks = get_tasks()
    cs = _choice_set_by_id(tasks, int(row["task_id"]))
    ids = [int(x) for x in (cs or {}).get("image_ids", [])][:4]
    if idx < 0 or idx >= len(ids):
        await query.answer("Неверный выбор", show_alert=True)
        return
    sel_id = ids[idx]
    t2_state.pending[uid] = T2Pending(row_id=row_id, step="choice_word", selected_image_id=sel_id)
    await query.message.reply_text("Напиши слово — почему выбрал это изображение (или что откликнулось).", reply_markup=ReplyKeyboardRemove())
    await query.answer()


async def _t2_text_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    message = update.message
    pend = t2_state.pending.get(uid)
    if not pend:
        return
    raw = (message.text or "").strip()
    if raw.startswith("/"):
        await message.reply_text("Заверши шаг или используй /stop.")
        return
    row = await repo.get_scheduled_task_by_id(pend.row_id)
    if not row or row["user_id"] != uid or row["completed"]:
        t2_state.pending.pop(uid, None)
        return
    urow = await repo.get_user_row_for_t1(uid)
    ag = (urow or {}).get("age_group")
    lib_task_id = str(int(row["task_id"])) if row.get("task_id") is not None else "0"
    now_s = datetime.now().isoformat(sep=" ", timespec="seconds")

    if pend.step == "art_sents":
        if len(raw) < 15:
            await message.reply_text("Напиши чуть развёрнутее (хотя бы пара предложений).")
            return
        pend.response_draft = raw
        pend.step = "art_word"
        await message.reply_text("Напиши одно слово — итог.")
        return

    if pend.step == "art_word":
        if not pend.response_draft or len(raw) > 50 or " " in raw.strip():
            await message.reply_text("Одно слово, до 50 символов.")
            return
        actual = pend.art_seconds or 0
        await repo.complete_t2_task(
            pend.row_id, word=raw, response=pend.response_draft,
            completed_at=now_s, actual_duration_sec=actual, selected_image_id=None,
        )
        await repo.save_user_word(uid, raw, "T2", lib_task_id, ag)
        t2_state.pending.pop(uid, None)
        await message.reply_text("Ваши данные внесены", reply_markup=continue_keyboard())
        return

    if pend.step == "life_text":
        if len(raw) < 20:
            await message.reply_text("Напиши несколько предложений (3–5).")
            return
        pend.response_draft = raw
        pend.step = "life_word"
        await message.reply_text("Напиши одно слово — чувство или итог.")
        return

    if pend.step == "life_word":
        if not pend.response_draft or len(raw) > 50 or " " in raw.strip():
            await message.reply_text("Одно слово, до 50 символов.")
            return
        await repo.complete_t2_task(
            pend.row_id, word=raw, response=pend.response_draft, completed_at=now_s,
        )
        await repo.save_user_word(uid, raw, "T2", lib_task_id, ag)
        t2_state.pending.pop(uid, None)
        await message.reply_text("Ваши данные внесены", reply_markup=continue_keyboard())
        return

    if pend.step == "choice_word":
        if len(raw) > 80:
            await message.reply_text("Короче, одно слово или короткая фраза.")
            return
        await repo.complete_t2_task(
            pend.row_id, word=raw, response=None,
            completed_at=now_s, selected_image_id=pend.selected_image_id,
        )
        await repo.save_user_word(uid, raw, "T2", lib_task_id, ag)
        t2_state.pending.pop(uid, None)
        await message.reply_text("Ваши данные внесены", reply_markup=continue_keyboard())
        return

    if pend.step == "final_word":
        if len(raw) > 50 or " " in raw.strip():
            await message.reply_text("Одно слово.")
            return
        await repo.complete_t2_task(pend.row_id, word=raw, response=None, completed_at=now_s)
        await repo.save_user_word(uid, raw, "T2", lib_task_id, ag)
        t2_state.pending.pop(uid, None)
        await message.reply_text("Ваши данные внесены", reply_markup=continue_keyboard())
        return


async def deliver_t2_slot(
    bot: Bot,
    repo: Repository,
    settings: Settings,
    uid: int,
    tz: str,
    age_group: str,
    today: date,
) -> None:
    now_iso = _now_utc_iso()
    urow = await repo.get_user_row_for_t1(uid)
    if not urow:
        return
    cs = urow.get("course_start_date")
    if not cs:
        return
    try:
        course_start = date.fromisoformat(cs)
    except ValueError:
        return
    cal_day = course_calendar_day(course_start, today)
    sched = get_schedule(age_group)
    assign = t2_assignment_for_calendar_day(sched, cal_day)
    if not assign:
        return
    subtype = map_schedule_t2_type_to_subtype(str(assign["type"]))
    tid = assign.get("id")
    task_id_val = int(tid) if tid is not None else 0

    week_num = int(urow.get("week_num") or 1)
    sets = await repo.get_user_settings(uid)
    th, tm = parse_t2_start_clock(sets.get("t2_time"), week_num, cal_day)
    tz_name = resolve_tz_name(tz, settings.timezone)
    ws, we = local_window_to_utc_iso(today, time(th, tm), time(21, 0), tz_name)

    task_date = today.isoformat()
    await repo.upsert_t2_daily_task(uid, task_date, ws, we, task_id_val, subtype)
    meta = await repo.get_t2_row_meta(uid, task_date)
    if not meta or meta["completed"] or meta["skipped"]:
        return
    now_local = datetime.now(ZoneInfo(tz_name))
    if now_local.date() != today:
        return
    tloc = now_local.time()
    if tloc >= time(21, 0):
        if not meta["completed"]:
            await repo.skip_scheduled_task(meta["id"], now_iso)
        return
    start_t = time(th, tm)
    if tloc < start_t:
        return
    if meta["sent_at"]:
        return
    row_id = meta["id"]
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Т2", callback_data=f"t2:o:{row_id}")]
        ]
    )
    try:
        await bot.send_message(
            uid,
            "Сегодня есть задание Т2. Нажми кнопку ниже, чтобы открыть его.\nПосле 21:00 задание удаляется.",
            reply_markup=kb,
        )
        await repo.mark_scheduled_sent(row_id, now_iso)
    except Exception:
        pass
