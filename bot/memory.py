import aiosqlite
from typing import Literal
import datetime
import logging
import re
import os

# Use absolute path for DB to avoid issues with CWD.
# In Docker the DB lives on a mounted volume so memory survives container
# recreation; allow overriding the path via BOT_DB_PATH.
DB_NAME = os.getenv(
    "BOT_DB_PATH",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "bot_memory.db"),
)

SAVED_MEDIA_PER_CHAT_LIMIT = 50
SAVED_MEDIA_GLOBAL_LIMIT = 500
SAVED_MEDIA_PROMPT_LIMIT = 12

MAX_MEMORY_SUMMARY_LEN = 4000
MAX_MEDIA_DESCRIPTION_LEN = 2000
MAX_MEDIA_UNIQUE_ID_LEN = 128
_MEDIA_UNIQUE_ID_RE = re.compile(r"^[\w-]+$")

# Durable cache of ponder research briefs (per chat).
MAX_RESEARCH_RESULT_LEN = 8000
MAX_RESEARCH_QUERY_LEN = 500
MAX_RESEARCH_NOTES_PER_CHAT = 40
RESEARCH_RETRIEVE_LIMIT = 2


async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                thoughts TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS general_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic TEXT,
                summary TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS media_descriptions (
                media_unique_id TEXT PRIMARY KEY,
                description TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS saved_media (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                media_unique_id TEXT NOT NULL,
                media_type TEXT NOT NULL CHECK(media_type IN ('photo', 'sticker', 'animation')),
                file_id TEXT NOT NULL,
                description TEXT NOT NULL,
                sender_user_id INTEGER,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_seen_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_used_at DATETIME,
                use_count INTEGER NOT NULL DEFAULT 0,
                is_favorite INTEGER NOT NULL DEFAULT 0,
                UNIQUE(chat_id, media_unique_id)
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_saved_media_chat_seen 
            ON saved_media(chat_id, last_seen_at DESC, id DESC)
        """)

        await _migrate_saved_media_schema(db)
        await _ensure_saved_media_favorite_column(db)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_saved_media_chat_used 
            ON saved_media(chat_id, last_used_at DESC, use_count ASC)
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS whitelist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_id INTEGER UNIQUE,
                entity_type TEXT,
                added_by INTEGER,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS chat_config (
                chat_id INTEGER,
                key TEXT,
                value TEXT,
                PRIMARY KEY (chat_id, key)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS daily_messages (
                chat_id INTEGER PRIMARY KEY,
                time TEXT,
                message_type TEXT,
                content TEXT,
                file_id TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS daily_tasks (
                chat_id INTEGER PRIMARY KEY,
                time TEXT,
                task_content TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                action_type TEXT NOT NULL CHECK(action_type IN ('reply', 'message', 'research', 'task')),
                execute_at TEXT NOT NULL,
                reason TEXT NOT NULL,
                instruction TEXT NOT NULL,
                context TEXT,
                target_user_id INTEGER,
                target_username TEXT,
                reply_to_message_id INTEGER,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'running', 'done', 'cancelled', 'failed')),
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                completed_at TEXT,
                error_message TEXT
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_scheduled_actions_due
            ON scheduled_actions(status, execute_at)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_scheduled_actions_chat
            ON scheduled_actions(chat_id, status, execute_at)
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS event_states (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                state_key TEXT NOT NULL,
                value TEXT NOT NULL,
                reason TEXT,
                target_user_id INTEGER,
                target_username TEXT,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                active INTEGER NOT NULL DEFAULT 1
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_event_states_chat_active
            ON event_states(chat_id, active, expires_at)
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS persona_outputs (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                content_kind TEXT NOT NULL
                    CHECK(content_kind IN ('text', 'caption', 'photo', 'sticker', 'animation', 'poll', 'media')),
                text_excerpt TEXT,
                state TEXT NOT NULL DEFAULT 'active'
                    CHECK(state IN ('active', 'edited', 'deleted')),
                sent_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                changed_at TEXT,
                PRIMARY KEY (chat_id, message_id)
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_persona_outputs_chat_sent
            ON persona_outputs(chat_id, sent_at DESC, message_id DESC)
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS persona_reactions (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                emoji TEXT,
                state TEXT NOT NULL DEFAULT 'active'
                    CHECK(state IN ('active', 'removed')),
                reacted_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                changed_at TEXT,
                PRIMARY KEY (chat_id, message_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS research_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                query TEXT NOT NULL,
                result TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_accessed DATETIME,
                access_count INTEGER NOT NULL DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_research_notes_chat_created
            ON research_notes(chat_id, created_at DESC)
        """)
        await db.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS research_notes_fts USING fts5(
                query,
                result,
                content='research_notes',
                content_rowid='id'
            )
        """)
        await db.execute(
            "INSERT INTO research_notes_fts(research_notes_fts) VALUES('rebuild');"
        )

        # Migration: Add chat_id to general_memory if not exists
        async with db.execute("PRAGMA table_info(general_memory)") as cursor:
            columns = [row[1] for row in await cursor.fetchall()]
            if "chat_id" not in columns:
                logging.info("Migrating DB: Adding chat_id to general_memory")
                await db.execute(
                    "ALTER TABLE general_memory ADD COLUMN chat_id INTEGER"
                )
            if "importance" not in columns:
                logging.info("Migrating DB: Adding importance to general_memory")
                await db.execute(
                    "ALTER TABLE general_memory ADD COLUMN importance INTEGER DEFAULT 3"
                )
            if "access_count" not in columns:
                logging.info("Migrating DB: Adding access_count to general_memory")
                await db.execute(
                    "ALTER TABLE general_memory ADD COLUMN access_count INTEGER DEFAULT 0"
                )
            if "last_accessed" not in columns:
                logging.info("Migrating DB: Adding last_accessed to general_memory")
                await db.execute(
                    "ALTER TABLE general_memory ADD COLUMN last_accessed DATETIME"
                )

        # Create general_memory FTS table
        await db.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS general_memory_fts USING fts5(
                topic, 
                summary, 
                content='general_memory', 
                content_rowid='id'
            );
        """)
        # Rebuild general_memory FTS
        await db.execute("INSERT INTO general_memory_fts(general_memory_fts) VALUES('rebuild');")

        # Create users FTS table
        await db.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS users_fts USING fts5(
                user_id UNINDEXED, 
                username, 
                thoughts
            );
        """)
        # Populate users FTS
        await db.execute("DELETE FROM users_fts;")
        await db.execute("""
            INSERT INTO users_fts(user_id, username, thoughts) 
            SELECT user_id, username, thoughts FROM users;
        """)

        await db.commit()


async def get_user_thought(user_id: int) -> str:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT thoughts FROM users WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            logging.info(f"DEBUG: get_user_thought({user_id}) -> {row}")
            return row[0] if row else ""


async def update_user_thought(user_id: int, username: str, thought: str):
    logging.info(f"DEBUG: update_user_thought({user_id}, {username}, {thought})")
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            INSERT INTO users (user_id, username, thoughts) 
            VALUES (?, ?, ?) 
            ON CONFLICT(user_id) DO UPDATE SET 
                username=excluded.username, 
                thoughts=excluded.thoughts
        """,
            (user_id, username, thought),
        )
        # Update users_fts virtual table
        await db.execute("DELETE FROM users_fts WHERE user_id = ?", (user_id,))
        await db.execute(
            "INSERT INTO users_fts(user_id, username, thoughts) VALUES (?, ?, ?)",
            (user_id, username, thought),
        )
        await db.commit()
        logging.info("DEBUG: Committed user thought to DB")


