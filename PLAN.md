# Plan de développement — Agent IA de tri d'e-mails

Ce document suit l'architecture cible (voir `README.md` pour l'usage) et l'état réel du code, phase par phase. Statuts : ✅ fait · 🟡 partiel · ⬜ à faire.

## Phase 1 — Setup de base et ingestion

- ✅ OAuth2 Gmail (`src/gmail/client.py`), env-driven via `.env`
- ✅ `fetch_history` avec pagination + backoff exponentiel sur 429
- ✅ Dockerisation, `docker-compose.yml`, `install.sh`
- ✅ `fetch_unread` partage désormais le même backoff exponentiel (`_execute_with_backoff`)

## Phase 2 — Intégration JEV et triage logique

- ✅ `DecisionEngineClient` : appelle JEV via API HTTP + fallback heuristique si indisponible
- ✅ `EmailWorkflow` (LangGraph) : graphe `classify → (llm | archive | label)`
- 🟡 JEV appelé en HTTP externe, pas hébergé localement comme prévu au plan initial (accepté tel quel, hors scope actuel)
- ✅ Échec JEV loggé (`logger.warning`) et compté (`jev_fallback_total`)

## Phase 3 — LLM et boucle d'action

- ✅ `GeminiClient` : summary + draft reply
- ✅ Routage 3 voies : `archive` (low+general), `label` (medium/offer), `llm` (high/low confidence)
- ✅ `archive_message`, `label_message`/`ensure_label`, `create_draft` (bug d'encodage hex→base64url corrigé) appelés depuis `main.py::process_email`
- ⬜ Boucle d'auto-apprentissage (feedback sur les prédictions JEV) — absente, pas de stockage d'état
- ⬜ Interaction retour utilisateur (répondre depuis Telegram/Discord → envoi mail) — absente, alertes one-way uniquement

## Phase 4 — Observabilité et gateways

- ✅ Prometheus + Grafana conteneurisés, volumes persistants
- ✅ Compteurs Prometheus (`processed_emails_total`, `triage_latency_seconds`, `llm_tokens_total`)
- ⬜ Dashboards Grafana provisionnés (JSON dashboards, datasource auto-config)
- ⬜ Tracing coût réel LLM (OpenLLMetry / LangSmith) — `mark_tokens` compte des mots, pas des tokens/coût réels
- ⬜ Webhooks entrants Discord/Telegram pour interaction temps réel

## Risques ouverts (indépendants des phases)

- Pas de limite mémoire Docker sur `prometheus`/`grafana`, seul `assistant` est borné — risque swap/crash sur VPS 4 Go
- Pas de déduplication par `message.id` — un crash entre triage et action peut retraiter un e-mail en double
- Pas de persistance (DB/state store) pour le feedback ou l'historique de décisions

---

## Prochaine étape

Phase 3 (routage + actions Gmail réelles) traitée. Prochaine itération candidate : boucle d'auto-apprentissage JEV et/ou interaction retour utilisateur (répondre depuis Telegram/Discord → envoi mail), ou dashboards Grafana + tracing coût LLM réel (Phase 4).
