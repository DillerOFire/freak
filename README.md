# Freak 🎭

A persona-driven Telegram bot that hangs out in your chats, replies when it feels like it, reacts to messages with emoji, remembers things about people, and can download music or video on demand. Powered by any OpenAI-compatible LLM gateway (defaults to [OpenRouter](https://openrouter.ai)), with a sandboxed research agent that can browse the web before answering.

Think of it as a moody, opinionated chat member with memory — not a question-answering assistant.

---

## ✨ Features

- **Persona-based replies** — define a system prompt and the bot plays the character. Replies are triggered by mentions, replies, cooldowns, or a tunable random chance.
- **Emoji reactions** — independently tunable chance to drop a reaction on a message.
- **Memory** — per-user "thoughts" and shared general memories, searchable with `/memory`. The bot recalls relevant context when replying.
- **Media handling** — downloads video/audio via `yt-dlp`, analyzes images and video frames with a vision model, and can reuse saved GIFs/photos/stickers as replies.
- **Ponder research agent** — when the main LLM wants more context, it spawns a sandboxed ReAct agent with `web_search`, `fetch_web_page`, memory read/write tools, and admin config tools. SSRF-guarded, no private network access.
- **Human-like schedules & moods** — the RP bot can schedule delayed replies, proactive messages, later research, or free-form tasks (`schedule_action`), and set temporary event states (`set_event_state`: angry until tomorrow, ignore someone for an hour, etc.). Reasons and context are stored so future replies stay continuous. A 30s job poller fires due actions.
- **Per-chat settings** — reply chance, reaction chance, cooldown, bot-to-bot ping-pong cap. Set globally or per chat via a button panel.
- **Daily schedules** — send a message or run an LLM prompt every day at a given time.
- **Whitelist** — only respond in chats/users you allow.
- **Telegram-native telemetry** — an admin-only Web App for usage stats, LLM context, and memory behavior.
- **Auto-update** — on bare-metal, `/update_bot` pulls git updates, runs `uv sync`, verifies imports, then restarts; `/update_ytdlp` refreshes the downloader. Under Docker an external orchestrator replaces the image, so these jobs are disabled there (see [Deployment](#-deployment)).

---

## 🧱 Tech stack

| Piece | Tool |
|-------|------|
| Telegram API | `python-telegram-bot` |
| LLM gateway | OpenAI-compatible API (default: OpenRouter) |
| Database | SQLite via `aiosqlite` |
| Media download | `yt-dlp` |
| Vision | OpenCV (headless) + vision LLM |
| Scheduler | APScheduler |
| Deps / runner | `uv` + `just` |

Python **3.11+** is required.

---

## 🚀 Quick start

### 1. Get the tokens you'll need

- **Telegram bot token** — talk to [@BotFather](https://t.me/BotFather), create a bot, copy the token.
- **LLM API key** — sign up at [openrouter.ai](https://openrouter.ai) (or any compatible provider) and create a key.
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
LLM_API_KEY=sk-or-...
ADMIN_ID=123456789
```

The model defaults are sensible; override them only if you want different ones:

| Variable | Purpose | Default |
|----------|---------|---------|
| `LLM_BASE_URL` | OpenAI-compatible API base URL | `https://openrouter.ai/api/v1` |
| `LLM_MODEL` | Main persona / chat LLM | `google/gemini-flash-2.5` |
| `LLM_REASONING_EFFORT` | Chat/reaction reasoning (`none`/`minimal`/`low`/`medium`/`high`/`xhigh`; empty = model default) | *(empty)* |
| `LLM_PONDER_MODEL` | Research agent | `deepseek/deepseek-v4-flash` |
| `LLM_PONDER_REASONING_EFFORT` | Ponder reasoning effort (same values as above) | *(empty)* |
| `LLM_PONDER_MAX_STEPS` | Maximum ponder research/tool iterations | `10` |
| `LLM_VISION_MODEL` | Image / frame analysis | `google/gemini-flash-2.5` |
| `LLM_VISION_REASONING_EFFORT` | Vision reasoning effort (same values as above) | *(empty)* |

Media downloads are always capped at Telegram's 50 MiB file limit. The
downloader also has bounded concurrency and deadlines:

| Variable | Purpose | Default |
|----------|---------|---------|
| `YTDLP_MAX_CONCURRENT_DOWNLOADS` | Maximum downloads/ffmpeg jobs across all chats | `2` |
| `YTDLP_QUEUE_TIMEOUT_SEC` | Maximum wait for a global or per-chat slot | `30` |
| `YTDLP_DOWNLOAD_TIMEOUT_SEC` | Cooperative total download deadline | `180` |
| `YTDLP_SOCKET_TIMEOUT_SEC` | Network socket timeout inside yt-dlp | `20` |

Bare-metal nightly updates are installed into a staging directory, imported in
a fresh Python process, and atomically activated. The previous verified build
is retained as a startup fallback. Docker keeps yt-dlp image-owned instead.

### Telegram Web Apps

Set `WEB_SETTINGS_URL` to the public **HTTPS** URL of the embedded Web App
listener. From the admin's private chat, `/web_settings` edits the same
persisted environment file, persona prompt, and global behavior settings as
the bot commands; `/web_telemetry` opens usage, context, reply, and memory
telemetry. Both surfaces require fresh Telegram-signed Web App credentials.

```ini
WEB_SETTINGS_URL=https://bot.example.com
WEB_SETTINGS_HOST=127.0.0.1
WEB_SETTINGS_PORT=8780
WEB_SETTINGS_INIT_DATA_MAX_AGE=3600
```

Put TLS in front of the listener (for example, with Caddy, nginx, or Traefik)
and proxy both `/` and `/api/` to `WEB_SETTINGS_HOST:WEB_SETTINGS_PORT`.
Telegram Web Apps require HTTPS; do not put the plain listener directly on the
public internet. The API accepts only fresh, Telegram-signed Web App data from
the configured `ADMIN_ID`. Saved API keys are never returned to the browser:
the page shows only their masked state and accepts a replacement value.

Docker-only overrides (set in `docker-compose.yml` or `.env`):

| Variable | Purpose | Default |
|----------|---------|---------|
| `RUN_MODE` | Set to `docker` to disable in-process self-update jobs | _(unset — bare-metal mode)_ |
| `BOT_DB_PATH` | SQLite database location | `bot_memory.db` next to the code; `/data/bot_memory.db` in the image |
| `COOKIES_DIR` | Where `cookies.txt` files live | `cookies/` next to the code; `/data/cookies` in the image |
| `WEB_SETTINGS_HOST` | Web App listener bind address | `127.0.0.1`; set `0.0.0.0` in Docker when a reverse proxy needs the mapped port |

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

Tests use a temporary SQLite DB and mock Telegram / LLM / yt-dlp calls.

---

## 📦 Deployment

### Option A — Docker + GHCR (recommended)

CI builds a multi-arch image on every push to `master`, on version tags, and on a daily schedule (keeps yt-dlp fresh). Publish to your own registry, for example:

```
ghcr.io/your-org/freak:master      # moving branch tag for image discovery
ghcr.io/your-org/freak:sha-<sha>   # per-commit tag for selecting a release
ghcr.io/your-org/freak:v1.2.3      # semver tags
```

The included `docker-compose.yml` builds locally by default. Once you publish,
replace `build: .` with the reviewed immutable `image:` digest.

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
    image: ghcr.io/your-org/freak@sha256:<reviewed-digest>
    container_name: freak
    restart: unless-stopped
    env_file: .env
    environment:
      RUN_MODE: docker
      WEB_SETTINGS_HOST: "0.0.0.0"
    volumes:
      - ./data:/data
    ports:
      - "127.0.0.1:${WEB_SETTINGS_PORT:-8780}:${WEB_SETTINGS_PORT:-8780}"
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

#### 5. Update the immutable image

After CI publishes a release, resolve its registry digest and review the change
to `image:`. Back up `./data`, retain the previous digest for rollback, and then
deploy through your infrastructure orchestrator. A safe update must wait for
the `/health` endpoint before removing the previous image.

Do not give an unattended registry poller the Docker socket. Image publication
and production deployment are separate operations.

#### Running multiple instances

Each instance is its own deploy dir with its own `.env`, `data/`, and `docker-compose.yml`. Use distinct `container_name`s and `WEB_SETTINGS_PORT`s.

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
- `/web_settings`, `/web_telemetry` — open the admin-only Telegram Web Apps in a DM (requires `WEB_SETTINGS_URL`).
- `/bot_env`, `/set_env <KEY> <value>` — inspect masked editable environment values or update one in an admin DM.
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
  logic.py         Reply/react decision logic (cooldowns, chances, ping-pong, ignore states)
  memory.py        SQLite access (users, general memory, schedules, event states, whitelist, config)
  llm.py           LLM calls + tool-call routing (memory, schedule/state, ponder)
  schedule.py      Time parsing + execution of LLM-scheduled actions
  agent.py         Sandboxed ponder ReAct agent (web_search, fetch, recall)
  media_utils.py   yt-dlp download + video frame extraction
  vision.py        Image / frame analysis via vision model
  commands.py      Bot command handlers
  jobs.py          Daily tasks + update checker + 30s poller for deferred LLM actions
  system.py        Self-update helpers (git pull, yt-dlp upgrade, restart)
  messages.py      Reaction emoji pool
  telemetry/        Telemetry storage, analysis, export, and Web App payloads
```

Database file `bot_memory.db` is created next to the code on first run. In Docker it lives on the `./data` volume (`/data/bot_memory.db` inside the container) so memory survives container recreation.

---

## 🔧 Optional: cookies for media

Some services (YouTube, Instagram, etc.) need auth cookies for downloads. Drop a `cookies.txt` per service into the `cookies/` dir, or upload it via `/update_cookies <service>` (admin only, as a file attachment). Supported services include `youtube`, `instagram`, `x`, `tiktok`, `facebook`, `reddit`, `spotify`, `soundcloud`, `bandcamp`, `vk`, `rutube`, and others.

Under Docker, cookies live in `data/cookies/` (mounted at `/data/cookies` inside the container) and persist across recreations.

---

## 📄 License

MIT — see [LICENSE](LICENSE).
