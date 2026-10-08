import unittest
from dataclasses import replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from src.domain import CLOSED, IGNORED, WAITING_FOR_THEM, ThreadState
from src.followup.state import (
    ANSWERED,
    AUTOMATED_RECIPIENT,
    BEFORE_ACTIVATION,
    BOUNCED,
    DISMISSED,
    NO_RECIPIENT,
    NOT_MINE,
    add_business_days,
    derive,
    track,
)
from tests.followup_helpers import ACTIVATED, MINE, message, snapshot

PARIS = ZoneInfo("Europe/Paris")
NOW = datetime(2026, 10, 8, tzinfo=UTC)


def state_of(*messages):
    return derive(snapshot(*messages), MINE, ACTIVATED)


class DeriveTests(unittest.TestCase):
    def test_my_last_message_without_answer_waits_for_them(self):
        derived = state_of(message("m1", "jean@example.com", to=("me@example.com",), day=2),
                           message("m2", cc=("paul@example.com", "alias@example.com"), day=3))

        self.assertEqual(derived.state, WAITING_FOR_THEM)
        anchor = derived.anchor
        self.assertEqual(anchor.message_id, "m2")
        self.assertEqual((anchor.to, anchor.cc), (("jean@example.com",), ("paul@example.com",)))
        self.assertEqual(anchor.references, ("<m1@x>", "<m2@x>"))

    def test_any_person_writing_after_my_message_answers_it(self):
        cases = {
            "the recipient": message("m2", "jean@example.com", to=("me@example.com",)),
            "a third party in copy": message("m2", "paul@example.com", to=("jean@example.com",)),
        }
        for name, reply in cases.items():
            with self.subTest(name):
                derived = state_of(message("m1", day=3), replace(reply, sent_at=NOW))
                self.assertEqual((derived.state, derived.reason), (CLOSED, ANSWERED))

    def test_an_auto_reply_is_not_an_answer(self):
        out_of_office = message("m2", "jean@example.com", automated=True, day=6)

        self.assertEqual(state_of(message("m1"), out_of_office).state, WAITING_FOR_THEM)

    def test_a_bounce_closes_the_thread(self):
        bounce = message("m2", "mailer-daemon@googlemail.com", automated=True, bounce=True, day=6)

        self.assertEqual(state_of(message("m1"), bounce).reason, BOUNCED)

    def test_my_later_message_becomes_the_anchor(self):
        derived = state_of(
            message("m1", day=2), message("m2", "jean@example.com", day=3), message("m3", day=4)
        )

        self.assertEqual((derived.state, derived.anchor.message_id), (WAITING_FOR_THEM, "m3"))

    def test_my_other_spellings_are_not_counted_as_recipients(self):
        derived = derive(
            snapshot(message("m1", to=("Me+notes@example.com", "jean@example.com"))),
            frozenset({"me@example.com"}),
            ACTIVATED,
        )

        self.assertEqual(derived.anchor.to, ("jean@example.com",))

    def test_a_message_from_an_alias_is_mine(self):
        derived = state_of(message("m1", "alias@example.com"))

        self.assertEqual(derived.state, WAITING_FOR_THEM)

    def test_order_in_the_thread_wins_over_an_unreadable_date(self):
        undated = replace(message("m2", "jean@example.com"), sent_at=datetime(1970, 1, 1, tzinfo=UTC))

        self.assertEqual(state_of(message("m1"), undated).reason, ANSWERED)

    def test_threads_that_are_never_followed(self):
        cases = {
            NOT_MINE: (message("m1", "jean@example.com"),),
            "my auto-reply": (message("m1", automated=True),),
            BEFORE_ACTIVATION: (replace(message("m1"), sent_at=datetime(2026, 9, 30, tzinfo=UTC)),),
            NO_RECIPIENT: (message("m1", to=("me@example.com",)),),
            AUTOMATED_RECIPIENT: (message("m1", to=("noreply@shop.example",)),),
        }
        expected = {"my auto-reply": NOT_MINE}
        for name, messages in cases.items():
            with self.subTest(name):
                derived = state_of(*messages)
                self.assertEqual((derived.state, derived.reason), (IGNORED, expected.get(name, name)))

    def test_one_person_among_automated_recipients_is_enough(self):
        derived = state_of(message("m1", to=("noreply@shop.example", "jean@example.com")))

        self.assertEqual(derived.state, WAITING_FOR_THEM)


