import pytest
from unittest.mock import AsyncMock, patch
from bot import logic



def reset_reply_state(chat_id=12345):
    logic.messages_since_last_reply.pop(chat_id, None)
    logic.bot_reply_locks.pop(chat_id, None)
    logic.bot_ping_pong_counts.pop(chat_id, None)

@pytest.mark.asyncio
async def test_should_reply_direct_mention(mock_update):
    """Test that the bot replies to direct mentions."""
    mock_update.message.text = "@test_bot hello"
    reset_reply_state()
    mock_update.message.from_user.is_bot = False

    # Mock memory functions
    with patch("bot.logic.get_logic_config", new_callable=AsyncMock) as mock_config:
        mock_config.return_value = (10, 0.0, 0.0)  # High cooldown, 0 chance

        reply = await logic.should_reply(mock_update.message, "@test_bot", 12345)
        assert reply is True
        assert logic.messages_since_last_reply[12345] == 0




@pytest.mark.asyncio
async def test_should_reply_bot_mention_creates_lock(mock_update):
    """Test that bot senders can trigger one direct mention before being locked."""
    reset_reply_state()
    mock_update.message.text = "@test_bot hello"
    mock_update.message.from_user.is_bot = True
    mock_update.message.from_user.id = 777

    with (
        patch("bot.logic.get_logic_config", new_callable=AsyncMock) as mock_config,
        patch("bot.logic.get_max_ping_pong", new_callable=AsyncMock) as mock_max_ping_pong,
    ):
        mock_config.return_value = (10, 0.0, 0.0)
        mock_max_ping_pong.return_value = 2

        reply = await logic.should_reply(mock_update.message, "@test_bot", 12345)

    assert reply is True
    assert logic.messages_since_last_reply[12345] == 0
    assert logic.bot_reply_locks[12345][777] == logic.BOT_REPLY_LOCK_TTL_MESSAGES
    assert logic.bot_ping_pong_counts[12345][777] == 1


@pytest.mark.asyncio
async def test_should_reply_bot_mention_respects_max_ping_pong(mock_update):
    """Test that bot-to-bot replies stop after the configured ping-pong cap."""
    reset_reply_state()
    mock_update.message.text = "@test_bot still there?"
    mock_update.message.from_user.is_bot = True
    mock_update.message.from_user.id = 777
    logic.messages_since_last_reply[12345] = 0
    logic.bot_ping_pong_counts[12345] = {777: 2}

    with (
        patch("bot.logic.get_logic_config", new_callable=AsyncMock) as mock_config,
        patch("bot.logic.get_max_ping_pong", new_callable=AsyncMock) as mock_max_ping_pong,
    ):
        mock_config.return_value = (10, 0.0, 0.0)
        mock_max_ping_pong.return_value = 2

        reply = await logic.should_reply(mock_update.message, "@test_bot", 12345)

    assert reply is False
    assert logic.messages_since_last_reply[12345] == 1
    assert logic.bot_ping_pong_counts[12345][777] == 2


@pytest.mark.asyncio
async def test_should_reply_human_message_resets_ping_pong(mock_update):
    """Test that human messages start a fresh bot-to-bot conversation window."""
    reset_reply_state()
    mock_update.message.text = "human interjection"
    mock_update.message.reply_to_message = None
    mock_update.message.from_user.is_bot = False
    logic.messages_since_last_reply[12345] = 0
    logic.bot_ping_pong_counts[12345] = {777: 2}

    with (
        patch("bot.logic.get_logic_config", new_callable=AsyncMock) as mock_config,
        patch("bot.logic.get_max_ping_pong", new_callable=AsyncMock) as mock_max_ping_pong,
    ):
        mock_config.return_value = (10, 0.0, 0.0)
        mock_max_ping_pong.return_value = 2

        reply = await logic.should_reply(mock_update.message, "@test_bot", 12345)

    assert reply is False
    assert 12345 not in logic.bot_ping_pong_counts


