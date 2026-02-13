# Project Overview

This is a Telegram bot built with `python-telegram-bot`, designed to act as a persona-based assistant ("Maestro Ponasenkov"). It features memory persistence, media handling (video/audio downloading), and LLM integration via OpenRouter.

## 🛠 Development & Testing

### Dependency Management
This project uses `uv` for dependency management.
- Install dependencies: `uv sync`
- Add a dependency: `uv add <package>`
- Add a dev dependency: `uv add --dev <package>`

### Running Tests
A `pytest` suite is setup in the `tests/` directory.

To run all tests:
```bash
uv run pytest tests/ -v
```

> **Note:** Tests use a temporary file-based SQLite database and mock external API calls (Telegram, OpenRouter, yt-dlp).

### Linting/Formatting
(Add if applicable, e.g., `ruff check .`)

## 🏗 Architecture

### Key Components

- **`bot/logic.py`**: Core decision-making logic. Determines if the bot should reply or react to a message based on cooldowns, random chances, and mentions.
- **`bot/memory.py`**: Handles all database interactions using `aiosqlite`. Manages user thoughts, general memories, whitelists, and configuration.
- **`bot/llm.py`**: Integration with OpenRouter (LLM) for generating text responses and reactions.
- **`bot/media_utils.py`**: Utilities for downloading media (video/audio) using `yt-dlp` and processing images/video frames (using `cv2` and `bot/vision.py`).
- **`bot/handlers.py`**: Telegram message handlers. Orchestrates the flow: Receive Message -> Check Logic -> Process Media -> call LLM -> Send Reply.
- **`bot/commands.py`**: Handlers for bot commands (e.g., `/start`, `/help`, `/music`, `/settings`).
- **`bot/jobs.py`**: Scheduled tasks (daily messages, auto-updates).

### Database
The bot uses a SQLite database (`bot_memory.db`) storing:
- `users`: User thoughts/personas.
- `general_memory`: Shared facts/context.
- `whitelist`: Allowed users/groups.
- `chat_config`: Per-chat settings (reply chance, etc.).

## 🔐 Environment Variables

Create a `.env` file with:

```ini
TELEGRAM_BOT_TOKEN=your_token_here
OPENROUTER_API_KEY=your_key_here
OPENROUTER_MODEL=google/gemini-flash-2.5 (or similar)
ADMIN_ID=123456789
```

## 🚀 Deployment

The bot can be deployed using the provided `Dockerfile` or `systemd` service (`freak.service`).
systemd service is preferable, it's in `/etc/systemd/system/freak.service` and can be started with `systemctl start freak`.
Start the bot with: `uv run python main.py`
