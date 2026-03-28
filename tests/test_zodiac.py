from datetime import date

from app.services.zodiac import get_age_group, get_zodiac_and_element


def test_capricorn_boundary() -> None:
    sign, elem = get_zodiac_and_element(date(1990, 1, 10))
    assert sign == "Козерог"
    assert elem == "earth"


def test_aquarius() -> None:
    sign, elem = get_zodiac_and_element(date(1990, 2, 5))
    assert sign == "Водолей"
    assert elem == "air"


def test_age_group() -> None:
    today = date.today()
    young = date(today.year - 20, 1, 1)
    mid = date(today.year - 30, 1, 1)
    old = date(today.year - 40, 1, 1)
    assert get_age_group(young) == "15-25"
    assert get_age_group(mid) == "25-35"
    assert get_age_group(old) == "35+"
