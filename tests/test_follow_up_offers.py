import contextlib
import unittest
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from src.domain import CLOSED, WAITING_FOR_THEM, CallbackEvent, FollowUpAnchor
from src.followup.offers import ANCHOR_CHANGED, FollowUpOffers, format_offer, recheck, recipients
from src.followup.refresh import FollowUpTracker
from src.followup.state import (
    ANSWERED,
    ANSWERED_ELSEWHERE,
    DELETED,
    DISMISSED,
    EXPIRED_OFFER,
    HANDED_OFF,
    OFFERED,
    SENT,
    SNOOZED,
    TOO_MANY_RECIPIENTS,
    derive,
    would_propose,
)
from src.followup.template import follow_up_text, is_english
from src.interactions import follow_up as actions_module
from src.interactions.callbacks import Callback, follow_up_offer_buttons, parse_callback
from src.interactions.follow_up import FollowUpActions
from src.scheduling import ProactiveBudget
from src.storage import SqliteDecisionStore
from tests.fakes import FakeMail
from tests.followup_helpers import ACTIVATED, MINE, message, snapshot

PARIS = ZoneInfo("Europe/Paris")
# Thursday 8 October 2026, 11:00 in Paris: the follow-up of a Monday mail is due.
THURSDAY = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)


@dataclass
class RecordingMail(FakeMail):
    drafts: list = field(default_factory=list)
    sent: list = field(default_factory=list)
    send_error: Exception | None = None

    def create_draft(self, thread_id, to, subject, body, in_reply_to="", cc=(), references=()):
        self.drafts.append({"thread_id": thread_id, "to": to, "subject": subject, "body": body,
                            "in_reply_to": in_reply_to, "cc": tuple(cc), "references": tuple(references)})
        return f"draft-{len(self.drafts)}"

    def send_draft(self, draft_id):
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(draft_id)
        return True


def anchor_of(*messages):
    return derive(snapshot(*messages), MINE, ACTIVATED).anchor


@contextlib.contextmanager
def _nothing():
    yield


class TemplateTests(unittest.TestCase):
    ANCHOR = FollowUpAnchor("m1", datetime(2026, 10, 5, 9, tzinfo=UTC), ("j@x.com",), (), "Re: TR: Devis")

    def test_french_by_default_with_date_subject_and_signature(self):
        text = follow_up_text(self.ANCHOR, "Pouvez-vous m'envoyer le devis ?", "Aristide", PARIS)

        self.assertEqual(text, "Bonjour,\n\nJe me permets de revenir vers vous au sujet de mon message "
                               "du 05/10 concernant « Devis ».\n\nBien cordialement,\nAristide")

    def test_english_when_my_text_is_english(self):
        text = follow_up_text(self.ANCHOR, "Could you send me the contract please?", "", PARIS)

        self.assertEqual(text, 'Hello,\n\nI am following up on my message of October 5 about "Devis".'
                               "\n\nBest regards,")

    def test_without_subject_the_mention_is_left_out(self):
        anchor = replace(self.ANCHOR, subject="")

        self.assertIn("du 05/10.", follow_up_text(anchor, "Tu confirmes ?", "", PARIS))

    def test_language_detection(self):
        cases = {
            ("Hi Tom, could you send the file? Thanks", ""): True,
            ("Any update on this?", ""): True,
            ("See attached.", ""): True,
            ("Salut Tom, tu peux m'envoyer le fichier ? Merci", ""): False,
            ("Voici le devis.", ""): False,
            ("", "Quarterly report for you"): True,
            ("", ""): False,
        }
        for (text, subject), english in cases.items():
            with self.subTest(text=text, subject=subject):
                self.assertEqual(is_english(text, subject), english)

    def test_tu_when_i_wrote_tu(self):
        text = follow_up_text(self.ANCHOR, "Tu peux me renvoyer le devis ?", "", PARIS)

        self.assertIn("revenir vers toi", text)
        self.assertTrue(text.endswith("Bonne journée,"))
        self.assertIn("revenir vers vous", follow_up_text(self.ANCHOR, "Tu sais si vous venez ?", "", PARIS))


