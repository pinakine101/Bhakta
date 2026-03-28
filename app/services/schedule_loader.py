"""Загрузка расписания и библиотек заданий из JSON."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"

_SCHEDULES: dict[str, dict] = {}
_TASKS: dict[str, Any] | None = None


def _load_json_relaxed(path: Path) -> dict[str, Any]:
    """Load JSON and recover from raw newlines/tabs inside string literals."""
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        fixed_chars: list[str] = []
        in_string = False
        escaped = False
        text_len = len(text)
        i = 0
        while i < text_len:
            ch = text[i]
            if in_string:
                if escaped:
                    fixed_chars.append(ch)
                    escaped = False
                    i += 1
                    continue
                if ch == "\\":
                    fixed_chars.append(ch)
                    escaped = True
                    i += 1
                    continue
                if ch == '"':
                    # Keep quote as closing only when followed by valid JSON separators.
                    j = i + 1
                    while j < text_len and text[j] in {" ", "\t", "\r", "\n"}:
                        j += 1
                    next_sig = text[j] if j < text_len else ""
                    if next_sig in {",", "}", "]", ":"}:
                        fixed_chars.append(ch)
                        in_string = False
                    else:
                        fixed_chars.append('\\"')
                    i += 1
                    continue
                if ch == "\n":
                    fixed_chars.append("\\n")
                    i += 1
                    continue
                if ch == "\r":
                    i += 1
                    continue
                if ch == "\t":
                    fixed_chars.append("\\t")
                    i += 1
                    continue
                fixed_chars.append(ch)
                i += 1
                continue
            if ch == '"':
                in_string = True
            fixed_chars.append(ch)
            i += 1
        return json.loads("".join(fixed_chars))


def get_schedule(age_group: str) -> dict:
    """Загружает расписание для возрастной группы."""
    if age_group not in _SCHEDULES:
        fname = {
            "15-25": "schedule_15_25.json",
            "25-35": "schedule_25_35.json",
            "35+": "schedule_35_plus.json",
        }.get(age_group, "schedule_15_25.json")
        path = _DATA_DIR / fname
        if not path.exists():
            path = _DATA_DIR / "schedule_15_25.json"
        _SCHEDULES[age_group] = _load_json_relaxed(path)
    return _SCHEDULES[age_group]


def get_tasks() -> dict:
    """Загружает библиотеку заданий (t1, t2_*, t3_*, t4_tasks)."""
    global _TASKS
    if _TASKS is None:
        path = _DATA_DIR / "tasks_library.json"
        if not path.exists():
            path = _DATA_DIR / "tasks.json"
        _TASKS = _load_json_relaxed(path)
    return _TASKS


def t2_art_pool(tasks: dict) -> list[dict]:
    return tasks.get("t2_art") or tasks.get("art", [])


def t2_life_themes_pool(tasks: dict) -> list[dict]:
    return tasks.get("t2_life_themes") or tasks.get("life_themes", [])


def t2_choice_sets_pool(tasks: dict) -> list[dict]:
    return tasks.get("t2_choice_sets") or tasks.get("choice_sets", [])


def get_t1_morning_text(day: int, target_sec: int) -> str:
    del day  # номер дня в расписании; текст задаётся в JSON
    tasks = get_tasks()
    t1 = tasks.get("t1") or {}
    inst = (t1.get("morning") or {}).get("instruction", "").strip()
    if inst:
        if "{target_sec}" in inst or "{target}" in inst:
            return inst.format(target_sec=target_sec, target=target_sec)
        return inst
    return (
        f"Утро. Смотри на точку в центре экрана. Не моргай, не отводи взгляд. "
        f"Цель: {target_sec} секунд. Как только отвёл — нажми «Выполнил»."
    )


def get_t1_evening_text(day: int, rhythm: str, cycles: int) -> str:
    del day
    tasks = get_tasks()
    t1 = tasks.get("t1") or {}
    inst = (t1.get("evening") or {}).get("instruction", "").strip()
    if inst:
        return inst.format(rhythm=rhythm, cycles=cycles)
    return (
        f"Вечер. Дыши в ритме {rhythm}. Количество циклов: {cycles}. "
        f"После дыхания — 30 секунд обычного дыхания, внимание на сознание. "
        f"После — нажми «Выполнил» и напиши одно слово."
    )


def _art_caption(art: dict) -> str:
    if art.get("description"):
        return str(art["description"])
    if art.get("title"):
        auth = art.get("author", "")
        return f"{auth} — {art['title']}".strip(" —")
    return "задание"


def get_t2_art_text(art: dict, duration_min: int | None = None) -> str:
    dm = duration_min if duration_min is not None else int(art.get("duration_min") or 2)
    cat = art.get("category") or ""
    src = art.get("source") or ""
    meta = " · ".join(p for p in (cat, src) if p)
    body = _art_caption(art)
    head = f"Т2 (арт). {meta}" if meta else "Т2 (арт)"
    return (
        f"{head}\n{body}\n\n"
        f"Смотри или представляй образ {dm} мин. Не оценивай. "
        f"После — «Выполнил» и одно слово."
    )


def get_t2_life_theme_text(theme: dict) -> str:
    return (
        f"Тема жизни: {theme['title']}.\n{theme['prompt']}\n"
        "Напиши несколько предложений. Затем нажми «Выполнил» и напиши одно слово."
    )


def get_t2_choice_text(image_ids: list[int], tasks: dict) -> str:
    arts = t2_art_pool(tasks)
    lines: list[str] = []
    for aid in image_ids:
        for a in arts:
            if a["id"] == aid:
                lines.append(f"• {_art_caption(a)}")
                break
    block = "\n".join(lines) if lines else "Выбери один образ из списка задания."
    return (
        "Выбери один образ, который больше откликается:\n"
        f"{block}\n"
        "После выбора нажми «Выполнил» и напиши одно слово — что откликнулось."
    )


def course_calendar_day(course_start: date, today: date) -> int:
    """Номер дня курса 1..30 от даты старта."""
    d = (today - course_start).days + 1
    return max(1, min(d, 30))


def t2_assignment_for_calendar_day(sched: dict, day: int) -> dict[str, Any] | None:
    for a in sched.get("t2_assignments", []):
        if int(a["day"]) == int(day):
            return a
    return None


def parse_t2_start_clock(
    t2_time_setting: str | None, week_num: int, course_cal_day: int | None = None
) -> tuple[int, int]:
    """Час и минута начала окна Т2. С 3-й недели или с 15-го дня курса — из user_settings.t2_time, иначе 12:00."""
    third_phase = week_num >= 3 or (course_cal_day is not None and course_cal_day >= 15)
    if third_phase and t2_time_setting and str(t2_time_setting).strip():
        s = str(t2_time_setting).strip()
        if "-" in s:
            s = s.split("-")[0].strip()
        parts = s.replace(".", ":").split(":")
        try:
            h = int(parts[0])
            m = int(parts[1]) if len(parts) > 1 else 0
            return max(0, min(h, 23)), max(0, min(m, 59))
        except (ValueError, IndexError):
            pass
    return 12, 0


def map_schedule_t2_type_to_subtype(t: str) -> str:
    return {"art": "art", "life_theme": "life_theme", "choice": "choice", "final": "final"}.get(
        t, t
    )


def get_t2_final_text(tasks: dict) -> str:
    final = tasks.get("t2_final") or tasks.get("final") or {}
    if isinstance(final, dict):
        prompt = final.get("prompt", "Напиши одно слово об этом.")
    else:
        prompt = "Напиши одно слово об этом."
    return f"{prompt}\nНажми «Выполнил»."


def get_t3_text(t3_item: dict) -> str:
    return t3_item.get("text", "")


def get_t4_challenge_text(desc: str) -> str:
    return f"Вызов. Принять?\n\nПосле нажатия: {desc}\n\nКогда выполнишь, нажми «Выполнил»."


def split_telegram_chunks(text: str, max_len: int = 3900) -> list[str]:
    """Делит длинный текст на части под лимит Telegram (~4096)."""
    text = text.strip()
    if not text:
        return []
    chunks: list[str] = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        cut = text.rfind("\n", 0, max_len)
        if cut < max_len // 3:
            cut = max_len
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n")
    return chunks


def build_tasks_catalog_text() -> str:
    """Полный перечень заданий из tasks.json (для /plan без рассылки по времени)."""
    tasks = get_tasks()
    parts: list[str] = []
    parts.append(
        "📋 Все задания программы\n"
        "(сейчас бот не шлёт их по часам — ориентируйся на этот список в любое время).\n"
    )

    t1 = tasks.get("t1") or {}
    m = (t1.get("morning") or {}).get("instruction", "")
    e = (t1.get("evening") or {}).get("instruction", "")
    parts.append("━━ Т1 ━━")
    parts.append("Утро:\n" + m)
    parts.append("Вечер (в полном курсе подставляются {rhythm} и {cycles}):\n" + e)

    arts = t2_art_pool(tasks)
    parts.append("\n━━ Т2 — арт ━━")
    for a in arts:
        parts.append(f"{a['id']}. {_art_caption(a)} — {a.get('category', '')}")

    themes = t2_life_themes_pool(tasks)
    parts.append("\n━━ Т2 — жизненные темы ━━")
    for th in themes:
        parts.append(f"{th['id']}. {th['title']}\n{th['prompt']}")

    sets = t2_choice_sets_pool(tasks)
    parts.append("\n━━ Т2 — выбор (номера образов из списка «арт») ━━")
    for cs in sets:
        parts.append(f"Набор {cs['id']}: {cs.get('image_ids', [])}")

    fin = tasks.get("t2_final") or tasks.get("final") or {}
    if isinstance(fin, dict) and fin.get("prompt"):
        parts.append("\n━━ Т2 — финал ━━")
        parts.append(str(fin["prompt"]))

    t3_sections = [
        ("Т3 — тексты", "t3_texts"),
        ("Т3 — навыки", "t3_skills"),
        ("Т3 — творчество", "t3_creativity"),
        ("Т3 — практики", "t3_practices"),
    ]
    for title, key in t3_sections:
        pool = tasks.get(key, [])
        if not pool:
            continue
        parts.append(f"\n━━ {title} ━━")
        for item in pool:
            parts.append(f"{item.get('id', '')}. {item.get('text', '')}")

    parts.append("\n━━ Т4 — телесные вызовы ━━")
    t4_all = tasks.get("t4_tasks", [])
    by_ag: dict[str, list[dict]] = {}
    for t in t4_all:
        by_ag.setdefault(str(t.get("age_group", "?")), []).append(t)
    for ag in sorted(by_ag.keys()):
        parts.append(f"\n— возраст {ag} —")
        for t in by_ag[ag]:
            parts.append(f"{t['id']}. {t.get('description', '')}")

    return "\n".join(parts)
