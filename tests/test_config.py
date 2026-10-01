import unittest

from pydantic import ValidationError

from src.config import Settings, parse_user_ids


class ParseUserIdsTests(unittest.TestCase):
    def test_comma_separated_ids_with_blanks_and_duplicates(self):
        self.assertEqual(parse_user_ids(" 7, 8,,7 "), frozenset({7, 8}))

    def test_empty_means_no_explicit_allowlist(self):
        self.assertEqual(parse_user_ids(""), frozenset())

    def test_non_integer_or_non_positive_ids_are_rejected(self):
        for raw in ("abc", "7,x", "-100123", "0", "1.5"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_user_ids(raw)


class SettingsAllowedUserIdsTests(unittest.TestCase):
    def test_parsed_from_settings(self):
        settings = Settings(_env_file=None, telegram_allowed_user_ids="7,8")

        self.assertEqual(settings.allowed_user_ids, frozenset({7, 8}))

    def test_invalid_value_fails_at_startup(self):
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, telegram_allowed_user_ids="me")


class SettingsBackupTests(unittest.TestCase):
    def test_backups_are_off_by_default_and_keep_a_week(self):
        settings = Settings(_env_file=None)

        self.assertEqual((settings.backup_dir, settings.backup_keep), ("", 7))

    def test_keeping_no_backup_fails_at_startup(self):
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, backup_keep=0)


if __name__ == "__main__":
    unittest.main()
