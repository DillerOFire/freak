"""Rerunnable prompt-budget audit for exported telemetry documents."""

from __future__ import annotations

import re
from typing import Any


def _section_chars(event: dict[str, Any], section: str) -> int:
    recorded = (event.get("prompt_sections") or {}).get(section) or {}
    if isinstance(recorded.get("chars"), int):
        return max(0, recorded["chars"])
    match = re.search(
        rf"\s*<{section}(?:\s[^>]*)?>.*?</{section}>",
        str(event.get("context_prompt") or ""),
        re.DOTALL,
    )
    return len(match.group(0)) if match else 0


def audit_telemetry_export(document: dict[str, Any]) -> dict[str, Any]:
    events = [
        event
        for event in document.get("events") or []
        if isinstance(event, dict) and event.get("source") != "ponder_agent"
    ]
    usage_events = [
        event
        for event in events
        if isinstance(event.get("prompt_tokens"), (int, float))
        and event.get("prompt_tokens", 0) > 0
    ]
    total_prompt = sum(int(event["prompt_tokens"]) for event in usage_events)
    total_cached = sum(
        min(
            int(event["prompt_tokens"]),
            max(0, int(event.get("prompt_cached_tokens") or 0)),
        )
        for event in usage_events
    )
    total_uncached = total_prompt - total_cached
    cache_hit_calls = sum(
        1 for event in usage_events if int(event.get("prompt_cached_tokens") or 0) > 0
    )

    research_chars = [_section_chars(event, "related_research") for event in events]
    media_chars = [_section_chars(event, "saved_media") for event in events]
    normal_media_events = [
        event
        for event in events
        if (event.get("saved_media_policy") or {}).get("mode") == "normal"
    ]
    research_note_counts = [
        len(re.findall(r"<note\b", str(event.get("context_prompt") or "")))
        for event in events
    ]

    violations: list[str] = []
    if any(count > 1 for count in research_note_counts):
        violations.append("A prompt injected more than one related research note.")
    if any(
        int(event.get("saved_media_option_count") or 0) > 4
        for event in normal_media_events
    ):
        violations.append("A normal-chat prompt injected more than four media options.")
    if any(
        int(event.get("cache_stable_message_count") or 0) not in {0, 20}
        for event in events
    ):
        violations.append("A cache epoch reported a stable history size other than 20.")

    count = len(events)
    return {
        "event_count": count,
        "usage_event_count": len(usage_events),
        "total_prompt_tokens": total_prompt,
        "total_cached_tokens": total_cached,
        "total_uncached_tokens": total_uncached,
        "weighted_cached_share": total_cached / total_prompt if total_prompt else None,
        "cache_hit_call_rate": (
            cache_hit_calls / len(usage_events) if usage_events else None
        ),
        "avg_related_research_chars": (
            sum(research_chars) / count if count else None
        ),
        "avg_saved_media_chars": sum(media_chars) / count if count else None,
        "normal_media_event_count": len(normal_media_events),
        "normal_media_max_options": max(
            (
                int(event.get("saved_media_option_count") or 0)
                for event in normal_media_events
            ),
            default=0,
        ),
        "max_related_research_notes": max(research_note_counts, default=0),
        "violations": violations,
    }
