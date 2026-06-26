import pytest
import aiosqlite
from bot import memory


@pytest.mark.asyncio
async def test_user_memory(temp_db_path):
    """Test user thought storage."""
    user_id = 123
    username = "test_user"
    thought = "Thinking about tests"

    await memory.update_user_thought(user_id, username, thought)

    saved_thought = await memory.get_user_thought(user_id)
    assert saved_thought == thought


@pytest.mark.asyncio
async def test_general_memory(temp_db_path):
    """Test general memory storage."""
    topic = "Testing"
    summary = "We are testing the bot"
    chat_id = 12345

    await memory.add_general_memory(topic, summary, chat_id)

    memories = await memory.get_general_memories(chat_id)
    assert len(memories) == 1
    assert "Topic: Testing" in memories[0]
    assert "Summary: We are testing the bot" in memories[0]


@pytest.mark.asyncio
async def test_whitelist(temp_db_path):
    """Test whitelist operations."""
    entity_id = 999
    entity_type = "user"
    added_by = 1

    await memory.add_whitelist(entity_id, entity_type, added_by)
    assert await memory.is_whitelisted(entity_id) is True

    await memory.remove_whitelist(entity_id)
    assert await memory.is_whitelisted(entity_id) is False


@pytest.mark.asyncio
async def test_daily_message_crud(temp_db_path):
    """Test daily message CRUD."""
    chat_id = 555
    time = "12:00"
    msg_type = "text"
    content = "Daily hello"

    # Create
    await memory.set_daily_message(chat_id, time, msg_type, content)

    # Read
    msg = await memory.get_daily_message(chat_id)
    assert msg is not None
    assert msg["chat_id"] == chat_id
    assert msg["content"] == content

    # Update
    new_content = "Updated hello"
    await memory.set_daily_message(chat_id, time, msg_type, new_content)
    msg = await memory.get_daily_message(chat_id)
    assert msg["content"] == new_content

    # Delete
    await memory.remove_daily_message(chat_id)
    msg = await memory.get_daily_message(chat_id)
    assert msg is None


@pytest.mark.asyncio
async def test_bot_config(temp_db_path):
    """Test bot configuration."""
    key = "test_key"
    value = "test_value"

    await memory.set_config(key, value)
    fetched_value = await memory.get_config(key)
    assert fetched_value == value

    await memory.set_config(key, "new_value")
    fetched_value = await memory.get_config(key)
    assert fetched_value == "new_value"


@pytest.mark.asyncio
async def test_chat_config(temp_db_path):
    """Test chat-specific configuration."""
    chat_id = 111
    key = "reaction_chance"
    value = "0.5"

    await memory.set_chat_config(chat_id, key, value)
    fetched_value = await memory.get_chat_config(chat_id, key)
    assert fetched_value == value

    # Test separation of chats
    chat_id_2 = 222
    assert await memory.get_chat_config(chat_id_2, key) is None


@pytest.mark.asyncio
async def test_relevance_ranked_retrieval(temp_db_path):
    """Test FTS5 relevance-ranked memory retrieval."""
    chat_id = 12345
    # Insert multiple general memories
    await memory.add_general_memory("Оперный театр", "Маэстро обожает оперу Верди Травиата", chat_id, importance=5)
    await memory.add_general_memory("Кулинария", "Рецепт блинов от бабушки", chat_id, importance=2)
    
    # Retrieval matches opera query
    results = await memory.get_relevant_general_memories(chat_id, "опера Верди", limit=1)
    assert len(results) == 1
    assert "Оперный театр" in results[0]
    
    # Fallback to recent on term mismatch
    fallback = await memory.get_relevant_general_memories(chat_id, "космос", limit=2)
    assert len(fallback) == 2
    assert "Оперный театр" in fallback[0] or "Оперный театр" in fallback[1]


@pytest.mark.asyncio
async def test_user_memory_fts_and_target_lookups(temp_db_path):
    """Test user thought update triggers FTS indexing and supports targeted lookups."""
    # Update thought
    await memory.update_user_thought(123, "alice", "Alice likes opera and champagne")
    
    # Search
    search_results = await memory.search_user_memories("opera", limit=5)
    assert len(search_results) == 1
    assert search_results[0] == (123, "alice", "Alice likes opera and champagne")
    
    # Target username
    target_uname = await memory.get_user_memory_by_target("@alice")
    assert target_uname == (123, "alice", "Alice likes opera and champagne")
    
    # Target user_id
    target_uid = await memory.get_user_memory_by_target("123")
    assert target_uid == (123, "alice", "Alice likes opera and champagne")

