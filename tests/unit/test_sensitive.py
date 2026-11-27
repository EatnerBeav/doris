"""sensitive.scan_message / scan_text: each check of screen 5 on both sides."""
import unittest

from doris import mail, sensitive

from tests.unit import screens_eml as E

# Card, routing and account numbers are built at runtime so this file never holds a literal one.
VISA = "4111" + " 1111" * 3                # passes Luhn
VISA_BAD = "4111" + " 1111" * 2 + " 1112"  # fails Luhn
AMEX = "3782" + "-822463-" + "10005"        # 15 digits, passes Luhn
ROUTING = "0210" + "00021"                 # passes the ABA checksum
ROUTING_BAD = "0210" + "00022"


def msg(body="Could Shane do Thursday, December 3 at 2:00 PM Pacific?\n", **kw):
    return mail.parse_eml(E.plain(body, **kw))


class SenderDomain(unittest.TestCase):
    def test_listed_domains_and_subdomains(self):
        for frm in ("alerts@chase.com", "no-reply@alerts.Chase.com", "service@paypal.com", "x@notices.irs.gov",
                    "a@mychart.ucsf.example", "b@ucsd-mychart.example"):
            with self.subTest(frm=frm):
                self.assertEqual(sensitive.scan_message(msg(frm=frm)), "sender-domain")

    def test_names_inside_other_domains_do_not_match(self):
        for frm in ("Chase Miller <chase@acme.example>", "deals@purchase.com", "a@discovery.example",
                    "b@mychartreuse.example", "c@notchase.com"):
            with self.subTest(frm=frm):
                self.assertIsNone(sensitive.scan_message(msg(frm=frm)))


class SubjectPhrase(unittest.TestCase):
    def test_phrases(self):
        for subj in ("Your verification code", "Security code inside", "Your one-time passcode",
                     "One-time password", "Password reset requested", "New sign-in to your account",
                     "New login from Chrome", "Two-factor authentication", "Your 2FA settings",
                     "Your account statement", "Your statement is ready", "Your W-2 is available",
                     "Form 1099-NEC"):
            with self.subTest(subj=subj):
                self.assertEqual(sensitive.scan_message(msg(subject=subj)), "subject-phrase")

    def test_bare_words_do_not_match(self):
        for subj in ("Statement of work call?", "One-time intro", "password for the deck", "Sign in sheet",
                     "Window seat"):
            with self.subTest(subj=subj):
                self.assertIsNone(sensitive.scan_message(msg(subject=subj)))


