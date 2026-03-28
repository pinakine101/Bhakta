import pytest

from app.db.database import init_db
from app.db.repository import Repository


@pytest.mark.asyncio
async def test_set_user_settings_accepts_named_t2_time(tmp_path) -> None:
    db_path = tmp_path / "test.sqlite3"
    await init_db(str(db_path))
    repo = Repository(str(db_path))
    user_id = 1001
    await repo.ensure_user(user_id, "u1")

    await repo.set_user_settings(user_id, t2_time="13:30")

    settings = await repo.get_user_settings(user_id)
    assert settings["t2_time"] == "13:30"


@pytest.mark.asyncio
async def test_update_user_v2_allows_partial_week_num_update(tmp_path) -> None:
    db_path = tmp_path / "test.sqlite3"
    await init_db(str(db_path))
    repo = Repository(str(db_path))
    user_id = 1002
    await repo.ensure_user(user_id, "u2")

    await repo.update_user_v2(user_id, week_num=2)

    v2 = await repo.get_user_v2(user_id)
    assert v2 is not None
    assert v2["week_num"] == 2


@pytest.mark.asyncio
async def test_get_user_v2_contains_required_fields(tmp_path) -> None:
    db_path = tmp_path / "test.sqlite3"
    await init_db(str(db_path))
    repo = Repository(str(db_path))
    user_id = 1003
    await repo.ensure_user(user_id, "u3")

    v2 = await repo.get_user_v2(user_id)
    assert v2 is not None
    required = {"age_group", "zodiac_sign", "element", "week_num", "course_day", "birth_date", "timezone"}
    assert required.issubset(v2.keys())
