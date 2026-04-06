from __future__ import annotations

import asyncio
import contextlib
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from zoneinfo import ZoneInfo

from aiohttp import web
from dotenv import load_dotenv
from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from app.config import Settings, get_settings
from app.db.database import init_db
from app.db.repository import Repository
from app.services.schedule_loader import (
    course_calendar_day,
    get_tasks,
    get_t3_text,
    get_t4_challenge_text,
)
from app.services.word_analysis import build_default_word_dict_rows
from app.services.zodiac import get_age_group, get_zodiac_and_element
from app.t1_bot import register_t1_handlers, t1_background_loop
from app.t1_bot import _create_daily_tasks
from app.t1_bot import _t1_morning_word_message, _t1_evening_word_message
from app.t2_bot import register_t2_handlers
from app.t2_bot import _t2_text_flow

from app.states import t1_state, t2_state, T1State, T2State

_t1_state = t1_state
_t2_state = t2_state


def _is_valid_birth_date(raw_value: str) -> bool:
    from datetime import datetime
    try:
        datetime.strptime(raw_value, "%d.%m.%Y")
        return True
    except ValueError:
        return False

load_dotenv()
DB_PATH = os.path.join(os.getcwd(), "cont_bot.sqlite3")
TASKS_TODAY = "tasks:today"

settings = get_settings()
repo = None
application: Application | None = None
background_task: asyncio.Task | None = None
application_initialized: bool = False


def resolve_tz_name(tz_name: str | None, fallback: str) -> str:
    name = (tz_name or fallback).strip()
    try:
        ZoneInfo(name)
        return name
    except Exception:
        return fallback


HOW_IT_WORKS_TEXT = """Бот помогает выстроить ежедневную практику созерцания. Задания приходят автоматически в нужное время — остаётся нажимать кнопки и иногда писать слово.

 ТИПЫ ЗАДАНИЙ

 Т1 — ежедневно: утром — концентрация на точке, вечером — дыхание в ритме.
 Т2 — желательные: искусство, темы жизни, выборы, итог месяца.
 Т3 — рекомендации: статьи, навыки, творчество, прогулки.
 Т4 — вызовы: короткие телесные упражнения в случайное время.

 КАК ВЫПОЛНЯТЬ
 Бот присылает кнопку с названием задания.
 Нажимаете — появляется описание и кнопка «Готово».
 Делаете практику, нажимаете «Готово».
 Бот отвечает: «Ваши данные внесены». Если задание требует слово — запросит после.

 БАЛЫ И ОТЧЕТ
 За каждое выполненное задание начисляются баллы.
 Раз в неделю бот присылает отчёт: сколько баллов набрано, какой процент от недельного максимума.
 Рейтингов нет — это ваша личная статистика.

 ВАЖНО
 Задания живут ограниченное время. Не успели — пропадает, но вы всегда можете вернуться к следующему.
 Бот не учитель и не наставник. Он просто напоминает и фиксирует. Остальное — тишина и вы."""


def continue_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(text="Продолжить", callback_data=TASKS_TODAY)
    ]])


def _task_title(task_type: str, t2_subtype: str | None = None) -> str:
    del t2_subtype
    normalized = str(task_type or "").strip()
    if normalized in {"T1_morning", "T1_evening"} or normalized.startswith("T1"):
        return "Т1"
    if normalized == "T2" or normalized.startswith("T2"):
        return "Т2"
    if normalized == "T3" or normalized.startswith("T3"):
        return "Т3"
    if normalized == "T4" or normalized.startswith("T4"):
        return "Т4"
    return normalized


def _task_open_callback(task_type: str, row_id: int) -> str | None:
    normalized = str(task_type or "").strip()
    if normalized == "T1_morning":
        return f"t1:mo:{row_id}"
    if normalized == "T1_evening":
        return f"t1:eo:{row_id}"
    if normalized == "T2" or normalized.startswith("T2"):
        return f"t2:o:{row_id}"
    if normalized == "T3" or normalized.startswith("T3"):
        return f"t3:o:{row_id}"
    if normalized == "T4" or normalized.startswith("T4"):
        return f"t4:o:{row_id}"
    return None


