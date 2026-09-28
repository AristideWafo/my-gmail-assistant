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
- ✅ Règles déterministes par expéditeur (`src/triage/rules.py`) avant JEV ; catégories `alerte_emploi` (label, jamais archivé), `promotion` (reject) et `alerte_technique` ; définition stricte de l'urgence et dates (`received_at`, `today`) envoyées à JEV
- ✅ **[Priorité 4]** Taxonomie JEV enrichie (`offre_emploi / mise_en_relation / newsletter / notification_systeme / personnel / spam`) — préalable levé, testé en direct contre l'API JEV. `urgent` disparaît (doublonnait l'axe `urgency`), `general` devient `personnel`. Règle d'archivage silencieux (`reject`) à basse urgence recentrée sur `notification_systeme` uniquement : `personnel` à basse urgence est désormais toujours `label`/`llm`, jamais archivé sans intervention. Le fallback heuristique reste limité à `newsletter`/`offre_emploi`/`personnel` (ne produit jamais `mise_en_relation`/`notification_systeme`, pas détectables fiablement par mots-clés)

## Phase 3 — LLM et boucle d'action

- ✅ `GeminiClient` : summary + draft reply
- ✅ Routage 3 voies : `archive` (low+general), `label` (medium/offer), `llm` (high/low confidence)
- ✅ `archive_message`, `label_message`/`ensure_label`, `create_draft` (bug d'encodage hex→base64url corrigé) appelés depuis `main.py::process_email`
- ✅ **[Priorité 5]** `GeminiClient.extract_job_entities` (poste/entreprise/stack/salaire/prochaine_etape en JSON strict, fallback `{}` sur parsing invalide) appelé depuis `_llm_node` uniquement quand `category == "offer"` ; entités ajoutées au texte d'alerte dans `main.py`
- ⬜ **[Priorité 6 · coût élevé, à différer]** Boucle d'auto-apprentissage (boutons `[Valider]/[Faux-Urgent]/[Faux-Spam]` sur Telegram/Discord, stockage des corrections, few-shot injecté dans le prompt JEV) — nécessite : (a) un store persistant inexistant aujourd'hui (SQLite/JSON), (b) un récepteur de callbacks entrants (bot Telegram polling/webhook, endpoint Discord Interactions) — actuellement `AlertGateway` n'envoie qu'en sortant. **Préalable obligatoire : confirmer que JEV accepte l'injection de contexte few-shot** (si API à schéma fixe, cette brique entière est à revoir) — ne pas démarrer avant d'avoir mesuré la précision du routage actuel en usage réel
- ⬜ Interaction retour utilisateur (répondre depuis Telegram/Discord → envoi mail) — dépend de la même brique callback entrant que la boucle d'apprentissage ci-dessus, à traiter ensemble

## Phase 4 — Observabilité et gateways

- ✅ Prometheus + Grafana conteneurisés, volumes persistants
- ✅ Compteurs Prometheus (`processed_emails_total`, `triage_latency_seconds`, `llm_tokens_total`)
- ✅ Dashboards Grafana provisionnés (`grafana/provisioning/`, datasource Prometheus + dashboard "Gmail Assistant" auto-chargés)
- ✅ Tracing coût réel LLM — `GeminiClient` lit `response.usage_metadata` (tokens réels prompt/completion par appel) et calcule le coût USD via une table de prix vérifiée (`PRICING_PER_MILLION_TOKENS`, source ai.google.dev) ; modèle non tarifé → tokens comptés, coût omis + warning loggé une fois. Pas d'OpenLLMetry/LangSmith (pas nécessaire : les deux SDK ne trackent qu'un provider et le compte déjà via `usage_metadata`)
- ⬜ Webhooks entrants Discord/Telegram pour interaction temps réel

## Risques ouverts (indépendants des phases)

- Pas de limite mémoire Docker sur `prometheus`/`grafana`, seul `assistant` est borné — risque swap/crash sur VPS 4 Go
- Déduplication par `message.id` en mémoire seulement (24 h, `ExpiringSet`) : un redémarrage entre l'alerte et le label peut ré-alerter un mail (point de commit = label qui retire UNREAD)
- Pas de persistance (DB/state store) pour le feedback ou l'historique de décisions

---

## Prochaine étape

Priorités 1, 2, 3, 5 traitées (stack de 3 PR : #6 base64+pré-traitement, #7 routage reject, #8 extraction entités offres). Restent :

4. Taxonomie JEV enrichie (Phase 2) — **sous réserve** que JEV supporte le schéma étendu
6. Boucle d'auto-apprentissage + interaction chat→email (Phase 3) — reporté, coût d'infra le plus élevé (bot + stockage + endpoint public), à ne démarrer qu'après mesure de la précision réelle et confirmation du support few-shot par JEV

Phase 4 (dashboards Grafana provisionnés, tracing coût LLM réel) reste en parallèle, indépendante de ce backlog.