def _format_general_memory(memory_id: int, topic: str, summary: str) -> str:
    return f"id={memory_id}, Topic: {topic}, Summary: {summary}"


def _is_valid_media_unique_id(media_unique_id: str) -> bool:
    if not media_unique_id or len(media_unique_id) > MAX_MEDIA_UNIQUE_ID_LEN:
        return False
    return bool(_MEDIA_UNIQUE_ID_RE.match(media_unique_id))


async def get_general_memories(chat_id: int, limit: int = 5) -> list[str]:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT id, topic, summary FROM general_memory WHERE chat_id = ? ORDER BY timestamp DESC LIMIT ?",
            (chat_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_format_general_memory(row[0], row[1], row[2]) for row in rows]


async def add_general_memory(topic: str, summary: str, chat_id: int, importance: int = 3):
    # Clamp importance into 1..5
    importance = max(1, min(5, importance))
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "INSERT INTO general_memory (topic, summary, chat_id, importance) VALUES (?, ?, ?, ?)",
            (topic, summary, chat_id, importance),
        )
        rowid = cursor.lastrowid
        # Update FTS table
        await db.execute(
            "INSERT INTO general_memory_fts(rowid, topic, summary) VALUES (?, ?, ?)",
            (rowid, topic, summary),
        )
        await db.commit()


def _memory_query_terms(text: str, max_terms: int = 12) -> str:
    if not text:
        return ""
    # Extract unique tokens matching [A-Za-zА-Яа-яЁё0-9_]{3,}
    raw_tokens = re.findall(r"[A-Za-zА-Яа-яЁё0-9_]{3,}", text.lower())
    seen = set()
    tokens = []
    for token in raw_tokens:
        if token not in seen:
            seen.add(token)
            tokens.append(token)
    if not tokens:
        return ""
    return " OR ".join(tokens[:max_terms])


async def get_relevant_general_memories(chat_id: int, query: str, limit: int = 5) -> list[str]:
    query_str = _memory_query_terms(query)
    if not query_str:
        return await get_general_memories(chat_id, limit)

    try:
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute(
                """
                SELECT gm.id, gm.topic, gm.summary
                FROM general_memory gm
                JOIN general_memory_fts fts ON gm.id = fts.rowid
                WHERE gm.chat_id = ? AND general_memory_fts MATCH ?
                ORDER BY bm25(general_memory_fts), gm.importance DESC, gm.timestamp DESC
                LIMIT ?
                """,
                (chat_id, query_str, limit),
            ) as cursor:
                rows = await cursor.fetchall()
            
            if not rows:
                return await get_general_memories(chat_id, limit)
            
            # Increment access count and set last_accessed for matching rows
            ids = [row[0] for row in rows]
            placeholders = ",".join("?" for _ in ids)
            await db.execute(
                f"""
                UPDATE general_memory 
                SET access_count = access_count + 1, 
                    last_accessed = CURRENT_TIMESTAMP 
                WHERE id IN ({placeholders})
                """,
                ids,
            )
            await db.commit()
            
            return [_format_general_memory(row[0], row[1], row[2]) for row in rows]
    except aiosqlite.Error as e:
        logging.error(f"FTS query error in get_relevant_general_memories: {e}")
        return await get_general_memories(chat_id, limit)


def _normalize_research_query(query: str) -> str:
    return " ".join((query or "").strip().lower().split())


def _is_saveable_research_result(result: str) -> bool:
    text = (result or "").strip()
    if len(text) < 40:
        return False
    lowered = text.lower()
    failure_prefixes = (
        "pondering failed",
        "could not complete verified research",
        "tool error:",
        "error:",
    )
    return not any(lowered.startswith(prefix) for prefix in failure_prefixes)


async def _prune_research_notes(db: aiosqlite.Connection, chat_id: int) -> None:
    async with db.execute(
        """
        SELECT id FROM research_notes
        WHERE chat_id = ?
        ORDER BY created_at DESC, id DESC
        LIMIT -1 OFFSET ?
        """,
        (chat_id, MAX_RESEARCH_NOTES_PER_CHAT),
    ) as cursor:
        stale_ids = [row[0] for row in await cursor.fetchall()]
    if not stale_ids:
        return
    placeholders = ",".join("?" for _ in stale_ids)
    await db.execute(
        f"DELETE FROM research_notes WHERE id IN ({placeholders})",
        stale_ids,
    )
    for note_id in stale_ids:
        await db.execute(
            "INSERT INTO research_notes_fts(research_notes_fts, rowid) VALUES('delete', ?)",
            (note_id,),
        )


