import time
from collections.abc import Callable

from src.observability.metrics import Metrics

from .spend import SpendTracker
from .watch import Check, RecentIncrease

LISTENER_SILENT_AFTER_SECONDS = 600


def _minutes(seconds: float) -> int:
    return max(1, round(seconds / 60))


def _burst(
    name: str,
    read: Callable[[], float],
    window_seconds: float,
    min_events: int,
    describe: Callable[[int, int], str],
    recovery: str,
    clock: Callable[[], float],
) -> Check:
    increase = RecentIncrease(read, window_seconds, clock)

    def problem() -> str | None:
        events = round(increase.value())
        return describe(events, _minutes(window_seconds)) if events >= min_events else None

    return Check(name, problem, recovery)


def counter_checks(
    window_seconds: float, min_events: int, clock: Callable[[], float] = time.monotonic
) -> list[Check]:
    """One check per failure counter: too many failures over the window."""
    return [
        _burst(
            "jev_fallback",
            lambda: Metrics.total(Metrics.jev_fallback),
            window_seconds,
            min_events,
            lambda events, minutes: (
                f"⚠️ JEV ne répond pas : {events} mails classés par l'heuristique de repli en "
                f"{minutes} min. Le tri continue, moins précis."
            ),
            "✅ JEV : plus aucun repli sur l'heuristique.",
            clock,
        ),
        _burst(
            "emails_skipped",
            lambda: Metrics.total(Metrics.emails_skipped),
            window_seconds,
            min_events,
            lambda events, minutes: (
                f"⚠️ {events} traitements de mail en échec en {minutes} min. Ces mails restent "
                "non lus et sont réessayés à chaque relève."
            ),
            "✅ Plus aucun traitement de mail en échec.",
            clock,
        ),
        _burst(
            "llm_errors",
            # A call skipped for the budget is not an error of the LLM; the budget has its check.
            lambda: Metrics.total(Metrics.llm_errors, without={"reason": "budget"}),
            window_seconds,
            min_events,
            lambda events, minutes: (
                f"⚠️ Gemini en erreur : {events} appels en échec en {minutes} min. Les alertes "
                "partent sans résumé ni brouillon."
            ),
            "✅ Gemini : plus aucune erreur.",
            clock,
        ),
    ]


def listener_check(
    is_alive: Callable[[], bool],
    clock: Callable[[], float] = time.time,
    silent_after_seconds: float = LISTENER_SILENT_AFTER_SECONDS,
) -> Check:
    """The chat listener stopped, or has not completed a poll for too long."""
    started = clock()

    def problem() -> str | None:
        # Before the first successful poll the gauge is 0: count the silence from the start.
        last_poll = Metrics.total(Metrics.telegram_last_poll) or started
        silent_for = clock() - last_poll
        if is_alive() and silent_for < silent_after_seconds:
            return None
        return (
            "⚠️ Boutons et commandes Telegram hors service : aucune relève réussie depuis "
            f"{_minutes(silent_for)} min. Les alertes continuent d'arriver."
        )

    return Check("chat_listener", problem, "✅ Boutons et commandes Telegram rétablis.")


def budget_check(spend: SpendTracker) -> Check:
    def problem() -> str | None:
        if not spend.over_budget():
            return None
        return (
            f"⚠️ Budget LLM du jour atteint : {spend.spent_today():.2f} $ sur "
            f"{spend.daily_budget_usd:.2f} $. Jusqu'à demain, les alertes partent sans résumé ni "
            "brouillon."
        )

    return Check("llm_budget", problem)
