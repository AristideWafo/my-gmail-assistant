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
- ✅ **[Priorité 6]** Boucle de feedback : boutons `[Valider]/[Faux-Urgent]/[Faux-Spam]` sur les alertes Telegram (`src/gateways/alerts.py`, `callback_data` `fb:<v|u|s>:<gmail_id>`), reçus par un bot en long polling (`src/gateways/telegram_bot.py`, thread dédié, offset persisté, livraison at-least-once ; seuls le chat `TELEGRAM_CHAT_ID` **et** un utilisateur autorisé sont acceptés — en groupe, `TELEGRAM_ALLOWED_USER_IDS` est obligatoire, sinon l'écoute ne démarre pas ; rejets comptés dans `telegram_inbound_rejected_total{reason}`, santé via `telegram_poll_errors_total` et `telegram_last_poll_timestamp_seconds`) et traités par `src/interactions/handlers.py`. Chaque décision de triage et chaque verdict sont stockés en SQLite (`src/storage/decision_store.py`, `DB_PATH`) et comptés (`feedback_total{verdict}`) pour mesurer la précision réelle. Few-shot JEV livré (`src/triage/few_shot.py` → `state.examples`, uniquement domaine de l'expéditeur + objet tronqué à 100 caractères, jamais le corps, pour limiter l'injection de prompt persistante) mais **désactivé par défaut** (`JEV_FEW_SHOT_ENABLED=false`) : il reste à vérifier en direct que JEV accepte `state.examples` (impossible sans clé API) et à accumuler assez de verdicts avant de l'activer. Discord reste sortant uniquement
- ✅ Interaction retour utilisateur (Telegram) : répondre à une alerte crée un brouillon Gmail dans le fil d'origine (`In-Reply-To`/`References`), prévisualisé avec `[Envoyer]/[Annuler]` ; l'envoi n'a lieu qu'après confirmation explicite sur l'aperçu qui l'a proposé (`bot_draft:<id>`, un id de brouillon forgé est refusé) et au plus une fois (état réservé avant l'appel Gmail ; en cas d'échec l'envoi est incertain, vérifier les Envoyés avant de renvoyer). Métrique `chat_replies_total{status}`

## Phase 4 — Observabilité et gateways

- ✅ Prometheus + Grafana conteneurisés, volumes persistants
- ✅ Compteurs Prometheus (`processed_emails_total`, `triage_latency_seconds`, `llm_tokens_total`)
- ✅ Dashboards Grafana provisionnés (`grafana/provisioning/`, datasource Prometheus + dashboard "Gmail Assistant" auto-chargés)
- ✅ Tracing coût réel LLM — `GeminiClient` lit `response.usage_metadata` (tokens réels prompt/completion par appel) et calcule le coût USD via une table de prix vérifiée (`PRICING_PER_MILLION_TOKENS`, source ai.google.dev) ; modèle non tarifé → tokens comptés, coût omis + warning loggé une fois. Pas d'OpenLLMetry/LangSmith (pas nécessaire : les deux SDK ne trackent qu'un provider et le compte déjà via `usage_metadata`)
- ✅ Entrant Telegram via long polling (`TELEGRAM_INBOUND_ENABLED`), pas de webhook : aucun endpoint public à exposer
- ⬜ Entrant Discord : les boutons Discord exigent un endpoint HTTPS public (Interactions) signé — hors scope tant que le VPS n'expose rien publiquement

## Risques ouverts (indépendants des phases)

- Architecture ports & adapters : le cœur ne dépend que de `src/ports` ; `src/bootstrap.py` choisit les adaptateurs via `MAIL_PROVIDER`, `CLASSIFIER`, `LLM_PROVIDER`, `ALERT_CHANNELS`, `CHAT_INBOX`, `STORE_BACKEND` (valeur inconnue = arrêt au démarrage). Une seule implémentation par port hors triage/canaux pour l'instant
- Pas de limite mémoire Docker sur `prometheus`/`grafana`, seul `assistant` est borné — risque swap/crash sur VPS 4 Go
- Un seul poller par token Telegram : deux instances avec `TELEGRAM_INBOUND_ENABLED=true` (ex. local + VPS) se volent les mises à jour (409) — n'activer le flag que sur une instance
- Le fichier SQLite (`/data/assistant.db` + `-wal`/`-shm`) est désormais un état à sauvegarder : sa perte efface l'historique des verdicts et la dédup des alertes (risque de ré-alerte d'un mail non commité). Créé en `0600` (dossier `0700` s'il est créé par l'app) ; préférer le volume nommé à un bind mount, dont propriétaire et droits restent à gérer côté hôte
- Rétention 90 jours (purge au démarrage puis quotidienne) : décisions sans verdict, marqueurs d'alerte et état de dédup réponse/envoi ; verdicts et offset Telegram conservés. Dédup d'alerte sur 24 h : un mail remis en non-lu après 24 h est retraité
- Few-shot : même réduit au domaine et à l'objet, un objet reste du texte choisi par l'expéditeur, rejoué dans chaque classification — ne l'activer qu'après vérification
- Few-shot JEV non vérifié en direct : si l'API ignore ou rejette `state.examples`, l'activer n'apportera rien ou fera basculer le triage sur le fallback heuristique (`jev_fallback_total`) — tester avec une vraie clé avant d'activer

---

## Prochaine étape

Priorités 1 à 6 traitées. Phase 3 terminée côté Telegram (feedback + réponse chat→brouillon→envoi confirmé). Restent :

- Activer `TELEGRAM_INBOUND_ENABLED` sur le VPS, collecter des verdicts quelques semaines et suivre la précision (`feedback_total` par verdict)
- Vérifier en direct que JEV accepte `state.examples`, puis activer `JEV_FEW_SHOT_ENABLED` et comparer le taux de faux-urgents avant/après
- Mettre en place une sauvegarde régulière du volume `assistant-data`
