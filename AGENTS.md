# Project Overview

This is a Telegram bot built with `python-telegram-bot`, designed to act as a persona-based group-chat assistant. It features memory persistence, media handling (video/audio downloading), LLM integration via an OpenAI-compatible gateway (defaults to OpenRouter), and a sandboxed **ponder** research agent for deeper lookups before replying.

## 🛠 Development & Testing

### Setup

1. Copy environment template: `cp .env.example .env` and set `TELEGRAM_BOT_TOKEN`, `LLM_API_KEY`, and `ADMIN_ID`.
2. Install dependencies: `uv sync`
3. Run tests: `just test`
4. Run the bot: `just run`

### Dependency Management

This project uses `uv` for dependency management.

* Install dependencies: `uv sync`
* Add a dependency: `uv add <package>`
* Add a dev dependency: `uv add --dev <package>`

### Command Runner (`just`)

* List all available commands: `just`
* Sync python dependencies: `just sync`
* Run the bot: `just run`
* Run the test suite: `just test`
* Clean up cache and virtual environment: `just clean`

### Running Tests

A `pytest` suite is setup in the `tests/` directory.

```bash
just test
```

> **Note:** Tests use a temporary file-based SQLite database and mock external API calls (Telegram, LLM gateway, yt-dlp).

### Optional: Nix development shell

On NixOS (or with Nix installed), `shell.nix` provides `ffmpeg`, system libs for OpenCV, `uv`, and `just`. Use it only if your host environment is missing those pieces—not required for normal `uv` + `just` workflow.

```bash
nix-shell   # then: uv sync && just test
```

## 🏗 Architecture

### Key Components

- **`bot/logic.py`**: Core decision-making logic. Determines if the bot should reply or react to a message based on cooldowns, random chances, and mentions.
- **`bot/memory.py`**: Handles all database interactions using `aiosqlite`. Manages user thoughts, general memories, whitelists, and configuration.
- **`bot/llm.py`**: Integration with the LLM gateway for generating text responses and reactions; supports `ponder` tool_calls.
- **`bot/agent.py`**: Sandboxed ReAct ponder agent (`web_search`, `fetch_web_page`, `recall_memories`) used when the main LLM requests research.
- **`bot/media_utils.py`**: Utilities for downloading media (video/audio) using `yt-dlp` and processing images/video frames (using `cv2` and `bot/vision.py`).
- **`bot/handlers.py`**: Telegram message handlers. Orchestrates the flow: Receive Message → Check Logic → Process Media → call LLM → (optional ponder) → Send Reply.
- **`bot/commands.py`**: Handlers for bot commands (e.g., `/start`, `/help`, `/music`, `/settings`).
- **`bot/jobs.py`**: Scheduled tasks (daily messages, auto-updates).

### Database

The bot uses a SQLite database (`bot_memory.db`) storing:

- `users`: User thoughts/personas.
- `general_memory`: Shared facts/context.
- `whitelist`: Allowed users/groups.
- `chat_config`: Per-chat settings (reply chance, etc.).

## 🔐 Environment Variables

See `.env.example` for the full list. Minimum required:

```ini
TELEGRAM_BOT_TOKEN=your_token_here
LLM_API_KEY=your_key_here
ADMIN_ID=123456789
```

Common optional overrides:

| Variable | Purpose | Default |
|----------|---------|---------|
| `LLM_BASE_URL` | OpenAI-compatible API base URL | `https://openrouter.ai/api/v1` |
| `LLM_MODEL` | Main persona / chat LLM | `google/gemini-flash-2.5` |
| `LLM_PONDER_MODEL` | Ponder research agent | `deepseek/deepseek-v4-flash` |
| `LLM_VISION_MODEL` | Image / frame analysis | `google/gemini-flash-2.5` |

## 🚀 Deployment

The bot can be deployed using the provided `Dockerfile` or `systemd` service (`freak.service`).
Copy `freak.service` to `/etc/systemd/system/`, adjust paths, then `systemctl enable --now freak`.
Start the bot with: `uv run python main.py`
