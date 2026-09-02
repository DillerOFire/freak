"""Prompt fingerprints and section sizes for cache diagnosis."""

from __future__ import annotations

import hashlib
import re


_SECTION_NAMES = (
    "working_memory",
    "core_memory",
    "retrieved_semantic_memory",
    "related_research",
    "saved_media",
    "saved_media_policy",
    "behavior_settings",
    "pending_scheduled_actions",
    "active_event_states",
    "active_instruction",
)


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def prompt_section_metrics(context_prompt: str) -> dict[str, dict[str, int | str]]:
    metrics: dict[str, dict[str, int | str]] = {}
    for name in _SECTION_NAMES:
        match = re.search(
            rf"\s*<{name}(?:\s[^>]*)?>.*?</{name}>",
            context_prompt,
            re.DOTALL,
        )
        if match is None:
            continue
        section = match.group(0)
        metrics[name] = {
            "chars": len(section),
            "sha256": text_sha256(section),
        }
    return metrics


def cache_prefix_fingerprint(
    system_prompt: str,
    context_prompt: str,
    context_cache_boundary: int | None,
) -> tuple[str, int]:
    context_prefix = (
        context_prompt[:context_cache_boundary]
        if context_cache_boundary is not None
        else ""
    )
    combined = system_prompt + "\0" + context_prefix
    return text_sha256(combined), len(system_prompt) + len(context_prefix)
