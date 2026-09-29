import argparse
import asyncio
import logging
import os
import threading
from collections.abc import Callable
from contextlib import suppress
from datetime import timedelta
from time import monotonic, perf_counter

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from src.config import Settings
from src.expiring_set import ExpiringSet
from src.gateways import AlertGateway
from src.gateways.telegram_bot import TelegramBot, run_listener
from src.gmail import GmailClient
from src.gmail.client import build_unread_query
from src.health import (
    PollHealth,
    StartupCheckMode,
    format_status_report,
    run_startup_checks,
    run_watchdog,
)
from src.interactions import InteractionHandler
from src.llm import GeminiClient
from src.observability import Metrics
from src.storage import SqliteDecisionStore
from src.triage import FallbackClassifier, HeuristicClassifier, JevClassifier
from src.triage.few_shot import MAX_EXAMPLES, build_examples
from src.workflow import EmailWorkflow

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gmail-assistant")

TELEGRAM_OFFSET_KEY = "telegram_offset"
LISTENER_JOIN_TIMEOUT_SECONDS = 5
# A mail re-marked unread after this window is processed (and alerted) again.
ALERTED_TTL_SECONDS = 24 * 3600
RETENTION = timedelta(days=90)
PRUNE_INTERVAL_SECONDS = 24 * 3600


