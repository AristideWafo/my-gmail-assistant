import unittest
from unittest.mock import MagicMock

from src.observability.metrics import Metrics
from src.ports import SentMailJudge
from src.triage.engine import JevClassifier
from src.triage.sent_mail import EXPECTS_ANSWER_QUESTION, JevSentMailJudge


def judge_answering(data):
    classifier = JevClassifier("https://jev.example", "key")
    classifier.ask = MagicMock(return_value=data)
    return JevSentMailJudge(classifier), classifier.ask


class JevSentMailJudgeTests(unittest.TestCase):
    def test_implements_the_port_and_follows_the_classifier_configuration(self):
        self.assertIsInstance(judge_answering({})[0], SentMailJudge)
        self.assertFalse(JevSentMailJudge(JevClassifier("https://jev.example", "")).is_configured)

    def test_asks_one_question_about_my_text_only(self):
        judge, ask = judge_answering({"answers": {"expects_answer": {"noul": 0.97}}})

        probability = judge.expects_answer("Devis", "Pouvez-vous m'envoyer le devis ?")

        self.assertEqual(probability, 0.97)
        request = ask.call_args.args[0]
        self.assertEqual(list(request["questions"]), ["expects_answer"])
        self.assertEqual(request["questions"]["expects_answer"], EXPECTS_ANSWER_QUESTION)
        self.assertEqual(request["state"]["body"], "Pouvez-vous m'envoyer le devis ?")
        self.assertEqual(request["state"]["subject"], "Devis")
        self.assertNotIn("sender", request["state"])

    def test_a_missing_answer_is_an_error_not_a_no(self):
        for data in ({"answers": {}}, {"answers": {"expects_answer": {"noul": "high"}}}):
            with self.subTest(data=data):
                judge, _ = judge_answering(data)
                with self.assertRaises(ValueError):
                    judge.expects_answer("s", "t")

    def test_tokens_are_counted_apart_from_triage(self):
        judge, _ = judge_answering(
            {"answers": {"expects_answer": {"noul": 0.1}}, "usage": {"input_tokens": 300, "output_tokens": 2}}
        )
        prompt = Metrics.llm_tokens.labels(kind="followup", token_type="prompt")
        before = prompt._value.get()

        judge.expects_answer("s", "t")

        self.assertEqual(prompt._value.get(), before + 300)


if __name__ == "__main__":
    unittest.main()