async def task_info_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    match = re.match(r"^task:info:(\d+)$", query.data or "")
    if not match:
        return
    row_id = int(match.group(1))
    row = await repo.get_scheduled_task_by_id(row_id)
    if not row:
        await query.message.reply_text("Задание не найдено.")
        return
    task_type = str(row["task_type"])
    ws = str(row.get("window_start") or "")
    we = str(row.get("window_end") or "")
    urow = await repo.get_user_row_for_t1(query.from_user.id)
    tz = resolve_tz_name(urow.get("timezone") if urow else None, settings.timezone)
    try:
        ws_local = datetime.fromisoformat(ws.replace("Z", "+00:00")).astimezone(ZoneInfo(tz))
        we_local = datetime.fromisoformat(we.replace("Z", "+00:00")).astimezone(ZoneInfo(tz))
        duration = we_local - ws_local
        hours = int(duration.total_seconds() // 3600)
        minutes = int((duration.total_seconds() % 3600) // 60)
        dur_str = f"{hours}ч {minutes}м" if hours > 0 else f"{minutes}м"
        time_info = f"⏰ {ws_local.strftime('%H:%M')}–{we_local.strftime('%H:%M')} ({dur_str})"
    except Exception:
        time_info = "⏰ Спонтанно"
    title = _task_title(task_type, row.get("t2_subtype"))
    done = row.get("completed")
    status = "✅ Выполнено" if done else "📋 Доступно"
    alert_text = f"{title}\n{status}\n{time_info}"
    cb = _task_open_callback(task_type, row_id)
    if cb and not done:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=f"▶ Открыть", callback_data=cb)]]
        )
        await query.edit_message_text(f"{title}\n{time_info}", reply_markup=kb)
    else:
        await query.answer(alert_text, show_alert=True)


async def send_today_tasks_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    urow = await repo.get_user_row_for_t1(uid)
    if not urow or not urow.get("age_group"):
        await update.effective_message.reply_text("Сначала заполни профиль — /start")
        return
    tz = resolve_tz_name(urow.get("timezone"), settings.timezone)
    today = datetime.now(ZoneInfo(tz)).date()
    today_s = today.isoformat()
    try:
        await _create_daily_tasks(
            application.bot, repo, settings, uid,
            tz, urow["age_group"], today,
        )
    except Exception:
        pass
    rows = await repo.list_scheduled_for_date(uid, today_s)
    kb_rows: list[list[InlineKeyboardButton]] = []
    for r in rows:
        done = bool(r.get("completed"))
        skipped = bool(r.get("skipped"))
        if done or skipped:
            continue
        if str(r["task_type"]) == "T4":
            continue
        title = _task_title(str(r["task_type"]), r.get("t2_subtype"))
        cb = f"task:info:{int(r['id'])}"
        kb_rows.append([InlineKeyboardButton(text=title, callback_data=cb)])

    if not kb_rows:
        await update.effective_message.reply_text("На сегодня заданий нет.")
    else:
        await update.effective_message.reply_text(
            "Задания на сегодня:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
        )


class OnboardingStep(str, Enum):
    WAIT_NAME = "wait_name"
    WAIT_BIRTH_DATE = "wait_birth_date"


@dataclass
class OnboardingState:
    step: OnboardingStep
    full_name: str | None = None
    birth_date: str | None = None
    detected_timezone: str = "Europe/Moscow"


onboarding_state: dict[int, OnboardingState] = {}

_LANGUAGE_TZ_MAP = {
    "ru": "Europe/Moscow",
    "uk": "Europe/Kyiv",
    "be": "Europe/Minsk",
    "kk": "Asia/Almaty",
    "uz": "Asia/Tashkent",
    "ky": "Asia/Bishkek",
    "tg": "Asia/Dushanbe",
    "az": "Asia/Baku",
    "hy": "Asia/Yerevan",
    "ka": "Asia/Tbilisi",
    "en": "Europe/London",
    "de": "Europe/Berlin",
    "fr": "Europe/Paris",
    "es": "Europe/Madrid",
    "it": "Europe/Rome",
    "pt": "Europe/Lisbon",
    "tr": "Europe/Istanbul",
    "zh": "Asia/Shanghai",
    "ja": "Asia/Tokyo",
    "ko": "Asia/Seoul",
    "ar": "Asia/Riyadh",
    "hi": "Asia/Kolkata",
}