class RecheckTests(unittest.TestCase):
    def setUp(self):
        self.store = SqliteDecisionStore(":memory:")
        self.addCleanup(self.store.close)
        self.mail = RecordingMail(addresses=MINE, threads=[snapshot(message("m1"))])
        self.thread = self.track()

    def track(self):
        tracker = FollowUpTracker(self.mail, self.store, PARIS, 3, 50, clock=lambda: THURSDAY)
        self.store.set_state("followup:activated_at", ACTIVATED.isoformat())
        tracker.refresh()
        return self.store.threads.get("t1")

    def test_still_waiting(self):
        self.assertIsNone(recheck(self.mail, self.thread))

    def test_every_reason_not_to_follow_up(self):
        cases = {
            ANSWERED: [snapshot(message("m1"), message("m2", "jean@example.com", day=6))],
            ANCHOR_CHANGED: [snapshot(message("m1"), message("m2", day=6))],
            DELETED: [],
        }
        for reason, threads in cases.items():
            with self.subTest(reason):
                self.mail.threads = threads
                self.assertEqual(recheck(self.mail, self.thread), reason)

    def test_an_answer_in_another_thread_counts(self):
        self.mail.answered_elsewhere = {"jean@example.com"}

        self.assertEqual(recheck(self.mail, self.thread), ANSWERED_ELSEWHERE)

    def test_cc_moves_to_to_when_to_held_only_me(self):
        anchor = anchor_of(message("m1", to=("me@example.com",), cc=("jean@example.com",)))

        self.assertEqual(recipients(anchor), (("jean@example.com",), ()))

    def test_a_group_mail_is_never_followed(self):
        group = message("m1", to=tuple(f"p{i}@example.com" for i in range(6)))

        self.assertEqual(derive(snapshot(group), MINE, ACTIVATED).reason, TOO_MANY_RECIPIENTS)


class OfferTestCase(unittest.TestCase):
    def setUp(self):
        self.now = THURSDAY
        self.store = SqliteDecisionStore(":memory:", clock=lambda: self.now)
        self.addCleanup(self.store.close)
        self.mail = RecordingMail(
            addresses=MINE,
            threads=[snapshot(message("m1", cc=("paul@example.com",)))],
            texts={"m1": "Pouvez-vous m'envoyer le devis ?"},
        )
        self.chat = MagicMock()
        self.chat.send_message.return_value = 77
        self.budget = ProactiveBudget(self.store, PARIS, 6, clock=lambda: self.now)
        self.store.set_state("followup:activated_at", ACTIVATED.isoformat())
        self.tracker = FollowUpTracker(self.mail, self.store, PARIS, 3, 50, clock=lambda: self.now)
        self.tracker.refresh()
        self.store.threads.update("t1", lambda t: replace(t, expects_answer=0.9, jev_asked_for="m1"))
        self.offers = self.make_offers()

    def make_offers(self, daily_max=3):
        return FollowUpOffers(
            self.store, self.mail, self.chat, self.budget, follow_up_offer_buttons, 0.5, PARIS,
            hour=10, daily_max=daily_max, signature="Aristide", clock=lambda: self.now,
        )

    def thread(self):
        return self.store.threads.get("t1")


