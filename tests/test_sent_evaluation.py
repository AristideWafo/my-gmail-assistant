import contextlib
import io
import os
import stat
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from src.config import Settings
from src.evaluation.__main__ import main
from src.evaluation.sent import (
    SentSample,
    find_samples,
    format_sent_report,
    judge_samples,
    label_samples,
    load_samples,
    pick_for_labelling,
    sample_messages,
    save_samples,
)
from tests.fakes import FakeMail
from tests.followup_helpers import MINE, message, snapshot

PARIS = ZoneInfo("Europe/Paris")
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
SINCE = datetime(2026, 7, 22, tzinfo=UTC)


def sample(message_id, probability=None, label=None, answered=None, to_label=True):
    return SentSample(message_id, "t", "2026-10-05T09:00:00+00:00", "s", "text", 1,
                      probability, answered, to_label, label)


class SampleMessagesTests(unittest.TestCase):
    def samples(self, *messages, now=NOW):
        return sample_messages(snapshot(*messages), MINE, SINCE, 3, PARIS, now)

    def test_every_mail_i_wrote_to_a_person_is_a_sample_with_its_outcome(self):
        found = self.samples(
            message("m1", day=5),
            message("m2", "jean@example.com", day=7),
            message("m3", day=8),
        )

        self.assertEqual([(s.message_id, s.answered_in_time) for s in found], [("m1", True), ("m3", False)])

    def test_an_answer_after_the_delay_does_not_count_as_in_time(self):
        found = self.samples(message("m1", day=5), message("m2", "jean@example.com", day=12))

        self.assertFalse(found[0].answered_in_time)

    def test_a_recent_mail_has_no_outcome_yet(self):
        found = self.samples(message("m1", day=19))

        self.assertIsNone(found[0].answered_in_time)

    def test_mails_the_tracker_would_never_follow_are_left_out(self):
        found = self.samples(
            message("auto", automated=True),
            message("self", to=("me@example.com",)),
            message("robots", to=("noreply@shop.example",)),
            message("theirs", "jean@example.com"),
            replace(message("old"), sent_at=datetime(2026, 7, 1, tzinfo=UTC)),
        )

        self.assertEqual(found, [])


class CollectTests(unittest.TestCase):
    def test_samples_are_found_judged_and_failures_counted(self):
        mail = FakeMail(
            addresses=MINE,
            threads=[snapshot(message("m1")), snapshot(message("m2"), thread_id="t2")],
            texts={"m1": "Tu confirmes ?", "m2": "Merci"},
        )

        class Judge:
            is_configured = True

            def expects_answer(self, subject, text):
                if text == "Merci":
                    raise RuntimeError("timeout")
                return 0.9

        samples = find_samples(mail, 90, 200, 3, PARIS, NOW)
        failed = judge_samples(samples, mail, Judge())

        self.assertEqual(failed, 1)
        self.assertEqual([(s.text, s.probability) for s in samples], [("Tu confirmes ?", 0.9), ("Merci", None)])


class PickForLabellingTests(unittest.TestCase):
    def test_half_of_the_picks_are_below_one_half_spread_over_the_range(self):
        samples = [sample(f"m{i}", probability=i / 100, to_label=False) for i in range(100)]

        pick_for_labelling(samples, count=10)

        picked = [s.probability for s in samples if s.to_label]
        self.assertEqual(len(picked), 10)
        self.assertEqual(sum(p < 0.5 for p in picked), 5)
        self.assertLess(min(picked), 0.1)
        self.assertGreater(max(picked), 0.9)

    def test_fewer_samples_than_wanted_are_all_picked_and_unjudged_never(self):
        samples = [sample("a", 0.2, to_label=False), sample("b", 0.8, to_label=False),
                   sample("c", None, to_label=False)]

        pick_for_labelling(samples, count=10)

        self.assertEqual([s.to_label for s in samples], [True, True, False])


