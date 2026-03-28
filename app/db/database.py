import aiosqlite


CREATE_USERS = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    username TEXT NOT NULL,
    course_day INTEGER NOT NULL DEFAULT 1,
    full_name TEXT NULL,
    birth_date TEXT NULL,
    location TEXT NULL,
    timezone TEXT NULL
);
"""

CREATE_USER_WORDS = """
CREATE TABLE IF NOT EXISTS user_words (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    word TEXT NOT NULL,
    task_type TEXT,
    task_id TEXT,
    age_group TEXT,
    is_in_dict INTEGER DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""

CREATE_USER_PROFILE = """
CREATE TABLE IF NOT EXISTS user_profile (
    user_id INTEGER PRIMARY KEY,
    dominant_cluster TEXT,
    trend TEXT,
    responsive_tasks TEXT,
    avoidant_tasks TEXT,
    reports_enabled INTEGER DEFAULT 1,
    last_analysis DATETIME,
    last_week_key TEXT,
    last_month_key TEXT
);
"""

CREATE_WORD_DICT = """
CREATE TABLE IF NOT EXISTS word_dict (
    word TEXT PRIMARY KEY,
    cluster TEXT NOT NULL,
    added_by INTEGER,
    added_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""

CREATE_USER_SETTINGS = """
CREATE TABLE IF NOT EXISTS user_settings (
    user_id INTEGER PRIMARY KEY,
    t2_time TEXT,
    t3_time TEXT,
    t4_time TEXT,
    custom_schedule TEXT,
    FOREIGN KEY (user_id) REFERENCES users(telegram_id)
);
"""

CREATE_USER_SCHEDULED_TASKS = """
CREATE TABLE IF NOT EXISTS user_scheduled_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    task_type TEXT NOT NULL,
    task_id INTEGER,
    task_date DATE NOT NULL,
    slot TEXT,
    window_start DATETIME,
    window_end DATETIME,
    completed INTEGER DEFAULT 0,
    skipped INTEGER DEFAULT 0,
    word TEXT,
    points INTEGER DEFAULT 0,
    completed_at DATETIME,
    sent_at DATETIME,
    UNIQUE(user_id, task_type, task_date, slot),
    FOREIGN KEY (user_id) REFERENCES users(telegram_id)
);
"""

CREATE_USER_T1_PROGRESS = """
CREATE TABLE IF NOT EXISTS user_t1_progress (
    user_id INTEGER PRIMARY KEY,
    morning_seconds INTEGER NOT NULL DEFAULT 20,
    last_morning_date TEXT,
    FOREIGN KEY(user_id) REFERENCES users(telegram_id)
);
"""


async def init_db(db_path: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(CREATE_USERS)
        await db.execute(CREATE_USER_WORDS)
        await db.execute(CREATE_USER_PROFILE)
        await db.execute(CREATE_WORD_DICT)
        await db.execute(CREATE_USER_SETTINGS)
        await db.execute(CREATE_USER_SCHEDULED_TASKS)
        await db.execute(CREATE_USER_T1_PROGRESS)
        await _ensure_user_profile_columns(db)
        await _ensure_users_v2_columns(db)
        await _ensure_scheduled_tasks_t1_columns(db)
        await _ensure_analysis_profile_columns(db)
        await db.commit()


async def _ensure_user_profile_columns(db: aiosqlite.Connection) -> None:
    cursor = await db.execute("PRAGMA table_info(users)")
    columns = {row[1] for row in await cursor.fetchall()}
    if "full_name" not in columns:
        await db.execute("ALTER TABLE users ADD COLUMN full_name TEXT NULL")
    if "birth_date" not in columns:
        await db.execute("ALTER TABLE users ADD COLUMN birth_date TEXT NULL")
    if "location" not in columns:
        await db.execute("ALTER TABLE users ADD COLUMN location TEXT NULL")
    if "timezone" not in columns:
        await db.execute("ALTER TABLE users ADD COLUMN timezone TEXT NULL")


async def _ensure_users_v2_columns(db: aiosqlite.Connection) -> None:
    cursor = await db.execute("PRAGMA table_info(users)")
    columns = {row[1] for row in await cursor.fetchall()}
    if "age_group" not in columns:
        await db.execute("ALTER TABLE users ADD COLUMN age_group TEXT NULL")
    if "zodiac_sign" not in columns:
        await db.execute("ALTER TABLE users ADD COLUMN zodiac_sign TEXT NULL")
    if "element" not in columns:
        await db.execute("ALTER TABLE users ADD COLUMN element TEXT NULL")
    if "week_num" not in columns:
        await db.execute("ALTER TABLE users ADD COLUMN week_num INTEGER DEFAULT 1")
    if "first_exercise_due_utc" not in columns:
        await db.execute("ALTER TABLE users ADD COLUMN first_exercise_due_utc TEXT NULL")
    if "first_exercise_sent" not in columns:
        await db.execute("ALTER TABLE users ADD COLUMN first_exercise_sent INTEGER NOT NULL DEFAULT 0")
    if "course_start_date" not in columns:
        await db.execute("ALTER TABLE users ADD COLUMN course_start_date TEXT NULL")


async def _ensure_scheduled_tasks_t1_columns(db: aiosqlite.Connection) -> None:
    cursor = await db.execute("PRAGMA table_info(user_scheduled_tasks)")
    columns = {row[1] for row in await cursor.fetchall()}
    if "actual_duration_sec" not in columns:
        await db.execute("ALTER TABLE user_scheduled_tasks ADD COLUMN actual_duration_sec INTEGER NULL")
    if "target_duration_sec" not in columns:
        await db.execute("ALTER TABLE user_scheduled_tasks ADD COLUMN target_duration_sec INTEGER NULL")
    if "evening_rhythm" not in columns:
        await db.execute("ALTER TABLE user_scheduled_tasks ADD COLUMN evening_rhythm TEXT NULL")
    if "evening_cycles" not in columns:
        await db.execute("ALTER TABLE user_scheduled_tasks ADD COLUMN evening_cycles INTEGER NULL")
    if "response" not in columns:
        await db.execute("ALTER TABLE user_scheduled_tasks ADD COLUMN response TEXT NULL")
    if "t2_subtype" not in columns:
        await db.execute("ALTER TABLE user_scheduled_tasks ADD COLUMN t2_subtype TEXT NULL")
    if "t2_selected_image_id" not in columns:
        await db.execute("ALTER TABLE user_scheduled_tasks ADD COLUMN t2_selected_image_id INTEGER NULL")


async def _ensure_analysis_profile_columns(db: aiosqlite.Connection) -> None:
    cursor = await db.execute("PRAGMA table_info(user_profile)")
    columns = {row[1] for row in await cursor.fetchall()}
    if "reports_enabled" not in columns:
        await db.execute("ALTER TABLE user_profile ADD COLUMN reports_enabled INTEGER DEFAULT 1")
    if "last_week_key" not in columns:
        await db.execute("ALTER TABLE user_profile ADD COLUMN last_week_key TEXT")
    if "last_month_key" not in columns:
        await db.execute("ALTER TABLE user_profile ADD COLUMN last_month_key TEXT")
