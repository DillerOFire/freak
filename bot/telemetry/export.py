"""LLM-friendly JSON export shaping for telemetry review."""

from typing import Any

from bot.telemetry.analysis import (
    build_context_engineering_suggestions,
    summarize_telemetry,
    summarize_telemetry_by_role,
)


def build_llm_telemetry_export(
    events: list[dict], persona_prompt: str | None, filters: dict
) -> dict:
    """Return a JSON-serializable dict for LLM context-engineering review."""
    summary = summarize_telemetry(events)
    role_summaries = summarize_telemetry_by_role(events)
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
                "prompt_cached_tokens": event.get("prompt_cached_tokens"),
                "prompt_cache_write_tokens": event.get("prompt_cache_write_tokens"),
                "uncached_prompt_tokens": event.get("uncached_prompt_tokens"),
                "prompt_cache_hit_rate": event.get("prompt_cache_hit_rate"),
                "completion_tokens": event.get("completion_tokens"),
                "total_tokens": event.get("total_tokens"),
                "context_message_count": event.get("context_message_count", 0),
                "context_chars": event.get("context_chars", 0),
                "system_prompt_chars": event.get("system_prompt_chars", 0),
                "system_prompt_hash": event.get("system_prompt_hash"),
                "context_prompt_hash": event.get("context_prompt_hash"),
                "cache_prefix_hash": event.get("cache_prefix_hash"),
                "cache_prefix_chars": event.get("cache_prefix_chars", 0),
                "cache_stable_message_count": event.get(
                    "cache_stable_message_count", 0
                ),
                "prompt_sections": event.get("prompt_sections", {}),
                "response_attempt_count": event.get("response_attempt_count", 1),
                "response_id": event.get("response_id"),
                "response_model": event.get("response_model"),
                "provider": event.get("provider"),
                "user_thought_count": event.get("user_thought_count", 0),
                "retrieved_memory_count": event.get("retrieved_memory_count", 0),
                "memory_query": event.get("memory_query"),
                "trigger_messages": event.get("trigger_messages", []),
                "used_user_thoughts": event.get("used_user_thoughts", {}),
                "used_general_memories": event.get("used_general_memories", []),
                "tool_calls": event.get("tool_calls", []),
                "memory_writes": event.get("memory_writes", []),
                "response_messages": event.get("response_messages", []),
                "active_event_states": event.get("active_event_states", []),
                "pending_scheduled_actions": event.get("pending_scheduled_actions", []),
                "saved_media_options": event.get("saved_media_options", []),
                "saved_media_option_count": event.get("saved_media_option_count", 0),
                "saved_media_policy": event.get("saved_media_policy", {}),
                "error_type": event.get("error_type"),
                "error_message": event.get("error_message"),
                "system_prompt": event.get("system_prompt"),
                "context_prompt": event.get("context_prompt"),
                "raw_response": event.get("raw_response"),
                "response_media": event.get("response_media", {}),
            }
        )

    return {
        "schema_version": 2,
        "generated_for": "llm_context_engineering_review",
        "filters": filters,
        "persona_prompt": persona_prompt,
        "summary": summary,
        "main_summary": role_summaries["main"],
        "ponder_summary": role_summaries["ponder"],
        "main_event_count": role_summaries["main_event_count"],
        "ponder_event_count": role_summaries["ponder_event_count"],
        "suggestions": suggestions,
        "events": exported_events,
    }
