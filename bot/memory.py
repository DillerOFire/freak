import aiosqlite
import datetime
import logging
import re
import os

# Use absolute path for DB to avoid issues with CWD
DB_NAME = os.path.join(os.path.dirname(os.path.dirname(__file__)), "bot_memory.db")


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


async def get_general_memories(chat_id: int, limit: int = 5) -> list[str]:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT topic, summary FROM general_memory WHERE chat_id = ? ORDER BY timestamp DESC LIMIT ?",
            (chat_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [f"Topic: {row[0]}, Summary: {row[1]}" for row in rows]


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
            
            return [f"Topic: {row[1]}, Summary: {row[2]}" for row in rows]
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
            
            return [f"Topic: {row[1]}, Summary: {row[2]}" for row in rows]
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
