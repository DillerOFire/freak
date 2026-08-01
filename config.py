import os
import sys
from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent


def _default_env_file_path() -> Path:
    explicit = os.getenv("ENV_FILE", "").strip()
    if explicit:
        return Path(explicit)

    cookies_dir = os.getenv("COOKIES_DIR", "")
    if cookies_dir.startswith("/data") or os.getenv("RUN_MODE", "").strip().lower() == "docker":
        return Path("/data/.env")

    return PROJECT_ROOT / ".env"


def _load_env_files() -> None:
    managed_env = _default_env_file_path()
    project_env = PROJECT_ROOT / ".env"
    if project_env.exists() and project_env != managed_env:
        load_dotenv(project_env, override=False)
    if managed_env.exists():
        load_dotenv(managed_env, override=True)
    elif project_env.exists():
        load_dotenv(project_env, override=True)


_load_env_files()

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://openrouter.ai/api/v1")
LLM_PONDER_BASE_URL = os.getenv("LLM_PONDER_BASE_URL", LLM_BASE_URL)
LLM_VISION_BASE_URL = os.getenv("LLM_VISION_BASE_URL", LLM_BASE_URL)


def _default_ytdlp_package_dir() -> str:
    cookies_dir = os.getenv("COOKIES_DIR", "")
    if cookies_dir.startswith("/data"):
        return "/data/python-packages"
    return os.path.join(os.path.dirname(__file__), "data", "python-packages")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
LLM_API_KEY = os.getenv("LLM_API_KEY") or os.getenv("OPENROUTER_API_KEY")
LLM_MODEL = os.getenv("LLM_MODEL", "google/gemini-flash-2.5")
LLM_PONDER_MODEL = os.getenv("LLM_PONDER_MODEL", "deepseek/deepseek-v4-flash")


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return min(max(value, minimum), maximum)


LLM_PONDER_MAX_STEPS = _bounded_env_int("LLM_PONDER_MAX_STEPS", 10, 1, 20)

LLM_PROMPT_CACHE = os.getenv("LLM_PROMPT_CACHE", "true").lower() not in {"0", "false", "no"}
REACTION_CHANCE = 0.05

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN not set in .env")
if not LLM_API_KEY:
    raise ValueError("LLM_API_KEY not set in .env")

LLM_VISION_MODEL = os.getenv("LLM_VISION_MODEL", "google/gemini-flash-2.5")

# Optional attribution headers for OpenRouter (and compatible gateways).
LLM_REFERER = os.getenv("LLM_REFERER", "https://github.com/your-org/freak")
LLM_TITLE = os.getenv("LLM_TITLE", "Freak Telegram Bot")

ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))
TELEMETRY_DASHBOARD_ENABLED = os.getenv("TELEMETRY_DASHBOARD_ENABLED", "true").lower() not in {"0", "false", "no"}
TELEMETRY_DASHBOARD_HOST = os.getenv("TELEMETRY_DASHBOARD_HOST", "127.0.0.1")
TELEMETRY_DASHBOARD_PORT = int(os.getenv("TELEMETRY_DASHBOARD_PORT", "8765"))
TELEMETRY_DASHBOARD_TOKEN = os.getenv("TELEMETRY_DASHBOARD_TOKEN")

# Telegram Web App used for the admin settings editor.  Telegram only opens
# Web Apps over public HTTPS, so WEB_SETTINGS_URL should be the public URL of
# the reverse proxy in front of the local listener below.  Keep the listener
# on loopback unless a proxy/container network needs a different bind address.
WEB_SETTINGS_URL = os.getenv("WEB_SETTINGS_URL", "").strip()
WEB_SETTINGS_HOST = os.getenv("WEB_SETTINGS_HOST", "127.0.0.1")
WEB_SETTINGS_PORT = _bounded_env_int("WEB_SETTINGS_PORT", 8780, 1, 65535)
WEB_SETTINGS_INIT_DATA_MAX_AGE = _bounded_env_int(
    "WEB_SETTINGS_INIT_DATA_MAX_AGE", 3600, 60, 86400
)

# Firecrawl (optional): default search and page-to-markdown extractor used by
# the ponder agent. When unset, searches use DDGS and fetching skips this stage.
FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY", "").strip() or None
FIRECRAWL_API_URL = os.getenv("FIRECRAWL_API_URL", "https://api.firecrawl.dev").strip() or "https://api.firecrawl.dev"

COOKIES_DIR = os.getenv("COOKIES_DIR", os.path.join(os.path.dirname(__file__), "cookies"))
if not os.path.exists(COOKIES_DIR):
    os.makedirs(COOKIES_DIR)

# Writable overlay for in-container yt-dlp upgrades (venv may be root-owned in Docker).
YTDLP_PACKAGE_DIR = os.getenv("YTDLP_PACKAGE_DIR", _default_ytdlp_package_dir())
if os.path.isdir(YTDLP_PACKAGE_DIR) and YTDLP_PACKAGE_DIR not in sys.path:
    sys.path.insert(0, YTDLP_PACKAGE_DIR)
