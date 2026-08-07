import pytest

from bot.telemetry import (
    build_telemetry_snapshot,
    parse_telemetry_filters,
    record_llm_telemetry,
)


@pytest.mark.asyncio
async def test_telemetry_web_app_snapshot_returns_filtered_events(temp_db_path):
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
            "response_media": {"media_unique_id": "photo_u1", "media_type": "photo", "description": "web test photo"},
        }
    )
    await record_llm_telemetry(
        {
            "chat_id": 555,
            "source": "ponder_agent",
            "status": "success",
            "trigger_messages": [],
            "used_user_thoughts": {},
            "used_general_memories": [],
            "tool_calls": [{"name": "web_search", "arguments": {"tool_input": "x"}}],
            "memory_writes": [],
            "response_messages": ["research done"],
            "prompt_tokens": 80,
            "prompt_cached_tokens": 40,
            "prompt_cache_hit_rate": 0.5,
        }
    )

    snapshot = await build_telemetry_snapshot(
        {"chat_id": "555", "status": "success", "limit": "50"}
    )

    assert snapshot["filters"] == {
        "chat_id": 555,
        "status": "success",
        "source": None,
        "limit": 50,
    }
    assert snapshot["summary"]["total_events"] == 2
    assert snapshot["main_summary"]["total_events"] == 1
    assert snapshot["ponder_summary"]["total_events"] == 1
    assert snapshot["main_event_count"] == 1
    assert snapshot["ponder_event_count"] == 1
    assert snapshot["events"][0]["source"] in {"message", "ponder_agent"}
    assert any(e["response_media"].get("media_unique_id") == "photo_u1" for e in snapshot["events"])
    assert snapshot["chats"] == [555]


def test_telemetry_web_app_filter_normalization():
    assert parse_telemetry_filters(
        {"chat_id": "not-a-chat", "status": "all", "source": "all", "limit": "999"}
    ) == {"chat_id": None, "status": None, "source": None, "limit": 500}
