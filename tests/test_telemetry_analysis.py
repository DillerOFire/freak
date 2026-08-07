from bot.telemetry.analysis import (
    summarize_telemetry,
    summarize_telemetry_by_role,
    partition_telemetry_by_role,
    is_ponder_telemetry_source,
    build_context_engineering_suggestions,
)
from bot.telemetry.export import build_llm_telemetry_export


def _success_event(eid, with_memory=True, response_count=1):
    return {
        "id": eid,
        "timestamp": f"2026-01-0{eid} 10:00:00",
        "chat_id": 1,
        "source": "message",
        "status": "success",
        "context_message_count": 3,
        "context_chars": 500,
        "system_prompt_chars": 1000,
        "user_thought_count": 1,
        "retrieved_memory_count": 2,
        "latency_ms": 800,
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "prompt_cached_tokens": 60,
        "prompt_cache_hit_rate": 0.6,
        "saved_media_option_count": 3 if with_memory else 0,
        "saved_media_options": (
            [
                {
                    "media_unique_id": "sticker_1",
                    "media_type": "sticker",
                    "description": "smug face",
                    "use_count": 1,
                    "is_favorite": True,
                }
            ]
            if with_memory
            else []
        ),
        "saved_media_policy": {"mode": "normal", "max_items": 1} if with_memory else {},
        "tool_calls": [{"name": "add_general_memory", "arguments": {"topic": "Opera"}}],
        "memory_writes": (
            [
                {
                    "type": "general_memory",
                    "status": "succeeded",
                    "arguments": {"topic": "Opera", "chat_id": 1},
                }
            ]
            if with_memory
            else []
        ),
        "response_messages": ["reply"] * response_count,
        "response_message_count": response_count,
        "memory_write_count": 1 if with_memory else 0,
        "failed_memory_write_count": 0,
        "tool_call_count": 1,
        "trigger_messages": [{"text": f"message {eid}"}],
        "active_event_states": [
            {
                "id": 1,
                "state_key": "ignore",
                "value": "ignore for an hour",
                "expires_at": "2026-01-01T11:00:00Z",
            }
        ]
        if with_memory
        else [],
        "pending_scheduled_actions": [
            {
                "id": 2,
                "action_type": "message",
                "execute_at": "2026-01-01T12:00:00Z",
                "reason": "check in",
                "instruction": "say hi later",
            }
        ]
        if with_memory
        else [],
    }


def _invalid_json_event(eid):
    return {
        "id": eid,
        "timestamp": f"2026-01-0{eid} 11:00:00",
        "chat_id": 1,
        "source": "message",
        "status": "invalid_json",
        "context_message_count": 2,
        "context_chars": 400,
        "system_prompt_chars": 1000,
        "user_thought_count": 0,
        "retrieved_memory_count": 1,
        "latency_ms": 300,
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
        "tool_calls": [],
        "memory_writes": [],
        "response_messages": [],
        "response_message_count": 0,
        "memory_write_count": 0,
        "failed_memory_write_count": 0,
        "tool_call_count": 0,
        "error_type": "JSONDecodeError",
        "error_message": "bad json",
        "trigger_messages": [{"text": "broken"}],
    }


def test_summarize_telemetry_rates_and_memory():
    events = [
        _success_event(1, with_memory=True),
        _success_event(2, with_memory=False),
        _invalid_json_event(3),
    ]
    summary = summarize_telemetry(events)

    assert summary["total_events"] == 3
    assert summary["status_counts"]["success"] == 2
    assert summary["status_counts"]["invalid_json"] == 1
    assert summary["failure_rate"] == 1 / 3
    assert summary["success_rate"] == 2 / 3

    topics = summary["top_memory_write_topics"]
    assert any(t["topic"] == "Opera" for t in topics)

    no_mem = summary["recent_no_memory_examples"]
    assert any(ex["id"] == 2 for ex in no_mem)

    assert summary["avg_prompt_tokens"] == 100  # ignores None
    assert summary["avg_prompt_cached_tokens"] == 60
    assert summary["avg_prompt_cache_hit_rate"] == 0.6
    assert summary["avg_saved_media_option_count"] == 1.5


