import asyncio
import logging
import os
from pathlib import Path

from aiogram import Bot

# ========== НАСТРОЙКИ ==========
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOUNDS_DIR = _PROJECT_ROOT / "sounds"
START_SOUND = SOUNDS_DIR / "start.wav"
END_SOUND = SOUNDS_DIR / "end.wav"
DONE_SOUND = SOUNDS_DIR / "done.wav"
_PYGAME_READY = False


# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
def play_sound(sound_path: str | Path) -> None:
    """Воспроизводит звуковой файл локально на ПК, где запущен бот."""
    sound_file = str(sound_path)
    if not Path(sound_file).exists():
        logging.warning("Файл звука не найден: %s", sound_file)
        return

    # Для Windows используем winsound как надёжный fallback.
    if os.name == "nt":
        try:
            import winsound

            winsound.PlaySound(sound_file, winsound.SND_FILENAME | winsound.SND_ASYNC)
            return
        except Exception as exc:
            logging.warning("winsound не сработал, пробуем pygame: %s", exc)

    global _PYGAME_READY
    try:
        import pygame

        if not _PYGAME_READY:
            pygame.mixer.init()
            _PYGAME_READY = True
        pygame.mixer.music.load(sound_file)
        pygame.mixer.music.play()
    except ImportError:
        logging.warning("Pygame не установлен, звуки отключены")
    except Exception as exc:
        logging.error("Ошибка воспроизведения звука %s: %s", sound_path, exc)


async def countdown_notify(chat_id: int, bot: Bot, duration_seconds: int) -> bool:
    """
    Отправляет сообщения с обратным отсчётом.
    Возвращает True при успешном завершении.
    """
    if duration_seconds <= 0:
        return True
    for remaining in range(duration_seconds, 0, -1):
        if remaining % 5 == 0 or remaining <= 10:
            minutes = remaining // 60
            seconds = remaining % 60
            await bot.send_message(
                chat_id,
                f"⏳ Осталось: {minutes:02d}:{seconds:02d}",
                disable_notification=True,
            )
        await asyncio.sleep(1)
    return True


async def start_task_timer(
    chat_id: int,
    bot: Bot,
    duration_seconds: int,
    task_name: str = "задание",
    *,
    notify_countdown: bool = False,
    send_start_message: bool = True,
) -> bool:
    """
    Запускает таймер задания и уведомляет по завершении.
    """
    safe_duration = max(0, int(duration_seconds))
    play_sound(START_SOUND)

    if send_start_message:
        await bot.send_message(
            chat_id,
            f"🎯 Задание «{task_name}» началось!\n⏱️ Времени: {safe_duration} сек.",
        )

    if notify_countdown:
        completed = await countdown_notify(chat_id, bot, safe_duration)
    else:
        await asyncio.sleep(safe_duration)
        completed = True

    if completed:
        play_sound(END_SOUND)
        await bot.send_message(chat_id, f"✅ Время на задание «{task_name}» истекло!")
    return completed