def _tz_from_language_code(lang_code: str | None, fallback: str) -> str:
    if not lang_code:
        return fallback
    code = lang_code.split("-")[0].lower()
    return _LANGUAGE_TZ_MAP.get(code, fallback)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    await repo.ensure_user(user_id, update.effective_user.username or "anonymous")
    await repo.ensure_analysis_profile(user_id)
    profile = await repo.get_user_profile(user_id)
    if profile and profile.get("full_name") and profile.get("birth_date"):
        await update.effective_message.reply_text(
            "С возвращением!",
            reply_markup=continue_keyboard(),
        )
        return

    lang_code = update.effective_user.language_code
    detected_tz = _tz_from_language_code(lang_code, settings.timezone)
    onboarding_state[user_id] = OnboardingState(step=OnboardingStep.WAIT_NAME, detected_timezone=detected_tz)
    tz_label = detected_tz.replace("_", " ").split("/")[-1]
    await update.effective_message.reply_text(
        f"Определён часовой пояс: {detected_tz} ({tz_label}).\n\n"
        "Сначала заполним профиль.\nВведите ваше имя:",
        reply_markup=ReplyKeyboardRemove(),
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Команды:\n"
        "/start — регистрация и профиль\n"
        "/stop — сбросить незавершённый ввод\n"
        "/reset — удалить все твои данные и активность в боте (безвозвратно)\n"
        "/resettmp — (временно) сбросить курс/задания для теста, профиль сохранится\n"
        "/sett2 ЧЧ:ММ — время напоминания Т2 (с 3-й недели курса)\n\n"
        "После /start: имя, дата рождения, город.",
        reply_markup=ReplyKeyboardRemove(),
    )


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    onboarding_state.pop(uid, None)
    t1_state.clear_user(uid)
    t2_state.clear_user(uid)
    await update.effective_message.reply_text(
        "В Telegram нет кнопки «выйти из бота»: это обычный чат.\n"
        "Можно просто перестать писать, отключить уведомления (⋮ в чате → Уведомления) "
        "или удалить чат с ботом.\n\n"
        "Незавершённый ввод анкеты сброшен. Снова с нуля — /start.",
        reply_markup=ReplyKeyboardRemove(),
    )


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    onboarding_state.pop(uid, None)
    t1_state.clear_user(uid)
    t2_state.clear_user(uid)
    await repo.wipe_all_user_data(uid)
    await update.effective_message.reply_text(
        "Все твои данные и активность в боте удалены из базы.\n"
        "Начать заново — /start.",
        reply_markup=ReplyKeyboardRemove(),
    )


async def reset_tmp_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    onboarding_state.pop(uid, None)
    t1_state.clear_user(uid)
    t2_state.clear_user(uid)
    await repo.reset_course_state(uid)
    await update.effective_message.reply_text(
        "Тестовый сброс выполнен: курс/задания/прогресс очищены, профиль сохранён.\n"
        "Можешь снова нажать «Начать прохождение».",
        reply_markup=ReplyKeyboardRemove(),
    )


async def sett2_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    v2 = await repo.get_user_v2(uid)
    urow = await repo.get_user_row_for_t1(uid)
    wn = int(v2.get("week_num") or 1) if v2 else 1
    cal_ok = False
    if urow and urow.get("course_start_date"):
        try:
            cs = date.fromisoformat(urow["course_start_date"])
            cal_ok = course_calendar_day(cs, date.today()) >= 15
        except ValueError:
            pass
    if wn < 3 and not cal_ok:
        await update.effective_message.reply_text(
            "Настройка времени Т2 доступна с 3-й недели курса (или с 15-го дня программы)."
        )
        return
    if not context.args:
        await update.effective_message.reply_text("Формат: /sett2 12:00")
        return
    tval = context.args[0].strip()
    await repo.set_user_settings(uid, t2_time=tval)
    await update.effective_message.reply_text(f"Время старта Т2 сохранено: {tval}")


async def addword_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in settings.admin_ids:
        await update.effective_message.reply_text("Недостаточно прав.")
        return
    if len(context.args) < 2:
        await update.effective_message.reply_text("Использование: /addword слово кластер")
        return
    word, cluster = context.args[0], context.args[1]
    await repo.add_word_to_dictionary(word, cluster, update.effective_user.id)
    await repo.recalc_word_dictionary_links()
    await update.effective_message.reply_text(f"Добавлено: {word} -> {cluster}")


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in settings.admin_ids:
        await update.effective_message.reply_text("Недостаточно прав.")
        return
    count = await repo.stats_new_words_weekly()
    await update.effective_message.reply_text(f"Новых слов за неделю: {count}")


