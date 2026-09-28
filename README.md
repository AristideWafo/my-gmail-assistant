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

## Local setup

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

Also configure:

- `GEMINI_API_KEY`
- `JEV_API_URL`
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
