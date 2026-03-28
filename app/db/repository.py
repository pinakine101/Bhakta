from __future__ import annotations

import json
from datetime import date
from datetime import datetime, timedelta
import aiosqlite

from app.services.word_analysis import normalize_word


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def ensure_user(self, telegram_id: int, username: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO users (telegram_id, username)
                VALUES (?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET username = excluded.username
                """,
                (telegram_id, username or "anonymous"),
            )
            await db.commit()

    async def get_course_day(self, telegram_id: int) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT course_day FROM users WHERE telegram_id = ?",
                (telegram_id,),
            )
            row = await cursor.fetchone()
            return int(row[0]) if row else 1

    async def set_course_day(self, telegram_id: int, day: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE users SET course_day = ? WHERE telegram_id = ?",
                (day, telegram_id),
            )
            await db.commit()

    async def update_user_profile(
        self,
        telegram_id: int,
        full_name: str,
        birth_date: str,
        location: str,
        timezone: str,
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE users
                SET full_name = ?, birth_date = ?, location = ?, timezone = ?
                WHERE telegram_id = ?
                """,
                (full_name, birth_date, location, timezone, telegram_id),
            )
            await db.commit()

    async def get_user_profile(self, telegram_id: int) -> dict | None:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT full_name, birth_date, location, timezone
                FROM users
                WHERE telegram_id = ?
                """,
                (telegram_id,),
            )
            row = await cursor.fetchone()
            if not row:
                return None
            return {
                "full_name": row[0],
                "birth_date": row[1],
                "location": row[2],
                "timezone": row[3],
            }

    async def ensure_analysis_profile(self, user_id: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO user_profile
                (user_id, responsive_tasks, avoidant_tasks, reports_enabled)
                VALUES (?, ?, ?, 1)
                """,
                (user_id, "[]", "[]"),
            )
            await db.commit()

    async def try_schedule_first_exercise(self, user_id: int, due_utc_iso: str) -> tuple[str, str | None]:
        """Returns ('scheduled', due_utc), ('pending', existing_due_utc), or ('already_done', None)."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT first_exercise_sent, first_exercise_due_utc FROM users WHERE telegram_id = ?",
                (user_id,),
            )
            row = await cursor.fetchone()
            if not row:
                return ("already_done", None)
            sent, due = int(row[0] or 0), row[1]
            if sent:
                return ("already_done", None)
            if due is not None:
                return ("pending", due)
            await db.execute(
                """
                UPDATE users
                SET first_exercise_due_utc = ?, first_exercise_sent = 0
                WHERE telegram_id = ?
                """,
                (due_utc_iso, user_id),
            )
            await db.commit()
        return ("scheduled", due_utc_iso)

    async def list_first_exercise_due_user_ids(self, now_utc_iso: str) -> list[int]:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT telegram_id FROM users
                WHERE first_exercise_sent = 0
                  AND first_exercise_due_utc IS NOT NULL
                  AND first_exercise_due_utc <= ?
                """,
                (now_utc_iso,),
            )
            rows = await cursor.fetchall()
            return [int(r[0]) for r in rows]

    async def mark_first_exercise_sent(self, user_id: int, course_start_date_local: str | None = None) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            if course_start_date_local:
                await db.execute(
                    """
                    UPDATE users
                    SET first_exercise_sent = 1,
                        first_exercise_due_utc = NULL,
                        course_start_date = COALESCE(course_start_date, ?)
                    WHERE telegram_id = ?
                    """,
                    (course_start_date_local, user_id),
                )
            else:
                await db.execute(
                    """
                    UPDATE users
                    SET first_exercise_sent = 1, first_exercise_due_utc = NULL
                    WHERE telegram_id = ?
                    """,
                    (user_id,),
                )
            await db.commit()

    async def ensure_course_start_date_today(self, user_id: int, local_date_iso: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE users SET course_start_date = COALESCE(course_start_date, ?)
                WHERE telegram_id = ?
                """,
                (local_date_iso, user_id),
            )
            await db.commit()

    async def wipe_all_user_data(self, telegram_id: int) -> None:
        """Удаляет строку пользователя и все связанные данные (задания, слова, настройки, аналитика)."""
        uid = telegram_id
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM user_scheduled_tasks WHERE user_id = ?", (uid,))
            await db.execute("DELETE FROM user_words WHERE user_id = ?", (uid,))
            await db.execute("DELETE FROM user_settings WHERE user_id = ?", (uid,))
            await db.execute("DELETE FROM user_t1_progress WHERE user_id = ?", (uid,))
            await db.execute("DELETE FROM user_profile WHERE user_id = ?", (uid,))
            await db.execute("UPDATE word_dict SET added_by = NULL WHERE added_by = ?", (uid,))
            await db.execute("DELETE FROM users WHERE telegram_id = ?", (uid,))
            await db.commit()

    async def reset_course_state(self, telegram_id: int) -> None:
        """
        Временный reset для тестирования: очищает курс/задания, но сохраняет профиль пользователя.
        Сбрасывает прогресс, scheduled tasks, слова, аналитику и курс-мета в users.
        """
        uid = telegram_id
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM user_scheduled_tasks WHERE user_id = ?", (uid,))
            await db.execute("DELETE FROM user_words WHERE user_id = ?", (uid,))
            await db.execute("DELETE FROM user_t1_progress WHERE user_id = ?", (uid,))
            await db.execute(
                """
                UPDATE users
                SET course_day = 1,
                    week_num = 1,
                    first_exercise_sent = 0,
                    first_exercise_due_utc = NULL,
                    course_start_date = NULL
                WHERE telegram_id = ?
                """,
                (uid,),
            )
            await db.execute(
                """
                UPDATE user_profile
                SET dominant_cluster = NULL,
                    trend = NULL,
                    responsive_tasks = '[]',
                    avoidant_tasks = '[]',
                    last_analysis = NULL,
                    last_week_key = NULL
                WHERE user_id = ?
                """,
                (uid,),
            )
            await db.commit()

    async def ensure_t1_progress(self, user_id: int, age_group: str) -> None:
        from app.services.t1_runtime import starting_morning_seconds

        start_sec = starting_morning_seconds(age_group)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO user_t1_progress (user_id, morning_seconds)
                VALUES (?, ?)
                """,
                (user_id, start_sec),
            )
            await db.commit()

    async def get_t1_progress(self, user_id: int) -> dict | None:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT morning_seconds, last_morning_date FROM user_t1_progress WHERE user_id = ?",
                (user_id,),
            )
            row = await cursor.fetchone()
            if not row:
                return None
            return {"morning_seconds": int(row[0]), "last_morning_date": row[1]}

    async def update_t1_morning_after_session(self, user_id: int, session_date: str, reached_target: bool) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            if reached_target:
                await db.execute(
                    """
                    UPDATE user_t1_progress
                    SET morning_seconds = morning_seconds + 2, last_morning_date = ?
                    WHERE user_id = ?
                    """,
                    (session_date, user_id),
                )
            else:
                await db.execute(
                    """
                    UPDATE user_t1_progress SET last_morning_date = ? WHERE user_id = ?
                    """,
                    (session_date, user_id),
                )
            await db.commit()

    async def complete_t1_morning_task(
        self, row_id: int, actual_sec: int, target_sec: int, completed_at: str, word: str | None = None
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE user_scheduled_tasks
                SET completed = 1, skipped = 0, points = 1,
                    actual_duration_sec = ?, target_duration_sec = ?, completed_at = ?, word = ?
                WHERE id = ?
                """,
                (actual_sec, target_sec, completed_at, word, row_id),
            )
            await db.commit()

    async def complete_t1_evening_task(self, row_id: int, word: str, completed_at: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE user_scheduled_tasks
                SET completed = 1, skipped = 0, points = 1, word = ?, completed_at = ?
                WHERE id = ?
                """,
                (word, completed_at, row_id),
            )
            await db.commit()

    async def upsert_t1_daily_task(
        self,
        user_id: int,
        task_type: str,
        task_date: str,
        window_start: str,
        window_end: str,
        task_id: int = 0,
        evening_rhythm: str | None = None,
        evening_cycles: int | None = None,
    ) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO user_scheduled_tasks
                (user_id, task_type, task_id, task_date, slot, window_start, window_end,
                 evening_rhythm, evening_cycles, points)
                VALUES (?, ?, ?, ?, '', ?, ?, ?, ?, 0)
                ON CONFLICT(user_id, task_type, task_date, slot) DO UPDATE SET
                    window_start = excluded.window_start,
                    window_end = excluded.window_end,
                    task_id = excluded.task_id,
                    evening_rhythm = COALESCE(excluded.evening_rhythm, user_scheduled_tasks.evening_rhythm),
                    evening_cycles = COALESCE(excluded.evening_cycles, user_scheduled_tasks.evening_cycles)
                """,
                (
                    user_id,
                    task_type,
                    task_id,
                    task_date,
                    window_start,
                    window_end,
                    evening_rhythm,
                    evening_cycles,
                ),
            )
            await db.commit()
            cursor = await db.execute(
                """
                SELECT id FROM user_scheduled_tasks
                WHERE user_id = ? AND task_type = ? AND task_date = ? AND COALESCE(slot, '') = ''
                """,
                (user_id, task_type, task_date),
            )
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    async def get_t1_row_meta(self, user_id: int, task_type: str, task_date: str) -> dict | None:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT id, sent_at, completed, skipped, evening_rhythm, evening_cycles
                FROM user_scheduled_tasks
                WHERE user_id = ? AND task_type = ? AND task_date = ? AND COALESCE(slot, '') = ''
                """,
                (user_id, task_type, task_date),
            )
            row = await cursor.fetchone()
            if not row:
                return None
            return {
                "id": int(row[0]),
                "sent_at": row[1],
                "completed": bool(row[2]),
                "skipped": bool(row[3]),
                "evening_rhythm": row[4],
                "evening_cycles": int(row[5]) if row[5] is not None else None,
            }

    async def upsert_t2_daily_task(
        self,
        user_id: int,
        task_date: str,
        window_start: str,
        window_end: str,
        task_id: int,
        t2_subtype: str,
    ) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO user_scheduled_tasks
                (user_id, task_type, task_id, task_date, slot, window_start, window_end,
                 t2_subtype, points)
                VALUES (?, 'T2', ?, ?, '', ?, ?, ?, 0)
                ON CONFLICT(user_id, task_type, task_date, slot) DO UPDATE SET
                    window_start = excluded.window_start,
                    window_end = excluded.window_end,
                    task_id = excluded.task_id,
                    t2_subtype = excluded.t2_subtype
                """,
                (user_id, task_id, task_date, window_start, window_end, t2_subtype),
            )
            await db.commit()
            cursor = await db.execute(
                """
                SELECT id FROM user_scheduled_tasks
                WHERE user_id = ? AND task_type = 'T2' AND task_date = ? AND COALESCE(slot, '') = ''
                """,
                (user_id, task_date),
            )
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    async def get_t2_row_meta(self, user_id: int, task_date: str) -> dict | None:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT id, sent_at, completed, skipped, t2_subtype, task_id
                FROM user_scheduled_tasks
                WHERE user_id = ? AND task_type = 'T2' AND task_date = ? AND COALESCE(slot, '') = ''
                """,
                (user_id, task_date),
            )
            row = await cursor.fetchone()
            if not row:
                return None
            return {
                "id": int(row[0]),
                "sent_at": row[1],
                "completed": bool(row[2]),
                "skipped": bool(row[3]),
                "t2_subtype": row[4],
                "task_id": int(row[5]) if row[5] is not None else 0,
            }

    async def complete_t2_task(
        self,
        row_id: int,
        word: str,
        response: str | None,
        completed_at: str,
        actual_duration_sec: int | None = None,
        selected_image_id: int | None = None,
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE user_scheduled_tasks
                SET completed = 1, skipped = 0, points = 2, word = ?, response = ?,
                    completed_at = ?, actual_duration_sec = COALESCE(?, actual_duration_sec),
                    t2_selected_image_id = ?
                WHERE id = ?
                """,
                (word, response, completed_at, actual_duration_sec, selected_image_id, row_id),
            )
            await db.commit()

    async def list_users_active_course(self) -> list[tuple[int, str, str, str | None]]:
        """telegram_id, timezone, age_group, course_start_date (may be NULL)."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT telegram_id,
                       COALESCE(timezone, 'Europe/Moscow'),
                       COALESCE(age_group, '25-35'),
                       course_start_date
                FROM users
                WHERE first_exercise_sent = 1
                  AND full_name IS NOT NULL AND birth_date IS NOT NULL AND location IS NOT NULL
                  AND age_group IS NOT NULL
                """
            )
            return [(int(r[0]), r[1], r[2], r[3]) for r in await cursor.fetchall()]

    async def get_user_row_for_t1(self, user_id: int) -> dict | None:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT age_group, timezone, full_name, birth_date,
                       first_exercise_sent, course_start_date, week_num
                FROM users WHERE telegram_id = ?
                """,
                (user_id,),
            )
            row = await cursor.fetchone()
            if not row:
                return None
            return {
                "age_group": row[0],
                "timezone": row[1],
                "full_name": row[2],
                "birth_date": row[3],
                "first_exercise_sent": int(row[4] or 0),
                "course_start_date": row[5],
                "week_num": int(row[6] or 1),
            }

    async def seed_word_dictionary(self, rows: list[tuple[str, str]]) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.executemany(
                """
                INSERT OR IGNORE INTO word_dict (word, cluster, added_by)
                VALUES (?, ?, 0)
                """,
                rows,
            )
            await db.commit()

    async def add_word_to_dictionary(self, word: str, cluster: str, added_by: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO word_dict (word, cluster, added_by)
                VALUES (?, ?, ?)
                ON CONFLICT(word) DO UPDATE SET cluster = excluded.cluster, added_by = excluded.added_by
                """,
                (normalize_word(word), cluster, added_by),
            )
            await db.commit()

    async def get_cluster_for_word(self, word: str) -> str | None:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT cluster FROM word_dict WHERE word = ?", (normalize_word(word),))
            row = await cursor.fetchone()
            return row[0] if row else None

    async def save_user_word(
        self,
        user_id: int,
        word: str,
        task_type: str,
        task_id: str,
        age_group: str | None = None,
    ) -> int:
        cluster = await self.get_cluster_for_word(word)
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO user_words (user_id, word, task_type, task_id, age_group, is_in_dict)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    normalize_word(word),
                    task_type,
                    task_id,
                    age_group,
                    1 if cluster else 0,
                ),
            )
            await db.commit()
            return int(cursor.lastrowid)

    async def weekly_word_count(self, user_id: int, now_utc: datetime | None = None) -> int:
        now_utc = now_utc or datetime.utcnow()
        start = (now_utc - timedelta(days=7)).isoformat(sep=" ")
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM user_words WHERE user_id = ? AND created_at >= ?",
                (user_id, start),
            )
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    async def get_user_weekly_word_records(self, user_id: int, week_shift: int = 0) -> list[tuple]:
        now_utc = datetime.utcnow() - timedelta(days=7 * week_shift)
        start = now_utc - timedelta(days=7)
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT uw.word, uw.task_type, uw.task_id, uw.is_in_dict, wd.cluster
                FROM user_words uw
                LEFT JOIN word_dict wd ON wd.word = uw.word
                WHERE uw.user_id = ? AND uw.created_at >= ? AND uw.created_at < ?
                """,
                (user_id, start.isoformat(sep=" "), now_utc.isoformat(sep=" ")),
            )
            return await cursor.fetchall()

    async def get_all_user_ids(self) -> list[int]:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT telegram_id FROM users")
            return [int(row[0]) for row in await cursor.fetchall()]

    async def update_user_profile_analysis(
        self,
        user_id: int,
        dominant_cluster: str | None,
        trend: str,
        responsive_tasks: list[str],
        avoidant_tasks: list[str],
        last_analysis: str,
    ) -> None:
        await self.ensure_analysis_profile(user_id)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE user_profile
                SET dominant_cluster = ?, trend = ?, responsive_tasks = ?, avoidant_tasks = ?, last_analysis = ?
                WHERE user_id = ?
                """,
                (
                    dominant_cluster,
                    trend,
                    json.dumps(responsive_tasks, ensure_ascii=False),
                    json.dumps(avoidant_tasks, ensure_ascii=False),
                    last_analysis,
                    user_id,
                ),
            )
            await db.commit()

    async def get_analysis_profile(self, user_id: int) -> dict:
        await self.ensure_analysis_profile(user_id)
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT dominant_cluster, trend, responsive_tasks, avoidant_tasks, reports_enabled, last_analysis, last_week_key, last_month_key
                FROM user_profile WHERE user_id = ?
                """,
                (user_id,),
            )
            row = await cursor.fetchone()
        return {
            "dominant_cluster": row[0],
            "trend": row[1],
            "responsive_tasks": json.loads(row[2] or "[]"),
            "avoidant_tasks": json.loads(row[3] or "[]"),
            "reports_enabled": bool(row[4]),
            "last_analysis": row[5],
            "last_week_key": row[6],
            "last_month_key": row[7],
        }

    async def set_reports_enabled(self, user_id: int, enabled: bool) -> None:
        await self.ensure_analysis_profile(user_id)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE user_profile SET reports_enabled = ? WHERE user_id = ?",
                (1 if enabled else 0, user_id),
            )
            await db.commit()

    async def set_last_week_key(self, user_id: int, week_key: str) -> None:
        await self.ensure_analysis_profile(user_id)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("UPDATE user_profile SET last_week_key = ? WHERE user_id = ?", (week_key, user_id))
            await db.commit()

    async def set_last_month_key(self, user_id: int, month_key: str) -> None:
        await self.ensure_analysis_profile(user_id)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("UPDATE user_profile SET last_month_key = ? WHERE user_id = ?", (month_key, user_id))
            await db.commit()

    async def month_points(self, user_id: int, month_start: date) -> int:
        """Сумма баллов за календарный месяц (по task_date)."""
        if month_start.month == 12:
            next_month = date(month_start.year + 1, 1, 1)
        else:
            next_month = date(month_start.year, month_start.month + 1, 1)
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT COALESCE(SUM(points), 0) FROM user_scheduled_tasks
                WHERE user_id = ? AND task_date >= ? AND task_date < ?
                """,
                (user_id, month_start.isoformat(), next_month.isoformat()),
            )
            return int((await cursor.fetchone())[0] or 0)

    async def recalc_word_dictionary_links(self) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                UPDATE user_words
                SET is_in_dict = CASE
                    WHEN EXISTS (SELECT 1 FROM word_dict wd WHERE wd.word = user_words.word) THEN 1
                    ELSE 0
                END
                """
            )
            await db.commit()
            return cursor.rowcount

    async def stats_new_words_weekly(self) -> int:
        since = (datetime.utcnow() - timedelta(days=7)).isoformat(sep=" ")
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM user_words WHERE is_in_dict = 0 AND created_at >= ?",
                (since,),
            )
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    async def review_unknown_words(self) -> list[tuple[str, int]]:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT word, COUNT(*) as freq
                FROM user_words
                WHERE is_in_dict = 0
                GROUP BY word
                ORDER BY freq DESC, word ASC
                LIMIT 50
                """
            )
            return [(row[0], int(row[1])) for row in await cursor.fetchall()]

    async def update_user_v2(
        self,
        user_id: int,
        age_group: str | None = None,
        zodiac_sign: str | None = None,
        element: str | None = None,
        week_num: int | None = None,
    ) -> None:
        parts = []
        vals: list = []
        if age_group is not None:
            parts.append("age_group = ?")
            vals.append(age_group)
        if zodiac_sign is not None:
            parts.append("zodiac_sign = ?")
            vals.append(zodiac_sign)
        if element is not None:
            parts.append("element = ?")
            vals.append(element)
        if week_num is not None:
            parts.append("week_num = ?")
            vals.append(week_num)
        if not parts:
            return
        vals.append(user_id)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                f"UPDATE users SET {', '.join(parts)} WHERE telegram_id = ?",
                vals,
            )
            await db.commit()

    async def get_user_v2(self, user_id: int) -> dict | None:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT age_group, zodiac_sign, element, week_num, course_day, birth_date, timezone
                FROM users WHERE telegram_id = ?
                """,
                (user_id,),
            )
            row = await cursor.fetchone()
            if not row:
                return None
            return {
                "age_group": row[0],
                "zodiac_sign": row[1],
                "element": row[2],
                "week_num": int(row[3] or 1),
                "course_day": int(row[4] or 1),
                "birth_date": row[5],
                "timezone": row[6] or "Europe/Moscow",
            }

    async def ensure_user_settings(self, user_id: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO user_settings (user_id) VALUES (?)",
                (user_id,),
            )
            await db.commit()

    async def get_user_settings(self, user_id: int) -> dict:
        await self.ensure_user_settings(user_id)
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT t2_time, t3_time, t4_time, custom_schedule FROM user_settings WHERE user_id = ?",
                (user_id,),
            )
            row = await cursor.fetchone()
        return {
            "t2_time": row[0],
            "t3_time": row[1],
            "t4_time": row[2],
            "custom_schedule": row[3],
        }

    async def set_user_settings(
        self,
        user_id: int,
        t2_time: str | None = None,
        t3_time: str | None = None,
        t4_time: str | None = None,
        custom_schedule: str | None = None,
    ) -> None:
        await self.ensure_user_settings(user_id)
        parts = []
        vals: list = []
        if t2_time is not None:
            parts.append("t2_time = ?")
            vals.append(t2_time)
        if t3_time is not None:
            parts.append("t3_time = ?")
            vals.append(t3_time)
        if t4_time is not None:
            parts.append("t4_time = ?")
            vals.append(t4_time)
        if custom_schedule is not None:
            parts.append("custom_schedule = ?")
            vals.append(custom_schedule)
        if not parts:
            return
        vals.append(user_id)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                f"UPDATE user_settings SET {', '.join(parts)} WHERE user_id = ?",
                vals,
            )
            await db.commit()

    async def upsert_scheduled_task(
        self,
        user_id: int,
        task_type: str,
        task_date: str,
        slot: str | None,
        task_id: int | None,
        window_start: str,
        window_end: str,
        sent_at: str | None = None,
    ) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO user_scheduled_tasks
                (user_id, task_type, task_id, task_date, slot, window_start, window_end, sent_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, task_type, task_date, slot) DO UPDATE SET
                    task_id = excluded.task_id,
                    window_start = excluded.window_start,
                    window_end = excluded.window_end,
                    sent_at = COALESCE(excluded.sent_at, user_scheduled_tasks.sent_at)
                """,
                (user_id, task_type, task_id, task_date, slot or "", window_start, window_end, sent_at),
            )
            await db.commit()
            cursor = await db.execute(
                """
                SELECT id FROM user_scheduled_tasks
                WHERE user_id = ? AND task_type = ? AND task_date = ? AND slot = ?
                """,
                (user_id, task_type, task_date, slot or ""),
            )
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    async def get_scheduled_task_by_id(self, row_id: int) -> dict | None:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT id, user_id, task_type, task_id, task_date, slot,
                       window_start, window_end, completed, skipped, word, points,
                       actual_duration_sec, target_duration_sec, evening_rhythm, evening_cycles,
                       response, t2_subtype, t2_selected_image_id
                FROM user_scheduled_tasks WHERE id = ?
                """,
                (row_id,),
            )
            row = await cursor.fetchone()
            if not row:
                return None
            return {
                "id": row[0],
                "user_id": row[1],
                "task_type": row[2],
                "task_id": row[3],
                "task_date": row[4],
                "slot": row[5],
                "window_start": row[6],
                "window_end": row[7],
                "completed": bool(row[8]),
                "skipped": bool(row[9]),
                "word": row[10],
                "points": int(row[11] or 0),
                "actual_duration_sec": row[12],
                "target_duration_sec": row[13],
                "evening_rhythm": row[14],
                "evening_cycles": row[15],
                "response": row[16],
                "t2_subtype": row[17],
                "t2_selected_image_id": row[18],
            }

    async def scheduled_lifetime_counts(self, user_id: int) -> tuple[int, int, int]:
        """Возвращает (выполнено, пропущено, всего строк) по user_scheduled_tasks."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT
                    COUNT(*),
                    COALESCE(SUM(CASE WHEN completed = 1 THEN 1 ELSE 0 END), 0),
                    COALESCE(SUM(CASE WHEN skipped = 1 THEN 1 ELSE 0 END), 0)
                FROM user_scheduled_tasks
                WHERE user_id = ?
                """,
                (user_id,),
            )
            row = await cursor.fetchone()
            if not row:
                return 0, 0, 0
            total = int(row[0] or 0)
            done = int(row[1] or 0)
            skipped = int(row[2] or 0)
            return done, skipped, total

    async def list_scheduled_for_date(self, user_id: int, task_date: str) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT id, task_type, task_id, slot, window_start, window_end, sent_at, completed, skipped,
                       COALESCE(points, 0), t2_subtype
                FROM user_scheduled_tasks
                WHERE user_id = ? AND task_date = ?
                ORDER BY window_start
                """,
                (user_id, task_date),
            )
            rows = await cursor.fetchall()
        return [
            {
                "id": r[0],
                "task_type": r[1],
                "task_id": r[2],
                "slot": r[3],
                "window_start": r[4],
                "window_end": r[5],
                "sent_at": r[6],
                "completed": bool(r[7]),
                "skipped": bool(r[8]),
                "points": int(r[9] or 0),
                "t2_subtype": r[10],
            }
            for r in rows
        ]

    async def complete_scheduled_task(
        self,
        row_id: int,
        points: int,
        word: str | None,
        completed_at: str,
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE user_scheduled_tasks
                SET completed = 1, skipped = 0, points = ?, word = ?, completed_at = ?
                WHERE id = ?
                """,
                (points, word, completed_at, row_id),
            )
            await db.commit()

    async def skip_scheduled_task(self, row_id: int, completed_at: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE user_scheduled_tasks
                SET skipped = 1, completed = 0, points = 0, completed_at = ?
                WHERE id = ?
                """,
                (completed_at, row_id),
            )
            await db.commit()

    async def mark_scheduled_sent(self, row_id: int, sent_at: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE user_scheduled_tasks SET sent_at = ? WHERE id = ?",
                (sent_at, row_id),
            )
            await db.commit()

    async def expire_pending_tasks(self, user_id: int, now_iso: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE user_scheduled_tasks
                SET skipped = 1, completed = 0, points = 0, completed_at = ?
                WHERE user_id = ? AND completed = 0 AND skipped = 0
                  AND window_end < ? AND sent_at IS NOT NULL
                """,
                (now_iso, user_id, now_iso),
            )
            await db.commit()

    async def t1_morning_completed_today(self, user_id: int, task_date: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT completed FROM user_scheduled_tasks
                WHERE user_id = ? AND task_type = 'T1_morning' AND task_date = ?
                """,
                (user_id, task_date),
            )
            row = await cursor.fetchone()
            return bool(row and row[0])

    async def weekly_points_and_max(self, user_id: int, week_start: date) -> tuple[int, int]:
        week_end = week_start + timedelta(days=7)
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT COALESCE(SUM(points), 0) FROM user_scheduled_tasks
                WHERE user_id = ? AND task_date >= ? AND task_date < ?
                """,
                (user_id, week_start.isoformat(), week_end.isoformat()),
            )
            earned = int((await cursor.fetchone())[0])
        max_points = await self._max_weekly_points_for_user(user_id, week_start)
        return earned, max_points

    async def _max_weekly_points_for_user(self, user_id: int, week_start: date) -> int:
        """Сумма возможных баллов за неделю по расписанию (упрощённо: 7 дней * типичный набор)."""
        v2 = await self.get_user_v2(user_id)
        if not v2 or not v2.get("age_group"):
            return 7 * (1 + 1 + 2 + 3 + 4)
        from app.services.schedule_loader import get_schedule

        sched = get_schedule(v2["age_group"])
        day = v2.get("course_day", 1)
        total = 0
        for d in range(7):
            cd = min(30, day + d)
            idx = cd - 1
            total += 2
            t2 = next((x for x in sched.get("t2_assignments", []) if x["day"] == cd), None)
            if t2:
                total += 2
            t3 = next((x for x in sched.get("t3_assignments", []) if x["day"] == cd), None)
            if t3:
                total += 3
            t4_list = sched.get("t4_assignments", [])
            if idx < len(t4_list) and t4_list[idx]:
                total += 4
        return total

    async def advance_week_if_threshold(self, user_id: int, week_start: date) -> bool:
        earned, max_pts = await self.weekly_points_and_max(user_id, week_start)
        if max_pts <= 0:
            return False
        if earned >= 0.8 * max_pts:
            v2 = await self.get_user_v2(user_id)
            w = int(v2["week_num"] or 1) if v2 else 1
            if w < 4:
                await self.update_user_v2(user_id, week_num=w + 1)
                return True
        return False

    # --- Legacy V2 helpers (kept for compatibility, do not use directly) ---

    async def update_user_v2_legacy(
        self,
        telegram_id: int,
        age_group: str,
        zodiac_sign: str,
        element: str,
        week_num: int = 1,
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE users SET age_group = ?, zodiac_sign = ?, element = ?, week_num = ?
                WHERE telegram_id = ?
                """,
                (age_group, zodiac_sign, element, week_num, telegram_id),
            )
            await db.commit()

    async def get_user_v2_legacy(self, telegram_id: int) -> dict | None:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT age_group, zodiac_sign, element, week_num, timezone
                FROM users WHERE telegram_id = ?
                """,
                (telegram_id,),
            )
            row = await cursor.fetchone()
            if not row:
                return None
            return {
                "age_group": row[0] or "25-35",
                "zodiac_sign": row[1] or "",
                "element": row[2] or "earth",
                "week_num": int(row[3] or 1),
                "timezone": row[4] or "Europe/Moscow",
            }

    async def get_users_for_scheduler(self) -> list[tuple[int, str, str]]:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT telegram_id, COALESCE(age_group, '25-35'), COALESCE(timezone, 'Europe/Moscow')
                FROM users
                WHERE full_name IS NOT NULL AND birth_date IS NOT NULL AND location IS NOT NULL
                """
            )
            return [(int(r[0]), r[1], r[2]) for r in await cursor.fetchall()]

    async def insert_scheduled_task(
        self,
        user_id: int,
        task_type: str,
        task_id: int | None,
        task_date: str,
        slot: str | None,
        window_start: str,
        window_end: str,
        points: int,
    ) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO user_scheduled_tasks
                (user_id, task_type, task_id, task_date, slot, window_start, window_end, points)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, task_type, task_id, task_date, slot, window_start, window_end, points),
            )
            await db.commit()
            cursor = await db.execute(
                "SELECT id FROM user_scheduled_tasks WHERE user_id=? AND task_type=? AND task_date=? AND COALESCE(slot,'')=?",
                (user_id, task_type, task_date, slot or ""),
            )
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    async def mark_scheduled_completed(
        self, user_id: int, task_type: str, task_date: str, slot: str | None, word: str | None = None
    ) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                UPDATE user_scheduled_tasks
                SET completed = 1, word = ?, completed_at = datetime('now')
                WHERE user_id = ? AND task_type = ? AND task_date = ? AND COALESCE(slot,'') = COALESCE(?,'')
                """,
                (word, user_id, task_type, task_date, slot),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def mark_scheduled_skipped(
        self, user_id: int, task_type: str, task_date: str, slot: str | None
    ) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                UPDATE user_scheduled_tasks
                SET skipped = 1, points = 0, completed_at = datetime('now')
                WHERE user_id = ? AND task_type = ? AND task_date = ? AND COALESCE(slot,'') = COALESCE(?,'')
                """,
                (user_id, task_type, task_date, slot),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def get_scheduled_task(
        self, user_id: int, task_type: str, task_date: str, slot: str | None
    ) -> dict | None:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT id, task_id, completed, skipped, points
                FROM user_scheduled_tasks
                WHERE user_id = ? AND task_type = ? AND task_date = ? AND COALESCE(slot,'') = COALESCE(?,'')
                """,
                (user_id, task_type, task_date, slot or ""),
            )
            row = await cursor.fetchone()
            if not row:
                return None
            return {"id": row[0], "task_id": row[1], "completed": bool(row[2]), "skipped": bool(row[3]), "points": row[4]}

    async def did_morning_t1_today(self, user_id: int, task_date: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT 1 FROM user_scheduled_tasks
                WHERE user_id = ? AND task_type = 'T1_morning' AND task_date = ? AND completed = 1
                LIMIT 1
                """,
                (user_id, task_date),
            )
            return await cursor.fetchone() is not None

    async def get_weekly_points(self, user_id: int, week_start: str, week_end: str) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT COALESCE(SUM(points), 0) FROM user_scheduled_tasks
                WHERE user_id = ? AND task_date >= ? AND task_date <= ? AND completed = 1
                """,
                (user_id, week_start, week_end),
            )
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    async def get_weekly_possible_points(self, user_id: int, week_start: str, week_end: str) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT COALESCE(SUM(
                    CASE task_type
                        WHEN 'T1_morning' THEN 1 WHEN 'T1_evening' THEN 1
                        WHEN 'T2' THEN 2 WHEN 'T3' THEN 3 WHEN 'T4' THEN 4
                        ELSE 0
                    END
                ), 0) FROM user_scheduled_tasks
                WHERE user_id = ? AND task_date >= ? AND task_date <= ?
                """,
                (user_id, week_start, week_end),
            )
            row = await cursor.fetchone()
            return int(row[0]) if row else 1

    async def increment_week(self, user_id: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE users SET week_num = week_num + 1 WHERE telegram_id = ?",
                (user_id,),
            )
            await db.commit()

    async def get_user_settings_legacy(self, user_id: int) -> dict:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT t2_time, t3_time, t4_time FROM user_settings WHERE user_id = ?",
                (user_id,),
            )
            row = await cursor.fetchone()
            if not row:
                return {"t2_time": "12:00", "t3_time": "14:00-18:00", "t4_time": "10:00-20:00"}
            return {"t2_time": row[0] or "12:00", "t3_time": row[1] or "14:00-18:00", "t4_time": row[2] or "10:00-20:00"}

    async def set_user_settings_legacy(
        self, user_id: int, t2: str | None, t3: str | None, t4: str | None
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO user_settings (user_id, t2_time, t3_time, t4_time)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    t2_time = COALESCE(excluded.t2_time, t2_time),
                    t3_time = COALESCE(excluded.t3_time, t3_time),
                    t4_time = COALESCE(excluded.t4_time, t4_time)
                """,
                (user_id, t2, t3, t4),
            )
            await db.commit()
