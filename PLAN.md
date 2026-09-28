# Plan de développement — Agent IA de tri d'e-mails

Ce document suit l'architecture cible (voir `README.md` pour l'usage) et l'état réel du code, phase par phase. Statuts : ✅ fait · 🟡 partiel · ⬜ à faire. Coût = effort de mise en œuvre estimé (trivial / faible / moyen / élevé), pas un coût monétaire — sert à prioriser pour minimiser le coût de production futur.

## Phase 1 — Setup de base et ingestion

- ✅ OAuth2 Gmail (`src/gmail/client.py`), env-driven via `.env`
- ✅ `fetch_history` avec pagination + backoff exponentiel sur 429
- ✅ Dockerisation, `docker-compose.yml`, `install.sh`
- ✅ `fetch_unread` partage désormais le même backoff exponentiel (`_execute_with_backoff`)
- ✅ **[Priorité 1]** `EmailMessage.body` décodé du base64 Gmail (`src/gmail/text_cleaning.decode_body`), plus fallback sur la part `text/html` si aucune part `text/plain` n'existe
- ✅ **[Priorité 2]** Pré-traitement du contenu avant triage (`src/gmail/text_cleaning.py`) : strip HTML/entités, coupe au séparateur de signature RFC 3676, troncature à 1000 mots, extraction du domaine expéditeur (`EmailMessage.sender_domain`)

## Phase 2 — Intégration JEV et triage logique

- ✅ `DecisionEngineClient` : appelle JEV via API HTTP + fallback heuristique si indisponible
- ✅ `EmailWorkflow` (LangGraph) : graphe `classify → (llm | archive | label)`
- 🟡 JEV appelé en HTTP externe, pas hébergé localement comme prévu au plan initial (accepté tel quel, hors scope actuel)
- ✅ Échec JEV loggé (`logger.warning`) et compté (`jev_fallback_total`)
- ✅ **[Priorité 3]** Route renommée `archive` → `reject` ; catégories `spam`/`newsletter` désormais acceptées (`_normalize_category`) et routées vers `reject` peu importe l'urgence ; fallback heuristique détecte les newsletters via sender (`no-reply`/`newsletter`/`unsubscribe`)
- ⬜ **[Priorité 4 · coût moyen-élevé, conditionnel]** Taxonomie JEV enrichie (`offre_emploi / mise_en_relation / newsletter / notification_systeme / personnel / spam` au lieu de `offer/general/urgent`) — **préalable obligatoire : vérifier que l'API JEV externe accepte ce schéma avant de coder quoi que ce soit** ; le fallback heuristique par mots-clés restera peu fiable sur spam/newsletter (mieux détectés via header `List-Unsubscribe` que via le texte)

## Phase 3 — LLM et boucle d'action

- ✅ `GeminiClient` : summary + draft reply
- ✅ Routage 3 voies : `archive` (low+general), `label` (medium/offer), `llm` (high/low confidence)
- ✅ `archive_message`, `label_message`/`ensure_label`, `create_draft` (bug d'encodage hex→base64url corrigé) appelés depuis `main.py::process_email`
- ⬜ **[Priorité 5 · coût moyen]** Extraction d'entités structurées par Gemini pour les offres d'emploi (poste, stack, salaire, entreprise, prochaine étape) — isolé à la branche déjà escaladée (`llm`), surcoût Gemini marginal car limité aux mails à haute valeur
- ⬜ **[Priorité 6 · coût élevé, à différer]** Boucle d'auto-apprentissage (boutons `[Valider]/[Faux-Urgent]/[Faux-Spam]` sur Telegram/Discord, stockage des corrections, few-shot injecté dans le prompt JEV) — nécessite : (a) un store persistant inexistant aujourd'hui (SQLite/JSON), (b) un récepteur de callbacks entrants (bot Telegram polling/webhook, endpoint Discord Interactions) — actuellement `AlertGateway` n'envoie qu'en sortant. **Préalable obligatoire : confirmer que JEV accepte l'injection de contexte few-shot** (si API à schéma fixe, cette brique entière est à revoir) — ne pas démarrer avant d'avoir mesuré la précision du routage actuel en usage réel
- ⬜ Interaction retour utilisateur (répondre depuis Telegram/Discord → envoi mail) — dépend de la même brique callback entrant que la boucle d'apprentissage ci-dessus, à traiter ensemble

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

Backlog priorisé pour minimiser le coût de production futur (du moins cher/plus sûr au plus cher/plus risqué) :

1. Fix décodage base64 du corps (Phase 1) — bug, gratuit
2. Pré-traitement du contenu (Phase 1) — faible coût, gain direct sur tokens/bruit
3. Split route archive rejet/standard (Phase 2) — trivial, extension du code existant
4. Taxonomie JEV enrichie (Phase 2) — **sous réserve** que JEV supporte le schéma étendu
5. Extraction d'entités Gemini pour offres (Phase 3) — coût maîtrisé, isolé à la branche à haute valeur
6. Boucle d'auto-apprentissage + interaction chat→email (Phase 3) — reporté, coût d'infra le plus élevé (bot + stockage + endpoint public), à ne démarrer qu'après mesure de la précision réelle et confirmation du support few-shot par JEV

Phase 4 (dashboards Grafana provisionnés, tracing coût LLM réel) reste en parallèle, indépendante de ce backlog.
