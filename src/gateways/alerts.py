import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

from src.domain import Button, EmailMessage, TriageResult
from src.formatting import strip_markdown, truncate
from src.gateways.circuit_breaker import CircuitBreaker
from src.interactions.callbacks import feedback_buttons
from src.observability.metrics import Metrics
from src.ports import AlertChannel, ChannelDeliveryError
from src.triage.rules import is_automated_sender

logger = logging.getLogger(__name__)

ALERT_MESSAGE_LIMIT = 4000
INCIDENT_FOOTER_LIMIT = 300
DEDUP_WINDOW_SECONDS = 30 * 60
_COMMIT_SHA_RE = re.compile(r"\s*\([0-9a-f]{7,40}\)")
_NUMBER_REF_RE = re.compile(r"#\d+")


def dedup_key(email: EmailMessage) -> tuple[str, str]:
    subject = _NUMBER_REF_RE.sub("", _COMMIT_SHA_RE.sub("", email.subject))
    return email.sender.lower(), " ".join(subject.lower().split())


def format_urgent_alert(email: EmailMessage, triage: TriageResult, summary: str = "") -> str:
    lines = [
        "🚨 Email urgent",
        f"De : {email.sender}",
        f"Objet : {email.subject}",
        f"Catégorie : {triage.category} · confiance {triage.confidence:.0%}",
    ]
    text = "\n".join(lines)
    body = strip_markdown(summary)
    if body:
        text = f"{text}\n\n{body}"
    return truncate(text, ALERT_MESSAGE_LIMIT)


@dataclass
class Incident:
    """A first automated alert and the repeats that were folded into it."""

    text: str
    buttons: list[list[Button]] | None
    channel: AlertChannel | None
    message_id: int | None
    started_at: float
    count: int = 1


def format_incident_update(incident: Incident, latest: EmailMessage, now: float) -> str:
    minutes = max(1, round((now - incident.started_at) / 60))
    footer = f"🔁 {incident.count} occurrences en {minutes} min · dernière : {latest.subject}"
    footer = truncate(footer, INCIDENT_FOOTER_LIMIT)
    return f"{truncate(incident.text, ALERT_MESSAGE_LIMIT - len(footer) - 2)}\n\n{footer}"


class AlertDeliveryError(RuntimeError):
    pass


class AlertGateway:
    def __init__(
        self,
        channels: list[AlertChannel],
        feedback_buttons: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.channels = list(channels)
        self._feedback_buttons = feedback_buttons
        self._breakers = {channel.name: CircuitBreaker() for channel in self.channels}
        self._clock = clock
        self._incidents: dict[tuple[str, str], Incident] = {}

    def send_urgent_alert(
        self, email: EmailMessage, triage: TriageResult, summary: str = ""
    ) -> int | None:
        """Returns the message id from the first interactive channel that delivered it.

        Raises AlertDeliveryError when every configured channel failed, so the mail is retried.
        """
        incident = self._open_incident(email)
        if incident is not None:
            logger.info("Grouping repeated alert for %s: %s", email.sender, email.subject)
            Metrics.mark_alert("all", "deduplicated")
            self._record_repeat(incident, email)
            return None
        text = format_urgent_alert(email, triage, summary)
        buttons = feedback_buttons(email.id) if self._feedback_buttons else None
        outcomes = []
        chat_message_id = None
        origin = None
        for channel in self._configured():
            delivered, message_id = self._safe_send(
                channel, text, buttons if channel.interactive else None
            )
            outcomes.append(delivered)
            if chat_message_id is None and delivered and channel.interactive:
                chat_message_id = message_id
                origin = channel
        if outcomes and not any(outcomes):
            raise AlertDeliveryError(f"No channel delivered the alert for {email.subject!r}")
        if outcomes and is_automated_sender(email.sender):
            self._incidents[dedup_key(email)] = Incident(
                text, buttons, origin, chat_message_id, started_at=self._clock()
            )
        return chat_message_id

    def send_text(self, text: str) -> bool:
        """Best-effort message to the interactive channels only; True when one delivered it."""
        outcomes = [
            self._safe_send(channel, text, None)[0]
            for channel in self._configured()
            if channel.interactive
        ]
        return any(outcomes)

    def _configured(self) -> list[AlertChannel]:
        return [channel for channel in self.channels if channel.is_configured]

    def _open_incident(self, email: EmailMessage) -> Incident | None:
        # Repeated CI failures share sender and subject; human mail is never grouped. The key
        # keeps the workflow name, so another workflow of the same repository alerts at once.
        if not is_automated_sender(email.sender):
            return None
        key = dedup_key(email)
        incident = self._incidents.get(key)
        if incident is not None and self._clock() - incident.started_at >= DEDUP_WINDOW_SECONDS:
            del self._incidents[key]
            return None
        return incident

    def _record_repeat(self, incident: Incident, email: EmailMessage) -> None:
        incident.count += 1
        if incident.channel is None or incident.message_id is None:
            return
        breaker = self._breakers[incident.channel.name]
        if not breaker.allow():
            Metrics.mark_alert(incident.channel.name, "skipped")
            return
        text = format_incident_update(incident, email, self._clock())
        try:
            incident.channel.update(incident.message_id, text, incident.buttons)
        except ChannelDeliveryError as exc:
            # The first alert already reached the user: a failed counter update loses nothing.
            Metrics.mark_alert(incident.channel.name, "update_failed")
            logger.warning("Failed to update %s alert: %s", incident.channel.name, exc)
            breaker.record_failure()
            return
        breaker.record_success()
        Metrics.mark_alert(incident.channel.name, "updated")

    def _safe_send(
        self, channel: AlertChannel, text: str, buttons: list[list[Button]] | None
    ) -> tuple[bool, int | None]:
        # A failed notification must never crash email processing or startup; it must also
        # never disappear silently, or a dead integration goes unnoticed indefinitely.
        breaker = self._breakers[channel.name]
        if not breaker.allow():
            Metrics.mark_alert(channel.name, "skipped")
            return False, None
        try:
            message_id = channel.send(text, buttons)
        except ChannelDeliveryError as exc:
            Metrics.mark_alert(channel.name, "failed")
            logger.warning("Failed to send %s notification: %s", channel.name, exc)
            if breaker.record_failure():
                logger.error(
                    "%s notifications failing repeatedly; pausing them for a while", channel.name
                )
            return False, None
        breaker.record_success()
        Metrics.mark_alert(channel.name, "sent")
        return True, message_id