async def save_research_note(chat_id: int, query: str, result: str) -> int | None:
    """Persist a successful ponder brief for later related-question hop-up."""
    query = (query or "").strip()[:MAX_RESEARCH_QUERY_LEN]
    result = (result or "").strip()[:MAX_RESEARCH_RESULT_LEN]
    if not query or not _is_saveable_research_result(result):
        return None

    normalized = _normalize_research_query(query)
    async with aiosqlite.connect(DB_NAME) as db:
        # Update in place when the same normalized query already exists for this chat.
        async with db.execute(
            """
            SELECT id, query FROM research_notes
            WHERE chat_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 80
            """,
            (chat_id,),
        ) as cursor:
            existing_rows = await cursor.fetchall()

        existing_id = None
        for row_id, existing_query in existing_rows:
            if _normalize_research_query(existing_query) == normalized:
                existing_id = row_id
                break

        if existing_id is not None:
            await db.execute(
                """
                UPDATE research_notes
                SET query = ?, result = ?, created_at = CURRENT_TIMESTAMP,
                    last_accessed = CURRENT_TIMESTAMP,
                    access_count = access_count + 1
                WHERE id = ?
                """,
                (query, result, existing_id),
            )
            await db.execute(
                "INSERT INTO research_notes_fts(research_notes_fts, rowid) VALUES('delete', ?)",
                (existing_id,),
            )
            await db.execute(
                "INSERT INTO research_notes_fts(rowid, query, result) VALUES (?, ?, ?)",
                (existing_id, query, result),
            )
            await _prune_research_notes(db, chat_id)
            await db.commit()
            return existing_id

        cursor = await db.execute(
            """
            INSERT INTO research_notes (chat_id, query, result, last_accessed, access_count)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP, 0)
            """,
            (chat_id, query, result),
        )
        rowid = cursor.lastrowid
        await db.execute(
            "INSERT INTO research_notes_fts(rowid, query, result) VALUES (?, ?, ?)",
            (rowid, query, result),
        )
        await _prune_research_notes(db, chat_id)
        await db.commit()
        return rowid


def _format_research_note(
    note_id: int, query: str, result: str, created_at: str | None
) -> dict[str, str | int]:
    return {
        "id": note_id,
        "query": query,
        "result": result,
        "created_at": created_at or "",
    }


async def get_recent_research_notes(
    chat_id: int, limit: int = RESEARCH_RETRIEVE_LIMIT
) -> list[dict[str, str | int]]:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            """
            SELECT id, query, result, created_at
            FROM research_notes
            WHERE chat_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (chat_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
    return [_format_research_note(row[0], row[1], row[2], row[3]) for row in rows]


async def get_relevant_research_notes(
    chat_id: int,
    query: str,
    limit: int = RESEARCH_RETRIEVE_LIMIT,
) -> list[dict[str, str | int]]:
    """FTS-hop prior ponder briefs only when they match the current text."""
    query_str = _memory_query_terms(query)
    if not query_str:
        return []

    try:
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute(
                """
                SELECT rn.id, rn.query, rn.result, rn.created_at
                FROM research_notes rn
                JOIN research_notes_fts fts ON rn.id = fts.rowid
                WHERE rn.chat_id = ? AND research_notes_fts MATCH ?
                ORDER BY bm25(research_notes_fts), rn.created_at DESC
                LIMIT ?
                """,
                (chat_id, query_str, limit),
            ) as cursor:
                rows = await cursor.fetchall()

            if not rows:
                return []

            ids = [row[0] for row in rows]
            placeholders = ",".join("?" for _ in ids)
            await db.execute(
                f"""
                UPDATE research_notes
                SET access_count = access_count + 1,
                    last_accessed = CURRENT_TIMESTAMP
                WHERE id IN ({placeholders})
                """,
                ids,
            )
            await db.commit()
            return [_format_research_note(row[0], row[1], row[2], row[3]) for row in rows]
    except aiosqlite.Error as e:
        logging.error(f"FTS query error in get_relevant_research_notes: {e}")
        return []


async def get_user_memory_by_target(target: str) -> tuple[int, str, str] | None:
    if not target or target.strip() == ".":
        return None
    normalized = target.strip().lstrip("@")
    async with aiosqlite.connect(DB_NAME) as db:
        if normalized.isdigit():
            async with db.execute(
                "SELECT user_id, username, thoughts FROM users WHERE user_id = ?",
                (int(normalized),),
            ) as cursor:
                row = await cursor.fetchone()
        else:
            async with db.execute(
                "SELECT user_id, username, thoughts FROM users WHERE lower(username) = lower(?)",
                (normalized,),
            ) as cursor:
                row = await cursor.fetchone()
        return row if row else None


async def search_user_memories(query: str, limit: int = 10) -> list[tuple[int, str, str]]:
    query_str = _memory_query_terms(query)
    if not query_str:
        return []
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute(
                """
                SELECT user_id, username, thoughts
                FROM users_fts
                WHERE users_fts MATCH ?
                ORDER BY bm25(users_fts)
                LIMIT ?
                """,
                (query_str, limit),
            ) as cursor:
                return await cursor.fetchall()
    except aiosqlite.Error as e:
        logging.error(f"FTS query error in search_user_memories: {e}")
        return []


async def search_general_memories(chat_id: int, query: str, limit: int = 10) -> list[str]:
    query_str = _memory_query_terms(query)
    if not query_str:
        return []
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute(
                """
                SELECT gm.id, gm.topic, gm.summary
                FROM general_memory gm
                JOIN general_memory_fts fts ON gm.id = fts.rowid
                WHERE gm.chat_id = ? AND general_memory_fts MATCH ?
                ORDER BY bm25(general_memory_fts), gm.importance DESC, gm.timestamp DESC
                LIMIT ?
                """,
                (chat_id, query_str, limit),
            ) as cursor:
                rows = await cursor.fetchall()
            
            if not rows:
                return []
            
            ids = [row[0] for row in rows]
            placeholders = ",".join("?" for _ in ids)
            await db.execute(
                f"""
                UPDATE general_memory 
                SET access_count = access_count + 1, 
                    last_accessed = CURRENT_TIMESTAMP 
                WHERE id IN ({placeholders})
                """,
                ids,
            )
            await db.commit()
            
            return [_format_general_memory(row[0], row[1], row[2]) for row in rows]
    except aiosqlite.Error as e:
        logging.error(f"FTS query error in search_general_memories: {e}")
        return []


async def get_media_description(media_unique_id: str) -> str | None:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT description FROM media_descriptions WHERE media_unique_id = ?",
            (media_unique_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


async def save_media_description(media_unique_id: str, description: str):
    if not _is_valid_media_unique_id(media_unique_id):
        return
    description = description.strip()[:MAX_MEDIA_DESCRIPTION_LEN]
    if not description:
        return
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            INSERT INTO media_descriptions (media_unique_id, description) 
            VALUES (?, ?) 
            ON CONFLICT(media_unique_id) DO UPDATE SET 
                description=excluded.description
            """,
            (media_unique_id, description),
        )
        await db.commit()


