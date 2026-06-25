"""Embedded local HTTP server for the telemetry dashboard."""

import asyncio
import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from bot.telemetry.dashboard import render_dashboard_html
from bot.telemetry.export import build_llm_telemetry_export
from bot.telemetry.storage import (
    fetch_llm_telemetry,
    fetch_llm_telemetry_event,
    get_telemetry_chats,
)

logger = logging.getLogger(__name__)


def _parse_filters(query: dict[str, list[str]]) -> dict:
    def first(key: str, default: str | None = None) -> str | None:
        values = query.get(key)
        if not values:
            return default
        return values[0]

    limit_raw = first("limit", "100")
    try:
        limit = max(1, min(500, int(limit_raw)))
    except (TypeError, ValueError):
        limit = 100

    chat_id_raw = first("chat_id")
    chat_id = None
    if chat_id_raw and chat_id_raw != "all":
        try:
            chat_id = int(chat_id_raw)
        except (TypeError, ValueError):
            chat_id = None

    status_raw = first("status")
    status = None
    if status_raw and status_raw != "all":
        status = status_raw

    source_raw = first("source")
    source = None
    if source_raw and source_raw != "all":
        source = source_raw

    return {
        "chat_id": chat_id,
        "status": status,
        "source": source,
        "limit": limit,
    }


def _run_async(coro):
    """Run a coroutine to completion in a dedicated event loop (thread-safe)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # silence default stderr logging
        return

    def _check_auth(self) -> bool:
        token = self.server.telemetry_token  # type: ignore[attr-defined]
        if token is None:
            return True
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        token_values = query.get("token")
        if token_values and token_values[0] == token:
            return True
        auth_header = self.headers.get("Authorization", "")
        if auth_header == f"Bearer {token}":
            return True
        return False

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status: int, text: str) -> None:
        self._send(status, text.encode("utf-8"), "text/plain; charset=utf-8")

    def _send_json(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def do_GET(self):  # noqa: N802 - stdlib naming
        try:
            if not self._check_auth():
                self._send_text(401, "Unauthorized")
                return

            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            filters = _parse_filters(query)

            if path == "/telemetry":
                chats = _run_async(get_telemetry_chats())
                events = _run_async(
                    fetch_llm_telemetry(
                        chat_id=filters["chat_id"],
                        limit=filters["limit"],
                        status=filters["status"],
                        source=filters["source"],
                    )
                )
                html_doc = render_dashboard_html(events, chats, filters)
                self._send(
                    200, html_doc.encode("utf-8"), "text/html; charset=utf-8"
                )
                return

            if path == "/telemetry/export.json":
                events = _run_async(
                    fetch_llm_telemetry(
                        chat_id=filters["chat_id"],
                        limit=filters["limit"],
                        status=filters["status"],
                        source=filters["source"],
                    )
                )

                async def _get_persona():
                    import bot.memory as memory

                    return await memory.get_config("persona_prompt")

                persona_prompt = _run_async(_get_persona())
                export = build_llm_telemetry_export(events, persona_prompt, filters)
                self._send_json(200, export)
                return

            match = re.match(r"^/telemetry/event/(\d+)\.json$", path)
            if match:
                event_id = int(match.group(1))
                event = _run_async(fetch_llm_telemetry_event(event_id))
                if event is None:
                    self._send_json(404, {"error": "event not found"})
                    return
                self._send_json(200, event)
                return

            self._send_text(404, "Not found")
        except Exception:
            logger.exception("Telemetry dashboard request failed")
            self._send_text(500, "Internal server error")


def start_telemetry_dashboard(
    host: str, port: int, token: str | None = None
) -> ThreadingHTTPServer:
    """Start the embedded dashboard HTTP server in a daemon thread."""
    server = ThreadingHTTPServer((host, port), _Handler)
    server.telemetry_token = token  # type: ignore[attr-defined]
    thread = threading.Thread(
        target=server.serve_forever, daemon=True, name="telemetry-dashboard"
    )
    thread.start()
    return server
