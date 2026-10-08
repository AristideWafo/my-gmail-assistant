import base64
import unittest

from src.gmail.text_cleaning import (
    clean_body,
    decode_body,
    extract_domain,
    strip_html,
    strip_quoted_reply,
    strip_signature,
    truncate_words,
)


class TextCleaningTests(unittest.TestCase):
    def test_decode_body_handles_missing_padding(self):
        raw = base64.urlsafe_b64encode(b"Hello world").decode("utf-8").rstrip("=")
        self.assertEqual(decode_body(raw), "Hello world")

    def test_decode_body_empty_input(self):
        self.assertEqual(decode_body(""), "")

    def test_strip_html_removes_tags_and_unescapes_entities(self):
        result = strip_html("<p>Hello &amp; welcome</p>")
        self.assertEqual(result, "Hello & welcome")

    def test_strip_signature_cuts_at_rfc3676_delimiter(self):
        text = "Body text\n-- \nJohn Doe\nCEO"
        self.assertEqual(strip_signature(text), "Body text")

    def test_strip_signature_no_delimiter_returns_unchanged(self):
        text = "Just a plain message"
        self.assertEqual(strip_signature(text), text)

    def test_truncate_words_limits_length(self):
        text = " ".join(f"word{i}" for i in range(1500))
        result = truncate_words(text, max_words=1000)
        self.assertEqual(len(result.split()), 1000)

    def test_truncate_words_can_keep_the_end_after_a_visible_cut(self):
        text = " ".join(f"word{i}" for i in range(1500))

        result = truncate_words(text, max_words=700, tail_words=300).split()

        self.assertEqual(len(result), 1001)
        self.assertEqual((result[699], result[700], result[701]), ("word699", "[…]", "word1200"))
        self.assertEqual(result[-1], "word1499")

    def test_truncate_words_with_a_tail_leaves_a_text_that_fits_untouched(self):
        text = " ".join(f"word{i}" for i in range(1000))

        self.assertEqual(truncate_words(text, max_words=700, tail_words=300), text)

    def test_clean_body_can_return_the_whole_text(self):
        text = " ".join(f"word{i}" for i in range(1500))

        self.assertEqual(len(clean_body(text, is_html=False).split()), 1000)
        self.assertEqual(len(clean_body(text, is_html=False, max_words=None).split()), 1500)

    def test_truncate_words_under_limit_unchanged(self):
        text = "short message"
        self.assertEqual(truncate_words(text), text)

    def test_extract_domain(self):
        self.assertEqual(extract_domain("recruiter@linkedin.com"), "linkedin.com")

    def test_extract_domain_no_at_sign(self):
        self.assertEqual(extract_domain("not-an-email"), "")

    def test_clean_body_html_pipeline(self):
        html_text = "<div>Hi there</div>\n-- \nSignature block"
        self.assertEqual(clean_body(html_text, is_html=True), "Hi there")

    def test_clean_body_plain_pipeline(self):
        plain_text = "Hi there\n-- \nSignature block"
        self.assertEqual(clean_body(plain_text, is_html=False), "Hi there")


class StripQuotedReplyTests(unittest.TestCase):
    def test_cuts_at_a_french_attribution_even_when_wrapped(self):
        text = (
            "Merci, bien reçu.\n\nLe lun. 5 oct. 2026 à 10:00, Jean <jean@example.com> a\n"
            "écrit :\n> Peux-tu me renvoyer le document ?"
        )

        self.assertEqual(strip_quoted_reply(text), "Merci, bien reçu.")

    def test_cuts_at_an_english_attribution(self):
        text = "Can you send it by Friday?\n\nOn Mon, Oct 5, 2026 at 10:00 AM Jean <j@x.com> wrote:\n> hi"

        self.assertEqual(strip_quoted_reply(text), "Can you send it by Friday?")

    def test_cuts_at_an_outlook_header_block_or_a_forward_marker(self):
        outlook = "Voir ci-dessous.\n\nDe : Jean\nEnvoyé : lundi 5 octobre\nObjet : Devis\n\nUne question ?"
        forward = "Pour info.\n\n---------- Forwarded message ---------\nFrom: a@x.com"

        self.assertEqual(strip_quoted_reply(outlook), "Voir ci-dessous.")
        self.assertEqual(strip_quoted_reply(forward), "Pour info.")

    def test_drops_quoted_lines_written_inline(self):
        text = "> Tu viens samedi ?\nOui, je viens.\n> Et dimanche ?\nNon."

        self.assertEqual(strip_quoted_reply(text), "Oui, je viens.\nNon.")

    def test_html_is_cut_at_the_gmail_quote_before_its_tags_are_removed(self):
        html = (
            "<div>Ok pour moi</div><div class=\"gmail_quote\"><div>Le 5 oct., Jean a écrit :</div>"
            "<blockquote>Tu confirmes ?</blockquote></div>"
        )

        self.assertEqual(strip_quoted_reply(html, is_html=True), "Ok pour moi")

    def test_a_mail_without_quote_is_kept_whole(self):
        text = "Le dossier est prêt.\nLe reste suit demain."

        self.assertEqual(strip_quoted_reply(text), text)


if __name__ == "__main__":
    unittest.main()
