# Plan de développement — de l'agent de tri Gmail à l'assistant personnel

Ce document décrit le scope cible et l'état réel du code, phase par phase (voir `README.md` pour l'usage). Statuts : ✅ fait · 🟡 partiel · ⬜ à faire. Coût = effort de mise en œuvre estimé (trivial / faible / moyen / élevé), pas un coût monétaire.

Hors scope : carrière / CV / scoring d'offres d'emploi. Les catégories `offre_emploi` et `alerte_emploi` restent de simples catégories de tri.

## Socle livré (v0.7.0)

Détail dans `CHANGELOG.md` et l'historique git de ce fichier.

- ✅ Ingestion Gmail par polling (`is:unread in:inbox newer_than:3d`, 60 s), nettoyage du corps, backoff sur 429
- ✅ Triage en deux étages : règles par expéditeur (`src/triage/rules.py`) → JEV (repli heuristique) → Gemini uniquement sur la route `llm`
- ✅ Routes `reject` (archivage) / `label` / `llm` (alerte + résumé + brouillon), seuil de confiance basse
- ✅ Alertes Telegram + Discord ; boutons `[Valider]/[Faux-Urgent]/[Faux-Spam]` sur les alertes urgentes
- ✅ Réponse Telegram → brouillon Gmail → `[Envoyer]/[Annuler]`, envoi au plus une fois, callback forgé refusé
- ✅ SQLite (`decisions`, `feedback`, `alerted`, `kv_state`), rétention 90 jours
- ✅ Ports & adapters (`src/ports`, `src/bootstrap.py`), Prometheus + Grafana, watchdog, CI/release
- 🟡 Few-shot JEV codé (`src/triage/few_shot.py`) mais jamais activé ni vérifié en direct

## Ce qui change par rapport au scope proposé

Le scope proposé a été confronté au code. Les écarts retenus :

| Sujet | Constat | Décision |
| --- | --- | --- |
| `/review` avec les boutons existants | `Faux-Urgent` n'a aucun sens sur un mail `low`/`medium`. L'erreur qui compte là est l'inverse : un mail important classé bas ou archivé en silence | Nouveaux verdicts `missed_urgent` et `wrong_archive`, boutons adaptés à la route |
| Commande Telegram | Le bot ne comprend que les callbacks et les réponses à une alerte (`_parse_reply` exige `reply_to_message`). Aucune commande n'existe | Routeur de commandes construit en Phase 0, réutilisé par toutes les phases suivantes |
| Comparaison few-shot avant/après sur 1 semaine | Volume personnel trop faible, et le mix de mails change d'une semaine à l'autre : le résultat ne prouve rien | Rejeu hors ligne des mails déjà notés, avec et sans few-shot, sur le même jeu |
| Push Gmail (Pub/Sub) | Le critère « < 1 min » est déjà tenu par le polling à 60 s. Le push exige soit un endpoint HTTPS public (exclu sur ce VPS), soit un abonnement pull avec compte de service, plus un renouvellement de `watch()` tous les 7 jours et un polling de secours | Non retenu (voir Backlog) |
| `VACUUM` périodique | Base de quelques milliers de lignes avec purge à 90 jours : les pages libérées sont réutilisées, aucun gain | Remplacé par ce qui manque vraiment : migrations de schéma et sauvegarde |
| Métrique de dérive de confiance | `triage_confidence` et son panneau Grafana existent déjà, mais mélangent JEV, heuristique (valeurs fixes) et règles (1.0) | Ajout d'un label `source`, pas de nouvelle métrique |
| Calendar « via MCP » | L'app utilise déjà `google-api-python-client` et un OAuth Google. MCP ajoute un serveur, un client et un transport pour un seul consommateur | Port `CalendarProvider` + adaptateur Google direct ; un adaptateur MCP reste possible derrière le même port |
| ReAct pour Calendar, récap, voix | Chercher un créneau libre est un calcul d'intervalles ; « nombre de tâches variable » est une boucle. Un agent à outils d'écriture nourri de texte choisi par l'expéditeur est une surface d'injection | Pipelines déterministes + appels LLM structurés uniques. Boucle d'agent réservée aux questions en langage naturel, en lecture seule |
| États `waiting_for_me` / `waiting_for_them` | L'assistant ne voit jamais les mails envoyés ni les mails déjà lus, et aucune logique de thread n'existe (seul `thread_id` est stocké) | Table `threads` + lecture des threads côté Gmail ; question JEV `needs_reply` dans l'appel existant |
| Récap « à faire / fait / pas fait » | Aucune notion de tâche dans le système | Modèle `action_items` explicite avec une définition de « fait » |
| Récap soir + hebdo + vue du matin + coaching | Quatre messages programmés distincts, générés par quatre mécanismes | Un seul moteur de digest ; le coaching devient une section du récap hebdo |
| Phase 7 | Cinq fonctionnalités sans lien entre elles | Réparties : vue du matin → digests, recherche naturelle → interface langage naturel, le reste → phase proactive |
| Calendar bloqué derrière le few-shot | La fiabilisation du triage est rythmée par l'accumulation de verdicts (des semaines), pas par l'effort | Piste parallèle : elle ne bloque rien |
| Ordre des phases | La demande initiale (bilan de ce qui est fait / pas fait) arrivait en Phase 5, derrière Calendar | Récap v1 livré sans agenda ; la reprogrammation s'ajoute quand Calendar existe |

