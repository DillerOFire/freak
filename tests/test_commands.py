import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from bot import commands


# Mock ADMIN_ID in config
# We can patch it in the test or fixture
@pytest.fixture
def mock_admin_update(mock_update):
    with patch(
        "bot.commands.ADMIN_ID", 12345
    ):  # Match mock_update.effective_chat.id or user.id
        mock_update.effective_user.id = 12345
        yield mock_update


@pytest.mark.asyncio
async def test_start_command(mock_admin_update, mock_context):
    """Test /start command."""
    with patch("bot.commands.set_paused", new_callable=AsyncMock) as mock_set_paused:
        # Mock reply_text as AsyncMock because it is awaited
        mock_admin_update.message.reply_text = AsyncMock()
        await commands.start_command(mock_admin_update, mock_context)

        mock_set_paused.assert_called_once_with(False)
        mock_admin_update.message.reply_text.assert_called_once_with("Bot resumed.")


@pytest.mark.asyncio
async def test_stop_command(mock_admin_update, mock_context):
    """Test /stop command."""
    with patch("bot.commands.set_paused", new_callable=AsyncMock) as mock_set_paused:
        # Mock reply_text as AsyncMock
        mock_admin_update.message.reply_text = AsyncMock()
        await commands.stop_command(mock_admin_update, mock_context)

        mock_set_paused.assert_called_once_with(True)
        mock_admin_update.message.reply_text.assert_called_once_with("Bot paused.")


@pytest.mark.asyncio
async def test_help_command(mock_update, mock_context):
    """Test /help command."""
    mock_update.message.reply_text = AsyncMock()
    await commands.help_command(mock_update, mock_context)
    mock_update.message.reply_text.assert_called_once()
    args, _ = mock_update.message.reply_text.call_args
    args, _ = mock_update.message.reply_text.call_args
    assert "Available Commands" in args[0]


@pytest.mark.asyncio
async def test_ping_command(mock_update, mock_context):
    """Test /ping command."""
    # Mock add_message_to_history as it's called after reply
    with patch("bot.commands.add_message_to_history") as mock_add_hist:
        mock_update.message.reply_text = AsyncMock()
        mock_update.message.reply_text.return_value = (
            MagicMock()
        )  # Return a mock message

        await commands.ping_command(mock_update, mock_context)

        mock_update.message.reply_text.assert_called_once()
        mock_add_hist.assert_called_once()


@pytest.mark.asyncio
async def test_music_command(mock_update, mock_context):
    """Test /music command."""
    mock_context.args = ["https://youtube.com/watch?v=123"]

    # Mock get_utils_disabled to return False
    with (
        patch("bot.commands.get_utils_disabled", new_callable=AsyncMock) as mock_utils,
        patch(
            "bot.media_utils.download_audio_ytdlp", new_callable=MagicMock
        ) as mock_download,
        patch("builtins.open", new_callable=MagicMock),
        patch("os.remove"),
    ):
        mock_utils.return_value = False
        mock_download.return_value = {
            "audio_path": "test.mp3",
            "title": "Test Song",
            "description": "Desc",
            "thumbnail_path": "thumb.jpg",
            "duration": 100,
            "uploader": "Artist",
        }

        # Mock reply_audio and reply_text as AsyncMock
        mock_update.message.reply_audio = AsyncMock()
        mock_update.message.reply_text = AsyncMock()

        await commands.music_command(mock_update, mock_context)

        mock_update.message.reply_audio.assert_called_once()
        mock_download.assert_called_once()
