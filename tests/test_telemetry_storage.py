import pytest
from bot.telemetry import (
    init_telemetry_db,
    record_llm_telemetry,
    fetch_llm_telemetry,
    fetch_llm_telemetry_event,
    get_telemetry_chats,
)


@pytest.mark.asyncio
async def test_record_and_fetch_llm_telemetry(temp_db_path):
    event = {
        "chat_id": 111,
        "source": "message",
        "model": "test-model",
        "status": "success",
        "trigger_messages": [
            {"message_id": 1, "sender": "Alice", "user_id": 123, "text": "Hello bot"}
        ],
        "used_user_thoughts": {"Alice": "Needs help"},
        "used_general_memories": ["Topic: Greeting, Summary: hello"],
        "tool_calls": [
            {"name": "add_general_memory", "arguments": {"topic": "Greeting"}}
        ],
        "memory_writes": [
            {
                "type": "general_memory",
                "status": "succeeded",
                "arguments": {"topic": "Greeting", "chat_id": 111},
            }
        ],
        "response_messages": ["Hi there!"],
        "context_message_count": 1,
        "context_chars": 100,
        "system_prompt_chars": 500,
        "user_thought_count": 1,
        "retrieved_memory_count": 1,
        "tool_call_count": 1,
        "memory_write_count": 1,
        "failed_memory_write_count": 0,
        "response_message_count": 1,
        "response_chars": 8,
        "response_media": {"media_unique_id": "photo_u1", "media_type": "photo", "description": "some image"},
    }
    await record_llm_telemetry(event)

    fetched = await fetch_llm_telemetry(chat_id=111)
    assert len(fetched) == 1
    row = fetched[0]
    assert row["chat_id"] == 111
    assert row["status"] == "success"
    assert row["trigger_messages"][0]["text"] == "Hello bot"
    assert row["used_user_thoughts"] == {"Alice": "Needs help"}
    assert row["used_general_memories"] == ["Topic: Greeting, Summary: hello"]
    assert row["memory_writes"][0]["status"] == "succeeded"
    assert row["response_messages"] == ["Hi there!"]
    assert row["response_media"] == {"media_unique_id": "photo_u1", "media_type": "photo", "description": "some image"}

    chats = await get_telemetry_chats()
    assert chats == [111]


@pytest.mark.asyncio
async def test_fetch_filters_and_event_detail(temp_db_path):
    await record_llm_telemetry(
        {
            "chat_id": 1,
            "source": "message",
            "status": "success",
            "trigger_messages": [],
            "used_user_thoughts": {},
            "used_general_memories": [],
            "tool_calls": [],
            "memory_writes": [],
            "response_messages": [],
        }
    )
    await record_llm_telemetry(
        {
            "chat_id": 2,
            "source": "daily_task",
            "status": "invalid_json",
            "trigger_messages": [],
            "used_user_thoughts": {},
            "used_general_memories": [],
            "tool_calls": [],
            "memory_writes": [],
            "response_messages": [],
        }
    )

    only_chat2 = await fetch_llm_telemetry(chat_id=2)
    assert len(only_chat2) == 1
    assert only_chat2[0]["chat_id"] == 2

    only_invalid = await fetch_llm_telemetry(status="invalid_json")
    assert len(only_invalid) == 1
    assert only_invalid[0]["status"] == "invalid_json"

    only_daily = await fetch_llm_telemetry(source="daily_task")
    assert len(only_daily) == 1
    assert only_daily[0]["source"] == "daily_task"

    event_id = only_chat2[0]["id"]
    detail = await fetch_llm_telemetry_event(event_id)
    assert detail is not None
    assert detail["id"] == event_id
    assert detail["chat_id"] == 2

    missing = await fetch_llm_telemetry_event(99999)
    assert missing is None
