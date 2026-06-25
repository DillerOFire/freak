"""Pure summary and context-engineering suggestion logic for telemetry.

This module must not import Telegram, OpenRouter, aiosqlite, or http.server.
"""

from typing import Any

_FAILURE_STATUSES = {"invalid_json", "validation_error", "empty_content", "exception"}


def _safe_average(values: list[float | int | None]) -> float | None:
    """Average of non-None values; None when no data."""
    nums = [v for v in values if v is not None]
    if not nums:
        return None
    return sum(nums) / len(nums)


def _last_trigger_text(event: dict[str, Any], limit: int = 160) -> str:
    triggers = event.get("trigger_messages") or []
    if not triggers:
        return ""
    last = triggers[-1]
    if isinstance(last, dict):
        text = str(last.get("text", ""))
    else:
        text = str(last)
    return text[:limit]


def summarize_telemetry(events: list[dict]) -> dict:
    """Return a JSON-serializable summary of telemetry events."""
    total_events = len(events)
    if total_events == 0:
        return {
            "total_events": 0,
            "status_counts": {},
            "source_counts": {},
            "success_rate": None,
            "no_reply_rate": None,
            "failure_rate": None,
            "avg_latency_ms": None,
            "avg_context_chars": None,
            "max_context_chars": None,
            "avg_retrieved_memory_count": None,
            "avg_user_thought_count": None,
            "avg_tool_call_count": None,
            "avg_memory_write_count": None,
            "avg_failed_memory_write_count": None,
            "memory_write_success_rate": None,
            "avg_response_message_count": None,
            "avg_prompt_tokens": None,
            "avg_completion_tokens": None,
            "latest_errors": [],
            "top_memory_write_topics": [],
            "recent_no_memory_examples": [],
        }

    status_counts: dict[str, int] = {}
    source_counts: dict[str, int] = {}
    for event in events:
        status = event.get("status", "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        source = event.get("source") or "message"
        source_counts[source] = source_counts.get(source, 0) + 1

    success_count = status_counts.get("success", 0)
    no_reply_count = status_counts.get("no_reply", 0)
    failure_count = sum(
        count for status, count in status_counts.items() if status in _FAILURE_STATUSES
    )

    success_rate = success_count / total_events
    no_reply_rate = no_reply_count / total_events
    failure_rate = failure_count / total_events

    avg_latency_ms = _safe_average([event.get("latency_ms") for event in events])
    avg_context_chars = _safe_average(
        [event.get("context_chars") for event in events]
    )
    max_context_chars = max(
        (event.get("context_chars") or 0 for event in events), default=None
    )
    avg_retrieved_memory_count = _safe_average(
        [event.get("retrieved_memory_count") for event in events]
    )
    avg_user_thought_count = _safe_average(
        [event.get("user_thought_count") for event in events]
    )
    avg_tool_call_count = _safe_average(
        [event.get("tool_call_count") for event in events]
    )
    avg_memory_write_count = _safe_average(
        [event.get("memory_write_count") for event in events]
    )
    avg_failed_memory_write_count = _safe_average(
        [event.get("failed_memory_write_count") for event in events]
    )

    total_memory_writes = sum(
        event.get("memory_write_count", 0) + event.get("failed_memory_write_count", 0)
        for event in events
    )
    succeeded_memory_writes = sum(
        event.get("memory_write_count", 0) for event in events
    )
    if total_memory_writes > 0:
        memory_write_success_rate = succeeded_memory_writes / total_memory_writes
    else:
        memory_write_success_rate = None

    avg_response_message_count = _safe_average(
        [event.get("response_message_count") for event in events]
    )
    avg_prompt_tokens = _safe_average(
        [event.get("prompt_tokens") for event in events]
    )
    avg_completion_tokens = _safe_average(
        [event.get("completion_tokens") for event in events]
    )

    # Latest errors: up to five newest failure events.
    failure_events = [
        event for event in events if event.get("status") in _FAILURE_STATUSES
    ]
    failure_events_sorted = sorted(
        failure_events,
        key=lambda e: (e.get("timestamp", ""), e.get("id", 0)),
        reverse=True,
    )
    latest_errors = [
        {
            "id": e.get("id"),
            "timestamp": e.get("timestamp"),
            "status": e.get("status"),
            "error_type": e.get("error_type"),
            "error_message": e.get("error_message"),
        }
        for e in failure_events_sorted[:5]
    ]

    # Top memory write topics from succeeded general_memory writes.
    topic_counts: dict[str, int] = {}
    for event in events:
        for write in event.get("memory_writes") or []:
            if write.get("status") != "succeeded":
                continue
            if write.get("type") != "general_memory":
                continue
            args = write.get("arguments") or {}
            topic = args.get("topic")
            if topic is None:
                continue
            topic_counts[str(topic)] = topic_counts.get(str(topic), 0) + 1
    top_topics = sorted(
        topic_counts.items(), key=lambda kv: (-kv[1], kv[0])
    )
    top_memory_write_topics = [
        {"topic": topic, "count": count} for topic, count in top_topics[:10]
    ]

    # Recent no-memory examples: up to five newest success/no_reply events
    # with zero memory writes.
    no_memory_candidates = [
        event
        for event in events
        if event.get("status") in {"success", "no_reply"}
        and event.get("memory_write_count", 0) == 0
    ]
    no_memory_sorted = sorted(
        no_memory_candidates,
        key=lambda e: (e.get("timestamp", ""), e.get("id", 0)),
        reverse=True,
    )
    recent_no_memory_examples = [
        {
            "id": e.get("id"),
            "timestamp": e.get("timestamp"),
            "status": e.get("status"),
            "context_message_count": e.get("context_message_count", 0),
            "retrieved_memory_count": e.get("retrieved_memory_count", 0),
            "last_trigger_text": _last_trigger_text(e),
        }
        for e in no_memory_sorted[:5]
    ]

    return {
        "total_events": total_events,
        "status_counts": status_counts,
        "source_counts": source_counts,
        "success_rate": success_rate,
        "no_reply_rate": no_reply_rate,
        "failure_rate": failure_rate,
        "avg_latency_ms": avg_latency_ms,
        "avg_context_chars": avg_context_chars,
        "max_context_chars": max_context_chars,
        "avg_retrieved_memory_count": avg_retrieved_memory_count,
        "avg_user_thought_count": avg_user_thought_count,
        "avg_tool_call_count": avg_tool_call_count,
        "avg_memory_write_count": avg_memory_write_count,
        "avg_failed_memory_write_count": avg_failed_memory_write_count,
        "memory_write_success_rate": memory_write_success_rate,
        "avg_response_message_count": avg_response_message_count,
        "avg_prompt_tokens": avg_prompt_tokens,
        "avg_completion_tokens": avg_completion_tokens,
        "latest_errors": latest_errors,
        "top_memory_write_topics": top_memory_write_topics,
        "recent_no_memory_examples": recent_no_memory_examples,
    }


def build_context_engineering_suggestions(events: list[dict]) -> list[str]:
    """Return deterministic, human-readable context-engineering suggestions."""
    summary = summarize_telemetry(events)
    total_events = summary["total_events"]

    if total_events == 0:
        return [
            "No telemetry recorded yet; let the bot handle a few real replies "
            "before changing memory or roleplay prompts."
        ]

    suggestions: list[str] = []
    failure_rate = summary["failure_rate"] or 0
    no_reply_rate = summary["no_reply_rate"] or 0
    avg_context_chars = summary["avg_context_chars"] or 0
    max_context_chars = summary["max_context_chars"] or 0
    avg_retrieved_memory_count = summary["avg_retrieved_memory_count"] or 0
    avg_tool_call_count = summary["avg_tool_call_count"] or 0
    memory_write_success_rate = summary["memory_write_success_rate"]
    avg_response_message_count = summary["avg_response_message_count"] or 0

    latest_errors = summary["latest_errors"]
    error_statuses = {e.get("status") for e in latest_errors}

    if failure_rate >= 0.10:
        suggestions.append(
            "Tighten the JSON output contract in SYSTEM_INSTRUCTIONS: invalid or "
            "failed responses block both replies and memory writes."
        )

    if "invalid_json" in error_statuses or "validation_error" in error_statuses:
        suggestions.append(
            "Add stricter JSON-only examples that include both `messages` and "
            "`tool_calls` so the model returns valid structured output."
        )

    if no_reply_rate >= 0.50:
        suggestions.append(
            "Clarify when the persona should reply versus stay silent in "
            "SYSTEM_INSTRUCTIONS; the model often uses context but produces no "
            "visible answer."
        )

    if avg_context_chars >= 8000 or max_context_chars >= 16000:
        suggestions.append(
            "Reduce working-memory length or summarize older chat history before "
            "prompt assembly; context prompts are too large."
        )

    if avg_retrieved_memory_count < 1 and total_events >= 5:
        suggestions.append(
            "Improve semantic-memory retrieval queries; replies are being "
            "generated without prior general memories."
        )

    if avg_tool_call_count == 0 and total_events >= 5:
        suggestions.append(
            "Make memorization criteria more explicit in SYSTEM_INSTRUCTIONS; "
            "the model is not attempting to write memories."
        )

    if (
        memory_write_success_rate is not None
        and memory_write_success_rate < 0.90
    ):
        suggestions.append(
            "Inspect failed memory-write arguments and align tool-call examples "
            "with `update_user_thought(user_id, username, thought)` and "
            "`add_general_memory(topic, summary, importance)`."
        )

    if len(summary["recent_no_memory_examples"]) >= 3:
        suggestions.append(
            "Add examples of what should be remembered from user messages; the "
            "bot is responding without storing new user or topic facts."
        )

    if avg_response_message_count > 2:
        suggestions.append(
            "Constrain multi-message output unless the roleplay intentionally "
            "speaks in bursts."
        )

    if not suggestions:
        suggestions.append(
            "Telemetry does not show an obvious memory or context-engineering "
            "issue yet; inspect successful examples and no-memory examples "
            "before changing the persona."
        )

    return suggestions
