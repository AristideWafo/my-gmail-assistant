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
5. **Budget de notifications** : heures calmes et plafond quotidien de messages proactifs ; seules les alertes urgentes y échappent. En place (`src/scheduling/budget.py`, `PROACTIVE_DAILY_CAP`, `QUIET_HOURS`) : toute nouvelle source de message proactif doit y passer.
6. **Fuseau explicite** (`TIMEZONE`, en place depuis la liste quotidienne) pour tout ce qui est horaire. Le stockage reste en UTC.

## Dépendances

```text
S1 ──► P0 ──► P2 ──► P3 ──► P4 ──► P6
        │      ▲      │      │
        │      S2     └──────┴──► P5
        └──► P1 ──► L1 ──► L2, L3, L4, L5   (piste parallèle, rythmée par les données)
```

S1 et S2 sont les deux lots de la stabilisation ci-dessous. S3 est une règle de passage appliquée entre chaque phase. L1 à L5 forment la Phase 1bis : L2 à L5 ne démarrent qu'après la validation de L1.

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

- ✅ `sync_history_once` reprend la garde de `poll_once` : un mail en erreur est compté et sauté, et un historique illisible n'empêche plus le démarrage
- ✅ Délai d'attente sur chaque appel Gemini (`GEMINI_TIMEOUT_SECONDS`, 30 s) : un appel bloqué est abandonné sans nouvel essai, compté dans `llm_errors_total{reason="timeout"}`, et le mail suit le chemin dégradé existant (alerte sans résumé, pas de brouillon)
- ✅ Alertes Telegram (`src/health/watch.py`, `HEALTH_ALERT_WINDOW_MINUTES`, `HEALTH_ALERT_MIN_EVENTS`) : replis JEV, mails en échec, erreurs Gemini au-delà d'un seuil sur 15 min ; listener Telegram mort ou muet depuis 10 min. Un message au début du problème, un à la fin. **Limite** : les trois premiers comptent de vrais échecs, donc sans trafic une panne reste muette ; une sonde périodique des connexions les compléterait
- ✅ Plafond de dépense LLM quotidien (`LLM_DAILY_BUDGET_USD`, désactivé par défaut) : au-delà, Gemini n'est plus appelé jusqu'au lendemain (alertes sans résumé ni brouillon) et un message part sur Telegram. Total du jour gardé en base. **Limite** : seuls les modèles dont le prix est connu du code sont comptés ; JEV ne l'est pas
- 🟡 Exposition réseau : les ports 8000, 9090 et 3000 sont publiés sur `BIND_ADDRESS`, `127.0.0.1` par défaut ; accès distant par tunnel SSH. **Reste à faire** : définir `GRAFANA_ADMIN_PASSWORD` sur le VPS (toujours `admin` par défaut : le rendre obligatoire aurait cassé toutes les commandes `docker compose` tant qu'il n'est pas défini), puis vérifier depuis une autre machine que rien ne répond
- ✅ Images épinglées : `prom/prometheus:v3.15.0`, `grafana/grafana:13.2.3`
- 🟡 Déploiement : `docker-compose.yml` fait tourner l'image publiée (`VERSION` dans `.env`), la construction locale passe par `docker-compose.build.yml`. Procédure de retour arrière écrite dans le README, migration de schéma comprise. **Reste à faire** : l'exécuter une fois sur le VPS et noter la durée
- ✅ Dépendances de développement déclarées (`requirements-dev.txt`, versions figées), utilisées par la CI et la release
- ✅ Couverture mesurée en CI (`pytest --cov`) : 98,2 % à ce jour, plancher à 98 % (`fail_under`), à relever quand la couverture monte, jamais à baisser
- ✅ Dependabot (`.github/dependabot.yml`) : Python chaque semaine, GitHub Actions et image de base chaque mois. **À surveiller** : la première salve proposera des sauts importants (SDK Gemini, FastAPI, LangGraph), à fusionner un par un
- ✅ `@app.on_event` remplacé par `lifespan` : un démarrage qui échoue libère ce qui était ouvert. Version de l'app lue dans `pyproject.toml` (`src/version.py`), exposée par l'API et écrite dans le journal au démarrage
- ✅ Runbook (`RUNBOOK.md`) : token Gmail révoqué, Gmail injoignable, 409 Telegram, JEV indisponible, quota ou délai Gemini, budget LLM atteint, mails en échec, base plus récente que le code, base corrompue, silence total. Chaque entrée part du signe visible (message Telegram, ligne de journal)

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
- ✅ Boutons selon la route : `label` → `[OK] [À voir] [Urgent raté] [Spam]` ; `reject` → `[OK] [À garder] [À voir] [Urgent raté]`. `[À voir]` (`missed_important`) : le mail méritait d'être mis en avant sans notification ; `[Urgent raté]` : il méritait une alerte immédiate
- ✅ `/stats` (`src/interactions/stats.py`) : précision et types d'erreur par route et par source, volume de décisions par source sur 30 jours, lus dans SQLite ; `feedback_total{verdict, route}` et panneau Grafana par route
- ✅ `missed_urgent` ajouté à `_VERDICT_CORRECTIONS` (`high`, catégorie inchangée). `wrong_archive` et `missed_important` sont mesurés mais pas injectés en few-shot : ni la bonne catégorie ni une urgence à corriger ne sont connues

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

