from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest


class Metrics:
    processed_emails = Counter(
        "processed_emails_total",
        "Total number of processed emails",
        ["urgency", "category"],
    )
    triage_latency = Histogram("triage_latency_seconds", "Latency of triage and routing execution")
    llm_tokens = Counter("llm_tokens_total", "Estimated tokens consumed", ["kind"])

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
    def mark_tokens(cls, kind: str, amount: int) -> None:
        cls.llm_tokens.labels(kind=kind).inc(max(amount, 0))