async def review_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in settings.admin_ids:
        await update.effective_message.reply_text("Недостаточно прав.")
        return
    rows = await repo.review_unknown_words()
    if not rows:
        await update.effective_message.reply_text("Новых слов без словаря нет.")
        return
    lines = [f"- {word}: {freq}" for word, freq in rows[:20]]
    await update.effective_message.reply_text("Слова вне словаря:\n" + "\n".join(lines))


async def recalc_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in settings.admin_ids:
        await update.effective_message.reply_text("Недостаточно прав.")
        return
    updated = await repo.recalc_word_dictionary_links()
    await update.effective_message.reply_text(f"Пересчет выполнен. Обновлено строк: {updated}")


async def progress_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    urow = await repo.get_user_row_for_t1(uid)
    if not urow or not urow.get("age_group"):
        await update.effective_message.reply_text("Сначала заполни профиль — /start")
        return
    tz = resolve_tz_name(urow.get("timezone"), settings.timezone)
    today = datetime.now(ZoneInfo(tz)).date()
    cs = urow.get("course_start_date")
    if not cs:
        await update.effective_message.reply_text("Курс ещё не начат.")
        return
    course_start = date.fromisoformat(cs)
    days_elapsed = (today - course_start).days + 1
    earned, max_pts = await repo.weekly_points_and_max(uid, course_start)
    pct = int(round((earned / max_pts) * 100)) if max_pts > 0 else 0
    text = (
        f"📊 Прогресс за неделю:\n"
        f"Баллы: {earned} из {max_pts} ({pct}%)\n"
        f"Дней с начала курса: {days_elapsed}"
    )
    await update.effective_message.reply_text(text)


async def tasks_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await send_today_tasks_list(update, context)


async def t3_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    print(f"[T3] callback data: {query.data}", flush=True)
    try:
        await query.answer()
    except Exception as e:
        print(f"[T3] answer error: {e}", flush=True)
    uid = query.from_user.id
    data = query.data
    parts = data.split(":")
    if len(parts) != 3:
        await query.message.reply_text("Ошибка")
        return
    action, sid = parts[1], parts[2]
    try:
        row_id = int(sid)
    except ValueError:
        await query.message.reply_text("Ошибка")
        return
    row = await repo.get_scheduled_task_by_id(row_id)
    if not row or row["user_id"] != uid or row["task_type"] != "T3":
        await query.message.reply_text("Задание не найдено")
        return
    ws = str(row.get("window_start") or "")
    we = str(row.get("window_end") or "")
    now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    if now_iso < ws:
        await query.answer("Дождитесь временного окна", show_alert=True)
        return
    if now_iso > we:
        await query.message.reply_text("Окно задания закончилось — задание удалено.")
        return
    if row["skipped"] and not row["completed"]:
        await query.message.reply_text("Окно задания закончилось — задание удалено.")
        return
    if row["completed"]:
        await query.message.reply_text("Уже обработано")
        return
    if action == "o":
        tasks = get_tasks()
        pool_map = {
            "text": tasks.get("t3_texts", []),
            "skill": tasks.get("t3_skills", []),
            "creativity": tasks.get("t3_creativity", []),
            "practice": tasks.get("t3_practices", []),
        }
        subtype = ""
        for item in pool_map:
            if any(int(x.get("id", 0)) == int(row.get("task_id") or 0) for x in pool_map[item]):
                subtype = item
                break
        pool = pool_map.get(subtype, [])
        task_obj = next((x for x in pool if int(x.get("id", 0)) == int(row.get("task_id") or 0)), {"text": "Задание Т3"})
        text = get_t3_text(task_obj)
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(text="Готово", callback_data=f"t3:d:{row_id}")
        ]])
        await query.message.reply_text(text, reply_markup=kb)
        return
    if action == "d":
        now_s = datetime.now().isoformat(sep=" ", timespec="seconds")
        await repo.complete_scheduled_task(row_id, 3, None, now_s)
        await query.message.reply_text("Ваши данные внесены", reply_markup=continue_keyboard())
        return
    await query.message.reply_text("Неизвестное действие")


