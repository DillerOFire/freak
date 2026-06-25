"""Persistent telemetry storage backed by the bot's SQLite database."""

import json
import logging
from typing import Any

import aiosqlite

import bot.memory as memory


_JSON_COLUMNS = {
    "trigger_messages": "trigger_messages_json",
    "used_user_thoughts": "used_user_thoughts_json",
    "used_general_memories": "used_general_memories_json",
    "tool_calls": "tool_calls_json",
    "memory_writes": "memory_writes_json",
    "response_messages": "response_messages_json",
    "response_media": "response_media_json",
}


def _decode_json_field(field: str, raw: str | None) -> Any:
    """Decode a stored JSON column, returning a tolerant default on failure."""
    if raw is None:
        return [] if field != "used_user_thoughts" else {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return [] if field != "used_user_thoughts" else {}


def _decode_row(row: aiosqlite.Row) -> dict[str, Any]:
    event = dict(row)
    for field, column in _JSON_COLUMNS.items():
        event[field] = _decode_json_field(field, event.pop(column, None))
    return event


async def init_telemetry_db() -> None:
    """Create the telemetry table and indexes if they do not exist."""
    async with aiosqlite.connect(memory.DB_NAME) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS llm_telemetry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                chat_id INTEGER NOT NULL,
                source TEXT NOT NULL,
                model TEXT,
                focus_message_id INTEGER,
                status TEXT NOT NULL,
                error_type TEXT,
                error_message TEXT,
                latency_ms INTEGER,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                total_tokens INTEGER,
                context_message_count INTEGER NOT NULL DEFAULT 0,
                context_chars INTEGER NOT NULL DEFAULT 0,
                system_prompt_chars INTEGER NOT NULL DEFAULT 0,
                user_thought_count INTEGER NOT NULL DEFAULT 0,
                retrieved_memory_count INTEGER NOT NULL DEFAULT 0,
                memory_query TEXT,
                trigger_messages_json TEXT NOT NULL DEFAULT '[]',
                used_user_thoughts_json TEXT NOT NULL DEFAULT '{}',
                used_general_memories_json TEXT NOT NULL DEFAULT '[]',
                tool_calls_json TEXT NOT NULL DEFAULT '[]',
                memory_writes_json TEXT NOT NULL DEFAULT '[]',
                tool_call_count INTEGER NOT NULL DEFAULT 0,
                memory_write_count INTEGER NOT NULL DEFAULT 0,
                failed_memory_write_count INTEGER NOT NULL DEFAULT 0,
                response_message_count INTEGER NOT NULL DEFAULT 0,
                response_chars INTEGER NOT NULL DEFAULT 0,
                reply_to_message_id INTEGER,
                response_messages_json TEXT NOT NULL DEFAULT '[]',
                system_prompt TEXT,
                context_prompt TEXT,
                raw_response TEXT,
                response_media_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        # Migration: Add response_media_json if missing
        async with db.execute("PRAGMA table_info(llm_telemetry)") as cursor:
            columns = [row[1] for row in await cursor.fetchall()]
            if "response_media_json" not in columns:
                logging.info("Migrating DB: Adding response_media_json to llm_telemetry")
                await db.execute(
                    "ALTER TABLE llm_telemetry ADD COLUMN response_media_json TEXT NOT NULL DEFAULT '{}'"
                )

        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_llm_telemetry_chat_timestamp "
            "ON llm_telemetry(chat_id, timestamp)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_llm_telemetry_status ON llm_telemetry(status)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_llm_telemetry_source ON llm_telemetry(source)"
        )
        await db.commit()


async def record_llm_telemetry(event: dict[str, Any]) -> None:
    """Insert one telemetry event row. Does not swallow SQLite errors."""
    source = event.get("source") or "message"

    def _list(key: str) -> str:
        value = event.get(key)
        if value is None:
            value = []
        return json.dumps(value, ensure_ascii=False)

    def _dict(key: str) -> str:
        value = event.get(key)
        if value is None:
            value = {}
        return json.dumps(value, ensure_ascii=False)

    def _count(key: str) -> int:
        value = event.get(key)
        if value is None:
            return 0
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def _opt(key: str) -> Any:
        value = event.get(key)
        return value

    async with aiosqlite.connect(memory.DB_NAME) as db:
        await db.execute(
            """
            INSERT INTO llm_telemetry (
                chat_id, source, model, focus_message_id, status,
                error_type, error_message, latency_ms,
                prompt_tokens, completion_tokens, total_tokens,
                context_message_count, context_chars, system_prompt_chars,
                user_thought_count, retrieved_memory_count, memory_query,
                trigger_messages_json, used_user_thoughts_json,
                used_general_memories_json, tool_calls_json, memory_writes_json,
                tool_call_count, memory_write_count, failed_memory_write_count,
                response_message_count, response_chars, reply_to_message_id,
                response_messages_json, system_prompt, context_prompt, raw_response,
                response_media_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(event["chat_id"]),
                source,
                _opt("model"),
                _opt("focus_message_id"),
                event["status"],
                _opt("error_type"),
                _opt("error_message"),
                _opt("latency_ms"),
                _opt("prompt_tokens"),
                _opt("completion_tokens"),
                _opt("total_tokens"),
                _count("context_message_count"),
                _count("context_chars"),
                _count("system_prompt_chars"),
                _count("user_thought_count"),
                _count("retrieved_memory_count"),
                _opt("memory_query"),
                _list("trigger_messages"),
                _dict("used_user_thoughts"),
                _list("used_general_memories"),
                _list("tool_calls"),
                _list("memory_writes"),
                _count("tool_call_count"),
                _count("memory_write_count"),
                _count("failed_memory_write_count"),
                _count("response_message_count"),
                _count("response_chars"),
                _opt("reply_to_message_id"),
                _list("response_messages"),
                _opt("system_prompt"),
                _opt("context_prompt"),
                _opt("raw_response"),
                _dict("response_media"),
            ),
        )
        await db.commit()


async def fetch_llm_telemetry(
    chat_id: int | None = None,
    limit: int = 100,
    status: str | None = None,
    source: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch telemetry events, newest first, with optional filters."""
    limit = max(1, min(500, int(limit)))

    query = "SELECT * FROM llm_telemetry"
    conditions: list[str] = []
    params: list[Any] = []

    if chat_id is not None:
        conditions.append("chat_id = ?")
        params.append(int(chat_id))
    if status is not None:
        conditions.append("status = ?")
        params.append(status)
    if source is not None:
        conditions.append("source = ?")
        params.append(source)

    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY timestamp DESC, id DESC LIMIT ?"
    params.append(limit)

    async with aiosqlite.connect(memory.DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()
        return [_decode_row(row) for row in rows]


async def fetch_llm_telemetry_event(event_id: int) -> dict[str, Any] | None:
    """Fetch a single decoded telemetry event by id, or None."""
    async with aiosqlite.connect(memory.DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM llm_telemetry WHERE id = ?", (int(event_id),)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return _decode_row(row)


async def get_telemetry_chats() -> list[int]:
    """Return distinct chat_ids ordered by newest activity first."""
    async with aiosqlite.connect(memory.DB_NAME) as db:
        cursor = await db.execute(
            "SELECT chat_id FROM llm_telemetry GROUP BY chat_id "
            "ORDER BY MAX(timestamp) DESC"
        )
        rows = await cursor.fetchall()
        return [int(row[0]) for row in rows]
