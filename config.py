import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemini-flash-1.5")

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN not set in .env")
if not OPENROUTER_API_KEY:
    raise ValueError("OPENROUTER_API_KEY not set in .env")

OPENROUTER_VISION_MODEL = os.getenv(
    "OPENROUTER_VISION_MODEL", "google/gemini-flash-1.5"
)
