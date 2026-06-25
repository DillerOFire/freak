import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from bot import commands


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
        mock_admin_update.message.reply_text = AsyncMock()
        await commands.start_command(mock_admin_update, mock_context)

        mock_set_paused.assert_called_once_with(False)
        mock_admin_update.message.reply_text.assert_called_once_with("Bot resumed.")


@pytest.mark.asyncio
async def test_stop_command(mock_admin_update, mock_context):
    """Test /stop command."""
    with patch("bot.commands.set_paused", new_callable=AsyncMock) as mock_set_paused:
        mock_admin_update.message.reply_text = AsyncMock()
        await commands.stop_command(mock_admin_update, mock_context)

        mock_set_paused.assert_called_once_with(True)
        mock_admin_update.message.reply_text.assert_called_once_with("Bot paused.")


@pytest.mark.asyncio
async def test_update_prompt_command_generates_reaction_prompt(mock_admin_update, mock_context):
    """Test /update_prompt also refreshes the stored reaction prompt."""
    mock_admin_update.message.text = "/update_prompt New persona"
    mock_admin_update.message.reply_text = AsyncMock()
    mock_context.args = ["New", "persona"]

    with (
        patch("bot.commands.set_config", new_callable=AsyncMock) as mock_set_config,
        patch("bot.commands.generate_reaction_prompt", new_callable=AsyncMock) as mock_generate,
    ):
        mock_generate.return_value = "generated reaction prompt"

        await commands.update_prompt_command(mock_admin_update, mock_context)

    mock_generate.assert_called_once_with("New persona")
    mock_set_config.assert_any_call("persona_prompt", "New persona")
    mock_set_config.assert_any_call("reaction_prompt", "generated reaction prompt")
    mock_admin_update.message.reply_text.assert_called_once_with(
        "System and reaction prompts updated successfully."
    )


@pytest.mark.asyncio
async def test_settings_command_shows_inline_panel(mock_admin_update, mock_context):
    """Test /settings renders current values with inline controls."""
    mock_admin_update.message.reply_text = AsyncMock()

    with (
        patch("bot.commands.get_logic_config", new_callable=AsyncMock) as mock_config,
        patch("bot.commands.get_paused") as mock_paused,
        patch("bot.commands.get_utils_disabled", new_callable=AsyncMock) as mock_utils,
        patch("bot.commands.get_max_ping_pong", new_callable=AsyncMock) as mock_max_ping_pong,
    ):
        mock_config.return_value = (10, 0.05, 0.07)
        mock_max_ping_pong.return_value = 2
        mock_paused.return_value = False
        mock_utils.return_value = False

        await commands.settings_command(mock_admin_update, mock_context)

    mock_admin_update.message.reply_text.assert_called_once()
    text, = mock_admin_update.message.reply_text.call_args.args
    kwargs = mock_admin_update.message.reply_text.call_args.kwargs
    assert "Settings for Chat 12345" in text
    assert "Reply Chance: 5%" in text
    assert "Max Ping Pong: 2 replies" in text
    assert kwargs["reply_markup"].inline_keyboard[0][0].text == "Pause bot"


@pytest.mark.asyncio
async def test_settings_callback_applies_reply_preset(mock_admin_update, mock_context):
    """Test settings callback applies a preset and refreshes the panel."""
    query = MagicMock()
    query.from_user.id = 12345
    query.message.chat_id = 12345
    query.data = "settings:reply=0.15"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    mock_admin_update.callback_query = query

    with (
        patch("bot.commands.set_reply_chance", new_callable=AsyncMock) as mock_set_reply,
        patch("bot.commands._build_settings_panel", new_callable=AsyncMock) as mock_panel,
    ):
        mock_panel.return_value = ("updated settings", None)

        await commands.settings_callback(mock_admin_update, mock_context)

    mock_set_reply.assert_called_once_with(12345, 0.15)
    query.edit_message_text.assert_called_once_with("updated settings", reply_markup=None)
    query.answer.assert_called_once_with("Settings updated.")


@pytest.mark.asyncio
async def test_settings_callback_adjusts_max_ping_pong(mock_admin_update, mock_context):
    """Test settings callback adjusts the max ping-pong value."""
    query = MagicMock()
    query.from_user.id = 12345
    query.message.chat_id = 12345
    query.data = "settings:adj_pingpong=1"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    mock_admin_update.callback_query = query

    with (
        patch("bot.commands.get_max_ping_pong", new_callable=AsyncMock) as mock_get_max,
        patch("bot.commands.set_max_ping_pong", new_callable=AsyncMock) as mock_set_max,
        patch("bot.commands._build_settings_panel", new_callable=AsyncMock) as mock_panel,
    ):
        mock_get_max.return_value = 2
        mock_panel.return_value = ("updated settings", None)

        await commands.settings_callback(mock_admin_update, mock_context)

    mock_set_max.assert_called_once_with(12345, 3)


@pytest.mark.asyncio
async def test_set_max_ping_pong_command(mock_admin_update, mock_context):
    """Test /set_max_ping_pong exact value command."""
    mock_context.args = ["4"]
    mock_admin_update.message.reply_text = AsyncMock()

    with patch("bot.commands.set_max_ping_pong", new_callable=AsyncMock) as mock_set_max:
        await commands.set_max_ping_pong_command(mock_admin_update, mock_context)

    mock_set_max.assert_called_once_with(12345, 4)
    mock_admin_update.message.reply_text.assert_called_once_with(
        "Maximum ping pong set to 4"
    )


