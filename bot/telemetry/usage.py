"""Normalize OpenAI-compatible response usage and cache accounting."""

from __future__ import annotations

from typing import Any


USAGE_TOTAL_KEYS = (
    "prompt_tokens",
    "prompt_cached_tokens",
    "prompt_cache_write_tokens",
    "completion_tokens",
    "total_tokens",
)


def usage_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def usage_field(value: object, *path: str) -> object:
    current = value
    for key in path:
        if current is None:
            return None
        if isinstance(current, dict):
            current = current.get(key)
        else:
            current = getattr(current, key, None)
    return current


def usage_snapshot(usage: object) -> dict[str, int | None]:
    return {
        "prompt_tokens": usage_int(usage_field(usage, "prompt_tokens")),
        "prompt_cached_tokens": usage_int(
            usage_field(usage, "prompt_tokens_details", "cached_tokens")
        ),
        "prompt_cache_write_tokens": usage_int(
            usage_field(usage, "prompt_tokens_details", "cache_write_tokens")
        ),
        "completion_tokens": usage_int(usage_field(usage, "completion_tokens")),
        "total_tokens": usage_int(usage_field(usage, "total_tokens")),
    }


def accumulate_usage(
    totals: dict[str, int | None],
    usage: object,
) -> None:
    snapshot = usage_snapshot(usage)
    for key in USAGE_TOTAL_KEYS:
        value = snapshot[key]
        if value is None:
            continue
        current = totals.get(key)
        totals[key] = value if current is None else int(current) + value


def prompt_cache_hit_rate(
    prompt_tokens: int | None,
    prompt_cached_tokens: int | None,
) -> float | None:
    if prompt_tokens is None or prompt_cached_tokens is None or prompt_tokens <= 0:
        return None
    cached = max(0, min(int(prompt_cached_tokens), int(prompt_tokens)))
    return cached / float(prompt_tokens)


def uncached_prompt_tokens(
    prompt_tokens: int | None,
    prompt_cached_tokens: int | None,
) -> int | None:
    if prompt_tokens is None:
        return None
    cached = max(0, min(int(prompt_cached_tokens or 0), int(prompt_tokens)))
    return max(0, int(prompt_tokens) - cached)


def response_metadata(response: object) -> dict[str, Any]:
    """Read optional gateway fields without assuming one provider schema."""
    def _text(path: str) -> str | None:
        value = usage_field(response, path)
        return str(value) if isinstance(value, (str, int, float)) else None

    return {
        "response_id": _text("id"),
        "response_model": _text("model"),
        "provider": _text("provider"),
    }
