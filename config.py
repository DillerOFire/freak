import os
from dotenv import load_dotenv

load_dotenv(override=True)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemini-flash-2.5")
OPENROUTER_PONDER_MODEL = os.getenv(
    "OPENROUTER_PONDER_MODEL", "deepseek/deepseek-v4-flash"
)
REACTION_CHANCE = 0.05

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN not set in .env")
if not OPENROUTER_API_KEY:
    raise ValueError("OPENROUTER_API_KEY not set in .env")

OPENROUTER_VISION_MODEL = os.getenv(
    "OPENROUTER_VISION_MODEL", "google/gemini-flash-2.5"
)

# OpenRouter app attribution headers
OPENROUTER_REFERER = os.getenv("OPENROUTER_REFERER", "https://github.com/DillerOFire/freak")
OPENROUTER_TITLE = os.getenv("OPENROUTER_TITLE", "Freaky tg bot")

# Admin ID configuration
# Default to the user provided ID if not set in env
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))
TELEMETRY_DASHBOARD_ENABLED = os.getenv("TELEMETRY_DASHBOARD_ENABLED", "true").lower() not in {"0", "false", "no"}
TELEMETRY_DASHBOARD_HOST = os.getenv("TELEMETRY_DASHBOARD_HOST", "127.0.0.1")
TELEMETRY_DASHBOARD_PORT = int(os.getenv("TELEMETRY_DASHBOARD_PORT", "8765"))
TELEMETRY_DASHBOARD_TOKEN = os.getenv("TELEMETRY_DASHBOARD_TOKEN")

COOKIES_DIR = os.path.join(os.path.dirname(__file__), "cookies")
if not os.path.exists(COOKIES_DIR):
    os.makedirs(COOKIES_DIR)
