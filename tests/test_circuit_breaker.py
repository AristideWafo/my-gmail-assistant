import unittest

from src.gateways.circuit_breaker import CircuitBreaker


class CircuitBreakerTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=100, clock=lambda: self.now)

    def test_allows_calls_while_failures_stay_under_the_threshold(self):
        self.assertFalse(self.breaker.record_failure())
        self.assertFalse(self.breaker.record_failure())
        self.assertTrue(self.breaker.allow())

    def test_opens_after_consecutive_failures_and_reports_it_once(self):
        results = [self.breaker.record_failure() for _ in range(3)]

        self.assertEqual(results, [False, False, True])
        self.assertFalse(self.breaker.allow())

    def test_success_resets_the_failure_count(self):
        self.breaker.record_failure()
        self.breaker.record_failure()
        self.breaker.record_success()

        self.assertFalse(self.breaker.record_failure())
        self.assertTrue(self.breaker.allow())

    def test_closes_again_after_the_cooldown(self):
        for _ in range(3):
            self.breaker.record_failure()
        self.now = 99
        self.assertFalse(self.breaker.allow())
        self.now = 100
        self.assertTrue(self.breaker.allow())
