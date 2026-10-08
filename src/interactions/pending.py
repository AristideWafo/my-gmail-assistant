from collections.abc import Callable
from datetime import UTC, datetime, tzinfo

from src.domain import WAITING_FOR_THEM, CommandEvent, TrackedThread
from src.followup.state import NOT_USEFUL, USEFUL, would_propose
from src.interactions.callbacks import follow_up_buttons
from src.ports import ChatInbox, DecisionStore

MAX_ITEMS = 10
NOTHING_PENDING = "Rien en attente : aucun mail envoyé n'attend de réponse."
# What `on` needs before it can be trusted, from the plan agreed for follow-ups.
MIN_RATED = 10
MIN_USEFUL_RATE = 0.8
VERDICT_LABELS = {USEFUL: "relance utile", NOT_USEFUL: "pas de relance"}


def format_thread(
    thread: TrackedThread, threshold: float, now: datetime, timezone: tzinfo, position: int, total: int
) -> str:
    anchor = thread.anchor
    recipients = anchor.to + anchor.cc
    more = f" (+{len(recipients) - 1})" if len(recipients) > 1 else ""
    sent = anchor.sent_at.astimezone(timezone).strftime("%d/%m")
    lines = [
        f"⏳ En attente {position}/{total}",
        f"À : {recipients[0]}{more}",
        f"Objet : {anchor.subject or '(sans objet)'}",
    ]
    if thread.due_at is not None:
        due = thread.due_at.astimezone(timezone).strftime("%d/%m")
        late = "relance due depuis le" if thread.due_at <= now else "relance prévue le"
        lines.append(f"Envoyé le {sent}, {late} {due}")
    lines.append(_judgement(thread.expects_answer, threshold))
    if would_propose(thread, threshold, now):
        lines.append("Une relance serait proposée.")
    if thread.verdict:
        lines.append(f"Ton avis : {VERDICT_LABELS.get(thread.verdict, thread.verdict)}")
    return "\n".join(lines)


def _judgement(probability: float | None, threshold: float) -> str:
    if probability is None:
        return "JEV : pas encore évalué"
    if probability >= threshold:
        return f"JEV : attend une réponse ({probability:.0%})"
    return f"JEV : ne semble rien attendre ({probability:.0%})"


def format_header(waiting: int, shown: int, rated: list[TrackedThread], threshold: float) -> str:
    lines = [f"⏳ {waiting} mail(s) envoyé(s) en attente de réponse" + (
        f", les {shown} plus urgents ci-dessous" if shown < waiting else ""
    )]
    judged_yes = [t for t in rated if t.expects_answer is not None and t.expects_answer >= threshold]
    useful = sum(1 for t in judged_yes if t.verdict == USEFUL)
    if judged_yes:
        lines.append(
            f"Relances jugées utiles : {useful} sur {len(judged_yes)} "
            f"(pour passer en on : au moins {MIN_RATED} notées, {MIN_USEFUL_RATE:.0%} utiles)"
        )
    return "\n".join(lines)


class PendingCommand:
    """Mails I sent that still wait for an answer, with what a follow-up would do about them."""

    def __init__(
        self,
        store: DecisionStore,
        chat: ChatInbox,
        threshold: float,
        timezone: tzinfo,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._chat = chat
        self._threshold = threshold
        self._timezone = timezone
        self._clock = clock

    def run(self, event: CommandEvent) -> None:
        threads = self._store.threads
        waiting = [t for t in threads.in_state(WAITING_FOR_THEM) if t.anchor is not None]
        if not waiting:
            self._chat.send_message(NOTHING_PENDING, reply_to=event.message_id)
            return
        shown = waiting[:MAX_ITEMS]
        self._chat.send_message(
            format_header(len(waiting), len(shown), threads.rated(), self._threshold),
            reply_to=event.message_id,
        )
        now = self._clock()
        for position, thread in enumerate(shown, start=1):
            buttons = None if thread.verdict else follow_up_buttons(thread.thread_id)
            self._chat.send_message(
                format_thread(thread, self._threshold, now, self._timezone, position, len(shown)),
                buttons=buttons,
            )
