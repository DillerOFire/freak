import json
import urllib.request
import urllib.error

import pytest

from bot.telemetry import (
    init_telemetry_db,
    record_llm_telemetry,
    start_telemetry_dashboard,
)


@pytest.mark.asyncio
async def test_telemetry_dashboard_routes(temp_db_path):
    await record_llm_telemetry(
        {
            "chat_id": 555,
            "source": "message",
            "status": "success",
            "trigger_messages": [{"text": "hi"}],
            "used_user_thoughts": {},
            "used_general_memories": [],
            "tool_calls": [],
            "memory_writes": [],
            "response_messages": ["hello"],
        }
    )

    # fetch the id
    from bot.telemetry import fetch_llm_telemetry

    events = await fetch_llm_telemetry(chat_id=555)
    event_id = events[0]["id"]

    server = start_telemetry_dashboard("127.0.0.1", 0, token="secret")
    port = server.server_address[1]
    base = f"http://127.0.0.1:{port}"
    try:
        # /telemetry with token
        with urllib.request.urlopen(f"{base}/telemetry?token=secret") as r:
            assert r.status == 200
            body = r.read().decode("utf-8")
            assert "Bot Telemetry Dashboard" in body

        # /telemetry/export.json with token
        with urllib.request.urlopen(f"{base}/telemetry/export.json?token=secret") as r:
            assert r.status == 200
            data = json.loads(r.read().decode("utf-8"))
            assert data["generated_for"] == "llm_context_engineering_review"

        # /telemetry/event/<id>.json with token
        with urllib.request.urlopen(
            f"{base}/telemetry/event/{event_id}.json?token=secret"
        ) as r:
            assert r.status == 200
            ev = json.loads(r.read().decode("utf-8"))
            assert ev["id"] == event_id
            assert ev["chat_id"] == 555
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.asyncio
async def test_telemetry_dashboard_unauthorized(temp_db_path):
    server = start_telemetry_dashboard("127.0.0.1", 0, token="secret")
    port = server.server_address[1]
    base = f"http://127.0.0.1:{port}"
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"{base}/telemetry")
        assert exc.value.code == 401
    finally:
        server.shutdown()
        server.server_close()
