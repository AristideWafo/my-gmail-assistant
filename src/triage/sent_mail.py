from datetime import UTC, datetime

from src.triage.engine import JEV_MODEL, JevClassifier

# Asked about my own text with the quoted mail cut off: with the quote, the question I was
# answering would make every "Merci, bien reçu" look like it awaits an answer.
EXPECTS_ANSWER_QUESTION = {
    "type": "noul",
    "instructions": (
        "This email was written and sent by its author. Does the author now wait for something "
        "from a recipient: an answer, a document, a decision, a confirmation or an action? "
        "Judge from `body`, which holds only what the author wrote, then `subject`."
    ),
    "criteria": {
        "true": "The author asks a question, requests something, proposes a date or an option "
        "to accept, or asks to be told, sent or confirmed something",
        "false": "Nothing awaited: thanks, acknowledgement, an answer to the recipient's own "
        "request, information only, a confirmation of something already settled, a closing "
        "or farewell message, or an empty text",
    },
}


class JevSentMailJudge:
    def __init__(self, classifier: JevClassifier) -> None:
        self._classifier = classifier

    @property
    def is_configured(self) -> bool:
        return self._classifier.is_configured

    def build_request(self, subject: str, text: str) -> dict:
        return {
            "model": JEV_MODEL,
            "state": {
                "subject": subject,
                "body": text,
                "today": datetime.now(UTC).date().isoformat(),
            },
            "questions": {"expects_answer": EXPECTS_ANSWER_QUESTION},
        }

    def expects_answer(self, subject: str, text: str) -> float:
        data = self._classifier.ask(self.build_request(subject, text))
        probability = JevClassifier.parse_probability(data["answers"].get("expects_answer"))
        if probability is None:
            raise ValueError("JEV gave no expects_answer probability")
        JevClassifier.record_usage(data, kind="followup")
        return probability
