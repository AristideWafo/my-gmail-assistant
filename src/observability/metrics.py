from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest


class Metrics:
    processed_emails = Counter(
        "processed_emails_total",
        "Total number of processed emails",
        ["urgency", "category"],
    )
    triage_latency = Histogram("triage_latency_seconds", "Latency of triage and routing execution")
    llm_tokens = Counter("llm_tokens_total", "Tokens consumed, as reported by the LLM API", ["kind", "token_type"])
    llm_cost_usd = Counter("llm_cost_usd_total", "Estimated USD cost of LLM calls", ["kind"])
    alerts = Counter("alerts_total", "Alert delivery outcomes per channel", ["channel", "status"])
    llm_errors = Counter("llm_errors_total", "Failed or degraded LLM calls", ["reason"])
    jev_fallback = Counter(
        "jev_fallback_total",
        "Times the JEV API was unreachable and the heuristic fallback was used",
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
