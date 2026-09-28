import unittest

from src.expiring_set import ExpiringSet


class ExpiringSetTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.items = ExpiringSet(ttl_seconds=60, clock=lambda: self.now)

    def test_contains_added_key_within_ttl(self):
        self.items.add("a")
        self.now += 59
        self.assertIn("a", self.items)

    def test_key_expires_after_ttl(self):
        self.items.add("a")
        self.now += 60
        self.assertNotIn("a", self.items)

    def test_unknown_key_is_absent(self):
        self.assertNotIn("missing", self.items)

    def test_re_adding_refreshes_expiry(self):
        self.items.add("a")
        self.now += 50
        self.items.add("a")
        self.now += 50
        self.assertIn("a", self.items)