async def clear_media_description(media_unique_id: str) -> bool:
    """Remove a cached media summary so it will be re-analyzed on next send."""
    if not _is_valid_media_unique_id(media_unique_id):
        return False
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "DELETE FROM media_descriptions WHERE media_unique_id = ?",
            (media_unique_id,),
        )
        await db.commit()
        return cursor.rowcount > 0


async def search_media_descriptions(query: str, limit: int = 5) -> list[str]:
    """Search cached media summaries by description text (read-only)."""
    query = query.strip()
    if not query:
        return []
    limit = max(1, min(10, limit))
    terms = [t for t in re.findall(r"[A-Za-zА-Яа-яЁё0-9_]{2,}", query.lower())]
    if not terms:
        terms = [query.lower()]

    conditions = " AND ".join("LOWER(description) LIKE ?" for _ in terms)
    params = [f"%{term}%" for term in terms]
    params.append(limit)

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            f"""
            SELECT media_unique_id, description
            FROM media_descriptions
            WHERE {conditions}
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            params,
        ) as cursor:
            rows = await cursor.fetchall()
    return [
        f"media_unique_id={row[0]}, description: {row[1]}"
        for row in rows
    ]


async def update_saved_media_description(
    chat_id: int,
    media_unique_id: str,
    description: str,
) -> bool:
    """Update the description on a chat's saved reusable media row."""
    if not _is_valid_media_unique_id(media_unique_id):
        return False
    description = description.strip()[:MAX_MEDIA_DESCRIPTION_LEN]
    if not description:
        return False
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            """
            UPDATE saved_media
            SET description = ?
            WHERE chat_id = ? AND media_unique_id = ?
            """,
            (description, chat_id, media_unique_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def delete_general_memory(memory_id: int, chat_id: int) -> bool:
    """Delete a single general memory row scoped to the chat."""
    if memory_id <= 0:
        return False
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "DELETE FROM general_memory WHERE id = ? AND chat_id = ?",
            (memory_id, chat_id),
        )
        if cursor.rowcount == 0:
            return False
        await db.execute(
            "INSERT INTO general_memory_fts(general_memory_fts, rowid) VALUES('delete', ?)",
            (memory_id,),
        )
        await db.commit()
        return True


async def update_general_memory(
    memory_id: int,
    chat_id: int,
    *,
    topic: str | None = None,
    summary: str | None = None,
    importance: int | None = None,
) -> bool:
    """Update fields on a single general memory row scoped to the chat."""
    if memory_id <= 0:
        return False

    updates: list[str] = []
    params: list[object] = []

    if topic is not None:
        topic = topic.strip()
        if not topic:
            return False
        updates.append("topic = ?")
        params.append(topic[:500])
    if summary is not None:
        summary = summary.strip()
        if not summary:
            return False
        updates.append("summary = ?")
        params.append(summary[:MAX_MEMORY_SUMMARY_LEN])
    if importance is not None:
        updates.append("importance = ?")
        params.append(max(1, min(5, importance)))

    if not updates:
        return False

    params.extend([memory_id, chat_id])
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            f"UPDATE general_memory SET {', '.join(updates)} WHERE id = ? AND chat_id = ?",
            params,
        )
        if cursor.rowcount == 0:
            return False

        async with db.execute(
            "SELECT topic, summary FROM general_memory WHERE id = ? AND chat_id = ?",
            (memory_id, chat_id),
        ) as row_cursor:
            row = await row_cursor.fetchone()
        if not row:
            return False

        await db.execute(
            "INSERT INTO general_memory_fts(general_memory_fts, rowid) VALUES('delete', ?)",
            (memory_id,),
        )
        await db.execute(
            "INSERT INTO general_memory_fts(rowid, topic, summary) VALUES (?, ?, ?)",
            (memory_id, row[0], row[1]),
        )
        await db.commit()
        return True


