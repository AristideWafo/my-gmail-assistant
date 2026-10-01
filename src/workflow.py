import logging
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from src.domain import EmailMessage, LLMAnalysis, TriageResult
from src.formatting import truncate
from src.observability.metrics import Metrics
from src.ports import EmailAnalyzer, EmailClassifier
from src.triage.rules import RuleSet, is_automated_sender

logger = logging.getLogger(__name__)


class TriageState(TypedDict, total=False):
    email: EmailMessage
    triage: TriageResult
    summary: str
    draft: str
    route: str
    entities: dict[str, Any]
    reply_expected: bool


NON_ALERTABLE_CATEGORIES = {"spam", "newsletter", "promotion", "alerte_emploi"}
KEEP_VISIBLE_CATEGORIES = {"alerte_emploi"}
DRAFTABLE_CATEGORIES = {"personnel", "mise_en_relation", "offre_emploi"}
SUMMARY_UNAVAILABLE = "(résumé indisponible)"
FALLBACK_SNIPPET_LIMIT = 300


def route_for(triage: TriageResult, low_confidence_threshold: float) -> str:
    confident = triage.confidence >= low_confidence_threshold
    # Low confidence must never trigger an alert or an archive: an uncertain mail stays visible, labeled.
    if triage.category in NON_ALERTABLE_CATEGORIES:
        archivable = triage.urgency != "high" and confident and triage.category not in KEEP_VISIBLE_CATEGORIES
        return "reject" if archivable else "label"
    if triage.urgency == "high":
        return "llm"
    if triage.urgency == "low" and triage.category == "notification_systeme" and confident:
        return "reject"
    return "label"


def expects_reply(email: EmailMessage, triage: TriageResult, threshold: float | None) -> bool:
    if threshold is None or triage.needs_reply is None or triage.needs_reply < threshold:
        return False
    # Bulk and scam mail asks to be answered too: the category and the sender decide before the
    # model's opinion on the body does.
    return triage.category not in NON_ALERTABLE_CATEGORIES and not is_automated_sender(email.sender)


class EmailWorkflow:
    def __init__(
        self,
        classifier: EmailClassifier,
        analyzer: EmailAnalyzer,
        low_confidence_threshold: float = 0.50,
        rules: RuleSet | None = None,
        needs_reply_threshold: float | None = None,
    ):
        self.classifier = classifier
        self.analyzer = analyzer
        self.low_confidence_threshold = low_confidence_threshold
        self.rules = rules or RuleSet()
        # None turns reply drafts off for mails that are not urgent.
        self.needs_reply_threshold = needs_reply_threshold
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
        return {"triage": self.rules.classify(email) or self.classifier.classify(email)}

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

    def _label_node(self, state: TriageState) -> dict[str, Any]:
        email = state["email"]
        if not expects_reply(email, state["triage"], self.needs_reply_threshold):
            return {"route": "label"}
        result: dict[str, Any] = {"route": "label", "reply_expected": True}
        try:
            draft = self.analyzer.analyze(email, want_draft=True, want_entities=False).draft
        except Exception:
            # The mail is still flagged as awaiting a reply; only the draft is lost.
            logger.exception("Gemini draft failed for email %s; flagging without draft", email.id)
            Metrics.mark_llm_error("unavailable")
            draft = ""
        if draft:
            result["draft"] = draft
        return result

    def _route_after_triage(self, state: TriageState) -> str:
        return route_for(state["triage"], self.low_confidence_threshold)
