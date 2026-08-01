"""Read/write bot .env settings with atomic, permission-safe updates."""

from __future__ import annotations

import logging
import os
import re
import stat
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values, load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Keys the bot may change via /bot_env or /set_env (admin DM only).
EDITABLE_ENV_KEYS: frozenset[str] = frozenset(
    {
        "TELEGRAM_BOT_TOKEN",
        "LLM_API_KEY",
        "ADMIN_ID",
        "LLM_BASE_URL",
        "LLM_MODEL",
        "LLM_PONDER_MODEL",
        "LLM_PONDER_MAX_STEPS",
        "LLM_VISION_MODEL",
        "LLM_PROMPT_CACHE",
        "LLM_PONDER_BASE_URL",
        "LLM_VISION_BASE_URL",
        "LLM_REFERER",
        "LLM_TITLE",
        "TELEMETRY_DASHBOARD_ENABLED",
        "TELEMETRY_DASHBOARD_HOST",
        "TELEMETRY_DASHBOARD_PORT",
        "TELEMETRY_DASHBOARD_TOKEN",
        "FIRECRAWL_API_KEY",
        "FIRECRAWL_API_URL",
        "WEB_SETTINGS_URL",
        "WEB_SETTINGS_HOST",
        "WEB_SETTINGS_PORT",
        "WEB_SETTINGS_INIT_DATA_MAX_AGE",
        "UV_EXECUTABLE",
    }
)

# Managed by Docker/orchestrator — never rewrite from inside the container.
PROTECTED_ENV_KEYS: frozenset[str] = frozenset(
    {
        "BOT_DB_PATH",
        "COOKIES_DIR",
        "YTDLP_PACKAGE_DIR",
        "RUN_MODE",
        "ENV_FILE",
    }
)

SECRET_ENV_KEYS: frozenset[str] = frozenset(
    {
        "TELEGRAM_BOT_TOKEN",
        "LLM_API_KEY",
        "TELEMETRY_DASHBOARD_TOKEN",
        "FIRECRAWL_API_KEY",
    }
)

RESTART_REQUIRED_KEYS: frozenset[str] = frozenset(
    {
        "TELEGRAM_BOT_TOKEN",
        "LLM_PONDER_BASE_URL",
        "LLM_VISION_BASE_URL",
        "ADMIN_ID",
        "TELEMETRY_DASHBOARD_HOST",
        "TELEMETRY_DASHBOARD_PORT",
        "TELEMETRY_DASHBOARD_ENABLED",
        "TELEMETRY_DASHBOARD_TOKEN",
        "WEB_SETTINGS_URL",
        "WEB_SETTINGS_HOST",
        "WEB_SETTINGS_PORT",
        "WEB_SETTINGS_INIT_DATA_MAX_AGE",
    }
)

_KEY_PATTERN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_BOOL_VALUES = frozenset({"0", "1", "false", "true", "no", "yes", "off", "on"})
MAX_ENV_VALUE_LENGTH = 4096


class EnvUpdateError(ValueError):
    """A user-facing validation failure while editing the managed env file."""


def _validate_int(value: str, *, key: str, minimum: int, maximum: int) -> None:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise EnvUpdateError(f"{key} must be an integer from {minimum} to {maximum}.") from exc
    if not minimum <= parsed <= maximum:
        raise EnvUpdateError(f"{key} must be an integer from {minimum} to {maximum}.")


