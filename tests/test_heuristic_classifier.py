import unittest

from src.domain import EmailMessage
from src.ports import EmailClassifier
from src.triage.heuristic import HeuristicClassifier


def email(sender="person@example.com", subject="", snippet="", sender_domain=""):
    return EmailMessage(
        id="1",
        thread_id="t1",
        sender=sender,
        subject=subject,
        snippet=snippet,
        body="",
        sender_domain=sender_domain,
    )


class HeuristicClassifierTests(unittest.TestCase):
    def setUp(self):
        self.classifier = HeuristicClassifier()

    def classify(self, **fields):
        result = self.classifier.classify(email(**fields))
        return result.urgency, result.category, result.confidence

    def test_implements_classifier_port_and_is_always_configured(self):
        self.assertIsInstance(self.classifier, EmailClassifier)
        self.assertTrue(self.classifier.is_configured)
        self.assertEqual(self.classifier.check_connection(), "local heuristic")

    def test_newsletter_detected_by_sender(self):
        self.assertEqual(
            self.classify(sender="newsletter@brand.com", subject="This week's digest"),
            ("low", "newsletter", 0.70),
        )

    def test_newsletter_detected_by_sender_domain_or_unsubscribe(self):
        self.assertEqual(self.classify(sender_domain="noreply.example")[:2], ("low", "newsletter"))
        self.assertEqual(self.classify(snippet="click to Unsubscribe")[:2], ("low", "newsletter"))

    def test_newsletter_wins_over_urgent_wording(self):
        self.assertEqual(self.classify(sender="no-reply@x.io", subject="URGENT")[:2], ("low", "newsletter"))

    def test_urgent_wording_is_high_personnel(self):
        self.assertEqual(
            self.classify(subject="Urgent: please respond", snippet="Need this immediately"),
            ("high", "personnel", 0.76),
        )

    def test_job_wording_is_medium_offer(self):
        self.assertEqual(
            self.classify(subject="Job offer", snippet="We'd like to interview you"),
            ("medium", "offre_emploi", 0.68),
        )

    def test_default_is_low_personnel(self):
        self.assertEqual(self.classify(subject="Hello", snippet="notes attached"), ("low", "personnel", 0.60))


if __name__ == "__main__":
    unittest.main()