async def _migrate_saved_media_schema(db) -> None:
    async with db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='saved_media'"
    ) as cursor:
        row = await cursor.fetchone()
        if not row or not row[0] or "'animation'" in row[0]:
            return

    logging.info("Migrating DB: expanding saved_media media_type to include animation")
    await db.execute("""
        CREATE TABLE saved_media_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            media_unique_id TEXT NOT NULL,
            media_type TEXT NOT NULL CHECK(media_type IN ('photo', 'sticker', 'animation')),
            file_id TEXT NOT NULL,
            description TEXT NOT NULL,
            sender_user_id INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            last_seen_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            last_used_at DATETIME,
            use_count INTEGER NOT NULL DEFAULT 0,
            is_favorite INTEGER NOT NULL DEFAULT 0,
            UNIQUE(chat_id, media_unique_id)
        )
    """)
    await db.execute("""
        INSERT INTO saved_media_new (
            id, chat_id, media_unique_id, media_type, file_id, description,
            sender_user_id, created_at, last_seen_at, last_used_at, use_count
        )
        SELECT
            id, chat_id, media_unique_id, media_type, file_id, description,
            sender_user_id, created_at, last_seen_at, last_used_at, use_count
        FROM saved_media
    """)
    await db.execute("DROP TABLE saved_media")
    await db.execute("ALTER TABLE saved_media_new RENAME TO saved_media")
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_saved_media_chat_seen
        ON saved_media(chat_id, last_seen_at DESC, id DESC)
    """)
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_saved_media_chat_used
        ON saved_media(chat_id, last_used_at DESC, use_count ASC)
    """)


async def _ensure_saved_media_favorite_column(db) -> None:
    async with db.execute("PRAGMA table_info(saved_media)") as cursor:
        columns = {row[1] for row in await cursor.fetchall()}
    if "is_favorite" not in columns:
        logging.info("Migrating DB: adding saved media favorites")
        await db.execute(
            "ALTER TABLE saved_media ADD COLUMN is_favorite INTEGER NOT NULL DEFAULT 0"
        )

async def _prune_saved_media(db, chat_id: int, per_chat_limit: int, global_limit: int) -> None:
    per_chat_limit = max(1, per_chat_limit)
    global_limit = max(1, global_limit)
    
    # Prune per chat (keep newest per_chat_limit by last_seen_at DESC, id DESC)
    await db.execute(
        """
        DELETE FROM saved_media
        WHERE chat_id = ? AND id NOT IN (
            SELECT id FROM saved_media
            WHERE chat_id = ?
            ORDER BY is_favorite DESC, last_seen_at DESC, id DESC
            LIMIT ?
        )
        """,
        (chat_id, chat_id, per_chat_limit)
    )
    
    # Prune globally (keep newest global_limit across all chats by last_seen_at DESC, id DESC)
    await db.execute(
        """
        DELETE FROM saved_media
        WHERE id NOT IN (
            SELECT id FROM saved_media
            ORDER BY is_favorite DESC, last_seen_at DESC, id DESC
            LIMIT ?
        )
        """,
        (global_limit,)
    )


