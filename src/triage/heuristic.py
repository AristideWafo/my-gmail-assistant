from src.domain import EmailMessage, TriageResult


class HeuristicClassifier:
    @property
    def is_configured(self) -> bool:
        return True

    def check_connection(self) -> str:
        return "local heuristic"

    def classify(self, email: EmailMessage) -> TriageResult:
        text = f"{email.subject} {email.snippet}".lower()
        sender = f"{email.sender} {email.sender_domain}".lower()

        if any(term in sender for term in ["no-reply", "noreply", "newsletter"]) or "unsubscribe" in text:
            return self._result("low", "newsletter", 0.70)

        if any(term in text for term in ["urgent", "asap", "immediately", "deadline"]):
            return self._result("high", "personnel", 0.76)

        if any(term in text for term in ["offer", "interview", "recruiter", "position"]):
            return self._result("medium", "offre_emploi", 0.68)

        return self._result("low", "personnel", 0.60)

    @staticmethod
    def _result(urgency: str, category: str, confidence: float) -> TriageResult:
        return TriageResult(urgency, category, confidence, source="heuristic")
