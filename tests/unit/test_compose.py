"""compose.py: the outgoing .eml, the locked signature and her deterministic Message-IDs."""
import email
import email.policy
import hashlib
import os
import time
import unittest
from unittest import mock

from doris import compose, templates
from doris.compose import ComposeError
from tests.unit.voice_fakes import FakeZones, make_msg, utc

DORIS = "doris.htm@fastmail.com"
OWNER = "whizz@fastmail.com"
LINK = "https://meet.example/" + "voice-test-room"
WHEN = utc(2026, 12, 1, 16, 3)

ICS_PUBLISH = (b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Doris//EN\r\nMETHOD:PUBLISH\r\nBEGIN:VEVENT\r\n"
               b"UID:doris-abc\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
ICS_CANCEL = ICS_PUBLISH.replace(b"METHOD:PUBLISH", b"METHOD:CANCEL")


def t1_slots():
    return {"start": utc(2026, 12, 2, 19, 0), "duration": 30, "zone": "Central",
            "meeting_link": templates.MEETING_LINK}


class Base(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(templates, "ZONES", FakeZones())
        p.start()
        self.addCleanup(p.stop)
        e = mock.patch.dict(os.environ, {"DORIS_MEETING_LINK": LINK})
        e.start()
        self.addCleanup(e.stop)
        self.inbound = make_msg(references=["<root@northwind.example>"])

    def reply(self, template_id="T1", slots=None, ics=ICS_PUBLISH, **kw):
        args = dict(msg=self.inbound, template_id=template_id, slots=slots if slots is not None else t1_slots(),
                    doris_addr=DORIS, message_id=compose.new_message_id(DORIS, self.inbound.key, template_id),
                    date=WHEN, ics=ics)
        args.update(kw)
        return compose.reply(**args)


class MessageIdTest(unittest.TestCase):
    def test_formula_from_the_contract(self):
        digest = hashlib.sha256("msg-1@northwind.example|T2|1".encode("utf-8")).hexdigest()[:32]
        self.assertEqual(compose.new_message_id(DORIS, "msg-1@northwind.example", "T2"),
                         "<" + digest + "@fastmail.com>")
        self.assertNotEqual(compose.new_message_id(DORIS, "msg-1@northwind.example", "T2", n=2),
                            compose.new_message_id(DORIS, "msg-1@northwind.example", "T2"))

    def test_digest_key(self):
        self.assertEqual(compose.digest_key(utc(2026, 12, 2, 1, 0)), "digest:2026-12-01")


class SignatureTest(unittest.TestCase):
    def test_signature_is_the_two_locked_lines(self):
        self.assertEqual(compose.SIGNATURE.split("\n"), [
            "Doris, AI assistant to Shane Bornstein, Hire the Machines.",
            "I'm an AI, not a person. Reply \"stop\" to opt out."])

    def test_signature_ok_exact(self):
        good = "Hello.\n\n" + compose.SIGNATURE
        self.assertTrue(compose.signature_ok(good))
        self.assertFalse(compose.signature_ok("Hello."))
        self.assertFalse(compose.signature_ok(good + " "))
        self.assertFalse(compose.signature_ok(good.replace("opt out.", "opt out!")))
        self.assertFalse(compose.signature_ok(good + "\n\n" + compose.SIGNATURE))
        self.assertFalse(compose.signature_ok(None))


class ReplyTest(Base):
    def test_headers_and_threading(self):
        raw, body = self.reply()
        self.assertIn(b"\r\n", raw)
        self.assertNotIn(b"\r\r", raw)
        self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"))
        m = email.message_from_bytes(raw, policy=email.policy.default)
        self.assertEqual(m["From"], DORIS)
        self.assertEqual(m["To"], "priya@northwind.example")
        self.assertIsNone(m["Cc"])
        self.assertIsNone(m["Bcc"])
        self.assertEqual(m["Subject"], "Re: Quick call this week?")
        self.assertEqual(m["In-Reply-To"], "<msg-1@northwind.example>")
        self.assertEqual(m["References"], "<root@northwind.example> <msg-1@northwind.example>")
        self.assertEqual(m["Message-ID"], compose.new_message_id(DORIS, self.inbound.key, "T1"))
        self.assertEqual(m["Auto-Submitted"], "auto-replied")
        self.assertEqual(m["Date"], "Tue, 01 Dec 2026 16:03:00 +0000")

    def test_body_is_template_plus_signature(self):
        raw, body = self.reply()
        self.assertTrue(body.startswith("You're booked with Shane Bornstein for Wed Dec 2, 1:00 PM Central"))
        self.assertTrue(compose.signature_ok(body))
        m = email.message_from_bytes(raw, policy=email.policy.default)
        text = m.get_body(preferencelist=("plain",)).get_content()
        self.assertEqual(text.replace("\r\n", "\n").rstrip("\n"), body)

    def test_ics_part_carries_method(self):
        raw, _ = self.reply()
        m = email.message_from_bytes(raw, policy=email.policy.default)
        parts = [p for p in m.walk() if p.get_content_type() == "text/calendar"]
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].get_param("method"), "PUBLISH")
        self.assertEqual(parts[0].get_content_disposition(), "attachment")
        self.assertEqual(parts[0].get_payload(decode=True), ICS_PUBLISH)

    def test_ics_only_where_the_template_carries_one(self):
        with self.assertRaises(ComposeError):
            self.reply(ics=None)
        offer = {"slots": [utc(2026, 12, 2, 19, 30), utc(2026, 12, 3, 17, 0)], "duration": 30, "zone": None}
        with self.assertRaises(ComposeError):
            self.reply(template_id="T2", slots=offer, ics=ICS_PUBLISH)
        raw, _ = self.reply(template_id="T2", slots=offer, ics=None)
        self.assertNotIn(b"text/calendar", raw)
        self.assertNotIn(b"multipart", raw)

    def test_ics_method_must_match_template(self):
        with self.assertRaises(ComposeError):
            self.reply(ics=ICS_CANCEL)
        raw, _ = self.reply(template_id="T10", slots={"start": utc(2026, 12, 2, 19, 0), "zone": "Central"},
                            ics=ICS_CANCEL)
        self.assertIn(b"method=CANCEL", raw)

    def test_identical_inputs_identical_bytes(self):
        self.assertEqual(self.reply()[0], self.reply()[0])

    def test_existing_re_kept_and_header_injection_stripped(self):
        self.inbound = make_msg(subject="RE: Friday?\r\nBcc: x@evil.example")
        raw, _ = self.reply()
        m = email.message_from_bytes(raw, policy=email.policy.default)
        self.assertEqual(m["Subject"], "RE: Friday? Bcc: x@evil.example")
        self.assertIsNone(m["Bcc"])

    def test_a_code_in_the_subject_is_not_echoed(self):
        # Fable review of PR #4, finding 1: the reply's Subject carried the sender's canary back.
        from doris import traps
        self.inbound = make_msg(subject="Sync AGW-CANARY-­7QXZ next week?")
        raw, _ = self.reply()
        m = email.message_from_bytes(raw, policy=email.policy.default)
        self.assertEqual(m["Subject"], "Re: Sync [redacted] next week?")
        self.assertEqual(traps.find_tokens(raw.decode("latin-1")), [])

    def test_a_long_subject_is_read_only_so_far(self):
        # Second Fable review of PR #4, H2: a Subject of "The key is", 12,000 spaces and "!" took 3.6 s.
        self.inbound = make_msg(subject="The key is" + " " * 30000 + "!")
        start = time.monotonic()
        raw, _ = self.reply()
        self.assertLess(time.monotonic() - start, 2.0)
        m = email.message_from_bytes(raw, policy=email.policy.default)
        self.assertEqual(m["Subject"], "Re: The key [redacted]")

    def test_non_ascii_subject_encoded(self):
        self.inbound = make_msg(subject="Café chat")
        raw, _ = self.reply()
        raw.decode("ascii")
        m = email.message_from_bytes(raw, policy=email.policy.default)
        self.assertEqual(m["Subject"], "Re: Café chat")

    def test_a_reference_outside_ascii_is_left_out(self):
        # one byte outside ASCII in References must not stop the reply after the booking (Fable #2)
        self.inbound = make_msg(references=["<café@x.example>", "<root@northwind.example>"])
        raw, _ = self.reply()
        raw.decode("ascii")
        m = email.message_from_bytes(raw, policy=email.policy.default)
        self.assertEqual(m["References"], "<root@northwind.example> <msg-1@northwind.example>")

    def test_a_message_id_outside_ascii_threads_on_nothing(self):
        self.inbound = make_msg(key="café@x.example", message_id="<café@x.example>")
        raw, _ = self.reply()
        raw.decode("ascii")
        m = email.message_from_bytes(raw, policy=email.policy.default)
        self.assertIsNone(m["In-Reply-To"])
        self.assertIsNone(m["References"])

    def test_a_control_character_in_a_message_id_is_left_out(self):
        self.inbound = make_msg(references=["<a\x7fb@x.example>"])
        raw, _ = self.reply()
        m = email.message_from_bytes(raw, policy=email.policy.default)
        self.assertEqual(m["References"], "<msg-1@northwind.example>")

    def test_a_sender_address_outside_ascii_is_refused(self):
        self.inbound = make_msg(from_addr="josé@x.example")
        with self.assertRaises(ComposeError):
            self.reply()

    def test_unapproved_template_refused(self):
        with self.assertRaises(templates.TemplateError):
            self.reply(template_id="T17", slots={}, ics=None)
        with self.assertRaises(ComposeError):
            self.reply(template_id="S1", slots={}, ics=None)

    def test_naive_date_refused(self):
        import datetime as dt
        with self.assertRaises(ComposeError):
            self.reply(date=dt.datetime(2026, 12, 1, 16, 3))

    def test_envelope_bccs_the_owner_on_prospect_sends(self):
        self.assertEqual(compose.reply_envelope(self.inbound, OWNER), ["priya@northwind.example", OWNER])
        with self.assertRaises(ComposeError):
            compose.reply_envelope(make_msg(from_addr=""), OWNER)


