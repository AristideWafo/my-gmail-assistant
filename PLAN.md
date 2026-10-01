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
P0 ──► P2 ──► P3 ──► P4 ──► P6
 │             │      │
 │             └──────┴──► P5
 └──► P1   (piste parallèle, rythmée par les données)
```

---

## Phase 0 — Feedback élargi et socle de commandes

**Pourquoi** : seules les alertes urgentes peuvent être notées. Les erreurs coûteuses (mail important étiqueté bas ou archivé) sont invisibles.

**Coût** : moyen.

- ⬜ Migrations de schéma versionnées (`PRAGMA user_version`) ; aujourd'hui seul `CREATE TABLE IF NOT EXISTS` existe, et chaque phase suivante ajoute des tables
- ⬜ `CommandEvent` dans `src/domain`, analyse des messages `/commande` dans `src/gateways/telegram_bot.py`, routeur dans `src/interactions/`
- ⬜ Colonne `decisions.source` (`rule` / `jev` / `heuristic`), portée par `TriageResult`
- ⬜ Verdicts `missed_urgent` (aurait dû alerter) et `wrong_archive` (archivé à tort) ; colonne `feedback.origin` (`alert` / `review`)
- ⬜ `/review [n]` : échantillon stratifié sur 7 jours, jamais déjà noté, hors décisions de règle. Priorité à la route `reject`, puis aux `label` proches du seuil de confiance. `n` = 5 par défaut, 10 max
- ⬜ Boutons selon la route : `label` → `[OK] [Urgent raté] [Spam]` ; `reject` → `[OK] [À garder] [Urgent raté]`
- ⬜ `/stats` : précision par route et par source, lue dans SQLite ; `feedback_total{verdict, route}`
- ⬜ `missed_urgent` ajouté à `_VERDICT_CORRECTIONS` (`high`, catégorie inchangée). `wrong_archive` est mesuré mais pas injecté en few-shot : la bonne catégorie n'est pas connue

**DoD**
- `/review` et `/stats` documentés dans le README
- Un verdict posé via `/review` est stocké au même format que ceux des alertes et distingué par `origin`
- Au moins 30 verdicts hors alertes en base, avec le taux de `missed_urgent` et de `wrong_archive` par route
- Les tests du flux de feedback et de réponse existants passent sans modification de leurs assertions

---

## Phase 1 — Fiabilité et coût du triage (piste parallèle)

**Pourquoi** : c'est le levier le moins cher sur les erreurs de classification, mais il se décide sur des données, pas sur une impression.

**Coût** : moyen, étalé dans le temps.

- ⬜ Vérifier en direct que JEV accepte `state.examples` (un appel avec une vraie clé)
- ⬜ `MailProvider.fetch_message(id)` : le store ne garde que 300 caractères d'extrait, le rejeu a besoin du mail complet
- ⬜ Banc d'évaluation hors ligne (`python -m src.triage.eval`) : rejoue les mails notés sur plusieurs variantes de classifieur, sort la matrice de confusion par urgence et par route, et le nombre d'appels
- ⬜ Séparation temporelle obligatoire : exemples few-shot tirés des corrections antérieures à une date T, évaluation sur les verdicts postérieurs. Sans cela le few-shot est évalué sur ses propres exemples
- ⬜ Activation de `JEV_FEW_SHOT_ENABLED` si le banc montre un gain
- ⬜ Règles en fichier de config (`TRIAGE_RULES_PATH`) : expéditeur **et** objet, urgence et catégorie par règle. Candidats tirés du store : expéditeurs à fort volume toujours classés pareil
- ⬜ Liste VIP par adresse exacte, forçant `high`. Appliquée seulement si l'en-tête `Authentication-Results` indique `dmarc=pass` : le champ `From` est falsifiable
- ⬜ Label `source` sur `triage_confidence` et sur le panneau Grafana existant
- ⬜ Décision documentée sur un repli Gemini pour les classifications à basse confiance, avec un seuil lu sur le banc

**Attention** : une règle sur « Run failed » ne doit pas écraser une panne de production, que `URGENCIES` classe `high`. La règle porte donc sur le dépôt, pas sur le seul motif.

**DoD**
- Banc exécutable en une commande, résultat reproductible sur le même jeu
- Conclusion few-shot tirée d'au moins 100 verdicts dont 20 corrections ; en dessous, la décision reste ouverte
- Baisse mesurée des appels JEV sur les expéditeurs couverts par les règles (`decisions.source`)
- Un expéditeur VIP testé de bout en bout, plus un test de `From` falsifié refusé
- Décision repli Gemini écrite dans ce fichier, chiffres à l'appui

---

## Phase 2 — Threads en attente

**Pourquoi** : chaque mail est traité isolément. Rien ne dit ce qui attend une action de ta part ni ce qui attend une réponse de l'autre côté.

**Coût** : moyen à élevé.

- ⬜ Table `threads` : interlocuteur, état, dernière entrée, dernière sortie, date de relance, raison de clôture. Les threads ouverts sont exclus de la purge à 90 jours
- ⬜ `MailProvider` : lecture d'un thread en métadonnées (qui a écrit en dernier, labels) et liste des threads envoyés récents. Indispensable : une réponse faite depuis Gmail doit être vue
- ⬜ Question JEV `needs_reply` ajoutée à l'appel de classification existant, pour les catégories `personnel` / `mise_en_relation` / `offre_emploi` et les expéditeurs non automatisés. À vérifier : coût par question supplémentaire
- ⬜ Dérivation d'état déterministe : dernier message de l'autre + `needs_reply` → `waiting_for_me` ; dernier message de toi → `waiting_for_them` ; sinon clos
- ⬜ Rafraîchissement à cadence réduite (15 min) et à la demande, pas à chaque cycle de polling
- ⬜ `/pending` avec boutons `[Fait] [Ignorer] [Relancer]`. `[Ignorer]` sert aussi de verdict sur `needs_reply`
- ⬜ Relance : après `FOLLOW_UP_AFTER_DAYS` sans réponse, brouillon Gemini proposé via le flux `[Envoyer]/[Annuler]` existant. Une seule proposition par thread

**DoD**
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

**DoD**
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

**DoD**
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

- **Sauvegarde SQLite toujours absente.** La base va porter les threads, les items et les actions en attente : sa perte coûtera bien plus qu'aujourd'hui. À traiter avant la Phase 2
- Un seul poller par token Telegram : n'activer `TELEGRAM_INBOUND_ENABLED` que sur une instance
- Few-shot : un objet reste du texte choisi par l'expéditeur, rejoué dans chaque classification
- Nouveau consentement OAuth en Phase 4 : l'ancien refresh token ne porte pas le scope Calendar, prévoir la bascule
- Lecture des threads : un appel Gmail par thread ouvert et par rafraîchissement ; borner le nombre de threads suivis
- Fatigue de notification : trois digests, briefs et relances s'ajoutent aux alertes. Le plafond de l'invariant 5 doit exister dès la Phase 3
- `main.py` et `ApplicationContext` grossissent à chaque phase : l'extraction prévue en Phase 3 ne doit pas être repoussée

---

## Prochaine étape

Phase 0, en quatre PR :

1. Migrations de schéma + colonnes `decisions.source` et `feedback.origin`
2. `CommandEvent` + routeur de commandes
3. Verdicts étendus + `/review`
4. `/stats` + mise à jour README et dashboard

En parallèle, sans code : vérifier `state.examples` contre l'API JEV, et mettre en place la sauvegarde du volume `assistant-data`.
