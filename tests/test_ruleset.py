import os
import tempfile
import unittest
from unittest.mock import MagicMock

from prometheus_client import REGISTRY

from main import ApplicationContext
from src.config import Settings
from src.domain import EmailMessage
from src.errors import ConfigurationError
from src.gmail.client import GmailClient, dmarc_passed
from src.observability.metrics import Metrics
from src.triage.rules import DEFAULT_RULES, RuleSet, _rule, apply_rules, load_ruleset
from src.workflow import EmailWorkflow
from tests.fakes import fake_components

GMAIL_PASS = {"name": "Authentication-Results", "value": "mx.google.com; dkim=pass; dmarc=pass (p=REJECT)"}
GMAIL_FAIL = {"name": "Authentication-Results", "value": "mx.google.com; spf=fail; dmarc=fail (p=NONE)"}


def email(sender="boss@corp.com", subject="Hello", dmarc_pass=True) -> EmailMessage:
    return EmailMessage(
        id="1", thread_id="t", sender=sender, subject=subject, snippet="s", body="b",
        dmarc_pass=dmarc_pass,
    )


class SubjectRuleTests(unittest.TestCase):
    def setUp(self):
        self.rules = (
            _rule(r"^notifications@github\.com$", "low", "alerte_technique", r"^\[org/sandbox\] Run failed"),
        )

    def test_a_subject_pattern_narrows_the_sender_match(self):
        sender = "notifications@github.com"

        matched = apply_rules(sender, self.rules, "[org/sandbox] Run failed: tests")

        self.assertEqual((matched.urgency, matched.category, matched.source), ("low", "alerte_technique", "rule"))
        self.assertIsNone(apply_rules(sender, self.rules, "[org/prod] Run failed: deploy"))
        self.assertIsNone(apply_rules(sender, self.rules))

    def test_rules_are_checked_in_order(self):
        rules = (_rule("@a\\.com$", "high", "personnel"), _rule("@a\\.com$", "low", "spam"))

        self.assertEqual(apply_rules("x@a.com", rules).urgency, "high")


class VipTests(unittest.TestCase):
    def setUp(self):
        self.ruleset = RuleSet(vip=frozenset({"boss@corp.com"}))

    def test_authenticated_vip_is_urgent_whatever_the_case_of_the_address(self):
        result = self.ruleset.classify(email(sender="Boss@Corp.com"))

        self.assertEqual(
            (result.urgency, result.category, result.confidence, result.source),
            ("high", "personnel", 1.0, "vip"),
        )

    def test_a_vip_address_without_dmarc_pass_is_not_trusted(self):
        self.assertIsNone(self.ruleset.classify(email(dmarc_pass=False)))

    def test_lookalike_addresses_are_not_vip(self):
        for sender in ("boss@corp.com.evil.io", "notboss@corp.com", "boss@corp.co"):
            with self.subTest(sender=sender):
                self.assertIsNone(self.ruleset.classify(email(sender=sender)))

    def test_vip_wins_over_a_rule_and_rules_still_apply_to_others(self):
        ruleset = RuleSet(
            rules=(_rule("@corp\\.com$", "low", "newsletter"),), vip=frozenset({"boss@corp.com"})
        )

        self.assertEqual(ruleset.classify(email()).source, "vip")
        self.assertEqual(ruleset.classify(email(sender="hr@corp.com")).source, "rule")

    def test_default_ruleset_has_the_built_in_rules_and_no_vip(self):
        self.assertEqual((RuleSet().rules, RuleSet().vip), (DEFAULT_RULES, frozenset()))


class LoadRulesetTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = os.path.join(directory.name, "rules.toml")

    def write(self, content: str) -> str:
        with open(self.path, "w", encoding="utf-8") as rules:
            rules.write(content)
        return self.path

    def test_empty_path_keeps_the_built_in_rules(self):
        self.assertEqual(load_ruleset(""), RuleSet())

    def test_file_rules_come_before_the_built_in_ones_and_vip_is_lowercased(self):
        path = self.write(
            'vip = ["Boss@Corp.com"]\n'
            "[[rules]]\n"
            "sender = '@substack\\.com$'\n"
            "subject = 'urgent'\n"
            'urgency = "medium"\n'
            'category = "personnel"\n'
        )

        ruleset = load_ruleset(path)

        self.assertEqual(ruleset.vip, frozenset({"boss@corp.com"}))
        self.assertEqual(ruleset.rules[1:], DEFAULT_RULES)
        self.assertEqual(ruleset.classify(email("a@substack.com", "URGENT: read")).category, "personnel")
        self.assertEqual(ruleset.classify(email("a@substack.com", "weekly")).category, "newsletter")

    def test_the_example_file_is_valid(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        ruleset = load_ruleset(os.path.join(root, "config", "triage_rules.example.toml"))

        self.assertEqual(len(ruleset.rules), 2 + len(DEFAULT_RULES))

    def test_invalid_files_stop_startup_with_a_precise_message(self):
        cases = {
            "not toml": ("vip = [", "not valid TOML"),
            "unknown key": ('vips = ["a@b.com"]', "unknown key"),
            "rule key typo": ('[[rules]]\nsendr = "x"\nurgency = "low"\ncategory = "spam"', "unknown key"),
            "missing sender": ('[[rules]]\nurgency = "low"\ncategory = "spam"', "needs a non-empty 'sender'"),
            "bad urgency": ('[[rules]]\nsender = "x"\nurgency = "critical"\ncategory = "spam"', "urgency 'critical'"),
            "bad category": ('[[rules]]\nsender = "x"\nurgency = "low"\ncategory = "junk"', "category 'junk'"),
            "bad regex": ('[[rules]]\nsender = "("\nurgency = "low"\ncategory = "spam"', "rule 1 has an invalid pattern"),
            "empty subject": ('[[rules]]\nsender = "x"\nsubject = ""\nurgency = "low"\ncategory = "spam"', "subject"),
            "vip pattern": ('vip = ["@corp.com", 3]', "is not an email address"),
            "vip not list": ('vip = "a@b.com"', "must be a list"),
            "rules not tables": ("rules = [1]", "must be a table"),
        }
        for name, (content, message) in cases.items():
            with self.subTest(name=name), self.assertRaisesRegex(ConfigurationError, message):
                load_ruleset(self.write(content))

    def test_missing_file_stops_startup(self):
        with self.assertRaisesRegex(ConfigurationError, "cannot be read"):
            load_ruleset("/nonexistent/rules.toml")


class DmarcTests(unittest.TestCase):
    def test_reads_gmails_own_verdict(self):
        self.assertTrue(dmarc_passed([GMAIL_PASS]))
        self.assertFalse(dmarc_passed([GMAIL_FAIL]))
        self.assertFalse(dmarc_passed([]))

    def test_a_header_shipped_with_the_message_cannot_override_gmails(self):
        forged_same_id = {"name": "Authentication-Results", "value": "mx.google.com; dmarc=pass"}
        forged_other_id = {"name": "authentication-results", "value": "mail.evil.io; dmarc=pass"}

        self.assertFalse(dmarc_passed([GMAIL_FAIL, forged_same_id]))
        self.assertFalse(dmarc_passed([forged_other_id]))
        self.assertTrue(dmarc_passed([forged_other_id, GMAIL_PASS]))

    def test_a_pass_must_be_dmarc_not_only_spf_or_dkim(self):
        header = {"name": "Authentication-Results", "value": "mx.google.com; spf=pass; dkim=pass; dmarc=none"}

        self.assertFalse(dmarc_passed([header]))

    def test_parsed_message_carries_the_verdict(self):
        headers = [{"name": "From", "value": "Boss <boss@corp.com>"}, GMAIL_PASS]

        message = GmailClient._parse_message({"id": "1", "payload": {"headers": headers}})

        self.assertTrue(message.dmarc_pass)
        self.assertFalse(GmailClient._parse_message({"id": "1", "payload": {}}).dmarc_pass)


class WiringTests(unittest.TestCase):
    def test_vip_mail_skips_the_classifier_and_alerts(self):
        classifier, analyzer = MagicMock(), MagicMock()
        workflow = EmailWorkflow(classifier, analyzer, rules=RuleSet(vip=frozenset({"boss@corp.com"})))

        result = workflow.run(email())

        classifier.classify.assert_not_called()
        self.assertEqual((result["route"], result["triage"].source), ("llm", "vip"))

    def test_rules_file_from_the_settings_reaches_the_workflow(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", encoding="utf-8") as rules:
            rules.write('vip = ["boss@corp.com"]\n')
            rules.flush()
            settings = Settings(_env_file=None, db_path=":memory:", triage_rules_path=rules.name)
            ctx = ApplicationContext(settings, fake_components())
            self.addCleanup(ctx.close)

            self.assertEqual(ctx.workflow.rules.vip, frozenset({"boss@corp.com"}))

    def test_invalid_rules_file_stops_startup(self):
        settings = Settings(_env_file=None, db_path=":memory:", triage_rules_path="/nonexistent.toml")
        components = fake_components()
        self.addCleanup(components.store.close)

        with self.assertRaises(ConfigurationError):
            ApplicationContext(settings, components)

    def test_confidence_is_observed_per_deciding_stage(self):
        def count(source):
            labels = {"urgency": "low", "source": source}
            return REGISTRY.get_sample_value("triage_confidence_count", labels) or 0.0

        before = count("jev"), count("unknown")

        Metrics.mark_route("label", "low", 0.42, "jev")
        Metrics.mark_route("label", "low", 0.42)

        self.assertEqual((count("jev"), count("unknown")), (before[0] + 1, before[1] + 1))


if __name__ == "__main__":
    unittest.main()