@pytest.mark.asyncio
async def test_saved_media_crud_and_limits(temp_db_path):
    # Save first media
    await memory.save_reusable_media(
        chat_id=12345,
        media_unique_id="photo_u1",
        file_id="photo_f1",
        media_type="photo",
        description="first photo",
        sender_user_id=111,
        per_chat_limit=3,
        global_limit=10,
    )
    
    # Assert get_saved_media_options returns one row
    options = await memory.get_saved_media_options(12345)
    assert len(options) == 1
    assert options[0]["media_unique_id"] == "photo_u1"
    assert options[0]["media_type"] == "photo"
    assert options[0]["file_id"] == "photo_f1"
    assert options[0]["use_count"] == 0
    assert options[0]["description"] == "first photo"
    
    # Upsert the same media
    await memory.save_reusable_media(
        chat_id=12345,
        media_unique_id="photo_u1",
        file_id="photo_f2",
        media_type="photo",
        description="updated photo",
        sender_user_id=111,
        per_chat_limit=3,
        global_limit=10,
    )
    
    options = await memory.get_saved_media_options(12345)
    assert len(options) == 1
    assert options[0]["file_id"] == "photo_f2"
    assert options[0]["description"] == "updated photo"
    
    # Save up to limit
    await memory.save_reusable_media(12345, "photo_u2", "photo_f3", "photo", "photo 2", 111, per_chat_limit=3)
    await memory.save_reusable_media(12345, "photo_u3", "photo_f4", "photo", "photo 3", 111, per_chat_limit=3)
    await memory.save_reusable_media(12345, "photo_u4", "photo_f5", "photo", "photo 4", 111, per_chat_limit=3)
    
    # Assert only 3 rows remain and photo_u1 is pruned (newest by last_seen_at DESC)
    options = await memory.get_saved_media_options(12345)
    assert len(options) == 3
    media_ids = [opt["media_unique_id"] for opt in options]
    assert "photo_u1" not in media_ids
    assert "photo_u4" in media_ids
    
    # Mark used
    await memory.mark_saved_media_used(12345, "photo_u4")
    val = await memory.get_saved_media_by_unique_id(12345, "photo_u4")
    assert val is not None
    assert val["use_count"] == 1

    # Save gif media type
    await memory.save_reusable_media(
        chat_id=12345,
        media_unique_id="gif_u1",
        file_id="gif_f1",
        media_type="animation",
        description="cat dancing",
        sender_user_id=111,
        per_chat_limit=10,
        global_limit=10,
    )
    gif = await memory.get_saved_media_by_unique_id(12345, "gif_u1")
    assert gif is not None
    assert gif["media_type"] == "animation"


@pytest.mark.asyncio
async def test_saved_media_global_limit(temp_db_path):
    # Save four rows across two chats with per_chat_limit=10, global_limit=3
    await memory.save_reusable_media(11, "m1", "f1", "photo", "desc", 111, per_chat_limit=10, global_limit=3)
    await memory.save_reusable_media(11, "m2", "f2", "photo", "desc", 111, per_chat_limit=10, global_limit=3)
    await memory.save_reusable_media(22, "m3", "f3", "photo", "desc", 111, per_chat_limit=10, global_limit=3)
    await memory.save_reusable_media(22, "m4", "f4", "photo", "desc", 111, per_chat_limit=10, global_limit=3)
    
    async with aiosqlite.connect(memory.DB_NAME) as db:
        async with db.execute("SELECT COUNT(*) FROM saved_media") as cursor:
            row = await cursor.fetchone()
            assert row[0] == 3


@pytest.mark.asyncio
async def test_general_memory_update_and_delete(temp_db_path):
    chat_id = 100
    other_chat = 200
    await memory.add_general_memory("Topic A", "Summary A", chat_id)
    await memory.add_general_memory("Topic B", "Summary B", other_chat)

    memories = await memory.get_general_memories(chat_id)
    assert len(memories) == 1
    assert memories[0].startswith("id=")

    memory_id = int(memories[0].split(",")[0].removeprefix("id="))

    assert await memory.update_general_memory(
        memory_id, chat_id, summary="Updated summary", importance=5
    )
    updated = await memory.get_general_memories(chat_id)
    assert "Updated summary" in updated[0]

    assert await memory.delete_general_memory(memory_id, chat_id) is True
    assert await memory.get_general_memories(chat_id) == []

    # Cannot delete from wrong chat
    other_memories = await memory.get_general_memories(other_chat)
    other_id = int(other_memories[0].split(",")[0].removeprefix("id="))
    assert await memory.delete_general_memory(other_id, chat_id) is False
    assert len(await memory.get_general_memories(other_chat)) == 1

    assert await memory.delete_general_memory(0, chat_id) is False


@pytest.mark.asyncio
async def test_media_description_clear_update_and_search(temp_db_path):
    await memory.save_media_description("vid_abc", "A cat playing piano")
    assert await memory.get_media_description("vid_abc") == "A cat playing piano"

    results = await memory.search_media_descriptions("cat piano")
    assert len(results) == 1
    assert "vid_abc" in results[0]

    await memory.save_media_description("vid_abc", "A dog on a skateboard")
    assert await memory.get_media_description("vid_abc") == "A dog on a skateboard"

    assert await memory.clear_media_description("vid_abc") is True
    assert await memory.get_media_description("vid_abc") is None
    assert await memory.clear_media_description("vid_abc") is False
    assert await memory.clear_media_description("") is False


@pytest.mark.asyncio
async def test_update_saved_media_description(temp_db_path):
    await memory.save_reusable_media(
        chat_id=42,
        media_unique_id="gif_u1",
        file_id="gif_f1",
        media_type="animation",
        description="old desc",
        sender_user_id=1,
    )
    assert await memory.update_saved_media_description(42, "gif_u1", "new desc")
    row = await memory.get_saved_media_by_unique_id(42, "gif_u1")
    assert row["description"] == "new desc"