_t4_timer_tasks: dict[int, asyncio.Task] = {}


async def _t4_timer_countdown(bot: Bot, chat_id: int, target: int) -> None:
    await asyncio.sleep(target)
    try:
        await bot.send_message(chat_id, "⏱ Время вышло! Нажми «Готово» если выполнил задание.")
    except Exception:
        pass


async def t4_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    uid = query.from_user.id
    data = query.data
    print(f"[T4] callback={data} uid={uid}", flush=True)
    parts = data.split(":")
    if len(parts) != 3:
        await query.answer("Ошибка формата", show_alert=True)
        return
    action, sid = parts[1], parts[2]
    try:
        row_id = int(sid)
    except ValueError:
        await query.answer("Ошибка ID", show_alert=True)
        return
    row = await repo.get_scheduled_task_by_id(row_id)
    print(f"[T4] row={row}", flush=True)
    if not row or row["user_id"] != uid or row["task_type"] != "T4":
        await query.answer("Задание не найдено", show_alert=True)
        return
    ws = str(row.get("window_start") or "")
    we = str(row.get("window_end") or "")
    now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[T4] ws={ws} we={we} now={now_iso} action={action}", flush=True)
    if now_iso < ws:
        await query.answer("Дождитесь временного окна", show_alert=True)
        return
    if now_iso > we:
        await query.answer("Окно задания закончилось", show_alert=True)
        return
    if row["skipped"] and not row["completed"]:
        await query.answer("Окно задания закончилось", show_alert=True)
        return
    if row["completed"]:
        await query.answer("Уже обработано", show_alert=True)
        return
    tasks = get_tasks()
    t4_obj = next(
        (x for x in tasks.get("t4_tasks", []) if int(x.get("id", 0)) == int(row.get("task_id") or 0)),
        {"description": "Короткое задание"},
    )
    if action == "o":
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(text="Принять", callback_data=f"t4:a:{row_id}")
        ]])
        await query.message.reply_text(get_t4_challenge_text(t4_obj.get("description", "")), reply_markup=kb)
        await query.answer()
        return
    if action == "a":
        dm = int(t4_obj.get("duration_min") or 1)
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(text="Готово", callback_data=f"t4:d:{row_id}")
        ]])
        await query.message.reply_text("Выполни задание и нажми «Готово».", reply_markup=kb)
        mm, ss = divmod(dm * 60, 60)
        await query.message.reply_text(f"<i>⏱ Таймер: {mm:02d}:{ss:02d}</i>", parse_mode="HTML")
        old = _t4_timer_tasks.pop(uid, None)
        if old:
            old.cancel()
        _t4_timer_tasks[uid] = asyncio.create_task(_t4_timer_countdown(context.bot, query.message.chat.id, dm * 60))
        await query.answer()
        return
    if action == "d":
        _t4_timer_tasks.pop(uid, None)
        now_s = datetime.now().isoformat(sep=" ", timespec="seconds")
        await repo.complete_scheduled_task(row_id, 4, None, now_s)
        await query.message.reply_text("Ваши данные внесены", reply_markup=continue_keyboard())
        await query.answer()
        return
    await query.answer("Неизвестное действие", show_alert=True)