class BusinessDayTests(unittest.TestCase):
    def test_weekends_are_skipped(self):
        cases = {
            # Monday 10:00 -> Thursday 10:00.
            (5, 3): datetime(2026, 10, 8, 10, 0, tzinfo=PARIS),
            # Thursday -> Tuesday.
            (8, 3): datetime(2026, 10, 13, 10, 0, tzinfo=PARIS),
            # Saturday counts from Monday -> Thursday.
            (10, 3): datetime(2026, 10, 15, 10, 0, tzinfo=PARIS),
        }
        for (day, days), expected in cases.items():
            with self.subTest(day=day):
                start = datetime(2026, 10, day, 8, 0, tzinfo=UTC)
                self.assertEqual(add_business_days(start, days, PARIS), expected)

    def test_the_local_weekday_decides_not_the_utc_one(self):
        # Sunday 23:30 UTC is already Monday 01:30 in Paris.
        start = datetime(2026, 10, 11, 23, 30, tzinfo=UTC)

        self.assertEqual(add_business_days(start, 1, PARIS).date().isoformat(), "2026-10-13")


class TrackTests(unittest.TestCase):
    def tracked(self, *messages, existing=None, history="1"):
        snap = snapshot(*messages, history_id=history)
        return track(existing, snap, derive(snap, MINE, ACTIVATED), NOW, NOW)

    def test_a_new_thread_starts_with_no_proposal(self):
        thread = self.tracked(message("m1"))

        self.assertEqual((thread.state, thread.proposal_state, thread.proposals_count),
                         (WAITING_FOR_THEM, "none", 0))
        self.assertEqual((thread.due_at, thread.history_id), (NOW, "1"))

    def test_a_reread_with_the_same_anchor_keeps_what_was_learned(self):
        first = replace(self.tracked(message("m1")), expects_answer=0.9, jev_asked_for="m1",
                        proposal_state="offered", proposal_text="Bonjour")

        again = self.tracked(message("m1"), existing=first, history="2")

        self.assertEqual((again.expects_answer, again.proposal_state, again.proposal_text),
                         (0.9, "offered", "Bonjour"))
        self.assertEqual(again.history_id, "2")

    def test_a_new_anchor_resets_its_answers_but_keeps_the_proposal_count(self):
        first = replace(self.tracked(message("m1")), expects_answer=0.9, jev_asked_for="m1",
                        proposal_state="sent", proposals_count=1)

        followed_up = self.tracked(message("m1"), message("m2", day=8), existing=first)

        self.assertEqual(followed_up.anchor.message_id, "m2")
        self.assertIsNone(followed_up.expects_answer)
        self.assertEqual((followed_up.jev_asked_for, followed_up.proposal_state), ("", "none"))
        self.assertEqual(followed_up.proposals_count, 1)

    def test_a_dismissed_thread_stays_closed_for_the_same_anchor(self):
        dismissed = replace(self.tracked(message("m1")), state=CLOSED, reason=DISMISSED,
                            proposal_state=DISMISSED)

        again = self.tracked(message("m1"), existing=dismissed)

        self.assertEqual((again.state, again.reason), (CLOSED, DISMISSED))

    def test_an_answer_closes_a_tracked_thread(self):
        first = self.tracked(message("m1"))

        answered = self.tracked(message("m1"), message("m2", "jean@example.com", day=6), existing=first)

        self.assertEqual((answered.state, answered.reason), (CLOSED, ANSWERED))

    def test_a_derived_state_without_anchor_is_stored_as_such(self):
        thread = track(None, snapshot(message("m1", "jean@example.com")),
                       ThreadState(IGNORED, NOT_MINE), None, NOW)

        self.assertIsNone(thread.anchor)
        self.assertEqual(thread.reason, NOT_MINE)


if __name__ == "__main__":
    unittest.main()
