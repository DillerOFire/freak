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
                UNIQUE(chat_id, media_unique_id)
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_saved_media_chat_seen 
            ON saved_media(chat_id, last_seen_at DESC, id DESC)
        """)

        await _migrate_saved_media_schema(db)
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
            ORDER BY last_seen_at DESC, id DESC
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
            ORDER BY last_seen_at DESC, id DESC
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
) -> list[dict]:
    # Clamp limit to 1..SAVED_MEDIA_PROMPT_LIMIT
    limit = max(1, min(SAVED_MEDIA_PROMPT_LIMIT, limit))
    
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT media_unique_id, media_type, file_id, description, use_count, last_seen_at, last_used_at
            FROM saved_media
            WHERE chat_id = ?
            ORDER BY last_seen_at DESC, id DESC
            LIMIT ?
            """,
            (chat_id, limit)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def get_saved_media_by_unique_id(chat_id: int, media_unique_id: str) -> dict | None:
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT id, media_unique_id, media_type, file_id, description, use_count, last_seen_at, last_used_at
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
