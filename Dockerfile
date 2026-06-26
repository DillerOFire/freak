FROM python:3.11-slim

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# Install system dependencies (git for updates, ffmpeg for media processing)
RUN apt-get update && apt-get install -y --no-install-recommends git ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root user for the running process
RUN useradd --create-home --uid 1000 --shell /bin/bash freak

WORKDIR /app

# Copy dependency files
COPY --chown=freak:freak pyproject.toml uv.lock ./

# Install dependencies (chown venv so in-container uv pip upgrades work as freak)
RUN uv sync --frozen --no-install-project && chown -R freak:freak /app/.venv

# Persistent data lives on a volume so memory and cookies survive recreation.
RUN mkdir -p /data/cookies /data/python-packages && chown -R freak:freak /data
ENV BOT_DB_PATH=/data/bot_memory.db \
    COOKIES_DIR=/data/cookies \
    YTDLP_PACKAGE_DIR=/data/python-packages

COPY --chown=freak:freak . .

USER freak

# Telemetry dashboard is the in-container health signal.
# /telemetry returns 200 when the dashboard (and thus the bot's async runtime
# + DB) is live. If TELEMETRY_DASHBOARD_TOKEN is set, configure a reverse
# proxy health probe or override this check.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -sf "http://127.0.0.1:${TELEMETRY_DASHBOARD_PORT:-8765}/telemetry" || exit 1

# Run the application
CMD ["uv", "run", "--no-sync", "main.py"]
