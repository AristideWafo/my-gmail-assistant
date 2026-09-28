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


class EmailWorkflow:
    def __init__(self, decision_engine: DecisionEngineClient, gemini: GeminiClient):
        self.decision_engine = decision_engine
        self.gemini = gemini
        self.graph = self._build_graph()

    def _build_graph(self):
        workflow = StateGraph(TriageState)
        workflow.add_node("classify", self._triage_node)
        workflow.add_node("llm", self._llm_node)
        workflow.add_node("archive", self._archive_node)
        workflow.add_node("label", self._label_node)

        workflow.set_entry_point("classify")
        workflow.add_conditional_edges(
            "classify",
            self._route_after_triage,
            {
                "llm": "llm",
                "archive": "archive",
                "label": "label",
            },
        )
        workflow.add_edge("llm", END)
        workflow.add_edge("archive", END)
        workflow.add_edge("label", END)
        return workflow.compile()

    def run(self, email: EmailMessage) -> dict[str, Any]:
        return self.graph.invoke({"email": email})

    def _triage_node(self, state: TriageState) -> dict[str, Any]:
        triage = self.decision_engine.classify(state["email"])
        return {"triage": triage}

    def _llm_node(self, state: TriageState) -> dict[str, Any]:
        email = state["email"]
        summary = self.gemini.summarize(email)
        draft = self.gemini.draft_reply(email)
        return {"summary": summary, "draft": draft, "route": "llm"}

    @staticmethod
    def _archive_node(_: TriageState) -> dict[str, Any]:
        return {"route": "archive"}

    @staticmethod
    def _label_node(_: TriageState) -> dict[str, Any]:
        return {"route": "label"}

    @staticmethod
    def _route_after_triage(state: TriageState) -> str:
        triage = state["triage"]
        if triage.urgency == "high" or triage.confidence < 0.50:
            return "llm"
        if triage.urgency == "low" and triage.category == "general":
            return "archive"
        return "label"