def validate_env_value(key: str, value: str) -> tuple[str, str]:
    """Normalize and validate one user-editable environment value."""
    normalized_key = key.strip().upper()
    if normalized_key in PROTECTED_ENV_KEYS:
        raise EnvUpdateError(
            f"{normalized_key} is managed by the deployment and cannot be changed here."
        )
    if normalized_key not in EDITABLE_ENV_KEYS:
        raise EnvUpdateError(f"Unknown or non-editable key: {normalized_key}")
    if not isinstance(value, str):
        raise EnvUpdateError(f"{normalized_key} must be a text value.")
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        raise EnvUpdateError(f"{normalized_key} must be a non-empty single-line value.")
    if len(value) > MAX_ENV_VALUE_LENGTH:
        raise EnvUpdateError(
            f"{normalized_key} is too long (maximum {MAX_ENV_VALUE_LENGTH} characters)."
        )

    if normalized_key == "LLM_PONDER_MAX_STEPS":
        _validate_int(value, key=normalized_key, minimum=1, maximum=20)
    elif normalized_key == "ADMIN_ID":
        _validate_int(value, key=normalized_key, minimum=1, maximum=2**63 - 1)
    elif normalized_key in {"TELEMETRY_DASHBOARD_PORT", "WEB_SETTINGS_PORT"}:
        _validate_int(value, key=normalized_key, minimum=1, maximum=65535)
    elif normalized_key == "WEB_SETTINGS_INIT_DATA_MAX_AGE":
        _validate_int(value, key=normalized_key, minimum=60, maximum=86400)
    elif normalized_key == "WEB_SETTINGS_URL":
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.netloc:
            raise EnvUpdateError(
                "WEB_SETTINGS_URL must be a public HTTPS URL, for example https://bot.example.com."
            )
    elif normalized_key in {"LLM_PROMPT_CACHE", "TELEMETRY_DASHBOARD_ENABLED"}:
        if value.lower() not in _BOOL_VALUES:
            raise EnvUpdateError(f"{normalized_key} must be a boolean value.")

    return normalized_key, value


def resolve_env_file_path() -> Path:
    """Pick a writable env file path (Docker defaults to /data/.env)."""
    explicit = os.getenv("ENV_FILE", "").strip()
    if explicit:
        return Path(explicit)

    cookies_dir = os.getenv("COOKIES_DIR", "")
    if cookies_dir.startswith("/data") or os.getenv("RUN_MODE", "").strip().lower() == "docker":
        return Path("/data/.env")

    return PROJECT_ROOT / ".env"


def mask_env_value(key: str, value: str | None) -> str:
    if value is None or value == "":
        return "(not set)"
    if key not in SECRET_ENV_KEYS:
        return value
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}…{value[-4:]}"


def _check_writable(path: Path) -> tuple[bool, str]:
    parent = path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"Cannot create env directory {parent}: {exc}"

    if not os.access(parent, os.W_OK):
        return False, f"Env directory is not writable: {parent}"

    if path.exists() and not os.access(path, os.W_OK):
        return False, f"Env file is not writable: {path}"

    return True, ""


def _parse_env_lines(text: str) -> list[tuple[str, str | None, str | None]]:
    """Return (kind, key, value) per line. kind is comment|blank|kv."""
    lines: list[tuple[str, str | None, str | None]] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            lines.append(("blank", None, None))
            continue
        if stripped.startswith("#"):
            lines.append(("comment", None, raw))
            continue
        match = _KEY_PATTERN.match(raw.lstrip())
        if not match:
            lines.append(("comment", None, raw))
            continue
        key, value = match.group(1), match.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        lines.append(("kv", key, value))
    return lines


def _format_env_value(value: str) -> str:
    if not value:
        return ""
    if any(ch.isspace() for ch in value) or "#" in value or "=" in value:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _serialize_env_lines(
    lines: list[tuple[str, str | None, str | None]], updates: dict[str, str]
) -> str:
    seen: set[str] = set()
    output: list[str] = []

    for kind, key, value in lines:
        if kind == "kv" and key in updates:
            output.append(f"{key}={_format_env_value(updates[key])}")
            seen.add(key)
        elif kind == "comment":
            output.append(value or "")
        elif kind == "blank":
            output.append("")
        elif kind == "kv":
            output.append(f"{key}={_format_env_value(value or '')}")

    for key, value in updates.items():
        if key not in seen:
            if output and output[-1] != "":
                output.append("")
            output.append(f"{key}={_format_env_value(value)}")

    return "\n".join(output).rstrip() + "\n"


def _atomic_write_text(path: Path, content: str) -> None:
    ok, message = _check_writable(path)
    if not ok:
        raise PermissionError(message)

    old_mode: int | None = None
    if path.exists():
        old_mode = stat.S_IMODE(path.stat().st_mode)

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.chmod(tmp_path, old_mode if old_mode is not None else 0o644)
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


