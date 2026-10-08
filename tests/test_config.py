import unittest

from pydantic import ValidationError

from src.config import Settings, in_quiet_hours, parse_quiet_hours, parse_user_ids


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


class SettingsPollFailureAlertTests(unittest.TestCase):
    def test_alerts_after_ten_minutes_by_default(self):
        self.assertEqual(Settings(_env_file=None).poll_failure_alert_minutes, 10)

    def test_negative_delay_fails_at_startup(self):
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, poll_failure_alert_minutes=-1)


class SettingsBackupTests(unittest.TestCase):
    def test_backups_are_off_by_default_and_keep_a_week(self):
        settings = Settings(_env_file=None)

        self.assertEqual((settings.backup_dir, settings.backup_keep), ("", 7))

    def test_keeping_no_backup_fails_at_startup(self):
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, backup_keep=0)


class SettingsAttentionTests(unittest.TestCase):
    def test_off_by_default_with_an_even_threshold(self):
        settings = Settings(_env_file=None)

        self.assertEqual((settings.attention_mode, settings.attention_threshold), ("off", 0.5))

    def test_unknown_mode_or_threshold_out_of_range_fails_at_startup(self):
        self.assertEqual(Settings(_env_file=None, attention_mode="on").attention_mode, "on")
        for overrides in ({"attention_mode": "loud"}, {"attention_threshold": 1.5}):
            with self.subTest(overrides), self.assertRaises(ValidationError):
                Settings(_env_file=None, **overrides)


class SettingsScheduleTests(unittest.TestCase):
    def test_daily_list_is_off_and_times_are_utc_by_default(self):
        settings = Settings(_env_file=None)

        self.assertEqual((settings.attention_list_hour, settings.timezone), (-1, "UTC"))

    def test_time_zone_is_resolved(self):
        settings = Settings(_env_file=None, timezone="Europe/Paris")

        self.assertEqual(str(settings.tzinfo), "Europe/Paris")

    def test_unknown_time_zone_or_hour_fails_at_startup(self):
        for overrides in ({"timezone": "Mars/Olympus"}, {"attention_list_hour": 24}):
            with self.subTest(overrides), self.assertRaises(ValidationError):
                Settings(_env_file=None, **overrides)


class QuietHoursTests(unittest.TestCase):
    def test_parsed_as_a_start_and_an_end_hour(self):
        self.assertEqual(parse_quiet_hours("22-8"), (22, 8))
        self.assertEqual(parse_quiet_hours(" 13-14 "), (13, 14))
        self.assertIsNone(parse_quiet_hours(""))

    def test_malformed_windows_are_rejected(self):
        for value in ("22", "22-", "a-8", "22-24", "-1-8", "8-8", "22-8-1"):
            with self.subTest(value), self.assertRaises(ValueError):
                parse_quiet_hours(value)

    def test_a_window_may_cross_midnight(self):
        night = (22, 8)
        quiet = [hour for hour in range(24) if in_quiet_hours(hour, night)]

        self.assertEqual(quiet, [0, 1, 2, 3, 4, 5, 6, 7, 22, 23])

    def test_a_window_within_the_day_ends_before_its_last_hour(self):
        lunch = (12, 14)

        self.assertEqual([h for h in range(24) if in_quiet_hours(h, lunch)], [12, 13])
        self.assertFalse(in_quiet_hours(3, None))


class SettingsProactiveBudgetTests(unittest.TestCase):
    def test_six_messages_a_day_and_no_quiet_hours_by_default(self):
        settings = Settings(_env_file=None)

        self.assertEqual((settings.proactive_daily_cap, settings.quiet_hours_window), (6, None))

    def test_quiet_hours_are_read_from_the_environment_format(self):
        settings = Settings(_env_file=None, quiet_hours="22-8")

        self.assertEqual(settings.quiet_hours_window, (22, 8))

    def test_invalid_values_fail_at_startup(self):
        for overrides in ({"quiet_hours": "late"}, {"proactive_daily_cap": 0}):
            with self.subTest(overrides), self.assertRaises(ValidationError):
                Settings(_env_file=None, **overrides)

    def test_a_daily_list_hour_inside_the_quiet_hours_fails_at_startup(self):
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, quiet_hours="22-8", attention_list_hour=7)

        settings = Settings(_env_file=None, quiet_hours="22-8", attention_list_hour=8)
        self.assertEqual(settings.attention_list_hour, 8)


if __name__ == "__main__":
    unittest.main()
