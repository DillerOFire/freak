import pytest
from unittest.mock import AsyncMock, MagicMock, patch, ANY
from bot import handlers


@pytest.fixture
def mock_context(mock_context):
    mock_context.bot.send_message = AsyncMock(return_value=MagicMock())
    mock_context.bot.send_video = AsyncMock()
    mock_context.bot.set_message_reaction = AsyncMock()
    return mock_context


@pytest.fixture
def mock_update_handler(mock_update):
    mock_update.message.from_user.id = 67890
    mock_update.message.from_user.username = "test_user"
    mock_update.effective_chat.id = 12345
    return mock_update


@pytest.mark.asyncio
async def test_handle_message_paused(temp_db_path, mock_update_handler, mock_context):
    """Test that suspended bot ignores messages."""
    with patch("bot.handlers.get_paused", return_value=True):
        await handlers.handle_message(mock_update_handler, mock_context)
        mock_context.bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_handle_message_reply_logic(temp_db_path, mock_update_handler, mock_context):
    """Test that bot replies when should_reply is True."""
    mock_update_handler.message.text = "Hello bot"

    with (
        patch("bot.handlers.get_paused", return_value=False),
        patch("bot.handlers.is_whitelisted", new_callable=AsyncMock) as mock_whitelist,
        patch("bot.handlers.should_reply", new_callable=AsyncMock) as mock_should_reply,
        patch("bot.handlers.generate_response", new_callable=AsyncMock) as mock_llm,
        patch(
            "bot.handlers.get_message_media_description", new_callable=AsyncMock
        ) as mock_media_desc,
    ):
        mock_whitelist.return_value = True
        mock_should_reply.return_value = True
        mock_llm.return_value = {"content": "Hello human"}
        mock_media_desc.return_value = None

        await handlers.handle_message(mock_update_handler, mock_context)

        mock_llm.assert_called_once()
        mock_context.bot.send_message.assert_called_once_with(
            chat_id=12345, text="Hello human"
        )


@pytest.mark.asyncio
async def test_handle_message_reaction(temp_db_path, mock_update_handler, mock_context):
    """Test reaction logic."""
    mock_update_handler.message.text = "Funny joke"

    with (
        patch("bot.handlers.get_paused", return_value=False),
        patch("bot.handlers.is_whitelisted", new_callable=AsyncMock) as mock_whitelist,
        patch("bot.handlers.should_reply", new_callable=AsyncMock) as mock_should_reply,
        patch("bot.handlers.should_react", new_callable=AsyncMock) as mock_should_react,
        patch(
            "bot.handlers.generate_reaction", new_callable=AsyncMock
        ) as mock_gen_react,
        patch(
            "bot.handlers.get_message_media_description", new_callable=AsyncMock
        ) as mock_media_desc,
    ):
        mock_whitelist.return_value = True
        mock_should_reply.return_value = False
        mock_should_react.return_value = True
        mock_gen_react.return_value = "👍"
        mock_media_desc.return_value = None

        await handlers.handle_message(mock_update_handler, mock_context)

        mock_context.bot.set_message_reaction.assert_called_once_with(
            chat_id=12345, message_id=ANY, reaction="👍"
        )


@pytest.mark.asyncio
async def test_handle_message_ignore(temp_db_path, mock_update_handler, mock_context):
    """Test that bot ignores when should_reply is False."""
    mock_update_handler.message.text = "Just talking"

    with (
        patch("bot.handlers.get_paused", return_value=False),
        patch("bot.handlers.is_whitelisted", new_callable=AsyncMock) as mock_whitelist,
        patch("bot.handlers.should_reply", new_callable=AsyncMock) as mock_should_reply,
        patch(
            "bot.handlers.get_message_media_description", new_callable=AsyncMock
        ) as mock_media_desc,
    ):
        mock_whitelist.return_value = True
        mock_should_reply.return_value = False
        mock_media_desc.return_value = None

        await handlers.handle_message(mock_update_handler, mock_context)

        mock_context.bot.send_message.assert_not_called()


# We can add more tests for media handling, etc. but this covers core logic.
