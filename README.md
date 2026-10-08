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
- **Urgency alerts** to Telegram + Discord. Repeated automated alerts (same sender and same subject once commit hashes and numbers are removed, within 30 minutes) are folded into the first one, whose Telegram message is updated with the count and the latest subject instead of notifying again. A different subject, such as another workflow of the same repository, alerts immediately; mail from a person is never grouped. Outcomes: `alerts_total{status="deduplicated"|"updated"|"update_failed"}`
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

To work on the code, install `requirements-dev.txt` instead: it adds `pytest`, `pytest-cov` and `ruff` at the versions CI uses. Then `ruff check .` and `pytest --cov`; the run fails below the coverage floor set in `pyproject.toml` (`fail_under`).

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
- `GEMINI_TIMEOUT_SECONDS` (default `30`): a Gemini call that takes longer is abandoned, not retried, and counted in `llm_errors_total{reason="timeout"}`. The alert then goes out without a summary and a reply draft is skipped, as when Gemini is down
- `USER_DISPLAY_NAME` (name used to sign drafted replies; without it drafts have no signature)
- `JEV_API_URL` (default `https://api.typesafe.ai/v1/systemone`)
- `JEV_API_KEY` (TypeSafe key from https://console.typesafe.ai/; without it the heuristic fallback is used)
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `DISCORD_WEBHOOK_URL` (must be `https://discord.com/api/webhooks/<id>/<token>`)
- `FETCH_MAX_AGE_DAYS` (default `3`; only unread inbox mails newer than this are processed; promotions are filtered by the classifier, never dropped by the query) and `FETCH_QUERY` (full Gmail query override)
- `LOW_CONFIDENCE_THRESHOLD` (default `0.50`; below it a mail is labeled and never archived, and only `high` urgency triggers an alert)

## Triage rules and VIP senders

Deterministic rules run before the classifier: a matching mail costs no JEV call and gets confidence `1.0`. A few bulk senders are built in (`src/triage/rules.py`). To add your own, copy `config/triage_rules.example.toml` to `config/triage_rules.toml` (git-ignored, mounted read-only at `/config` by `docker-compose.yml`) and set `TRIAGE_RULES_PATH=/config/triage_rules.toml`.

- `[[rules]]`: `sender` and optional `subject` are case-insensitive regular expressions, `urgency` and `category` must be valid values. File rules are checked in order, before the built-in ones. A rule on a CI subject should name the repository, so a production failure elsewhere still reaches the classifier.
- `vip`: exact addresses whose mail always alerts (`high`, source `vip`). A VIP is only honoured when Gmail's own `Authentication-Results` header reports `dmarc=pass`, since a `From` address alone can be forged; otherwise the mail is classified normally. A domain without DMARC therefore never gets VIP treatment.

The file is read at startup and an invalid one stops the app with the offending rule. `python -m src.evaluation candidates` suggests senders worth a rule. Each decision records its `source` (`vip`, `rule`, `jev`, `heuristic`), which also labels the `triage_confidence` histogram so the JEV confidence can be followed on its own.

## Unsubscribe proposals

With `UNSUBSCRIBE_PROPOSALS_ENABLED=true` (off by default; needs `TELEGRAM_INBOUND_ENABLED`), the assistant offers, once per sender, to unsubscribe from a sender when at least `UNSUBSCRIBE_MIN_ARCHIVED` (default `5`) of its mails were archived over 30 days and none was kept or marked as wrongly archived. The message carries **[Se désabonner] / [Garder]**; nothing is sent until you press the button on that message, and at most once.

Only the one-click mechanism of RFC 8058 is used: the mail must carry `List-Unsubscribe-Post: List-Unsubscribe=One-Click` and an `https` link, and pass DMARC, since these headers are written by the sender. The request is a single `POST` to that link, which stays on the server and never travels in the button. Because the link is chosen by the sender, the request is refused unless the host resolves only to public addresses, the connection goes to the address that was checked, and redirects are not followed. `mailto:` unsubscribe links are not handled: use Gmail for those. Outcomes: `unsubscribes_total{status}` (`offered`, `done`, `failed`, `kept`, `rejected`, `duplicate`).

## Reply drafts whatever the urgency

With `NEEDS_REPLY_ENABLED=true` (off by default), the JEV call that classifies a mail also asks whether a person expects a written reply. It is one more question in the same request, not a second call; on test mails it added about 15 % of tokens and no measurable latency.

When the answer reaches `NEEDS_REPLY_THRESHOLD` (default `0.5`), the assistant writes a reply draft in the Gmail thread and adds the label `Assistant/A_repondre`, whether the mail is urgent or not. Preparing a reply and notifying are two separate decisions: the Telegram alert depends on urgency alone, so a mail that is not urgent gets its draft silently. Nothing is ever sent by mail: the draft waits in Gmail.

An urgent mail is drafted when a reply is expected **or**, as before, when it is personal mail, an introduction or a job offer from a sender that is not automated: mails decided without the question, or just under the threshold, keep the draft they always had.

- Mails from automated senders, and mails classified as spam, newsletter, promotion or job alert, are never drafted, whatever they ask.
- Mails decided by a rule, the VIP list or the heuristic fallback are not asked the question: not urgent, they get no draft; urgent, only the category rule above applies.
- A draft never decides for you: it does not accept or decline an invitation or an offer and promises no date or amount. When the answer depends on something the mail does not contain, the draft acknowledges the mail and announces a later answer.
- If Gemini is unavailable the mail is still labeled `Assistant/A_repondre`, without a draft.
- The probability is stored with each decision and logged, to tune the threshold. Outcomes: `reply_drafts_total{status}` (`drafted`, `no_draft`). JEV token usage: `llm_tokens_total{kind="triage"}`.

## Mails to put forward: observation mode

Some mails are neither urgent nor ordinary: an event you are registered for, a planned outage of something you use, something to do before a date, a person waiting for an answer. With `ATTENTION_MODE=shadow` or `on` (default `off`), the JEV call that classifies a mail also asks three yes/no questions, and whether a reply is expected:

| Question | Says yes to | Says no to |
| --- | --- | --- |
| `personal_event` | an event, meeting or appointment you are invited to by name, registered for or reminded of | advertised webinars, courses and meetups, a newsletter agenda |
| `service_change` | a dated outage, maintenance, migration or removal of a service you use | terms updates needing no action, product news |
| `personal_deadline` | something you must provide, pay, renew or confirm before a date | sales offers ending soon, enrolment deadlines of advertised things |

In `shadow` mode the answers are **stored and counted, nothing else changes**: no label, no message, no change of route. It exists to measure, on your real mail, how many mails the questions would put forward before they are allowed to do it.

- A mail classified as spam, newsletter, promotion or job alert is never counted, whatever the answers. Automated senders are counted: outage notices come from them, and so do messages a platform relays for a person.
- `ATTENTION_THRESHOLD` (default `0.5`) is the probability from which an answer counts as yes.
- Read the result with `docker compose exec assistant python -m src.evaluation attention [--days 14]`: number of mails per day that would be put forward, reasons, and the list. No JEV call is made. Counted live in `attention_signals_total{signal}`.
- Cost, measured on the test mails: 526 more tokens per mail for the four questions (942 to 1468), latency unchanged (0.25 s to 0.26 s).

### Putting them forward

`ATTENTION_MODE=on` acts on the answers. A mail that is not urgent and has at least one reason (one of the three questions, or a person waiting for a reply):

- gets the Gmail label `Assistant/A_voir`, next to its category label, with no notification;
- is **kept in the inbox** even when the "low-urgency notification" rule would have archived it. Spam, newsletters, promotions and job alerts are archived as before, whatever the answers.

Urgent mails are unchanged: they are alerted. A mail labeled `Assistant/A_repondre` is also labeled `Assistant/A_voir`; a person's message relayed from a `noreply` address gets `A_voir` only, since no reply can be drafted to it. The decision is stored (`decisions.put_forward`) and counted in `mails_put_forward_total`.

Switch from `shadow` to `on` once `python -m src.evaluation attention` shows a volume you are willing to look at every day.

### The daily list

A label alone shows little: a processed mail is marked read. With `ATTENTION_LIST_HOUR` set (local hour `0` to `23` in `TIMEZONE`; `-1`, the default, disables it) and `ATTENTION_MODE=on`, the assistant sends once a day, **without sound**, the mails put forward since the previous list: one header, then one message per mail with the reason, at most 5; the mails beyond five come with the next day's list. Nothing is sent on a day with nothing new. The hour is yours to choose, the list never rings: it is the only proactive message besides urgent alerts.

- Each mail carries **[Vu] / [Pas utile]** when `TELEGRAM_INBOUND_ENABLED` is on. Both remove it from the list; `[Pas utile]` is stored as the verdict `false_important` (origin `list`), which is how wrongly promoted mails get measured.
- **`/avoir`** lists, on demand, the mails put forward over the last 7 days that still wait (the 10 latest): not rated, and still in your Gmail inbox. Archiving or deleting a mail in Gmail handles it too. The command only exists with `ATTENTION_MODE=on`.
- The list is sent at the first polling cycle at or after the hour; a restart neither repeats nor skips it, and a late start still sends it that day. If Telegram fails, the mails that did not go out come with the next day's list.
- `TIMEZONE` is an IANA name such as `Europe/Paris` (default `UTC`); an unknown name stops the startup.

### Notification budget

Messages the assistant sends on its own initiative, the daily list today and follow-up offers later, go through a daily budget. Urgent alerts, replies to your commands and health messages never do.

- `PROACTIVE_DAILY_CAP` (default `6`) is the number of such messages per local day. The daily list counts as one, whatever its length. Only a message that went out is counted.
- `QUIET_HOURS` (empty by default) is a window of local hours with none, written `22-8`: from 22:00 to 07:59. It may cross midnight.
- A list held back by the cap goes out at a later cycle, or with the next day's list. A list hour inside the quiet hours stops the startup, since it would never go out.
- The day's count is kept in the database: a restart does not reset it.

## Follow-ups of mails you sent: observation mode

With `FOLLOW_UP_MODE=shadow` (default `off`), the assistant keeps track of the threads holding a mail you sent and whether they still wait for an answer. Nothing is sent and no message is posted: this mode only measures, before follow-ups are offered.

- Every `FOLLOW_UP_REFRESH_MINUTES` (15), it lists the threads with a mail you sent in the last 14 days and reads their **headers only**, never the bodies. A thread is read again only when Gmail says it changed (`historyId`).
- Only mail sent **after the mode was first turned on** is followed, so turning it on does not bring up two weeks of past mail. The date is kept in the database.
- Your last mail in a thread is its anchor. The thread **waits for them** until any person writes after it, whatever the address: the recipient, someone in copy, or someone new. Auto-replies (`Auto-Submitted`, `Precedence: auto_reply`, `X-Autoreply`) do not count as an answer; an answer sent through a mailing list does. A delivery failure closes the thread. A mail you send later in the thread becomes the new anchor.
- "You" means every address you send from (Gmail's *Send mail as* aliases), whatever the `+tag` and, at Gmail, the dots.
- Not followed: mail sent only to yourself, or only to automated addresses (`noreply`, `notifications`...).
- A follow-up becomes due `FOLLOW_UP_AFTER_DAYS` weekdays (3) after the anchor, in `TIMEZONE`. A thread still unanswered after 30 days stops being followed.
- Only the `FOLLOW_UP_MAX_THREADS` (50) most recent threads with a mail you sent are listed; `followup_listing_capped` is `1` when there were more, which then go unfollowed. Threads still waiting but out of the listing are read again at every refresh until they expire (`followup_unlisted_waiting_threads`).
- Gauge `followup_threads{state}` (`waiting_for_them`, `closed`, `ignored`); read errors in `followup_refresh_errors_total{step}`.
- A thread still waiting is never pruned; a settled one is forgotten after 90 days.

### Does my mail wait for something?

A thread waiting for an answer is not enough: after they answer, your "Merci !" becomes the new anchor. So JEV is asked, once per anchor, whether **your own text** waits for something: an answer, a document, a decision, a confirmation. The quoted mail you were answering and your signature are cut off first; with the quote, the question you answered would make your "Merci, bien reçu" look like it waits.

- `FOLLOW_UP_THRESHOLD` (default `0.5`) is the probability from which the answer counts as yes. Only a thread judged yes, still waiting and past its due date would get a follow-up; `followup_due_threads` counts them.
- A mail JEV could not judge is asked again at the next refresh, never assumed to wait. At most 20 are asked per refresh. Tokens are counted in `llm_tokens_total{kind="followup"}`.
- Needs `CLASSIFIER=jev`. Measured on 25 invented sent mails, French and English: 25 right at `0.5`, about 0.25 s per call. With the quote kept, "Voir ci-dessous." above a question went from 0.09 to 0.35.

**Choosing the threshold on your own mail** (writes only `data/sent_samples.json`, readable by you alone, ignored by git):

```bash
docker compose exec assistant python -m src.evaluation sent-collect   # one JEV call per mail sent in the last 90 days
docker compose exec -it assistant python -m src.evaluation sent-label # yes / no on 60 of them, spread over the range
docker compose exec assistant python -m src.evaluation sent-report    # precision and recall per threshold
```

The report also says, per threshold, how often an answer came within the delay anyway. It is a hint, not a truth: people answer without being asked, and ignore real requests.

### `/pending`

Lists, on demand, your sent mails still waiting for an answer (the 10 due first): recipient, subject, when it was sent and when a follow-up is due, what JEV thinks, and whether a follow-up **would be offered** now. Only exists with `FOLLOW_UP_MODE=shadow` or `on`; it answers, it never pushes, so it is outside the notification budget.

- **[Relance utile] / [Pas de relance]** on each thread rate it, whatever JEV said: a "relance utile" on a mail JEV judged as waiting for nothing is a miss to learn from. A thread rated "pas de relance" is never offered a follow-up. Counted in `followup_verdicts_total{verdict}`.
- The header counts, among the mails JEV judged as waiting, how many you rated useful. Move to `on` only after at least 10 rated, with at least 80 % useful.

### Offering follow-ups (`FOLLOW_UP_MODE=on`)

Once `/pending` shows enough useful verdicts, `on` offers the follow-ups. Nothing is ever sent without your press on **[Envoyer]**.

- From `FOLLOW_UP_HOUR` (10, local) on weekdays, never on Friday after 17:00 nor at the weekend, each due follow-up is offered in **its own silent Telegram message**, at most `FOLLOW_UP_DAILY_MAX` (3) a day and within the notification budget. Without `QUIET_HOURS`, an offer can come late in the evening: it is silent, but set them to avoid it. Needs `TELEGRAM_INBOUND_ENABLED=true`, since the offer is made of buttons.
- Before an offer, the thread is read again in Gmail, and every recipient is searched for a mail sent after yours, in any thread: an answer found closes the thread instead.
- The message shows the recipients (those of your mail, To and Cc), the subject and **the exact text that would be sent**: a fixed reminder in the language of your mail (from its text, then its subject, French when neither tells), with "tu" if you wrote "tu", "Bonjour," with no name guessed from an address, your `USER_DISPLAY_NAME` as signature. No LLM writes it: read it before pressing, and use [Modifier dans Gmail] when it does not fit.
- **[Envoyer]** checks everything again: the mode is still `on`, the offer is less than two weekdays old and pressed on the message that made it, your mail is still the last of the thread, no answer came in the thread or elsewhere. Then it creates the reply in the thread and sends it, once: a second press, or one redelivered after a crash, sends nothing and creates no second draft. If Gmail fails after the draft was created, you are told to check your Sent folder.
- **[Reporter 3 j]** offers it again three days later. **[Ne pas relancer]** closes the thread for good. **[Modifier dans Gmail]** creates the draft and leaves it to you; the bot will not send it.
- A follow-up is offered once per thread: the one you send becomes the thread's last mail, and is never followed up in turn. An offer left unanswered for two weekdays expires and is not made again.
- Mails to more than 5 people are not followed.
- Setting `FOLLOW_UP_MODE` back to `shadow` or `off` makes [Envoyer] and [Modifier dans Gmail], on buttons already on screen, answer that follow-ups are off; [Reporter] and [Ne pas relancer] still record your choice.
- Outcomes in `followup_proposals_total{outcome}`: `offered`, `sent`, `send_failed`, `duplicate`, `snoozed`, `dismissed`, `handed_off`, `expired`, `rejected`, and why a due follow-up was dropped at offer or send time: `answered` (in the thread), `answered_elsewhere` (any mail from a recipient since yours, auto-replies included: out-of-office replies end the follow-up too), `bounced`, `deleted`, `anchor_changed`.

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
| `UNSUBSCRIBER` | `http` | `http` (RFC 8058 one-click POST), `none` |

An unknown value stops startup with an error listing the valid choices. Startup connection checks are named after the selected implementation (`gmail`, `gemini`, `jev`/`heuristic`, `telegram`, `discord`).

To add an implementation:

1. Implement the matching protocol from `src/ports` in a new adapter module.
2. Register a factory for it in the matching registry of `src/bootstrap.py` (`MAIL_PROVIDERS`, `CLASSIFIERS`, `ANALYZERS`, `ALERT_CHANNELS`, `CHAT_INBOXES`, `STORES`, `UNSUBSCRIBERS`). Factories receive a `BuildContext` exposing the settings, the store and shared clients.
3. `tests/test_ports_conformance.py` iterates the registries, so the new adapter is checked against its protocol automatically; it also fails if a core module imports an adapter directly.

## Health and watchdog

When something goes wrong, [RUNBOOK.md](RUNBOOK.md) lists the known failures by the sign you see first, with the check and the fix.

`/healthz` returns 503 when the polling task has stopped or shows no activity for `max(5 x POLL_INTERVAL_SECONDS, 600)` seconds. With `WATCHDOG_ENABLED=true` (default) an in-process watchdog then exits the process so Docker's `restart: unless-stopped` brings it back; a Docker healthcheck alone only marks the container unhealthy.

A failing mail fetch (revoked refresh token, Gmail outage, exhausted rate-limit retries) does not trip the watchdog: restarting would not fix it. Instead, once fetching has failed for `POLL_FAILURE_ALERT_MINUTES` (default `10`, `0` disables), the assistant sends one message to the chat naming the error type and HTTP status, and a second one with the outage duration when fetching works again. The message is retried every cycle until delivered. `RefreshError` means the Gmail refresh token is no longer valid: generate a new one (see above) and restart. Failed cycles are counted in `poll_failures_total`.

### Alerts when something degrades

The dashboards showed these failures; nobody was told. At each polling cycle the assistant now checks them and sends a Telegram message when one starts, and another when it is over:

| Check | Problem when | Effect on mail |
| --- | --- | --- |
| JEV | `HEALTH_ALERT_MIN_EVENTS` (default 3) heuristic fallbacks over `HEALTH_ALERT_WINDOW_MINUTES` (default 15) | triage goes on, less precise |
| Mail processing | as many mails skipped over the window | they stay unread and are retried |
| Gemini | as many failed calls over the window (budget skips excluded) | alerts go out without summary or draft |
| Telegram listener | listener thread dead, or no successful poll for 10 minutes (only with `TELEGRAM_INBOUND_ENABLED`) | buttons and commands stop, alerts still arrive |
| LLM budget | the day's estimated spend reached `LLM_DAILY_BUDGET_USD` | see below |

`HEALTH_ALERT_WINDOW_MINUTES=0` turns the checks off. A message that could not be delivered is tried again at the next cycle. The first three count real failures, so they need traffic: an outage of JEV on a day with two mails stays silent. The recovery message means "no more failure over the window", not that the service was probed.

**Daily LLM budget.** `LLM_DAILY_BUDGET_USD` (default `0`, no cap) is a cap on the estimated cost of Gemini calls per local day (`TIMEZONE`). Once reached, Gemini is no longer called until the next day: urgent alerts still go out, without summary or draft, and each skipped call is counted in `llm_errors_total{reason="budget"}`. The day's total is kept in the database, so a restart does not reset it. The estimate comes from `llm_cost_usd_total`, which only knows the models listed in `PRICING_PER_MILLION_TOKENS` (`src/llm/gemini.py`): with another model, or for JEV whose price is not known to the app, the cap sees nothing.

## Startup connection checks

At startup the app probes every configured connection (Gmail, Gemini, JEV, Telegram, Discord) with read-only calls, logs one line per service (`OK`, `FAILED` or `SKIPPED` when not configured) and sends the same report to the chat. Secrets never appear in the logs or in the report. `STARTUP_CHECKS` controls the behavior:

- `warn` (default): log results and start anyway.
- `strict`: refuse to start if any configured connection fails.
- `off`: skip the checks.

## Feedback and replies from Telegram

With `TELEGRAM_INBOUND_ENABLED=true` (and `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` set) the assistant long-polls the bot in a background thread:

- Each urgent alert carries **[Valider] / [Faux-Urgent] / [Faux-Spam]**. Verdicts are stored in SQLite and counted in `feedback_total{verdict}` to measure routing precision.
- **Replying** to an alert in Telegram creates a Gmail reply draft in the original thread and shows a preview with **[Envoyer] / [Annuler]**. Nothing is sent until you press [Envoyer] on that preview (buttons forged for another draft or pressed elsewhere are refused); each draft is sent at most once. If the send call fails its outcome is uncertain: check Gmail's Sent folder before sending the draft by hand. Outcomes are counted in `chat_replies_total{status}`.
- **`/review [n]`** (default 5, at most 10) sends mails from the last 7 days that were archived or labeled without an alert and that you have not rated yet, half archived ones and half the labeled ones the classifier was least sure about, one per sender. Decisions made by a deterministic rule are left out. An archived mail carries **[OK] / [À garder] / [À voir] / [Urgent raté]**, a labeled one **[OK] / [À voir] / [Urgent raté] / [Spam]**. **[À voir]** means the mail should have been put forward without a notification, **[Urgent raté]** that it deserved an immediate alert. These verdicts (`wrong_archive`, `missed_important`, `missed_urgent`) are stored like the alert ones, tagged `origin=review`, and `feedback_total` is labelled by `verdict` and `route`. Until schema version 4 `/review` only had [Urgent raté] for both meanings: the upgrade turns those earlier review verdicts into `missed_important` (the previous state stays in `assistant.db.pre-v4`).
- **`/stats`** answers with the share of correct decisions and the kinds of mistakes, per decision (alerted, labeled, archived) and per source (`jev`, `rule`, `heuristic`), computed from every stored verdict, plus the number of decisions per source over 30 days. It reads SQLite, so it survives restarts, unlike the Prometheus counters.
- **`/avoir`** lists the mails put forward that still wait (see [The daily list](#the-daily-list)).
- **Commands**: a message starting with `/` that is not a reply to an alert is a command; `/help` lists them. A redelivered command runs once. Counted in `chat_commands_total{command,status}`.
- `JEV_FEW_SHOT_ENABLED=true` additionally sends your latest Faux-Urgent, Faux-Spam and Urgent raté corrections to JEV as examples ([À voir] and [À garder] are not: they do not say which urgency or category was wrong). Only the sender's domain and the subject (truncated to 100 characters) are sent, never the body, but a subject is still attacker-chosen text replayed into every later classification. Off by default until verified against the live API.

Only one process may poll a given bot token: Telegram returns 409 to a second poller, so enable the flag on a single instance.

**Who may act.** Updates are accepted only from `TELEGRAM_CHAT_ID` *and* from an allowed user. In a private chat the chat id is your user id, so nothing else is needed. For a group or channel chat set `TELEGRAM_ALLOWED_USER_IDS` (comma-separated numeric user ids); without it the listener refuses to start, logs an error and alerts are sent without buttons. Posts made anonymously as the group or a channel are always refused. Rejected updates are only logged at DEBUG and counted in `telegram_inbound_rejected_total{reason}` (`foreign_chat`, `unauthorized_user`, `malformed`). Listener health: `telegram_poll_errors_total` and `telegram_last_poll_timestamp_seconds`. Discord stays outbound-only (buttons there need a public HTTPS Interactions endpoint).

State lives in the SQLite file at `DB_PATH` (default `data/assistant.db`; `/data/assistant.db` in the image, on the `assistant-data` named volume). It holds subjects, senders and body excerpts, so when the app creates the file it is `0600` (and its directory `0700` if the app creates it). Prefer the named volume: with a bind mount the host directory must be writable by the container's `app` user, and its ownership and mode are yours to manage. See [Backup and restore](#backup-and-restore).

The schema is versioned (`PRAGMA user_version`) and upgraded at startup, one transaction per step. Before upgrading a database that already holds data the app saves its previous state next to it as `assistant.db.pre-v<version>`; delete these copies once the new version has run for a while. The running version is logged at startup (`Starting my-gmail-assistant <version>`) and served in the OpenAPI document (`/openapi.json`, field `info.version`): it is read from `pyproject.toml`, which the release bumps. A database written by a newer build is refused rather than opened: run that build, or restore an older backup. Each decision records which stage made it (`rule`, `jev` or `heuristic`).

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

## Offline evaluation

`python -m src.evaluation` replays the mails you gave a verdict on, to compare classifier variants on the same set instead of comparing one week with the next. In Docker: `docker compose exec assistant python -m src.evaluation run`.

- `run` reloads each rated mail from Gmail, routes it through the rules and each variant (`heuristic`, `jev`, and `jev+few-shot` when JEV is configured), and prints the share of cases whose route agrees with your verdict, overall and per verdict, with the number of classifier calls. Corrections given before the split only serve as few-shot examples and are never scored, so the few-shot variant is not graded on its own examples; the split defaults to the middle correction and can be set with `--split <ISO timestamp>`. `--limit N` keeps the N most recent verdicts, `--variants a,b` runs a subset. Each JEV variant costs one call per case. Below 100 cases including 20 corrections the output is marked `NOT CONCLUSIVE`.
- `check-examples` makes a single live JEV call carrying `state.examples` and reports whether the API accepts it. Run it before turning `JEV_FEW_SHOT_ENABLED` on.
- `attention [--days N]` reads the answers stored in observation mode (see [Mails to put forward](#mails-to-put-forward-observation-mode)); no JEV call.
- `routing-rules [--days 90]` replays the archive rule in production (low-urgency notifications) and two candidates (low-urgency CI and code-hosting notifications; spam that claims to be urgent) on the urgency, category and confidence already stored, and confronts each with your verdicts: how many mails it applies to or would move, how many verdicts agree, and the mails whose verdict disagrees. No JEV call. A candidate is only worth adopting if no verdict disagrees.
- `candidates [--min-count N]` lists senders JEV has classified the same way at least N times over 90 days without any correction from you: candidates for a deterministic rule.
- `lab` compares **question variants** on live JEV calls, to check an idea in minutes before changing the triage. `--source corpus` (default) uses the 68 invented mails of `src/evaluation/corpus.toml`, each with its acceptable routes and expected yes/no answers; `--source rated` replays the real mails you gave a verdict on, leaving out those a rule decides. Variants: `current` (what production sends), `direct-action` (one alert / keep / archive question instead of urgency), `signals` (current plus candidate yes/no questions: reply expected, meeting, deadline, payment due, commitment), `attention` (current plus what `ATTENTION_MODE=shadow` asks; also reports which mails would be put forward). The report gives, per variant, the correct routes per run, the mails whose route changed between runs (`--repeats N`), tokens per mail, median and p95 latency, and the misrouted mails; yes/no answers are compared with the truth on the corpus and only listed on real mails, except that a mail you marked [À voir] is known to deserve being put forward. The number of JEV calls is printed first and the run is refused beyond `--max-calls` (default 500); `--limit` and `--variants` reduce it. Nothing is written to the store or to Gmail.
  - `--source recent` takes the latest mails whose body is longer than `--min-words` (default 300; `--days 30`, `--limit 60`), which carry no verdict. It first runs `current` once to get a reference route for each mail, then scores every variant on how often it routes like that reference. The `current` row is therefore the disagreement between two identical calls, the noise the other rows must be read against.
  - Body cuts are variants too: `head-700-tail-300`, `head-150`, `head-100-tail-50` (start and end around a visible `[…]`), against `current`, which sends the first 1000 words as production does. They are the default of `--source recent` and can be named with `--variants` on `--source rated`; real mails are fetched uncut for the lab so that there is an end to keep. On the short test mails they change nothing.

A verdict is read as a constraint on the route: `valid` expects the same route, `false_urgent` anything but an alert, `false_spam` an archive, `missed_urgent` an alert, `wrong_archive` and `missed_important` anything but an archive, `false_important` anything but an alert.

## Docker deployment

`docker-compose.yml` runs the image the release workflow publishes (`ghcr.io/aristidewafo/my-gmail-assistant`), with Prometheus and Grafana pinned to a version:

```bash
docker compose pull
docker compose up -d
```

- **Version.** `VERSION` in `.env` chooses the image tag (default `latest`). Pin it to a release (`VERSION=0.12.0`) so that a restart never changes the code that runs, and upgrade by raising it, then `docker compose pull assistant && docker compose up -d assistant`.
- **From a checkout** (development, or a change not released yet): `docker compose -f docker-compose.yml -f docker-compose.build.yml up --build -d`. `./install.sh --docker` does this.
- **Network exposure.** The ports are published on `BIND_ADDRESS`, `127.0.0.1` by default: `/metrics` and Prometheus have no authentication. From another machine use an SSH tunnel, e.g. `ssh -L 3000:localhost:3000 user@host`. Set `BIND_ADDRESS=0.0.0.0` only behind a firewall, and with `GRAFANA_ADMIN_PASSWORD` set.
- **Upgrading Prometheus or Grafana** is a change of the pinned tag in `docker-compose.yml`, on purpose: `latest` moved them at any restart.

### Rolling back

```bash
# .env: VERSION=<previous release>
docker compose pull assistant
docker compose up -d assistant
```

If the release you leave upgraded the database schema, the older build refuses to open it (see the log line about a newer schema). Restore the copy the upgrade left next to the database, then start the older version:

```bash
docker compose stop assistant
docker compose run --rm --no-deps assistant sh -c \
  'ls /data/assistant.db.pre-v* && cp /data/assistant.db.pre-v<N> /data/assistant.db && rm -f /data/assistant.db-wal /data/assistant.db-shm'
docker compose up -d assistant
```

`<N>` is the schema version the newer release brought. Verdicts and decisions recorded since the upgrade are lost with it; a daily backup taken before the upgrade (see [Backup and restore](#backup-and-restore)) is the alternative.

Useful endpoints (from the host itself, or through a tunnel):

- Assistant health: `http://localhost:8000/healthz`
- Metrics: `http://localhost:8000/metrics`
- Prometheus: `http://localhost:9090`
- Grafana: `http://localhost:3000` — pre-provisioned with the Prometheus datasource and a "Gmail Assistant" dashboard (processed emails, triage latency, JEV fallback rate, LLM tokens and cost, feedback by route and verdict, Telegram replies and Telegram listener health). Login `admin` / `GRAFANA_ADMIN_PASSWORD` (defaults to `admin` if unset — set it in `.env`; mandatory if you ever change `BIND_ADDRESS`).

## CI/CD

- `.github/workflows/ci.yml`: lint (ruff) + tests (pytest) with a coverage floor (`fail_under` in `pyproject.toml`, to be raised when coverage rises and never lowered) + Docker build sanity check on every PR and push to `prod`.
- `.github/dependabot.yml`: weekly pull requests for Python dependencies (test and lint tools grouped), monthly for GitHub Actions, the Docker base image (patch versions of Python only) and the pinned Prometheus and Grafana images. Their commits are prefixed `chore`, so a bump alone cuts no release.
- `.github/workflows/release.yml`: on push to `prod`, runs [python-semantic-release](https://python-semantic-release.readthedocs.io/) against [Conventional Commits](https://www.conventionalcommits.org/) to bump `pyproject.toml`, update `CHANGELOG.md`, tag (`vX.Y.Z`) and cut a GitHub Release; on a new release it builds and pushes the Docker image to `ghcr.io/<repo>:<version>` and `:latest`.

Commit convention (drives the version bump):

- `fix: ...` → patch
- `feat: ...` → minor
- `feat!: ...` / `BREAKING CHANGE:` footer → major
