# my-gmail-assistant

Modular AI email triage assistant built with **FastAPI** and a **LangGraph** workflow. It runs on low-resource VPS hosts (Docker) and routes every email through a cheap classifier first, then escalates only high-priority/complex messages to Gemini.

## Architecture

```text
main.py
└── src/
    ├── gmail/          # OAuth2 Gmail client + unread/history ingestion + labels/drafts
    ├── triage/         # Fast triage decision engine (JEV API + fallback heuristics)
    ├── llm/            # Gemini summary + draft generation
    ├── gateways/       # Telegram/Discord webhook notifications
    └── observability/  # /metrics endpoint (Prometheus counters + latency + token usage)
```

## Features

- **Dual-stage routing**:
  1. `src/triage` classifies each email into `{ urgency, category, confidence }`
  2. only `urgency=high` (or low confidence) paths invoke `src/llm` Gemini generation
- **Retroactive ingestion** with `--sync-history` startup flag and Gmail 429 exponential backoff
- **Urgency alerts** to Telegram + Discord
- **Observability** at `GET /metrics`

## Quick install

```bash
./install.sh          # local venv setup
./install.sh --docker # build + start via docker compose
```

## Local setup (manual)

1. Create a venv and install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Configure environment:

```bash
cp .env.example .env
# edit .env and set your credentials
```

3. Run the service:

```bash
python main.py --sync-history
```

## Google Cloud / Gmail credentials

1. Create a Google Cloud project and enable **Gmail API**.
2. Configure OAuth consent screen and create OAuth client credentials.
3. Set in `.env`:
   - `GOOGLE_CLIENT_ID`
   - `GOOGLE_CLIENT_SECRET`
   - `GMAIL_REFRESH_TOKEN`

### Getting the refresh token (server without a browser)

The server only needs the three values in `.env`; it refreshes access tokens on its own. Generate them once on a machine that has a browser:

1. In Google Cloud, create an OAuth client of type **Desktop app** and download its JSON (keep it out of git).
2. On the OAuth consent screen, publish the app (**In production**); in *Testing* mode the refresh token expires after 7 days.
3. On your workstation:

   ```bash
   pip install google-auth-oauthlib
   python -m src.gmail.token_setup client_secret.json
   ```

4. Log in in the browser, then paste the three printed lines into the server's `.env`.

Also configure:

- `GEMINI_API_KEY`
- `GEMINI_MAX_RPM` (default `12`, keep below your Gemini tier's requests-per-minute quota; one request per urgent mail)
- `USER_DISPLAY_NAME` (name used to sign drafted replies; without it drafts have no signature)
- `JEV_API_URL` (default `https://api.typesafe.ai/v1/systemone`)
- `JEV_API_KEY` (TypeSafe key from https://console.typesafe.ai/; without it the heuristic fallback is used)
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `DISCORD_WEBHOOK_URL` (must be `https://discord.com/api/webhooks/<id>/<token>`)
- `FETCH_MAX_AGE_DAYS` (default `3`; only unread inbox mails newer than this, excluding promotions/social, are processed) and `FETCH_QUERY` (full Gmail query override)
- `LOW_CONFIDENCE_THRESHOLD` (default `0.50`; below it a mail is labeled and never archived, and only `high` urgency triggers an alert)

## Health and watchdog

`/healthz` returns 503 when the polling task has stopped or shows no activity for `max(5 x POLL_INTERVAL_SECONDS, 600)` seconds. With `WATCHDOG_ENABLED=true` (default) an in-process watchdog then exits the process so Docker's `restart: unless-stopped` brings it back; a Docker healthcheck alone only marks the container unhealthy.

## Startup connection checks

At startup the app probes every configured connection (Gmail, Gemini, JEV, Telegram, Discord) with read-only calls and logs one line per service (`OK`, `FAILED` or `SKIPPED` when not configured). No message is sent; secrets never appear in the logs. `STARTUP_CHECKS` controls the behavior:

- `warn` (default): log results and start anyway.
- `strict`: refuse to start if any configured connection fails.
- `off`: skip the checks.

## Docker deployment

Build and run the assistant, Prometheus, and Grafana:

```bash
docker compose up --build -d
```

Useful endpoints:

- Assistant health: `http://localhost:8000/healthz`
- Metrics: `http://localhost:8000/metrics`
- Prometheus: `http://localhost:9090`
- Grafana: `http://localhost:3000` — pre-provisioned with the Prometheus datasource and a "Gmail Assistant" dashboard (processed emails, triage latency, JEV fallback rate, LLM tokens and cost). Login `admin` / `GRAFANA_ADMIN_PASSWORD` (defaults to `admin` if unset — set it in `.env`).

## CI/CD

- `.github/workflows/ci.yml`: lint (ruff) + tests (pytest) + Docker build sanity check on every PR and push to `prod`.
- `.github/workflows/release.yml`: on push to `prod`, runs [python-semantic-release](https://python-semantic-release.readthedocs.io/) against [Conventional Commits](https://www.conventionalcommits.org/) to bump `pyproject.toml`, update `CHANGELOG.md`, tag (`vX.Y.Z`) and cut a GitHub Release; on a new release it builds and pushes the Docker image to `ghcr.io/<repo>:<version>` and `:latest`.

Commit convention (drives the version bump):

- `fix: ...` → patch
- `feat: ...` → minor
- `feat!: ...` / `BREAKING CHANGE:` footer → major