def read_env_file(path: Path | None = None) -> dict[str, str]:
    env_path = path or resolve_env_file_path()
    if not env_path.exists():
        return {
            key: value
            for key, value in os.environ.items()
            if key in EDITABLE_ENV_KEYS and value is not None
        }
    parsed = dotenv_values(env_path)
    return {key: value for key, value in parsed.items() if value is not None}


def get_env_entries() -> dict[str, str]:
    """Current values for editable keys (file overrides process env)."""
    file_values = read_env_file()
    entries: dict[str, str] = {}
    for key in sorted(EDITABLE_ENV_KEYS):
        if key in file_values:
            entries[key] = file_values[key]
        elif key in os.environ:
            entries[key] = os.environ[key]
    return entries


def ensure_env_file_seeded() -> None:
    """Create the env file from the current process environment when missing."""
    path = resolve_env_file_path()
    if path.exists():
        return

    ok, message = _check_writable(path)
    if not ok:
        logging.info("Skipping env file seed: %s", message)
        return

    seed = {
        key: os.environ[key]
        for key in EDITABLE_ENV_KEYS
        if key in os.environ and os.environ[key] != ""
    }
    if not seed:
        return

    header = (
        "# Auto-generated from process environment.\n"
        "# Managed by the bot; safe to edit via /set_env.\n"
    )
    body = "\n".join(f"{key}={_format_env_value(value)}" for key, value in sorted(seed.items()))
    _atomic_write_text(path, header + body + "\n")
    logging.info("Seeded env file at %s", path)


def set_env_values(updates: dict[str, str]) -> tuple[bool, str]:
    """Atomically persist validated environment updates.

    All values are validated before the file is touched. This makes a single
    Web App Save action all-or-nothing when a field contains an invalid value.
    """
    if not updates:
        return False, "No environment changes supplied."

    normalized_updates: dict[str, str] = {}
    try:
        for key, value in updates.items():
            normalized_key, normalized_value = validate_env_value(key, value)
            normalized_updates[normalized_key] = normalized_value
    except EnvUpdateError as exc:
        return False, str(exc)

    path = resolve_env_file_path()
    ok, message = _check_writable(path)
    if not ok:
        return False, message

    if path.exists():
        text = path.read_text(encoding="utf-8")
        lines = _parse_env_lines(text)
    else:
        lines = [
            ("comment", None, "# Bot environment settings"),
            ("blank", None, None),
        ]

    content = _serialize_env_lines(lines, normalized_updates)
    _atomic_write_text(path, content)

    load_dotenv(path, override=True)
    restart_required = False
    for key, value in normalized_updates.items():
        restart_required = apply_env_to_runtime(key, value) or restart_required
    if restart_required:
        return True, "Updated environment settings. Restart the bot to apply all changes."
    return False, "Updated environment settings."


def set_env_value(key: str, value: str) -> tuple[bool, str]:
    """Persist one env key. Returns (restart_required, message)."""
    normalized_key = key.strip().upper()
    restart_required, message = set_env_values({normalized_key: value})
    if message.startswith("Updated environment settings"):
        suffix = " Restart the bot to apply this change." if restart_required else "."
        return restart_required, f"Updated {normalized_key}.{suffix.lstrip('.')}"
    return restart_required, message