class OwnerNoticeTest(Base):
    def test_s2_to_owner_only(self):
        slots = templates.empty_digest(utc(2026, 12, 2, 1, 0))
        mid = compose.new_message_id(DORIS, compose.digest_key(utc(2026, 12, 2, 1, 0)), "S2")
        raw, body = compose.owner_notice("S2", slots, DORIS, OWNER, mid, WHEN)
        m = email.message_from_bytes(raw, policy=email.policy.default)
        self.assertEqual(m["To"], OWNER)
        self.assertIsNone(m["Bcc"])
        self.assertEqual(m["Subject"], "Handled 0, held 0, human approvals 0. Tue Dec 1")
        self.assertEqual(m["Auto-Submitted"], "auto-replied")
        self.assertIsNone(m["In-Reply-To"])
        self.assertTrue(compose.signature_ok(body))

    def test_s1_subject_and_prospect_template_refused(self):
        slots = {"name": "Elena Voss", "address": "elena@harborline.example", "reason": "hold.no_model",
                 "wrote_at": utc(2026, 12, 1, 21, 10), "quote": "hi", "first_name": "Elena",
                 "draft_slots": [], "zone": None}
        raw, body = compose.owner_notice("S1", slots, DORIS, OWNER, "<x@fastmail.com>", WHEN)
        m = email.message_from_bytes(raw, policy=email.policy.default)
        self.assertEqual(m["Subject"], "Held for you: Elena Voss")
        with self.assertRaises(ComposeError):
            compose.owner_notice("T1", t1_slots(), DORIS, OWNER, "<x@fastmail.com>", WHEN)


if __name__ == "__main__":
    unittest.main()
