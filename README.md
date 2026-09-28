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
- `JEV_API_URL` (default `https://api.typesafe.ai/v1/systemone`)
- `JEV_API_KEY` (TypeSafe key from https://console.typesafe.ai/; without it the heuristic fallback is used)
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `DISCORD_WEBHOOK_URL`

## Docker deployment

Build and run the assistant, Prometheus, and Grafana:

```bash
docker compose up --build -d
```

Useful endpoints:

- Assistant health: `http://localhost:8000/healthz`
- Metrics: `http://localhost:8000/metrics`
- Prometheus: `http://localhost:9090`
- Grafana: `http://localhost:3000`

## CI/CD

- `.github/workflows/ci.yml`: lint (ruff) + tests (pytest) + Docker build sanity check on every PR and push to `prod`.
- `.github/workflows/release.yml`: on push to `prod`, runs [python-semantic-release](https://python-semantic-release.readthedocs.io/) against [Conventional Commits](https://www.conventionalcommits.org/) to bump `pyproject.toml`, update `CHANGELOG.md`, tag (`vX.Y.Z`) and cut a GitHub Release; on a new release it builds and pushes the Docker image to `ghcr.io/<repo>:<version>` and `:latest`.

Commit convention (drives the version bump):

- `fix: ...` → patch
- `feat: ...` → minor
- `feat!: ...` / `BREAKING CHANGE:` footer → major
