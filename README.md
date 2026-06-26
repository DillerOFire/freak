# Freak 🎭

A persona-driven Telegram bot that hangs out in your chats, replies when it feels like it, reacts to messages with emoji, remembers things about people, and can download music or video on demand. Powered by LLMs through [OpenRouter](https://openrouter.ai), with a sandboxed research agent that can browse the web before answering.

Think of it as a moody, opinionated chat member with memory — not a question-answering assistant.

---

## ✨ Features

- **Persona-based replies** — define a system prompt and the bot plays the character. Replies are triggered by mentions, replies, cooldowns, or a tunable random chance.
- **Emoji reactions** — independently tunable chance to drop a reaction on a message.
- **Memory** — per-user "thoughts" and shared general memories, searchable with `/memory`. The bot recalls relevant context when replying.
- **Media handling** — downloads video/audio via `yt-dlp`, analyzes images and video frames with a vision model, and can reuse saved GIFs/photos/stickers as replies.
- **Ponder research agent** — when the main LLM wants more context, it spawns a sandboxed ReAct agent with `web_search`, `fetch_web_page`, and `recall_memories` tools. SSRF-guarded, no private network access.
- **Per-chat settings** — reply chance, reaction chance, cooldown, bot-to-bot ping-pong cap. Set globally or per chat via a button panel.
- **Daily schedules** — send a message or run an LLM prompt every day at a given time.
- **Whitelist** — only respond in chats/users you allow.
- **Telemetry dashboard** — optional local web dashboard of usage stats.
- **Self-update** — `/update_bot` pulls git updates, runs `uv sync`, verifies imports, then restarts; `/update_ytdlp` refreshes the downloader.

---

## 🧱 Tech stack

| Piece | Tool |
|-------|------|
| Telegram API | `python-telegram-bot` |
| LLM gateway | OpenRouter (OpenAI-compatible client) |
| Database | SQLite via `aiosqlite` |
| Media download | `yt-dlp` |
| Vision | OpenCV (headless) + OpenRouter vision model |
| Scheduler | APScheduler |
| Deps / runner | `uv` + `just` |

Python **3.11+** is required.

---

## 🚀 Quick start

### 1. Get the tokens you'll need

- **Telegram bot token** — talk to [@BotFather](https://t.me/BotFather), create a bot, copy the token.
- **OpenRouter API key** — sign up at [openrouter.ai](https://openrouter.ai), create a key.
- **Your Telegram user ID** — forward a message to [@userinfobot](https://t.me/userinfobot) or use `/ping` after first run. This becomes `ADMIN_ID`.

### 2. Install dependencies

You need [`uv`](https://docs.astral.sh/uv/) and [`just`](https://github.com/casey/just) installed. Then:

```bash
uv sync
```

> **NixOS / Nix users:** `nix-shell` drops you into a shell with `uv`, `just`, `ffmpeg`, and the libs OpenCV needs. Then run `uv sync` inside it.

### 3. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and fill in at minimum:

```ini
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
OPENROUTER_API_KEY=sk-or-...
ADMIN_ID=123456789
```

The model defaults are sensible; override them only if you want different ones:

| Variable | Purpose | Default |
|----------|---------|---------|
| `OPENROUTER_MODEL` | Main persona / chat LLM | `google/gemini-flash-2.5` |
| `OPENROUTER_PONDER_MODEL` | Research agent | `deepseek/deepseek-v4-flash` |
| `OPENROUTER_VISION_MODEL` | Image / frame analysis | `google/gemini-flash-2.5` |

### 4. Run it

```bash
just run
```

Or directly:

```bash
uv run python main.py
```

Send `/ping` to the bot in Telegram to confirm it's alive, then `/help` for the full command list.

### 5. Whitelist your chat

By default the bot only responds to the admin and whitelisted chats. In the chat you want it active, run:

```
/whitelist_add
```

(no args, in a group — adds the current group) or `/whitelist_add <id> user|group`.

---

## 🧪 Testing

```bash
just test
```

Tests use a temporary SQLite DB and mock Telegram / OpenRouter / yt-dlp calls.

---

## 📦 Deployment

### Option A — systemd (recommended)

1. Put the project where you want it (e.g. `/home/you/freak`).
2. Copy and adjust `freak.service`:

```ini
[Unit]
Description=Freak Bot Service
After=network.target

[Service]
Type=simple
User=you
WorkingDirectory=/home/you/freak
ExecStart=/home/you/freak/.venv/bin/python main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

3. Install and start:

```bash
sudo cp freak.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now freak
```

Logs: `journalctl -u freak -f`. Updates land cleanly via the in-bot `/update_bot` command (it pulls and exits; systemd restarts it).

### Option B — Docker

```bash
./deploy.sh
```

This builds the image and runs a container with the DB mounted for persistence, reading env from `.env`. Stops/restarts with Docker. Requires Docker installed.

---

## 🎛 Commands

Send `/help` in Telegram for the live list. Highlights:

**General**
- `/ping` — chat ID, user ID info.
- `/music <url>` — download audio from a supported service.
- `/memory [.|@user|user_id|username] ["query"]` — search or inspect memories.

**Daily schedules** (reply to a message with `/add_daily_msg`, or give a prompt to `/add_daily_task`)
- `/add_daily_msg <HH:MM>`, `/add_daily_task <HH:MM> <prompt>`, `/daily_list`, `/daily_cancel_msg`, `/daily_cancel_task`

**Admin config**
- `/settings` — button panel for all tunables.
- `/set_reply_chance`, `/set_reaction_chance`, `/set_cooldown`, `/set_max_ping_pong` — exact values.
- `/update_prompt <text>`, `/show_prompt` — edit the persona.
- `/stop` / `/start` — pause / resume the bot.
- `/stop_utils` / `/start_utils` — toggle media downloading per chat.
- `/update_cookies <service>` — attach a `cookies.txt` for YouTube/Instagram/etc.
- `/whitelist_add`, `/whitelist_remove`, `/whitelist_list`.
- `/update_ytdlp`, `/update_bot`.

---

## 🗂 Project layout

```
main.py            Entry point: handlers, polling, post_init
config.py          Loads .env and exports settings
bot/
  handlers.py      Message pipeline: logic → media → LLM → (ponder) → reply
  logic.py         Reply/react decision logic (cooldowns, chances, ping-pong)
  memory.py        SQLite access (users, general memory, whitelist, config)
  llm.py           OpenRouter calls + tool-call routing to ponder
  agent.py         Sandboxed ponder ReAct agent (web_search, fetch, recall)
  media_utils.py   yt-dlp download + video frame extraction
  vision.py        Image / frame analysis via vision model
  commands.py      Bot command handlers
  jobs.py          Scheduled daily tasks + update checker
  system.py        Self-update helpers (git pull, yt-dlp upgrade, restart)
  messages.py      Reaction emoji pool
  telemetry/        Optional usage dashboard
```

Database file `bot_memory.db` is created next to the code on first run.

---

## 🔧 Optional: cookies for media

Some services (YouTube, Instagram, etc.) need auth cookies for downloads. Drop a `cookies.txt` per service into the `cookies/` dir, or upload it via `/update_cookies <service>` (admin only, as a file attachment). Supported services include `youtube`, `instagram`, `x`, `tiktok`, `facebook`, `reddit`, `spotify`, `soundcloud`, `bandcamp`, `vk`, `rutube`, and others.

---

## 📄 License

See repository for details. Built by [@DillerOFire](https://github.com/DillerOFire).
