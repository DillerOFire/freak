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
- **`bot/llm.py`**: The **RP bot** — the main persona/chat LLM. Generates text responses and reactions. Has inline tools for *memory only* (`update_user_thought`, `add_general_memory`, `search_media_summaries`, etc.) and a single `ponder` tool to defer everything else to the agent. Does NOT have persona/behavior/admin tools — those belong to the agent.
- **`bot/agent.py`**: The **ponder agent** — a sandboxed ReAct agent (`run_ponder_agent`) invoked when the RP bot calls `ponder`. Owns all *non-memory* tools: `web_search`, `fetch_web_page`, `recall_memories`, and admin/config tools (`get_persona_prompt`, `update_persona_prompt`, `reset_persona_prompt`, `get_behavior_settings`, `update_behavior_settings`). Receives `requesting_user_id` and `settings_chat_id` from the handler for admin-gated operations.
- **`bot/media_utils.py`**: Utilities for downloading media (video/audio) using `yt-dlp` and processing images/video frames (using `cv2` and `bot/vision.py`).
- **`bot/handlers.py`**: Telegram message handlers. Orchestrates the flow: Receive Message → Check Logic → Process Media → call LLM → (optional ponder) → Send Reply.
- **`bot/commands.py`**: Handlers for bot commands (e.g., `/start`, `/help`, `/music`, `/settings`).
- **`bot/jobs.py`**: Scheduled tasks (daily messages, auto-updates).

### Tool Ownership: RP Bot vs Ponder Agent

There are two LLM-backed components with distinct roles. **Adding a tool to the wrong one is a bug.**

| | **RP bot** (`bot/llm.py`) | **Ponder agent** (`bot/agent.py`) |
|---|---|---|
| **Role** | Persona/chat participant in the group | Sandboxed research & admin assistant |
| **Model** | `LLM_MODEL` (conversational) | `LLM_PONDER_MODEL` (cheaper/faster) |
| **Invoked** | On every eligible message | Only when the RP bot calls `ponder` |
| **Memory tools** | ✅ Inline — `update_user_thought`, `add_general_memory`, `update/delete_general_memory`, `clear/update_media_summary`, `search_media_summaries` | ❌ No (uses `recall_memories` read-only) |
| **Web tools** | ❌ No | ✅ `web_search`, `fetch_web_page` |
| **Persona/behavior/admin tools** | ❌ No — sees `is_admin` in context to refuse non-admins inline and route admin requests via `ponder` | ✅ `get/update/reset_persona_prompt`, `get/update_behavior_settings` (admin-gated via `requesting_user_id`) |
| **`ponder` tool** | ✅ Single deferred call per response | ❌ Cannot call itself |

**Rule of thumb:** The RP bot is a roleplay character with a memory. It does NOT modify its own config — it asks the ponder agent to do that. If a new tool mutates bot configuration, persona, or behavior, it goes in `bot/agent.py`'s `PONDER_TOOLS`. If it mutates conversational memory, it goes in `bot/llm.py`'s `_apply_tool_call`.

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
