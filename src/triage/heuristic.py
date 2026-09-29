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
            return TriageResult(urgency="low", category="newsletter", confidence=0.70)

        if any(term in text for term in ["urgent", "asap", "immediately", "deadline"]):
            return TriageResult(urgency="high", category="personnel", confidence=0.76)

        if any(term in text for term in ["offer", "interview", "recruiter", "position"]):
            return TriageResult(urgency="medium", category="offre_emploi", confidence=0.68)

        return TriageResult(urgency="low", category="personnel", confidence=0.60)
