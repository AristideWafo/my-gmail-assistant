import argparse
import asyncio
import logging
from contextlib import suppress
from time import perf_counter

import uvicorn
from fastapi import FastAPI

from src.config import Settings
from src.gateways import AlertGateway
from src.gmail import GmailClient
from src.llm import GeminiClient
from src.observability import Metrics
from src.triage import DecisionEngineClient
from src.workflow import EmailWorkflow

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gmail-assistant")


class ApplicationContext:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.gmail = GmailClient(
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
            refresh_token=settings.gmail_refresh_token,
            user_id=settings.gmail_user_id,
        )
        self.triage = DecisionEngineClient(settings.jev_api_url)
        self.gemini = GeminiClient(settings.gemini_api_key, settings.gemini_model)
        self.alerts = AlertGateway(
            telegram_bot_token=settings.telegram_bot_token,
            telegram_chat_id=settings.telegram_chat_id,
            discord_webhook_url=settings.discord_webhook_url,
        )
        self.workflow = EmailWorkflow(self.triage, self.gemini)

    def process_email(self, email):
        started = perf_counter()
        result = self.workflow.run(email)
        triage = result["triage"]
        route = result["route"]

        if route == "reject":
            self.gmail.archive_message(email.id)
        elif route == "label":
            self.gmail.label_message(email.id, triage.category)
        elif route == "llm":
            self.gmail.create_draft(email.thread_id, email.sender, email.subject, result.get("draft", ""))
            self.gmail.label_message(email.id, "urgent")
            self.alerts.send_urgent_alert(email, triage, result.get("summary", ""))

        Metrics.mark_processed(triage.urgency, triage.category)
        with suppress(Exception):
            Metrics.mark_tokens("summary", len(result.get("summary", "").split()))
            Metrics.mark_tokens("draft", len(result.get("draft", "").split()))

        Metrics.triage_latency.observe(perf_counter() - started)
        logger.info(
            "Processed email %s with urgency=%s category=%s confidence=%.2f",
            email.id,
            triage.urgency,
            triage.category,
            triage.confidence,
        )


async def polling_loop(ctx: ApplicationContext):
    while True:
        for email in ctx.gmail.fetch_unread():
            ctx.process_email(email)
        await asyncio.sleep(ctx.settings.poll_interval_seconds)


def create_app(sync_history: bool | None = None) -> FastAPI:
    settings = Settings()
    if sync_history is not None:
        settings.sync_history = sync_history

    ctx = ApplicationContext(settings)
    app = FastAPI(title="my-gmail-assistant", version="0.1.0")
    app.include_router(Metrics.router())

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.on_event("startup")
    async def startup_event():
        if settings.sync_history:
            logger.info("Syncing Gmail history...")
            for email in ctx.gmail.fetch_history():
                ctx.process_email(email)

        app.state.polling_task = asyncio.create_task(polling_loop(ctx))

    @app.on_event("shutdown")
    async def shutdown_event():
        app.state.polling_task.cancel()
        with suppress(asyncio.CancelledError):
            await app.state.polling_task

    return app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Gmail triage assistant")
    parser.add_argument("--sync-history", action="store_true", help="Process historical Gmail messages on startup")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    uvicorn.run(create_app(sync_history=args.sync_history), host=args.host, port=args.port)