@pytest.mark.asyncio
async def test_should_reply_bot_mention_ignored_while_locked(mock_update):
    """Test that a bot sender cannot immediately ping-pong mentions."""
    reset_reply_state()
    mock_update.message.text = "@test_bot hello again"
    mock_update.message.from_user.is_bot = True
    mock_update.message.from_user.id = 777
    logic.messages_since_last_reply[12345] = 0
    logic.bot_reply_locks[12345] = {777: logic.BOT_REPLY_LOCK_TTL_MESSAGES}

    with (
        patch("bot.logic.get_logic_config", new_callable=AsyncMock) as mock_config,
        patch("bot.logic.get_max_ping_pong", new_callable=AsyncMock) as mock_max_ping_pong,
    ):
        mock_config.return_value = (10, 0.0, 0.0)
        mock_max_ping_pong.return_value = 2

        reply = await logic.should_reply(mock_update.message, "@test_bot", 12345)

    assert reply is False
    assert logic.messages_since_last_reply[12345] == 1
    assert logic.bot_reply_locks[12345][777] == logic.BOT_REPLY_LOCK_TTL_MESSAGES


@pytest.mark.asyncio
async def test_should_reply_human_mention_still_replies(mock_update):
    """Test that human direct mentions still reply normally."""
    reset_reply_state()
    mock_update.message.text = "@test_bot hello"
    mock_update.message.from_user.is_bot = False

    with patch("bot.logic.get_logic_config", new_callable=AsyncMock) as mock_config:
        mock_config.return_value = (10, 0.0, 0.0)

        reply = await logic.should_reply(mock_update.message, "@test_bot", 12345)

    assert reply is True
    assert logic.messages_since_last_reply[12345] == 0
    assert 12345 not in logic.bot_reply_locks


@pytest.mark.asyncio
async def test_should_reply_bot_sender_skips_random_chance(mock_update):
    """Test that bot senders cannot trigger random unsolicited replies."""
    reset_reply_state()
    mock_update.message.text = "random bot message"
    mock_update.message.reply_to_message = None
    mock_update.message.from_user.is_bot = True
    mock_update.message.from_user.id = 777
    logic.messages_since_last_reply[12345] = 100

    with (
        patch("bot.logic.get_logic_config", new_callable=AsyncMock) as mock_config,
        patch("bot.logic.get_max_ping_pong", new_callable=AsyncMock) as mock_max_ping_pong,
        patch("random.random") as mock_random,
    ):
        mock_config.return_value = (0, 0.5, 0.0)
        mock_max_ping_pong.return_value = 2
        mock_random.return_value = 0.1

        reply = await logic.should_reply(mock_update.message, "@test_bot", 12345)

    assert reply is False
    assert logic.messages_since_last_reply[12345] == 101
    mock_random.assert_not_called()


@pytest.mark.asyncio
async def test_should_reply_reply_to_bot(mock_update):
    """Test that the bot replies when replying to its own message."""
    mock_update.message.text = "replying to you"
    mock_update.message.reply_to_message.from_user.username = "test_bot"
    reset_reply_state()
    mock_update.message.from_user.is_bot = False

    with patch("bot.logic.get_logic_config", new_callable=AsyncMock) as mock_config:
        mock_config.return_value = (10, 0.0, 0.0)

        reply = await logic.should_reply(mock_update.message, "@test_bot", 12345)
        assert reply is True
        assert logic.messages_since_last_reply[12345] == 0


@pytest.mark.asyncio
async def test_should_reply_cooldown(mock_update):
    """Test cooldown logic."""
    mock_update.message.text = "just a message"
    mock_update.message.reply_to_message = None
    reset_reply_state()
    mock_update.message.from_user.is_bot = False

    with patch("bot.logic.get_logic_config", new_callable=AsyncMock) as mock_config:
        mock_config.return_value = (10, 0.0, 0.0)

        # Reset state
        logic.messages_since_last_reply[12345] = 0

        # Should NOT reply because count (0) < cooldown (10)
        reply = await logic.should_reply(mock_update.message, "@test_bot", 12345)
        assert reply is False
        assert logic.messages_since_last_reply[12345] == 1


