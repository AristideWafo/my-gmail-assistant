# my-gmail-assistant

Modular AI email triage assistant built with **FastAPI** and a **LangGraph** workflow. It runs on low-resource VPS hosts (Docker) and routes every email through a cheap classifier first, then escalates only high-priority/complex messages to Gemini.

## Architecture

```text
main.py
└── src/
    ├── domain/         # Plain models shared by every layer (EmailMessage, TriageResult, ...)
    ├── ports/          # typing.Protocol interfaces the core depends on
    ├── bootstrap.py    # The only place that picks and builds concrete adapters
    ├── gmail/          # OAuth2 Gmail client + unread/history ingestion + labels/drafts
    ├── triage/         # Fast triage decision engine (JEV API + fallback heuristics)
    ├── llm/            # Gemini summary + draft generation
    ├── gateways/       # Telegram bot (alerts + inbound polling), Discord webhook
    ├── interactions/   # Telegram buttons and replies -> feedback, Gmail drafts
    ├── storage/        # SQLite decisions, feedback, alert dedup, bot state
    ├── maintenance/    # Daily verified backups of the store, with rotation
    └── observability/  # /metrics endpoint (Prometheus counters + latency + token usage)
```

## Features

- **Dual-stage routing**:
  1. `src/triage` classifies each email into `{ urgency, category, confidence }`
  2. only `urgency=high` (or low confidence) paths invoke `src/llm` Gemini generation
- **Retroactive ingestion** with `--sync-history` startup flag and Gmail 429 exponential backoff
- **Urgency alerts** to Telegram + Discord
- **Feedback and replies from Telegram** (see below)
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
- `FETCH_MAX_AGE_DAYS` (default `3`; only unread inbox mails newer than this are processed; promotions are filtered by the classifier, never dropped by the query) and `FETCH_QUERY` (full Gmail query override)
- `LOW_CONFIDENCE_THRESHOLD` (default `0.50`; below it a mail is labeled and never archived, and only `high` urgency triggers an alert)

## Choosing / adding implementations

The core (`main.py`, `src/workflow.py`, `src/gateways/alerts.py`, `src/interactions/`) only talks to the protocols in `src/ports`. `src/bootstrap.py` builds the concrete adapters from these selectors:

| Variable | Default | Choices |
| --- | --- | --- |
| `MAIL_PROVIDER` | `gmail` | `gmail` |
| `CLASSIFIER` | `jev` | `jev` (JEV with heuristic fallback), `heuristic` (local rules only) |
| `LLM_PROVIDER` | `gemini` | `gemini` |
| `ALERT_CHANNELS` | `telegram,discord` | comma-separated list of `telegram`, `discord`; every listed channel receives alerts in this order, the first interactive one carries reply buttons; unlisted channels are not built |
| `CHAT_INBOX` | `telegram` | `telegram`, `none` (no inbound buttons or replies) |
| `STORE_BACKEND` | `sqlite` | `sqlite` |

An unknown value stops startup with an error listing the valid choices. Startup connection checks are named after the selected implementation (`gmail`, `gemini`, `jev`/`heuristic`, `telegram`, `discord`).

To add an implementation:

1. Implement the matching protocol from `src/ports` in a new adapter module.
2. Register a factory for it in the matching registry of `src/bootstrap.py` (`MAIL_PROVIDERS`, `CLASSIFIERS`, `ANALYZERS`, `ALERT_CHANNELS`, `CHAT_INBOXES`, `STORES`). Factories receive a `BuildContext` exposing the settings, the store and shared clients.
3. `tests/test_ports_conformance.py` iterates the registries, so the new adapter is checked against its protocol automatically; it also fails if a core module imports an adapter directly.

## Health and watchdog

`/healthz` returns 503 when the polling task has stopped or shows no activity for `max(5 x POLL_INTERVAL_SECONDS, 600)` seconds. With `WATCHDOG_ENABLED=true` (default) an in-process watchdog then exits the process so Docker's `restart: unless-stopped` brings it back; a Docker healthcheck alone only marks the container unhealthy.

A failing mail fetch (revoked refresh token, Gmail outage, exhausted rate-limit retries) does not trip the watchdog: restarting would not fix it. Instead, once fetching has failed for `POLL_FAILURE_ALERT_MINUTES` (default `10`, `0` disables), the assistant sends one message to the chat naming the error type and HTTP status, and a second one with the outage duration when fetching works again. The message is retried every cycle until delivered. `RefreshError` means the Gmail refresh token is no longer valid: generate a new one (see above) and restart. Failed cycles are counted in `poll_failures_total`.

## Startup connection checks

At startup the app probes every configured connection (Gmail, Gemini, JEV, Telegram, Discord) with read-only calls and logs one line per service (`OK`, `FAILED` or `SKIPPED` when not configured). No message is sent; secrets never appear in the logs. `STARTUP_CHECKS` controls the behavior:

- `warn` (default): log results and start anyway.
- `strict`: refuse to start if any configured connection fails.
- `off`: skip the checks.

## Feedback and replies from Telegram

With `TELEGRAM_INBOUND_ENABLED=true` (and `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` set) the assistant long-polls the bot in a background thread:

