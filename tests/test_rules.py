import unittest
from unittest.mock import MagicMock

from src.gmail.client import EmailMessage
from src.triage.rules import apply_rules
from src.workflow import EmailWorkflow


class ApplyRulesTests(unittest.TestCase):
    def test_linkedin_job_alerts_are_alerte_emploi(self):
        for sender in ("jobalerts-noreply@linkedin.com", "jobs-noreply@linkedin.com"):
            with self.subTest(sender=sender):
                result = apply_rules(sender)
                self.assertEqual((result.urgency, result.category, result.confidence), ("low", "alerte_emploi", 1.0))

    def test_substack_including_subdomains_is_newsletter(self):
        for sender in ("bytebytego@substack.com", "x@mail.substack.com"):
            with self.subTest(sender=sender):
                self.assertEqual(apply_rules(sender).category, "newsletter")

    def test_leboncoin_marketing_is_promotion(self):
        self.assertEqual(apply_rules("info@news.leboncoin.fr").category, "promotion")

    def test_rule_matching_is_case_insensitive(self):
        self.assertEqual(apply_rules("JobAlerts-NoReply@LinkedIn.com").category, "alerte_emploi")

    def test_unknown_and_lookalike_senders_do_not_match(self):
        for sender in ("celinekougang@outlook.com", "jobalerts-noreply@linkedin.com.evil.io", "a@fake-substack.com"):
            with self.subTest(sender=sender):
                self.assertIsNone(apply_rules(sender))

    def test_human_linkedin_messages_are_not_ruled(self):
        self.assertIsNone(apply_rules("messages-noreply@linkedin.com"))


class WorkflowRuleBypassTests(unittest.TestCase):
    def test_matched_sender_skips_jev_and_llm(self):
        engine, gemini = MagicMock(), MagicMock()
        email = EmailMessage(
            id="1", thread_id="t", sender="jobalerts-noreply@linkedin.com", subject="Thales recrute", snippet="s", body=""
        )

        result = EmailWorkflow(engine, gemini).run(email)

        engine.classify.assert_not_called()
        gemini.analyze.assert_not_called()
        self.assertEqual((result["triage"].category, result["route"]), ("alerte_emploi", "label"))

    def test_unmatched_sender_goes_through_the_classifier(self):
        engine, gemini = MagicMock(), MagicMock()
        engine.classify.return_value = MagicMock(urgency="low", category="personnel", confidence=0.9)
        email = EmailMessage(id="2", thread_id="t", sender="a@b.io", subject="Hi", snippet="s", body="")

        EmailWorkflow(engine, gemini).run(email)

        engine.classify.assert_called_once_with(email)
