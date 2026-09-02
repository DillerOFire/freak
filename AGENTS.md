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
- **`bot/llm.py`**: The **RP bot** is the main persona/chat LLM. It generates replies and owns conversational memory, scheduling, moods, and mutations of its own Telegram output. It has a single `ponder` tool for research, old-output lookup, and admin requests. Persona, behavior, and admin tools belong to the agent.
- **`bot/agent.py`**: The **ponder agent** is a sandboxed ReAct agent (`run_ponder_agent`) invoked when the RP bot calls `ponder`. It owns web tools (`web_search`, `fetch_web_page`), full memory tools (`recall_memories`, `update_user_thought`, `add/update/delete_general_memory`, `search/clear/update_media_summaries`), read-only old-output lookup (`search_own_outputs`), and admin/config tools (`get_persona_prompt`, `update_persona_prompt`, `reset_persona_prompt`, `get_behavior_settings`, `update_behavior_settings`). It receives `requesting_user_id` and `settings_chat_id` from the handler for admin-gated operations.
- **`bot/persona_output.py`**: Validates and executes RP-owned edits, deletions, and deliberate reaction batches. It verifies ownership and chat scope against the `persona_outputs` index before calling Telegram.
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
| **Memory tools** | ✅ Inline — `update_user_thought`, `add_general_memory`, `update/delete_general_memory`, `clear/update_media_summary`, `search_media_summaries` | ✅ Same set plus `recall_memories` — can store research findings and fulfill forget/update requests during ponder |
| **Schedule / event-state tools** | ✅ Inline — `schedule_action`, `cancel_scheduled_action`, `set_event_state`, `clear_event_state` (human-like deferred replies, moods, ignore) | ❌ No — scheduling is persona behavior on the RP bot |
| **Own-output tools** | ✅ `edit_own_message`, `delete_own_message`, `set_own_reactions` | 🔎 Read-only `search_own_outputs`; it returns candidates and never mutates Telegram |
| **Web tools** | ❌ No | ✅ `web_search`, `fetch_web_page` |
| **Persona/behavior/admin tools** | ❌ No — sees `is_admin` in context to refuse non-admins inline and route admin requests via `ponder` | ✅ `get/update/reset_persona_prompt`, `get/update_behavior_settings` (admin-gated via `requesting_user_id`) |
| **`ponder` tool** | ✅ Single deferred call per response | ❌ Cannot call itself |

**Rule of thumb:** The RP bot is a roleplay character with memory, timing, moods, and control over its own visible output. It does NOT modify its own config — it asks the ponder agent to do that. If a new tool mutates bot configuration, persona, or global behavior knobs, it goes in `bot/agent.py`'s `PONDER_TOOLS`. Conversational memory tools live on both. Telegram output mutations stay on the RP bot; ponder may only locate old output IDs. Deferred actions and temporary moods (`bot/schedule.py` + DB tables `scheduled_actions` / `event_states`) are RP-bot tools only; a job poller in `bot/jobs.py` executes due actions every 30s.

### Scheduled actions & event states

- **`scheduled_actions`**: LLM can defer a `reply`, proactive `message`, delayed `research` (ponder then speak), or free-form `task`. Each row stores `reason`, `instruction`, and `context` so future-you knows why it waited. Relative times get slight human jitter; vague phrases (`later`, `tonight`) are supported. When a plan fires, recent chat history is included and the model is told to speak like it remembered — not like a cron job.
- **`event_states`**: Time-bounded moods (`angry`, `mood`, …) injected into prompt context. `ignore` is a **soft** cold shoulder: spontaneous replies are blocked, but direct @mentions / replies still reach the LLM so it can stay silent, snub, or break silence (admin always bypasses).
- Pending actions and active states are injected into every RP prompt as `<pending_scheduled_actions>` / `<active_event_states>` with human-relative times (`in about 2 hours`).

### Database

The bot uses a SQLite database (`bot_memory.db`) storing:

- `users`: User thoughts/personas.
- `general_memory`: Shared facts/context.
- `whitelist`: Allowed users/groups.
- `chat_config`: Per-chat settings (reply chance, etc.).
- `persona_outputs`: Minimal index of messages sent by the RP bot for ownership checks and ponder lookup.
- `persona_reactions`: Latest deliberate or random reaction made by the RP bot per message.

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
| `LLM_REASONING_EFFORT` | Chat/reaction reasoning (`none`/`minimal`/`low`/`medium`/`high`/`xhigh`; empty = model default) | *(empty)* |
| `LLM_PONDER_MODEL` | Ponder research agent | `deepseek/deepseek-v4-flash` |
| `LLM_PONDER_REASONING_EFFORT` | Ponder reasoning effort (same values as above) | *(empty)* |
| `LLM_VISION_MODEL` | Image / frame analysis | `google/gemini-flash-2.5` |
| `LLM_VISION_REASONING_EFFORT` | Vision reasoning effort (same values as above) | *(empty)* |

## 🚀 Deployment

The bot can be deployed using the provided `Dockerfile` or `systemd` service (`freak.service`).
Copy `freak.service` to `/etc/systemd/system/`, adjust paths, then `systemctl enable --now freak`.
Start the bot with: `uv run python main.py`
