import unittest

from src.domain import TriageResult
from src.triage import HeuristicClassifier
from src.workflow import EmailWorkflow

# (urgency, category, confidence, expected route) taken from a real production log where
# uncertain or low-urgency mails wrongly triggered "Urgent email detected" alerts.
OBSERVED_CASES = [
    ("high", "notification_systeme", 0.34, "llm"),
    ("high", "personnel", 0.89, "llm"),
    ("high", "newsletter", 0.95, "label"),
    ("medium", "notification_systeme", 0.05, "label"),
    ("medium", "notification_systeme", 0.84, "label"),
    ("low", "notification_systeme", 0.30, "label"),
    ("low", "notification_systeme", 0.62, "reject"),
    ("low", "offre_emploi", 0.37, "label"),
    ("low", "newsletter", 0.30, "label"),
    ("low", "newsletter", 0.99, "reject"),
    ("low", "promotion", 0.90, "reject"),
    ("low", "promotion", 0.30, "label"),
    ("high", "promotion", 0.90, "label"),
    ("low", "alerte_emploi", 1.0, "label"),
    ("high", "alerte_emploi", 0.80, "label"),
    ("high", "alerte_technique", 0.60, "llm"),
    ("low", "alerte_technique", 0.90, "label"),
]


class RoutingRegressionTests(unittest.TestCase):
    def setUp(self):
        self.workflow = EmailWorkflow(HeuristicClassifier(), analyzer=None)

    def route(self, urgency, category, confidence):
        return self.workflow._route_after_triage({"triage": TriageResult(urgency, category, confidence)})

    def test_observed_cases_route_as_expected(self):
        for urgency, category, confidence, expected in OBSERVED_CASES:
            with self.subTest(urgency=urgency, category=category, confidence=confidence):
                self.assertEqual(self.route(urgency, category, confidence), expected)

    def test_only_high_urgency_outside_non_alertable_categories_reaches_llm(self):
        llm_cases = {(u, c) for u, c, conf, route in OBSERVED_CASES if route == "llm"}
        self.assertEqual(llm_cases, {("high", "notification_systeme"), ("high", "personnel"), ("high", "alerte_technique")})

    def test_uncertain_mail_is_never_archived(self):
        for category in ("notification_systeme", "newsletter", "spam"):
            with self.subTest(category=category):
                self.assertEqual(self.route("low", category, 0.10), "label")

    def test_threshold_is_configurable(self):
        workflow = EmailWorkflow(HeuristicClassifier(), analyzer=None, low_confidence_threshold=0.80)
        state = {"triage": TriageResult("low", "notification_systeme", 0.70)}
        self.assertEqual(workflow._route_after_triage(state), "label")


if __name__ == "__main__":
    unittest.main()