## Invariants (valables pour toutes les phases)

1. **Aucune écriture sans confirmation explicite** : envoi de mail, création ou modification d'événement. La confirmation est liée au message Telegram qui l'a proposée, et exécutée au plus une fois (même garde que `_offered_here` aujourd'hui).
2. **Le LLM ne tient jamais d'outil d'écriture.** Il produit une proposition typée ; le code l'exécute après confirmation. Le contenu d'un mail est du texte choisi par l'expéditeur.
3. **Chaque fonctionnalité derrière un flag d'env, désactivé par défaut**, comme `JEV_FEW_SHOT_ENABLED`.
4. **Tout composant externe derrière un port** (`src/ports`), choisi dans `src/bootstrap.py`.
5. **Budget de notifications** : heures calmes et plafond quotidien de messages proactifs ; seules les alertes urgentes y échappent.
6. **Fuseau explicite** (`TIMEZONE`) pour tout ce qui est horaire. Le stockage reste en UTC.

## Dépendances

```text
S1 ──► P0 ──► P2 ──► P3 ──► P4 ──► P6
        │      ▲      │      │
        │      S2     └──────┴──► P5
        └──► P1 ──► L1 ──► L2, L3, L4   (piste parallèle, rythmée par les données)
```

S1 et S2 sont les deux lots de la stabilisation ci-dessous. S3 est une règle de passage appliquée entre chaque phase. L1 à L4 forment la Phase 1bis : L2, L3 et L4 ne démarrent qu'après la validation de L1.

---

## Stabilisation

**Pourquoi** : les phases suivantes transforment un trieur en assistant sur lequel tu comptes (relances, récaps, agenda). Une panne silencieuse devient alors un vrai coût. Les points ci-dessous viennent d'une lecture du code, pas d'incidents observés.

**Coût** : moyen au total ; S1 est faible.

### S1 — Avant la Phase 0 (bloquant)

La Phase 0 introduit les migrations de schéma : la sauvegarde doit exister avant.

- ✅ Sauvegarde quotidienne vérifiée (`BACKUP_DIR`, `BACKUP_KEEP`, `src/maintenance/backup.py`) sur un volume distinct, avec rotation ; métriques `backup_last_success_timestamp_seconds` et `backup_failures_total`
- 🟡 Copie hors de l'hôte : le volume de sauvegarde est sur le même disque que la base ; commande documentée, planification à mettre en place sur le VPS
- 🟡 Restauration : procédure écrite dans le README et couverte par un test automatisé (copie → réouverture → verdicts présents). Reste à l'exécuter une fois sur le VPS
- ✅ Alerte de panne silencieuse (`src/health/outage.py`) : message Telegram quand la relève échoue depuis `POLL_FAILURE_ALERT_MINUTES` (10 par défaut), réessayé à chaque cycle jusqu'à livraison, puis message de rétablissement avec la durée. Le watchdog reste volontairement muet sur ce cas ; compteur `poll_failures_total`
- ✅ « Aucun mail » distingué de « échec » : `_execute_with_backoff` lève l'erreur une fois les tentatives sur 429 épuisées, au lieu de renvoyer une liste vide comptée comme un poll réussi

### S2 — Avant la Phase 2

- ⬜ `sync_history_once` sans garde par mail : un seul mail en erreur interrompt le démarrage. Reprendre la garde de `poll_once`
- ⬜ Délais d'attente explicites sur les appels Gemini (à vérifier : aucun n'apparaît dans `src/llm/gemini.py`). Un appel bloqué gèle le tri jusqu'au watchdog
- ⬜ Alertes sur les métriques existantes, envoyées sur Telegram : taux de `jev_fallback_total`, `emails_skipped_total`, listener Telegram muet, `llm_errors_total`. Aujourd'hui les tableaux existent mais personne n'est prévenu
- ⬜ Plafond de dépense LLM quotidien (`llm_cost_usd_total`) avec alerte au dépassement ; nécessaire avant d'ajouter des appels en Phases 2 à 6
- ⬜ Exposition réseau : les ports 8000, 9090 et 3000 sont publiés sur toutes les interfaces, Prometheus et `/metrics` sans authentification, Grafana en `admin`/`admin` si `GRAFANA_ADMIN_PASSWORD` est absent. Lier à `127.0.0.1` ou confirmer le pare-feu du VPS
- ⬜ Images `prometheus` et `grafana` épinglées à une version au lieu de `latest`
- ⬜ Déploiement : `docker-compose.yml` construit l'image sur place alors que la release publie une image versionnée sur GHCR. Faire tourner l'image publiée, et écrire la procédure de retour à la version précédente
- ⬜ Dépendances de développement déclarées (`requirements-dev.txt`) : la CI installe `ruff` et `pytest` à la main, et le venv local n'a pas `pytest`
- ⬜ Couverture de tests mesurée en CI, avec un seuil qui ne peut que monter
- ⬜ Mises à jour de dépendances automatisées (Dependabot ou équivalent) ; versions actuelles figées depuis longtemps, dont le SDK Gemini
- ⬜ `@app.on_event` (déprécié par FastAPI) remplacé par `lifespan` ; version de l'app lue depuis `pyproject.toml` au lieu de `0.1.0` en dur
- ⬜ Runbook des pannes connues : token Gmail révoqué, 409 Telegram (double poller), quota Gemini, JEV indisponible, base corrompue

