import pytest
from unittest.mock import AsyncMock, patch
from bot import logic


@pytest.mark.asyncio
async def test_should_reply_direct_mention(mock_update):
    """Test that the bot replies to direct mentions."""
    mock_update.message.text = "@test_bot hello"

    # Mock memory functions
    with patch("bot.logic.get_logic_config", new_callable=AsyncMock) as mock_config:
        mock_config.return_value = (10, 0.0, 0.0)  # High cooldown, 0 chance

        reply = await logic.should_reply(mock_update.message, "@test_bot", 12345)
        assert reply is True
        assert logic.messages_since_last_reply[12345] == 0


@pytest.mark.asyncio
async def test_should_reply_reply_to_bot(mock_update):
    """Test that the bot replies when replying to its own message."""
    mock_update.message.text = "replying to you"
    mock_update.message.reply_to_message.from_user.username = "test_bot"

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