@pytest.mark.asyncio
async def test_settings_callback_adjusts_values(mock_admin_update, mock_context):
    """Test settings callback adjusts values with step deltas."""
    query = MagicMock()
    query.from_user.id = 12345
    query.message.chat_id = 12345
    query.data = "settings:adj_reply=0.01"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    mock_admin_update.callback_query = query

    with (
        patch("bot.commands.get_logic_config", new_callable=AsyncMock) as mock_config,
        patch("bot.commands.set_reply_chance", new_callable=AsyncMock) as mock_set_reply,
        patch("bot.commands._build_settings_panel", new_callable=AsyncMock) as mock_panel,
    ):
        mock_config.return_value = (10, 0.05, 0.07)
        mock_panel.return_value = ("updated settings", None)

        await commands.settings_callback(mock_admin_update, mock_context)

    mock_set_reply.assert_called_once_with(12345, 0.06)


@pytest.mark.asyncio
async def test_help_command(mock_update, mock_context):
    """Test /help command."""
    mock_update.message.reply_text = AsyncMock()
    await commands.help_command(mock_update, mock_context)
    mock_update.message.reply_text.assert_called_once()
    args, kwargs = mock_update.message.reply_text.call_args
    assert "<b>Available commands</b>" in args[0]
    assert "- <code>/help</code> - Show this message." in args[0]
    assert "- <code>/start</code> - Resume the bot." in args[0]
    assert kwargs["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_ping_command(mock_update, mock_context):
    """Test /ping command."""
    with patch("bot.commands.add_message_to_history") as mock_add_hist:
        mock_update.message.reply_text = AsyncMock()
        mock_update.message.reply_text.return_value = (
            MagicMock()
        )

        await commands.ping_command(mock_update, mock_context)

        mock_update.message.reply_text.assert_called_once()
        mock_add_hist.assert_called_once()


@pytest.mark.asyncio
async def test_music_command(mock_update, mock_context):
    """Test /music command."""
    mock_context.args = ["https://youtube.com/watch?v=123"]

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

        mock_update.message.reply_audio = AsyncMock()
        mock_update.message.reply_text = AsyncMock()

        await commands.music_command(mock_update, mock_context)

        mock_update.message.reply_audio.assert_called_once()
        mock_download.assert_called_once()


@pytest.mark.asyncio
async def test_memory_command_search_all(mock_update, mock_context):
    """Test /memory . "opera" search across user and general memories."""
    mock_update.message.text = '/memory . "opera"'
    mock_update.message.reply_text = AsyncMock()
    
    with (
        patch("bot.commands.search_user_memories", new_callable=AsyncMock) as mock_user_search,
        patch("bot.commands.search_general_memories", new_callable=AsyncMock) as mock_gen_search,
    ):
        mock_user_search.return_value = [(123, "alice", "Alice likes opera")]
        mock_gen_search.return_value = ["Topic: Opera, Summary: Verdi rules"]
        
        await commands.memories_command(mock_update, mock_context)
        
        mock_update.message.reply_text.assert_called_once()
        reply = mock_update.message.reply_text.call_args[0][0]
        assert "Memory search for \"opera\":" in reply
        assert "User Memories:" in reply
        assert "- alice (ID: 123): Alice likes opera" in reply
        assert "General Memories:" in reply
        assert "- Topic: Opera, Summary: Verdi rules" in reply


@pytest.mark.asyncio
async def test_memory_command_inspect_target(mock_update, mock_context):
    """Test /memory @alice inspect target."""
    mock_update.message.text = '/memory @alice'
    mock_update.message.reply_text = AsyncMock()
    
    with patch("bot.commands.get_user_memory_by_target", new_callable=AsyncMock) as mock_target:
        mock_target.return_value = (123, "alice", "Alice likes opera")
        
        await commands.memories_command(mock_update, mock_context)
        
        mock_update.message.reply_text.assert_called_once_with(
            "Memories of alice (ID: 123):\nAlice likes opera"
        )


@pytest.mark.asyncio
async def test_memory_command_inspect_target_with_query(mock_update, mock_context):
    """Test /memory @alice "opera" targets user thought and searches general memories."""
    mock_update.message.text = '/memory @alice "opera"'
    mock_update.message.reply_text = AsyncMock()
    
    with (
        patch("bot.commands.get_user_memory_by_target", new_callable=AsyncMock) as mock_target,
        patch("bot.commands.search_general_memories", new_callable=AsyncMock) as mock_gen_search,
    ):
        mock_target.return_value = (123, "alice", "Alice likes opera")
        mock_gen_search.return_value = ["Topic: Opera, Summary: Verdi rules"]
        
        await commands.memories_command(mock_update, mock_context)
        
        mock_update.message.reply_text.assert_called_once()
        reply = mock_update.message.reply_text.call_args[0][0]
        assert "Memory search for @alice / \"opera\":" in reply
        assert "User Memories:" in reply
        assert "- alice (ID: 123): Alice likes opera" in reply
        assert "General Memories:" in reply


@pytest.mark.asyncio
async def test_memory_command_malformed_quotes(mock_update, mock_context):
    """Test /memory command with malformed quotes."""
    mock_update.message.text = '/memory . "opera'
    mock_update.message.reply_text = AsyncMock()
    
    await commands.memories_command(mock_update, mock_context)
    mock_update.message.reply_text.assert_called_once_with(
        "Usage: /memory [.|@username|user_id|username] [\"query\"]"
    )
