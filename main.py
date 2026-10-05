import argparse
import asyncio
import logging
import os
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
from time import monotonic, perf_counter

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from src.bootstrap import Components, build_components, connection_probes
from src.config import Settings
from src.expiring_set import ExpiringSet
from src.gateways.alerts import AlertGateway
from src.health import (
    BudgetedAnalyzer,
    HealthWatch,
    OutageNotifier,
    PollHealth,
    SpendTracker,
    StartupCheckMode,
    budget_check,
    counter_checks,
    format_status_report,
    listener_check,
    run_startup_checks,
    run_watchdog,
)
from src.interactions import InteractionHandler
from src.interactions.listener import run_listener
from src.interactions.unsubscribe import UnsubscribeProposer
from src.maintenance import BackupRotation
from src.observability import Metrics
from src.scheduling import DailyJob
from src.triage.rules import load_ruleset
from src.version import app_version
from src.workflow import EmailWorkflow, attention_reasons

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gmail-assistant")

TELEGRAM_OFFSET_KEY = "telegram_offset"
LISTENER_JOIN_TIMEOUT_SECONDS = 5
# A mail re-marked unread after this window is processed (and alerted) again.
ALERTED_TTL_SECONDS = 24 * 3600
RETENTION = timedelta(days=90)
MAINTENANCE_INTERVAL_SECONDS = 24 * 3600
REPLY_EXPECTED_LABEL = "a_repondre"
PUT_FORWARD_LABEL = "a_voir"