async def onboarding_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    current = onboarding_state.get(user_id)
    if not current:
        return

    raw_text = (update.effective_message.text or "").strip()
    if not raw_text:
        await update.effective_message.reply_text("Введите текстовое значение.")
        return
    if raw_text.startswith("/"):
        await update.effective_message.reply_text("Сначала завершите заполнение профиля.")
        return

    if current.step == OnboardingStep.WAIT_NAME:
        current.full_name = raw_text
        current.step = OnboardingStep.WAIT_BIRTH_DATE
        onboarding_state[user_id] = current
        await update.effective_message.reply_text("Введите дату рождения в формате ДД.ММ.ГГГГ (например 21.03.1999):")
        return

    if current.step == OnboardingStep.WAIT_BIRTH_DATE:
        if not _is_valid_birth_date(raw_text):
            await update.effective_message.reply_text("Неверный формат даты. Используйте ДД.ММ.ГГГГ")
            return
        current.birth_date = raw_text
        try:
            bd = datetime.strptime(raw_text, "%d.%m.%Y").date()
            ag = get_age_group(bd)
            sign, elem = get_zodiac_and_element(bd)
            await repo.update_user_v2(user_id, age_group=ag, zodiac_sign=sign, element=elem)
        except ValueError:
            pass
        timezone_name = current.detected_timezone
        await repo.update_user_profile(
            telegram_id=user_id,
            full_name=current.full_name or "",
            birth_date=current.birth_date or "",
            location="",
            timezone=timezone_name,
        )
        onboarding_state.pop(user_id, None)
        await repo.ensure_analysis_profile(user_id)
        today_s = datetime.now(ZoneInfo(timezone_name)).date().isoformat()
        await repo.mark_first_exercise_sent(user_id, today_s)
        await update.effective_message.reply_text(
            "Профиль сохранен.\n\n"
            + HOW_IT_WORKS_TEXT,
            reply_markup=continue_keyboard(),
        )
        return

    if current.step.value == "wait_location":
        timezone_name = current.detected_timezone
        await repo.update_user_profile(
            telegram_id=user_id,
            full_name=current.full_name or "",
            birth_date=current.birth_date or "",
            location=raw_text,
            timezone=timezone_name,
        )
        onboarding_state.pop(user_id, None)
        await repo.ensure_analysis_profile(user_id)
        today_s = datetime.now(ZoneInfo(timezone_name)).date().isoformat()
        await repo.mark_first_exercise_sent(user_id, today_s)
        await update.effective_message.reply_text(
            "Профиль сохранен.\n\n"
            + HOW_IT_WORKS_TEXT,
            reply_markup=continue_keyboard(),
        )
        return


async def handle_webhook(request: web.Request) -> web.Response:
    if request.method == "GET":
        return web.Response(text="OK")
    if request.method != "POST":
        return web.Response(status=405, text="Method Not Allowed")
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400, text="Bad Request")
    print(f"[WEBHOOK] POST data: {str(data)[:200]}", flush=True, file=sys.stderr)
    print(f"[WEBHOOK] application={application is not None}, initialized={application_initialized}", flush=True, file=sys.stderr)
    if application and application_initialized:
        try:
            update = Update.de_json(data, application.bot)
            if update and update.message:
                print(f"[WEBHOOK] Processing message from {update.effective_user.id}: {update.message.text}", flush=True, file=sys.stderr)
            await application.process_update(update)
        except Exception as e:
            print(f"[WEBHOOK] error: {e}", flush=True, file=sys.stderr)
            import traceback
            traceback.print_exc()
    else:
        print(f"[WEBHOOK] not ready", flush=True, file=sys.stderr)
    return web.Response(text="OK")


async def health(request: web.Request) -> web.Response:
    return web.Response(text="OK")


async def _init_with_retry(ptb_app, max_retries=5, base_delay=2.0) -> bool:
    import telegram.error
    for attempt in range(max_retries):
        try:
            await ptb_app.initialize()
            print(f"[BOOT] PTB Application initialized (attempt {attempt+1})", flush=True, file=sys.stderr)
            return True
        except telegram.error.InvalidToken:
            print(f"[BOOT] InvalidToken — token is wrong, aborting", flush=True, file=sys.stderr)
            return False
        except Exception as e:
            print(f"[BOOT] PTB init attempt {attempt+1}/{max_retries} failed: {e}", flush=True, file=sys.stderr)
            if attempt < max_retries - 1:
                await asyncio.sleep(base_delay * (2 ** attempt))
    print("[BOOT] PTB init failed after retries", flush=True, file=sys.stderr)
    return False


async def on_startup(app: web.Application) -> None:
    global background_task, application_initialized
    webhook_url_env = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("WEBHOOK_URL")
    if not webhook_url_env:
        raise RuntimeError("WEBHOOK_URL env var is not set")
    webhook_url = f"{webhook_url_env.rstrip('/')}/webhook"
    if application:
        try:
            await application.initialize()
            print("[BOOT] PTB Application initialized", flush=True, file=sys.stderr)
        except Exception as e:
            print(f"[BOOT] PTB init failed: {e}", flush=True, file=sys.stderr)
            return
        try:
            await application.start()
            print("[BOOT] PTB started", flush=True, file=sys.stderr)
        except Exception as e:
            print(f"[BOOT] PTB start failed: {e}", flush=True, file=sys.stderr)
        try:
            await application.bot.set_webhook(webhook_url)
            print(f"[BOOT] Webhook set to {webhook_url}", flush=True, file=sys.stderr)
        except Exception as e:
            print(f"[BOOT] set_webhook failed: {e}", flush=True, file=sys.stderr)
        application_initialized = True
        background_task = asyncio.create_task(t1_background_loop(application.bot, repo, settings))
        print("[BOOT] Background task started", flush=True, file=sys.stderr)
    else:
        print("[BOOT] application is None!", flush=True, file=sys.stderr)


