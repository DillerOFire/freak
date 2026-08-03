"""Telegram Web App data helpers for the telemetry dashboard.

HTTP routing and Telegram init-data authentication deliberately live in
``bot.settings_web`` with the other admin Web App surfaces.  Keeping this
module free of a web-server implementation means telemetry can only be read
through that authenticated app instead of a separate token-protected port.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from bot.telemetry.analysis import build_context_engineering_suggestions, summarize_telemetry
from bot.telemetry.storage import fetch_llm_telemetry, get_telemetry_chats


def parse_telemetry_filters(query: Mapping[str, Any]) -> dict[str, int | str | None]:
    """Normalize Web App telemetry filters without trusting browser input."""

    def first(key: str, default: str | None = None) -> str | None:
        value = query.get(key, default)
        if isinstance(value, (list, tuple)):
            value = value[0] if value else default
        return str(value) if value is not None else None

    try:
        limit = max(1, min(500, int(first("limit", "100") or "100")))
    except (TypeError, ValueError):
        limit = 100

    chat_id = None
    chat_id_raw = first("chat_id")
    if chat_id_raw and chat_id_raw != "all":
        try:
            chat_id = int(chat_id_raw)
        except (TypeError, ValueError):
            pass

    status = first("status")
    if status == "all":
        status = None
    source = first("source")
    if source == "all":
        source = None

    return {"chat_id": chat_id, "status": status, "source": source, "limit": limit}


async def build_telemetry_snapshot(filters: Mapping[str, Any]) -> dict[str, object]:
    """Build one JSON-safe, filter-aware telemetry document for the Web App."""
    normalized = parse_telemetry_filters(filters)
    events = await fetch_llm_telemetry(**normalized)
    return {
        "filters": normalized,
        "chats": await get_telemetry_chats(),
        "events": events,
        "summary": summarize_telemetry(events),
        "suggestions": build_context_engineering_suggestions(events),
    }
