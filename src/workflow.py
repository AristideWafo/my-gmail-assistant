from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from src.gmail.client import EmailMessage
from src.llm.gemini import GeminiClient
from src.triage.engine import DecisionEngineClient, TriageResult


class TriageState(TypedDict, total=False):
    email: EmailMessage
    triage: TriageResult
    summary: str
    draft: str
    route: str
    entities: dict[str, Any]


NON_ALERTABLE_CATEGORIES = {"spam", "newsletter"}


class EmailWorkflow:
    def __init__(
        self, decision_engine: DecisionEngineClient, gemini: GeminiClient, low_confidence_threshold: float = 0.50
    ):
        self.decision_engine = decision_engine
        self.gemini = gemini
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
        triage = self.decision_engine.classify(state["email"])
        return {"triage": triage}

    def _llm_node(self, state: TriageState) -> dict[str, Any]:
        email = state["email"]
        triage = state["triage"]
        summary = self.gemini.summarize(email)
        draft = self.gemini.draft_reply(email)
        result: dict[str, Any] = {"summary": summary, "draft": draft, "route": "llm"}
        if triage.category == "offre_emploi":
            result["entities"] = self.gemini.extract_job_entities(email)
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
            return "reject" if triage.urgency != "high" and confident else "label"
        if triage.urgency == "high":
            return "llm"
        if triage.urgency == "low" and triage.category == "notification_systeme" and confident:
            return "reject"
        return "label"
