import hashlib
import hmac
import json
import time
from urllib.parse import urlencode
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from bot import settings_web


def _signed_init_data(*, token: str, user_id: int, auth_date: int) -> str:
    fields = {
        "auth_date": str(auth_date),
        "query_id": "test-query",
        "user": json.dumps({"id": user_id, "first_name": "Admin"}),
    }
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_validate_telegram_web_app_init_data_accepts_fresh_admin_data():
    now = int(time.time())
    init_data = _signed_init_data(token="test-token", user_id=42, auth_date=now)

    user = settings_web.validate_telegram_web_app_init_data(
        init_data,
        bot_token="test-token",
        admin_id=42,
        max_age_seconds=3600,
        now=now,
    )

    assert user["id"] == 42


def test_validate_telegram_web_app_init_data_rejects_tampering_and_non_admin():
    now = int(time.time())
    init_data = _signed_init_data(token="test-token", user_id=42, auth_date=now)

    with pytest.raises(settings_web.WebSettingsAuthError, match="signature"):
        settings_web.validate_telegram_web_app_init_data(
            init_data.replace("test-query", "tampered"),
            bot_token="test-token",
            admin_id=42,
            max_age_seconds=3600,
            now=now,
        )
    with pytest.raises(settings_web.WebSettingsAuthError, match="only available"):
        settings_web.validate_telegram_web_app_init_data(
            init_data,
            bot_token="test-token",
            admin_id=99,
            max_age_seconds=3600,
            now=now,
        )


@pytest.mark.asyncio
async def test_settings_snapshot_masks_saved_secrets():
    with (
        patch(
            "bot.settings_web.get_env_entries",
            return_value={"LLM_API_KEY": "super-secret-value", "LLM_MODEL": "test/model"},
        ),
        patch("bot.settings_web.get_config", new_callable=AsyncMock, return_value="Custom persona"),
        patch(
            "bot.settings_web.get_behavior_settings",
            new_callable=AsyncMock,
            return_value={"reply_chance": 0.05},
        ),
        patch("bot.settings_web.get_utils_disabled", new_callable=AsyncMock, return_value=False),
        patch("bot.settings_web.get_paused", return_value=False),
    ):
        snapshot = await settings_web.build_settings_snapshot()

    api_entry = next(entry for entry in snapshot["environment"] if entry["key"] == "LLM_API_KEY")
    assert api_entry["value"] is None
    assert "super-secret-value" not in json.dumps(snapshot)
    assert snapshot["persona_prompt"] == "Custom persona"


@pytest.mark.asyncio
async def test_settings_update_rejects_non_admin():
    with patch("bot.settings_web.config.ADMIN_ID", 42):
        with pytest.raises(settings_web.WebSettingsAuthError, match="only available"):
            await settings_web.apply_settings_update({"paused": True}, requesting_user_id=99)


def test_rendered_settings_includes_fullscreen_persona_editor_overlay():
    html = settings_web.render_settings_html()

    assert "persona-overlay" in html
    assert "edit-fullscreen" in html
    assert "viewport-fit=cover" in html
    assert "env(safe-area-inset-top" in html
    assert "requestFullscreen" not in html
    assert "reset-persona" in html
    assert "/api/default_persona" in html


@pytest.mark.asyncio
async def test_default_persona_endpoint_requires_admin_init_data():
    now = int(time.time())
    valid_init_data = _signed_init_data(token="test-token", user_id=42, auth_date=now)
    with patch("bot.settings_web.config.TELEGRAM_BOT_TOKEN", "test-token"), patch(
        "bot.settings_web.config.ADMIN_ID", 42
    ):
        with pytest.raises(web.HTTPUnauthorized):
            await settings_web.api_get_default_persona(
                make_mocked_request("GET", "/api/default_persona")
            )

        with patch("bot.llm.DEFAULT_PERSONA", "Built-in test persona"):
            response = await settings_web.api_get_default_persona(
                make_mocked_request(
                    "GET",
                    "/api/default_persona",
                    headers={settings_web.INIT_DATA_HEADER: valid_init_data},
                )
            )

    assert json.loads(response.text) == {"persona": "Built-in test persona"}