async def save_reusable_media(
    chat_id: int,
    media_unique_id: str,
    file_id: str,
    media_type: Literal["photo", "sticker", "animation"],
    description: str,
    sender_user_id: int | None = None,
    per_chat_limit: int = SAVED_MEDIA_PER_CHAT_LIMIT,
    global_limit: int = SAVED_MEDIA_GLOBAL_LIMIT,
) -> None:
    if not media_unique_id or not file_id or not description.strip():
        return
        
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            INSERT INTO saved_media (
                chat_id, media_unique_id, media_type, file_id, description, sender_user_id, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(chat_id, media_unique_id) DO UPDATE SET
                file_id = excluded.file_id,
                media_type = excluded.media_type,
                description = excluded.description,
                sender_user_id = excluded.sender_user_id,
                last_seen_at = CURRENT_TIMESTAMP
            """,
            (chat_id, media_unique_id, media_type, file_id, description.strip(), sender_user_id)
        )
        await _prune_saved_media(db, chat_id, per_chat_limit, global_limit)
        await db.commit()


async def get_saved_media_options(
    chat_id: int,
    limit: int = SAVED_MEDIA_PROMPT_LIMIT,
    exclude_media_ids: set[str] | None = None,
    media_types: set[Literal["photo", "sticker", "animation"]] | None = None,
) -> list[dict]:
    # Clamp limit to 1..SAVED_MEDIA_PROMPT_LIMIT
    limit = max(1, min(SAVED_MEDIA_PROMPT_LIMIT, limit))
    excluded = sorted(media_id for media_id in (exclude_media_ids or set()) if media_id)
    exclusion_sql = ""
    params: list[object] = [chat_id]
    if excluded:
        placeholders = ", ".join("?" for _ in excluded)
        exclusion_sql = f"AND media_unique_id NOT IN ({placeholders})"
        params.extend(excluded)
    selected_types = sorted(media_types or set())
    if selected_types:
        placeholders = ", ".join("?" for _ in selected_types)
        exclusion_sql += f" AND media_type IN ({placeholders})"
        params.extend(selected_types)
    params.append(limit)

    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"""
            SELECT media_unique_id, media_type, file_id, description, use_count,
                   is_favorite, last_seen_at, last_used_at
            FROM saved_media
            WHERE chat_id = ?
            {exclusion_sql}
            ORDER BY
                is_favorite DESC,
                CASE WHEN last_used_at IS NULL THEN 0 ELSE 1 END,
                use_count ASC,
                last_used_at ASC,
                last_seen_at DESC,
                id DESC
            LIMIT ?
            """,
            params,
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def get_saved_media_by_unique_id(chat_id: int, media_unique_id: str) -> dict | None:
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT id, media_unique_id, media_type, file_id, description, use_count,
                   is_favorite, last_seen_at, last_used_at
            FROM saved_media
            WHERE chat_id = ? AND media_unique_id = ?
            """,
            (chat_id, media_unique_id)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def mark_saved_media_used(chat_id: int, media_unique_id: str) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            UPDATE saved_media
            SET last_used_at = CURRENT_TIMESTAMP,
                use_count = use_count + 1
            WHERE chat_id = ? AND media_unique_id = ?
            """,
            (chat_id, media_unique_id)
        )
        await db.commit()


async def set_saved_media_favorite(
    chat_id: int, media_unique_id: str, favorite: bool
) -> bool:
    """Persist the bot's own taste; Telegram bots have no account Favorites API."""
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            """
            UPDATE saved_media
            SET is_favorite = ?
            WHERE chat_id = ? AND media_unique_id = ? AND media_type = 'sticker'
            """,
            (int(favorite), chat_id, media_unique_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def add_whitelist(entity_id: int, entity_type: str, added_by: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            INSERT INTO whitelist (entity_id, entity_type, added_by)
            VALUES (?, ?, ?)
            ON CONFLICT(entity_id) DO NOTHING
            """,
            (entity_id, entity_type, added_by),
        )
        await db.commit()


async def remove_whitelist(entity_id: int) -> bool:
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "DELETE FROM whitelist WHERE entity_id = ?", (entity_id,)
        )
        await db.commit()
        return cursor.rowcount > 0


async def is_whitelisted(entity_id: int) -> bool:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT 1 FROM whitelist WHERE entity_id = ?", (entity_id,)
        ) as cursor:
            return await cursor.fetchone() is not None


async def get_whitelist() -> list[tuple]:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT entity_id, entity_type, timestamp FROM whitelist"
        ) as cursor:
            return await cursor.fetchall()


async def get_config(key: str) -> str | None:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT value FROM bot_config WHERE key = ?", (key,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


async def set_config(key: str, value: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            INSERT INTO bot_config (key, value) 
            VALUES (?, ?) 
            ON CONFLICT(key) DO UPDATE SET 
                value=excluded.value
            """,
            (key, value),
        )
        await db.commit()


async def get_chat_config(chat_id: int, key: str) -> str | None:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT value FROM chat_config WHERE chat_id = ? AND key = ?",
            (chat_id, key),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


async def set_chat_config(chat_id: int, key: str, value: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            INSERT INTO chat_config (chat_id, key, value) 
            VALUES (?, ?, ?) 
            ON CONFLICT(chat_id, key) DO UPDATE SET 
                value=excluded.value
            """,
            (chat_id, key, value),
        )
        await db.commit()


async def get_all_daily_messages():
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM daily_messages") as cursor:
            return await cursor.fetchall()


async def set_daily_message(
    chat_id: int, time: str, message_type: str, content: str, file_id: str = None
):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            INSERT INTO daily_messages (chat_id, time, message_type, content, file_id)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                time=excluded.time,
                message_type=excluded.message_type,
                content=excluded.content,
                file_id=excluded.file_id
            """,
            (chat_id, time, message_type, content, file_id),
        )
        await db.commit()


async def remove_daily_message(chat_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM daily_messages WHERE chat_id = ?", (chat_id,))
        await db.commit()


async def get_daily_message(chat_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM daily_messages WHERE chat_id = ?", (chat_id,)
        ) as cursor:
            return await cursor.fetchone()


async def get_all_daily_tasks():
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM daily_tasks") as cursor:
            return await cursor.fetchall()


async def set_daily_task(chat_id: int, time: str, task_content: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            INSERT INTO daily_tasks (chat_id, time, task_content)
            VALUES (?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                time=excluded.time,
                task_content=excluded.task_content
            """,
            (chat_id, time, task_content),
        )
        await db.commit()


async def remove_daily_task(chat_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM daily_tasks WHERE chat_id = ?", (chat_id,))
        await db.commit()


async def get_daily_task(chat_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM daily_tasks WHERE chat_id = ?", (chat_id,)
        ) as cursor:
            return await cursor.fetchone()


# ---------------------------------------------------------------------------
# Persona output index (ownership checks and old-message lookup)
# ---------------------------------------------------------------------------

PERSONA_OUTPUT_KINDS = frozenset(
    {"text", "caption", "photo", "sticker", "animation", "poll", "media"}
)
MAX_PERSONA_OUTPUT_EXCERPT_LEN = 1000


def _normalize_persona_output_time(value: object | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        if value.tzinfo is not None:
            value = value.astimezone(datetime.timezone.utc)
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
    text = str(value).strip()
    return text[:40] or None


async def record_persona_output(
    chat_id: int,
    message_id: int,
    content_kind: str,
    text: str | None = None,
    *,
    sent_at: object | None = None,
) -> bool:
    """Record an outgoing persona message without reviving an old terminal row."""
    content_kind = str(content_kind).strip().lower()
    if content_kind not in PERSONA_OUTPUT_KINDS:
        raise ValueError(f"Invalid persona output kind: {content_kind}")
    excerpt = str(text or "").strip()[:MAX_PERSONA_OUTPUT_EXCERPT_LEN] or None
    normalized_time = _normalize_persona_output_time(sent_at)

    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            """
            INSERT INTO persona_outputs (
                chat_id, message_id, content_kind, text_excerpt, sent_at
            ) VALUES (?, ?, ?, ?, COALESCE(?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now')))
            ON CONFLICT(chat_id, message_id) DO NOTHING
            """,
            (chat_id, message_id, content_kind, excerpt, normalized_time),
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_persona_output(chat_id: int, message_id: int) -> dict | None:
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM persona_outputs WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def mark_persona_output_edited(
    chat_id: int, message_id: int, replacement_text: str
) -> bool:
    excerpt = replacement_text.strip()[:MAX_PERSONA_OUTPUT_EXCERPT_LEN]
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            """
            UPDATE persona_outputs
            SET state = 'edited', text_excerpt = ?,
                changed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            WHERE chat_id = ? AND message_id = ? AND state IN ('active', 'edited')
            """,
            (excerpt, chat_id, message_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def mark_persona_output_deleted(chat_id: int, message_id: int) -> bool:
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            """
            UPDATE persona_outputs
            SET state = 'deleted',
                changed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            WHERE chat_id = ? AND message_id = ? AND state IN ('active', 'edited')
            """,
            (chat_id, message_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def search_persona_outputs(
    chat_id: int, query: str, *, limit: int = 10
) -> list[dict]:
    """Search the bot's own sent-message index within one chat."""
    query = str(query or "").strip()[:500]
    limit = max(1, min(int(limit), 20))
    id_match = re.search(r"\bmessage(?:_id)?\s*[=:]?\s*(\d+)\b", query, re.I)
    if query.isdigit():
        exact_id = int(query)
    elif id_match:
        exact_id = int(id_match.group(1))
    else:
        exact_id = None
    search_terms = [query.lower()] if query else []
    for token in re.findall(r"[\w-]{3,}", query.lower()):
        if token not in search_terms:
            search_terms.append(token)
        if len(search_terms) >= 8:
            break

    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        if exact_id is not None:
            sql = """
                SELECT * FROM persona_outputs
                WHERE chat_id = ? AND message_id = ?
                ORDER BY sent_at DESC
                LIMIT ?
            """
            params: tuple = (chat_id, exact_id, limit)
        elif query:
            predicates = " OR ".join(
                "lower(COALESCE(text_excerpt, '')) LIKE ?" for _ in search_terms
            )
            sql = f"""
                SELECT * FROM persona_outputs
                WHERE chat_id = ? AND ({predicates})
                ORDER BY sent_at DESC, message_id DESC
                LIMIT ?
            """
            params = (chat_id, *(f"%{term}%" for term in search_terms), limit)
        else:
            sql = """
                SELECT * FROM persona_outputs
                WHERE chat_id = ?
                ORDER BY sent_at DESC, message_id DESC
                LIMIT ?
            """
            params = (chat_id, limit)
        async with db.execute(sql, params) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def record_persona_reaction(
    chat_id: int, message_id: int, emoji: str | None
) -> None:
    state = "active" if emoji else "removed"
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            INSERT INTO persona_reactions (
                chat_id, message_id, emoji, state, reacted_at, changed_at
            ) VALUES (
                ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
                strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            )
            ON CONFLICT(chat_id, message_id) DO UPDATE SET
                emoji = excluded.emoji,
                state = excluded.state,
                changed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            """,
            (chat_id, message_id, emoji, state),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Scheduled actions (LLM-driven deferred messages / research / tasks)
# ---------------------------------------------------------------------------

MAX_PENDING_SCHEDULED_ACTIONS_PER_CHAT = 20
MAX_ACTIVE_EVENT_STATES_PER_CHAT = 15
SCHEDULED_ACTION_TYPES = frozenset({"reply", "message", "research", "task"})


def _row_to_dict(row) -> dict:
    return dict(row) if row is not None else {}


async def count_pending_scheduled_actions(chat_id: int) -> int:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM scheduled_actions WHERE chat_id = ? AND status = 'pending'",
            (chat_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row else 0


async def add_scheduled_action(
    chat_id: int,
    *,
    action_type: str,
    execute_at: str,
    reason: str,
    instruction: str,
    context: str | None = None,
    target_user_id: int | None = None,
    target_username: str | None = None,
    reply_to_message_id: int | None = None,
) -> int:
    if action_type not in SCHEDULED_ACTION_TYPES:
        raise ValueError(f"Invalid action_type: {action_type}")
    reason = (reason or "").strip()[:1000]
    instruction = (instruction or "").strip()[:2000]
    context = (context or "").strip()[:4000] or None
    if not reason:
        raise ValueError("reason is required")
    if not instruction:
        raise ValueError("instruction is required")

    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            """
            INSERT INTO scheduled_actions (
                chat_id, action_type, execute_at, reason, instruction, context,
                target_user_id, target_username, reply_to_message_id, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            """,
            (
                chat_id,
                action_type,
                execute_at,
                reason,
                instruction,
                context,
                target_user_id,
                target_username,
                reply_to_message_id,
            ),
        )
        await db.commit()
        return int(cursor.lastrowid)


async def get_scheduled_action(action_id: int) -> dict | None:
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM scheduled_actions WHERE id = ?", (action_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_dict(row) if row else None


async def list_pending_scheduled_actions(
    chat_id: int | None = None, *, limit: int = 50
) -> list[dict]:
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        if chat_id is None:
            query = """
                SELECT * FROM scheduled_actions
                WHERE status = 'pending'
                ORDER BY execute_at ASC
                LIMIT ?
            """
            params: tuple = (limit,)
        else:
            query = """
                SELECT * FROM scheduled_actions
                WHERE chat_id = ? AND status = 'pending'
                ORDER BY execute_at ASC
                LIMIT ?
            """
            params = (chat_id, limit)
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_dict(r) for r in rows]


async def get_due_scheduled_actions(now_iso: str, *, limit: int = 20) -> list[dict]:
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM scheduled_actions
            WHERE status = 'pending' AND execute_at <= ?
            ORDER BY execute_at ASC
            LIMIT ?
            """,
            (now_iso, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_dict(r) for r in rows]


async def claim_scheduled_action(action_id: int) -> bool:
    """Mark pending action as running. Returns False if already claimed/cancelled."""
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            """
            UPDATE scheduled_actions
            SET status = 'running'
            WHERE id = ? AND status = 'pending'
            """,
            (action_id,),
        )
        await db.commit()
        return cursor.rowcount > 0


async def complete_scheduled_action(
    action_id: int, *, status: str = "done", error_message: str | None = None
) -> None:
    if status not in ("done", "failed", "cancelled"):
        raise ValueError(f"Invalid terminal status: {status}")
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            UPDATE scheduled_actions
            SET status = ?, completed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
                error_message = ?
            WHERE id = ?
            """,
            (status, (error_message or "")[:500] or None, action_id),
        )
        await db.commit()


async def cancel_scheduled_action(action_id: int, chat_id: int | None = None) -> bool:
    async with aiosqlite.connect(DB_NAME) as db:
        if chat_id is None:
            cursor = await db.execute(
                """
                UPDATE scheduled_actions
                SET status = 'cancelled',
                    completed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
                WHERE id = ? AND status = 'pending'
                """,
                (action_id,),
            )
        else:
            cursor = await db.execute(
                """
                UPDATE scheduled_actions
                SET status = 'cancelled',
                    completed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
                WHERE id = ? AND chat_id = ? AND status = 'pending'
                """,
                (action_id, chat_id),
            )
        await db.commit()
        return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Event states (time-bounded moods / ignore / attitude)
# ---------------------------------------------------------------------------

async def count_active_event_states(chat_id: int) -> int:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            """
            SELECT COUNT(*) FROM event_states
            WHERE chat_id = ? AND active = 1 AND expires_at > strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            """,
            (chat_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row else 0


async def add_event_state(
    chat_id: int,
    *,
    state_key: str,
    value: str,
    expires_at: str,
    reason: str | None = None,
    target_user_id: int | None = None,
    target_username: str | None = None,
) -> int:
    state_key = (state_key or "").strip().lower()[:64]
    value = (value or "").strip()[:1000]
    reason = (reason or "").strip()[:1000] or None
    if not state_key:
        raise ValueError("state_key is required")
    if not value:
        raise ValueError("value is required")

    async with aiosqlite.connect(DB_NAME) as db:
        # Replace existing active state with same key + target scope in this chat
        if target_user_id is None:
            await db.execute(
                """
                UPDATE event_states SET active = 0
                WHERE chat_id = ? AND state_key = ? AND target_user_id IS NULL AND active = 1
                """,
                (chat_id, state_key),
            )
        else:
            await db.execute(
                """
                UPDATE event_states SET active = 0
                WHERE chat_id = ? AND state_key = ? AND target_user_id = ? AND active = 1
                """,
                (chat_id, state_key, target_user_id),
            )
        cursor = await db.execute(
            """
            INSERT INTO event_states (
                chat_id, state_key, value, reason, target_user_id, target_username,
                expires_at, active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                chat_id,
                state_key,
                value,
                reason,
                target_user_id,
                target_username,
                expires_at,
            ),
        )
        await db.commit()
        return int(cursor.lastrowid)


async def list_active_event_states(
    chat_id: int | None = None, *, limit: int = 50
) -> list[dict]:
    """Return non-expired active states. Pass chat_id=None for all chats (job cleanup)."""
    now_clause = "expires_at > strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        if chat_id is None:
            query = f"""
                SELECT * FROM event_states
                WHERE active = 1 AND {now_clause}
                ORDER BY expires_at ASC
                LIMIT ?
            """
            params: tuple = (limit,)
        else:
            query = f"""
                SELECT * FROM event_states
                WHERE chat_id = ? AND active = 1 AND {now_clause}
                ORDER BY expires_at ASC
                LIMIT ?
            """
            params = (chat_id, limit)
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_dict(r) for r in rows]


async def get_event_state(state_id: int) -> dict | None:
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM event_states WHERE id = ?", (state_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_dict(row) if row else None


async def clear_event_state(
    state_id: int | None = None,
    *,
    chat_id: int | None = None,
    state_key: str | None = None,
    target_user_id: int | None = None,
) -> int:
    """Deactivate event state(s). Returns number of rows cleared."""
    async with aiosqlite.connect(DB_NAME) as db:
        if state_id is not None:
            if chat_id is None:
                cursor = await db.execute(
                    "UPDATE event_states SET active = 0 WHERE id = ? AND active = 1",
                    (state_id,),
                )
            else:
                cursor = await db.execute(
                    """
                    UPDATE event_states SET active = 0
                    WHERE id = ? AND chat_id = ? AND active = 1
                    """,
                    (state_id, chat_id),
                )
            await db.commit()
            return cursor.rowcount

        if chat_id is None or not state_key:
            raise ValueError("Provide state_id, or chat_id + state_key")

        state_key = state_key.strip().lower()
        if target_user_id is None:
            cursor = await db.execute(
                """
                UPDATE event_states SET active = 0
                WHERE chat_id = ? AND state_key = ? AND active = 1
                """,
                (chat_id, state_key),
            )
        else:
            cursor = await db.execute(
                """
                UPDATE event_states SET active = 0
                WHERE chat_id = ? AND state_key = ? AND target_user_id = ? AND active = 1
                """,
                (chat_id, state_key, target_user_id),
            )
        await db.commit()
        return cursor.rowcount


async def expire_event_states(now_iso: str | None = None) -> int:
    """Mark expired active states as inactive. Returns count expired."""
    async with aiosqlite.connect(DB_NAME) as db:
        if now_iso:
            cursor = await db.execute(
                """
                UPDATE event_states SET active = 0
                WHERE active = 1 AND expires_at <= ?
                """,
                (now_iso,),
            )
        else:
            cursor = await db.execute(
                """
                UPDATE event_states SET active = 0
                WHERE active = 1
                  AND expires_at <= strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
                """
            )
        await db.commit()
        return cursor.rowcount


async def is_user_ignored(chat_id: int, user_id: int | None) -> tuple[bool, str | None]:
    """
    Check ignore event states for chat-wide or user-specific ignore.
    Returns (is_ignored, reason).
    """
    try:
        states = await list_active_event_states(chat_id, limit=50)
    except Exception as e:
        logging.warning("is_user_ignored failed for chat %s: %s", chat_id, e)
        return False, None
    ignore_keys = {"ignore", "ignoring", "silent", "silent_treatment"}
    for state in states:
        key = (state.get("state_key") or "").lower()
        if key not in ignore_keys:
            continue
        target = state.get("target_user_id")
        if target is None:
            return True, state.get("reason") or state.get("value")
        if user_id is not None and int(target) == int(user_id):
            return True, state.get("reason") or state.get("value")
    return False, None
