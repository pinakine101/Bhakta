from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from zoneinfo import ZoneInfo

from aiohttp import web
from dotenv import load_dotenv
from telegram import (
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
from app.services.profile import detect_timezone_by_location, is_valid_birth_date
from app.services.schedule_loader import (
    course_calendar_day,
    get_tasks,
    get_t3_text,
    get_t4_challenge_text,
)
from app.services.word_analysis import build_default_word_dict_rows
from app.services.zodiac import get_age_group, get_zodiac_and_element
from app.timer import DONE_SOUND, START_SOUND, play_sound
from app.t1_bot import T1State, register_t1_handlers, send_daily_plan_now, t1_background_loop
from app.t2_bot import T2State, register_t2_handlers

load_dotenv()
DB_PATH = os.path.join(os.getcwd(), "cont_bot.sqlite3")
PRACTICE_START = "practice:start"
TASKS_TODAY = "tasks:today"

t1_state = T1State()
t2_state = T2State()

settings = get_settings()
repo = None
application: Application | None = None
background_task: asyncio.Task | None = None


def resolve_tz_name(tz_name: str | None, fallback: str) -> str:
    name = (tz_name or fallback).strip()
    try:
        ZoneInfo(name)
        return name
    except Exception:
        return fallback


def build_practice_ready_message(tz_name: str, fallback_tz: str) -> str:
    return (
        "После нажатия «Начать прохождение» ты сразу получишь список заданий на текущие сутки "
        "без привязки к времени внутри дня.\n\n"
        "Подтверди готовность кнопкой ниже."
    )


HOW_IT_WORKS_TEXT = """Бот помогает выстроить ежедневную практику созерцания. Задания приходят сами — остаётся нажимать кнопки и иногда писать слово.

ТИПЫ ЗАДАНИЙ

Т1 — ежедневно: утром — концентрация на точке, вечером — дыхание в ритме.
Т2 — желательные: искусство, темы жизни, выборы, итог месяца.
Т3 — рекомендации: статьи, навыки, творчество, прогулки.
Т4 — вызовы: короткие телесные упражнения в случайное время.

КАК ВЫПОЛНЯТЬ
Бот присылает кнопку с названием задания.
Нажимаете — появляется описание и кнопки «Начать» / «Готово».
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


def practice_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(text="Начать прохождение", callback_data=PRACTICE_START)
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


async def send_today_tasks_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    urow = await repo.get_user_row_for_t1(uid)
    tz = resolve_tz_name((urow or {}).get("timezone"), settings.timezone)
    today_s = datetime.now(ZoneInfo(tz)).date().isoformat()
    rows = await repo.list_scheduled_for_date(uid, today_s)
    if not rows:
        await update.effective_message.reply_text(
            "На сегодня заданий нет.",
            reply_markup=ReplyKeyboardRemove()
        )
        return

    lines: list[str] = ["Список заданий на сегодня:"]
    kb_rows: list[list[InlineKeyboardButton]] = []
    seen_button_titles: set[str] = set()
    for r in rows:
        title = _task_title(str(r["task_type"]), r.get("t2_subtype"))
        done = bool(r.get("completed"))
        skipped = bool(r.get("skipped"))
        if done:
            lines.append(f"• <s>{title}</s>")
        elif skipped:
            lines.append(f"• <s>{title}</s> (пропущено)")
        else:
            lines.append(f"• {title}")
            cb = _task_open_callback(str(r["task_type"]), int(r["id"]))
            if cb and title not in seen_button_titles:
                kb_rows.append([InlineKeyboardButton(text=title, callback_data=cb)])
                seen_button_titles.add(title)

    await update.effective_message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb_rows) if kb_rows else ReplyKeyboardRemove(),
    )


class OnboardingStep(str, Enum):
    WAIT_NAME = "wait_name"
    WAIT_BIRTH_DATE = "wait_birth_date"
    WAIT_LOCATION = "wait_location"


@dataclass
class OnboardingState:
    step: OnboardingStep
    full_name: str | None = None
    birth_date: str | None = None


onboarding_state: dict[int, OnboardingState] = {}


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    await repo.ensure_user(user_id, update.effective_user.username or "anonymous")
    await repo.ensure_analysis_profile(user_id)
    profile = await repo.get_user_profile(user_id)
    if profile and profile.get("full_name") and profile.get("birth_date") and profile.get("location"):
        await update.effective_message.reply_text(
            "С возвращением! Профиль уже заполнен.\n\n"
            + build_practice_ready_message(profile.get("timezone"), settings.timezone),
            reply_markup=practice_keyboard(),
        )
        return

    onboarding_state[user_id] = OnboardingState(step=OnboardingStep.WAIT_NAME)
    await update.effective_message.reply_text(
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


async def practice_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    urow = await repo.get_user_row_for_t1(uid)
    tz = resolve_tz_name((urow or {}).get("timezone"), settings.timezone)
    now_local = datetime.now(ZoneInfo(tz))
    today_s = now_local.date().isoformat()
    already = bool((urow or {}).get("first_exercise_sent"))
    if not already:
        await repo.mark_first_exercise_sent(uid, today_s)

    refreshed = await repo.get_user_row_for_t1(uid)
    if refreshed and not refreshed.get("age_group") and refreshed.get("birth_date"):
        try:
            bd = datetime.strptime(str(refreshed["birth_date"]), "%d.%m.%Y").date()
            ag = get_age_group(bd)
            sign, elem = get_zodiac_and_element(bd)
            await repo.update_user_v2(uid, age_group=ag, zodiac_sign=sign, element=elem)
        except ValueError:
            pass
        refreshed = await repo.get_user_row_for_t1(uid)

    if refreshed and refreshed.get("age_group"):
        await repo.ensure_t1_progress(uid, refreshed["age_group"])

    await send_daily_plan_now(query.bot, repo, settings, uid)


async def tasks_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await send_today_tasks_list(update, context)


async def t3_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
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
        play_sound(DONE_SOUND)
        now_s = datetime.now().isoformat(sep=" ", timespec="seconds")
        await repo.complete_scheduled_task(row_id, 3, None, now_s)
        await query.message.reply_text("Ваши данные внесены", reply_markup=continue_keyboard())
        return
    await query.message.reply_text("Неизвестное действие")


async def t4_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
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
    if not row or row["user_id"] != uid or row["task_type"] != "T4":
        await query.message.reply_text("Задание не найдено")
        return
    if row["skipped"] and not row["completed"]:
        await query.message.reply_text("Окно задания закончилось — задание удалено.")
        return
    if row["completed"]:
        await query.message.reply_text("Уже обработано")
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
        return
    if action == "a":
        play_sound(START_SOUND)
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(text="Готово", callback_data=f"t4:d:{row_id}")
        ]])
        await query.message.reply_text("Выполни задание и нажми «Готово».", reply_markup=kb)
        return
    if action == "d":
        play_sound(DONE_SOUND)
        now_s = datetime.now().isoformat(sep=" ", timespec="seconds")
        await repo.complete_scheduled_task(row_id, 4, None, now_s)
        await query.message.reply_text("Ваши данные внесены", reply_markup=continue_keyboard())
        return
    await query.message.reply_text("Неизвестное действие")


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
        if not is_valid_birth_date(raw_text):
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
        current.step = OnboardingStep.WAIT_LOCATION
        onboarding_state[user_id] = current
        await update.effective_message.reply_text("Введите место рождения (город, страна), например: Москва, Россия")
        return

    if current.step == OnboardingStep.WAIT_LOCATION:
        timezone_name = detect_timezone_by_location(raw_text, settings.timezone)
        await repo.update_user_profile(
            telegram_id=user_id,
            full_name=current.full_name or "",
            birth_date=current.birth_date or "",
            location=raw_text,
            timezone=timezone_name,
        )
        onboarding_state.pop(user_id, None)
        await repo.ensure_analysis_profile(user_id)
        await update.effective_message.reply_text(
            f"Профиль сохранен.\nЧасовой пояс определен: {timezone_name}\n\n"
            + HOW_IT_WORKS_TEXT
            + "\n\n"
            + build_practice_ready_message(timezone_name, settings.timezone),
            reply_markup=practice_keyboard(),
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
    if application:
        update = Update.de_json(data, application.bot)
        await application.process_update(update)
    return web.Response(text="OK")


async def health(request: web.Request) -> web.Response:
    return web.Response(text="OK")


async def on_startup(app: web.Application) -> None:
    global background_task
    webhook_url_env = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("WEBHOOK_URL")
    if not webhook_url_env:
        raise RuntimeError("RENDER_EXTERNAL_URL or WEBHOOK_URL env var is not set")
    webhook_url = f"{webhook_url_env.rstrip('/')}/webhook"
    print(f"[BOOT] Setting webhook to {webhook_url}", flush=True, file=sys.stderr)
    if application:
        await application.initialize()
        await application.start()
        await application.bot.set_webhook(webhook_url)
        background_task = asyncio.create_task(t1_background_loop(application.bot, repo, settings))
        print("[BOOT] Background task started", flush=True, file=sys.stderr)


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

    application.add_handler(CallbackQueryHandler(practice_start, pattern=f"^{PRACTICE_START}$"))
    application.add_handler(CallbackQueryHandler(tasks_today, pattern=f"^{TASKS_TODAY}$"))
    application.add_handler(CallbackQueryHandler(t3_callbacks, pattern="^t3:"))
    application.add_handler(CallbackQueryHandler(t4_callbacks, pattern="^t4:"))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, onboarding_handler))

    register_t1_handlers(application, repo, settings, t1_state)
    register_t2_handlers(application, repo, settings, t2_state)

    webhook_url = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("WEBHOOK_URL")

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
