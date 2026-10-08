from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest


class Metrics:
    processed_emails = Counter(
        "processed_emails_total",
        "Total number of processed emails",
        ["urgency", "category"],
    )
    triage_latency = Histogram("triage_latency_seconds", "Latency of triage and routing execution")
    llm_tokens = Counter("llm_tokens_total", "Tokens consumed, as reported by the LLM API", ["kind", "token_type"])
    llm_cost_usd = Counter("llm_cost_usd_total", "Estimated USD cost of LLM calls", ["kind"])
    routes = Counter("triage_route_total", "Emails per routing decision", ["route"])
    triage_confidence = Histogram(
        "triage_confidence",
        "Triage confidence by deciding stage; source=jev calibrates the low-confidence threshold",
        ["urgency", "source"],
        buckets=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
    )
    emails_skipped = Counter("emails_skipped_total", "Emails whose processing failed and was retried next cycle")
    last_poll_timestamp = Gauge("last_successful_poll_timestamp_seconds", "Unix time of the last successful Gmail fetch")
    telegram_rejected = Counter(
        "telegram_inbound_rejected_total", "Inbound Telegram updates dropped", ["reason"]
    )
    telegram_poll_errors = Counter("telegram_poll_errors_total", "Failed Telegram getUpdates calls")
    telegram_last_poll = Gauge(
        "telegram_last_poll_timestamp_seconds", "Unix time of the last successful Telegram poll"
    )
    alerts = Counter("alerts_total", "Alert delivery outcomes per channel", ["channel", "status"])
    llm_errors = Counter("llm_errors_total", "Failed or degraded LLM calls", ["reason"])
    jev_fallback = Counter(
        "jev_fallback_total",
        "Times the JEV API was unreachable and the heuristic fallback was used",
    )
    feedback = Counter(
        "feedback_total", "User verdicts on triage decisions", ["verdict", "route"]
    )
    chat_replies = Counter(
        "chat_replies_total", "Outcomes of Telegram replies turned into Gmail drafts", ["status"]
    )

    unsubscribes = Counter(
        "unsubscribes_total", "Unsubscribe proposals and their outcomes", ["status"]
    )
    reply_drafts = Counter(
        "reply_drafts_total", "Mails flagged as awaiting a reply, with or without a draft", ["status"]
    )
    attention_signals = Counter(
        "attention_signals_total",
        "Mails a question gave a reason to put forward, after the category gate",
        ["signal"],
    )
    put_forward = Counter(
        "mails_put_forward_total", "Mails kept in the inbox and labeled as worth seeing"
    )
    chat_commands = Counter(
        "chat_commands_total", "Chat commands received", ["command", "status"]
    )
    poll_failures = Counter("poll_failures_total", "Polling cycles whose mail fetch failed")
    followup_threads = Gauge(
        "followup_threads", "Threads holding a mail I sent, by follow-up state", ["state"]
    )
    followup_refresh_errors = Counter(
        "followup_refresh_errors_total", "Sent threads that could not be read", ["step"]
    )
    followup_judged = Counter(
        "followup_judged_total", "Sent mails JEV judged, by whether they await something", ["answer"]
    )
    followup_due = Gauge(
        "followup_due_threads", "Threads a follow-up would be offered for now"
    )
    followup_verdicts = Counter(
        "followup_verdicts_total", "Follow-up verdicts given in the chat", ["verdict"]
    )
    followup_capped = Gauge(
        "followup_listing_capped",
        "1 when the last refresh found more recent sent threads than FOLLOW_UP_MAX_THREADS",
    )
    followup_unlisted_waiting = Gauge(
        "followup_unlisted_waiting_threads",
        "Waiting threads outside the listing window, read again at every refresh",
    )
    backup_last_success = Gauge(
        "backup_last_success_timestamp_seconds", "Unix time of the last verified store backup"
    )
    backup_failures = Counter("backup_failures_total", "Store backups that failed")

    @staticmethod
    def total(metric: Counter | Gauge, without: dict[str, str] | None = None) -> float:
        """Current value of a metric summed over its label sets, minus those matching `without`."""
        excluded = (without or {}).items()
        return sum(
            sample.value
            for family in metric.collect()
            for sample in family.samples
            # A counter also exposes a `_created` timestamp per label set.
            if not sample.name.endswith("_created")
            and not (excluded and excluded <= sample.labels.items())
        )

    @staticmethod
    def router() -> APIRouter:
        router = APIRouter()

        @router.get("/metrics")
        async def metrics():
            return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

        return router

    @classmethod
    def mark_processed(cls, urgency: str, category: str) -> None:
        cls.processed_emails.labels(urgency=urgency, category=category).inc()

    @classmethod
    def mark_llm_usage(cls, kind: str, prompt_tokens: int, completion_tokens: int, cost_usd: float | None) -> None:
        cls.llm_tokens.labels(kind=kind, token_type="prompt").inc(max(prompt_tokens, 0))
        cls.llm_tokens.labels(kind=kind, token_type="completion").inc(max(completion_tokens, 0))
        if cost_usd is not None:
            cls.llm_cost_usd.labels(kind=kind).inc(max(cost_usd, 0))

    @classmethod
    def mark_jev_fallback(cls) -> None:
        cls.jev_fallback.inc()

    @classmethod
    def mark_llm_error(cls, reason: str) -> None:
        cls.llm_errors.labels(reason=reason).inc()

    @classmethod
    def mark_alert(cls, channel: str, status: str) -> None:
        cls.alerts.labels(channel=channel, status=status).inc()

    @classmethod
    def mark_telegram_rejected(cls, reason: str) -> None:
        cls.telegram_rejected.labels(reason=reason).inc()

    @classmethod
    def mark_telegram_poll_success(cls) -> None:
        cls.telegram_last_poll.set_to_current_time()

    @classmethod
    def mark_telegram_poll_error(cls) -> None:
        cls.telegram_poll_errors.inc()

    @classmethod
    def mark_route(cls, route: str, urgency: str, confidence: float, source: str = "") -> None:
        cls.routes.labels(route=route).inc()
        cls.triage_confidence.labels(urgency=urgency, source=source or "unknown").observe(
            confidence
        )

    @classmethod
    def mark_email_skipped(cls) -> None:
        cls.emails_skipped.inc()

    @classmethod
    def mark_feedback(cls, verdict: str, route: str) -> None:
        cls.feedback.labels(verdict=verdict, route=route).inc()

    @classmethod
    def mark_chat_reply(cls, status: str) -> None:
        cls.chat_replies.labels(status=status).inc()

    @classmethod
    def mark_backup(cls, succeeded: bool) -> None:
        if succeeded:
            cls.backup_last_success.set_to_current_time()
        else:
            cls.backup_failures.inc()

    @classmethod
    def mark_unsubscribe(cls, status: str) -> None:
        cls.unsubscribes.labels(status=status).inc()

    @classmethod
    def mark_reply_draft(cls, status: str) -> None:
        cls.reply_drafts.labels(status=status).inc()

    @classmethod
    def mark_attention(cls, signal: str) -> None:
        cls.attention_signals.labels(signal=signal).inc()

    @classmethod
    def set_followup_threads(cls, counts: dict[str, int]) -> None:
        for state in ("waiting_for_them", "closed", "ignored"):
            cls.followup_threads.labels(state=state).set(counts.get(state, 0))

    @classmethod
    def mark_followup_refresh_error(cls, step: str) -> None:
        cls.followup_refresh_errors.labels(step=step).inc()

    @classmethod
    def mark_followup_judged(cls, awaits: bool) -> None:
        cls.followup_judged.labels(answer="yes" if awaits else "no").inc()

    @classmethod
    def mark_followup_verdict(cls, verdict: str) -> None:
        cls.followup_verdicts.labels(verdict=verdict).inc()

    @classmethod
    def set_followup_capped(cls, capped: bool) -> None:
        cls.followup_capped.set(1 if capped else 0)

    @classmethod
    def mark_put_forward(cls) -> None:
        cls.put_forward.inc()

    @classmethod
    def mark_chat_command(cls, command: str, status: str) -> None:
        cls.chat_commands.labels(command=command, status=status).inc()

    @classmethod
    def mark_poll_failure(cls) -> None:
        cls.poll_failures.inc()

    @classmethod
    def mark_poll_success(cls) -> None:
        cls.last_poll_timestamp.set_to_current_time()
