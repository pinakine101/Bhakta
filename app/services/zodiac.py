"""Определение знака зодиака и стихии по дате рождения."""

from datetime import date
from typing import Tuple


# Конец каждого знака (месяц, день), знак, стихия. Козерог: 22 дек - 19 янв.
_ZODIAC_ORDER = [
    ((1, 19), "Козерог", "earth"),
    ((2, 18), "Водолей", "air"),
    ((3, 20), "Рыбы", "water"),
    ((4, 19), "Овен", "fire"),
    ((5, 20), "Телец", "earth"),
    ((6, 20), "Близнецы", "air"),
    ((7, 22), "Рак", "water"),
    ((8, 22), "Лев", "fire"),
    ((9, 22), "Дева", "earth"),
    ((10, 22), "Весы", "air"),
    ((11, 21), "Скорпион", "water"),
    ((12, 21), "Стрелец", "fire"),
]


def get_zodiac_and_element(birth_date: date) -> Tuple[str, str]:
    """Возвращает (знак, стихия) для даты рождения."""
    m, d = birth_date.month, birth_date.day
    if (m == 12 and d >= 22) or (m == 1 and d <= 19):
        return "Козерог", "earth"
    md = (m, d)
    for (em, ed), sign, element in _ZODIAC_ORDER:
        if md <= (em, ed):
            return sign, element
    return "Козерог", "earth"


def get_age_group(birth_date: date) -> str:
    """Определяет возрастную группу по дате рождения."""
    today = date.today()
    age = today.year - birth_date.year - ((today.month, today.day) < (birth_date.month, birth_date.day))
    if age < 25:
        return "15-25"
    if age < 35:
        return "25-35"
    return "35+"
