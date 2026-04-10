from dataclasses import dataclass
import os

from dotenv import load_dotenv


load_dotenv()

print(f"[DEBUG] BOT_TOKEN from env: {os.getenv('BOT_TOKEN', 'NOT SET')}", flush=True)
print(f"[DEBUG] All env vars: {dict(os.environ)}", flush=True)


@dataclass(frozen=True)
class Settings:
    bot_token: str
    timezone: str
    admin_ids: set[int]
    api_port: int


def get_settings() -> Settings:
    raw_admin_ids = os.getenv("ADMIN_IDS", "")
    admin_ids = {int(value.strip()) for value in raw_admin_ids.split(",") if value.strip().isdigit()}
    return Settings(
        bot_token=os.getenv("BOT_TOKEN", "replace_with_real_token"),
        timezone=os.getenv("TIMEZONE", "Europe/Moscow"),
        admin_ids=admin_ids,
        api_port=int(os.getenv("API_PORT", "8080")),
    )
