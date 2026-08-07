"""Authenticated Telegram Web App for editing the bot's admin settings.

The HTTP listener intentionally has no cookie or password login. Every API
request must carry fresh, Telegram-signed Web App ``initData`` and must belong
to ``ADMIN_ID``. The page never receives saved secret values; it can only
replace them.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

from aiohttp import web
from telegram import InputFile

import config
from bot.env_config import (
    EDITABLE_ENV_KEYS,
    RESTART_REQUIRED_KEYS,
    SECRET_ENV_KEYS,
    get_env_entries,
    mask_env_value,
    set_env_values,
)
from bot.logic import (
    GLOBAL_SETTINGS_CHAT_ID,
    get_behavior_settings,
    get_paused,
    get_utils_disabled,
    set_paused,
    set_utils_disabled,
    update_behavior_settings,
)
from bot.memory import get_config, set_config
from bot.telemetry.export import build_llm_telemetry_export
from bot.telemetry.storage import fetch_llm_telemetry_event
from bot.telemetry.web import build_telemetry_snapshot

logger = logging.getLogger(__name__)

# Set by start_settings_web_server so export can DM files through the live bot.
_TELEGRAM_BOT: Any | None = None

INIT_DATA_HEADER = "X-Telegram-Init-Data"
MAX_JSON_BODY_BYTES = 2 * 1024 * 1024
MAX_PERSONA_PROMPT_LENGTH = 30_000
MAX_COOKIE_CONTENT_LENGTH = 512 * 1024

# Keep this list aligned with the services selected by the message and music
# download handlers.  ``twitter`` is accepted as a legacy alias for ``x``.
COOKIE_SERVICES: dict[str, str] = {
    "youtube": "YouTube",
    "instagram": "Instagram",
    "x": "X (Twitter)",
    "tiktok": "TikTok",
    "facebook": "Facebook",
    "reddit": "Reddit",
    "pinterest": "Pinterest",
    "spotify": "Spotify",
    "soundcloud": "SoundCloud",
    "bandcamp": "Bandcamp",
    "mixcloud": "Mixcloud",
    "twitch": "Twitch",
    "vk": "VK",
    "rutube": "Rutube",
}


class WebSettingsAuthError(ValueError):
    """The request did not carry valid Telegram Web App credentials."""


class WebSettingsValidationError(ValueError):
    """A submitted Web App value is malformed or outside the supported scope."""


def _web_app_secret(bot_token: str) -> bytes:
    return hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()


def validate_telegram_web_app_init_data(
    init_data: str,
    *,
    bot_token: str,
    admin_id: int,
    max_age_seconds: int,
    now: int | None = None,
) -> dict:
    """Validate Telegram's initData HMAC and return its signed user payload."""
    if not init_data or not bot_token:
        raise WebSettingsAuthError("Missing Telegram Web App credentials.")

    fields = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    values: dict[str, str] = {}
    for key, value in fields:
        if key in values:
            raise WebSettingsAuthError("Duplicate Telegram Web App credential field.")
        values[key] = value

    received_hash = values.pop("hash", "")
    if not received_hash or "user" not in values or "auth_date" not in values:
        raise WebSettingsAuthError("Incomplete Telegram Web App credentials.")

    data_check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(values.items())
    )
    expected_hash = hmac.new(
        _web_app_secret(bot_token),
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash):
        raise WebSettingsAuthError("Invalid Telegram Web App signature.")

    try:
        auth_date = int(values["auth_date"])
    except (TypeError, ValueError) as exc:
        raise WebSettingsAuthError("Invalid Telegram Web App timestamp.") from exc
    current_time = int(time.time()) if now is None else now
    if auth_date > current_time + 60 or current_time - auth_date > max_age_seconds:
        raise WebSettingsAuthError("Telegram Web App credentials have expired.")

    try:
        user = json.loads(values["user"])
        user_id = int(user["id"])
    except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise WebSettingsAuthError("Invalid Telegram Web App user payload.") from exc
    if user_id != admin_id:
        raise WebSettingsAuthError("This Web App is only available to the bot admin.")
    return user


def _require_admin(request: web.Request) -> dict:
    try:
        return validate_telegram_web_app_init_data(
            request.headers.get(INIT_DATA_HEADER, ""),
            bot_token=config.TELEGRAM_BOT_TOKEN or "",
            admin_id=config.ADMIN_ID,
            max_age_seconds=config.WEB_SETTINGS_INIT_DATA_MAX_AGE,
        )
    except (ValueError, WebSettingsAuthError) as exc:
        raise web.HTTPUnauthorized(text=str(exc)) from exc


def _env_entry(key: str, entries: Mapping[str, str]) -> dict[str, object]:
    value = entries.get(key)
    secret = key in SECRET_ENV_KEYS
    return {
        "key": key,
        "value": None if secret else value,
        "display_value": mask_env_value(key, value),
        "is_secret": secret,
        "is_set": bool(value),
        "restart_required": key in RESTART_REQUIRED_KEYS,
    }


async def build_settings_snapshot() -> dict[str, object]:
    """Build a browser-safe settings document; saved secrets stay server-side."""
    entries = get_env_entries()
    persona_prompt = await get_config("persona_prompt")
    if not persona_prompt:
        from bot.llm import DEFAULT_PERSONA

        persona_prompt = DEFAULT_PERSONA

    return {
        "environment": [_env_entry(key, entries) for key in sorted(EDITABLE_ENV_KEYS)],
        "persona_prompt": persona_prompt,
        "persona_uses_default": not bool(await get_config("persona_prompt")),
        "behavior": await get_behavior_settings(GLOBAL_SETTINGS_CHAT_ID),
        "paused": get_paused(),
        "utils_disabled": await get_utils_disabled(GLOBAL_SETTINGS_CHAT_ID),
    }


def _normalize_cookie_service(service: object) -> str:
    if not isinstance(service, str):
        raise WebSettingsValidationError("Cookie service must be text.")
    normalized = service.strip().lower()
    if normalized == "twitter":
        normalized = "x"
    if normalized not in COOKIE_SERVICES:
        raise WebSettingsValidationError("Unsupported cookie service.")
    return normalized


def _cookie_path(service: str) -> Path:
    # Service is normalized against a fixed allow-list, so this cannot escape
    # COOKIES_DIR even when a browser sends a malicious payload.
    return Path(config.COOKIES_DIR) / f"{service}.txt"


def _cookie_snapshot_entry(service: str) -> dict[str, object]:
    path = _cookie_path(service)
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
        from bot.media_utils import _SESSION_COOKIE_NAMES, normalize_netscape_cookies

        _normalized, names = normalize_netscape_cookies(raw)
        session = sorted({name for name in names if name.lower() in _SESSION_COOKIE_NAMES})
        return {
            "service": service,
            "label": COOKIE_SERVICES[service],
            "present": True,
            "valid_rows": len(names),
            "session_cookie_names": session,
            "updated_at": int(path.stat().st_mtime),
        }
    except FileNotFoundError:
        return {
            "service": service,
            "label": COOKIE_SERVICES[service],
            "present": False,
            "valid_rows": 0,
            "session_cookie_names": [],
            "updated_at": None,
        }
    except OSError:
        logger.exception("Could not inspect cookies for service=%s", service)
        return {
            "service": service,
            "label": COOKIE_SERVICES[service],
            "present": True,
            "valid_rows": 0,
            "session_cookie_names": [],
            "updated_at": None,
            "error": "Could not read the saved cookie file.",
        }