### S3 — Règle de passage entre phases (continu)

- Chaque phase arrive derrière un flag désactivé, activé en production après les tests
- Une phase n'est close qu'après 7 jours d'activation sans régression sur le tri existant
- Toute nouvelle table arrive avec sa migration, son test de migration et sa règle de rétention
- Tout nouvel appel externe arrive avec un délai d'attente, une métrique d'erreur et un comportement dégradé défini
- Toute panne en production ajoute un test qui la reproduit et une ligne au runbook

**DoD**
- 14 jours consécutifs sans intervention manuelle
- Chaque panne simulée (token révoqué, JEV coupé, Gemini coupé, listener arrêté) produit un message Telegram en moins de 15 minutes
- Base restaurée depuis une sauvegarde sur une machine vierge, verdicts présents
- Retour à la version précédente exécuté une fois, durée notée ici
- Aucun mail compté dans `emails_skipped_total` sans cause identifiée
- Prometheus et Grafana inaccessibles depuis l'extérieur du VPS, vérifié depuis une autre machine

---

## Phase 0 — Feedback élargi et socle de commandes

**Pourquoi** : seules les alertes urgentes peuvent être notées. Les erreurs coûteuses (mail important étiqueté bas ou archivé) sont invisibles.

**Coût** : moyen.

- ✅ Migrations de schéma versionnées (`PRAGMA user_version`, `src/storage/migrations.py`) : une transaction par étape, copie `assistant.db.pre-v<N>` avant toute montée de version d'une base non vide, base plus récente que le code refusée
- ✅ `CommandEvent` dans `src/domain`, analyse des messages `/commande` dans `src/gateways/telegram_bot.py`, routeur `src/interactions/commands.py` (`/help`, commande inconnue, exécution unique malgré la livraison at-least-once, `chat_commands_total`). Une réponse à une alerte reste toujours du texte de mail, même si elle commence par `/`
- ✅ Colonne `decisions.source` (`rule` / `jev` / `heuristic`), portée par `TriageResult`
- ✅ Verdicts `missed_urgent` (aurait dû alerter) et `wrong_archive` (archivé à tort) ; colonne `feedback.origin` (`alert` / `review`)
- ✅ `/review [n]` (`src/interactions/review.py`) : échantillon stratifié sur 7 jours, jamais déjà noté, hors décisions de règle. Priorité à la route `reject`, puis aux `label` proches du seuil de confiance. `n` = 5 par défaut, 10 max
- ✅ Boutons selon la route : `label` → `[OK] [Urgent raté] [Spam]` ; `reject` → `[OK] [À garder] [Urgent raté]`
- ✅ `/stats` (`src/interactions/stats.py`) : précision et types d'erreur par route et par source, volume de décisions par source sur 30 jours, lus dans SQLite ; `feedback_total{verdict, route}` et panneau Grafana par route
- ✅ `missed_urgent` ajouté à `_VERDICT_CORRECTIONS` (`high`, catégorie inchangée). `wrong_archive` est mesuré mais pas injecté en few-shot : la bonne catégorie n'est pas connue

**DoD**
- `/review` et `/stats` documentés dans le README
- Un verdict posé via `/review` est stocké au même format que ceux des alertes et distingué par `origin`
- Au moins 30 verdicts hors alertes en base, avec le taux de `missed_urgent` et de `wrong_archive` par route — **reste à faire** : dépend de l'usage réel de `/review` une fois déployé
- Les tests du flux de feedback et de réponse existants passent sans modification de leurs assertions

---

## Phase 1 — Fiabilité et coût du triage (piste parallèle)

**Pourquoi** : c'est le levier le moins cher sur les erreurs de classification, mais il se décide sur des données, pas sur une impression.

**Coût** : moyen, étalé dans le temps.

