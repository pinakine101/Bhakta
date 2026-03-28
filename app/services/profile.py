from __future__ import annotations

from datetime import datetime

from geopy.geocoders import Nominatim
from timezonefinder import TimezoneFinder


def is_valid_birth_date(raw_value: str) -> bool:
    try:
        datetime.strptime(raw_value, "%d.%m.%Y")
        return True
    except ValueError:
        return False


def detect_timezone_by_location(location: str, fallback_tz: str) -> str:
    geolocator = Nominatim(user_agent="cont_bot_profile_tz")
    point = geolocator.geocode(location, language="ru", timeout=10)
    if not point:
        return fallback_tz

    tz_finder = TimezoneFinder()
    timezone_name = tz_finder.timezone_at(lng=point.longitude, lat=point.latitude)
    return timezone_name or fallback_tz
