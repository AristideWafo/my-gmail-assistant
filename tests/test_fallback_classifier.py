import unittest
from unittest.mock import patch

import requests

from src.domain import EmailMessage, TriageResult
from src.observability.metrics import Metrics
from src.ports import EmailClassifier
from src.triage import FallbackClassifier, HeuristicClassifier, JevClassifier
from src.triage.engine import JEV_RECOVERABLE_ERRORS

EMAIL = EmailMessage(id="1", thread_id="t1", sender="a@b.io", subject="s", snippet="n", body="")


def jev_fallback(primary, secondary):
    return FallbackClassifier(
        primary, secondary, JEV_RECOVERABLE_ERRORS, on_fallback=Metrics.mark_jev_fallback
    )


class StubClassifier:
    def __init__(self, result=None, error=None, configured=True, connection="ok"):
        self.result = result
        self.error = error
        self.configured = configured
        self.connection = connection
        self.calls = []

    @property
    def is_configured(self) -> bool:
        return self.configured

    def check_connection(self) -> str:
        return self.connection

    def classify(self, email: EmailMessage) -> TriageResult:
        self.calls.append(email)
        if self.error:
            raise self.error
        return self.result


PRIMARY_RESULT = TriageResult("medium", "offre_emploi", 0.9)
SECONDARY_RESULT = TriageResult("low", "personnel", 0.6)


class FallbackClassifierTests(unittest.TestCase):
    def setUp(self):
        self.before = Metrics.jev_fallback._value.get()

    def fallback_count(self):
        return Metrics.jev_fallback._value.get() - self.before

    def test_implements_classifier_port(self):
        self.assertIsInstance(jev_fallback(StubClassifier(), StubClassifier()), EmailClassifier)

    def test_uses_primary_when_it_succeeds(self):
        secondary = StubClassifier(SECONDARY_RESULT)
        classifier = jev_fallback(StubClassifier(PRIMARY_RESULT), secondary)

        self.assertEqual(classifier.classify(EMAIL), PRIMARY_RESULT)
        self.assertEqual(secondary.calls, [])
        self.assertEqual(self.fallback_count(), 0)

    def test_primary_failure_falls_back_logs_and_counts_metric(self):
        for error in (requests.RequestException("boom"), KeyError("answers"), ValueError("x"), TypeError("y")):
            with self.subTest(error=type(error).__name__):
                before = Metrics.jev_fallback._value.get()
                secondary = StubClassifier(SECONDARY_RESULT)
                classifier = jev_fallback(StubClassifier(error=error), secondary)

                with self.assertLogs("src.triage.fallback", level="WARNING") as logs:
                    result = classifier.classify(EMAIL)

                self.assertEqual(result, SECONDARY_RESULT)
                self.assertEqual(secondary.calls, [EMAIL])
                self.assertEqual(Metrics.jev_fallback._value.get(), before + 1)
                self.assertIn("falling back", logs.output[0])

    def test_unexpected_primary_error_propagates(self):
        classifier = jev_fallback(
            StubClassifier(error=RuntimeError("bug")), StubClassifier(SECONDARY_RESULT)
        )

        with self.assertRaises(RuntimeError):
            classifier.classify(EMAIL)

    def test_unconfigured_primary_goes_straight_to_secondary_without_metric(self):
        primary = StubClassifier(PRIMARY_RESULT, configured=False)
        classifier = jev_fallback(primary, StubClassifier(SECONDARY_RESULT))

        with self.assertNoLogs("src.triage.fallback", level="WARNING"):
            self.assertEqual(classifier.classify(EMAIL), SECONDARY_RESULT)

        self.assertEqual(primary.calls, [])
        self.assertEqual(self.fallback_count(), 0)

    def test_is_configured_when_either_classifier_is(self):
        cases = [((True, False), True), ((False, True), True), ((False, False), False)]
        for (primary, secondary), expected in cases:
            with self.subTest(primary=primary, secondary=secondary):
                classifier = jev_fallback(
                    StubClassifier(configured=primary), StubClassifier(configured=secondary)
                )
                self.assertIs(classifier.is_configured, expected)

    def test_check_connection_delegates_to_primary(self):
        classifier = jev_fallback(
            StubClassifier(connection="API key accepted"), StubClassifier(connection="local heuristic")
        )

        self.assertEqual(classifier.check_connection(), "API key accepted")


class JevWithHeuristicFallbackTests(unittest.TestCase):
    def setUp(self):
        self.email = EmailMessage(
            id="1",
            thread_id="t1",
            sender="person@example.com",
            subject="Urgent: please respond",
            snippet="Need this immediately",
            body="",
        )

    def classifier(self, api_key="k"):
        return jev_fallback(
            JevClassifier("https://jev.example/triage", api_key=api_key), HeuristicClassifier()
        )

    def test_jev_request_error_uses_heuristic_and_counts_metric(self):
        before = Metrics.jev_fallback._value.get()

        with patch("src.triage.engine.requests.post", side_effect=requests.RequestException("boom")):
            result = self.classifier().classify(self.email)

        self.assertEqual((result.urgency, result.category), ("high", "personnel"))
        self.assertEqual(Metrics.jev_fallback._value.get(), before + 1)

    def test_jev_without_key_is_skipped(self):
        with patch("src.triage.engine.requests.post") as post:
            result = self.classifier(api_key="").classify(self.email)

        post.assert_not_called()
        self.assertEqual(result.urgency, "high")


if __name__ == "__main__":
    unittest.main()


class GenericFallbackTests(unittest.TestCase):
    def test_only_declared_errors_trigger_the_fallback(self):
        calls = []
        classifier = FallbackClassifier(
            StubClassifier(error=OSError("down")),
            StubClassifier(SECONDARY_RESULT),
            recoverable=(OSError,),
            on_fallback=lambda: calls.append(1),
        )

        self.assertEqual(classifier.classify(EMAIL), SECONDARY_RESULT)
        self.assertEqual(calls, [1])

    def test_undeclared_errors_propagate(self):
        classifier = FallbackClassifier(
            StubClassifier(error=KeyError("x")), StubClassifier(SECONDARY_RESULT), (OSError,)
        )

        with self.assertRaises(KeyError):
            classifier.classify(EMAIL)
