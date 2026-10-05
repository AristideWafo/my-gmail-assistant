import unittest

from src.domain import DecisionRecord, RatedDecision
from src.evaluation.routing_audit import RULES, audit, format_audit

BY_NAME = {rule.name: rule for rule in RULES}


def record(message_id, urgency, category, route, confidence=0.9, source="jev") -> DecisionRecord:
    return DecisionRecord(
        message_id=message_id,
        thread_id="t",
        sender="news@shop.example",
        subject=f"subject {message_id}",
        excerpt="e",
        urgency=urgency,
        category=category,
        confidence=confidence,
        route=route,
        created_at="2026-10-01",
        source=source,
    )


def rated(decision: DecisionRecord, verdict: str) -> RatedDecision:
    return RatedDecision(record=decision, verdict=verdict, rated_at="2026-10-02")


class RuleMatchingTests(unittest.TestCase):
    def test_each_rule_needs_its_category_its_urgency_and_confidence(self):
        cases = {
            "archive-low-notification": ("low", "notification_systeme"),
            "archive-low-alerte-technique": ("low", "alerte_technique"),
            "archive-urgent-spam": ("high", "spam"),
        }
        for name, (urgency, category) in cases.items():
            with self.subTest(name):
                rule = BY_NAME[name]
                self.assertTrue(rule.matches(record("m", urgency, category, "label"), 0.5))
                self.assertFalse(rule.matches(record("m", "medium", category, "label"), 0.5))
                self.assertFalse(rule.matches(record("m", urgency, "personnel", "label"), 0.5))
                self.assertFalse(rule.matches(record("m", urgency, category, "label", 0.4), 0.5))

    def test_only_the_notification_rule_is_in_production(self):
        self.assertEqual([rule.name for rule in RULES if rule.existing], ["archive-low-notification"])
        self.assertTrue(all(rule.route == "reject" for rule in RULES))


class AuditTests(unittest.TestCase):
    def audit_one(self, name, records, verdicts=()):
        return audit([BY_NAME[name]], records, verdicts, 0.5)[0]

    def test_existing_rule_is_confronted_with_the_verdicts_on_what_it_archived(self):
        fine = record("fine", "low", "notification_systeme", "reject")
        wanted = record("wanted", "low", "notification_systeme", "reject")
        forward = record("forward", "low", "notification_systeme", "reject")
        unrated = record("unrated", "low", "notification_systeme", "reject")
        verdicts = [
            rated(fine, "valid"),
            rated(wanted, "wrong_archive"),
            rated(forward, "missed_important"),
        ]

        result = self.audit_one(
            "archive-low-notification", [fine, wanted, forward, unrated], verdicts
        )

        self.assertEqual((result.matched, result.moved, result.agree), (4, 0, 1))
        self.assertEqual(
            result.disagree,
            [
                "wanted shop.example «subject wanted» [wrong_archive, was reject]",
                "forward shop.example «subject forward» [missed_important, was reject]",
            ],
        )

    def test_candidate_counts_what_it_would_move_and_who_approved_the_current_route(self):
        kept = record("kept", "low", "alerte_technique", "label")
        spam = record("spam", "low", "alerte_technique", "label")
        other = record("other", "medium", "alerte_technique", "label")

        result = self.audit_one(
            "archive-low-alerte-technique",
            [kept, spam, other],
            [rated(kept, "valid"), rated(spam, "false_spam"), rated(other, "valid")],
        )

        self.assertEqual((result.matched, result.moved, result.agree), (2, 2, 1))
        self.assertEqual(
            result.disagree, ["kept shop.example «subject kept» [valid, was label]"]
        )

    def test_decisions_made_by_a_rule_or_the_vip_list_are_left_out(self):
        records = [
            record("ruled", "low", "alerte_technique", "label", source="rule"),
            record("vip", "low", "alerte_technique", "label", source="vip"),
            record("fallback", "low", "alerte_technique", "label", source="heuristic"),
        ]

        self.assertEqual(self.audit_one("archive-low-alerte-technique", records).matched, 1)

    def test_a_mail_already_on_the_rule_route_is_matched_but_not_moved(self):
        result = self.audit_one(
            "archive-urgent-spam",
            [record("a", "high", "spam", "label"), record("b", "high", "spam", "reject")],
        )

        self.assertEqual((result.matched, result.moved), (2, 1))


class FormatAuditTests(unittest.TestCase):
    def test_report_tells_existing_from_candidate_and_bounds_the_list(self):
        records = [record(f"m{i:02d}", "low", "alerte_technique", "label") for i in range(12)]
        verdicts = [rated(item, "valid") for item in records]
        audits = audit(RULES, records, verdicts, 0.5)

        lines = format_audit(audits, 12, 90).splitlines()

        self.assertEqual(lines[0], "Classifier decisions over 90 days: 12")
        self.assertTrue(lines[2].startswith("[existing] archive-low-notification"))
        self.assertIn("  applies to 0 mail(s); 0 rated: 0 verdict(s) agree, 0 disagree", lines)
        self.assertIn(
            "  would move 12 of 12 matching mail(s); 12 rated: 0 verdict(s) agree, 12 disagree",
            lines,
        )
        self.assertEqual(sum(1 for line in lines if line.startswith("    disagrees:")), 10)
        self.assertIn("    and 2 more", lines)


if __name__ == "__main__":
    unittest.main()