async def build_cookie_snapshot() -> dict[str, object]:
    """Return cookie-file metadata only; values never leave the host."""
    return {"services": [_cookie_snapshot_entry(service) for service in COOKIE_SERVICES]}


async def apply_cookie_update(payload: object, *, requesting_user_id: int) -> dict[str, object]:
    if requesting_user_id != config.ADMIN_ID:
        raise WebSettingsAuthError("This Web App is only available to the bot admin.")
    body = _required_mapping(payload, "Cookie payload")
    service = _normalize_cookie_service(body.get("service"))
    content = body.get("content")
    if not isinstance(content, str):
        raise WebSettingsValidationError("Cookie content must be text.")
    if len(content) > MAX_COOKIE_CONTENT_LENGTH:
        raise WebSettingsValidationError(
            f"Cookie content is too long (maximum {MAX_COOKIE_CONTENT_LENGTH} characters)."
        )

    from bot.media_utils import save_netscape_cookies

    count, _names, session = save_netscape_cookies(str(_cookie_path(service)), content)
    return {
        "message": f"Saved {count} cookie rows for {COOKIE_SERVICES[service]}.",
        "service": service,
        "valid_rows": count,
        "session_cookie_names": session,
    }


async def delete_cookie_file(payload: object, *, requesting_user_id: int) -> dict[str, object]:
    if requesting_user_id != config.ADMIN_ID:
        raise WebSettingsAuthError("This Web App is only available to the bot admin.")
    body = _required_mapping(payload, "Cookie payload")
    service = _normalize_cookie_service(body.get("service"))
    try:
        os.remove(_cookie_path(service))
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.exception("Could not remove cookies for service=%s", service)
        raise WebSettingsValidationError("Could not remove the saved cookie file.") from exc
    return {"message": f"Removed saved cookies for {COOKIE_SERVICES[service]}.", "service": service}


def _required_mapping(payload: object, field: str) -> Mapping[str, object]:
    if not isinstance(payload, Mapping):
        raise WebSettingsValidationError(f"{field} must be an object.")
    return payload