- 🟡 Vérifier en direct que JEV accepte `state.examples` : la commande existe (`python -m src.evaluation check-examples`), reste à l'exécuter avec la vraie clé sur le VPS
- ✅ `MailProvider.fetch_message(id)` : le store ne garde que 300 caractères d'extrait, le rejeu a besoin du mail complet
- ✅ Banc d'évaluation hors ligne (`python -m src.evaluation run`) : rejoue les mails notés sur les variantes `heuristic`, `jev`, `jev+few-shot`, sort le taux d'accord avec les verdicts (global et par verdict) et le nombre d'appels. Sous 100 cas dont 20 corrections, le résultat est marqué non concluant
- ✅ `check-examples` (un appel JEV réel avec `state.examples`) et `candidates` (expéditeurs toujours classés pareil : candidats à une règle)
- ✅ Séparation temporelle obligatoire : exemples few-shot tirés des corrections antérieures à une date T, évaluation sur les verdicts postérieurs. Sans cela le few-shot est évalué sur ses propres exemples
- ⬜ Activation de `JEV_FEW_SHOT_ENABLED` si le banc montre un gain
- ✅ Règles en fichier de config (`TRIAGE_RULES_PATH`, TOML, `config/triage_rules.example.toml`) : expéditeur **et** objet, urgence et catégorie par règle, fichier invalide refusé au démarrage. Candidats tirés du store par `python -m src.evaluation candidates`
- ✅ Liste VIP par adresse exacte, forçant `high` (`source=vip`). Appliquée seulement si l'en-tête `Authentication-Results` de Gmail indique `dmarc=pass` : le champ `From` est falsifiable. Limite connue : si Gmail n'ajoutait aucun en-tête à un message, un en-tête forgé portant son identifiant serait lu ; l'effet se limite à une alerte, jamais à une action
- ✅ Label `source` sur `triage_confidence` ; le panneau Grafana de confiance ne suit plus que `source="jev"`
- ⬜ Décision documentée sur un repli Gemini pour les classifications à basse confiance, avec un seuil lu sur le banc
- ✅ **Regroupement d'incidents** : les répétitions d'une alerte automatisée (même expéditeur, même objet hors hash et numéros, 30 min) mettent à jour l'alerte d'origine (« 🔁 6 occurrences en 25 min · dernière : … ») au lieu d'être ignorées en silence. Regroupement par workflow et non par dépôt : un autre workflow du même dépôt, par exemple un déploiement de production, alerte immédiatement. État en mémoire : un redémarrage ouvre un nouvel incident
- ✅ **Désabonnement proposé** (`UNSUBSCRIBE_PROPOSALS_ENABLED`, désactivé par défaut) : expéditeur archivé `UNSUBSCRIBE_MIN_ARCHIVED` fois en 30 jours sans jamais être gardé → proposition unique `[Se désabonner] [Garder]`. One-click RFC 8058 en HTTPS uniquement, mail authentifié par DMARC, lien conservé côté serveur, confirmation liée au message et exécutée au plus une fois. Le lien étant choisi par l'expéditeur : adresses publiques seulement, connexion à l'adresse vérifiée, aucune redirection suivie. `mailto:` non géré

**Attention** : une règle sur « Run failed » ne doit pas écraser une panne de production, que `URGENCIES` classe `high`. La règle porte donc sur le dépôt, pas sur le seul motif.

**DoD**
- Banc exécutable en une commande, résultat reproductible sur le même jeu
- Conclusion few-shot tirée d'au moins 100 verdicts dont 20 corrections ; en dessous, la décision reste ouverte
- Baisse mesurée des appels JEV sur les expéditeurs couverts par les règles (`decisions.source`)
- Un expéditeur VIP testé de bout en bout, plus un test de `From` falsifié refusé
- Décision repli Gemini écrite dans ce fichier, chiffres à l'appui
- Une rafale réelle d'échecs CI produit une seule alerte, et une panne de production reste alertée immédiatement
- Un désabonnement réel exécuté après confirmation ; aucun sans appui sur le bouton

---

## Phase 1bis — Banc d'essai des questions JEV (piste parallèle)