class Body(unittest.TestCase):
    def check(self, body):
        return sensitive.scan_message(msg(body, subject="Hello"))

    def test_one_time_code(self):
        self.assertEqual(self.check("Your verification code is 482913. It expires soon.\n"), "one-time-code")
        self.assertEqual(self.check("Use this passcode:\n\n  4829\n"), "one-time-code")
        self.assertEqual(self.check("Your OTP is G-48X29Q\n"), "one-time-code")
        self.assertEqual(self.check("Your code: 482 913\n"), "one-time-code")

    def test_one_time_code_not_a_year_time_date_or_bare_number(self):
        for body in ("One-time intro for 2026?\n", "Your code review is at 10:30 on 2026-12-03.\n",
                     "Order 482913 shipped.\n", "Your code ships in 2026.\n"):
            with self.subTest(body=body):
                self.assertIsNone(self.check(body))

    def test_card_number(self):
        self.assertEqual(self.check("Pay with card %s by Friday.\n" % VISA), "card-number")
        self.assertEqual(self.check("Amex %s please.\n" % AMEX), "card-number")
        self.assertEqual(self.check("Card: %s\n" % VISA.replace(" ", "")), "card-number")

    def test_card_number_must_pass_luhn_and_not_be_a_phone(self):
        self.assertIsNone(self.check("Ref %s\n" % VISA_BAD))
        self.assertIsNone(self.check("Call +%s anytime.\n" % VISA.replace(" ", "")))
        self.assertIsNone(self.check("Phone: %s\n" % VISA.replace(" ", "")))

    def test_bank_routing(self):
        self.assertEqual(self.check("Our routing number is %s.\n" % ROUTING), "bank-routing")
        self.assertEqual(self.check("ABA %s\n" % ROUTING), "bank-routing")

    def test_bank_routing_needs_wording_and_checksum(self):
        self.assertIsNone(self.check("Ticket %s is open.\n" % ROUTING))
        self.assertIsNone(self.check("Routing number %s\n" % ROUTING_BAD))

    def test_bank_account(self):
        self.assertEqual(self.check("Account number: 00123456789\n"), "bank-account")
        self.assertEqual(self.check("Wire it to checking 987654321012.\n"), "bank-account")
        self.assertEqual(self.check("IBAN GB82 WEST 1234 5698 7654 32\n"), "bank-account")

    def test_bank_account_word_alone_does_not_match(self):
        self.assertIsNone(self.check("I just left the bank. My account manager says hi.\n"))
        self.assertIsNone(self.check("Bank holiday on 2026-12-25, account for it.\n"))

    def test_ssn(self):
        self.assertEqual(self.check("My SSN is 123-45-6789.\n"), "ssn")
        self.assertEqual(self.check("social security number 123456789\n"), "ssn")

    def test_ssn_impossible_or_phone_or_postal(self):
        for body in ("Ref 000-12-3456.\n", "Ref 666-12-3456.\n", "Call 415-555-0134.\n", "Zip 94107-1234.\n",
                     "Ref 123-00-6789.\n"):
            with self.subTest(body=body):
                self.assertIsNone(self.check(body))

    def test_reset_or_login_alert(self):
        for body in ("Reset your password here.\n", "We saw a new sign-in on Mac.\n", "Unusual sign-in activity\n",
                     "Was this you?\n", "Verify it\u2019s you.\n", "Login attempt blocked\n"):
            with self.subTest(body=body):
                self.assertEqual(self.check(body), "reset-or-login-alert")

    def test_password_alone_does_not_match(self):
        self.assertIsNone(self.check("The password for the deck is in the doc.\n"))

    def test_mentions_a_bank_still_clean(self):
        self.assertIsNone(self.check("I just left the bank. Could Shane do Thursday, December 3 at 2:00 PM Pacific?\n"))

    def test_phone_and_postal_code_in_signature_clean(self):
        body = "Could Shane do Dec 3 at 2pm PT?\n\nThanks,\nRae\n+1 415 555 0134\nSan Francisco, CA 94107\n"
        self.assertIsNone(self.check(body))

    def test_quoted_card_number_matches(self):
        body = "Sounds good.\n\nOn Tue, Dec 1, 2026 Bo wrote:\n> card %s\n" % VISA
        self.assertEqual(self.check(body), "card-number")

    def test_html_only_and_hidden_text_are_scanned(self):
        m = mail.parse_eml(E.html_only('<p>Hi.</p><span style="display:none">card %s</span>' % VISA))
        self.assertEqual(sensitive.scan_message(m), "card-number")

    def test_html_part_scanned_when_plain_part_is_clean(self):
        m = mail.parse_eml(E.alternative("Hi.\n", "<p>Your verification code is 482913</p>"))
        self.assertEqual(sensitive.scan_message(m), "one-time-code")


class Hostile(unittest.TestCase):
    def test_many_number_candidates_scan_quickly(self):
        import time
        text = ("Ticket " + "1234" + "56789 about the checkin. ") * 15000
        t = time.monotonic()
        sensitive.scan_text(text)
        self.assertLess(time.monotonic() - t, 5.0)


class HeadersNotScanned(unittest.TestCase):
    def test_reply_naming_her_sha256_message_id_is_clean(self):
        her = "<" + "021000021" + "4111111111111111" + "0a" * 4 + "@fastmail.com>"
        m = msg("2 works. Account number below? No.\n", mid="<" + "123456789" + "@acme.example>",
                extra=("In-Reply-To: " + her, "References: " + her))
        self.assertIsNone(sensitive.scan_message(m))


class ScanText(unittest.TestCase):
    def test_same_body_patterns(self):
        self.assertEqual(sensitive.scan_text("Quote: pay with %s" % VISA), "card-number")
        self.assertEqual(sensitive.scan_text("Your verification code is 482913"), "one-time-code")

    def test_digest_line_naming_a_bank_sender_is_clean(self):
        self.assertIsNone(sensitive.scan_text("Sensitive mail skipped: alerts@chase.com"))

    def test_her_own_template_text_is_clean(self):
        text = ("Shane is available at any of these, 30 minutes each:\n1. Wed Dec 2, 11:00 AM Pacific (2:00 PM Eastern)\n"
                "Doris, AI assistant to Shane Bornstein, Hire the Machines. I'm an AI, not a person. Reply \"stop\" to opt out.")
        self.assertIsNone(sensitive.scan_text(text))

    def test_none_and_empty(self):
        self.assertIsNone(sensitive.scan_text(""))
        self.assertIsNone(sensitive.scan_text(None))


if __name__ == "__main__":
    unittest.main()