def _optional_percentage(value: object, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise WebSettingsValidationError(f"{name} must be a number from 0 to 100.")
    try:
        percent = float(value)
    except (TypeError, ValueError) as exc:
        raise WebSettingsValidationError(
            f"{name} must be a number from 0 to 100."
        ) from exc
    if not 0 <= percent <= 100:
        raise WebSettingsValidationError(f"{name} must be a number from 0 to 100.")
    return percent / 100


def _optional_int(value: object, name: str, maximum: int) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise WebSettingsValidationError(f"{name} must be an integer from 0 to {maximum}.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise WebSettingsValidationError(
            f"{name} must be an integer from 0 to {maximum}."
        ) from exc
    if not 0 <= parsed <= maximum:
        raise WebSettingsValidationError(f"{name} must be an integer from 0 to {maximum}.")
    return parsed


async def _save_persona_prompt(persona_prompt: object) -> None:
    if not isinstance(persona_prompt, str):
        raise WebSettingsValidationError("Persona prompt must be text.")
    prompt = persona_prompt.strip()
    if not prompt:
        raise WebSettingsValidationError("Persona prompt cannot be empty.")
    if len(prompt) > MAX_PERSONA_PROMPT_LENGTH:
        raise WebSettingsValidationError(
            f"Persona prompt is too long (maximum {MAX_PERSONA_PROMPT_LENGTH} characters)."
        )

    # Keep the reaction picker in sync just as /update_prompt does. Generate
    # before storing either value so a failed generation leaves the old pair.
    from bot.llm import generate_reaction_prompt

    reaction_prompt = await generate_reaction_prompt(prompt)
    await set_config("persona_prompt", prompt)
    await set_config("reaction_prompt", reaction_prompt)


async def apply_settings_update(payload: object, *, requesting_user_id: int) -> dict[str, object]:
    """Apply one browser Save request using the existing durable config layers."""
    if requesting_user_id != config.ADMIN_ID:
        raise WebSettingsAuthError("This Web App is only available to the bot admin.")
    body = _required_mapping(payload, "Settings payload")
    results: list[str] = []
    restart_required = False

    if "environment" in body:
        env_payload = _required_mapping(body["environment"], "environment")
        env_updates: dict[str, str] = {}
        for key, value in env_payload.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise WebSettingsValidationError("Environment values must be text.")
            env_updates[key] = value
        if env_updates:
            needs_restart, message = set_env_values(env_updates)
            if not message.startswith("Updated environment settings"):
                raise WebSettingsValidationError(message)
            restart_required = restart_required or needs_restart
            results.append("environment")

    if "persona_prompt" in body:
        await _save_persona_prompt(body["persona_prompt"])
        results.append("persona")

    if "behavior" in body:
        behavior = _required_mapping(body["behavior"], "behavior")
        allowed_fields = {
            "reply_chance",
            "reaction_chance",
            "cooldown_threshold",
            "max_ping_pong",
            "media_reply_guidance",
        }
        unknown_fields = set(behavior) - allowed_fields
        if unknown_fields:
            raise WebSettingsValidationError(
                "Unknown behavior setting: " + ", ".join(sorted(unknown_fields))
            )
        guidance = behavior.get("media_reply_guidance")
        if guidance is not None and not isinstance(guidance, str):
            raise WebSettingsValidationError("Media reply guidance must be text.")
        ok, reason = await update_behavior_settings(
            GLOBAL_SETTINGS_CHAT_ID,
            requesting_user_id=requesting_user_id,
            admin_id=config.ADMIN_ID,
            reply_chance=_optional_percentage(behavior.get("reply_chance"), "Reply chance"),
            reaction_chance=_optional_percentage(
                behavior.get("reaction_chance"), "Reaction chance"
            ),
            cooldown_threshold=_optional_int(
                behavior.get("cooldown_threshold"), "Cooldown threshold", 200
            ),
            max_ping_pong=_optional_int(behavior.get("max_ping_pong"), "Max ping pong", 20),
            media_reply_guidance=guidance,
        )
        if not ok:
            raise WebSettingsValidationError(f"Could not update behavior settings: {reason}")
        results.append("behavior")

    if "paused" in body:
        if not isinstance(body["paused"], bool):
            raise WebSettingsValidationError("Paused must be true or false.")
        await set_paused(body["paused"])
        results.append("pause state")

    if "utils_disabled" in body:
        if not isinstance(body["utils_disabled"], bool):
            raise WebSettingsValidationError("Utils disabled must be true or false.")
        await set_utils_disabled(GLOBAL_SETTINGS_CHAT_ID, body["utils_disabled"])
        results.append("utilities state")

    if not results:
        raise WebSettingsValidationError("No changes supplied.")
    return {
        "updated": results,
        "restart_required": restart_required,
        "message": "Saved " + ", ".join(results) + ".",
    }


async def settings_page(request: web.Request) -> web.Response:
    return web.Response(text=render_settings_html(), content_type="text/html")


async def cookies_page(request: web.Request) -> web.Response:
    return web.Response(text=render_cookies_html(), content_type="text/html")


async def telemetry_page(request: web.Request) -> web.Response:
    """Serve the shell; its sensitive content is Telegram-authenticated API data."""
    return web.Response(text=render_telemetry_html(), content_type="text/html")


async def health_page(request: web.Request) -> web.Response:
    """Internal liveness/readiness probe used by the container healthcheck."""
    try:
        await build_telemetry_snapshot({"limit": "1"})
    except Exception:
        logger.exception("Web App health check failed")
        raise web.HTTPServiceUnavailable(text="Database unavailable")
    return web.Response(text="ok")


async def api_get_settings(request: web.Request) -> web.Response:
    _require_admin(request)
    return web.json_response(await build_settings_snapshot())


async def api_get_default_persona(request: web.Request) -> web.Response:
    """Return the built-in persona for the editor's reset action."""
    _require_admin(request)
    from bot.llm import DEFAULT_PERSONA

    return web.json_response({"persona": DEFAULT_PERSONA})


async def api_save_settings(request: web.Request) -> web.Response:
    user = _require_admin(request)
    if request.content_length and request.content_length > MAX_JSON_BODY_BYTES:
        raise web.HTTPRequestEntityTooLarge(
            max_size=MAX_JSON_BODY_BYTES, actual_size=request.content_length
        )
    try:
        payload = await request.json()
        result = await apply_settings_update(payload, requesting_user_id=int(user["id"]))
    except (json.JSONDecodeError, WebSettingsValidationError, WebSettingsAuthError) as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except Exception:
        logger.exception("Telegram Web settings save failed")
        return web.json_response({"error": "Could not save settings."}, status=500)
    return web.json_response(result)


async def api_get_cookies(request: web.Request) -> web.Response:
    _require_admin(request)
    return web.json_response(await build_cookie_snapshot())


async def api_save_cookies(request: web.Request) -> web.Response:
    user = _require_admin(request)
    try:
        payload = await request.json()
        result = await apply_cookie_update(payload, requesting_user_id=int(user["id"]))
    except (json.JSONDecodeError, WebSettingsValidationError, WebSettingsAuthError) as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except Exception:
        logger.exception("Telegram Web cookie save failed")
        return web.json_response({"error": "Could not save cookies."}, status=500)
    return web.json_response(result)


async def api_delete_cookies(request: web.Request) -> web.Response:
    user = _require_admin(request)
    try:
        payload = await request.json()
        result = await delete_cookie_file(payload, requesting_user_id=int(user["id"]))
    except (json.JSONDecodeError, WebSettingsValidationError, WebSettingsAuthError) as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except Exception:
        logger.exception("Telegram Web cookie deletion failed")
        return web.json_response({"error": "Could not remove cookies."}, status=500)
    return web.json_response(result)


async def api_get_telemetry(request: web.Request) -> web.Response:
    _require_admin(request)
    return web.json_response(await build_telemetry_snapshot(request.query))


async def api_get_telemetry_export(request: web.Request) -> web.Response:
    """Return the JSON export body (kept for debugging / non-WebView clients)."""
    _require_admin(request)
    snapshot = await build_telemetry_snapshot(request.query)
    persona_prompt = await get_config("persona_prompt")
    export = build_llm_telemetry_export(
        snapshot["events"], persona_prompt, snapshot["filters"]
    )
    return web.json_response(export)


async def api_post_telemetry_export_dm(request: web.Request) -> web.Response:
    """Build the telemetry export and send it as a JSON document to the admin DM."""
    user = _require_admin(request)
    bot = request.app.get("telegram_bot") or _TELEGRAM_BOT
    if bot is None:
        return web.json_response(
            {"error": "Bot is not ready to send DMs yet. Restart the bot and try again."},
            status=503,
        )

    snapshot = await build_telemetry_snapshot(request.query)
    persona_prompt = await get_config("persona_prompt")
    export = build_llm_telemetry_export(
        snapshot["events"], persona_prompt, snapshot["filters"]
    )
    payload = json.dumps(export, ensure_ascii=False, indent=2).encode("utf-8")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"freak-telemetry-{stamp}.json"
    chat_id = int(user["id"])
    event_count = len(export.get("events") or [])
    main_count = int(export.get("main_event_count") or 0)
    ponder_count = int(export.get("ponder_event_count") or 0)
    caption = (
        f"Telemetry export · {event_count} events "
        f"({main_count} main / {ponder_count} ponder)"
    )
    try:
        await bot.send_document(
            chat_id=chat_id,
            document=InputFile(BytesIO(payload), filename=filename),
            caption=caption,
        )
    except Exception:
        logger.exception("Failed to DM telemetry export to admin %s", chat_id)
        return web.json_response(
            {"error": "Could not send the JSON file in DM."},
            status=502,
        )

    return web.json_response(
        {
            "ok": True,
            "message": "Sent the JSON export to your DM.",
            "filename": filename,
            "bytes": len(payload),
            "event_count": event_count,
            "main_event_count": main_count,
            "ponder_event_count": ponder_count,
        }
    )


async def api_get_telemetry_event(request: web.Request) -> web.Response:
    _require_admin(request)
    try:
        event_id = int(request.match_info["event_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise web.HTTPBadRequest(text="Invalid telemetry event id") from exc
    event = await fetch_llm_telemetry_event(event_id)
    if event is None:
        raise web.HTTPNotFound(text="Telemetry event not found")
    return web.json_response(event)


def create_settings_web_app() -> web.Application:
    app = web.Application(client_max_size=MAX_JSON_BODY_BYTES)
    app.router.add_get("/", settings_page)
    app.router.add_get("/settings", settings_page)
    app.router.add_get("/settings/", settings_page)
    app.router.add_get("/cookies", cookies_page)
    app.router.add_get("/cookies/", cookies_page)
    app.router.add_get("/telemetry", telemetry_page)
    app.router.add_get("/telemetry/", telemetry_page)
    app.router.add_get("/health", health_page)
    app.router.add_get("/api/settings", api_get_settings)
    app.router.add_get("/api/default_persona", api_get_default_persona)
    app.router.add_post("/api/settings", api_save_settings)
    app.router.add_get("/api/cookies", api_get_cookies)
    app.router.add_post("/api/cookies", api_save_cookies)
    app.router.add_delete("/api/cookies", api_delete_cookies)
    app.router.add_get("/api/telemetry", api_get_telemetry)
    app.router.add_get("/api/telemetry/export.json", api_get_telemetry_export)
    app.router.add_post("/api/telemetry/export", api_post_telemetry_export_dm)
    app.router.add_get("/api/telemetry/event/{event_id}", api_get_telemetry_event)
    return app


async def start_settings_web_server(bot: Any | None = None) -> web.AppRunner:
    """Start the embedded settings listener and return its runner for shutdown."""
    global _TELEGRAM_BOT
    _TELEGRAM_BOT = bot
    app = create_settings_web_app()
    app["telegram_bot"] = bot
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, config.WEB_SETTINGS_HOST, config.WEB_SETTINGS_PORT)
    try:
        await site.start()
    except Exception:
        await runner.cleanup()
        raise
    logger.info(
        "Telegram Web settings listener on http://%s:%s (public URL: %s)",
        config.WEB_SETTINGS_HOST,
        config.WEB_SETTINGS_PORT,
        config.WEB_SETTINGS_URL,
    )
    return runner


async def stop_settings_web_server(runner: web.AppRunner | None) -> None:
    if runner is not None:
        await runner.cleanup()


def render_telemetry_html() -> str:
    """Return the responsive, Telegram-native telemetry Web App shell."""
    return r'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>Freak telemetry</title>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <style>
    :root { color-scheme: light dark; }
    * { box-sizing: border-box; }
    body { margin: 0; padding: env(safe-area-inset-top) 14px env(safe-area-inset-bottom); background: var(--tg-theme-bg-color, #fff); color: var(--tg-theme-text-color, #111); font: 15px/1.4 system-ui, sans-serif; }
    header { padding: 18px 2px 12px; } h1 { margin: 0; font-size: 24px; } .sub, .muted { color: var(--tg-theme-hint-color, #777); }
    .controls, .panel, details { background: var(--tg-theme-secondary-bg-color, #f4f4f5); border-radius: 14px; padding: 13px; margin: 10px 0; }
    .filters { display: grid; grid-template-columns: 1fr 1fr; gap: 9px; } label { color: var(--tg-theme-hint-color, #777); font-size: 12px; } select, input, button { width: 100%; margin-top: 4px; border-radius: 9px; border: 0; padding: 10px; font: inherit; background: var(--tg-theme-bg-color, #fff); color: inherit; }
    button { background: var(--tg-theme-button-color, #2481cc); color: var(--tg-theme-button-text-color, #fff); font-weight: 600; cursor: pointer; } button.secondary { background: var(--tg-theme-bg-color, #fff); color: var(--tg-theme-link-color, #2481cc); }
    .actions { display: flex; gap: 8px; grid-column: 1 / -1; }.actions button { flex: 1; }
    .cards { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }.card { background: var(--tg-theme-secondary-bg-color, #f4f4f5); border-radius: 12px; padding: 11px; }.label { font-size: 11px; color: var(--tg-theme-hint-color, #777); }.value { font-size: 20px; font-weight: 700; margin-top: 2px; }
    h2 { font-size: 18px; margin: 0 0 8px; } ul { margin: 0; padding-left: 20px; } li + li { margin-top: 6px; }.event-head { display: flex; justify-content: space-between; gap: 8px; align-items: center; }.event-title { font-weight: 700; }.meta { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 7px; color: var(--tg-theme-hint-color, #777); font-size: 12px; }.badge { border-radius: 99px; padding: 3px 8px; color: #fff; font-size: 12px; white-space: nowrap; }.success { background: #2e7d32; }.no_reply { background: #1565c0; }.failure { background: #c62828; }.unknown { background: #666; }
    summary { cursor: pointer; font-weight: 600; } pre { white-space: pre-wrap; overflow-wrap: anywhere; background: var(--tg-theme-bg-color, #fff); padding: 10px; border-radius: 9px; font-size: 12px; }.detail-title { font-size: 13px; font-weight: 700; margin: 13px 0 4px; }.hidden { display: none; }.error { color: #d32f2f; }
  </style>
</head>
<body>
  <header><h1>Telemetry</h1><div class="sub">LLM context, replies, and memory behavior</div></header>
  <div class="controls"><div class="filters">
    <label>Chat<select id="chat"><option value="all">All chats</option></select></label>
    <label>Status<select id="status"><option value="all">All statuses</option><option value="success">Success</option><option value="no_reply">No reply</option><option value="invalid_json">Invalid JSON</option><option value="validation_error">Validation error</option><option value="empty_content">Empty content</option><option value="exception">Exception</option></select></label>
    <label>Source<select id="source"><option value="all">All sources</option><option value="message">Message</option><option value="daily_task">Daily task</option><option value="scheduled_action">Scheduled action</option><option value="ponder_agent">Ponder agent</option><option value="ponder_followup">Ponder follow-up</option><option value="ponder">Ponder follow-up (legacy)</option></select></label>
    <label>Events<input id="limit" type="number" min="1" max="500" value="100"></label>
    <div class="actions"><button id="apply">Refresh</button><button id="export" class="secondary">Send JSON to DM</button></div>
  </div></div>
  <p id="notice" class="muted">Loading telemetry…</p><main id="content" class="hidden"><section class="panel"><h2>Main RP bot</h2><div id="main-cards" class="cards"></div></section><section class="panel"><h2>Ponder agent</h2><div id="ponder-cards" class="cards"></div></section><section class="panel"><h2>Combined</h2><div id="cards" class="cards"></div></section><section class="panel"><h2>Suggestions</h2><ul id="suggestions"></ul></section><section class="panel"><h2>Events</h2><div id="events"></div></section></main>
  <script>
  (() => {
    const tg = window.Telegram && window.Telegram.WebApp;
    if (tg) { tg.ready(); tg.expand(); document.documentElement.style.setProperty('--tg-theme-bg-color', tg.themeParams.bg_color || '#fff'); }
    const ids = ['chat', 'status', 'source', 'limit']; const $ = id => document.getElementById(id);
    const notice = $('notice'), content = $('content');
    const headers = () => ({'X-Telegram-Init-Data': tg ? tg.initData : ''});
    const fmt = value => value === null || value === undefined ? 'n/a' : (typeof value === 'number' && !Number.isInteger(value) ? value.toFixed(1) : String(value));
    const rate = value => value === null || value === undefined ? 'n/a' : `${(value * 100).toFixed(1)}%`;
    const make = (tag, text, cls) => { const el = document.createElement(tag); if (text !== undefined) el.textContent = text; if (cls) el.className = cls; return el; };
    const query = () => new URLSearchParams(Object.fromEntries(ids.map(id => [id === 'chat' ? 'chat_id' : id, $(id).value])));
    const select = (id, value) => { $(id).value = value == null ? 'all' : String(value); };
    function populateChats(chats, active) { const dropdown = $('chat'); const previous = dropdown.value; dropdown.replaceChildren(make('option', 'All chats')); dropdown.firstChild.value = 'all'; chats.forEach(chat => { const opt = make('option', chat); opt.value = chat; dropdown.append(opt); }); dropdown.value = active == null ? (previous || 'all') : String(active); }
    function summaryItems(s, role) {
      const base = [['Events', fmt(s.total_events)], ['Success rate', rate(s.success_rate)], ['Failure rate', rate(s.failure_rate)], ['Avg latency', s.avg_latency_ms == null ? 'n/a' : `${fmt(s.avg_latency_ms)} ms`], ['Avg prompt tokens', fmt(s.avg_prompt_tokens)], ['Cached prompt tokens', fmt(s.avg_prompt_cached_tokens)], ['Prompt cache hit rate', rate(s.avg_prompt_cache_hit_rate)]];
      if (role === 'main') {
        base.push(['No-reply rate', rate(s.no_reply_rate)], ['Avg context chars', fmt(s.avg_context_chars)], ['Retrieved memories', fmt(s.avg_retrieved_memory_count)], ['Media gallery size', fmt(s.avg_saved_media_option_count)], ['Memory writes', fmt(s.avg_memory_write_count)], ['Memory write success', rate(s.memory_write_success_rate)]);
      } else if (role === 'ponder') {
        base.push(['Avg tool calls', fmt(s.avg_tool_call_count)], ['Avg completion tokens', fmt(s.avg_completion_tokens)]);
      } else {
        base.push(['No-reply rate', rate(s.no_reply_rate)], ['Avg context chars', fmt(s.avg_context_chars)], ['Memory writes', fmt(s.avg_memory_write_count)]);
      }
      return base;
    }
    function fillCards(id, items) {
      const cards = $(id); cards.replaceChildren();
      items.forEach(([label, value]) => { const card = make('div', undefined, 'card'); card.append(make('div', label, 'label'), make('div', value, 'value')); cards.append(card); });
    }
    function render(snapshot) {
      const s = snapshot.summary || {}; const main = snapshot.main_summary || s; const ponder = snapshot.ponder_summary || {};
      populateChats(snapshot.chats, snapshot.filters.chat_id); select('status', snapshot.filters.status); select('source', snapshot.filters.source); $('limit').value = snapshot.filters.limit;
      fillCards('main-cards', summaryItems(main, 'main'));
      fillCards('ponder-cards', summaryItems(ponder, 'ponder'));
      fillCards('cards', summaryItems(s, 'all').concat([['Main events', fmt(snapshot.main_event_count)], ['Ponder events', fmt(snapshot.ponder_event_count)]]));
      const suggestions = $('suggestions'); suggestions.replaceChildren(); (snapshot.suggestions.length ? snapshot.suggestions : ['No suggestions yet.']).forEach(value => suggestions.append(make('li', value)));
      const events = $('events'); events.replaceChildren(); if (!snapshot.events.length) events.append(make('p', 'No telemetry recorded for these filters yet.', 'muted'));
      snapshot.events.forEach(event => {
        const details = make('details'); const summary = make('summary'); const head = make('div', undefined, 'event-head'); const title = make('span', `#${event.id} · ${event.timestamp || 'unknown time'}`, 'event-title'); const status = event.status || 'unknown'; const klass = status === 'success' ? 'success' : status === 'no_reply' ? 'no_reply' : ['invalid_json','validation_error','empty_content','exception'].includes(status) ? 'failure' : 'unknown'; head.append(title, make('span', status.replace('_', ' '), `badge ${klass}`)); summary.append(head); const meta = make('div', undefined, 'meta'); [`chat ${event.chat_id}`, event.source || 'message', `latency ${fmt(event.latency_ms)} ms`, `context ${fmt(event.context_message_count)} msgs`, `memories ${fmt(event.retrieved_memory_count)}`, `writes ${fmt(event.memory_write_count)}/${fmt(event.failed_memory_write_count)}`, `states ${(event.active_event_states || []).length}`, `scheduled ${(event.pending_scheduled_actions || []).length}`, `cache ${rate(event.prompt_cache_hit_rate)}`, `gallery ${fmt(event.saved_media_option_count)}`].forEach(text => meta.append(make('span', text))); summary.append(meta); details.append(summary);
        [['Trigger messages', event.trigger_messages || []], ['Memories used', {user_thoughts: event.used_user_thoughts || {}, general_memories: event.used_general_memories || []}], ['Active event states', event.active_event_states || []], ['Pending scheduled actions', event.pending_scheduled_actions || []], ['Media gallery', {options: event.saved_media_options || [], count: event.saved_media_option_count || 0, policy: event.saved_media_policy || {}}], ['Response', {messages: event.response_messages || [], media: event.response_media || {}, reply_to_message_id: event.reply_to_message_id}], ['Memorized', event.memory_writes || []], ['Tool calls', event.tool_calls || []]].forEach(([label, value]) => { details.append(make('div', label, 'detail-title')); details.append(make('pre', JSON.stringify(value, null, 2))); }); events.append(details);
      });
      notice.className = 'hidden'; content.classList.remove('hidden');
    }
    async function load() { notice.className = 'muted'; notice.textContent = 'Loading telemetry…'; content.classList.add('hidden'); try { const response = await fetch(`/api/telemetry?${query()}`, {headers: headers()}); if (!response.ok) throw new Error(await response.text() || 'Could not load telemetry.'); render(await response.json()); } catch (error) { notice.className = 'error'; notice.textContent = error.message || 'Could not load telemetry.'; } }
    $('apply').addEventListener('click', load); $('export').addEventListener('click', async () => { notice.className = 'muted'; notice.textContent = 'Sending JSON to your DM…'; try { const response = await fetch(`/api/telemetry/export?${query()}`, {method: 'POST', headers: headers()}); const body = await response.json().catch(() => ({})); if (!response.ok) throw new Error(body.error || body.message || 'Could not export telemetry.'); notice.className = 'muted'; notice.textContent = body.message || 'Sent the JSON export to your DM.'; if (tg && tg.HapticFeedback && tg.HapticFeedback.notificationOccurred) tg.HapticFeedback.notificationOccurred('success'); if (tg && tg.showAlert) tg.showAlert(body.message || 'Sent the JSON export to your DM.'); } catch (error) { notice.className = 'error'; notice.textContent = error.message || 'Could not export telemetry.'; if (tg && tg.HapticFeedback && tg.HapticFeedback.notificationOccurred) tg.HapticFeedback.notificationOccurred('error'); } });
    load();
  })();
  </script>
</body>
</html>'''


def render_settings_html() -> str:
    """Return the self-contained Web App UI (no secrets are interpolated)."""
    return r'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>Freak settings</title>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <style>
    :root { color-scheme: light dark; --bg: var(--tg-theme-bg-color, #10131a); --card: var(--tg-theme-secondary-bg-color, #1a202b); --text: var(--tg-theme-text-color, #edf1f7); --hint: var(--tg-theme-hint-color, #98a2b3); --accent: var(--tg-theme-button-color, #61a8ff); --accent-text: var(--tg-theme-button-text-color, #fff); --bad: var(--tg-theme-destructive-text-color, #f97066); --good: var(--tg-theme-accent-text-color, #4bb57b); }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--bg); color: var(--text); font: 16px/1.45 system-ui, sans-serif; }
    main { max-width: 760px; margin: auto; padding: 18px 14px 94px; }
    h1 { font-size: 24px; margin: 0 0 4px; } h2 { font-size: 18px; margin: 0 0 12px; }
    .intro, .hint { color: var(--hint); } .intro { margin: 0 0 18px; }
    .topbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
    .tabs { display: flex; gap: 8px; overflow-x: auto; margin: 14px 0; padding-bottom: 2px; }
    .card { background: var(--card); border-radius: 14px; padding: 16px; margin: 14px 0; }
    .field { display: grid; gap: 6px; margin: 12px 0; }
    .field label { font-weight: 650; } .field small { color: var(--hint); }
    input, textarea { width: 100%; border: 1px solid color-mix(in srgb, var(--hint) 45%, transparent); background: color-mix(in srgb, var(--bg) 72%, transparent); color: var(--text); border-radius: 9px; padding: 10px; font: inherit; }
    textarea { min-height: 220px; resize: vertical; } #persona { min-height: 320px; max-height: 60vh; font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; line-height: 1.5; } input[type=number] { max-width: 180px; }
    .switch { display: flex; align-items: center; justify-content: space-between; gap: 14px; padding: 10px 0; } .switch input { width: 22px; height: 22px; }
    .secret-note { color: var(--hint); margin: -2px 0 4px; font-size: 13px; }
    #status { position: fixed; left: 14px; right: 14px; bottom: 14px; max-width: 732px; margin: auto; padding: 12px 14px; border-radius: 10px; background: var(--card); box-shadow: 0 5px 30px #0006; display: none; }
    #status.ok { color: var(--good); display: block; } #status.error { color: var(--bad); display: block; }
    button { border: 0; border-radius: 10px; padding: 11px 13px; background: var(--accent); color: var(--accent-text); font: 700 16px system-ui, sans-serif; cursor: pointer; }
    .topbar button, .tabs button, .secondary { width: auto; background: color-mix(in srgb, var(--hint) 24%, transparent); color: var(--text); font-size: 14px; }
    .tabs button { white-space: nowrap; } .persona-actions { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-top: 12px; } .persona-actions #save { margin-left: auto; }
    .counter { color: var(--hint); font-size: 13px; text-align: right; } .counter.warning { color: var(--bad); font-weight: 700; }
    button:disabled { opacity: .55; cursor: wait; } .hidden { display: none; }
    .field-label { display: flex; align-items: center; justify-content: space-between; gap: 10px; } .field-label .secondary { padding: 6px 9px; font-size: 12px; }
    #persona-overlay { position: fixed; inset: 0; z-index: 1000; background: var(--bg); display: flex; flex-direction: column; gap: 12px; padding: calc(env(safe-area-inset-top, 0px) + 12px) 14px calc(env(safe-area-inset-bottom, 0px) + 12px); }
    #persona-overlay.hidden { display: none; }
    .overlay-header, .overlay-footer { display: flex; align-items: center; gap: 10px; } .overlay-header h2 { margin: 0; } .overlay-header .secondary { margin-left: auto; } .overlay-footer #overlay-save { margin-left: auto; }
    #persona-overlay-textarea { flex: 1; min-height: 0; width: 100%; resize: none; font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; font-size: 15px; line-height: 1.5; }
    @media (max-width: 599px) { main { padding: 10px 10px 84px; } .card { padding: 14px; } #persona { min-height: 320px; } .persona-actions { display: grid; grid-template-columns: 1fr 1fr; } .persona-actions #save { grid-column: 1 / -1; margin-left: 0; } .overlay-footer { flex-wrap: wrap; } .overlay-footer #overlay-save { margin-left: 0; } }
  </style>
</head>
<body>
  <main>
    <div class="topbar"><h1>Bot settings</h1></div>
    <p class="intro">Admin-only settings. API keys are kept on the bot and are never sent to this page.</p>
    <nav class="tabs" aria-label="Settings sections"><button type="button" data-scroll-target="persona-section">Persona</button><button type="button" data-scroll-target="behavior-section">Behavior</button><button type="button" data-scroll-target="environment-section">Environment</button></nav>
    <form id="settings-form">
      <section id="persona-section" class="card persona-card"><h2>Persona prompt</h2><p class="hint">This prompt defines the bot's personality and how it speaks in chat. Changes stay here until you save.</p><p id="default-persona-note" class="hint hidden">This is the built-in default. Saving it makes it a custom prompt.</p><div class="field"><div class="field-label"><label for="persona">Prompt</label><button id="edit-fullscreen-compact" type="button" class="secondary">Edit fullscreen</button></div><textarea id="persona" maxlength="30000"></textarea><div id="persona-counter" class="counter" aria-live="polite">0 / 30000</div></div><div class="persona-actions"><button id="copy-persona" type="button" class="secondary">Copy to clipboard</button><button id="edit-fullscreen" type="button" class="secondary">Edit fullscreen</button><button id="reset-persona" type="button" class="secondary">Reset to default</button><button id="save" type="submit">Save changes</button></div></section>
      <section id="behavior-section" class="card"><h2>Global behavior</h2>
        <div class="field"><label for="reply_chance">Reply chance (%)</label><input id="reply_chance" type="number" min="0" max="100" step="1"></div>
        <div class="field"><label for="reaction_chance">Reaction chance (%)</label><input id="reaction_chance" type="number" min="0" max="100" step="1"></div>
        <div class="field"><label for="cooldown_threshold">Cooldown threshold (messages)</label><input id="cooldown_threshold" type="number" min="0" max="200" step="1"></div>
        <div class="field"><label for="max_ping_pong">Maximum ping-pong replies</label><input id="max_ping_pong" type="number" min="0" max="20" step="1"></div>
        <div class="field"><label for="media_reply_guidance">Media reply guidance</label><textarea id="media_reply_guidance" maxlength="500" style="min-height:100px"></textarea></div>
        <div class="switch"><label for="paused">Bot paused</label><input id="paused" type="checkbox"></div>
        <div class="switch"><label for="utils_disabled">Disable video/audio utilities</label><input id="utils_disabled" type="checkbox"></div>
      </section>
      <section id="environment-section" class="card"><h2>Environment</h2><div id="environment"></div></section>
    </form>
  </main>
  <div id="status" role="status"></div>
  <div id="persona-overlay" class="hidden" role="dialog" aria-modal="true" aria-labelledby="persona-overlay-title">
    <div class="overlay-header"><h2 id="persona-overlay-title">Edit persona prompt</h2><button id="close-overlay" type="button" class="secondary">✕ Close</button></div>
    <textarea id="persona-overlay-textarea" maxlength="30000" spellcheck="false" aria-label="Persona prompt"></textarea>
    <div id="overlay-counter" class="counter" aria-live="polite">0 / 30000</div>
    <div class="overlay-footer"><button id="overlay-reset" type="button" class="secondary">Reset to default</button><button id="overlay-save" type="button">Save</button></div>
  </div>
  <script>
    const telegram = window.Telegram && window.Telegram.WebApp;
    const api = '/api/settings';
    const form = document.getElementById('settings-form');
    const save = document.getElementById('save');
    const status = document.getElementById('status');
    const persona = document.getElementById('persona');
    const personaCounter = document.getElementById('persona-counter');
    const personaOverlay = document.getElementById('persona-overlay');
    const overlayTextarea = document.getElementById('persona-overlay-textarea');
    const overlayCounter = document.getElementById('overlay-counter');
    let initial = null;
    function show(message, kind = 'ok') { status.textContent = message; status.className = kind; }
    function changed(id, value) { return initial && initial[id] !== value; }
    async function request(method, data, path = api) {
      const response = await fetch(path, { method, headers: { 'Content-Type': 'application/json', 'X-Telegram-Init-Data': telegram ? telegram.initData : '' }, body: data ? JSON.stringify(data) : undefined });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.error || body.message || 'Request failed.');
      return body;
    }
    function addEnvironmentField(entry) {
      const field = document.createElement('div'); field.className = 'field';
      const input = document.createElement('input'); input.id = 'env-' + entry.key; input.dataset.envKey = entry.key;
      const label = document.createElement('label'); label.htmlFor = input.id; label.textContent = entry.key + (entry.restart_required ? ' (restart required)' : '');
      field.append(label);
      if (entry.is_secret) { input.type = 'password'; input.autocomplete = 'off'; input.placeholder = entry.is_set ? 'Saved: ' + entry.display_value + ' — enter a replacement' : 'Not set'; const note = document.createElement('div'); note.className = 'secret-note'; note.textContent = 'Saved value stays private; leave blank to keep it unchanged.'; field.append(note); }
      else { input.type = 'text'; input.value = entry.value || ''; }
      field.append(input); document.getElementById('environment').append(field);
    }
    function setField(id, value) { document.getElementById(id).value = value; initial[id] = String(value); }
    function updateCounter(textarea, counter) { const length = textarea.value.length; counter.textContent = length + ' / 30000'; counter.classList.toggle('warning', length > 27000); }
    function updatePersonaCounter() { updateCounter(persona, personaCounter); }
    function updateOverlayCounter() { updateCounter(overlayTextarea, overlayCounter); }
    function showPersonaOverlay() { overlayTextarea.value = persona.value; updateOverlayCounter(); personaOverlay.classList.remove('hidden'); overlayTextarea.focus(); }
    function hidePersonaOverlay() { personaOverlay.classList.add('hidden'); }
    function populate(data) {
      initial = {};
      data.environment.forEach(addEnvironmentField);
      data.environment.forEach(entry => { initial['env-' + entry.key] = entry.is_secret ? '' : String(entry.value || ''); });
      setField('persona', data.persona_prompt); updatePersonaCounter(); document.getElementById('default-persona-note').classList.toggle('hidden', !data.persona_uses_default);
      setField('reply_chance', Math.round(data.behavior.reply_chance * 100)); setField('reaction_chance', Math.round(data.behavior.reaction_chance * 100));
      setField('cooldown_threshold', data.behavior.cooldown_threshold); setField('max_ping_pong', data.behavior.max_ping_pong); setField('media_reply_guidance', data.behavior.media_reply_guidance || '');
      for (const id of ['paused', 'utils_disabled']) { const el = document.getElementById(id); el.checked = Boolean(data[id]); initial[id] = el.checked; }
    }
    async function load() { if (!telegram || !telegram.initData) throw new Error('Open this page from the bot\'s Settings button in Telegram.'); telegram.ready(); telegram.expand(); populate(await request('GET')); }
    persona.addEventListener('input', updatePersonaCounter);
    overlayTextarea.addEventListener('input', () => { persona.value = overlayTextarea.value; updatePersonaCounter(); updateOverlayCounter(); });
    document.querySelectorAll('#edit-fullscreen, #edit-fullscreen-compact').forEach(button => button.addEventListener('click', showPersonaOverlay));
    document.getElementById('close-overlay').addEventListener('click', hidePersonaOverlay);
    document.addEventListener('keydown', event => { if (event.key === 'Escape' && !personaOverlay.classList.contains('hidden')) hidePersonaOverlay(); });
    document.getElementById('overlay-save').addEventListener('click', () => { if (form.requestSubmit) form.requestSubmit(); else form.dispatchEvent(new Event('submit', { cancelable: true })); });
    document.querySelectorAll('[data-scroll-target]').forEach(button => button.addEventListener('click', () => document.getElementById(button.dataset.scrollTarget).scrollIntoView({ behavior: 'smooth', block: 'start' })));
    document.getElementById('copy-persona').addEventListener('click', async () => { try { await navigator.clipboard.writeText(persona.value); show('Persona copied to clipboard.'); } catch { show('Could not copy the persona. Select and copy it manually.', 'error'); } });
    async function resetPersona() { if (!window.confirm('Replace the editor contents with the built-in default persona? You still need to save to apply it.')) return; try { const data = await request('GET', undefined, '/api/default_persona'); persona.value = data.persona; overlayTextarea.value = data.persona; updatePersonaCounter(); updateOverlayCounter(); document.getElementById('default-persona-note').classList.remove('hidden'); show('Default persona loaded. Save changes to apply it.'); } catch (error) { show(error.message, 'error'); } }
    document.getElementById('reset-persona').addEventListener('click', resetPersona);
    document.getElementById('overlay-reset').addEventListener('click', resetPersona);
    form.addEventListener('submit', async event => {
      event.preventDefault(); if (!initial) return; const payload = {}; const env = {};
      document.querySelectorAll('[data-env-key]').forEach(input => { if (input.value && changed(input.id, input.value)) env[input.dataset.envKey] = input.value; });
      if (Object.keys(env).length) payload.environment = env;
      const persona = document.getElementById('persona').value; if (changed('persona', persona)) payload.persona_prompt = persona;
      const behavior = {}; for (const id of ['reply_chance', 'reaction_chance', 'cooldown_threshold', 'max_ping_pong', 'media_reply_guidance']) { const value = document.getElementById(id).value; if (changed(id, value)) behavior[id] = value; }
      if (Object.keys(behavior).length) payload.behavior = behavior;
      for (const id of ['paused', 'utils_disabled']) { const value = document.getElementById(id).checked; if (changed(id, value)) payload[id] = value; }
      if (!Object.keys(payload).length) { show('No changes to save.'); return; }
      save.disabled = true; try { const result = await request('POST', payload); show(result.message + (result.restart_required ? ' Restart the bot to apply the marked environment changes.' : '')); await load(); } catch (error) { show(error.message, 'error'); } finally { save.disabled = false; }
    });
    load().catch(error => { form.classList.add('hidden'); show(error.message, 'error'); });
  </script>
</body>
</html>'''


def render_cookies_html() -> str:
    """Return the cookie replacement UI; it intentionally contains no saved data."""
    return r'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>Freak cookies</title>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <style>
    :root { color-scheme: light dark; --bg: var(--tg-theme-bg-color, #10131a); --card: var(--tg-theme-secondary-bg-color, #1a202b); --text: var(--tg-theme-text-color, #edf1f7); --hint: var(--tg-theme-hint-color, #98a2b3); --accent: var(--tg-theme-button-color, #61a8ff); --accent-text: var(--tg-theme-button-text-color, #fff); --bad: var(--tg-theme-destructive-text-color, #f97066); --good: var(--tg-theme-accent-text-color, #4bb57b); }
    * { box-sizing: border-box; } body { margin: 0; background: var(--bg); color: var(--text); font: 16px/1.45 system-ui, sans-serif; }
    main { max-width: 760px; margin: auto; padding: calc(env(safe-area-inset-top, 0px) + 18px) 14px calc(env(safe-area-inset-bottom, 0px) + 94px); }
    h1 { font-size: 24px; margin: 0 0 4px; } h2 { font-size: 18px; margin: 0 0 8px; } .hint { color: var(--hint); } .card { background: var(--card); border-radius: 14px; padding: 16px; margin: 14px 0; }
    .field { display: grid; gap: 6px; margin: 12px 0; } label { font-weight: 650; } select, textarea { width: 100%; border: 1px solid color-mix(in srgb, var(--hint) 45%, transparent); background: color-mix(in srgb, var(--bg) 72%, transparent); color: var(--text); border-radius: 9px; padding: 10px; font: inherit; }
    textarea { min-height: 280px; resize: vertical; font: 13px/1.35 ui-monospace, "SF Mono", Menlo, Consolas, monospace; } .actions { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 12px; } button { border: 0; border-radius: 10px; padding: 11px 13px; background: var(--accent); color: var(--accent-text); font: 700 16px system-ui, sans-serif; cursor: pointer; } button.secondary { background: color-mix(in srgb, var(--hint) 24%, transparent); color: var(--text); } button.danger { background: transparent; color: var(--bad); border: 1px solid color-mix(in srgb, var(--bad) 55%, transparent); } button:disabled { opacity: .55; cursor: wait; }
    #cookie-status { margin: 8px 0; } .ok { color: var(--good); } .error { color: var(--bad); } .hidden { display: none; } #summary { display: grid; gap: 8px; } .summary-row { display: flex; justify-content: space-between; gap: 12px; border-top: 1px solid color-mix(in srgb, var(--hint) 24%, transparent); padding-top: 8px; } .summary-row:first-child { border-top: 0; padding-top: 0; }
  </style>
</head>
<body>
  <main>
    <h1>Download cookies</h1>
    <p class="hint">Paste a Netscape <code>cookies.txt</code> export to replace one service’s jar. Saved cookie values are never shown or sent back to this page.</p>
    <section class="card"><h2>Saved jars</h2><div id="summary" class="hint">Loading…</div></section>
    <form id="cookie-form" class="card">
      <h2>Replace cookies</h2>
      <div class="field"><label for="service">Service</label><select id="service" required></select></div>
      <div class="field"><label for="content">cookies.txt content</label><textarea id="content" maxlength="524288" placeholder="# Netscape HTTP Cookie File&#10;.youtube.com&#9;TRUE&#9;/&#9;TRUE&#9;..." spellcheck="false" required></textarea><small class="hint">Export from the browser profile where you are logged in. JSON exports will not work.</small></div>
      <div id="cookie-status" role="status"></div>
      <div class="actions"><button id="save" type="submit">Save cookies</button><button id="clear" class="secondary" type="button">Clear paste</button><button id="remove" class="danger" type="button">Remove saved jar</button></div>
    </form>
  </main>
  <script>
    const telegram = window.Telegram && window.Telegram.WebApp;
    const serviceSelect = document.getElementById('service'); const content = document.getElementById('content'); const status = document.getElementById('cookie-status'); const summary = document.getElementById('summary'); const form = document.getElementById('cookie-form');
    let snapshot = null;
    function show(message, kind = 'ok') { status.textContent = message; status.className = kind; }
    async function request(method, data) { const response = await fetch('/api/cookies', { method, headers: { 'Content-Type': 'application/json', 'X-Telegram-Init-Data': telegram ? telegram.initData : '' }, body: data ? JSON.stringify(data) : undefined }); const body = await response.json().catch(() => ({})); if (!response.ok) throw new Error(body.error || body.message || 'Request failed.'); return body; }
    function renderSummary(data) { summary.replaceChildren(); data.services.forEach(item => { const row = document.createElement('div'); row.className = 'summary-row'; const name = document.createElement('strong'); name.textContent = item.label; const detail = document.createElement('span'); if (!item.present) detail.textContent = 'not saved'; else if (item.error) detail.textContent = item.error; else { const sessions = item.session_cookie_names.length ? '; session: ' + item.session_cookie_names.join(', ') : '; no session cookies detected'; detail.textContent = item.valid_rows + ' rows' + sessions; } row.append(name, detail); summary.append(row); }); }
    function populate(data) { snapshot = data; serviceSelect.replaceChildren(); data.services.forEach(item => { const option = document.createElement('option'); option.value = item.service; option.textContent = item.label; serviceSelect.append(option); }); const requested = new URLSearchParams(location.search).get('service'); if (requested && [...serviceSelect.options].some(option => option.value === requested)) { serviceSelect.value = requested; history.replaceState(null, '', location.pathname); } renderSummary(data); }
    async function load() { if (!telegram || !telegram.initData) throw new Error('Open this page from the bot\'s Cookies button in Telegram.'); telegram.ready(); telegram.expand(); populate(await request('GET')); }
    form.addEventListener('submit', async event => { event.preventDefault(); const save = document.getElementById('save'); save.disabled = true; try { const result = await request('POST', { service: serviceSelect.value, content: content.value }); content.value = ''; show(result.message + (result.session_cookie_names.length ? ' Session cookies: ' + result.session_cookie_names.join(', ') + '.' : ' No common session cookie was detected.'), 'ok'); await load(); } catch (error) { show(error.message, 'error'); } finally { save.disabled = false; } });
    document.getElementById('clear').addEventListener('click', () => { content.value = ''; content.focus(); });
    document.getElementById('remove').addEventListener('click', async () => { const selected = serviceSelect.options[serviceSelect.selectedIndex]; if (!selected || !window.confirm('Remove saved cookies for ' + selected.text + '?')) return; try { const result = await request('DELETE', { service: serviceSelect.value }); show(result.message); await load(); } catch (error) { show(error.message, 'error'); } });
    load().catch(error => { form.classList.add('hidden'); show(error.message, 'error'); });
  </script>
</body>
</html>'''
