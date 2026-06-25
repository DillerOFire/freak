from bot.telemetry.analysis import (
    summarize_telemetry,
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
    assert ev["memory_writes"][0]["status"] == "succeeded"
