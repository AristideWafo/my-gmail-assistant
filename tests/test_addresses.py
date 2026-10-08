import unittest

from src.domain import canonical_address


class CanonicalAddressTests(unittest.TestCase):
    def test_one_spelling_per_mailbox(self):
        cases = {
            "Jean@Example.com": "jean@example.com",
            "jean+invoices@example.com": "jean@example.com",
            "first.last@gmail.com": "firstlast@gmail.com",
            "First.Last+x@googlemail.com": "firstlast@gmail.com",
            "first.last@example.com": "first.last@example.com",
            " not-an-address ": "not-an-address",
        }
        for address, expected in cases.items():
            with self.subTest(address):
                self.assertEqual(canonical_address(address), expected)


if __name__ == "__main__":
    unittest.main()