async def on_shutdown(app: web.Application) -> None:
    global background_task
    print("[SHUTDOWN] Cleaning up...")
    if background_task:
        background_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await background_task
    if application:
        with contextlib.suppress(Exception):
            await application.stop()
        with contextlib.suppress(Exception):
            await application.bot.delete_webhook()


async def _run_polling() -> None:
    global background_task
    if application:
        await application.initialize()
        await application.start()
        background_task = asyncio.create_task(t1_background_loop(application.bot, repo, settings))
        print("[BOOT] Background task started", flush=True, file=sys.stderr)
        stop_event = asyncio.Event()
        try:
            await stop_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            await application.stop()
            if background_task:
                background_task.cancel()


def main() -> None:
    global repo, application

    settings = get_settings()
    asyncio.run(init_db(DB_PATH))
    repo = Repository(DB_PATH)
    asyncio.run(repo.seed_word_dictionary(build_default_word_dict_rows()))

    application = Application.builder().token(settings.bot_token).build()

    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("stop", stop_cmd))
    application.add_handler(CommandHandler("reset", reset_cmd))
    application.add_handler(CommandHandler("resettmp", reset_tmp_cmd))
    application.add_handler(CommandHandler("sett2", sett2_cmd))
    application.add_handler(CommandHandler("addword", addword_cmd))
    application.add_handler(CommandHandler("stats", stats_cmd))
    application.add_handler(CommandHandler("review", review_cmd))
    application.add_handler(CommandHandler("recalc", recalc_cmd))
    application.add_handler(CommandHandler("progress", progress_cmd))

    application.add_handler(CallbackQueryHandler(tasks_today, pattern=f"^{TASKS_TODAY}$"))
    application.add_handler(CallbackQueryHandler(task_info_callback, pattern=re.compile(r"^task:info:(\d+)$")))
    application.add_handler(CallbackQueryHandler(t3_callbacks, pattern=re.compile(r"^t3:")))
    application.add_handler(CallbackQueryHandler(t4_callbacks, pattern=re.compile(r"^t4:")))

    register_t1_handlers(application, repo, settings, t1_state)
    register_t2_handlers(application, repo, settings, t2_state)

    async def unified_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        uid = update.effective_user.id
        if uid in onboarding_state:
            await onboarding_handler(update, context)
        elif uid in _t1_state.pending_evening_word:
            await _t1_evening_word_message(update, context)
        elif uid in _t1_state.pending_morning_word:
            await _t1_morning_word_message(update, context)
        elif uid in _t2_state.pending:
            await _t2_text_flow(update, context)

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unified_text_handler))

    webhook_url = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("WEBHOOK_URL")
    print(f"[BOOT] webhook_url='{webhook_url}' RENDER_EXTERNAL_URL='{os.environ.get('RENDER_EXTERNAL_URL')}' WEBHOOK_URL='{os.environ.get('WEBHOOK_URL')}'", flush=True, file=sys.stderr)

    if webhook_url:
        port = int(os.environ.get("PORT", 8080))
        app = web.Application()
        app.router.add_post("/webhook", handle_webhook)
        app.router.add_get("/webhook", handle_webhook)
        app.router.add_get("/", health)
        app.on_startup.append(on_startup)
        app.on_shutdown.append(on_shutdown)
        print(f"[BOOT] Starting webhook server on 0.0.0.0:{port}", flush=True, file=sys.stderr)
        web.run_app(app, host="0.0.0.0", port=port, print=None)
    else:
        print("[BOOT] Starting polling mode", flush=True, file=sys.stderr)
        asyncio.run(_run_polling())


if __name__ == "__main__":
    main()
