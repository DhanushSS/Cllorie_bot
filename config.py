import os
import math
from datetime import time
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

NVIDIA_API_KEY = os.environ["NVIDIA_API_KEY"]
NVIDIA_MODEL = os.environ.get("NVIDIA_MODEL", "nvidia/nemotron-3.5-lightning-30b-a3b")
NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"

GOOGLE_SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
GOOGLE_SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
GOOGLE_SHEET_TAB = os.environ.get("GOOGLE_SHEET_TAB", "Logs")


# Optional: a free API Ninjas key (https://api-ninjas.com/register) unlocks real
# nutrition-database lookups for common/generic foods, which are consistent (same
# query -> same answer) unlike LLM estimates. If unset, the bot falls back to
# AI-only estimation for everything.
NUTRITION_API_KEY = os.environ.get("NUTRITION_API_KEY", "")
NUTRITION_API_URL = "https://api.api-ninjas.com/v1/nutrition"

# Flat multiplier applied to every logged calorie number, regardless of source
# (cache, database, or AI estimate). Protein is left untouched. Override via env
# var if you ever want a different factor without touching code.
CALORIE_MULTIPLIER = float(os.environ.get("CALORIE_MULTIPLIER", "1.3"))

NUTRITION_DB_MIN_RATIO = float(os.environ.get("NUTRITION_DB_MIN_RATIO", "0.6"))
ALLOWED_CHAT_ID = os.environ.get("ALLOWED_CHAT_ID", "").strip()
if not math.isfinite(CALORIE_MULTIPLIER) or CALORIE_MULTIPLIER <= 0:
    raise ValueError("CALORIE_MULTIPLIER must be a finite positive number")
if not 0 <= NUTRITION_DB_MIN_RATIO <= 1:
    raise ValueError("NUTRITION_DB_MIN_RATIO must be between 0 and 1")

TIMEZONE = ZoneInfo(os.environ.get("TIMEZONE", "Asia/Kolkata"))


def _parse_time(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m), tzinfo=TIMEZONE)


REMINDER_MORNING = _parse_time(os.environ.get("REMINDER_MORNING", "08:30"))
REMINDER_AFTERNOON = _parse_time(os.environ.get("REMINDER_AFTERNOON", "14:00"))
REMINDER_NIGHT = _parse_time(os.environ.get("REMINDER_NIGHT", "20:30"))

MEALS = ["morning", "afternoon", "night"]
MEAL_LABELS = {
    "morning": "Breakfast 🌅",
    "afternoon": "Lunch ☀️",
    "night": "Dinner 🌙",
}

DB_PATH = os.environ.get("DB_PATH", "bot_state.db")