class LabelTests(unittest.TestCase):
    def test_answers_are_saved_one_by_one_until_quit(self):
        samples = [sample("a", 0.9), sample("b", 0.1), sample("c", 0.5), sample("d", 0.5)]
        answers = iter(["maybe", "y", "n", "s", "q"])
        saves = []

        done = label_samples(samples, lambda prompt: next(answers), lambda text: None,
                             lambda: saves.append(1))

        self.assertEqual(done, 2)
        self.assertEqual([s.label for s in samples], [True, False, None, None])
        self.assertEqual(len(saves), 2)

    def test_mails_already_labelled_or_not_picked_are_not_asked(self):
        samples = [sample("a", 0.9, label=True), sample("b", 0.1, to_label=False)]

        done = label_samples(samples, lambda prompt: self.fail("asked"), lambda text: None, lambda: None)

        self.assertEqual(done, 0)


class ReportTests(unittest.TestCase):
    def test_precision_and_recall_per_threshold(self):
        samples = [
            sample("tp", 0.9, label=True, answered=True),
            sample("fp", 0.7, label=False, answered=False),
            sample("fn", 0.2, label=True, answered=True),
            sample("tn", 0.1, label=False, answered=False),
        ]

        report = format_sent_report(samples)

        labels_row, outcome_row = [line.split() for line in report.splitlines() if line.split()[:1] == ["0.5"]]
        self.assertIn("4 sent mail(s) judged, 4 labelled", report)
        # At 0.5: two said yes, one right; two real yes, one found; two offered.
        self.assertEqual(labels_row, ["0.5", "0.50", "±0.41", "0.50", "±0.41", "2"])
        # Answered in time: one of the two said yes, one of the two said no.
        self.assertEqual(outcome_row[1::2], ["0.50", "0.50"])

    def test_no_labels_reads_as_not_available(self):
        report = format_sent_report([sample("a", 0.9, to_label=False)])

        self.assertIn("n/a", report)


class StorageTests(unittest.TestCase):
    def test_samples_round_trip_in_a_file_only_the_owner_can_read(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "data", "sent.json")
        samples = [sample("a", 0.9, label=True, answered=False)]

        save_samples(path, samples)

        self.assertEqual(load_samples(path), samples)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)


class SentCommandTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "sent.json")
        self.db_path = os.path.join(tmp.name, "assistant.db")

    def run_cli(self, *argv, jev_api_key="k", mail=None):
        out = io.StringIO()
        with (
            contextlib.redirect_stdout(out),
            patch("src.evaluation.__main__.Settings") as settings,
            patch.dict("src.evaluation.__main__.MAIL_PROVIDERS", {"gmail": lambda ctx: mail}),
        ):
            settings.return_value = Settings(_env_file=None, db_path=self.db_path, jev_api_key=jev_api_key)
            code = main([*argv, "--path", self.path])
        return code, out.getvalue()

    def test_collect_needs_a_configured_jev(self):
        code, output = self.run_cli("sent-collect", jev_api_key="")

        self.assertEqual(code, 2)
        self.assertIn("not configured", output)

    def test_collect_refuses_beyond_the_call_budget(self):
        mail = FakeMail(addresses=MINE, threads=[snapshot(message(f"m{i}"), thread_id=f"t{i}") for i in range(3)])

        code, output = self.run_cli("sent-collect", "--max-calls", "2", mail=mail)

        self.assertEqual(code, 2)
        self.assertIn("Refused", output)
        self.assertFalse(os.path.exists(self.path))

    def test_collect_then_label_then_report(self):
        mail = FakeMail(addresses=MINE, threads=[snapshot(message("m1"))], texts={"m1": "Tu confirmes ?"})
        with patch("src.evaluation.__main__.JevSentMailJudge.expects_answer", return_value=0.9):
            code, output = self.run_cli("sent-collect", mail=mail)
        self.assertEqual(code, 0)
        self.assertEqual(load_samples(self.path)[0].probability, 0.9)

        with patch("builtins.input", return_value="y"):
            code, output = self.run_cli("sent-label")
        self.assertIn("1 mail(s) labelled", output)

        code, output = self.run_cli("sent-report")
        self.assertIn("1 labelled", output)


if __name__ == "__main__":
    unittest.main()