@pytest.mark.asyncio
async def test_should_reply_random_chance(mock_update):
    """Test random reply chance."""
    mock_update.message.text = "random message"
    mock_update.message.reply_to_message = None
    reset_reply_state()
    mock_update.message.from_user.is_bot = False

    with (
        patch("bot.logic.get_logic_config", new_callable=AsyncMock) as mock_config,
        patch("random.random") as mock_random,
    ):
        mock_config.return_value = (0, 0.5, 0.0)  # 0 cooldown, 50% chance

        # Force random to hit
        mock_random.return_value = 0.1

        # Reset state
        logic.messages_since_last_reply[12345] = 100

        reply = await logic.should_reply(mock_update.message, "@test_bot", 12345)
        assert reply is True
        assert logic.messages_since_last_reply[12345] == 0


@pytest.mark.asyncio
async def test_should_react(mock_update):
    """Test reaction logic."""
    with (
        patch("bot.logic.get_logic_config", new_callable=AsyncMock) as mock_config,
        patch("random.random") as mock_random,
    ):
        mock_config.return_value = (0, 0.0, 0.5)  # 50% reaction chance

        mock_random.return_value = 0.1
        assert await logic.should_react(12345) is True

        mock_random.return_value = 0.9
        assert await logic.should_react(12345) is False


@pytest.mark.asyncio
async def test_should_reply_private_chat_without_mention(mock_update):
    """Test that private chat messages reply without mentioning the bot."""
    mock_update.message.text = "hey there"
    mock_update.message.reply_to_message = None
    mock_update.message.chat.type = "private"
    reset_reply_state()
    mock_update.message.from_user.is_bot = False

    with patch("bot.logic.get_logic_config", new_callable=AsyncMock) as mock_config:
        mock_config.return_value = (10, 0.0, 0.0)

        reply = await logic.should_reply(mock_update.message, "@test_bot", 12345)

    assert reply is True
    assert logic.messages_since_last_reply[12345] == 0


@pytest.mark.asyncio
async def test_get_logic_config_uses_global_defaults(temp_db_path):
    """Test that chats inherit unset values from global defaults."""
    from bot.logic import GLOBAL_SETTINGS_CHAT_ID, set_reply_chance, get_logic_config

    await set_reply_chance(GLOBAL_SETTINGS_CHAT_ID, 0.42)
    _, reply_chance, _ = await get_logic_config(99999)
    assert reply_chance == 0.42


@pytest.mark.asyncio
async def test_behavior_settings_read_and_update(temp_db_path):
    from bot.logic import (
        GLOBAL_SETTINGS_CHAT_ID,
        get_behavior_settings,
        update_behavior_settings,
    )

    admin_id = 4242
    settings = await get_behavior_settings(GLOBAL_SETTINGS_CHAT_ID)
    assert "reply_chance" in settings
    assert settings["scope"] == "global"

    ok, reason = await update_behavior_settings(
        GLOBAL_SETTINGS_CHAT_ID,
        requesting_user_id=admin_id,
        admin_id=admin_id,
        reply_chance=0.25,
        reaction_chance=0.15,
        max_ping_pong=3,
        media_reply_guidance="Use stickers in about half of replies when they fit.",
    )
    assert ok is True
    assert reason == "ok"

    updated = await get_behavior_settings(GLOBAL_SETTINGS_CHAT_ID)
    assert updated["reply_chance"] == 0.25
    assert updated["reaction_chance"] == 0.15
    assert updated["max_ping_pong"] == 3
    assert "stickers" in updated["media_reply_guidance"]

    denied, reason = await update_behavior_settings(
        GLOBAL_SETTINGS_CHAT_ID,
        requesting_user_id=1,
        admin_id=admin_id,
        reply_chance=0.1,
    )
    assert denied is False
    assert reason == "admin_only"
