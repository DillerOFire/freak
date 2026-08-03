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
import time
from collections.abc import Mapping
from urllib.parse import parse_qsl

from aiohttp import web

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

logger = logging.getLogger(__name__)

INIT_DATA_HEADER = "X-Telegram-Init-Data"
MAX_JSON_BODY_BYTES = 96 * 1024
MAX_PERSONA_PROMPT_LENGTH = 30_000


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


def create_settings_web_app() -> web.Application:
    app = web.Application(client_max_size=MAX_JSON_BODY_BYTES)
    app.router.add_get("/", settings_page)
    app.router.add_get("/settings", settings_page)
    app.router.add_get("/settings/", settings_page)
    app.router.add_get("/api/settings", api_get_settings)
    app.router.add_get("/api/default_persona", api_get_default_persona)
    app.router.add_post("/api/settings", api_save_settings)
    return app


async def start_settings_web_server() -> web.AppRunner:
    """Start the embedded settings listener and return its runner for shutdown."""
    app = create_settings_web_app()
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


def render_settings_html() -> str:
    """Return the self-contained Web App UI (no secrets are interpolated)."""
    return r'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Freak settings</title>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <style>
    :root { color-scheme: light dark; --bg: var(--tg-theme-bg-color, #10131a); --card: var(--tg-theme-secondary-bg-color, #1a202b); --text: var(--tg-theme-text-color, #edf1f7); --hint: var(--tg-theme-hint-color, #98a2b3); --accent: var(--tg-theme-button-color, #61a8ff); --accent-text: var(--tg-theme-button-text-color, #fff); --bad: var(--tg-theme-destructive-text-color, #f97066); --good: var(--tg-theme-accent-text-color, #4bb57b); }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--bg); color: var(--text); font: 16px/1.45 system-ui, sans-serif; }
    main { width: 100%; margin: auto; padding: 14px 14px 94px; }
    h1 { font-size: 24px; margin: 0 0 4px; } h2 { font-size: 18px; margin: 0 0 12px; }
    .intro, .hint { color: var(--hint); } .intro { margin: 0 0 18px; }
    .topbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; max-width: 760px; margin: auto; }
    .tabs { display: flex; gap: 8px; overflow-x: auto; max-width: 760px; margin: 14px auto; padding-bottom: 2px; }
    .card { background: var(--card); border-radius: 14px; padding: 16px; max-width: 760px; margin: 14px auto; }
    .persona-card { max-width: none; }
    .field { display: grid; gap: 6px; margin: 12px 0; }
    .field label { font-weight: 650; } .field small { color: var(--hint); }
    input, textarea { width: 100%; border: 1px solid color-mix(in srgb, var(--hint) 45%, transparent); background: color-mix(in srgb, var(--bg) 72%, transparent); color: var(--text); border-radius: 9px; padding: 10px; font: inherit; }
    textarea { min-height: 220px; resize: vertical; } #persona { min-height: 60vh; max-height: 85vh; font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; line-height: 1.5; } input[type=number] { max-width: 180px; }
    .switch { display: flex; align-items: center; justify-content: space-between; gap: 14px; padding: 10px 0; } .switch input { width: 22px; height: 22px; }
    .secret-note { color: var(--hint); margin: -2px 0 4px; font-size: 13px; }
    #status { position: fixed; left: 14px; right: 14px; bottom: 14px; max-width: 732px; margin: auto; padding: 12px 14px; border-radius: 10px; background: var(--card); box-shadow: 0 5px 30px #0006; display: none; }
    #status.ok { color: var(--good); display: block; } #status.error { color: var(--bad); display: block; }
    button { border: 0; border-radius: 10px; padding: 11px 13px; background: var(--accent); color: var(--accent-text); font: 700 16px system-ui, sans-serif; cursor: pointer; }
    .topbar button, .tabs button, .secondary { width: auto; background: color-mix(in srgb, var(--hint) 24%, transparent); color: var(--text); font-size: 14px; }
    .tabs button { white-space: nowrap; } .persona-actions { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-top: 12px; } .persona-actions #save { margin-left: auto; }
    .counter { color: var(--hint); font-size: 13px; text-align: right; } .counter.warning { color: var(--bad); font-weight: 700; }
    button:disabled { opacity: .55; cursor: wait; } .hidden { display: none; }
    @media (max-width: 599px) { main { padding: 10px 10px 84px; } .card { padding: 14px; } #persona { min-height: 62vh; } .persona-actions { display: grid; grid-template-columns: 1fr 1fr; } .persona-actions #save { grid-column: 1 / -1; margin-left: 0; } }
  </style>
</head>
<body>
  <main>
    <div class="topbar"><h1>Bot settings</h1><button id="exit-fullscreen" type="button" class="hidden">Exit fullscreen</button></div>
    <p class="intro">Admin-only settings. API keys are kept on the bot and are never sent to this page.</p>
    <nav class="tabs" aria-label="Settings sections"><button type="button" data-scroll-target="persona-section">Persona</button><button type="button" data-scroll-target="behavior-section">Behavior</button><button type="button" data-scroll-target="environment-section">Environment</button></nav>
    <form id="settings-form">
      <section id="persona-section" class="card persona-card"><h2>Persona prompt</h2><p class="hint">This prompt defines the bot's personality and how it speaks in chat. Changes stay here until you save.</p><p id="default-persona-note" class="hint hidden">This is the built-in default. Saving it makes it a custom prompt.</p><div class="field"><label for="persona">Prompt</label><textarea id="persona" maxlength="30000"></textarea><div id="persona-counter" class="counter" aria-live="polite">0 / 30000</div></div><div class="persona-actions"><button id="copy-persona" type="button" class="secondary">Copy to clipboard</button><button id="reset-persona" type="button" class="secondary">Reset to default</button><button id="save" type="submit">Save changes</button></div></section>
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
  <script>
    const telegram = window.Telegram && window.Telegram.WebApp;
    const api = '/api/settings';
    const form = document.getElementById('settings-form');
    const save = document.getElementById('save');
    const status = document.getElementById('status');
    const persona = document.getElementById('persona');
    const personaCounter = document.getElementById('persona-counter');
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
    function updatePersonaCounter() { const length = persona.value.length; personaCounter.textContent = length + ' / 30000'; personaCounter.classList.toggle('warning', length > 27000); }
    function populate(data) {
      initial = {};
      data.environment.forEach(addEnvironmentField);
      data.environment.forEach(entry => { initial['env-' + entry.key] = entry.is_secret ? '' : String(entry.value || ''); });
      setField('persona', data.persona_prompt); updatePersonaCounter(); document.getElementById('default-persona-note').classList.toggle('hidden', !data.persona_uses_default);
      setField('reply_chance', Math.round(data.behavior.reply_chance * 100)); setField('reaction_chance', Math.round(data.behavior.reaction_chance * 100));
      setField('cooldown_threshold', data.behavior.cooldown_threshold); setField('max_ping_pong', data.behavior.max_ping_pong); setField('media_reply_guidance', data.behavior.media_reply_guidance || '');
      for (const id of ['paused', 'utils_disabled']) { const el = document.getElementById(id); el.checked = Boolean(data[id]); initial[id] = el.checked; }
    }
    async function load() { if (!telegram || !telegram.initData) throw new Error('Open this page from the bot\'s Settings button in Telegram.'); telegram.ready(); telegram.expand(); telegram.isVerticalSwipesEnabled = false; if (telegram.requestFullscreen) { telegram.requestFullscreen(); document.getElementById('exit-fullscreen').classList.remove('hidden'); } populate(await request('GET')); }
    persona.addEventListener('input', updatePersonaCounter);
    document.querySelectorAll('[data-scroll-target]').forEach(button => button.addEventListener('click', () => document.getElementById(button.dataset.scrollTarget).scrollIntoView({ behavior: 'smooth', block: 'start' })));
    document.getElementById('exit-fullscreen').addEventListener('click', () => { if (telegram && telegram.exitFullscreen) telegram.exitFullscreen(); });
    document.getElementById('copy-persona').addEventListener('click', async () => { try { await navigator.clipboard.writeText(persona.value); show('Persona copied to clipboard.'); } catch { show('Could not copy the persona. Select and copy it manually.', 'error'); } });
    document.getElementById('reset-persona').addEventListener('click', async () => { if (!window.confirm('Replace the editor contents with the built-in default persona? You still need to save to apply it.')) return; try { const data = await request('GET', undefined, '/api/default_persona'); persona.value = data.persona; updatePersonaCounter(); document.getElementById('default-persona-note').classList.remove('hidden'); show('Default persona loaded. Save changes to apply it.'); } catch (error) { show(error.message, 'error'); } });
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