class FollowUpOffersTests(OfferTestCase):
    def test_a_due_follow_up_is_offered_silently_with_its_exact_text(self):
        self.assertEqual(self.offers.run(), 1)

        text, = self.chat.send_message.call_args.args
        kwargs = self.chat.send_message.call_args.kwargs
        self.assertTrue(kwargs["silent"])
        self.assertEqual(kwargs["buttons"], follow_up_offer_buttons("t1"))
        thread = self.thread()
        self.assertEqual((thread.proposal_state, thread.offered_on, thread.proposals_count), (OFFERED, 77, 1))
        self.assertIn("À : jean@example.com\nCc : paul@example.com", text)
        self.assertTrue(text.endswith(thread.proposal_text))
        self.assertEqual(self.budget.available(), 5)

    def test_an_offer_is_made_once(self):
        self.offers.run()

        self.assertEqual(self.offers.run(), 0)
        self.assertEqual(self.chat.send_message.call_count, 1)

    def test_offers_wait_for_a_weekday_after_the_hour_and_not_friday_evening(self):
        cases = {
            "before the hour": datetime(2026, 10, 8, 7, 0, tzinfo=UTC),
            "friday evening": datetime(2026, 10, 9, 16, 0, tzinfo=UTC),
            "saturday": datetime(2026, 10, 10, 9, 0, tzinfo=UTC),
        }
        for name, now in cases.items():
            with self.subTest(name):
                self.now = now
                self.assertEqual(self.offers.run(), 0)
        self.chat.send_message.assert_not_called()

    def test_the_daily_maximum_and_the_budget_both_hold(self):
        self.assertEqual(self.make_offers(daily_max=1).run(), 1)
        self.store.threads.update("t1", lambda t: replace(t, proposal_state="none", proposals_count=0))
        self.assertEqual(self.make_offers(daily_max=1).run(), 0)

        self.budget.spend(units=6)
        self.assertEqual(self.make_offers(daily_max=5).run(), 0)

    def test_an_answer_found_at_offer_time_closes_the_thread_instead(self):
        self.mail.answered_elsewhere = {"paul@example.com"}

        self.assertEqual(self.offers.run(), 0)

        self.assertEqual((self.thread().state, self.thread().reason), (CLOSED, ANSWERED_ELSEWHERE))
        self.chat.send_message.assert_not_called()

    def test_a_chat_failure_changes_nothing_and_uses_no_budget(self):
        self.chat.send_message.side_effect = RuntimeError("telegram down")

        with self.assertLogs("src.followup.offers", level="WARNING"):
            self.assertEqual(self.offers.run(), 0)

        self.assertEqual((self.thread().proposal_state, self.thread().proposals_count), ("none", 0))
        self.assertEqual(self.budget.available(), 6)

    def test_an_offer_made_on_friday_is_still_valid_on_monday(self):
        self.now = datetime(2026, 10, 9, 9, 0, tzinfo=UTC)
        self.offers.run()
        self.now = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)

        self.offers.run()

        self.assertEqual(self.thread().proposal_state, OFFERED)

    def test_an_offer_whose_state_cannot_be_recorded_is_withdrawn(self):
        cases = {
            "store error": MagicMock(side_effect=RuntimeError("database is locked")),
            "anchor changed meanwhile": MagicMock(return_value=None),
        }
        for name, update in cases.items():
            with self.subTest(name):
                self.chat.reset_mock()
                real = self.store.threads.update
                self.store.threads.update = update
                try:
                    with self.assertLogs("src.followup.offers", level="WARNING") if name == "store error" else _nothing():
                        self.assertEqual(self.offers.run(), 0)
                finally:
                    self.store.threads.update = real
                self.chat.clear_buttons.assert_called_once_with(77)
                self.assertEqual(self.budget.available(), 6)

    def test_an_offer_left_unanswered_expires_after_two_weekdays_and_is_not_made_again(self):
        self.offers.run()
        self.now += timedelta(days=5)

        self.offers.run()

        self.assertEqual(self.thread().proposal_state, EXPIRED_OFFER)
        self.assertEqual(self.chat.send_message.call_count, 1)


