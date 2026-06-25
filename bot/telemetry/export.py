"""LLM-friendly JSON export shaping for telemetry review."""

from typing import Any

from bot.telemetry.analysis import (
    build_context_engineering_suggestions,
    summarize_telemetry,
)


def build_llm_telemetry_export(
    events: list[dict], persona_prompt: str | None, filters: dict
) -> dict:
    """Return a JSON-serializable dict for LLM context-engineering review."""
    summary = summarize_telemetry(events)
    suggestions = build_context_engineering_suggestions(events)

    exported_events: list[dict[str, Any]] = []
    for event in events:
        exported_events.append(
            {
                "id": event.get("id"),
                "timestamp": event.get("timestamp"),
                "chat_id": event.get("chat_id"),
                "source": event.get("source"),
                "status": event.get("status"),
                "model": event.get("model"),
                "focus_message_id": event.get("focus_message_id"),
                "latency_ms": event.get("latency_ms"),
                "prompt_tokens": event.get("prompt_tokens"),
                "completion_tokens": event.get("completion_tokens"),
                "total_tokens": event.get("total_tokens"),
                "context_message_count": event.get("context_message_count", 0),
                "context_chars": event.get("context_chars", 0),
                "system_prompt_chars": event.get("system_prompt_chars", 0),
                "user_thought_count": event.get("user_thought_count", 0),
                "retrieved_memory_count": event.get("retrieved_memory_count", 0),
                "memory_query": event.get("memory_query"),
                "trigger_messages": event.get("trigger_messages", []),
                "used_user_thoughts": event.get("used_user_thoughts", {}),
                "used_general_memories": event.get("used_general_memories", []),
                "tool_calls": event.get("tool_calls", []),
                "memory_writes": event.get("memory_writes", []),
                "response_messages": event.get("response_messages", []),
                "error_type": event.get("error_type"),
                "error_message": event.get("error_message"),
                "system_prompt": event.get("system_prompt"),
                "context_prompt": event.get("context_prompt"),
                "raw_response": event.get("raw_response"),
                "response_media": event.get("response_media", {}),
            }
        )

    return {
        "schema_version": 1,
        "generated_for": "llm_context_engineering_review",
        "filters": filters,
        "persona_prompt": persona_prompt,
        "summary": summary,
        "suggestions": suggestions,
        "events": exported_events,
    }
