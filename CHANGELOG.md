# CHANGELOG


## v0.16.0 (2026-10-05)

### Features

- **evaluation**: Measure shorter bodies on real long mails in the lab
  ([#81](https://github.com/AristideWafo/my-gmail-assistant/pull/81),
  [`5b67afe`](https://github.com/AristideWafo/my-gmail-assistant/commit/5b67afe0dac79bec96275103b6092fade8a62200))

* docs: read the lab misroutes by verdict

With verdicts shown, the nine mails the production questions misroute are all corrections replayed
  identically: seven missed alerts, one wrong archive, one mail kept that should have been archived.
  No false alert.

The plan now puts widening the "high" definition first in L4, after settling what the missed-urgent
  verdict is meant to say, and corrects the earlier reading of the meeting question.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

* feat(feedback): tell "put it forward" apart from "should have alerted"

/review had a single button, "Urgent raté", for every mail that deserved more than a label. Asked
  what he meant by it, the user answered "put it forward", not "ring". The new verdict
  missed_important ([À voir]) carries that meaning; missed_urgent keeps the meaning of an immediate
  alert.

Migration 4 turns the review verdicts already given with the single button into missed_important, so
  few-shot stops teaching the classifier that those mails are urgent. The previous state stays in
  the pre-v4 copy.

A sender whose archived mail was wanted back under any of the three verdicts is no longer proposed
  for unsubscription.

* feat(triage): prepare a reply draft whenever one is expected, urgent or not

Preparing a reply and notifying are now two separate decisions: the Telegram alert still depends on
  urgency alone, the draft on whether a reply is expected. An urgent mail was drafted only for three
  categories; with NEEDS_REPLY_ENABLED it is also drafted, and labeled Assistant/A_repondre, when
  JEV says a person expects a reply.

The category rule stays next to the model's answer, so a mail decided without the question (rule,
  VIP, fallback) or just under the threshold keeps the draft it always had.

Drafts are now written for more kinds of mail, invitations included, so the prompt forbids deciding
  for the author: no acceptance, refusal, date or amount; a holding reply when the answer is not in
  the mail.

* feat(triage): ask the attention questions in observation mode

Seven of the nine mistakes found by the lab were mails the user wanted put forward: an event he is
  registered for, a planned outage, something to do before a date, a person waiting.
  ATTENTION_MODE=shadow adds three narrow yes/no questions to the JEV call (personal_event,
  service_change, personal_deadline) and asks whether a reply is expected. The answers are stored in
  decisions.signals and counted; nothing else changes yet.

Narrow questions rather than one broad one, so each can be measured and dropped on its own. Bulk
  categories are gated out; automated senders are not, since outage notices and platform-relayed
  messages come from them.

`python -m src.evaluation attention` reads the stored answers and says how many mails a day would be
  put forward. The lab gets an `attention` variant and 18 more test mails.

* feat(triage): put forward the mails worth seeing, without a notification (#64)

* fix(triage): treat French no-reply addresses as automated senders

A reply draft was written to ne-pas-repondre@ addresses, and their repeated alerts were not grouped.

* feat(triage): put forward the mails worth seeing, without a notification

ATTENTION_MODE=on acts on the attention answers. A mail that is not urgent and has at least one
  reason gets the Gmail label Assistant/A_voir and stays in the inbox even when the low-urgency
  notification rule would have archived it: event tickets and works notices were archived by that
  rule. Bulk categories are archived as before; urgent mails are alerted as before.

The decision is stored in decisions.put_forward, for the daily list that comes next.

---------

Co-authored-by: Claude Opus 5.5 <noreply@anthropic.com>

* feat(chat): send a silent daily list of the mails put forward

A Gmail label alone shows little, since a processed mail is marked read: an event reminder for
  tomorrow would stay unseen. Once a day, at ATTENTION_LIST_HOUR in TIMEZONE, the assistant sends
  without sound the mails put forward since the previous list, at most five, each with its reason
  and [Vu] / [Pas utile]. Nothing is sent on a day with nothing new. /avoir lists on demand what
  still waits over seven days.

A mail leaves the list once rated or once it left the Gmail inbox. [Pas utile] is stored as the
  verdict false_important, the only source of negative examples for putting forward.

DailyJob keeps the last run day in kv_state, so a restart neither repeats nor skips the list; it is
  the scheduler the recaps will reuse.

* fix(gmail): apply every label of a mail in the single call that commits it

Adding a label also removes UNREAD, which is what stops a mail from being fetched again. The reply
  and put-forward labels were added in calls of their own, before the category or urgent label: once
  one of them had succeeded the mail was no longer unread, so a failure of the last call left it
  without its main label and never retried.

label_message now takes every label of the mail and sends one change. A failure leaves the mail
  untouched and unread for the next cycle; the draft and alert guards already keep that retry from
  drafting or alerting twice.

* feat(evaluation): measure shorter bodies on real long mails in the lab

The body is about 70 % of what a real mail costs to classify, and production sends its first 1000
  words. The lab can now say what a shorter body would change:

- `--source recent` takes the latest long mails, which have no verdict. A first pass of `current`
  gives each mail a reference route; every variant is scored on how often it agrees with it, and the
  `current` row is the noise between two identical calls. - Cut variants keep the start, or the
  start and the end around a visible mark: head-700-tail-300, head-150, head-100-tail-50. - The lab
  fetches real mails uncut (fetch_message(full_body=True)), so a variant that keeps the end has an
  end to keep. Production is unchanged.


## v0.15.1 (2026-10-05)

### Bug Fixes

- **gmail**: Apply every label of a mail in the single call that commits it
  ([#80](https://github.com/AristideWafo/my-gmail-assistant/pull/80),
  [`21f0bed`](https://github.com/AristideWafo/my-gmail-assistant/commit/21f0bed37dfc095456e357812fd4a2f741a9b69e))

* docs: read the lab misroutes by verdict

With verdicts shown, the nine mails the production questions misroute are all corrections replayed
  identically: seven missed alerts, one wrong archive, one mail kept that should have been archived.
  No false alert.

The plan now puts widening the "high" definition first in L4, after settling what the missed-urgent
  verdict is meant to say, and corrects the earlier reading of the meeting question.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

* feat(feedback): tell "put it forward" apart from "should have alerted"

/review had a single button, "Urgent raté", for every mail that deserved more than a label. Asked
  what he meant by it, the user answered "put it forward", not "ring". The new verdict
  missed_important ([À voir]) carries that meaning; missed_urgent keeps the meaning of an immediate
  alert.

Migration 4 turns the review verdicts already given with the single button into missed_important, so
  few-shot stops teaching the classifier that those mails are urgent. The previous state stays in
  the pre-v4 copy.

A sender whose archived mail was wanted back under any of the three verdicts is no longer proposed
  for unsubscription.

* feat(triage): prepare a reply draft whenever one is expected, urgent or not

Preparing a reply and notifying are now two separate decisions: the Telegram alert still depends on
  urgency alone, the draft on whether a reply is expected. An urgent mail was drafted only for three
  categories; with NEEDS_REPLY_ENABLED it is also drafted, and labeled Assistant/A_repondre, when
  JEV says a person expects a reply.

The category rule stays next to the model's answer, so a mail decided without the question (rule,
  VIP, fallback) or just under the threshold keeps the draft it always had.

Drafts are now written for more kinds of mail, invitations included, so the prompt forbids deciding
  for the author: no acceptance, refusal, date or amount; a holding reply when the answer is not in
  the mail.

* feat(triage): ask the attention questions in observation mode

Seven of the nine mistakes found by the lab were mails the user wanted put forward: an event he is
  registered for, a planned outage, something to do before a date, a person waiting.
  ATTENTION_MODE=shadow adds three narrow yes/no questions to the JEV call (personal_event,
  service_change, personal_deadline) and asks whether a reply is expected. The answers are stored in
  decisions.signals and counted; nothing else changes yet.

Narrow questions rather than one broad one, so each can be measured and dropped on its own. Bulk
  categories are gated out; automated senders are not, since outage notices and platform-relayed
  messages come from them.

`python -m src.evaluation attention` reads the stored answers and says how many mails a day would be
  put forward. The lab gets an `attention` variant and 18 more test mails.

* feat(triage): put forward the mails worth seeing, without a notification (#64)

* fix(triage): treat French no-reply addresses as automated senders

A reply draft was written to ne-pas-repondre@ addresses, and their repeated alerts were not grouped.

* feat(triage): put forward the mails worth seeing, without a notification

ATTENTION_MODE=on acts on the attention answers. A mail that is not urgent and has at least one
  reason gets the Gmail label Assistant/A_voir and stays in the inbox even when the low-urgency
  notification rule would have archived it: event tickets and works notices were archived by that
  rule. Bulk categories are archived as before; urgent mails are alerted as before.

The decision is stored in decisions.put_forward, for the daily list that comes next.

---------

Co-authored-by: Claude Opus 5.5 <noreply@anthropic.com>

* feat(chat): send a silent daily list of the mails put forward

A Gmail label alone shows little, since a processed mail is marked read: an event reminder for
  tomorrow would stay unseen. Once a day, at ATTENTION_LIST_HOUR in TIMEZONE, the assistant sends
  without sound the mails put forward since the previous list, at most five, each with its reason
  and [Vu] / [Pas utile]. Nothing is sent on a day with nothing new. /avoir lists on demand what
  still waits over seven days.

A mail leaves the list once rated or once it left the Gmail inbox. [Pas utile] is stored as the
  verdict false_important, the only source of negative examples for putting forward.

DailyJob keeps the last run day in kv_state, so a restart neither repeats nor skips the list; it is
  the scheduler the recaps will reuse.

* fix(gmail): apply every label of a mail in the single call that commits it

Adding a label also removes UNREAD, which is what stops a mail from being fetched again. The reply
  and put-forward labels were added in calls of their own, before the category or urgent label: once
  one of them had succeeded the mail was no longer unread, so a failure of the last call left it
  without its main label and never retried.

label_message now takes every label of the mail and sends one change. A failure leaves the mail
  untouched and unread for the next cycle; the draft and alert guards already keep that retry from
  drafting or alerting twice.


## v0.15.0 (2026-10-05)

### Features

- **chat**: Send a silent daily list of the mails put forward
  ([#79](https://github.com/AristideWafo/my-gmail-assistant/pull/79),
  [`3c3ca40`](https://github.com/AristideWafo/my-gmail-assistant/commit/3c3ca40da8e6704366831ae4a778b2c32281081e))

* docs: read the lab misroutes by verdict

With verdicts shown, the nine mails the production questions misroute are all corrections replayed
  identically: seven missed alerts, one wrong archive, one mail kept that should have been archived.
  No false alert.

The plan now puts widening the "high" definition first in L4, after settling what the missed-urgent
  verdict is meant to say, and corrects the earlier reading of the meeting question.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

* feat(feedback): tell "put it forward" apart from "should have alerted"

/review had a single button, "Urgent raté", for every mail that deserved more than a label. Asked
  what he meant by it, the user answered "put it forward", not "ring". The new verdict
  missed_important ([À voir]) carries that meaning; missed_urgent keeps the meaning of an immediate
  alert.

Migration 4 turns the review verdicts already given with the single button into missed_important, so
  few-shot stops teaching the classifier that those mails are urgent. The previous state stays in
  the pre-v4 copy.

A sender whose archived mail was wanted back under any of the three verdicts is no longer proposed
  for unsubscription.

* feat(triage): prepare a reply draft whenever one is expected, urgent or not

Preparing a reply and notifying are now two separate decisions: the Telegram alert still depends on
  urgency alone, the draft on whether a reply is expected. An urgent mail was drafted only for three
  categories; with NEEDS_REPLY_ENABLED it is also drafted, and labeled Assistant/A_repondre, when
  JEV says a person expects a reply.

The category rule stays next to the model's answer, so a mail decided without the question (rule,
  VIP, fallback) or just under the threshold keeps the draft it always had.

Drafts are now written for more kinds of mail, invitations included, so the prompt forbids deciding
  for the author: no acceptance, refusal, date or amount; a holding reply when the answer is not in
  the mail.

* feat(triage): ask the attention questions in observation mode

Seven of the nine mistakes found by the lab were mails the user wanted put forward: an event he is
  registered for, a planned outage, something to do before a date, a person waiting.
  ATTENTION_MODE=shadow adds three narrow yes/no questions to the JEV call (personal_event,
  service_change, personal_deadline) and asks whether a reply is expected. The answers are stored in
  decisions.signals and counted; nothing else changes yet.

Narrow questions rather than one broad one, so each can be measured and dropped on its own. Bulk
  categories are gated out; automated senders are not, since outage notices and platform-relayed
  messages come from them.

`python -m src.evaluation attention` reads the stored answers and says how many mails a day would be
  put forward. The lab gets an `attention` variant and 18 more test mails.

* feat(triage): put forward the mails worth seeing, without a notification (#64)

* fix(triage): treat French no-reply addresses as automated senders

A reply draft was written to ne-pas-repondre@ addresses, and their repeated alerts were not grouped.

* feat(triage): put forward the mails worth seeing, without a notification

ATTENTION_MODE=on acts on the attention answers. A mail that is not urgent and has at least one
  reason gets the Gmail label Assistant/A_voir and stays in the inbox even when the low-urgency
  notification rule would have archived it: event tickets and works notices were archived by that
  rule. Bulk categories are archived as before; urgent mails are alerted as before.

The decision is stored in decisions.put_forward, for the daily list that comes next.

---------

Co-authored-by: Claude Opus 5.5 <noreply@anthropic.com>

* feat(chat): send a silent daily list of the mails put forward

A Gmail label alone shows little, since a processed mail is marked read: an event reminder for
  tomorrow would stay unseen. Once a day, at ATTENTION_LIST_HOUR in TIMEZONE, the assistant sends
  without sound the mails put forward since the previous list, at most five, each with its reason
  and [Vu] / [Pas utile]. Nothing is sent on a day with nothing new. /avoir lists on demand what
  still waits over seven days.

A mail leaves the list once rated or once it left the Gmail inbox. [Pas utile] is stored as the
  verdict false_important, the only source of negative examples for putting forward.

DailyJob keeps the last run day in kv_state, so a restart neither repeats nor skips the list; it is
  the scheduler the recaps will reuse.


## v0.14.0 (2026-10-05)

### Features

- **triage**: Put forward the mails worth seeing, without a notification
  ([#78](https://github.com/AristideWafo/my-gmail-assistant/pull/78),
  [`2eb4b81`](https://github.com/AristideWafo/my-gmail-assistant/commit/2eb4b81503e13405c23e10f34ea32f7854e8a864))

* docs: read the lab misroutes by verdict

With verdicts shown, the nine mails the production questions misroute are all corrections replayed
  identically: seven missed alerts, one wrong archive, one mail kept that should have been archived.
  No false alert.

The plan now puts widening the "high" definition first in L4, after settling what the missed-urgent
  verdict is meant to say, and corrects the earlier reading of the meeting question.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

* feat(feedback): tell "put it forward" apart from "should have alerted"

/review had a single button, "Urgent raté", for every mail that deserved more than a label. Asked
  what he meant by it, the user answered "put it forward", not "ring". The new verdict
  missed_important ([À voir]) carries that meaning; missed_urgent keeps the meaning of an immediate
  alert.

Migration 4 turns the review verdicts already given with the single button into missed_important, so
  few-shot stops teaching the classifier that those mails are urgent. The previous state stays in
  the pre-v4 copy.

A sender whose archived mail was wanted back under any of the three verdicts is no longer proposed
  for unsubscription.

* feat(triage): prepare a reply draft whenever one is expected, urgent or not

Preparing a reply and notifying are now two separate decisions: the Telegram alert still depends on
  urgency alone, the draft on whether a reply is expected. An urgent mail was drafted only for three
  categories; with NEEDS_REPLY_ENABLED it is also drafted, and labeled Assistant/A_repondre, when
  JEV says a person expects a reply.

The category rule stays next to the model's answer, so a mail decided without the question (rule,
  VIP, fallback) or just under the threshold keeps the draft it always had.

Drafts are now written for more kinds of mail, invitations included, so the prompt forbids deciding
  for the author: no acceptance, refusal, date or amount; a holding reply when the answer is not in
  the mail.

* feat(triage): ask the attention questions in observation mode

Seven of the nine mistakes found by the lab were mails the user wanted put forward: an event he is
  registered for, a planned outage, something to do before a date, a person waiting.
  ATTENTION_MODE=shadow adds three narrow yes/no questions to the JEV call (personal_event,
  service_change, personal_deadline) and asks whether a reply is expected. The answers are stored in
  decisions.signals and counted; nothing else changes yet.

Narrow questions rather than one broad one, so each can be measured and dropped on its own. Bulk
  categories are gated out; automated senders are not, since outage notices and platform-relayed
  messages come from them.

`python -m src.evaluation attention` reads the stored answers and says how many mails a day would be
  put forward. The lab gets an `attention` variant and 18 more test mails.

* feat(triage): put forward the mails worth seeing, without a notification (#64)

* fix(triage): treat French no-reply addresses as automated senders

A reply draft was written to ne-pas-repondre@ addresses, and their repeated alerts were not grouped.

* feat(triage): put forward the mails worth seeing, without a notification

ATTENTION_MODE=on acts on the attention answers. A mail that is not urgent and has at least one
  reason gets the Gmail label Assistant/A_voir and stays in the inbox even when the low-urgency
  notification rule would have archived it: event tickets and works notices were archived by that
  rule. Bulk categories are archived as before; urgent mails are alerted as before.

The decision is stored in decisions.put_forward, for the daily list that comes next.

---------

Co-authored-by: Claude Opus 5.5 <noreply@anthropic.com>


## v0.13.0 (2026-10-05)

### Documentation

- Read the lab misroutes by verdict
  ([#60](https://github.com/AristideWafo/my-gmail-assistant/pull/60),
  [`276a368`](https://github.com/AristideWafo/my-gmail-assistant/commit/276a36885a257581d45deb28e340894c0ac65293))

With verdicts shown, the nine mails the production questions misroute are all corrections replayed
  identically: seven missed alerts, one wrong archive, one mail kept that should have been archived.
  No false alert.

The plan now puts widening the "high" definition first in L4, after settling what the missed-urgent
  verdict is meant to say, and corrects the earlier reading of the meeting question.

Co-authored-by: Claude Opus 5.5 <noreply@anthropic.com>

### Features

- **feedback**: Tell "put it forward" apart from "should have alerted"
  ([#61](https://github.com/AristideWafo/my-gmail-assistant/pull/61),
  [`3865dc7`](https://github.com/AristideWafo/my-gmail-assistant/commit/3865dc7113e24a57c63d30802dead844c541c9a5))

/review had a single button, "Urgent raté", for every mail that deserved more than a label. Asked
  what he meant by it, the user answered "put it forward", not "ring". The new verdict
  missed_important ([À voir]) carries that meaning; missed_urgent keeps the meaning of an immediate
  alert.

Migration 4 turns the review verdicts already given with the single button into missed_important, so
  few-shot stops teaching the classifier that those mails are urgent. The previous state stays in
  the pre-v4 copy.

A sender whose archived mail was wanted back under any of the three verdicts is no longer proposed
  for unsubscription.

Co-authored-by: Claude Opus 5.5 <noreply@anthropic.com>

- **triage**: Ask the attention questions in observation mode
  ([#63](https://github.com/AristideWafo/my-gmail-assistant/pull/63),
  [`3d45565`](https://github.com/AristideWafo/my-gmail-assistant/commit/3d4556551443ada2b708f3070a2754d4328fbb4b))

Seven of the nine mistakes found by the lab were mails the user wanted put forward: an event he is
  registered for, a planned outage, something to do before a date, a person waiting.
  ATTENTION_MODE=shadow adds three narrow yes/no questions to the JEV call (personal_event,
  service_change, personal_deadline) and asks whether a reply is expected. The answers are stored in
  decisions.signals and counted; nothing else changes yet.

Narrow questions rather than one broad one, so each can be measured and dropped on its own. Bulk
  categories are gated out; automated senders are not, since outage notices and platform-relayed
  messages come from them.

`python -m src.evaluation attention` reads the stored answers and says how many mails a day would be
  put forward. The lab gets an `attention` variant and 18 more test mails.

Co-authored-by: Claude Opus 5.5 <noreply@anthropic.com>

- **triage**: Prepare a reply draft whenever one is expected, urgent or not
  ([#62](https://github.com/AristideWafo/my-gmail-assistant/pull/62),
  [`c5c726c`](https://github.com/AristideWafo/my-gmail-assistant/commit/c5c726c2c1ce2b01da83bab19ef3001465923eef))

Preparing a reply and notifying are now two separate decisions: the Telegram alert still depends on
  urgency alone, the draft on whether a reply is expected. An urgent mail was drafted only for three
  categories; with NEEDS_REPLY_ENABLED it is also drafted, and labeled Assistant/A_repondre, when
  JEV says a person expects a reply.

The category rule stays next to the model's answer, so a mail decided without the question (rule,
  VIP, fallback) or just under the threshold keeps the draft it always had.

Drafts are now written for more kinds of mail, invitations included, so the prompt forbids deciding
  for the author: no acceptance, refusal, date or amount; a holding reply when the answer is not in
  the mail.

Co-authored-by: Claude Opus 5.5 <noreply@anthropic.com>


## v0.12.0 (2026-10-01)

### Documentation

- Record the first lab run on real rated mails
  ([#57](https://github.com/AristideWafo/my-gmail-assistant/pull/57),
  [`7b4640b`](https://github.com/AristideWafo/my-gmail-assistant/commit/7b4640b4557cb4ab685c3fa30316299959b7aadf))

* docs: record the first lab run on real rated mails

16 rated mails were replayed on the VPS. The sample is too small to rank the variants, but it shows
  that real mails cost 2.6 times the test corpus in tokens, that the meeting and deadline questions
  fire on webinars, and that the direct-action variant brings nothing.

Proposed decisions for L2, L3 and L4 are written down, pending confirmation.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

* docs: record the second lab run on real rated mails

With 37 rated mails replayed, direct-action is behind the production questions (24 against 28), the
  deadline question only fires on newsletters and events, and one of the production misroutes is an
  archive the verdict disagrees with, which L4 now measures first.

---------

Co-authored-by: Claude Opus 5.5 <noreply@anthropic.com>

### Features

- **evaluation**: Show the verdict of rated mails in the lab report
  ([#58](https://github.com/AristideWafo/my-gmail-assistant/pull/58),
  [`605f3df`](https://github.com/AristideWafo/my-gmail-assistant/commit/605f3dfc9bfe99b55dda69b2fb31474edde63033))

* feat(evaluation): show the verdict of rated mails in the lab report

A misrouted rated mail was listed with its sender and subject only, so the report could not tell a
  mistake JEV repeats after being corrected from a departure from a decision the user had validated.
  Each rated mail now carries its verdict and the route it originally took.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

* docs: say what the misrouted count cannot tell without the verdict

---------

Co-authored-by: Claude Opus 5.5 <noreply@anthropic.com>


## v0.11.0 (2026-10-01)

### Features

- **evaluation**: Add a lab to compare JEV question variants
  ([#56](https://github.com/AristideWafo/my-gmail-assistant/pull/56),
  [`3dce6b7`](https://github.com/AristideWafo/my-gmail-assistant/commit/3dce6b77602a1dc828105353a43baf3eb1dce3cf))

Checking an idea about the questions or the routing meant waiting for weeks of verdicts. `python -m
  src.evaluation lab` sends live JEV calls for each variant and reports correct routes per run,
  routes that change between runs, tokens, latency and the misrouted mails.

It runs on a versioned corpus of 50 invented mails with their acceptable routes and expected yes/no
  answers, or on the real rated mails, where a verdict is the constraint and mails decided by a rule
  are left out. The number of calls is printed first and capped by --max-calls.

Variants shipped: the production request, a direct alert/keep/archive question, and the production
  request plus candidate yes/no questions. JevClassifier exposes ask, build_request, parse_answers
  and parse_probability so the lab sends exactly what production sends.

Co-authored-by: Claude Opus 5.5 <noreply@anthropic.com>


## v0.10.0 (2026-10-01)

### Documentation

- Plan a JEV question lab and the changes that depend on it
  ([#54](https://github.com/AristideWafo/my-gmail-assistant/pull/54),
  [`67ac14f`](https://github.com/AristideWafo/my-gmail-assistant/commit/67ac14fd615658ea752ad09fe55476b3cbf81b62))

A first series of live JEV calls on test mails settled several questions about cost, stability and
  alternative question sets, but on mails written for the test. Phase 1bis records those results and
  plans the lab as a repository tool (L1) that replays the real rated mails.

Business questions (L2), head-and-tail truncation (L3) and three routing rules (L4) are planned but
  wait for L1 to be validated.

Co-authored-by: Claude Opus 5.5 <noreply@anthropic.com>

### Features

- **triage**: Draft a reply for non-urgent mails that expect one
  ([#53](https://github.com/AristideWafo/my-gmail-assistant/pull/53),
  [`ef99444`](https://github.com/AristideWafo/my-gmail-assistant/commit/ef99444f2762b7f9bf5a873dbb7bde6e191cae90))

Reply drafts were only written on the urgent route, so a personal mail asking a question without
  time pressure got neither a draft nor a signal.

The JEV classification call now carries a third question, needs_reply, when NEEDS_REPLY_ENABLED is
  set. A labeled mail whose probability reaches NEEDS_REPLY_THRESHOLD gets a Gmail draft and the
  label Assistant/A_repondre, with no Telegram message. The route stays "label", so stats, review
  and the evaluation harness are unaffected.

Bulk, scam and automated mail is excluded by category and sender, since it asks to be answered too.
  The probability is stored with each decision (schema version 3) and stays out of the routing
  confidence. With the flag off, the request sent to JEV is unchanged.

Co-authored-by: Claude Opus 5.5 <noreply@anthropic.com>


## v0.9.0 (2026-10-01)

### Features

- **telegram**: Route slash commands from the chat
  ([#50](https://github.com/AristideWafo/my-gmail-assistant/pull/50),
  [`184a2cb`](https://github.com/AristideWafo/my-gmail-assistant/commit/184a2cbfb73ecf32bfa9f5db9aa021aaaec52323))

* feat(telegram): route slash commands from the chat

The bot only understood button presses and replies to an alert, so there was no way to ask it for
  anything.

A message starting with "/" that is not a reply now becomes a CommandEvent ("/name@bot args" as
  Telegram writes it in groups is accepted). A reply to an alert stays mail text even when it starts
  with a slash, so a draft can never be swallowed as a command.

CommandRouter maps names to handlers, answers /help and /start with the list, and reports unknown or
  failing commands without raising into the listener. Updates are delivered at-least-once, so each
  command is claimed in kv_state before it runs and a redelivery is dropped.

Outcomes are counted in chat_commands_total{command,status}; unknown names share one label value
  since they are user-typed.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

* feat(feedback): review mails that were archived or labeled without an alert (#51)

* feat(feedback): review mails that were archived or labeled without an alert

Only urgent alerts could be rated, so the costly mistakes (an important mail labeled low or archived
  silently) were invisible, and "Faux-Urgent" makes no sense on a mail that was never alerted.

/review [n] samples recent unrated decisions the classifier made without alerting: half archived
  mails in random order, half the labeled ones it was least sure about, one per sender, rule
  decisions excluded. Buttons depend on what was done with the mail: [OK] [A garder] [Urgent rate]
  for an archived one, [OK] [Urgent rate] [Spam] for a labeled one.

Two verdicts are added, missed_urgent and wrong_archive. Review verdicts are stored like alert ones,
  tagged origin=review, and feedback_total is now labelled by verdict and route.

missed_urgent feeds the JEV few-shot examples (urgency high, category kept). wrong_archive does not,
  as it does not say what the category should have been; recent_corrections can now be limited to
  the usable verdicts so those rows do not use up example slots.

* feat(feedback): add /stats to read triage precision from the verdicts (#52)

Verdicts were only visible as Prometheus counters, which reset on restart and say nothing about
  which stage made the decision.

/stats answers from SQLite with the share of correct decisions and the kinds of mistakes, per
  decision (alerted, labeled, archived) and per source (jev, rule, heuristic), plus the number of
  decisions per source over 30 days, which is what the Phase 1 rule work needs to show fewer
  classifier calls.

The Grafana feedback panel now splits by route and verdict.

Co-authored-by: Claude Opus 5.5 <noreply@anthropic.com>

---------


## v0.8.0 (2026-10-01)

### Documentation

- Add business features and a stabilisation track to the plan
  ([#38](https://github.com/AristideWafo/my-gmail-assistant/pull/38),
  [`32ea9b5`](https://github.com/AristideWafo/my-gmail-assistant/commit/32ea9b52ead9fc3e15b85fc3bb0d4157fe1f7609))

Spread the business features over the phases that already build what they need (incident grouping,
  collective reply tracking, commitment tracking, renewals, quick capture), and add a stabilisation
  section: S1 (backup and silent-outage alert) blocks Phase 0, S2 hardens the app before Phase 2, S3
  is the gate applied between phases.

Co-authored-by: Claude Opus 5.5 <noreply@anthropic.com>

- Add new scope
  ([`a3bab33`](https://github.com/AristideWafo/my-gmail-assistant/commit/a3bab33ce48ed9a7fe8432f68045500476b781b8))

### Features

- **alerts**: Fold repeated automated alerts into the first one
  ([#47](https://github.com/AristideWafo/my-gmail-assistant/pull/47),
  [`54cc50a`](https://github.com/AristideWafo/my-gmail-assistant/commit/54cc50a26d506cb319287b3c98d872d5de4c42da))

Repeated CI failures were already deduplicated for 30 minutes, but silently: the first alert stayed
  as it was, so nothing showed whether the failure happened once or kept happening.

A repeat now updates the first alert in place with the number of occurrences, the elapsed time and
  the latest subject, without a new notification. The feedback buttons are sent again with the edit,
  since Telegram drops the keyboard of an edited message otherwise.

Grouping stays keyed on the sender and the normalised subject, which keeps the workflow name:
  another workflow of the same repository, such as a production deploy, still alerts at once. A
  failed update is logged and counted, never raised, as the first alert already reached the user.

AlertChannel gains update(); Telegram implements it with editMessageText, Discord webhooks cannot
  edit and are never asked to.

Co-authored-by: Claude Opus 5.5 <noreply@anthropic.com>

- **health**: Tell the user when mail fetching keeps failing
  ([#40](https://github.com/AristideWafo/my-gmail-assistant/pull/40),
  [`cc60b95`](https://github.com/AristideWafo/my-gmail-assistant/commit/cc60b95686e123c19ffb6818dcaf6a17ba9aec3f))

poll_once deliberately keeps the heartbeat alive when fetch_unread fails, so a Gmail outage does not
  restart-loop the container. The side effect is that a revoked refresh token stops all triage with
  no signal at all.

OutageNotifier now sends one chat message once fetching has failed for POLL_FAILURE_ALERT_MINUTES
  (default 10, 0 disables) and a second one, with the outage duration, when it works again. An
  undelivered message is retried every cycle, and the recovery is announced even if the outage
  message never got through, since the chat is often down for the same reason the mail is. The
  message carries the error type and HTTP status only, reusing the secret-free description of the
  startup checks.

Also stop reporting an exhausted 429 backoff as an empty inbox: _execute_with_backoff returned None,
  which became [] and was counted as a successful poll. It now re-raises on the last attempt.

AlertGateway.send_text returns whether a channel delivered the message. Failed cycles are counted in
  poll_failures_total.

Co-authored-by: Claude Opus 5.5 <noreply@anthropic.com>

- **storage**: Back up the decision store daily with rotation
  ([#39](https://github.com/AristideWafo/my-gmail-assistant/pull/39),
  [`e659248`](https://github.com/AristideWafo/my-gmail-assistant/commit/e65924836df12184a0a66411932d46de26cb0ddb))

The store now holds state whose loss is costly (verdicts, alert dedup) and the next phases add
  schema migrations on top of it, so a restorable copy has to exist first.

With BACKUP_DIR set, the app copies the database at startup and once a day to
  assistant-YYYY-MM-DD.db using SQLite's online backup, keeps the BACKUP_KEEP most recent files and
  only promotes a copy that passes PRAGMA integrity_check, so a failed run never replaces the last
  good one. Backups run before the prune so the copy still holds what is about to be deleted.
  docker-compose mounts a dedicated assistant-backups volume.

Outcomes are exposed as backup_last_success_timestamp_seconds and backup_failures_total. The README
  documents the restore procedure.

Co-authored-by: Claude Opus 5.5 <noreply@anthropic.com>

- **storage**: Version the schema and record which stage made each decision
  ([#41](https://github.com/AristideWafo/my-gmail-assistant/pull/41),
  [`5029c66`](https://github.com/AristideWafo/my-gmail-assistant/commit/5029c6637a11aa1c5844f88c2638341547a5c9d8))

The store only ran CREATE TABLE IF NOT EXISTS, so no column could ever be added to an existing
  database, and every next phase needs new tables.

Migrations are now an append-only list applied according to PRAGMA user_version, one transaction per
  step with the version bump inside it. Before upgrading a database that already holds data, the
  previous state is saved next to it as assistant.db.pre-v<N>. A database written by a newer build
  is refused instead of being opened.

Version 2 adds decisions.source and feedback.origin. TriageResult carries the source ("rule", "jev",
  "heuristic"), which the review sampling and the confidence metrics need to tell classifier
  decisions from rule decisions.

Co-authored-by: Claude Opus 5.5 <noreply@anthropic.com>

- **triage**: Add an offline evaluation harness over rated mails
  ([#45](https://github.com/AristideWafo/my-gmail-assistant/pull/45),
  [`e25296e`](https://github.com/AristideWafo/my-gmail-assistant/commit/e25296e84125a3b8b90a45c353964b73727ca455))

Comparing one week with few-shot to one week without proves nothing at a personal mail volume: a
  handful of alerts per week, and a different mix of mails each time.

python -m src.evaluation run replays the mails the user gave a verdict on through the rules and each
  classifier variant (heuristic, jev, jev+few-shot) and prints how often the resulting route agrees
  with the verdict, overall and per verdict, with the number of classifier calls. Every variant sees
  the same cases.

Corrections given before the split are only used as few-shot examples and are never scored,
  otherwise the few-shot variant would be graded on its own examples. Below 100 cases including 20
  corrections the output is flagged as not conclusive.

Also: check-examples makes one live JEV call carrying state.examples, the verification that was
  still missing before enabling few-shot, and candidates lists senders JEV always classifies the
  same way.

Supporting changes: MailProvider.fetch_message (the store only keeps a 300-character excerpt),
  route_for extracted from the workflow so the harness routes exactly as production does, and two
  store queries.

Co-authored-by: Claude Opus 5.5 <noreply@anthropic.com>

- **triage**: Load rules and VIP senders from a file
  ([#46](https://github.com/AristideWafo/my-gmail-assistant/pull/46),
  [`2a33bac`](https://github.com/AristideWafo/my-gmail-assistant/commit/2a33bac1d20f313f5a11d125c609f55b70548cdf))

The deterministic rules were three hard-coded sender patterns, so every other repetitive sender went
  through a JEV call, and nothing could force an alert for a sender that matters regardless of the
  classifier.

TRIAGE_RULES_PATH points at a TOML file read at startup:

- [[rules]] entries match on the sender and, optionally, the subject, and are checked before the
  built-in rules. A subject pattern lets a rule cover one repository's CI noise without hiding a
  production failure. - vip lists exact addresses whose mail always alerts. A VIP is honoured only
  when Gmail's own Authentication-Results header reports dmarc=pass: the From address alone is
  trivially forged. The header is the topmost one carrying Gmail's id; the previous header parsing
  kept the last occurrence, which a sender controls.

An invalid file stops startup naming the offending rule. The evaluation harness uses the same rule
  set as production.

The taxonomy moves out of the JEV adapter so rules can validate against it. triage_confidence gains
  a source label and the Grafana confidence panel now follows JEV decisions only, instead of mixing
  in rules (always 1.0) and the heuristic (fixed values).

Co-authored-by: Claude Opus 5.5 <noreply@anthropic.com>

- **triage**: Offer to unsubscribe from senders whose mail is always archived
  ([#48](https://github.com/AristideWafo/my-gmail-assistant/pull/48),
  [`a61bbda`](https://github.com/AristideWafo/my-gmail-assistant/commit/a61bbdaf840b2097469484c65a1d56d99c6534fc))

Some senders are archived every single time. Each of their mails still costs a classification, and
  the user never asked to be rid of them.

With UNSUBSCRIBE_PROPOSALS_ENABLED (off by default), once a sender has had UNSUBSCRIBE_MIN_ARCHIVED
  mails archived over 30 days and none kept or marked as wrongly archived, the assistant offers once
  to unsubscribe, with [Se desabonner] [Garder] buttons.

Nothing is sent without the button, pressed on the very message that made the offer, and at most
  once: the request is claimed before it is sent, like the draft send. The link stays in the store
  and never travels in the callback data.

Only RFC 8058 one-click is supported: List-Unsubscribe-Post present, an https link, and a mail that
  passes DMARC, since the sender writes these headers. Because the sender also chooses where the
  request goes, the HTTP adapter refuses a host that does not resolve only to public addresses,
  connects to the address it checked rather than resolving again, and does not follow redirects.
  Errors never include the link, which usually holds the subscriber's token.

The request goes through a new Unsubscriber port, selected by UNSUBSCRIBER.

Co-authored-by: Claude Opus 5.5 <noreply@anthropic.com>


## v0.7.0 (2026-09-29)

### Documentation

- Document the Phase 3 feedback loop and chat replies
  ([#35](https://github.com/AristideWafo/my-gmail-assistant/pull/35),
  [`28594db`](https://github.com/AristideWafo/my-gmail-assistant/commit/28594db0ad23464511ce297930a8655a9a43fd1e))

Co-authored-by: Claude Opus 5.5 <noreply@anthropic.com>

### Features

- Wire feedback loop and Telegram replies into the assistant
  ([#34](https://github.com/AristideWafo/my-gmail-assistant/pull/34),
  [`812726e`](https://github.com/AristideWafo/my-gmail-assistant/commit/812726e413d12d38ea7d8e41ce541276cd8c5d76))

Urgent Telegram alerts carry [Valider]/[Faux-Urgent]/[Faux-Spam] buttons when the inbound listener
  is enabled; verdicts land in the SQLite store and can feed JEV few-shot examples. Replying to an
  alert creates a threaded Gmail draft that is only sent after an explicit [Envoyer] press, claimed
  before the Gmail call so it is sent at most once.

Telegram delivery now goes through TelegramBot, so the bot token no longer leaks into logs;
  alerted-mail dedup is persisted and survives restarts.

Co-authored-by: Claude Opus 5.5 <noreply@anthropic.com>

- **gmail**: Thread reply drafts and allow sending a draft
  ([#31](https://github.com/AristideWafo/my-gmail-assistant/pull/31),
  [`3909145`](https://github.com/AristideWafo/my-gmail-assistant/commit/3909145b9b4625094f8a27ca41324de4bf1472fb))

Co-authored-by: Claude Opus 5.5 <noreply@anthropic.com>

- **storage**: Add SQLite decision and feedback store
  ([#30](https://github.com/AristideWafo/my-gmail-assistant/pull/30),
  [`4c86cab`](https://github.com/AristideWafo/my-gmail-assistant/commit/4c86cabad7aa777084623e1c06ebe9dd871d39b6))

Co-authored-by: Claude Opus 5.5 <noreply@anthropic.com>

- **telegram**: Add inbound long-polling bot with inline buttons
  ([#32](https://github.com/AristideWafo/my-gmail-assistant/pull/32),
  [`5fd8fcd`](https://github.com/AristideWafo/my-gmail-assistant/commit/5fd8fcd7c2bf61741d533989feb8c5031fa9fb22))

Co-authored-by: Claude Opus 5.5 <noreply@anthropic.com>

- **triage**: Inject user corrections as JEV few-shot examples
  ([#33](https://github.com/AristideWafo/my-gmail-assistant/pull/33),
  [`379f3a0`](https://github.com/AristideWafo/my-gmail-assistant/commit/379f3a033f42e6841e3e008fa0442068db27d063))

Co-authored-by: Claude Opus 5.5 <noreply@anthropic.com>

### Refactoring

- Put external components behind ports with env-selected adapters
  ([#36](https://github.com/AristideWafo/my-gmail-assistant/pull/36),
  [`970b6d1`](https://github.com/AristideWafo/my-gmail-assistant/commit/970b6d1ba9a61d8c072ad41aa2489d4a8438166d))

* refactor: extract domain models and ports

Move shared data types (EmailMessage, TriageResult, LLMAnalysis, DecisionRecord, Correction, chat
  events) into src/domain so core code no longer imports them from Gmail/JEV/Gemini/Telegram
  modules, and declare a typing.Protocol per external capability in src/ports (mail, classifier,
  LLM, alert channel, chat inbox, decision store).

Gmail create_draft now returns the draft id and send_draft a bool, so the mail port does not leak
  Gmail's response shape.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

* refactor(triage): split JEV and heuristic classifiers behind the classifier port

JevClassifier now only talks to JEV and raises on failure; HeuristicClassifier holds the local
  rules; FallbackClassifier composes them, logging and counting jev_fallback_total as before.
  EmailWorkflow depends on the EmailClassifier and EmailAnalyzer ports only.

* refactor(gateways): alert channels and chat inbox behind ports

* refactor: select adapters through a bootstrap registry

* refactor: address review findings on the ports refactor

- FallbackClassifier is now provider-agnostic: recoverable errors and the fallback hook are injected
  by the bootstrap (JEV keeps jev_fallback_total). - The chat inbox gets its own startup probe when
  no alert channel shares its client (e.g. ALERT_CHANNELS=discord with CHAT_INBOX=telegram). -
  src.gateways no longer re-exports adapters, so importing AlertGateway from core code does not load
  Telegram/Discord modules. - A Telegram poll failure logs one warning (listener) instead of two. -
  Docs: ALERT_CHANNELS order wording, PLAN.md classifier naming.

---------

Co-authored-by: Claude Opus 5.5 <noreply@anthropic.com>


## v0.6.0 (2026-09-28)

### Bug Fixes

- Never lose an urgent alert and avoid watchdog restart loops (review findings)
  ([#28](https://github.com/AristideWafo/my-gmail-assistant/pull/28),
  [`293c2df`](https://github.com/AristideWafo/my-gmail-assistant/commit/293c2df26eba685907b9303a9c1e805ddcf8b1ff))

send_urgent_alert now raises when no configured channel delivered, so the mail stays UNREAD and is
  retried; the dedup window is only recorded on success. Discord content is truncated to its
  2000-character limit. The heartbeat also fires on failed Gmail fetches and during history sync
  (now off the event loop), poll_once/history stop between emails on shutdown. The default Gmail
  query no longer excludes Promotions/Social, unparseable model output no longer leaks into alerts,
  drafts with [placeholders] are dropped, and "#123" is kept.

Co-authored-by: Claude Sonnet 5.5 <noreply@anthropic.com>

- Only high urgency triggers alerts; uncertain mails are labeled, never archived
  ([#21](https://github.com/AristideWafo/my-gmail-assistant/pull/21),
  [`d6bfd04`](https://github.com/AristideWafo/my-gmail-assistant/commit/d6bfd0463d9ae2f2c97460e323c97f3299caa815))

Low confidence used to route to the LLM path, which always sends an "Urgent" alert. Newsletters and
  spam never alert, even at high urgency. Threshold is configurable via LOW_CONFIDENCE_THRESHOLD.

Co-authored-by: Claude Sonnet 5.5 <noreply@anthropic.com>

- Scope the unread fetch and make urgent handling idempotent
  ([#22](https://github.com/AristideWafo/my-gmail-assistant/pull/22),
  [`67303dc`](https://github.com/AristideWafo/my-gmail-assistant/commit/67303dc0a93b6906e882c4bfff237cbca809c7d7))

Only recent inbox mail is fetched (no promotions/social, max age configurable), so the old backlog
  is no longer processed as new. Urgent flow is alert -> best-effort draft -> label (commit point),
  with a 24h in-memory guard so a failed commit never re-alerts.

Co-authored-by: Claude Sonnet 5.5 <noreply@anthropic.com>

- **ci**: Start the release job from the branch tip to avoid non-fast-forward pushes
  ([#27](https://github.com/AristideWafo/my-gmail-assistant/pull/27),
  [`b84dbf5`](https://github.com/AristideWafo/my-gmail-assistant/commit/b84dbf5d278b3ec452230b61877be41210781754))

* fix(ci): start the release job from the branch tip to avoid non-fast-forward pushes

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>

* docs: update open risks in PLAN.md after idempotence work

---------

Co-authored-by: Claude Sonnet 5.5 <noreply@anthropic.com>

### Features

- Deterministic sender rules and a richer category taxonomy
  ([#24](https://github.com/AristideWafo/my-gmail-assistant/pull/24),
  [`d9aa606`](https://github.com/AristideWafo/my-gmail-assistant/commit/d9aa606378d852c67079fbf3229977abcfce15aa))

Known bulk senders (LinkedIn job alerts, Substack, Leboncoin marketing) are classified without
  calling JEV or Gemini. New categories alerte_emploi (kept visible), promotion and
  alerte_technique; the urgency definition is strict and JEV now receives the received and current
  dates so stale mail is not judged urgent.

Co-authored-by: Claude Sonnet 5.5 <noreply@anthropic.com>

- Expose routing/alert/LLM metrics and make health reflect real polling activity
  ([#26](https://github.com/AristideWafo/my-gmail-assistant/pull/26),
  [`7bf27f5`](https://github.com/AristideWafo/my-gmail-assistant/commit/7bf27f524cc6618bf389d9904a631a5d62c09ac4))

/healthz now returns 503 if the polling task died or stalled, and an in-process watchdog exits so
  the restart policy recovers it (a Docker healthcheck alone does not restart). Polling runs off the
  event loop so /healthz stays responsive during LLM rate limiting. New metrics and Grafana panels:
  routes, alert delivery, LLM errors, confidence, poll age.

Co-authored-by: Claude Sonnet 5.5 <noreply@anthropic.com>

- Harden alert delivery with webhook validation, circuit breakers and dedup
  ([#25](https://github.com/AristideWafo/my-gmail-assistant/pull/25),
  [`4bb1f73`](https://github.com/AristideWafo/my-gmail-assistant/commit/4bb1f73ff986650d3929cf00ffb0eba5eb901c0e))

A Discord webhook URL without its token is now reported at startup with an explicit, secret-free
  message and disabled (one error instead of one warning per mail). Each channel pauses after 5
  consecutive failures, and repeated alerts from the same automated sender and subject (e.g. CI
  failures across commits) are sent once per 30 minutes.

Co-authored-by: Claude Sonnet 5.5 <noreply@anthropic.com>

- Single rate-limited Gemini call, clean drafts and readable alert messages
  ([#23](https://github.com/AristideWafo/my-gmail-assistant/pull/23),
  [`a13a7cf`](https://github.com/AristideWafo/my-gmail-assistant/commit/a13a7cff1c2d03036cc9f77bfb0cefd66bd9caa5))

One JSON call per urgent mail (summary, optional draft, optional job entities) behind a
  sliding-window rate limiter that retries on quota errors using the API's retry delay. The alert
  still goes out with the mail snippet when Gemini fails. Drafts are only generated for human
  senders, are stripped of chat preambles/subject/markdown/placeholders, and are encoded as proper
  UTF-8 MIME. Telegram/Discord alerts use a labeled plain-text layout.

Co-authored-by: Claude Sonnet 5.5 <noreply@anthropic.com>


## v0.5.2 (2026-09-28)

### Bug Fixes

- Make the polling loop resilient and visible instead of silently dying
  ([#20](https://github.com/AristideWafo/my-gmail-assistant/pull/20),
  [`d3f75fe`](https://github.com/AristideWafo/my-gmail-assistant/commit/d3f75fecf15ab87962f660ebfd409d06bb879d67))

asyncio.create_task(polling_loop(...))'s result is never awaited or retrieved, so any exception
  raised while fetching or processing an email killed the background task forever with zero log
  output - the app kept answering /healthz and /metrics as if nothing was wrong while never
  processing another email again, until a manual restart.

Extracted poll_once(ctx): fetch failures and per-email processing failures are now caught and logged
  individually (one bad email no longer stops the others or the loop), and every cycle logs how many
  unread emails it found - so "it's silent" now means "zero unread", never "it's dead".

Co-authored-by: Claude Sonnet 5 <noreply@anthropic.com>


## v0.5.1 (2026-09-28)

### Bug Fixes

- Log Telegram/Discord notification failures instead of dropping them silently
  ([#18](https://github.com/AristideWafo/my-gmail-assistant/pull/18),
  [`84093e3`](https://github.com/AristideWafo/my-gmail-assistant/commit/84093e344dab7f4679255f6ade985c866b6c4141))

_send_telegram/_send_discord never checked the HTTP response, so a rejected Telegram/Discord call
  (wrong chat id, bot blocked, bad webhook...) returned as if it had succeeded, and main.py
  additionally wrapped the startup report send in a bare suppress(Exception) with no logging. Both
  now raise_for_status() and are caught per-channel in AlertGateway._safe_send, which logs a warning
  and never propagates - this fixes the report going missing with zero trace, without risking a
  crash of process_email/polling_loop on a delivery failure.

Co-authored-by: Claude Sonnet 5 <noreply@anthropic.com>


## v0.5.0 (2026-09-28)

### Features

- Enriched JEV category taxonomy (P4)
  ([#17](https://github.com/AristideWafo/my-gmail-assistant/pull/17),
  [`c0aa582`](https://github.com/AristideWafo/my-gmail-assistant/commit/c0aa582abfa021acf65dd9b39d8ab2e7fad6f87e))

* feat: provision Grafana dashboards and track real LLM token cost

- GeminiClient now records real prompt/completion token counts from response.usage_metadata for
  every call (summary/draft/entities, was only word-counted for summary+draft before) and computes
  USD cost from a verified per-model price table; unpriced models still count tokens but skip cost
  and log a one-time warning instead of guessing. - Grafana auto-provisions a Prometheus datasource
  and a "Gmail Assistant" dashboard (processed emails, triage latency p95, JEV fallback rate, LLM
  tokens, LLM cost/hour) on startup, no manual setup. - Grafana admin password now comes from
  GRAFANA_ADMIN_PASSWORD; added memory/cpu limits to prometheus and grafana (previously unbounded).
  - PLAN.md Phase 4 items marked done.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>

* feat: enriched JEV category taxonomy (P4)

Replace offer/general/urgent/spam/newsletter with offre_emploi/
  mise_en_relation/newsletter/notification_systeme/personnel/spam. "urgent" is dropped (duplicated
  the urgency axis); "general" becomes "personnel". Silent-archive routing on low urgency is now
  scoped to notification_systeme only — personnel mail is never auto-archived, always label/llm.
  Heuristic fallback still limited to newsletter/ offre_emploi/personnel;
  mise_en_relation/notification_systeme need JEV.

---------

Co-authored-by: Claude Sonnet 5 <noreply@anthropic.com>

- Provision Grafana dashboards and track real LLM token cost
  ([#16](https://github.com/AristideWafo/my-gmail-assistant/pull/16),
  [`53fc758`](https://github.com/AristideWafo/my-gmail-assistant/commit/53fc758ea12eb905271f9dcc648b4e266b671cc1))

- GeminiClient now records real prompt/completion token counts from response.usage_metadata for
  every call (summary/draft/entities, was only word-counted for summary+draft before) and computes
  USD cost from a verified per-model price table; unpriced models still count tokens but skip cost
  and log a one-time warning instead of guessing. - Grafana auto-provisions a Prometheus datasource
  and a "Gmail Assistant" dashboard (processed emails, triage latency p95, JEV fallback rate, LLM
  tokens, LLM cost/hour) on startup, no manual setup. - Grafana admin password now comes from
  GRAFANA_ADMIN_PASSWORD; added memory/cpu limits to prometheus and grafana (previously unbounded).
  - PLAN.md Phase 4 items marked done.

Co-authored-by: Claude Sonnet 5 <noreply@anthropic.com>

- Send a Telegram status report at startup
  ([#15](https://github.com/AristideWafo/my-gmail-assistant/pull/15),
  [`acc1a3a`](https://github.com/AristideWafo/my-gmail-assistant/commit/acc1a3af469f5fff905cbbdecd514519a1f4d633))

After the connection checks run, post a one-line-per-service report to Telegram (bonjour + per-check
  emoji status) reusing the same results already logged. Sending is best-effort and never blocks
  startup.

Co-authored-by: Claude Sonnet 5 <noreply@anthropic.com>


## v0.4.0 (2026-09-28)

### Features

- Verify and log all service connections at startup
  ([#14](https://github.com/AristideWafo/my-gmail-assistant/pull/14),
  [`f2696b0`](https://github.com/AristideWafo/my-gmail-assistant/commit/f2696b05414d62858f2dd1f5ea4fc4e84d3989f9))

Probe Gmail, Gemini, JEV, Telegram and Discord with read-only calls when the app starts and log one
  line per service. STARTUP_CHECKS=warn (default) logs and continues, strict refuses to start if a
  configured connection fails, off disables. Failure details omit exception messages because they
  embed URLs carrying bot tokens and webhook secrets.

Co-authored-by: Claude Sonnet 5 <noreply@anthropic.com>


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
