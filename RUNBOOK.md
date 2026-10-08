# Runbook

What to do when the assistant misbehaves. Each entry gives the sign you will see first, how to confirm the cause, and the fix. Commands are run on the host, in the directory of `docker-compose.yml`.

Start here, whatever the problem:

```bash
docker compose ps                                  # is the container up, healthy, restarting?
docker compose logs --since 30m assistant          # what it says
curl -s localhost:8000/healthz                     # 200 ok, or 503 with the reason
```

The first log line of a start is `Starting my-gmail-assistant <version>`, followed by one `Connection check [...]` line per service. The same report is sent to Telegram at each start.

| Sign | Entry |
| --- | --- |
| Telegram: "Relève des mails en échec depuis N min : RefreshError" | [Gmail token revoked](#gmail-token-revoked) |
| Telegram: "Relève des mails en échec" with another error | [Gmail unreachable](#gmail-unreachable-or-rate-limited) |
| Telegram: "Boutons et commandes Telegram hors service" | [Telegram listener silent](#telegram-listener-silent-409-double-poller) |
| Telegram: "JEV ne répond pas" | [JEV unavailable](#jev-unavailable) |
| Telegram: "Gemini en erreur" or alerts ending with "(résumé indisponible)" | [Gemini quota or outage](#gemini-quota-timeout-or-outage) |
| Telegram: "Budget LLM du jour atteint" | [LLM budget reached](#llm-budget-reached) |
| Telegram: "N traitements de mail en échec" | [Mails skipped](#mails-skipped) |
| Grafana: `followup_refresh_errors_total` rising, log `Could not list sent threads` | [Follow-up refresh failing](#follow-up-refresh-failing) |
| Container restarts in a loop, log: `SchemaVersionError` | [Database newer than the build](#database-newer-than-the-build) |
| Container restarts in a loop, log: `sqlite3.DatabaseError` or "malformed" | [Database corrupted](#database-corrupted) |
| Nothing arrives at all, no message either | [Total silence](#total-silence) |

## Gmail token revoked

- **Sign.** Telegram message naming `RefreshError`; log `Failed to fetch unread emails`; `poll_failures_total` rising. The container stays up and healthy: restarting would not fix it, so the watchdog leaves it alone.
- **Cause.** The refresh token was revoked, or the OAuth consent screen is still in *Testing* mode, where tokens expire after 7 days.
- **Fix.** On a machine with a browser: `python -m src.gmail.token_setup client_secret.json`, paste the three printed lines into the host's `.env`, then `docker compose up -d assistant`. Publish the consent screen (*In production*) if it was in testing.
- **After.** A Telegram message "Relève des mails rétablie". Unread mails from the outage are processed, limited to the last `FETCH_MAX_AGE_DAYS` (3 by default): for a longer outage, raise it for one run.

## Gmail unreachable or rate limited

- **Sign.** Same Telegram message with `HttpError (HTTP 429)`, `HttpError (HTTP 5xx)`, `TimeoutError` or a connection error.
- **Check.** `docker compose exec assistant python -c "import urllib.request; print(urllib.request.urlopen('https://www.googleapis.com/discovery/v1/apis/gmail/v1/rest', timeout=15).status)"` prints `200` when the host reaches Google: the problem is then on Google's side or the quota, not the network.
- **Fix.** Nothing to do for a Google outage or a passing 429: the fetch is retried every `POLL_INTERVAL_SECONDS` and the recovery message follows. A 429 that lasts means the quota of the Google Cloud project is exhausted: check it in the Cloud console.

## Telegram listener silent (409, double poller)

- **Sign.** Telegram message "Boutons et commandes Telegram hors service"; buttons do nothing, `/review` gets no answer; log `Chat polling failed (TelegramApiError)` repeating; `telegram_poll_errors_total` rising. Alerts still arrive: sending does not depend on the listener.
- **Cause.** Almost always a second process polling the same bot token: Telegram answers `409 Conflict` to one of them. A second container, a local run with the production `.env`, or a webhook set on the bot.
- **Check.** `docker ps -a | grep gmail-assistant` on every machine that ever ran the bot. `curl -s "https://api.telegram.org/bot<token>/getWebhookInfo"` must show an empty `url`.
- **Fix.** Stop the other process, or set `TELEGRAM_INBOUND_ENABLED=false` on it. Remove a webhook with `deleteWebhook`. The listener recovers by itself within a minute; a message "Boutons et commandes Telegram rétablis" follows.
- **If the thread died.** The message comes at once instead of after 10 minutes. Restart: `docker compose restart assistant`, and keep the logs, this is a bug.

## JEV unavailable

- **Sign.** Telegram message "JEV ne répond pas : N mails classés par l'heuristique de repli"; `jev_fallback_total` rising; decisions stored with `source=heuristic`.
- **Effect.** Triage continues with keyword rules: fewer alerts are missed than you would fear, but categories are coarse and nothing is put forward or drafted on the reply question, since those answers come from JEV.
- **Check.** The startup line `Connection check [jev]`. `docker compose exec assistant python -m src.evaluation check-examples` makes one real call and prints what the API answers. HTTP 401 or 403 points at the key; for any other status, look at the TypeSafe console (credit, quota, incident).
- **Fix.** Renew `JEV_API_KEY` in `.env` and `docker compose up -d assistant`, or wait for the service. Mails classified by the heuristic during the outage are not classified again; `/review` shows them.
- **Blind spot.** The alert counts fallbacks, so it needs at least `HEALTH_ALERT_MIN_EVENTS` mails in the window. On a quiet day an outage is only visible in `/stats` (decisions by source) and in Grafana.

## Gemini quota, timeout or outage

- **Sign.** Alerts arrive with the start of the mail and "(résumé indisponible)" instead of a summary, and without a draft. Telegram message "Gemini en erreur". `llm_errors_total` by `reason`: `rate_limited` (quota), `timeout`, `unavailable`, `parse`, `placeholder`.
- **Effect.** No alert is lost: when the analysis fails, the alert goes out with the start of the mail instead of the summary.
- **Fix by reason.**
  - `rate_limited`: lower `GEMINI_MAX_RPM` below the quota of your tier, or wait for the daily quota to reset.
  - `timeout`: Gemini answers slower than `GEMINI_TIMEOUT_SECONDS`. Passing; raise it only if it lasts.
  - `unavailable` at every call with `Connection check [gemini] FAILED`: the key, or a model that was retired. Check `GEMINI_MODEL` against the models the API lists.
  - `parse` or `placeholder`: the model answered badly; a few are normal.

## LLM budget reached

- **Sign.** Telegram message "Budget LLM du jour atteint : X $ sur Y $". `llm_errors_total{reason="budget"}` rising.
- **Effect.** Until the next local day, Gemini is not called: alerts without summary or draft.
- **Check.** Whether the spend is real: `sum by (kind) (increase(llm_cost_usd_total[24h]))` in Prometheus, and `reply_drafts_total` for an unusual number of drafts.
- **Fix.** Raise `LLM_DAILY_BUDGET_USD` in `.env` and restart to resume today. If the spend is not explained, leave the cap and look at what triggered the calls before raising it.

## Mails skipped

- **Sign.** Telegram message "N traitements de mail en échec"; log `Failed to process email <id>; skipping` with a traceback; `emails_skipped_total` rising.
- **Effect.** The mail stays unread and is retried at every cycle, so one mail that always fails produces the message by itself. An urgent mail is not alerted again at each retry: the alert is recorded as soon as it is sent.
- **Check.** The traceback names the step. A Gmail error on labeling is passing. The same mail id failing for hours on something else is a bug.
- **Fix.** For a mail that will never pass, mark it read in Gmail: it leaves the fetch query. Keep the traceback.

## Follow-up refresh failing

- **Sign.** `followup_refresh_errors_total{step="list"}` or `{step="thread"}` rising; log `Could not list sent threads` or `Could not refresh sent thread <id>`. Triage and alerts are not affected.
- **Effect.** The states of the followed threads are not updated: an answer that came meanwhile is not seen until a refresh succeeds. In `shadow` mode nothing is sent, so only the measurement is late.
- **Check.** A `403` on `step="list"` right after enabling the mode points at the token's scope: `users.settings.sendAs.list` needs `gmail.modify`, which `token_setup` asks for. A `429` is the Gmail quota: lower `FOLLOW_UP_MAX_THREADS` or raise `FOLLOW_UP_REFRESH_MINUTES`.
- **Fix.** `FOLLOW_UP_MODE=off` stops it at once; the threads already followed are kept for when it is turned back on.

## Database newer than the build

- **Sign.** The container restarts in a loop; log `SchemaVersionError: database is at schema version N, this build only knows up to M`.
- **Cause.** An older image was started on a database a newer one had upgraded: a rollback, or `VERSION` lowered by mistake.
- **Fix.** Either run the newer version again (`VERSION` in `.env`), or follow "Rolling back" in the README: restore the `assistant.db.pre-v<N>` copy left by the upgrade, then start the older version. What was recorded since the upgrade is lost with the second choice.

## Database corrupted

- **Sign.** The container restarts in a loop with `sqlite3.DatabaseError`, or the daily backup fails: `backup_failures_total` rising and `backup_last_success_timestamp_seconds` getting old (a backup is kept only if it passes `PRAGMA integrity_check`).
- **Check.** `docker compose run --rm --no-deps assistant python -c "import sqlite3; print(sqlite3.connect('/data/assistant.db').execute('PRAGMA integrity_check').fetchone())"`.
- **Fix.** Restore the latest backup: "Backup and restore" in the README. Verdicts given after that backup are lost; an unread urgent mail from that window can be alerted again.
- **Without any backup.** Move the file away and start: the assistant creates an empty database and works, without history, verdicts or few-shot examples.

## Total silence

No alert, no message, nothing in Telegram.

1. `docker compose ps`: if the container is not running, `docker compose up -d` and read the logs of the failed start.
2. `curl -s localhost:8000/healthz`: a 503 names a stalled or stopped polling loop; the watchdog should already have restarted the container.
3. Is Telegram itself reachable from the host? `docker compose logs assistant | grep -i telegram`. Every health message goes through Telegram: when it is down, or the bot token was revoked, nothing can tell you. Discord, if configured, still receives urgent alerts.
4. No mail at all for hours is also normal on a quiet day: `docker compose logs --since 10m assistant | grep "Polled Gmail"` shows the loop alive.

## After an incident

Rule S3 of `PLAN.md`: every production failure adds a test that reproduces it and a line here.
