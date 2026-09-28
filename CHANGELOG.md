# CHANGELOG


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
