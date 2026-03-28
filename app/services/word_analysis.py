from __future__ import annotations

from collections import Counter, defaultdict


CLUSTERS: dict[str, list[str]] = {
    "тревога": ["тревога", "страх", "паника", "напряжение", "тяжесть", "беспокойство", "волнение", "давление"],
    "покой": ["покой", "тишина", "легкость", "пустота", "свет", "спокойствие", "расслабление", "тепло"],
    "тело": ["тело", "руки", "ноги", "спина", "шея", "дыхание", "пульс", "жар", "холод"],
    "эмоции": ["грусть", "радость", "злость", "обида", "тоска", "стыд", "вина"],
    "время": ["утро", "день", "вечер", "ночь", "сейчас", "потом"],
    "восприятие": ["вижу", "слышу", "чувствую", "замечаю", "наблюдаю", "смотрю"],
    "сопротивление": ["не хочу", "скучно", "зачем", "пусто", "непонятно", "бесполезно"],
}


def task_type_to_short(task_type: str) -> str:
    return {
        "daily_mandatory": "t1",
        "floating_deadline": "t2",
        "recommended": "t3",
        "surprise": "t4",
    }.get(task_type, task_type)


def task_type_to_course_label(task_type: str) -> str:
    """Короткая метка курса для кнопок и UI (Т1–Т4)."""
    return {
        "daily_mandatory": "Т1",
        "floating_deadline": "Т2",
        "recommended": "Т3",
        "surprise": "Т4",
        "T1_morning": "Т1",
        "T1_evening": "Т1",
        "T2": "Т2",
        "T3": "Т3",
        "T4": "Т4",
    }.get(task_type, "")


def normalize_word(value: str) -> str:
    return value.strip().lower()


def build_default_word_dict_rows() -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for cluster, words in CLUSTERS.items():
        rows.extend((normalize_word(word), cluster) for word in words)
    return rows


def calc_trend(current_count: int, previous_count: int) -> str:
    if current_count > previous_count:
        return "рост"
    if current_count < previous_count:
        return "снижение"
    return "стабильно"


def top_words(words: list[str], limit: int = 5) -> list[tuple[str, int]]:
    return Counter(words).most_common(limit)


def dominant_cluster(clusters: list[str]) -> str | None:
    if not clusters:
        return None
    return Counter(clusters).most_common(1)[0][0]


def correlation_by_task(records: list[tuple[str, str]]) -> dict[str, dict[str, int]]:
    # records: [(task_type_short, cluster), ...]
    agg: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for task_type_short, cluster in records:
        agg[task_type_short][cluster] += 1
    return {k: dict(v) for k, v in agg.items()}
