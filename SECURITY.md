# Security

## Reporting a vulnerability

Please open a private security advisory on GitHub or contact the repository maintainers directly. Do not file public issues for undisclosed vulnerabilities.

## Secrets and local data

Never commit these to the repository:

- `.env` and any file containing API keys or bot tokens
- `cookies/` — browser session cookies for media downloaders
- `bot_memory.db` or other SQLite databases from production

All of the above are listed in `.gitignore`. Use `.env.example` as a template only.

If credentials or cookies were ever committed, rotate them immediately and rewrite git history before making the repository public.

## Telemetry dashboard

When `TELEMETRY_DASHBOARD_ENABLED` is true, bind the dashboard to `127.0.0.1` unless you protect it with a reverse proxy. Set `TELEMETRY_DASHBOARD_TOKEN` when exposing the port beyond localhost.

## Ponder agent

The research agent can fetch public web pages and search the web. It is SSRF-guarded against private networks, but treat outbound HTTP as untrusted input when hardening deployments.
