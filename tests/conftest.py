import pytest
import pytest_asyncio
import aiosqlite
import os
import tempfile
from unittest.mock import MagicMock, patch

# Mock environment variables if needed
os.environ["TELEGRAM_BOT_TOKEN"] = "test_token"
os.environ["OPENROUTER_API_KEY"] = "test_key"


@pytest_asyncio.fixture
async def temp_db_path():
    """Creates a temporary database file and patches bot.memory.DB_NAME."""
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        db_path = tmp.name

    # Patch the DB_NAME in bot.memory
    with patch("bot.memory.DB_NAME", db_path):
        # Initialize the database schema
        from bot.memory import init_db

        await init_db()

        yield db_path

    # Cleanup
    if os.path.exists(db_path):
        os.remove(db_path)


@pytest.fixture
def mock_update():
    update = MagicMock()
    update.effective_chat.id = 12345
    update.effective_user.id = 67890
    update.effective_user.username = "test_user"
    update.message.text = "Hello bot"
    return update


@pytest.fixture
def mock_context():
    context = MagicMock()
    context.bot.username = "@test_bot"
    return context