- Each urgent alert carries **[Valider] / [Faux-Urgent] / [Faux-Spam]**. Verdicts are stored in SQLite and counted in `feedback_total{verdict}` to measure routing precision.
- **Replying** to an alert in Telegram creates a Gmail reply draft in the original thread and shows a preview with **[Envoyer] / [Annuler]**. Nothing is sent until you press [Envoyer] on that preview (buttons forged for another draft or pressed elsewhere are refused); each draft is sent at most once. If the send call fails its outcome is uncertain: check Gmail's Sent folder before sending the draft by hand. Outcomes are counted in `chat_replies_total{status}`.
- **Commands**: a message starting with `/` that is not a reply to an alert is a command; `/help` lists them. A redelivered command runs once. Counted in `chat_commands_total{command,status}`.
- `JEV_FEW_SHOT_ENABLED=true` additionally sends your latest Faux-Urgent/Faux-Spam corrections to JEV as examples. Only the sender's domain and the subject (truncated to 100 characters) are sent, never the body, but a subject is still attacker-chosen text replayed into every later classification. Off by default until verified against the live API.

Only one process may poll a given bot token: Telegram returns 409 to a second poller, so enable the flag on a single instance.

**Who may act.** Updates are accepted only from `TELEGRAM_CHAT_ID` *and* from an allowed user. In a private chat the chat id is your user id, so nothing else is needed. For a group or channel chat set `TELEGRAM_ALLOWED_USER_IDS` (comma-separated numeric user ids); without it the listener refuses to start, logs an error and alerts are sent without buttons. Posts made anonymously as the group or a channel are always refused. Rejected updates are only logged at DEBUG and counted in `telegram_inbound_rejected_total{reason}` (`foreign_chat`, `unauthorized_user`, `malformed`). Listener health: `telegram_poll_errors_total` and `telegram_last_poll_timestamp_seconds`. Discord stays outbound-only (buttons there need a public HTTPS Interactions endpoint).

State lives in the SQLite file at `DB_PATH` (default `data/assistant.db`; `/data/assistant.db` in the image, on the `assistant-data` named volume). It holds subjects, senders and body excerpts, so when the app creates the file it is `0600` (and its directory `0700` if the app creates it). Prefer the named volume: with a bind mount the host directory must be writable by the container's `app` user, and its ownership and mode are yours to manage. See [Backup and restore](#backup-and-restore).

The schema is versioned (`PRAGMA user_version`) and upgraded at startup, one transaction per step. Before upgrading a database that already holds data the app saves its previous state next to it as `assistant.db.pre-v<version>`; delete these copies once the new version has run for a while. A database written by a newer build is refused rather than opened: run that build, or restore an older backup. Each decision records which stage made it (`rule`, `jev` or `heuristic`).

The store also persists alert dedup across restarts: an alerted mail is not alerted again for 24 hours, so a mail you mark unread again after that is processed anew. Retention is 90 days, pruned at startup and then daily: decisions without a verdict, alert markers and reply/send dedup state are deleted; verdicts and the Telegram offset are kept.

## Backup and restore

With `BACKUP_DIR` set (`/backups` in `docker-compose.yml`, on the `assistant-backups` volume; empty disables it), the assistant copies the database at startup and then once a day to `assistant-YYYY-MM-DD.db`, keeping the `BACKUP_KEEP` most recent files (default `7`). Each copy is taken with SQLite's online backup, so it is consistent while the app runs, and is only kept if it passes `PRAGMA integrity_check`; a failed run leaves the previous copy untouched. Files are `0600`. Watch `backup_last_success_timestamp_seconds` and `backup_failures_total`.

The backup volume sits on the same disk as the database: it protects against a corrupted file or a bad migration, not against losing the host. Copy it elsewhere on a schedule of your own:

```bash
docker cp my-gmail-assistant:/backups ./assistant-backups
```

To restore a copy:

```bash
docker compose stop assistant
docker compose run --rm --no-deps assistant sh -c \
  'cp /backups/assistant-2026-10-01.db /data/assistant.db && rm -f /data/assistant.db-wal /data/assistant.db-shm'
docker compose start assistant
```

The stale `-wal` and `-shm` files belong to the replaced database and must go with it. Mails processed after the restored copy was taken lose their verdicts and alert dedup, so an unread urgent mail from that window can be alerted again.

## Docker deployment

Build and run the assistant, Prometheus, and Grafana:

```bash
docker compose up --build -d
```

Useful endpoints:

- Assistant health: `http://localhost:8000/healthz`
- Metrics: `http://localhost:8000/metrics`
- Prometheus: `http://localhost:9090`
- Grafana: `http://localhost:3000` — pre-provisioned with the Prometheus datasource and a "Gmail Assistant" dashboard (processed emails, triage latency, JEV fallback rate, LLM tokens and cost, alert feedback, Telegram replies and Telegram listener health). Login `admin` / `GRAFANA_ADMIN_PASSWORD` (defaults to `admin` if unset — set it in `.env`).

## CI/CD

- `.github/workflows/ci.yml`: lint (ruff) + tests (pytest) + Docker build sanity check on every PR and push to `prod`.
- `.github/workflows/release.yml`: on push to `prod`, runs [python-semantic-release](https://python-semantic-release.readthedocs.io/) against [Conventional Commits](https://www.conventionalcommits.org/) to bump `pyproject.toml`, update `CHANGELOG.md`, tag (`vX.Y.Z`) and cut a GitHub Release; on a new release it builds and pushes the Docker image to `ghcr.io/<repo>:<version>` and `:latest`.

Commit convention (drives the version bump):

- `fix: ...` → patch
- `feat: ...` → minor
- `feat!: ...` / `BREAKING CHANGE:` footer → major