class ApplicationContext:
    def __init__(self, settings: Settings, components: Components | None = None):
        self.settings = settings
        self.components = components or build_components(settings)
        self.store = self.components.store
        self.mail = self.components.mail
        self.chat = self.components.chat
        self.inbound_enabled = self._resolve_inbound()
        self.alerts = AlertGateway(
            list(self.components.channels), feedback_buttons=self.inbound_enabled
        )
        self.interactions = (
            InteractionHandler(
                self.store,
                self.chat,
                self.mail,
                self.components.unsubscriber,
                settings.attention_threshold if settings.attention_mode == "on" else None,
            )
            if self.chat is not None
            else None
        )
        self.put_forward_job = self._build_put_forward_job()
        self.unsubscribes = self._build_unsubscribe_proposer()
        self.spend = SpendTracker(
            self.store,
            lambda: Metrics.total(Metrics.llm_cost_usd),
            settings.llm_daily_budget_usd,
            settings.tzinfo,
        )
        self.workflow = EmailWorkflow(
            self.components.classifier,
            BudgetedAnalyzer(self.components.analyzer, self.spend),
            settings.low_confidence_threshold,
            load_ruleset(settings.triage_rules_path),
            settings.needs_reply_threshold if settings.needs_reply_enabled else None,
            settings.attention_threshold if settings.attention_mode == "on" else None,
        )
        self.health = PollHealth(settings.poll_stale_after_seconds)
        self.outage = OutageNotifier(
            settings.poll_failure_alert_minutes * 60, self.alerts.send_text
        )
        self.stopping = threading.Event()
        self.listener: threading.Thread | None = None
        self.health_watch = self._build_health_watch()
        # Backs up the persisted flag: if both mark_alerted and the label fail, the mail must not
        # be re-alerted every cycle.
        self.recently_alerted = ExpiringSet(ALERTED_TTL_SECONDS)
        self.recently_drafted = ExpiringSet(ALERTED_TTL_SECONDS)
        self.backups = (
            BackupRotation(self.store, settings.backup_dir, settings.backup_keep)
            if settings.backup_dir
            else None
        )
        self._last_maintenance: dict[str, float] = {}

    def connection_probes(self):
        return connection_probes(self.components)

    def process_email(self, email):
        started = perf_counter()
        if email.id in self.recently_alerted or self.store.was_alerted(
            email.id, within_seconds=ALERTED_TTL_SECONDS
        ):
            # The alert already went out but the commit (label removing UNREAD) failed last cycle.
            self.mail.label_message(email.id, "urgent")
            return

        result = self.workflow.run(email)
        triage = result["triage"]
        route = result["route"]
        put_forward = bool(result.get("attention"))
        self._best_effort(
            lambda: self.store.record_decision(email, triage, route, put_forward),
            "record decision",
            email.id,
        )

        if route == "reject":
            self.mail.archive_message(email.id)
            if self.unsubscribes is not None:
                self._best_effort(
                    lambda: self.unsubscribes.consider(email), "propose unsubscribe", email.id
                )
        elif route == "label":
            labels = []
            if result.get("reply_expected"):
                self._draft_expected_reply(email, result.get("draft", ""))
                labels.append(REPLY_EXPECTED_LABEL)
            if put_forward:
                Metrics.mark_put_forward()
                labels.append(PUT_FORWARD_LABEL)
            self.mail.label_message(email.id, *labels, triage.category)
        elif route == "llm":
            self._handle_urgent(email, triage, result)

        if self.settings.attention_mode != "off":
            for signal in attention_reasons(triage, self.settings.attention_threshold):
                Metrics.mark_attention(signal)
        Metrics.mark_processed(triage.urgency, triage.category)
        Metrics.mark_route(route, triage.urgency, triage.confidence, triage.source)
        Metrics.triage_latency.observe(perf_counter() - started)
        logger.info(
            "Processed email %s with urgency=%s category=%s confidence=%.2f needs_reply=%s "
            "signals=%s",
            email.id,
            triage.urgency,
            triage.category,
            triage.confidence,
            "n/a" if triage.needs_reply is None else f"{triage.needs_reply:.2f}",
            " ".join(f"{name}:{value:.2f}" for name, value in triage.signals.items()) or "n/a",
        )

    def _handle_urgent(self, email, triage, result) -> None:
        # Order matters: alert first (never lose it), then persist it as alerted so a retry only
        # relabels, draft is best-effort, and the labeling that removes UNREAD is the commit point
        # so a failure anywhere earlier retries the mail.
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
            self._create_reply_draft(email, result["draft"])
        labels = []
        if result.get("reply_expected"):
            Metrics.mark_reply_draft("drafted" if result.get("draft") else "no_draft")
            labels.append(REPLY_EXPECTED_LABEL)
        self.mail.label_message(email.id, *labels, "urgent")

    def _draft_expected_reply(self, email, draft: str) -> None:
        if email.id in self.recently_drafted:
            # The labeling failed last cycle and the mail came back: its draft already exists.
            return
        if draft:
            self._create_reply_draft(email, draft)
            self.recently_drafted.add(email.id)
            Metrics.mark_reply_draft("drafted")
        else:
            Metrics.mark_reply_draft("no_draft")

    def _create_reply_draft(self, email, draft: str) -> None:
        self._best_effort(
            lambda: self.mail.create_draft(
                email.thread_id,
                email.sender,
                email.subject,
                draft,
                in_reply_to=email.message_id_header,
            ),
            "create draft",
            email.id,
        )

    @staticmethod
    def _best_effort(action: Callable[[], object], description: str, email_id: str) -> None:
        # Once the alert is out, raising would skip the label commit and re-alert the mail on
        # every retry; a lost row or draft only degrades feedback, dedup or convenience.
        try:
            action()
        except Exception:
            logger.exception("Failed to %s for email %s; continuing", description, email_id)

    def backup_if_due(self) -> None:
        if self.backups is None or not self._maintenance_due("backup"):
            return
        try:
            path = self.backups.run()
        except Exception:
            logger.exception("Failed to back up the decision store; will retry tomorrow")
            Metrics.mark_backup(succeeded=False)
            return
        Metrics.mark_backup(succeeded=True)
        logger.info("Backed up the decision store to %s", path)

    def prune_if_due(self) -> None:
        if not self._maintenance_due("prune"):
            return
        try:
            deleted = self.store.prune(RETENTION)
        except Exception:
            logger.exception("Failed to prune the decision store; will retry tomorrow")
            return
        logger.info("Pruned %d expired row(s) from the decision store", deleted)

    def _build_health_watch(self) -> HealthWatch | None:
        settings = self.settings
        if settings.health_alert_window_minutes <= 0:
            return None
        checks = counter_checks(
            settings.health_alert_window_minutes * 60, settings.health_alert_min_events
        )
        if self.inbound_enabled:
            checks.append(
                listener_check(lambda: self.listener is not None and self.listener.is_alive())
            )
        if settings.llm_daily_budget_usd > 0:
            checks.append(budget_check(self.spend))
        return HealthWatch(checks, self.alerts.send_text)

    def check_health(self) -> None:
        if self.health_watch is not None:
            self.health_watch.run()

    def send_put_forward_list_if_due(self) -> None:
        if self.put_forward_job is None:
            return
        try:
            if not self.put_forward_job.claim():
                return
            sent = self.interactions.put_forward.send_daily(interactive=self.inbound_enabled)
        except Exception:
            logger.exception("Failed to send the list of mails put forward; will retry")
            return
        logger.info("Sent the daily list of mails put forward: %d mail(s)", sent)

    def _build_put_forward_job(self) -> DailyJob | None:
        settings = self.settings
        if settings.attention_mode != "on" or settings.attention_list_hour < 0:
            return None
        if self.chat is None or not self.chat.is_configured:
            logger.warning("ATTENTION_LIST_HOUR ignored: no chat inbox is configured")
            return None
        return DailyJob(
            "put_forward_list", settings.attention_list_hour, settings.tzinfo, self.store
        )

    def _maintenance_due(self, task: str) -> bool:
        now = monotonic()
        last = self._last_maintenance.get(task)
        if last is not None and now - last < MAINTENANCE_INTERVAL_SECONDS:
            return False
        self._last_maintenance[task] = now
        return True

    def _resolve_inbound(self) -> bool:
        if not self.settings.telegram_inbound_enabled:
            return False
        if self.chat is None:
            logger.warning("TELEGRAM_INBOUND_ENABLED ignored: CHAT_INBOX is none")
            return False
        if not self.chat.is_configured:
            logger.warning("TELEGRAM_INBOUND_ENABLED ignored: Telegram is not configured")
            return False
        if not self.chat.inbound_authorized:
            logger.error(
                "TELEGRAM_INBOUND_ENABLED ignored: TELEGRAM_ALLOWED_USER_IDS is required when "
                "TELEGRAM_CHAT_ID is a group or channel"
            )
            return False
        return True

    def _build_unsubscribe_proposer(self) -> UnsubscribeProposer | None:
        if not self.settings.unsubscribe_proposals_enabled:
            return None
        # The proposal is only useful with its buttons, and those need the inbound listener.
        if not self.inbound_enabled:
            logger.warning("UNSUBSCRIBE_PROPOSALS_ENABLED ignored: chat inbound is not enabled")
            return None
        if self.components.unsubscriber is None:
            logger.warning("UNSUBSCRIBE_PROPOSALS_ENABLED ignored: UNSUBSCRIBER is none")
            return None
        return UnsubscribeProposer(
            self.store, self.chat, self.settings.unsubscribe_min_archived
        )

    def start_telegram_listener(self) -> None:
        if not self.inbound_enabled:
            return
        self.listener = threading.Thread(
            target=run_listener,
            args=(
                self.chat,
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
    # asyncio.create_task's result is never awaited or retrieved (see _start), so an
    # exception raised out of here would kill polling forever with zero log line - the task just
    # dies silently and nothing is ever processed again until the container restarts. Every
    # failure must therefore be caught and logged right here, never allowed to propagate.
    # Backup first, so the copy still holds what the prune is about to delete.
    ctx.backup_if_due()
    ctx.prune_if_due()
    ctx.send_put_forward_list_if_due()
    ctx.check_health()
    try:
        emails = ctx.mail.fetch_unread()
    except Exception as exc:
        logger.exception("Failed to fetch unread emails; will retry next cycle")
        Metrics.mark_poll_failure()
        # The loop is alive; a Gmail outage must not make the watchdog restart-loop the container.
        # The watchdog therefore stays quiet, so the user is told through the chat instead.
        ctx.health.beat()
        ctx.outage.record_failure(exc)
        return

    logger.info("Polled Gmail: %d unread email(s)", len(emails))
    Metrics.mark_poll_success()
    ctx.outage.record_success()
    ctx.health.beat()
    _process_each(ctx, emails)


def sync_history_once(ctx: ApplicationContext) -> None:
    # Runs inside startup: an exception here would keep the app from ever starting to poll.
    try:
        emails = ctx.mail.fetch_history()
    except Exception:
        logger.exception("Failed to fetch Gmail history; starting without the history sync")
        return
    _process_each(ctx, emails)


def _process_each(ctx: ApplicationContext, emails) -> None:
    for email in emails:
        if ctx.stopping.is_set():
            return
        try:
            ctx.process_email(email)
        except Exception:
            Metrics.mark_email_skipped()
            logger.exception("Failed to process email %s; skipping", email.id)
        ctx.health.beat()


async def polling_loop(ctx: ApplicationContext):
    while True:
        # poll_once blocks on network and LLM rate limiting; keep it off the event loop so /healthz stays responsive.
        await asyncio.to_thread(poll_once, ctx)
        await asyncio.sleep(ctx.settings.poll_interval_seconds)


def terminate_process(reason: str) -> None:
    logger.critical("Polling is unhealthy (%s); exiting so the container restarts", reason)
    os._exit(1)


async def _start(app: FastAPI, ctx: ApplicationContext) -> None:
    settings = ctx.settings
    logger.info("Starting my-gmail-assistant %s", app.version)
    app.state.connection_checks = await asyncio.to_thread(
        run_startup_checks, ctx.connection_probes(), StartupCheckMode(settings.startup_checks)
    )
    # send_text already logs and swallows delivery failures (AlertGateway._safe_send); this
    # only guards against an unexpected error in report formatting itself.
    try:
        await asyncio.to_thread(
            ctx.alerts.send_text, format_status_report(app.state.connection_checks)
        )
    except Exception:
        logger.exception("Failed to send startup status report to chat")

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


async def _stop(app: FastAPI, ctx: ApplicationContext) -> None:
    ctx.stopping.set()
    # Either task is missing when the startup failed before creating it.
    for name in ("polling_task", "watchdog_task"):
        task = getattr(app.state, name, None)
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
    await asyncio.to_thread(ctx.close)


def create_app(sync_history: bool | None = None) -> FastAPI:
    settings = Settings()
    if sync_history is not None:
        settings.sync_history = sync_history

    ctx = ApplicationContext(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            await _start(app, ctx)
            yield
        finally:
            await _stop(app, ctx)

    app = FastAPI(title="my-gmail-assistant", version=app_version(), lifespan=lifespan)
    app.state.ctx = ctx
    app.include_router(Metrics.router())

    @app.get("/healthz")
    async def healthz():
        task = getattr(app.state, "polling_task", None)
        problem = ctx.health.problem(task_done=task is not None and task.done())
        if problem:
            return JSONResponse({"status": "unhealthy", "reason": problem}, status_code=503)
        return {"status": "ok"}

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