class ApplicationContext:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = SqliteDecisionStore(settings.db_path)
        self.gmail = GmailClient(
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
            refresh_token=settings.gmail_refresh_token,
            user_id=settings.gmail_user_id,
            unread_query=settings.fetch_query or build_unread_query(settings.fetch_max_age_days),
        )
        self.triage = FallbackClassifier(
            JevClassifier(
                settings.jev_api_url,
                settings.jev_api_key,
                examples_provider=self._few_shot_examples if settings.jev_few_shot_enabled else None,
            ),
            HeuristicClassifier(),
        )
        self.gemini = GeminiClient(
            settings.gemini_api_key,
            settings.gemini_model,
            max_rpm=settings.gemini_max_rpm,
            user_name=settings.user_display_name,
        )
        self.telegram = TelegramBot(
            settings.telegram_bot_token,
            settings.telegram_chat_id,
            allowed_user_ids=settings.allowed_user_ids,
        )
        self.inbound_enabled = self._resolve_inbound()
        self.alerts = AlertGateway(
            telegram=self.telegram,
            discord_webhook_url=settings.discord_webhook_url,
            feedback_buttons=self.inbound_enabled,
        )
        self.interactions = InteractionHandler(self.store, self.telegram, self.gmail)
        self.workflow = EmailWorkflow(self.triage, self.gemini, settings.low_confidence_threshold)
        self.health = PollHealth(settings.poll_stale_after_seconds)
        self.stopping = threading.Event()
        self.listener: threading.Thread | None = None
        # Backs up the persisted flag: if both mark_alerted and the label fail, the mail must not
        # be re-alerted every cycle.
        self.recently_alerted = ExpiringSet(ALERTED_TTL_SECONDS)
        self._last_prune: float | None = None

    def connection_probes(self):
        return {
            "gmail": self.gmail.check_connection if self.gmail.is_configured else None,
            "gemini": self.gemini.check_connection if self.gemini.is_configured else None,
            "jev": self.triage.check_connection if self.triage.primary.is_configured else None,
            "telegram": self.alerts.check_telegram if self.alerts.telegram_configured else None,
            "discord": self.alerts.check_discord if self.alerts.discord_configured else None,
        }

    def process_email(self, email):
        started = perf_counter()
        if email.id in self.recently_alerted or self.store.was_alerted(
            email.id, within_seconds=ALERTED_TTL_SECONDS
        ):
            # The alert already went out but the commit (label removing UNREAD) failed last cycle.
            self.gmail.label_message(email.id, "urgent")
            return

        result = self.workflow.run(email)
        triage = result["triage"]
        route = result["route"]
        self._best_effort(
            lambda: self.store.record_decision(email, triage, route), "record decision", email.id
        )

        if route == "reject":
            self.gmail.archive_message(email.id)
        elif route == "label":
            self.gmail.label_message(email.id, triage.category)
        elif route == "llm":
            self._handle_urgent(email, triage, result)

        Metrics.mark_processed(triage.urgency, triage.category)
        Metrics.mark_route(route, triage.urgency, triage.confidence)
        Metrics.triage_latency.observe(perf_counter() - started)
        logger.info(
            "Processed email %s with urgency=%s category=%s confidence=%.2f",
            email.id,
            triage.urgency,
            triage.category,
            triage.confidence,
        )

    def _handle_urgent(self, email, triage, result) -> None:
        # Order matters: alert first (never lose it), then persist it as alerted so a retry only
        # relabels, draft is best-effort, and the label that removes UNREAD is the commit point so
        # a failure anywhere earlier retries the mail.
        chat_message_id = self.alerts.send_urgent_alert(email, triage, self._alert_summary(result))
        self.recently_alerted.add(email.id)
        self._best_effort(lambda: self.store.mark_alerted(email.id), "mark alerted", email.id)
        if chat_message_id is not None:
            self._best_effort(
                lambda: self.store.attach_chat_message(email.id, chat_message_id),
                "link Telegram alert",
                email.id,
            )
        if result.get("draft"):
            self._best_effort(
                lambda: self.gmail.create_draft(
                    email.thread_id,
                    email.sender,
                    email.subject,
                    result["draft"],
                    in_reply_to=email.message_id_header,
                ),
                "create draft",
                email.id,
            )
        self.gmail.label_message(email.id, "urgent")

    @staticmethod
    def _best_effort(action: Callable[[], object], description: str, email_id: str) -> None:
        # Once the alert is out, raising would skip the label commit and re-alert the mail on
        # every retry; a lost row or draft only degrades feedback, dedup or convenience.
        try:
            action()
        except Exception:
            logger.exception("Failed to %s for email %s; continuing", description, email_id)

    def _few_shot_examples(self) -> list[dict[str, str]]:
        return build_examples(self.store.recent_corrections(MAX_EXAMPLES))

    def prune_if_due(self) -> None:
        now = monotonic()
        if self._last_prune is not None and now - self._last_prune < PRUNE_INTERVAL_SECONDS:
            return
        self._last_prune = now
        try:
            deleted = self.store.prune(RETENTION)
        except Exception:
            logger.exception("Failed to prune the decision store; will retry tomorrow")
            return
        logger.info("Pruned %d expired row(s) from the decision store", deleted)

    def _resolve_inbound(self) -> bool:
        if not self.settings.telegram_inbound_enabled:
            return False
        if not self.telegram.configured:
            logger.warning("TELEGRAM_INBOUND_ENABLED ignored: Telegram is not configured")
            return False
        if not self.telegram.inbound_authorized:
            logger.error(
                "TELEGRAM_INBOUND_ENABLED ignored: TELEGRAM_ALLOWED_USER_IDS is required when "
                "TELEGRAM_CHAT_ID is a group or channel"
            )
            return False
        return True

    def start_telegram_listener(self) -> None:
        if not self.inbound_enabled:
            return
        self.listener = threading.Thread(
            target=run_listener,
            args=(
                self.telegram,
                self.interactions.dispatch,
                self._load_telegram_offset,
                self._save_telegram_offset,
                self.stopping,
            ),
            kwargs={"sleep": self.stopping.wait},
            name="telegram-listener",
            daemon=True,
        )
        self.listener.start()

    def close(self) -> None:
        self.stopping.set()
        # A long poll can keep the listener blocked for ~40s; it is a daemon, so wait only briefly.
        if self.listener is not None:
            self.listener.join(LISTENER_JOIN_TIMEOUT_SECONDS)
        self.store.close()

    def _load_telegram_offset(self) -> int | None:
        value = self.store.get_state(TELEGRAM_OFFSET_KEY)
        return int(value) if value else None

    def _save_telegram_offset(self, offset: int) -> None:
        self.store.set_state(TELEGRAM_OFFSET_KEY, str(offset))

    @staticmethod
    def _alert_summary(result) -> str:
        summary = result.get("summary", "")
        entities = result.get("entities")
        if entities:
            entity_lines = "\n".join(f"{key}: {value}" for key, value in entities.items() if value)
            summary = f"{summary}\n\n{entity_lines}" if entity_lines else summary
        return summary


