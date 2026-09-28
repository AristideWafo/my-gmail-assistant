# CHANGELOG


## v0.3.0 (2026-09-28)

### Features

- Add script to generate the Gmail refresh token
  ([#13](https://github.com/AristideWafo/my-gmail-assistant/pull/13),
  [`ed7dcea`](https://github.com/AristideWafo/my-gmail-assistant/commit/ed7dcea64059f40caa1ba1459faeed64fdcfa319))

The app only reads GMAIL_REFRESH_TOKEN and nothing produced it, so a headless server could not be
  set up. python -m src.gmail.token_setup runs the OAuth flow on a workstation with a browser and
  prints the .env lines. Also ignore client_secret*.json in git and docker contexts.

Co-authored-by: Claude Sonnet 5 <noreply@anthropic.com>


## v0.2.2 (2026-09-28)

### Bug Fixes

- Call TypeSafe JEV API with auth and real system-one contract
  ([#12](https://github.com/AristideWafo/my-gmail-assistant/pull/12),
  [`f317867`](https://github.com/AristideWafo/my-gmail-assistant/commit/f3178676e431cf92d5eae49b927abda5e607309f))

Previous client posted a custom payload with no API key and expected a made-up response shape, so
  failures silently hit the heuristic fallback. Now sends Bearer JEV_API_KEY to /v1/systemone with
  urgency/category choice questions, skips the API when no key is set, and falls back on malformed
  responses.

Co-authored-by: Claude Sonnet 5 <noreply@anthropic.com>


## v0.2.1 (2026-09-28)

### Bug Fixes

- Remove escaped quotes that broke the lowercase GHCR image tag
  ([#11](https://github.com/AristideWafo/my-gmail-assistant/pull/11),
  [`37558ac`](https://github.com/AristideWafo/my-gmail-assistant/commit/37558ac42791e70ca16a09f29cb8ad5b5a22a40c))

The `\"$REPOSITORY\"` escapes ended up literally in the tag
  (ghcr.io/"aristidewafo/my-gmail-assistant":0.2.0), causing buildx to fail with "invalid reference
  format".

Co-authored-by: Claude Sonnet 5 <noreply@anthropic.com>


## v0.2.0 (2026-09-28)

### Features

- Extract structured job entities via Gemini for offer emails (P5)
  ([#8](https://github.com/AristideWafo/my-gmail-assistant/pull/8),
  [`1989710`](https://github.com/AristideWafo/my-gmail-assistant/commit/19897106d5bb7dcdc0ab922792fe8a2f5c61dd19))

- GeminiClient.extract_job_entities: strict-JSON prompt for
  poste/entreprise/stack/salaire/prochaine_etape, returns {} on parse failure or when Gemini isn't
  configured - called from EmailWorkflow._llm_node only when triage.category == "offer" - no extra
  Gemini call outside the already-escalated path, so no cost impact on non-offer emails - main.py
  appends the extracted entities to the alert text sent to Telegram/Discord when present

Co-authored-by: Claude Sonnet 5 <noreply@anthropic.com>

- Route spam/newsletter to silent reject regardless of urgency (P3)
  ([#7](https://github.com/AristideWafo/my-gmail-assistant/pull/7),
  [`5c85edc`](https://github.com/AristideWafo/my-gmail-assistant/commit/5c85edc816ad4840dc3332156f581873bf6c1d61))

* chore(release): 0.1.1 [skip ci]

* feat: route spam/newsletter to silent reject regardless of urgency (P3)

- DecisionEngineClient accepts spam/newsletter categories instead of collapsing everything outside
  offer/general/urgent into "general" - the signal was being discarded even when JEV already returns
  it - fallback heuristic detects newsletters via sender (no-reply/newsletter) or "unsubscribe" in
  the body, reusing EmailMessage.sender_domain - workflow route "archive" renamed to "reject" to
  match the funnel strategy's vocabulary (Branch A); spam/newsletter now route there regardless of
  the urgency JEV reports, on top of the existing low-urgency+general case

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>

---------

Co-authored-by: semantic-release <semantic-release>

Co-authored-by: Claude Sonnet 5 <noreply@anthropic.com>


## v0.1.1 (2026-09-28)

### Bug Fixes

- Decode Gmail body from base64 and pre-clean content (P1+P2)
  ([#6](https://github.com/AristideWafo/my-gmail-assistant/pull/6),
  [`41b9bb2`](https://github.com/AristideWafo/my-gmail-assistant/commit/41b9bb25cafb5a76899ee2c57ada8adf2c26a1e9))

- new src/gmail/text_cleaning.py: pure stdlib helpers (decode_body, strip_html, strip_signature,
  truncate_words, extract_domain, clean_body) - GmailClient._parse_message now decodes the base64
  body Gmail returns instead of storing it raw (JEV/Gemini were receiving unusable base64) - falls
  back to the text/html part when no text/plain part exists, so HTML-only emails no longer lose
  their body entirely - body is stripped of HTML tags/signatures and truncated to 1000 words before
  it ever reaches JEV/Gemini, cutting noise and token usage - EmailMessage gains sender_domain,
  extracted once at parse time for reuse by future routing heuristics

Co-authored-by: Claude Sonnet 5 <noreply@anthropic.com>


## v0.1.0 (2026-09-28)

### Features

- Add install script, docker hardening, and semantic-release CI/CD
  ([`5f16c7d`](https://github.com/AristideWafo/my-gmail-assistant/commit/5f16c7de0cca9c9440126f6857dcd0185bdfa943))

- install.sh for local venv or docker compose bootstrap - docker-compose: healthcheck, restart
  policy, persistent volumes - .dockerignore for leaner build context - GitHub Actions: lint+test on
  PRs, semantic-release + GHCR image publish on prod - fix: langgraph node/state-key collision and
  end-node write breaking the workflow graph - fix: ruff RUF012 mutable class default in GmailClient

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>

- Gmail routing actions ([#4](https://github.com/AristideWafo/my-gmail-assistant/pull/4),
  [`99103a9`](https://github.com/AristideWafo/my-gmail-assistant/commit/99103a9bdb91d06a368b544e466f830462e5b252))

* feat: real Gmail actions on triage routing (archive/label/draft)

- PLAN.md tracks phase-by-phase status vs the original architecture plan - workflow: 3-way routing
  (archive low+general, label medium/offer, llm high/low-confidence) instead of llm-or-nothing -
  gmail client: archive_message, ensure_label/label_message, shared exponential backoff for
  fetch_unread + fetch_history - fix: create_draft encoded the MIME payload as hex instead of
  base64url, which Gmail's API requires for `raw` - triage engine: JEV unreachable now logs and
  increments jev_fallback_total instead of failing silently - tests: gmail client actions/backoff,
  JEV fallback metric, updated workflow routing tests

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>

* docs: prioritize funnel-strategy adjustments in PLAN.md by cost/benefit

Integrates the pre-processing, taxonomy, entity-extraction, and feedback-loop ideas from the funnel
  strategy discussion into Phase 1-3, each tagged with effort and ordered as a backlog
  (cheapest/safest first) to minimize future production cost.

* fix: correct semantic-release config for v9

build_command must be a string, not a bool; move changelog_file to changelog.default_templates to
  silence the deprecation warning.

---------

Co-authored-by: Claude Sonnet 5 <noreply@anthropic.com>

- Real Gmail actions on triage routing (archive/label/draft)
  ([#3](https://github.com/AristideWafo/my-gmail-assistant/pull/3),
  [`9b1dd37`](https://github.com/AristideWafo/my-gmail-assistant/commit/9b1dd378a486e042e438eb3069918ccceccb3e55))

* feat: real Gmail actions on triage routing (archive/label/draft)

- PLAN.md tracks phase-by-phase status vs the original architecture plan - workflow: 3-way routing
  (archive low+general, label medium/offer, llm high/low-confidence) instead of llm-or-nothing -
  gmail client: archive_message, ensure_label/label_message, shared exponential backoff for
  fetch_unread + fetch_history - fix: create_draft encoded the MIME payload as hex instead of
  base64url, which Gmail's API requires for `raw` - triage engine: JEV unreachable now logs and
  increments jev_fallback_total instead of failing silently - tests: gmail client actions/backoff,
  JEV fallback metric, updated workflow routing tests

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>

* docs: prioritize funnel-strategy adjustments in PLAN.md by cost/benefit

Integrates the pre-processing, taxonomy, entity-extraction, and feedback-loop ideas from the funnel
  strategy discussion into Phase 1-3, each tagged with effort and ordered as a backlog
  (cheapest/safest first) to minimize future production cost.

---------

Co-authored-by: Claude Sonnet 5 <noreply@anthropic.com>
