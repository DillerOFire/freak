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
