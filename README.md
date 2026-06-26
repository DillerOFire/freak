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
- **Auto-update** — on bare-metal, `/update_bot` pulls git updates, runs `uv sync`, verifies imports, then restarts; `/update_ytdlp` refreshes the downloader. Under Docker the image is replaced wholesale by Watchtower, so these jobs are disabled there (see [Deployment](#-deployment)).

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

Docker-only overrides (set in `docker-compose.yml` or `.env`):

| Variable | Purpose | Default |
|----------|---------|---------|
| `RUN_MODE` | Set to `docker` to disable in-process self-update jobs | _(unset — bare-metal mode)_ |
| `BOT_DB_PATH` | SQLite database location | `bot_memory.db` next to the code; `/data/bot_memory.db` in the image |
| `COOKIES_DIR` | Where `cookies.txt` files live | `cookies/` next to the code; `/data/cookies` in the image |
| `TELEMETRY_DASHBOARD_HOST` | Dashboard bind address | `127.0.0.1`; set `0.0.0.0` in Docker so the port-forwarded healthcheck reaches it |

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

### Option A — Docker + GHCR + Watchtower (recommended)

Every push to `master` (and every tag, plus a daily 04:00 UTC rebuild for yt-dlp freshness) builds a multi-arch image and publishes it to GHCR:

```
ghcr.io/dillerofire/freak:master      # branch tag, what Watchtower tracks
ghcr.io/dillerofire/freak:sha-<sha>   # per-commit pin
ghcr.io/dillerofire/freak:v1.2.3      # semver tags
```

The image is public, so no registry login is needed on the host.

#### 1. Create a deploy directory on the server

```bash
mkdir -p ~/deploy/freak/data/cookies
cd ~/deploy/freak
cp /path/to/repo/.env .env          # your secrets live here, not in the image
# migrate an existing DB (if upgrading from bare-metal):
cp /old/freak/bot_memory.db data/bot_memory.db
cp /old/freak/cookies/*.txt data/cookies/ 2>/dev/null || true
```

#### 2. Write a `docker-compose.yml`

```yaml
services:
  bot:
    image: ghcr.io/dillerofire/freak:master
    container_name: freak
    restart: unless-stopped
    env_file: .env
    environment:
      RUN_MODE: docker
      TELEMETRY_DASHBOARD_HOST: "0.0.0.0"
    volumes:
      - ./data:/data
    ports:
      - "127.0.0.1:${TELEMETRY_DASHBOARD_PORT:-8765}:${TELEMETRY_DASHBOARD_PORT:-8765}"
```

The `./data` volume is where the SQLite DB and cookies persist across container recreations. `RUN_MODE=docker` turns off the in-process `git pull` / yt-dlp self-update jobs — the image owns that lifecycle now.

#### 3. Run it

```bash
docker compose up -d
docker compose logs -f
```

#### 4. Manage it with systemd

So the bot starts on boot and survives restarts:

```ini
[Unit]
Description=Freak Bot (Docker Compose)
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
User=you
WorkingDirectory=/home/you/deploy/freak
ExecStart=/usr/bin/docker compose -f /home/you/deploy/freak/docker-compose.yml up
ExecStop=/usr/bin/docker compose -f /home/you/deploy/freak/docker-compose.yml stop
TimeoutStartSec=0
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo cp freak.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now freak
```

Logs: `docker compose -f ~/deploy/freak/docker-compose.yml logs -f` or `journalctl -u freak -f`.

#### 5. Auto-update with Watchtower

Watchtower polls GHCR every 5 minutes and recreates the container when a new image lands. Put this in its own deploy dir:

```yaml
# ~/deploy/watchtower/docker-compose.yml
services:
  watchtower:
    image: containrrr/watchtower
    container_name: watchtower
    restart: unless-stopped
    environment:
      DOCKER_API_VERSION: "1.41"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    command: --interval 300 --cleanup freak personfreak
```

```bash
cd ~/deploy/watchtower
docker compose up -d
```

List the container names you want Watchtower to watch at the end of the `command` line. `--cleanup` removes the old image after a successful update.

> **Note:** Watchtower stops and recreates the container itself. If it hits a transient compose-networking error and leaves the container in a bad state, `systemctl restart freak` will recover it (the foreground `up` in the unit recreates as needed).

#### Running multiple instances

Each instance is its own deploy dir with its own `.env`, `data/`, and `docker-compose.yml`. Use distinct `container_name`s and `TELEMETRY_DASHBOARD_PORT`s. Watchtower takes a list of container names to watch.

### Option B — Bare metal with systemd

Run directly from a git checkout with `uv`. The in-bot self-update commands (`/update_bot`, `/update_ytdlp`) work in this mode — they pull, sync, verify, and exit so systemd restarts into the new code.

1. Put the project where you want it (e.g. `/home/you/freak`).
2. `uv sync` to install dependencies.
3. Copy and adjust `freak.service`:

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

4. Install and start:

```bash
sudo cp freak.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now freak
```

Logs: `journalctl -u freak -f`. Updates land via `/update_bot` in Telegram (it pulls and exits; systemd restarts it).

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

Database file `bot_memory.db` is created next to the code on first run. In Docker it lives on the `./data` volume (`/data/bot_memory.db` inside the container) so memory survives container recreation.

---

## 🔧 Optional: cookies for media

Some services (YouTube, Instagram, etc.) need auth cookies for downloads. Drop a `cookies.txt` per service into the `cookies/` dir, or upload it via `/update_cookies <service>` (admin only, as a file attachment). Supported services include `youtube`, `instagram`, `x`, `tiktok`, `facebook`, `reddit`, `spotify`, `soundcloud`, `bandcamp`, `vk`, `rutube`, and others.

Under Docker, cookies live in `data/cookies/` (mounted at `/data/cookies` inside the container) and persist across recreations.

---

## 📄 License

See repository for details. Built by [@DillerOFire](https://github.com/DillerOFire).
