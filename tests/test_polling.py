import logging
import unittest
from unittest.mock import MagicMock

from main import poll_once


def make_ctx():
    ctx = MagicMock()
    ctx.stopping.is_set.return_value = False
    return ctx


class PollOnceTests(unittest.TestCase):
    def test_processes_every_fetched_email(self):
        ctx = make_ctx()
        emails = [MagicMock(id="1"), MagicMock(id="2")]
        ctx.mail.fetch_unread.return_value = emails

        poll_once(ctx)

        ctx.process_email.assert_any_call(emails[0])
        ctx.process_email.assert_any_call(emails[1])
        self.assertEqual(ctx.process_email.call_count, 2)

    def test_one_failing_email_does_not_stop_the_others_or_raise(self):
        ctx = make_ctx()
        good, bad = MagicMock(id="good"), MagicMock(id="bad")
        ctx.mail.fetch_unread.return_value = [bad, good]
        ctx.process_email.side_effect = [RuntimeError("boom"), None]

        with self.assertLogs("gmail-assistant", level=logging.ERROR) as logs:
            poll_once(ctx)  # must not raise

        ctx.process_email.assert_any_call(good)
        self.assertIn("bad", logs.output[0])

    def test_fetch_failure_is_logged_and_does_not_raise_or_call_process(self):
        ctx = make_ctx()
        ctx.mail.fetch_unread.side_effect = RuntimeError("gmail is down")

        with self.assertLogs("gmail-assistant", level=logging.ERROR) as logs:
            poll_once(ctx)  # must not raise

        ctx.process_email.assert_not_called()
        self.assertIn("Failed to fetch unread emails", logs.output[0])

    def test_logs_the_poll_result_even_with_zero_emails(self):
        ctx = make_ctx()
        ctx.mail.fetch_unread.return_value = []

        with self.assertLogs("gmail-assistant", level=logging.INFO) as logs:
            poll_once(ctx)

        self.assertIn("0 unread", logs.output[0])


if __name__ == "__main__":
    unittest.main()
