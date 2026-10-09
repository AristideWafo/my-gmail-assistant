import threading
import unittest
from datetime import UTC, datetime, timedelta

from src.domain import (
    ACTION_CANCELLED,
    ACTION_DONE,
    ACTION_EXECUTING,
    ACTION_FAILED,
    ACTION_PENDING,
)
from src.storage import SqliteDecisionStore

NOW = datetime(2026, 10, 9, 9, 0, tzinfo=UTC)
LIFETIME = timedelta(days=2)
PAYLOAD = {"thread_id": "t1", "to": ["jean@example.com"], "body": "Bonjour, où en est le devis ?"}


class PendingActionsTests(unittest.TestCase):
    def setUp(self):
        self.clock = [NOW]
        self.store = SqliteDecisionStore(":memory:", clock=lambda: self.clock[0])
        self.addCleanup(self.store.close)
        self.actions = self.store.pending_actions

    def offered(self, chat_message_id=50, payload=PAYLOAD):
        action = self.actions.propose("send_reply", payload, LIFETIME)
        self.actions.attach_chat_message(action.id, chat_message_id)
        return action

    def test_a_proposal_is_stored_whole_and_carries_nothing_out(self):
        action = self.actions.propose("send_reply", PAYLOAD, LIFETIME)

        stored = self.actions.get(action.id)
        self.assertEqual(stored, action)
        self.assertEqual(
            (stored.kind, stored.payload, stored.state), ("send_reply", PAYLOAD, ACTION_PENDING)
        )
        self.assertEqual(stored.expires_at, NOW + LIFETIME)
        self.assertIsNone(stored.chat_message_id)

    def test_two_proposals_get_different_ids(self):
        first = self.actions.propose("send_reply", PAYLOAD, LIFETIME)
        second = self.actions.propose("send_reply", PAYLOAD, LIFETIME)

        self.assertNotEqual(first.id, second.id)

    def test_an_unknown_action_is_not_found(self):
        self.assertIsNone(self.actions.get("nope"))
        self.assertIsNone(self.actions.begin("nope", 50))

    def test_begin_hands_over_the_stored_payload_once(self):
        action = self.offered()

        begun = self.actions.begin(action.id, 50)

        self.assertEqual((begun.payload, begun.state), (PAYLOAD, ACTION_EXECUTING))
        self.assertIsNone(self.actions.begin(action.id, 50))

    def test_begin_is_refused_from_another_chat_message(self):
        action = self.offered(chat_message_id=50)

        self.assertIsNone(self.actions.begin(action.id, 51))
        self.assertEqual(self.actions.get(action.id).state, ACTION_PENDING)

    def test_begin_is_refused_before_the_proposal_was_shown(self):
        action = self.actions.propose("send_reply", PAYLOAD, LIFETIME)

        self.assertIsNone(self.actions.begin(action.id, 50))

    def test_begin_is_refused_once_expired(self):
        action = self.offered()
        self.clock[0] = NOW + LIFETIME

        self.assertIsNone(self.actions.begin(action.id, 50))
        self.assertEqual(self.actions.get(action.id).state, ACTION_PENDING)

    def test_begin_is_refused_when_the_payload_changed_since_it_was_shown(self):
        action = self.offered()
        with self.store._conn:
            self.store._conn.execute(
                "UPDATE pending_actions SET payload = ? WHERE id = ?",
                ('{"to": ["evil@example.com"]}', action.id),
            )

        self.assertIsNone(self.actions.begin(action.id, 50))
        self.assertEqual(self.actions.get(action.id).state, ACTION_FAILED)

    def test_begin_is_refused_when_the_kind_changed_since_it_was_shown(self):
        action = self.offered()
        with self.store._conn:
            self.store._conn.execute(
                "UPDATE pending_actions SET kind = 'delete_thread' WHERE id = ?", (action.id,)
            )

        self.assertIsNone(self.actions.begin(action.id, 50))
        self.assertEqual(self.actions.get(action.id).state, ACTION_FAILED)

    def test_an_action_is_bound_to_a_single_chat_message(self):
        action = self.actions.propose("send_reply", PAYLOAD, LIFETIME)

        self.assertTrue(self.actions.attach_chat_message(action.id, 50))
        self.assertFalse(self.actions.attach_chat_message(action.id, 99))

        self.assertIsNone(self.actions.begin(action.id, 99))
        self.assertIsNotNone(self.actions.begin(action.id, 50))

    def test_an_action_already_begun_cannot_be_bound(self):
        action = self.offered()
        self.actions.begin(action.id, 50)

        self.assertFalse(self.actions.attach_chat_message(action.id, 99))
        self.assertEqual(self.actions.get(action.id).chat_message_id, 50)

    def test_a_clock_without_a_time_zone_still_round_trips(self):
        self.clock[0] = datetime(2026, 10, 9, 9, 0)  # noqa: DTZ001 - the case under test

        action = self.actions.propose("send_reply", PAYLOAD, LIFETIME)

        self.assertEqual(self.actions.get(action.id), action)

    def test_concurrent_presses_begin_a_single_time(self):
        action = self.offered()
        start = threading.Barrier(8)
        begun = []

        def press():
            start.wait()
            begun.append(self.actions.begin(action.id, 50))

        threads = [threading.Thread(target=press) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sum(result is not None for result in begun), 1)

    def test_finish_records_the_outcome_of_what_was_begun(self):
        sent, lost = self.offered(50), self.offered(51)
        self.actions.begin(sent.id, 50)
        self.actions.begin(lost.id, 51)

        self.actions.finish(sent.id, succeeded=True)
        self.actions.finish(lost.id, succeeded=False)

        self.assertEqual(self.actions.get(sent.id).state, ACTION_DONE)
        self.assertEqual(self.actions.get(lost.id).state, ACTION_FAILED)

    def test_finish_does_not_touch_an_action_never_begun(self):
        action = self.offered()

        self.actions.finish(action.id, succeeded=True)

        self.assertEqual(self.actions.get(action.id).state, ACTION_PENDING)

    def test_a_cancelled_action_can_no_longer_begin(self):
        action = self.offered()

        self.assertTrue(self.actions.cancel(action.id, 50))

        self.assertEqual(self.actions.get(action.id).state, ACTION_CANCELLED)
        self.assertIsNone(self.actions.begin(action.id, 50))
        self.assertFalse(self.actions.cancel(action.id, 50))

    def test_cancel_is_refused_from_another_chat_message(self):
        action = self.offered(chat_message_id=50)

        self.assertFalse(self.actions.cancel(action.id, 51))

    def test_an_expired_action_can_still_be_cancelled(self):
        action = self.offered()
        self.clock[0] = NOW + LIFETIME

        self.assertTrue(self.actions.cancel(action.id, 50))

    def test_prune_forgets_actions_expired_for_long_with_the_rest_of_the_store(self):
        old = self.offered()
        self.clock[0] = NOW + LIFETIME + timedelta(days=91)
        recent = self.offered(51)

        self.store.prune(timedelta(days=90))

        self.assertIsNone(self.actions.get(old.id))
        self.assertIsNotNone(self.actions.get(recent.id))

    def test_prune_keeps_an_action_that_has_not_expired_whatever_its_age(self):
        lasting = self.actions.propose("send_reply", PAYLOAD, timedelta(days=365))
        self.clock[0] = NOW + timedelta(days=200)

        self.store.prune(timedelta(days=90))

        self.assertIsNotNone(self.actions.get(lasting.id))

    def test_text_outside_ascii_survives_a_round_trip(self):
        action = self.offered(payload={"body": "Reçu, à jeudi — Aristide"})

        self.assertEqual(
            self.actions.begin(action.id, 50).payload["body"], "Reçu, à jeudi — Aristide"
        )


if __name__ == "__main__":
    unittest.main()
