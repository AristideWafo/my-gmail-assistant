import logging
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from src.domain import EmailMessage, LLMAnalysis, TriageResult
from src.formatting import truncate
from src.observability.metrics import Metrics
from src.ports import EmailAnalyzer, EmailClassifier
from src.triage.rules import apply_rules, is_automated_sender

logger = logging.getLogger(__name__)


class TriageState(TypedDict, total=False):
    email: EmailMessage
    triage: TriageResult
    summary: str
    draft: str
    route: str
    entities: dict[str, Any]


NON_ALERTABLE_CATEGORIES = {"spam", "newsletter", "promotion", "alerte_emploi"}
KEEP_VISIBLE_CATEGORIES = {"alerte_emploi"}
DRAFTABLE_CATEGORIES = {"personnel", "mise_en_relation", "offre_emploi"}
SUMMARY_UNAVAILABLE = "(résumé indisponible)"
FALLBACK_SNIPPET_LIMIT = 300


class EmailWorkflow:
    def __init__(
        self,
        classifier: EmailClassifier,
        analyzer: EmailAnalyzer,
        low_confidence_threshold: float = 0.50,
    ):
        self.classifier = classifier
        self.analyzer = analyzer
        self.low_confidence_threshold = low_confidence_threshold
        self.graph = self._build_graph()

    def _build_graph(self):
        workflow = StateGraph(TriageState)
        workflow.add_node("classify", self._triage_node)
        workflow.add_node("llm", self._llm_node)
        workflow.add_node("reject", self._reject_node)
        workflow.add_node("label", self._label_node)

        workflow.set_entry_point("classify")
        workflow.add_conditional_edges(
            "classify",
            self._route_after_triage,
            {
                "llm": "llm",
                "reject": "reject",
                "label": "label",
            },
        )
        workflow.add_edge("llm", END)
        workflow.add_edge("reject", END)
        workflow.add_edge("label", END)
        return workflow.compile()

    def run(self, email: EmailMessage) -> dict[str, Any]:
        return self.graph.invoke({"email": email})

    def _triage_node(self, state: TriageState) -> dict[str, Any]:
        email = state["email"]
        return {"triage": apply_rules(email.sender) or self.classifier.classify(email)}

    def _llm_node(self, state: TriageState) -> dict[str, Any]:
        email = state["email"]
        triage = state["triage"]
        want_draft = triage.category in DRAFTABLE_CATEGORIES and not is_automated_sender(email.sender)
        want_entities = triage.category == "offre_emploi"
        try:
            analysis = self.analyzer.analyze(email, want_draft, want_entities)
        except Exception:
            # An urgent alert must still go out when the LLM is down or over quota.
            logger.exception("Gemini analysis failed for email %s; alerting without summary", email.id)
            Metrics.mark_llm_error("unavailable")
            analysis = LLMAnalysis()

        summary = analysis.summary or f"{truncate(email.snippet, FALLBACK_SNIPPET_LIMIT)}\n{SUMMARY_UNAVAILABLE}"
        result: dict[str, Any] = {"summary": summary, "route": "llm"}
        if analysis.draft:
            result["draft"] = analysis.draft
        if analysis.entities:
            result["entities"] = analysis.entities
        return result

    @staticmethod
    def _reject_node(_: TriageState) -> dict[str, Any]:
        return {"route": "reject"}

    @staticmethod
    def _label_node(_: TriageState) -> dict[str, Any]:
        return {"route": "label"}

    def _route_after_triage(self, state: TriageState) -> str:
        triage = state["triage"]
        confident = triage.confidence >= self.low_confidence_threshold
        # Low confidence must never trigger an alert or an archive: an uncertain mail stays visible, labeled.
        if triage.category in NON_ALERTABLE_CATEGORIES:
            archivable = triage.urgency != "high" and confident and triage.category not in KEEP_VISIBLE_CATEGORIES
            return "reject" if archivable else "label"
        if triage.urgency == "high":
            return "llm"
        if triage.urgency == "low" and triage.category == "notification_systeme" and confident:
            return "reject"
        return "label"
