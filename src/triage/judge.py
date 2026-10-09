from src.triage.engine import JEV_MODEL, JevClassifier

QUESTION = "question"
USAGE_KIND = "agent"


class JevQuestionJudge:
    """Any yes/no question put to JEV about a state the caller describes."""

    def __init__(self, classifier: JevClassifier) -> None:
        self._classifier = classifier

    @property
    def is_configured(self) -> bool:
        return self._classifier.is_configured

    def probability(self, state: dict[str, str], question: str, yes: str, no: str) -> float:
        data = self._classifier.ask(
            {
                "model": JEV_MODEL,
                "state": state,
                "questions": {
                    QUESTION: {
                        "type": "noul",
                        "instructions": question,
                        "criteria": {"true": yes, "false": no},
                    }
                },
            }
        )
        probability = JevClassifier.parse_probability(data["answers"].get(QUESTION))
        if probability is None:
            raise ValueError("JEV gave no probability")
        JevClassifier.record_usage(data, kind=USAGE_KIND)
        return probability