def apply_env_to_runtime(key: str, value: str) -> bool:
    """Mirror one env change into config and dependent modules."""
    os.environ[key] = value

    import config

    if key == "LLM_MODEL":
        config.LLM_MODEL = value
        import bot.llm as llm

        llm.LLM_MODEL = value
    elif key == "LLM_BASE_URL":
        old_base_url = config.LLM_BASE_URL
        config.LLM_BASE_URL = value
        import bot.llm as llm
        import bot.vision as vision
        import bot.agent as agent

        llm.client.base_url = value
        if config.LLM_PONDER_BASE_URL == old_base_url:
            config.LLM_PONDER_BASE_URL = value
            agent.client.base_url = value
        if config.LLM_VISION_BASE_URL == old_base_url:
            config.LLM_VISION_BASE_URL = value
            vision.client.base_url = value
    elif key == "LLM_PONDER_MODEL":
        config.LLM_PONDER_MODEL = value
        import bot.agent as agent

        agent.LLM_PONDER_MODEL = value
    elif key == "LLM_PONDER_MAX_STEPS":
        step_budget = int(value)
        config.LLM_PONDER_MAX_STEPS = step_budget
        import bot.agent as agent

        agent.LLM_PONDER_MAX_STEPS = step_budget
    elif key == "LLM_PROMPT_CACHE":
        enabled = value.lower() not in {"0", "false", "no"}
        config.LLM_PROMPT_CACHE = enabled
        import bot.llm as llm
        import bot.agent as agent

        llm.LLM_PROMPT_CACHE = enabled
        agent.LLM_PROMPT_CACHE = enabled
    elif key == "LLM_PONDER_BASE_URL":
        config.LLM_PONDER_BASE_URL = value
        import bot.agent as agent

        agent.client.base_url = value
    elif key == "LLM_VISION_MODEL":
        config.LLM_VISION_MODEL = value
        import bot.vision as vision

        vision.LLM_VISION_MODEL = value
    elif key == "LLM_VISION_BASE_URL":
        config.LLM_VISION_BASE_URL = value
        import bot.vision as vision

        vision.client.base_url = value
    elif key == "LLM_API_KEY":
        config.LLM_API_KEY = value
        import bot.llm as llm
        import bot.vision as vision
        import bot.agent as agent

        llm.client.api_key = value
        vision.client.api_key = value
        agent.client.api_key = value
    elif key == "LLM_REFERER":
        config.LLM_REFERER = value
        import bot.llm as llm
        import bot.vision as vision
        import bot.agent as agent

        llm.client.default_headers["HTTP-Referer"] = value
        vision.client.default_headers["HTTP-Referer"] = value
        agent.client.default_headers["HTTP-Referer"] = value
    elif key == "LLM_TITLE":
        config.LLM_TITLE = value
        import bot.llm as llm
        import bot.vision as vision
        import bot.agent as agent

        llm.client.default_headers["X-Title"] = value
        vision.client.default_headers["X-Title"] = value
        agent.client.default_headers["X-Title"] = value
    elif key == "ADMIN_ID":
        config.ADMIN_ID = int(value)
    elif key == "TELEMETRY_DASHBOARD_ENABLED":
        config.TELEMETRY_DASHBOARD_ENABLED = value.lower() not in {"0", "false", "no"}
    elif key == "TELEMETRY_DASHBOARD_HOST":
        config.TELEMETRY_DASHBOARD_HOST = value
    elif key == "TELEMETRY_DASHBOARD_PORT":
        config.TELEMETRY_DASHBOARD_PORT = int(value)
    elif key == "TELEMETRY_DASHBOARD_TOKEN":
        config.TELEMETRY_DASHBOARD_TOKEN = value or None
    elif key == "FIRECRAWL_API_KEY":
        config.FIRECRAWL_API_KEY = value or None
        import bot.agent as agent

        agent.FIRECRAWL_API_KEY = value or None
    elif key == "FIRECRAWL_API_URL":
        config.FIRECRAWL_API_URL = value
        import bot.agent as agent

        agent.FIRECRAWL_API_URL = value
    elif key == "WEB_SETTINGS_URL":
        config.WEB_SETTINGS_URL = value
    elif key == "WEB_SETTINGS_HOST":
        config.WEB_SETTINGS_HOST = value
    elif key == "WEB_SETTINGS_PORT":
        config.WEB_SETTINGS_PORT = int(value)
    elif key == "WEB_SETTINGS_INIT_DATA_MAX_AGE":
        config.WEB_SETTINGS_INIT_DATA_MAX_AGE = int(value)

    return key in RESTART_REQUIRED_KEYS


def format_env_panel() -> str:
    path = resolve_env_file_path()
    entries = get_env_entries()
    lines = [
        f"Environment file: {path}",
        "Use /set_env KEY value to change a setting.",
        "",
    ]
    for key in sorted(EDITABLE_ENV_KEYS):
        value = entries.get(key)
        lines.append(f"{key}={mask_env_value(key, value)}")
    if any(key in RESTART_REQUIRED_KEYS for key in entries):
        lines.append("")
        lines.append(
            "Restart required after changing: "
            + ", ".join(sorted(RESTART_REQUIRED_KEYS & EDITABLE_ENV_KEYS))
        )
    return "\n".join(lines)
