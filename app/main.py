from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardRemove,
)
from aiohttp import web

from app.api import create_api_app
from app.config import Settings, get_settings
from app.db.database import init_db
from app.db.repository import Repository
from app.services.profile import detect_timezone_by_location, is_valid_birth_date
from app.services.schedule_loader import get_tasks
from app.services.schedule_loader import get_t3_text, get_t4_challenge_text
from app.services.word_analysis import build_default_word_dict_rows
from app.services.schedule_loader import course_calendar_day
from app.services.zodiac import get_age_group, get_zodiac_and_element
from app.timer import DONE_SOUND, START_SOUND, play_sound
from app.t1_bot import T1State, register_t1_handlers, send_daily_plan_now, t1_background_loop
from app.t2_bot import T2State, register_t2_handlers

DB_PATH = os.path.join(os.getcwd(), "cont_bot.sqlite3")
PRACTICE_START = "practice:start"
TASKS_TODAY = "tasks:today"

t1_state = T1State()
t2_state = T2State()

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
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Продолжить", callback_data=TASKS_TODAY)]]
    )


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


async def send_today_tasks_list(bot: Bot, repo: Repository, settings: Settings, uid: int) -> None:
    urow = await repo.get_user_row_for_t1(uid)
    tz = resolve_tz_name((urow or {}).get("timezone"), settings.timezone)
    today_s = datetime.now(ZoneInfo(tz)).date().isoformat()
    rows = await repo.list_scheduled_for_date(uid, today_s)
    if not rows:
        await bot.send_message(uid, "На сегодня заданий нет.", reply_markup=ReplyKeyboardRemove())
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

    await bot.send_message(
        uid,
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows) if kb_rows else ReplyKeyboardRemove(),
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


def practice_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Начать прохождение", callback_data=PRACTICE_START)],
        ]
    )