class FollowUpActionsTests(OfferTestCase):
    def setUp(self):
        super().setUp()
        self.offers.run()
        self.answers = []
        self.cleared = []
        self.actions = self.make_actions()

    def make_actions(self, enabled=True):
        return FollowUpActions(
            self.store, self.mail, lambda event, text: self.answers.append(text),
            self.cleared.append, enabled=enabled, clock=lambda: self.now,
        )

    def press(self, code, message_id=77, actions=None):
        data = f"fa:{code}:t1"
        (actions or self.actions).handle(CallbackEvent("cb", message_id, data), parse_callback(data))
        return self.answers[-1]

    def test_buttons_parse_back_to_their_action(self):
        rows = follow_up_offer_buttons("t1")

        self.assertEqual([parse_callback(data) for row in rows for _, data in row],
                         [Callback("fa", "t1", action) for action in ("send", "snooze", "dismiss", "edit")])
        self.assertIsNone(parse_callback("fa:x:t1"))

    def test_send_creates_the_draft_from_the_store_and_sends_it_once(self):
        answer = self.press("s")

        self.assertEqual(answer, actions_module.SENT_ACK)
        draft, = self.mail.drafts
        self.assertEqual(draft["to"], "jean@example.com")
        self.assertEqual(draft["cc"], ("paul@example.com",))
        self.assertEqual(draft["body"], self.thread().proposal_text)
        self.assertEqual((draft["in_reply_to"], draft["references"]), ("<m1@x>", ("<m1@x>",)))
        self.assertEqual(self.mail.sent, ["draft-1"])
        self.assertEqual(self.thread().proposal_state, SENT)
        self.assertEqual(self.cleared, [77])

    def test_a_second_press_sends_nothing(self):
        self.press("s")

        self.assertEqual(self.press("s"), actions_module.UNKNOWN_OFFER)
        self.assertEqual(len(self.mail.sent), 1)

    def test_a_redelivered_press_after_the_claim_sends_nothing_and_drafts_nothing(self):
        self.store.set_state("followup_sent:t1:1", "draft-0")

        self.assertEqual(self.press("s"), actions_module.SEND_FAILED)
        self.assertEqual((self.mail.drafts, self.mail.sent), ([], []))
        self.assertEqual(self.thread().proposal_state, SENT)

    def test_only_the_message_that_made_the_offer_is_honoured(self):
        self.assertEqual(self.press("s", message_id=78), actions_module.UNKNOWN_OFFER)
        self.assertEqual(self.mail.drafts, [])

    def test_turning_follow_ups_off_disables_the_buttons_already_shown(self):
        answer = self.press("s", actions=self.make_actions(enabled=False))

        self.assertEqual(answer, actions_module.DISABLED)
        self.assertEqual(self.mail.drafts, [])
        self.assertEqual(self.thread().proposal_state, OFFERED)

    def test_an_offer_older_than_two_weekdays_is_refused(self):
        # Offered on Thursday: still valid on Monday, expired on Tuesday.
        self.now += timedelta(days=5)

        self.assertEqual(self.press("s"), actions_module.EXPIRED)
        self.assertEqual(self.mail.drafts, [])
        self.assertEqual(self.thread().proposal_state, EXPIRED_OFFER)

    def test_an_answer_that_came_meanwhile_cancels_the_follow_up(self):
        self.mail.threads = [snapshot(message("m1", cc=("paul@example.com",)),
                                      message("m2", "paul@example.com", day=8))]

        self.assertEqual(self.press("s"), actions_module.ANSWERED_MEANWHILE)
        self.assertEqual(self.mail.drafts, [])
        self.assertEqual((self.thread().state, self.thread().reason), (CLOSED, ANSWERED))

    def test_a_new_mail_of_mine_in_the_thread_cancels_the_follow_up(self):
        self.mail.threads = [snapshot(message("m1", cc=("paul@example.com",)), message("m2", day=8))]

        self.assertEqual(self.press("s"), actions_module.THREAD_CHANGED)
        self.assertEqual(self.mail.drafts, [])

    def test_gmail_unable_to_tell_means_no(self):
        self.mail.thread_snapshot = MagicMock(side_effect=RuntimeError("500"))

        with self.assertLogs("src.interactions.follow_up", level="WARNING"):
            self.assertEqual(self.press("s"), actions_module.CANNOT_VERIFY)
        self.assertEqual(self.mail.drafts, [])
        self.assertEqual(self.thread().proposal_state, OFFERED)

    def test_a_draft_that_cannot_be_created_claims_nothing(self):
        self.mail.create_draft = MagicMock(return_value=None)

        self.assertEqual(self.press("s"), actions_module.DRAFT_FAILED)
        self.assertIsNone(self.store.get_state("followup_sent:t1:1"))
        self.assertEqual(self.thread().proposal_state, OFFERED)

    def test_a_failed_send_after_the_draft_says_uncertain_and_never_retries(self):
        self.mail.send_error = RuntimeError("timeout")

        with self.assertLogs("src.interactions.follow_up", level="ERROR"):
            self.assertEqual(self.press("s"), actions_module.SEND_FAILED)
        self.assertEqual(len(self.mail.drafts), 1)
        self.assertEqual(self.thread().proposal_state, SENT)

    def test_an_unconfigured_mailbox_is_an_uncertain_send(self):
        self.mail.send_draft = MagicMock(return_value=False)

        with self.assertLogs("src.interactions.follow_up", level="ERROR"):
            self.assertEqual(self.press("s"), actions_module.SEND_FAILED)

    def test_a_draft_error_is_logged_and_claims_nothing(self):
        self.mail.create_draft = MagicMock(side_effect=RuntimeError("quota"))

        with self.assertLogs("src.interactions.follow_up", level="ERROR"):
            self.assertEqual(self.press("e"), actions_module.DRAFT_FAILED)
        self.assertEqual(self.thread().proposal_state, OFFERED)

    def test_edit_is_also_disabled_when_follow_ups_are_off(self):
        self.assertEqual(self.press("e", actions=self.make_actions(enabled=False)), actions_module.DISABLED)
        self.assertEqual(self.mail.drafts, [])

    def test_a_state_change_on_a_new_anchor_is_not_applied_to_it(self):
        self.store.threads.update(
            "t1", lambda t: replace(t, anchor=replace(t.anchor, message_id="m9"))
        )
        stale = replace(self.thread(), anchor=replace(self.thread().anchor, message_id="m1"))

        self.actions._finish(CallbackEvent("cb", 77, "x"), stale, SNOOZED, "ok", "snoozed")

        self.assertEqual(self.thread().anchor.message_id, "m9")
        self.assertNotEqual(self.thread().proposal_state, SNOOZED)

    def test_snooze_offers_again_three_days_later(self):
        self.assertEqual(self.press("z"), actions_module.SNOOZED_ACK)
        self.assertEqual(self.thread().proposal_state, SNOOZED)
        self.assertFalse(would_propose(self.thread(), 0.5, self.now))

        self.now += timedelta(days=3)
        self.assertTrue(would_propose(self.thread(), 0.5, self.now))

    def test_dismiss_closes_the_thread_for_good(self):
        self.assertEqual(self.press("d"), actions_module.DISMISSED_ACK)
        self.assertEqual((self.thread().state, self.thread().reason), (CLOSED, DISMISSED))

        self.mail.threads = [snapshot(message("m1", cc=("paul@example.com",)), history_id="9")]
        self.tracker.refresh()
        self.assertEqual(self.thread().state, CLOSED)

    def test_edit_hands_a_draft_over_without_sending(self):
        self.assertEqual(self.press("e"), actions_module.HANDED_OFF_ACK)
        self.assertEqual(len(self.mail.drafts), 1)
        self.assertEqual(self.mail.sent, [])
        self.assertEqual(self.thread().proposal_state, HANDED_OFF)

    def test_my_follow_up_becomes_the_anchor_and_is_never_followed_up(self):
        self.press("s")
        self.mail.threads = [snapshot(message("m1", cc=("paul@example.com",)),
                                      message("f1", cc=("paul@example.com",), day=8), history_id="9")]
        self.tracker.refresh()
        self.store.threads.update("t1", lambda t: replace(t, expects_answer=0.9, jev_asked_for="f1"))
        self.now += timedelta(days=10)

        self.assertEqual(self.thread().anchor.message_id, "f1")
        self.assertEqual(self.thread().state, WAITING_FOR_THEM)
        self.assertFalse(would_propose(self.thread(), 0.5, self.now))


class FormatOfferTests(OfferTestCase):
    def test_the_offer_shows_recipients_subject_and_text(self):
        text = format_offer(self.thread(), "Bonjour,", PARIS)

        self.assertEqual(text.splitlines()[:4], ["🔁 Relance proposée", "À : jean@example.com",
                                                 "Cc : paul@example.com", "Objet : Devis"])
        self.assertTrue(text.endswith("Texte envoyé tel quel :\nBonjour,"))


if __name__ == "__main__":
    unittest.main()
