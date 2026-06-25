"""Modular telemetry package: storage, analysis, export, dashboard, and web."""

from bot.telemetry.storage import (
    init_telemetry_db,
    record_llm_telemetry,
    fetch_llm_telemetry,
    fetch_llm_telemetry_event,
    get_telemetry_chats,
)
from bot.telemetry.analysis import (
    summarize_telemetry,
    build_context_engineering_suggestions,
)
from bot.telemetry.export import build_llm_telemetry_export
from bot.telemetry.dashboard import render_dashboard_html
from bot.telemetry.web import start_telemetry_dashboard

__all__ = [
    "init_telemetry_db",
    "record_llm_telemetry",
    "fetch_llm_telemetry",
    "fetch_llm_telemetry_event",
    "get_telemetry_chats",
    "summarize_telemetry",
    "build_context_engineering_suggestions",
    "build_llm_telemetry_export",
    "render_dashboard_html",
    "start_telemetry_dashboard",
]
