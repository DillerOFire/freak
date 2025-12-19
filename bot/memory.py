import aiosqlite
import datetime
import logging

DB_NAME = "bot_memory.db"


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
        await db.commit()
        logging.info("DEBUG: Committed user thought to DB")


async def get_general_memories(chat_id: int, limit: int = 5) -> list[str]:
    async with aiosqlite.connect(DB_NAME) as db:
        # Filter by chat_id. We handle NULL chat_id as global or just ignore?
        # Plan said: "Existing memories will have NULL. I will update queries to filter by chat_id."
        # So we only fetch memories for this chat_id.
        async with db.execute(
            "SELECT topic, summary FROM general_memory WHERE chat_id = ? ORDER BY timestamp DESC LIMIT ?",
            (chat_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [f"Topic: {row[0]}, Summary: {row[1]}" for row in rows]


async def add_general_memory(topic: str, summary: str, chat_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO general_memory (topic, summary, chat_id) VALUES (?, ?, ?)",
            (topic, summary, chat_id),
        )
        await db.commit()


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
