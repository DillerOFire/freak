import json

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
        "memory_query": "hello search",
        "system_prompt": "You are a test bot.",
        "context_prompt": "<conversation_context>hi</conversation_context>",
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
        "active_event_states": [
            {
                "id": 9,
                "state_key": "ignore",
                "value": "cold shoulder Alice for an hour",
                "expires_at": "2026-08-07T12:00:00Z",
                "target_user_id": 123,
                "target_username": "Alice",
                "reason": "spam",
            }
        ],
        "pending_scheduled_actions": [
            {
                "id": 3,
                "action_type": "reply",
                "execute_at": "2026-08-07T18:00:00Z",
                "reason": "follow up later",
                "instruction": "check if Alice calmed down",
                "target_user_id": 123,
            }
        ],
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
        "saved_media_options": [
            {
                "media_unique_id": "photo_u1",
                "media_type": "photo",
                "description": "dramatic portrait",
                "use_count": 2,
                "is_favorite": True,
            }
        ],
        "saved_media_option_count": 1,
        "saved_media_policy": {"mode": "normal", "max_items": 1, "guidance": "use sparingly"},
        "prompt_tokens": 42,
        "prompt_cached_tokens": 24,
        "prompt_cache_write_tokens": 11,
        "uncached_prompt_tokens": 18,
        "prompt_cache_hit_rate": 24 / 42,
        "response_id": "generation-1",
        "response_model": "provider-model",
        "provider": "provider-a",
        "system_prompt_hash": "system-hash",
        "context_prompt_hash": "context-hash",
        "cache_prefix_hash": "prefix-hash",
        "cache_prefix_chars": 500,
        "cache_stable_message_count": 20,
        "prompt_sections": {"working_memory": {"chars": 100, "sha256": "abc"}},
        "response_attempt_count": 2,
        "completion_tokens": 7,
        "total_tokens": 49,
    }
    await record_llm_telemetry(event)

    fetched = await fetch_llm_telemetry(chat_id=111)
    assert len(fetched) == 1
    row = fetched[0]
    assert row["chat_id"] == 111
    assert row["status"] == "success"
    assert row["memory_query"] == "hello search"
    assert row["system_prompt"] == "You are a test bot."
    assert row["context_prompt"] == "<conversation_context>hi</conversation_context>"
    assert row["trigger_messages"][0]["text"] == "Hello bot"
    assert row["used_user_thoughts"] == {"Alice": "Needs help"}
    assert row["used_general_memories"] == ["Topic: Greeting, Summary: hello"]
    assert row["tool_calls"] == [
        {"name": "add_general_memory", "arguments": {"topic": "Greeting"}}
    ]
    assert row["memory_writes"][0]["status"] == "succeeded"
    assert row["response_messages"] == ["Hi there!"]
    assert row["active_event_states"][0]["state_key"] == "ignore"
    assert row["pending_scheduled_actions"][0]["action_type"] == "reply"
    assert row["response_media"] == {"media_unique_id": "photo_u1", "media_type": "photo", "description": "some image"}
    assert row["prompt_cached_tokens"] == 24
    assert row["prompt_cache_write_tokens"] == 11
    assert row["uncached_prompt_tokens"] == 18
    assert row["prompt_cache_hit_rate"] == pytest.approx(24 / 42)
    assert row["response_id"] == "generation-1"
    assert row["response_model"] == "provider-model"
    assert row["provider"] == "provider-a"
    assert row["cache_prefix_hash"] == "prefix-hash"
    assert row["cache_prefix_chars"] == 500
    assert row["cache_stable_message_count"] == 20
    assert row["prompt_sections"]["working_memory"]["chars"] == 100
    assert row["response_attempt_count"] == 2
    assert row["saved_media_options"] == [
        {
            "media_unique_id": "photo_u1",
            "media_type": "photo",
            "description": "dramatic portrait",
            "use_count": 2,
            "is_favorite": True,
        }
    ]
    assert row["saved_media_option_count"] == 1
    assert row["saved_media_policy"]["max_items"] == 1

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


@pytest.mark.asyncio
async def test_record_llm_telemetry_defaults_missing_prompt_cached_tokens(temp_db_path):
    await record_llm_telemetry(
        {
            "chat_id": 3,
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

    fetched = await fetch_llm_telemetry(chat_id=3)
    assert len(fetched) == 1
    assert fetched[0]["prompt_cached_tokens"] is None


@pytest.mark.asyncio
async def test_record_llm_telemetry_accepts_pre_serialized_json_lists(temp_db_path):
    """Legacy callers may pass tool_calls/memory_writes already JSON-encoded."""
    await record_llm_telemetry(
        {
            "chat_id": 4,
            "source": "message",
            "status": "success",
            "trigger_messages": [],
            "used_user_thoughts": {},
            "used_general_memories": [],
            "tool_calls": json.dumps(
                [{"name": "ponder", "arguments": {"query": "x"}}],
                ensure_ascii=False,
            ),
            "memory_writes": json.dumps(
                [{"type": "ponder", "status": "succeeded"}],
                ensure_ascii=False,
            ),
            "response_messages": [],
        }
    )

    fetched = await fetch_llm_telemetry(chat_id=4)
    assert len(fetched) == 1
    assert fetched[0]["tool_calls"] == [{"name": "ponder", "arguments": {"query": "x"}}]
    assert fetched[0]["memory_writes"] == [{"type": "ponder", "status": "succeeded"}]