def build_dispatcher(repo: Repository, settings: Settings) -> Dispatcher:
    dp = Dispatcher()
    onboarding_state: dict[int, OnboardingState] = {}
    register_t1_handlers(dp, repo, settings, t1_state)
    register_t2_handlers(dp, repo, settings, t2_state)

    async def send_help(message: Message) -> None:
        await message.answer(
            "Команды:\n"
            "/start — регистрация и профиль\n"
            "/stop — сбросить незавершённый ввод\n"
            "/reset — удалить все твои данные и активность в боте (безвозвратно)\n"
            "/resettmp — (временно) сбросить курс/задания для теста, профиль сохранится\n"
            "/sett2 ЧЧ:ММ — время напоминания Т2 (с 3-й недели курса)\n\n"
            "После /start: имя, дата рождения, город.",
            reply_markup=ReplyKeyboardRemove(),
        )

    @dp.message(Command("start"))
    async def start_cmd(message: Message) -> None:
        user_id = message.from_user.id
        await repo.ensure_user(user_id, message.from_user.username or "anonymous")
        await repo.ensure_analysis_profile(user_id)
        profile = await repo.get_user_profile(user_id)
        if profile and profile.get("full_name") and profile.get("birth_date") and profile.get("location"):
            await message.answer(
                "С возвращением! Профиль уже заполнен.\n\n"
                + build_practice_ready_message(profile.get("timezone"), settings.timezone),
                reply_markup=practice_keyboard(),
            )
            return

        onboarding_state[user_id] = OnboardingState(step=OnboardingStep.WAIT_NAME)
        await message.answer(
            "Сначала заполним профиль.\nВведите ваше имя:",
            reply_markup=ReplyKeyboardRemove(),
        )

    @dp.message(Command("help"))
    async def help_cmd(message: Message) -> None:
        await send_help(message)

    @dp.message(Command("stop"))
    async def stop_cmd(message: Message) -> None:
        uid = message.from_user.id
        onboarding_state.pop(uid, None)
        t1_state.clear_user(uid)
        t2_state.clear_user(uid)
        await message.answer(
            "В Telegram нет кнопки «выйти из бота»: это обычный чат.\n"
            "Можно просто перестать писать, отключить уведомления (⋮ в чате → Уведомления) "
            "или удалить чат с ботом.\n\n"
            "Незавершённый ввод анкеты сброшен. Снова с нуля — /start.",
            reply_markup=ReplyKeyboardRemove(),
        )

    @dp.message(Command("reset"))
    async def reset_cmd(message: Message) -> None:
        uid = message.from_user.id
        onboarding_state.pop(uid, None)
        t1_state.clear_user(uid)
        t2_state.clear_user(uid)
        await repo.wipe_all_user_data(uid)
        await message.answer(
            "Все твои данные и активность в боте удалены из базы.\n"
            "Начать заново — /start.",
            reply_markup=ReplyKeyboardRemove(),
        )

    @dp.message(Command("resettmp"))
    async def reset_tmp_cmd(message: Message) -> None:
        uid = message.from_user.id
        onboarding_state.pop(uid, None)
        t1_state.clear_user(uid)
        t2_state.clear_user(uid)
        await repo.reset_course_state(uid)
        await message.answer(
            "Тестовый сброс выполнен: курс/задания/прогресс очищены, профиль сохранён.\n"
            "Можешь снова нажать «Начать прохождение».",
            reply_markup=ReplyKeyboardRemove(),
        )

    @dp.message(lambda message: message.from_user and message.from_user.id in onboarding_state)
    async def onboarding_handler(message: Message) -> None:
        user_id = message.from_user.id
        current = onboarding_state.get(user_id)
        if not current:
            return

        raw_text = (message.text or "").strip()
        if not raw_text:
            await message.answer("Введите текстовое значение.")
            return
        if raw_text.startswith("/"):
            await message.answer("Сначала завершите заполнение профиля.")
            return

        if current.step == OnboardingStep.WAIT_NAME:
            current.full_name = raw_text
            current.step = OnboardingStep.WAIT_BIRTH_DATE
            onboarding_state[user_id] = current
            await message.answer("Введите дату рождения в формате ДД.ММ.ГГГГ (например 21.03.1999):")
            return

        if current.step == OnboardingStep.WAIT_BIRTH_DATE:
            if not is_valid_birth_date(raw_text):
                await message.answer("Неверный формат даты. Используйте ДД.ММ.ГГГГ")
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
            await message.answer("Введите место рождения (город, страна), например: Москва, Россия")
            return

        if current.step == OnboardingStep.WAIT_LOCATION:
            settings = get_settings()
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
            await message.answer(
                f"Профиль сохранен.\nЧасовой пояс определен: {timezone_name}\n\n"
                + HOW_IT_WORKS_TEXT
                + "\n\n"
                + build_practice_ready_message(timezone_name, settings.timezone),
                reply_markup=practice_keyboard(),
            )
            return

    @dp.callback_query(F.data == PRACTICE_START)
    async def practice_start(callback: CallbackQuery) -> None:
        uid = callback.from_user.id
        urow = await repo.get_user_row_for_t1(uid)
        tz = resolve_tz_name((urow or {}).get("timezone"), settings.timezone)
        now_local = datetime.now(ZoneInfo(tz))
        today_s = now_local.date().isoformat()
        already = bool((urow or {}).get("first_exercise_sent"))
        if not already:
            await repo.mark_first_exercise_sent(uid, today_s)

        # Старые профили могли быть без age_group; доопределяем из даты рождения.
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

        # Список на сегодня отправляем всегда, даже если курс уже активирован.
        await send_daily_plan_now(callback.bot, repo, settings, uid)
        with contextlib.suppress(TelegramBadRequest):
            await callback.answer()

    @dp.callback_query(F.data == TASKS_TODAY)
    async def tasks_today(callback: CallbackQuery) -> None:
        if not callback.from_user:
            return
        await send_today_tasks_list(callback.bot, repo, settings, callback.from_user.id)
        with contextlib.suppress(TelegramBadRequest):
            await callback.answer()

    @dp.callback_query(F.data.startswith("t3:"))
    async def t3_callbacks(callback: CallbackQuery) -> None:
        if not callback.from_user or not callback.message:
            await callback.answer("Ошибка", show_alert=True)
            return
        parts = (callback.data or "").split(":")
        if len(parts) != 3:
            await callback.answer("Ошибка", show_alert=True)
            return
        action, sid = parts[1], parts[2]
        try:
            row_id = int(sid)
        except ValueError:
            await callback.answer("Ошибка", show_alert=True)
            return
        row = await repo.get_scheduled_task_by_id(row_id)
        if not row or row["user_id"] != callback.from_user.id or row["task_type"] != "T3":
            await callback.answer("Задание не найдено", show_alert=True)
            return
        if row["skipped"] and not row["completed"]:
            await callback.answer("Окно задания закончилось — задание удалено.", show_alert=True)
            return
        if row["completed"]:
            await callback.answer("Уже обработано")
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
            kb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Готово", callback_data=f"t3:d:{row_id}")]]
            )
            await callback.message.answer(text, reply_markup=kb)
            await callback.answer()
            return
        if action == "d":
            play_sound(DONE_SOUND)
            now_s = datetime.now().isoformat(sep=" ", timespec="seconds")
            await repo.complete_scheduled_task(row_id, 3, None, now_s)
            await callback.message.answer("Ваши данные внесены", reply_markup=continue_keyboard())
            await callback.answer()
            return
        await callback.answer("Неизвестное действие", show_alert=True)

    @dp.callback_query(F.data.startswith("t4:"))
    async def t4_callbacks(callback: CallbackQuery) -> None:
        if not callback.from_user or not callback.message:
            await callback.answer("Ошибка", show_alert=True)
            return
        parts = (callback.data or "").split(":")
        if len(parts) != 3:
            await callback.answer("Ошибка", show_alert=True)
            return
        action, sid = parts[1], parts[2]
        try:
            row_id = int(sid)
        except ValueError:
            await callback.answer("Ошибка", show_alert=True)
            return
        row = await repo.get_scheduled_task_by_id(row_id)
        if not row or row["user_id"] != callback.from_user.id or row["task_type"] != "T4":
            await callback.answer("Задание не найдено", show_alert=True)
            return
        if row["skipped"] and not row["completed"]:
            await callback.answer("Окно задания закончилось — задание удалено.", show_alert=True)
            return
        if row["completed"]:
            await callback.answer("Уже обработано")
            return
        tasks = get_tasks()
        t4_obj = next(
            (x for x in tasks.get("t4_tasks", []) if int(x.get("id", 0)) == int(row.get("task_id") or 0)),
            {"description": "Короткое задание"},
        )
        if action == "o":
            kb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Принять", callback_data=f"t4:a:{row_id}")]]
            )
            await callback.message.answer(get_t4_challenge_text(t4_obj.get("description", "")), reply_markup=kb)
            await callback.answer()
            return
        if action == "a":
            play_sound(START_SOUND)
            kb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Готово", callback_data=f"t4:d:{row_id}")]]
            )
            await callback.message.answer("Выполни задание и нажми «Готово».", reply_markup=kb)
            await callback.answer()
            return
        if action == "d":
            play_sound(DONE_SOUND)
            now_s = datetime.now().isoformat(sep=" ", timespec="seconds")
            await repo.complete_scheduled_task(row_id, 4, None, now_s)
            await callback.message.answer("Ваши данные внесены", reply_markup=continue_keyboard())
            await callback.answer()
            return
        await callback.answer("Неизвестное действие", show_alert=True)

    def _is_admin(message: Message) -> bool:
        return message.from_user.id in settings.admin_ids

    @dp.message(Command("addword"))
    async def addword_cmd(message: Message) -> None:
        if not _is_admin(message):
            await message.answer("Недостаточно прав.")
            return
        parts = (message.text or "").split(maxsplit=2)
        if len(parts) < 3:
            await message.answer("Использование: /addword слово кластер")
            return
        _, word, cluster = parts
        await repo.add_word_to_dictionary(word, cluster, message.from_user.id)
        await repo.recalc_word_dictionary_links()
        await message.answer(f"Добавлено: {word} -> {cluster}")

    @dp.message(Command("stats"))
    async def stats_cmd(message: Message) -> None:
        if not _is_admin(message):
            await message.answer("Недостаточно прав.")
            return
        count = await repo.stats_new_words_weekly()
        await message.answer(f"Новых слов за неделю: {count}")

    @dp.message(Command("review"))
    async def review_cmd(message: Message) -> None:
        if not _is_admin(message):
            await message.answer("Недостаточно прав.")
            return
        rows = await repo.review_unknown_words()
        if not rows:
            await message.answer("Новых слов без словаря нет.")
            return
        lines = [f"- {word}: {freq}" for word, freq in rows[:20]]
        await message.answer("Слова вне словаря:\n" + "\n".join(lines))

    @dp.message(Command("recalc"))
    async def recalc_cmd(message: Message) -> None:
        if not _is_admin(message):
            await message.answer("Недостаточно прав.")
            return
        updated = await repo.recalc_word_dictionary_links()
        await message.answer(f"Пересчет выполнен. Обновлено строк: {updated}")

    @dp.message(Command("sett2"))
    async def sett2_cmd(message: Message) -> None:
        uid = message.from_user.id
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
            await message.answer("Настройка времени Т2 доступна с 3-й недели курса (или с 15-го дня программы).")
            return
        parts = (message.text or "").split()
        if len(parts) < 2:
            await message.answer("Формат: /sett2 12:00")
            return
        tval = parts[1].strip()
        await repo.set_user_settings(uid, t2_time=tval)
        await message.answer(f"Время старта Т2 сохранено: {tval}", reply_markup=ReplyKeyboardRemove())

    return dp


async def main() -> None:
    settings = get_settings()
    await init_db(DB_PATH)
    repo = Repository(DB_PATH)
    await repo.seed_word_dictionary(build_default_word_dict_rows())

    bot = Bot(token=settings.bot_token)
    dp = build_dispatcher(repo, settings)
    scheduler_task = asyncio.create_task(t1_background_loop(bot, repo, settings))
    api_app = create_api_app(repo)
    api_runner: web.AppRunner | None = None
    try:
        api_runner = web.AppRunner(api_app)
        await api_runner.setup()
        api_site = web.TCPSite(api_runner, "0.0.0.0", settings.api_port)
        await api_site.start()
    except OSError as exc:
        print(
            f"Предупреждение: HTTP API не запущен (порт {settings.api_port} занят): {exc}\n"
            f"Бот в Telegram продолжит работу. Для API: остановите процесс на этом порту "
            f"или задайте в .env другой API_PORT (например 8081)."
        )
        if api_runner is not None:
            await api_runner.cleanup()
            api_runner = None

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        scheduler_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await scheduler_task
        if api_runner is not None:
            await api_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