def poll_once(ctx: ApplicationContext) -> None:
    # asyncio.create_task's result is never awaited or retrieved (see startup_event), so an
    # exception raised out of here would kill polling forever with zero log line - the task just
    # dies silently and nothing is ever processed again until the container restarts. Every
    # failure must therefore be caught and logged right here, never allowed to propagate.
    ctx.prune_if_due()
    try:
        emails = ctx.gmail.fetch_unread()
    except Exception:
        logger.exception("Failed to fetch unread emails; will retry next cycle")
        # The loop is alive; a Gmail outage must not make the watchdog restart-loop the container.
        ctx.health.beat()
        return

    logger.info("Polled Gmail: %d unread email(s)", len(emails))
    Metrics.mark_poll_success()
    ctx.health.beat()
    for email in emails:
        if ctx.stopping.is_set():
            return
        try:
            ctx.process_email(email)
        except Exception:
            Metrics.mark_email_skipped()
            logger.exception("Failed to process email %s; skipping", email.id)
        ctx.health.beat()


def sync_history_once(ctx: ApplicationContext) -> None:
    for email in ctx.gmail.fetch_history():
        if ctx.stopping.is_set():
            return
        ctx.process_email(email)
        ctx.health.beat()


async def polling_loop(ctx: ApplicationContext):
    while True:
        # poll_once blocks on network and LLM rate limiting; keep it off the event loop so /healthz stays responsive.
        await asyncio.to_thread(poll_once, ctx)
        await asyncio.sleep(ctx.settings.poll_interval_seconds)


def terminate_process(reason: str) -> None:
    logger.critical("Polling is unhealthy (%s); exiting so the container restarts", reason)
    os._exit(1)


def create_app(sync_history: bool | None = None) -> FastAPI:
    settings = Settings()
    if sync_history is not None:
        settings.sync_history = sync_history

    ctx = ApplicationContext(settings)
    app = FastAPI(title="my-gmail-assistant", version="0.1.0")
    app.state.ctx = ctx
    app.include_router(Metrics.router())

    @app.get("/healthz")
    async def healthz():
        task = getattr(app.state, "polling_task", None)
        problem = ctx.health.problem(task_done=task is not None and task.done())
        if problem:
            return JSONResponse({"status": "unhealthy", "reason": problem}, status_code=503)
        return {"status": "ok"}

    @app.on_event("startup")
    async def startup_event():
        app.state.connection_checks = await asyncio.to_thread(
            run_startup_checks, ctx.connection_probes(), StartupCheckMode(settings.startup_checks)
        )
        if ctx.alerts.telegram_configured:
            # send_telegram_text already logs and swallows delivery failures (AlertGateway._safe_send);
            # this only guards against an unexpected error in report formatting itself.
            try:
                await asyncio.to_thread(
                    ctx.alerts.send_telegram_text, format_status_report(app.state.connection_checks)
                )
            except Exception:
                logger.exception("Failed to send startup status report to Telegram")

        if settings.sync_history:
            logger.info("Syncing Gmail history...")
            await asyncio.to_thread(sync_history_once, ctx)

        ctx.start_telegram_listener()
        ctx.health.beat()
        app.state.polling_task = asyncio.create_task(polling_loop(ctx))
        if settings.watchdog_enabled:
            app.state.watchdog_task = asyncio.create_task(
                run_watchdog(ctx.health, app.state.polling_task.done, terminate_process)
            )

    @app.on_event("shutdown")
    async def shutdown_event():
        ctx.stopping.set()
        for task in (app.state.polling_task, getattr(app.state, "watchdog_task", None)):
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        await asyncio.to_thread(ctx.close)

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
