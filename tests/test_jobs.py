import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from bot import jobs


@pytest.fixture
def mock_context(mock_context):
    mock_context.bot.send_message = AsyncMock(return_value=MagicMock())
    mock_context.bot.send_photo = AsyncMock()
    mock_context.bot.send_video = AsyncMock()
    mock_context.bot.set_message_reaction = AsyncMock()
    return mock_context


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
        patch("bot.jobs.get_relevant_general_memories", new_callable=AsyncMock) as mock_memories,
        patch("bot.jobs.generate_response", new_callable=AsyncMock) as mock_llm,
    ):
        mock_memories.return_value = []
        mock_llm.return_value = {"messages": ["Hello there", "Second message"]}

        await jobs.execute_daily_task_callback(mock_context)

        assert mock_llm.call_args.kwargs["source"] == "daily_task"
        assert mock_llm.call_args.kwargs["memory_query"] == "Say hello"
        assert mock_context.bot.send_message.call_count == 2
        mock_context.bot.send_message.assert_any_call(
            chat_id=123, text="Hello there"
        )
        mock_context.bot.send_message.assert_any_call(
            chat_id=123, text="Second message"
        )
