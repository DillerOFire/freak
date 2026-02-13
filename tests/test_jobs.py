import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from bot import jobs


@pytest.mark.asyncio
async def test_daily_message_callback_text(mock_context):
    """Test daily text message sending."""
    mock_context.job.data = {
        "chat_id": 123,
        "content": "Good morning",
        "message_type": "text",
    }

    await jobs.send_daily_message_callback(mock_context)

    mock_context.bot.send_message.assert_called_once_with(
        chat_id=123, text="Good morning"
    )


@pytest.mark.asyncio
async def test_daily_message_callback_photo(mock_context):
    """Test daily photo sending."""
    mock_context.job.data = {
        "chat_id": 123,
        "content": "Morning view",
        "message_type": "photo",
        "file_id": "file_123",
    }

    await jobs.send_daily_message_callback(mock_context)

    mock_context.bot.send_photo.assert_called_once_with(
        chat_id=123, photo="file_123", caption="Morning view"
    )


@pytest.mark.asyncio
async def test_execute_daily_task_callback(mock_context):
    """Test daily task execution."""
    mock_context.job.data = {"chat_id": 123, "task_content": "Say hello"}

    with (
        patch("bot.jobs.get_general_memories", new_callable=AsyncMock) as mock_memories,
        patch("bot.jobs.generate_response", new_callable=AsyncMock) as mock_llm,
    ):
        mock_memories.return_value = []
        mock_llm.return_value = {"content": "Hello there"}

        await jobs.execute_daily_task_callback(mock_context)

        mock_context.bot.send_message.assert_called_once_with(
            chat_id=123, text="Hello there"
        )
