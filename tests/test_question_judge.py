import unittest
from unittest.mock import MagicMock, patch

from src.ports import QuestionJudge
from src.triage.engine import JEV_MODEL, JevClassifier
from src.triage.judge import JevQuestionJudge

STATE = {"subject": "Devis", "body": "Pouvez-vous confirmer ?", "today": "2026-10-09"}


def judge_answering(data):
    classifier = JevClassifier("https://jev.example", "key")
    classifier.ask = MagicMock(return_value=data)
    return JevQuestionJudge(classifier), classifier.ask


class JevQuestionJudgeTests(unittest.TestCase):
    def test_implements_the_port_and_follows_the_classifier_configuration(self):
        self.assertIsInstance(judge_answering({})[0], QuestionJudge)
        self.assertFalse(JevQuestionJudge(JevClassifier("https://jev.example", "")).is_configured)

    def test_asks_the_one_question_it_was_given_about_the_state(self):
        judge, ask = judge_answering({"answers": {"question": {"noul": 0.91}}})

        probability = judge.probability(
            STATE, "Does it ask to confirm?", "It asks.", "It does not."
        )

        self.assertEqual(probability, 0.91)
        self.assertEqual(
            ask.call_args.args[0],
            {
                "model": JEV_MODEL,
                "state": STATE,
                "questions": {
                    "question": {
                        "type": "noul",
                        "instructions": "Does it ask to confirm?",
                        "criteria": {"true": "It asks.", "false": "It does not."},
                    }
                },
            },
        )

    def test_a_missing_answer_is_an_error_not_a_no(self):
        for data in ({"answers": {}}, {"answers": {"question": {"noul": "high"}}}):
            with self.subTest(data=data):
                judge, _ = judge_answering(data)
                with self.assertRaises(ValueError):
                    judge.probability(STATE, "q", "y", "n")

    def test_usage_is_counted_as_the_agents(self):
        judge, _ = judge_answering(
            {
                "answers": {"question": {"noul": 0.5}},
                "usage": {"input_tokens": 120, "output_tokens": 4},
            }
        )

        with patch("src.triage.engine.Metrics") as metrics:
            judge.probability(STATE, "q", "y", "n")

        metrics.mark_llm_usage.assert_called_once_with("agent", 120, 4, cost_usd=None)


if __name__ == "__main__":
    unittest.main()