def test_build_context_engineering_suggestions_json_contract():
    events = [_invalid_json_event(i) for i in range(1, 4)]
    suggestions = build_context_engineering_suggestions(events)
    joined = " ".join(suggestions)
    assert "JSON" in joined or "json" in joined


def test_build_context_engineering_suggestions_no_events():
    suggestions = build_context_engineering_suggestions([])
    assert "No telemetry recorded yet" in suggestions[0]


def test_build_context_engineering_suggestions_no_tool_calls():
    events = []
    for i in range(6):
        e = _success_event(i, with_memory=False)
        e["tool_calls"] = []
        e["tool_call_count"] = 0
        events.append(e)
    suggestions = build_context_engineering_suggestions(events)
    joined = " ".join(suggestions)
    assert "memorization criteria" in joined


def test_build_llm_telemetry_export():
    events = [_success_event(1, with_memory=True)]
    export = build_llm_telemetry_export(events, "persona text", {"limit": 100})
    assert export["schema_version"] == 1
    assert export["generated_for"] == "llm_context_engineering_review"
    assert export["persona_prompt"] == "persona text"
    assert "summary" in export
    assert "suggestions" in export
    ev = export["events"][0]
    assert ev["trigger_messages"][0]["text"] == "message 1"
    assert ev["used_general_memories"] == []
    assert ev["prompt_cached_tokens"] == 60
    assert ev["prompt_cache_hit_rate"] == 0.6
    assert ev["memory_writes"][0]["status"] == "succeeded"
    assert ev["active_event_states"][0]["state_key"] == "ignore"
    assert ev["pending_scheduled_actions"][0]["action_type"] == "message"
    assert ev["saved_media_options"][0]["media_unique_id"] == "sticker_1"


def test_build_llm_telemetry_export_includes_prompt_cache_summary():
    events = [_success_event(1, with_memory=True)]
    export = build_llm_telemetry_export(events, "persona text", {"limit": 100})

    assert export["summary"]["avg_prompt_cached_tokens"] == 60
    assert export["summary"]["avg_prompt_cache_hit_rate"] == 0.6
    assert export["events"][0]["prompt_cached_tokens"] == 60
    assert export["events"][0]["prompt_cache_hit_rate"] == 0.6


def test_partition_and_summarize_by_role():
    main = _success_event(1, with_memory=True)
    ponder = _success_event(2, with_memory=False)
    ponder["source"] = "ponder_agent"
    legacy = _success_event(3, with_memory=False)
    legacy["source"] = "ponder"
    followup = _success_event(4, with_memory=False)
    followup["source"] = "ponder_followup"

    assert is_ponder_telemetry_source("ponder_agent")
    assert is_ponder_telemetry_source("ponder_followup")
    assert is_ponder_telemetry_source("ponder")
    assert not is_ponder_telemetry_source("message")
    assert not is_ponder_telemetry_source("daily_task")

    main_events, ponder_events = partition_telemetry_by_role([main, ponder, legacy, followup])
    assert [e["id"] for e in main_events] == [1]
    assert [e["id"] for e in ponder_events] == [2, 3, 4]

    roles = summarize_telemetry_by_role([main, ponder, legacy, followup])
    assert roles["main_event_count"] == 1
    assert roles["ponder_event_count"] == 3
    assert roles["main"]["total_events"] == 1
    assert roles["ponder"]["total_events"] == 3
    assert roles["all"]["total_events"] == 4


def test_build_llm_telemetry_export_includes_role_summaries():
    main = _success_event(1, with_memory=True)
    ponder = _success_event(2, with_memory=False)
    ponder["source"] = "ponder_agent"
    export = build_llm_telemetry_export([main, ponder], "persona text", {"limit": 100})
    assert export["main_event_count"] == 1
    assert export["ponder_event_count"] == 1
    assert export["main_summary"]["total_events"] == 1
    assert export["ponder_summary"]["total_events"] == 1
