import unittest

from src.formatting import clean_draft, strip_markdown, truncate
from src.triage.rules import is_automated_sender


class StripMarkdownTests(unittest.TestCase):
    def test_removes_bold_headings_rules_and_backticks(self):
        text = "# Title\n**Event:** `run` failed\n***\n__done__"
        self.assertEqual(strip_markdown(text), "Title\nEvent: run failed\n\ndone")

    def test_converts_bullets_and_links(self):
        text = "* one\n- two\n[docs](https://a.io/x)"
        self.assertEqual(strip_markdown(text), "• one\n• two\ndocs (https://a.io/x)")

    def test_bullet_style_is_configurable(self):
        self.assertEqual(strip_markdown("* one", bullet="- "), "- one")

    def test_does_not_mistake_bold_start_for_a_bullet(self):
        self.assertEqual(strip_markdown("**Bold** text"), "Bold text")

    def test_collapses_blank_runs(self):
        self.assertEqual(strip_markdown("a\n\n\n\nb"), "a\n\nb")


class CleanDraftTests(unittest.TestCase):
    def test_drops_preamble_rule_and_subject_line(self):
        draft = "Voici une proposition de réponse courte et professionnelle :\n\n***\n\n**Objet :** RE: X\n\nBonjour,\n\nMerci."
        self.assertEqual(clean_draft(draft), "Bonjour,\n\nMerci.")

    def test_drops_english_preamble_and_subject(self):
        self.assertEqual(clean_draft("Here is a draft:\nSubject: Re: Hi\n\nHello,\nBest"), "Hello,\nBest")

    def test_keeps_a_body_that_merely_mentions_the_words_later(self):
        draft = "Bonjour,\n\nVoici les documents demandés :\n- a\n- b"
        self.assertEqual(clean_draft(draft), draft)

    def test_leaves_a_clean_draft_untouched(self):
        self.assertEqual(clean_draft("Bonjour,\n\nÀ demain,\nAristide"), "Bonjour,\n\nÀ demain,\nAristide")

    def test_empty_input(self):
        self.assertEqual(clean_draft(""), "")


class TruncateTests(unittest.TestCase):
    def test_short_text_is_unchanged(self):
        self.assertEqual(truncate("abc", 5), "abc")

    def test_long_text_is_cut_with_ellipsis_within_the_limit(self):
        result = truncate("abcdefghij", 5)
        self.assertEqual(result, "abcd…")
        self.assertLessEqual(len(result), 5)


class AutomatedSenderTests(unittest.TestCase):
    def test_detects_automated_senders(self):
        for sender in (
            "notifications@github.com",
            "jobalerts-noreply@linkedin.com",
            "no-reply@x.io",
            "donotreply@x.io",
            "newsletter@brand.com",
        ):
            with self.subTest(sender=sender):
                self.assertTrue(is_automated_sender(sender))

    def test_humans_are_not_automated(self):
        for sender in ("celinekougang@outlook.com", "recruiter@example.com", "alerte.martin@x.io"):
            with self.subTest(sender=sender):
                self.assertFalse(is_automated_sender(sender))
