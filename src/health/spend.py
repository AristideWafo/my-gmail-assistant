import logging
from collections.abc import Callable
from datetime import UTC, datetime, tzinfo

from src.domain import EmailMessage, LLMAnalysis
from src.observability.metrics import Metrics
from src.ports import DecisionStore, EmailAnalyzer

logger = logging.getLogger(__name__)

SPEND_KEY_PREFIX = "llm_spend:"


class SpendTracker:
    """Estimated LLM cost of the local day, kept in the store so a restart does not reset it."""

    def __init__(
        self,
        store: DecisionStore,
        read_cost: Callable[[], float],
        daily_budget_usd: float,
        timezone: tzinfo,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._read_cost = read_cost
        self.daily_budget_usd = daily_budget_usd
        self._timezone = timezone
        self._clock = clock
        # The cost counter only lives as long as the process: only its growth is carried over.
        self._last_read = read_cost()

    def spent_today(self) -> float:
        current = self._read_cost()
        growth = max(current - self._last_read, 0.0)
        self._last_read = current
        key = f"{SPEND_KEY_PREFIX}{self._clock().astimezone(self._timezone).date().isoformat()}"
        spent = float(self._store.get_state(key) or 0.0) + growth
        if growth:
            self._store.set_state(key, f"{spent:.6f}")
        return spent

    def over_budget(self) -> bool:
        return self.daily_budget_usd > 0 and self.spent_today() >= self.daily_budget_usd


class BudgetedAnalyzer:
    """Stops calling the analyzer once the day's budget is spent; mails take the degraded path."""

    def __init__(self, analyzer: EmailAnalyzer, spend: SpendTracker) -> None:
        self._analyzer = analyzer
        self._spend = spend

    @property
    def is_configured(self) -> bool:
        return self._analyzer.is_configured

    def check_connection(self) -> str:
        return self._analyzer.check_connection()

    def analyze(self, email: EmailMessage, want_draft: bool, want_entities: bool) -> LLMAnalysis:
        if self._spend.over_budget():
            logger.warning("LLM daily budget reached; email %s goes without analysis", email.id)
            Metrics.mark_llm_error("budget")
            return LLMAnalysis()
        return self._analyzer.analyze(email, want_draft, want_entities)