**Pourquoi** : avec une clé JEV, une hypothèse sur l'état, les questions ou le routage se vérifie en quelques minutes au lieu d'attendre des semaines de verdicts. Une première série (518 appels réels, 50 mails écrits pour l'occasion) a tranché plusieurs points, mais sur des mails de test : il faut le même outil sur les vrais mails notés avant de changer le tri.

**Coût** : faible pour L1, faible à moyen pour L2 à L4.

**Résultats préliminaires** (corpus de test, à confirmer par L1 sur les vrais mails) :

| Hypothèse | Résultat |
| --- | --- |
| Empiler les questions coûte cher | Faux. L'état est compté une seule fois ; une question oui/non ajoute 60 à 120 tokens. 7 questions sur un mail de 1000 mots : +17 % de tokens, latence inchangée (0,25 s) |
| Le routage varie d'un appel à l'autre | Faux. Aucun changement de route sur 50 mails × 3 appels |
| Une question directe « alerter / garder / archiver » fait mieux qu'urgence + catégorie | Non prouvé. 47/50 contre 45/50, mais erreurs plus graves (mail personnel archivé, fausse alerte) |
| Deux questions oui/non suffisent | Non. 46/50, −38 % de tokens, instable sur les arnaques, catégorie perdue |
| Passer « expéditeur automatisé » dans l'état aide | Aucun effet |
| Décider sur la probabilité cumulée des catégories à archiver | Aucun effet |
| Début + fin d'un long mail vaut mieux que le début seul | Confirmé sur 2 mails seulement : demande en fin de mail perdue avec le début seul, gardée avec début + fin pour moitié moins de tokens |
| Questions métier dans le même appel | Rendez-vous 7/7, engagement 2/2, paiement 3/4, sans faux positif. Échéance : se déclenche sur les promos et arnaques, inutilisable sans filtre de catégorie |

Les 5 erreurs du flow actuel sur ce corpus viennent des règles de routage, pas de JEV (voir L4). Coût fixe mesuré : environ 750 tokens par mail pour les définitions des 9 catégories et 3 urgences, puis 1,3 token par mot de corps.

### L1 — Banc d'essai intégré au dépôt

- ⬜ Corpus de test versionné (`src/evaluation/corpus.toml`) : les 50 mails de la première série, chacun avec ses routes acceptables, `needs_reply` attendu et faits métier attendus. Mails inventés uniquement, aucun mail réel dans le dépôt
- ⬜ Notion de variante dans `src/evaluation` : un nom, une transformation de la requête JEV (état, questions) et une fonction de routage. La variante `current` réutilise `JevClassifier` et `route_for` tels quels
- ⬜ `python -m src.evaluation lab --source corpus|rated --variants ... --repeats N` :
  - `corpus` : compare aux routes acceptables du corpus ;
  - `rated` : rejoue les vrais mails notés (`rated_decisions` + `fetch_message`) et compare aux contraintes déjà définies pour chaque verdict (`EXPECTATIONS`)
- ⬜ Rapport par variante : décisions correctes, routes qui changent entre deux appels, tokens et latence (médiane, p95), liste des mails mal routés. Pour chaque question oui/non : ratés et faux positifs quand la vérité est connue, sinon taux de oui et liste à contrôler à la main
- ⬜ Garde de coût : nombre d'appels affiché avant exécution, plafond `--max-calls`
- ⬜ Même avertissement « non concluant » que le banc existant sous 100 verdicts dont 20 corrections

**Validation de L1** (condition d'entrée de L2, L3 et L4)
- Sur le corpus, la variante `current` retrouve les chiffres de la première série (45/50, aucun changement de route entre appels)
- Exécuté sur le VPS avec `--source rated` ; nombre de cas, taux par variante et coût notés ici
- Décision écrite ici pour chacun des points L2, L3, L4 : lancé, reporté ou abandonné, chiffres à l'appui

### L2 — Questions métier (en attente de L1)

- ⬜ Stockage commun : colonne `decisions.signals` (JSON, probabilité par question), une seule migration au lieu d'une par question
- ⬜ Même garde que `needs_reply` pour toutes : expéditeur non automatisé et catégorie hors spam / newsletter / promotion / alerte emploi. Sans elle, `has_deadline` répond oui à « offre jusqu'à minuit »
- ⬜ Une question n'est ajoutée que lorsque la fonction qui la consomme est en cours, chacune derrière son flag :
  - `asks_for_meeting` → Phase 4 (créneaux, agenda)
  - `contains_commitment` → Phase 2 (`waiting_for_them` : l'autre a promis de revenir)
  - `has_payment_due` et `has_deadline` → rappels du récap (Phase 3) et de la Phase 6
- ⬜ Seuil de chaque question lu sur le banc, pas choisi à l'avance
- ⬜ Chaque question a son retour utilisateur avant d'être utilisée pour agir (bouton sur l'élément qu'elle produit)

**DoD** : pour chaque question activée, ratés et faux positifs mesurés sur le corpus et contrôlés à la main sur 30 vrais mails, chiffres notés ici.

### L3 — Troncature début + fin (en attente de L1)

- ⬜ Variantes du banc sur les vrais mails de plus de 300 mots : début 1000 mots (actuel), début 700 + fin 300, début 150, début 100 + fin 50
- ⬜ Prérequis : `fetch_message` renvoie aujourd'hui un corps déjà coupé à 1000 mots (`clean_body`), la fin d'un mail plus long est donc perdue avant le banc. Le banc doit pouvoir demander le corps nettoyé non coupé
- ⬜ Règle de décision : adopter la variante la moins chère dont l'accord avec les verdicts n'est pas inférieur à l'actuelle et qui ne change aucune route correcte ; sinon ne rien changer
- ⬜ Si adoptée : `truncate_words` garde le début et la fin, marqueur de coupure visible dans le texte envoyé

**DoD** : au moins 30 vrais mails longs rejoués, économie de tokens et écarts de route notés ici.

### L4 — Trois règles de routage (en attente de L1)

Les deux premières se vérifient sans aucun appel JEV : elles réinterprètent l'urgence, la catégorie et la confiance déjà stockées.

- ⬜ **Archiver `alerte_technique` d'urgence basse et confiante** (succès de CI, mises à jour de dépendances), comme `notification_systeme` aujourd'hui. À compter sur l'historique : mails concernés, et parmi eux ceux qui ont reçu `wrong_archive` ou `missed_urgent`
- ⬜ **Archiver le spam même quand il se dit urgent**. Aujourd'hui urgence haute + spam reste en boîte par prudence. Risque : un vrai mail urgent pris pour du spam. Garde proposée : seulement au-dessus d'un seuil de confiance sur la catégorie, lu sur le banc
- ⬜ **Élargir « haute » à une échéance sous 3 jours** (domaine qui expire, paiement à régulariser). Modifie la définition envoyée à JEV : nécessite un rejeu. À mesurer : alertes en plus par semaine, à confronter au plafond de notifications (invariant 5)
- ⬜ Chaque règle adoptée arrive seule, dans sa PR, avec le nombre de mails de l'historique qu'elle aurait déplacés

**DoD** : pour chaque règle, nombre de mails déplacés sur 90 jours et verdicts contredits notés ici ; aucune règle adoptée si elle contredit un verdict existant.

---

## Phase 2 — Threads en attente

**Pourquoi** : chaque mail est traité isolément. Rien ne dit ce qui attend une action de ta part ni ce qui attend une réponse de l'autre côté.

**Coût** : moyen à élevé.

- ⬜ Table `threads` : interlocuteur, état, dernière entrée, dernière sortie, date de relance, raison de clôture. Les threads ouverts sont exclus de la purge à 90 jours
- ⬜ `MailProvider` : lecture d'un thread en métadonnées (qui a écrit en dernier, labels) et liste des threads envoyés récents. Indispensable : une réponse faite depuis Gmail doit être vue
- ✅ Question JEV `needs_reply` ajoutée à l'appel de classification existant (`NEEDS_REPLY_ENABLED`, désactivé par défaut), avancée avant le reste de la phase. Mesuré sur la vraie API avec 34 mails de test : latence inchangée (0,25 s), +15 % de tokens, 13 mails non urgents sur 13 détectés, aucun faux positif après filtrage. Filtre : expéditeur non automatisé et catégorie hors spam / newsletter / promotion / alerte emploi, plus large que les trois catégories prévues (une demande d'une administration passe). Probabilité stockée dans `decisions.needs_reply`
- ✅ Brouillon silencieux : un mail non urgent qui attend une réponse reçoit un brouillon Gmail et le label `Assistant/A_repondre`, sans notification. **Reste à faire** : vérifier sur les vrais mails (les tests portent sur des mails écrits pour l'occasion) et ajouter un verdict, prévu avec `/pending`
- ⬜ Dérivation d'état déterministe : dernier message de l'autre + `needs_reply` → `waiting_for_me` ; dernier message de toi → `waiting_for_them` ; sinon clos
- ⬜ Rafraîchissement à cadence réduite (15 min) et à la demande, pas à chaque cycle de polling
- ⬜ `/pending` avec boutons `[Fait] [Ignorer] [Relancer]`. `[Ignorer]` sert aussi de verdict sur `needs_reply`
- ⬜ Relance : après `FOLLOW_UP_AFTER_DAYS` sans réponse, brouillon Gemini proposé via le flux `[Envoyer]/[Annuler]` existant. Une seule proposition par thread
- ⬜ **Suivi de réponses collectives** : mail envoyé à plusieurs destinataires (convocation, sondage) → qui a répondu, qui ne l'a pas fait, relance groupée proposée aux seuls silencieux
- ⬜ **Fiche interlocuteur** (`/contact <nom>`) : dernier échange, threads ouverts, délai de réponse habituel. Calculée depuis `threads`, sans LLM
- ⬜ **Synthèse de fil** : sur un thread long, résumé en quelques lignes et ce qui est attendu de toi. Un appel LLM, à la demande

**DoD**
- Un envoi réel à au moins 5 destinataires suivi correctement : liste des répondants exacte, relance adressée aux seuls silencieux
- Une réponse envoyée depuis Gmail (hors bot) fait sortir le thread de `waiting_for_me` au rafraîchissement suivant
- Précision et rappel de l'état mesurés sur 30 threads vérifiés à la main, chiffres notés ici
- Aucune relance envoyée sans appui sur `[Envoyer]` ; jamais deux propositions pour le même thread
- `/pending` répond correctement quand il n'y a rien en attente

---

## Phase 3 — Récaps (matin, soir, hebdo) v1

**Pourquoi** : c'est la demande initiale — ce que je devais faire, ce que j'ai fait, ce que je n'ai pas fait.

**Coût** : moyen.

- ⬜ Table `action_items` : source (`thread` / `alert` / `manual`), titre, échéance, statut. Bouton `[📌 À faire]` sur les alertes
- ⬜ Définition de « fait » : réponse envoyée dans le thread, mail archivé par toi, ou `[Fait]`
- ⬜ Planificateur (`src/scheduling/`) : heure locale, dernière exécution persistée dans `kv_state`. Un redémarrage ne doit ni doubler ni sauter un envoi ; un créneau manqué de plus d'une heure est sauté
- ⬜ Un moteur de digest, trois gabarits :
  - matin : urgents non traités, threads `waiting_for_me`, items du jour
  - soir : fait / pas fait / apparu aujourd'hui
  - hebdo (vendredi) : bilan + section métriques (délai médian de réponse, relances, volume par catégorie)
- ⬜ Contenu assemblé par du code. Un seul appel LLM hebdomadaire, pour la mise en récit de la section métriques ; chaque conseil cite la métrique qui le fonde
- ⬜ Trois mails à noter (`/review`) joints au récap du soir : le feedback de la Phase 0 se collecte sans effort
- ⬜ Extraction du planificateur et des jobs hors de `main.py`, qui porte déjà trop de responsabilités
- ⬜ **Suivi de mes engagements** : dans tes mails envoyés, question JEV `contains_commitment` comme filtre, puis extraction Gemini de la promesse et de sa date (« je te renvoie le doc vendredi ») → item proposé, créé après `[Garder]`. Sans cela le récap ne voit que les obligations nées des mails reçus
- ⬜ **Factures et renouvellements** : question JEV `has_payment_due` comme filtre, extraction du montant et de la date → item avec rappel `PAYMENT_REMINDER_DAYS` avant, et liste des prélèvements à venir dans le récap hebdo. Limité aux échéances : pas de suivi de dépenses

**DoD**
- 10 engagements réels pris par mail : taux de détection et taux de faux positifs notés ici
- Un renouvellement réel annoncé avant sa date
- Les trois messages partent à heure fixe pendant 7 jours sans intervention, redémarrage compris
- Un jour sans rien en retard produit un message court, jamais un message vide ni une erreur
- Un mail déjà alerté n'est pas ré-alerté par le récap : il y est listé
- La section métriques ne contient que des faits chiffrés ; coût de l'appel hebdo visible dans `llm_cost_usd_total` et stable

---

## Phase 4 — Calendar, créneaux et reprogrammation

**Pourquoi** : fondation de tout ce qui touche au temps.

**Coût** : élevé.

- ⬜ Port `CalendarProvider` (lister les événements d'une plage, créer un événement) + adaptateur Google, sélecteur `CALENDAR_PROVIDER`
- ⬜ Scope OAuth Calendar ajouté : nouveau consentement et nouveau refresh token (`src/gmail/token_setup.py` à étendre). À vérifier : scope minimal couvrant lecture des événements et création
- ⬜ Recherche de créneaux : fonction pure (événements, heures ouvrées, durée, horizon, marge) → créneaux. Aucun LLM
- ⬜ Demande de rendez-vous : question JEV `asks_for_meeting` comme filtre, puis une extraction Gemini structurée (durée, dates proposées, lieu)
- ⬜ Table `pending_actions` (type, charge utile, message d'offre, statut, expiration) généralisant la garde `bot_draft:` / `draft_sent:`. `callback_data` est limité à 64 octets : seul l'identifiant y circule. Le flux brouillon migre dessus
- ⬜ Proposition Telegram de créneaux → brouillon de réponse (flux d'envoi existant) ; l'événement est créé seulement après confirmation
- ⬜ Reprogrammation dans les récaps : un créneau proposé par item en retard ; la confirmation crée un bloc de temps
- ⬜ v1 : création uniquement. Déplacer un événement existant vient après, avec la même garde
- ⬜ **Conflit à l'arrivée d'une invitation** : chevauchement signalé avec l'événement en cause et un créneau alternatif
- ⬜ **Voyages et réservations** : billet ou réservation reçu → événement proposé avec horaires et référence
- ⬜ **Suivi post-réunion** : événement terminé avec participants externes → brouillon de compte-rendu et items proposés

**DoD**
- Une invitation en conflit et un billet réel traités de bout en bout
- Les disponibilités lues correspondent à l'agenda réel sur une semaine vérifiée à la main, fuseau compris
- Tests de garde : callback forgé, action expirée, double appui, bouton pressé sur un autre message — aucun ne crée d'événement
- Un cas réel de bout en bout : mail → créneaux proposés → validation → événement créé
- Chaque item en retard du récap porte un créneau concret ou la mention explicite qu'aucun n'est libre
- Le flux brouillon existant passe ses tests après migration sur `pending_actions`

---

## Phase 5 — Interface en langage naturel (texte, puis voix)

**Pourquoi** : la voix n'est qu'une entrée de plus. Le vrai travail est de comprendre une instruction libre ; aujourd'hui un message qui ne répond pas à une alerte est ignoré.

**Coût** : élevé.

- ⬜ `TextEvent` : message libre hors réponse à une alerte
- ⬜ Routeur d'intentions : appel de fonctions Gemini sur un ensemble fermé d'outils
- ⬜ Outils de lecture : recherche Gmail, threads en attente, agenda, statistiques. Boucle bornée (4 étapes, budget de tokens). C'est le seul endroit du plan où une boucle d'agent se justifie : les sources à consulter dépendent de la question
- ⬜ Intentions d'écriture (répondre, relancer, créer un événement, marquer fait) → `pending_actions`, jamais exécutées dans la boucle
- ⬜ Cible ambiguë → question avec boutons, jamais de choix silencieux
- ⬜ **Capture rapide** : « rappelle-moi d'appeler la banque jeudi » (texte ou voix) → item créé avec échéance, créneau proposé si Calendar est actif
- ⬜ Voix : `VoiceEvent`, téléchargement (`getFile`, 20 Mo max), transcription par Gemini, transcription renvoyée à l'écran avant toute action, puis même routeur
- ⬜ Préalable à vérifier : le SDK `google-generativeai` 0.8.3 et le modèle par défaut `gemini-1.5-flash` sont anciens ; confirmer leur support (audio, appel de fonctions) ou migrer avant de construire dessus

**DoD**
- 20 questions test variées (mails, agenda, en attente) : réponse correcte vérifiée à la main, taux noté ici
- 20 notes vocales en français : intention correcte, taux noté ici
- Plusieurs cibles possibles → l'assistant demande, testé
- Test d'injection : un mail contenant une instruction ne déclenche aucune action
- Un cas réel de bout en bout : dictée → proposition → confirmation → action

---

## Phase 6 — Assistant proactif

**Pourquoi** : exploite Calendar, les threads et le flux de confirmation.

**Coût** : moyen.

- ⬜ Brief de réunion : job planifié sur les événements à venir ayant des participants externes ; mails retrouvés par adresse des participants ; un appel LLM ; un seul brief par événement. Délai configurable (`MEETING_BRIEF_MINUTES_BEFORE`)
- ⬜ Échéances administratives : question JEV `has_deadline` comme filtre, extraction de la date par Gemini, rappel Calendar proposé via `pending_actions`
- ⬜ Profils de ton : fichier de config associant domaines ou adresses à un registre (association, MIC, pro) et une signature, injecté dans le prompt des brouillons. Indépendant de Calendar : peut être avancé dès la Phase 2

**DoD**
- Brief livré avant un événement test, contenu vérifié à la main, jamais deux fois pour le même événement
- Une échéance réelle détectée et proposée en rappel
- Deux brouillons pour deux profils différents montrent un registre différent, vérifié à la main

---

## Backlog et non retenu

- **Push Gmail (Pub/Sub)** — à rouvrir si un besoin de latence sous 10 s apparaît ou si le VPS expose un endpoint HTTPS
- **`VACUUM` planifié** — à rouvrir si le fichier dépasse 100 Mo
- **Adaptateur Calendar MCP** — à rouvrir si un second consommateur de l'agenda apparaît
- **Déplacement d'événements existants** — après la Phase 4 v1
- **Entrant Discord** — exige un endpoint HTTPS public signé
- **Scoring d'offres d'emploi** — hors scope

## Risques ouverts

- **Sauvegarde SQLite toujours absente.** La base va porter les threads, les items et les actions en attente : sa perte coûtera bien plus qu'aujourd'hui. Traité en S1, avant toute migration
- Les questions JEV s'accumulent (`needs_reply`, `asks_for_meeting`, `has_deadline`, `contains_commitment`, `has_payment_due`) : le coût et la latence sont mesurés et faibles (Phase 1bis), le risque restant est la qualité. Chacune doit avoir son filtre de catégorie et son propre retour utilisateur pour être évaluée
- Les chiffres de la Phase 1bis viennent de mails écrits pour le test : aucune décision de routage ne doit en découler avant le rejeu sur les vrais mails (L1)
- Un seul poller par token Telegram : n'activer `TELEGRAM_INBOUND_ENABLED` que sur une instance
- Few-shot : un objet reste du texte choisi par l'expéditeur, rejoué dans chaque classification
- Few-shot : un exemple ne porte que le domaine de l'expéditeur. Constaté avec deux exemples fictifs : une correction sur un expéditeur `gmail.com` a changé l'urgence d'autres mails `gmail.com` sans rapport. À confirmer sur les vraies corrections avec `python -m src.evaluation run`
- Nouveau consentement OAuth en Phase 4 : l'ancien refresh token ne porte pas le scope Calendar, prévoir la bascule
- Lecture des threads : un appel Gmail par thread ouvert et par rafraîchissement ; borner le nombre de threads suivis
- Fatigue de notification : trois digests, briefs et relances s'ajoutent aux alertes. Le plafond de l'invariant 5 doit exister dès la Phase 3
- `main.py` et `ApplicationContext` grossissent à chaque phase : l'extraction prévue en Phase 3 ne doit pas être repoussée

---

## Prochaine étape

Le code de S1, de la Phase 0 et de la Phase 1 est écrit. Ce qui reste dépend du déploiement et des données :

1. Déployer, puis vérifier sur le VPS : sauvegarde et restauration, alerte de panne, `/help`, `/review`, `/stats`
2. `python -m src.evaluation check-examples` avec la vraie clé JEV
3. Utiliser `/review` jusqu'à 30 verdicts hors alertes (critère de sortie de la Phase 0)
4. À 100 verdicts dont 20 corrections : `python -m src.evaluation run`, puis décider du few-shot et du repli Gemini, chiffres notés ici
5. `python -m src.evaluation candidates` pour écrire les premières règles et la liste VIP
6. Activer `NEEDS_REPLY_ENABLED`, puis contrôler pendant une semaine les brouillons créés et le label `Assistant/A_repondre` ; ajuster `NEEDS_REPLY_THRESHOLD` d'après les probabilités stockées
7. L1 (banc d'essai des questions JEV), puis décision sur L2, L3 et L4 d'après ses résultats sur les vrais mails
8. S2 avant le reste de la Phase 2