- ✅ Corpus de test versionné (`src/evaluation/corpus.toml`) : les 50 mails de la première série, chacun avec ses routes acceptables, `needs_reply` attendu et faits métier attendus. Mails inventés uniquement, aucun mail réel dans le dépôt
- ✅ Notion de variante dans `src/evaluation` : un nom, une transformation de la requête JEV (état, questions) et une fonction de routage. La variante `current` réutilise `JevClassifier` et `route_for` tels quels
- ✅ `python -m src.evaluation lab --source corpus|rated --variants ... --repeats N` :
  - `corpus` : compare aux routes acceptables du corpus ;
  - `rated` : rejoue les vrais mails notés (`rated_decisions` + `fetch_message`) et compare aux contraintes déjà définies pour chaque verdict (`EXPECTATIONS`)
- ✅ Rapport par variante : décisions correctes, routes qui changent entre deux appels, tokens et latence (médiane, p95), liste des mails mal routés. Pour chaque question oui/non : ratés et faux positifs quand la vérité est connue, sinon taux de oui et liste à contrôler à la main
- ✅ Garde de coût : nombre d'appels affiché avant exécution, plafond `--max-calls`
- ✅ Même avertissement « non concluant » que le banc existant sous 100 verdicts dont 20 corrections

**Validation de L1** (condition d'entrée de L2, L3 et L4)
- ✅ Sur le corpus, la variante `current` retrouve les chiffres de la première série : 45/50 sur 3 passes, aucun changement de route, 936 tokens par mail, latence médiane 0,26 s (`direct-action` 47/50, `signals` 45/50 pour 1 377 tokens)
- ✅ Exécuté sur le VPS avec `docker compose exec assistant python -m src.evaluation lab --source rated` (v0.11.0) : résultats ci-dessous
- ⬜ Décision écrite ici pour chacun des points L2, L3, L4 : lancé, reporté ou abandonné, chiffres à l'appui. Décisions proposées ci-dessous, à confirmer

**Résultats sur les vrais mails notés** — **non concluants** : moins de 100 verdicts dont 20 corrections

| | Passe 1 | Passe 2 |
| --- | --- | --- |
| Verdicts en base | 17 | 43 |
| Décidés par une règle, écartés | 1 | 6 |
| Mails rejoués | 16 | 37 |
| `current`, routes conformes au verdict | 10/16 | 28/37 |
| `direct-action` | 10/16 | 24/37 |
| `signals` | 11/16 | 28/37 |
| Tokens par mail (`current` / `signals`) | 2 431 / 2 872 | 2 375 / 2 816 |
| Latence médiane | 0,24 s | 0,24 s |

Ce que ces chiffres montrent, et ce qu'ils ne montrent pas :

- **Le coût réel est 2,5 fois celui du corpus** (2 375 tokens contre 936) : les vrais mails sont longs, proches du plafond de 1000 mots. Le corps pèse environ 70 % du coût, la troncature (L3) est donc le premier levier. La latence ne bouge pas
- **Les questions oui/non ajoutent 19 % de tokens** (441 par mail) sur de vrais mails, sans effet sur la latence
- **Bruit entre deux appels : un à deux mails sur 37.** `current` et `signals` posent les mêmes questions de routage ; ils diffèrent d'un mail à la passe 1, d'aucun à la passe 2, de deux à la passe 3. Les deux écarts de la passe 3 sont des archivages : le bruit peut donc faire disparaître un mail de la boîte
- **`direct-action` fait moins bien** : 24/37 contre 28/37. Ses erreurs propres sont quatre archivages de discussions de revue de code et une alerte sur un avis bancaire automatique. Même constat que sur le corpus
- **`has_deadline` est inutilisable tel quel** : 5 oui sur 37, dont 4 sur des mails que tu as validés comme archivés ou simplement gardés (lettres d'information, événements, conditions d'utilisation)
- **`asks_for_meeting` suit mieux tes verdicts que prévu** : 9 oui, dont 6 sur des mails que tu voulais voir alertés (2 alertes validées, 4 « urgent raté ») et 3 sur des mails validés sans alerte (deux webinaires archivés, un événement gardé). Les événements auxquels tu participes comptent donc pour toi ; ce qui reste à exclure, ce sont les webinaires promotionnels. Lecture corrigée : la passe 2 les comptait tous comme faux
- **`contains_commitment`** : un seul oui, sur un avis bancaire automatique. **`has_payment_due`** : aucun oui
- **`needs_reply`** : oui sur 3 mails sur 37, donc pas une copie de l'urgence. À contrôler à la main
- **Une des erreurs de `current` est un archivage** : un avis d'une administration, classé notification d'urgence basse, est archivé alors que le verdict demandait autre chose. La règle d'archivage existante se trompe donc déjà au moins une fois sur 37 (voir L4)

Limites de l'outil apparues à l'usage :

- Le rapport ne montrait pas le verdict d'un mail mal routé : impossible de dire si JEV répète une erreur déjà corrigée ou s'écarte d'une décision validée. Corrigé : chaque mail noté est affiché avec son verdict et sa route d'origine
- `current` n'envoie pas les exemples few-shot, alors que la production les envoie quand `JEV_FEW_SHOT_ENABLED` est actif. Le score de `current` n'est donc pas exactement celui de la production. Sans effet sur la comparaison entre variantes, qui partagent ce biais
- Passe 3, verdicts affichés : les 9 mails non conformes de `current` sont tous des corrections que le rejeu reproduit à l'identique. Aucune décision validée n'est modifiée par `current`

**Passe 3, avec le verdict de chaque mail** (v0.12.0, mêmes 37 mails : `current` 28/37, `direct-action` 24/37, `signals` 27/37)

Les 9 erreurs de `current`, par verdict :

| Verdict | Nombre | Nature |
| --- | --- | --- |
| `missed_urgent` (gardé, tu voulais une alerte) | 7 | Annonce datée qui te concerne, événements et invitation dans la semaine, rappel pour le lendemain, migration annoncée d'un outil que tu utilises, message de recruteur, question d'une personne |
| `wrong_archive` | 1 | Avis d'une administration archivé |
| `false_spam` (gardé, tu voulais l'archiver) | 1 | Résumé de discussions d'un réseau social |

- **7 erreurs sur 9 sont des urgences ratées, aucune n'est une fausse alerte.** Le tri est trop prudent sur l'alerte, pas trop bavard. La définition de « haute » envoyée à JEV (à traiter aujourd'hui ou demain) est plus étroite que ce que tu appelles urgent : des choses datées dans les jours qui viennent, et des personnes qui attendent quelque chose de toi
- **Sens de « Urgent raté », tranché** : « j'aurais voulu que ce soit mis en avant », pas « j'aurais voulu une alerte immédiate ». S'y ajoutent deux règles : une réponse est préparée dès qu'elle est attendue, quelle que soit l'urgence ; la notification Telegram, elle, dépend de l'urgence. La définition de « haute » n'est donc pas élargie : il manque un niveau entre l'alerte et le simple label (lot L5)
- **Les questions oui/non ne suffisent pas à rattraper ces 7 mails** : `asks_for_meeting` ou `needs_reply` en couvrent 5, mais déclencheraient aussi sur 4 mails validés sans alerte
- **`needs_reply`** : oui sur 3 mails. Deux sont des urgences ratées (message de recruteur, question d'une personne) : le brouillon silencieux les aurait signalés. Le troisième est un mail validé comme simplement gardé
- **`direct-action`** : ses erreurs propres sont 5 décisions validées qu'il modifie (4 discussions de revue de code archivées, 1 avis bancaire alerté) et 1 archive validée qu'il garde


**Décisions proposées** (à confirmer) :

- **L2 — reporté.** Chaque question attend la fonction qui la consomme, comme prévu. Acquis : coût faible. À faire avant tout usage : réécrire `has_deadline` et `asks_for_meeting`, qui se déclenchent sur les événements publics et les lettres d'information
- **L3 — à lancer en premier.** C'est le seul levier de coût significatif. Il lui faut une source `recent` (voir L3) : 37 mails notés ne suffisent pas, et comparer une troncature à la réponse sur le mail entier ne demande aucun verdict
- **L4 — « haute » n'est pas élargie.** Les 7 urgences ratées demandaient une mise en avant, pas une alerte : elles relèvent du lot L5. Restent pour L4 les règles d'archivage, à mesurer d'abord sur l'historique : la règle existante est déjà contredite par un verdict
- **L5 — lancé** : niveau « à voir » entre l'alerte et le label
- **`direct-action` — abandonné** : moins bon sur le corpus en gravité d'erreur, moins bon sur les vrais mails en nombre

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

### L3 — Troncature début + fin

- ✅ Source `recent` du banc (`lab --source recent`) : les derniers mails de plus de `--min-words` mots (300 par défaut), sans verdict. Une première passe de `current` donne la route de référence de chaque mail ; chaque variante est notée sur son accord avec elle. La ligne `current` mesure donc le bruit entre deux appels identiques
- ✅ Variantes de coupe : `current` (1000 premiers mots, comme la production), `head-700-tail-300`, `head-150`, `head-100-tail-50`. Utilisables aussi sur `--source rated`, pour confronter une coupe aux verdicts
- ✅ `fetch_message(full_body=True)` : le banc reçoit le corps nettoyé non coupé, la production ne change pas
- ✅ `truncate_words(text, max_words, tail_words)` garde le début et la fin autour d'une coupure visible `[…]`. La production l'appelle sans fin : adopter une coupe revient à changer cet appel
- ⬜ À lancer sur le VPS : `lab --source recent`, puis `lab --source rated --variants current,head-700-tail-300,head-150,head-100-tail-50`
- ⬜ Règle de décision : adopter la variante la moins chère dont l'accord avec les verdicts n'est pas inférieur à l'actuelle, qui ne change aucune route correcte, et dont l'écart avec `current` sur les mails récents ne dépasse pas le bruit ; sinon ne rien changer

**DoD** : au moins 30 vrais mails longs rejoués, économie de tokens et écarts de route notés ici.

### L4 — Règles d'archivage

Elles se vérifient sans aucun appel JEV : elles réinterprètent l'urgence, la catégorie et la confiance déjà stockées.

- ✅ `python -m src.evaluation routing-rules [--days 90]` : pour la règle en production et pour chaque candidate, nombre de mails concernés ou déplacés, verdicts qui l'approuvent, et liste des mails dont le verdict la contredit. Décisions de règle et de liste VIP écartées
- ⬜ À lancer sur le VPS, résultat à noter ici avant toute adoption
- ⬜ **Mesurer d'abord la règle existante** (`notification_systeme` d'urgence basse et confiante → archivé) : nombre de mails archivés par elle sur 90 jours, et parmi eux ceux qui ont reçu `wrong_archive` ou `missed_urgent`. Un avis d'administration archivé à tort a été vu au banc
- ⬜ **Archiver `alerte_technique` d'urgence basse et confiante** (succès de CI, mises à jour de dépendances), comme `notification_systeme` aujourd'hui. À compter sur l'historique : mails concernés, et parmi eux ceux qui ont reçu `wrong_archive` ou `missed_urgent`
- ⬜ **Archiver le spam même quand il se dit urgent**. Aujourd'hui urgence haute + spam reste en boîte par prudence. Risque : un vrai mail urgent pris pour du spam. Garde proposée : seulement au-dessus d'un seuil de confiance sur la catégorie, lu sur le banc. Limite de la mesure : la base ne garde que la plus faible des deux confiances (urgence, catégorie), l'audit est donc plus strict que la règle proposée
- **Élargir « haute » : abandonné.** Les mails datés dans les jours qui viennent et les personnes qui attendent une réponse ne doivent pas sonner : ils sont mis en avant (L5)
- ⬜ Chaque règle adoptée arrive seule, dans sa PR, avec le nombre de mails de l'historique qu'elle aurait déplacés

**DoD** : pour chaque règle, nombre de mails déplacés sur 90 jours et verdicts contredits notés ici ; aucune règle adoptée si elle contredit un verdict existant.

### L5 — Niveau « à voir » : mettre en avant sans notifier

**Pourquoi** : 7 des 9 erreurs relevées au banc sont des mails gardés que tu voulais voir mis en avant. Le tri ne connaît que trois sorties (alerte, label, archive) et le label seul ne montre rien : un mail traité est marqué lu.

Conception soumise à une relecture critique avant le code. Ce qui en est retenu :

- Un label Gmail ne suffit pas : il faut une liste envoyée une fois par jour, sans son, sinon un rappel pour le lendemain reste invisible
- Des questions étroites composées par le code, pas une question large : chacune se mesure et se coupe séparément
- Les questions tournent d'abord sans effet (probabilités stockées), le temps de compter ce qu'elles auraient mis en avant
- Les verdicts de mise en avant ne deviennent pas des exemples few-shot : un exemple ne porte que le domaine et l'objet, il agirait comme une liste d'expéditeurs
- `[OK]` sur un mail gardé voulait dire « pas urgent », pas « inutile à voir » : les verdicts existants ne prouvent pas qu'un mail ne devait pas être mis en avant

Lots :

- ✅ Verdict `missed_important` et bouton `[À voir]` dans `/review`, distinct de `[Urgent raté]`. Les verdicts « Urgent raté » déjà donnés par `/review` sont convertis (migration 4, état précédent gardé dans `assistant.db.pre-v4`) : ils cessent d'être envoyés à JEV comme « urgence correcte : haute ». Lecture du banc après conversion : `missed_important` exclut seulement l'archivage
- ✅ Brouillon de réponse indépendant de l'urgence : avec `NEEDS_REPLY_ENABLED`, un mail urgent reçoit un brouillon et le label `Assistant/A_repondre` dès qu'une réponse est attendue, quelle que soit sa catégorie. La règle par catégorie reste en complément, pour ne pas perdre le brouillon d'un mail décidé sans la question (règle, VIP, repli) ou juste sous le seuil. Le brouillon ne décide jamais à ta place : ni acceptation, ni refus, ni date, ni montant
- ✅ Questions de mise en avant en mode observation (`ATTENTION_MODE=shadow`) : `personal_event`, `service_change`, `personal_deadline`, plus `needs_reply`. Réponses stockées dans `decisions.signals` (JSON, le stockage commun prévu en L2), comptées dans `attention_signals_total`, sans aucun effet. Lecture : `python -m src.evaluation attention`
  - Garde par catégorie (spam, newsletter, promotion, alerte emploi), pas de garde sur l'expéditeur : les avis de coupure et les messages relayés par une plateforme viennent d'adresses automatiques
  - Mesure sur la vraie API, corpus porté à 68 mails (18 ajoutés : 9 à mettre en avant, 9 sosies à écarter) : 34 mails à mettre en avant, 34 retrouvés, 1 mis en avant à tort (un « pour information » sur un planning). `personal_event` : aucun raté, aucun faux positif. `personal_deadline` répond oui aux promotions et aux arnaques, écartées par la garde de catégorie
  - Coût : 526 tokens de plus par mail pour les quatre questions (942 → 1 468), latence inchangée
  - Deux mails du corpus à mettre en avant sont aujourd'hui archivés (billet d'un événement, avis de travaux) par la règle « notification d'urgence basse » : la mise en avant devra les sortir de l'archivage
  - **Limite** : questions et mails de test ont le même auteur. Seul le mode observation sur les vrais mails dit le volume réel ; cible : 3 mails par jour au plus
- ✅ Mise en avant (`ATTENTION_MODE=on`) : un mail non urgent qui a au moins une raison reçoit le label `Assistant/A_voir`, sans notification, et reste en boîte même si la règle « notification d'urgence basse » l'aurait archivé. Décision stockée (`decisions.put_forward`). Au banc, les deux mails du corpus archivés à tort sont rattrapés : 63/68 contre 61/68, aucune route correcte modifiée. **Reste à faire** : passer de `shadow` à `on` au vu du volume réel
- ✅ Liste quotidienne (`ATTENTION_LIST_HOUR`, `TIMEZONE`) : une fois par jour, sans son, les mails mis en avant depuis la liste précédente, 5 au plus (le reste le lendemain), rien si rien de nouveau. `/avoir` liste à la demande ce qui attend encore sur 7 jours. Un mail sort de la liste quand il est noté ou qu'il a quitté la boîte de réception dans Gmail
- ✅ Boutons `[Vu] [Pas utile]` : `[Pas utile]` est le verdict `false_important` (origine `list`), seule source de contre-exemples pour la mise en avant
- ✅ `DailyJob` (`src/scheduling`) : exécution une fois par jour local, dernière exécution dans `kv_state`. C'est le planificateur prévu en Phase 3, à réutiliser pour les récaps
- ⬜ À faire sur le VPS : `ATTENTION_MODE=shadow` quelques jours, lire `python -m src.evaluation attention`, puis `on` et `ATTENTION_LIST_HOUR` si le volume convient (cible : 3 mails par jour au plus)

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
- ⬜ Relance des mails envoyés sans réponse. Conception revue par un agent architecte ; les écarts avec la première idée viennent de la lecture du code :
  - un message par relance, pas un message groupé : un appui sur un bouton retire tous les boutons du message (`_clear`) ;
  - brouillon créé au moment de [Envoyer], pas avant : `send_draft` envoie le brouillon tel qu'il est dans Gmail, il pourrait différer de l'aperçu confirmé ;
  - texte par modèle français / anglais, sans Gemini en v1 : pas de coût, pas d'engagement inventé, pas d'injection ;
  - question JEV `expects_answer` posée sur ton texte sans la citation du mail auquel tu réponds ; seuil étalonné hors ligne sur tes envois passés (`lab --source sent`, ~60 mails étiquetés) ;
  - « répondu » au moindre doute : message d'une autre adresse que les tiennes (`sendAs`) et non automatique dans le fil, ou mail de l'interlocuteur dans un autre fil ; rebond = fil fermé ;
  - seuls les envois postérieurs à l'activation sont suivis ; une seule proposition par fil, sauf [Reporter] ;
  - avant l'envoi : mode toujours `on`, offre de moins de 48 h, ancre inchangée, toujours sans réponse, envoi réservé au plus une fois.
  - Lots : ✅ budget de notifications ; ✅ lecture des envois et des fils (adresses `sendAs`, en-têtes des fils, texte sans citation, recherche d'une réponse hors fil) ; ✅ table `threads` en observation (`FOLLOW_UP_MODE=shadow`) ; ⬜ question `expects_answer` et son banc ; ⬜ `/pending` ; ⬜ proposition et envoi
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
- 🟡 Planificateur (`src/scheduling/`) : heure locale, dernière exécution persistée dans `kv_state`. Un redémarrage ne doit ni doubler ni sauter un envoi. `DailyJob` existe (liste des mails mis en avant) ; reste la règle « un créneau manqué de plus d'une heure est sauté », inutile pour cette liste
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

- **Sauvegarde hors de l'hôte absente.** La sauvegarde quotidienne existe (S1) mais reste sur le même disque que la base, qui va porter les threads, les items et les actions en attente. La copie vers une autre machine reste à planifier sur le VPS
- Les questions JEV s'accumulent (`needs_reply`, `asks_for_meeting`, `has_deadline`, `contains_commitment`, `has_payment_due`) : le coût et la latence sont mesurés et faibles (Phase 1bis), le risque restant est la qualité. Chacune doit avoir son filtre de catégorie et son propre retour utilisateur pour être évaluée
- Les chiffres de la Phase 1bis viennent de mails écrits pour le test : aucune décision de routage ne doit en découler avant le rejeu sur les vrais mails (L1)
- Un seul poller par token Telegram : n'activer `TELEGRAM_INBOUND_ENABLED` que sur une instance
- Few-shot : un objet reste du texte choisi par l'expéditeur, rejoué dans chaque classification
- Few-shot : un exemple ne porte que le domaine de l'expéditeur. Constaté avec deux exemples fictifs : une correction sur un expéditeur `gmail.com` a changé l'urgence d'autres mails `gmail.com` sans rapport. À confirmer sur les vraies corrections avec `python -m src.evaluation run`
- Nouveau consentement OAuth en Phase 4 : l'ancien refresh token ne porte pas le scope Calendar, prévoir la bascule
- Lecture des threads : un appel Gmail par thread ouvert et par rafraîchissement ; borner le nombre de threads suivis
- Fatigue de notification : trois digests, briefs et relances s'ajoutent aux alertes. Le plafond de l'invariant 5 existe (`PROACTIVE_DAILY_CAP`) ; chaque nouveau message programmé doit y passer
- `main.py` et `ApplicationContext` grossissent à chaque phase : l'extraction prévue en Phase 3 ne doit pas être repoussée

---

## Prochaine étape

Le code de S1, de la Phase 0, de la Phase 1, de la Phase 1bis (L1, L3 à L5) et de S2 est écrit. Ce qui reste se fait sur le VPS, puis dépend des données.

**Au déploiement** (dans cet ordre) :

1. `.env` : `VERSION` (version publiée), `TIMEZONE=Europe/Paris`, `GRAFANA_ADMIN_PASSWORD`. Grafana et Prometheus ne répondent plus que sur `127.0.0.1` : tunnel SSH, ou `BIND_ADDRESS` derrière un pare-feu
2. `docker compose pull && docker compose up -d`, puis lire la première ligne du journal (`Starting my-gmail-assistant <version>`) et le rapport de démarrage sur Telegram
3. La base passe au schéma 6 ; l'état précédent reste dans `assistant.db.pre-v6`. Les « Urgent raté » déjà donnés deviennent « À voir »
4. `ATTENTION_MODE=shadow` et `NEEDS_REPLY_ENABLED=true`

**Après quelques jours** :

5. `python -m src.evaluation attention` : combien de mails par jour seraient mis en avant, et pour quelle raison. Cible : 3 au plus. Si le volume convient : `ATTENTION_MODE=on` et `ATTENTION_LIST_HOUR`
6. `python -m src.evaluation lab --source rated` : relire les 37 mails avec les verdicts convertis
7. `python -m src.evaluation lab --source recent` (L3) et `python -m src.evaluation routing-rules` (L4), résultats à noter ici avant toute adoption
8. Contrôler les brouillons créés et le label `Assistant/A_repondre` ; ajuster `NEEDS_REPLY_THRESHOLD` d'après les probabilités stockées
9. `python -m src.evaluation candidates` pour écrire les premières règles et la liste VIP

**Critères encore ouverts** :

10. S1 : copie des sauvegardes hors de l'hôte, restauration exécutée une fois
11. S2 : retour arrière exécuté une fois (durée notée), exposition réseau vérifiée depuis une autre machine, chaque panne simulée vue sur Telegram en moins de 15 minutes
12. Phase 0 : 30 verdicts hors alertes. Phase 1 : à 100 verdicts dont 20 corrections, `python -m src.evaluation run`, puis décision sur le few-shot et le repli Gemini
13. Ensuite seulement : le reste de la Phase 2 (table `threads`, `/pending`, relances)
