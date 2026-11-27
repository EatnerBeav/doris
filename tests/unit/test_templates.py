"""templates.py: the only sentences Doris can send, filled by code from typed slots."""
import os
import unittest
from unittest import mock

from doris import templates, why
from doris.templates import TemplateError, MEETING_LINK
from tests.unit.voice_fakes import FakeZones, utc

LINK = "https://meet.example/" + "voice-test-room"

# Demo day (Tue Dec 1 2026, Pacific is UTC-8)
WED_1130 = utc(2026, 12, 2, 19, 30)
THU_0900 = utc(2026, 12, 3, 17, 0)
FRI_1000 = utc(2026, 12, 4, 18, 0)
OFFER = [WED_1130, THU_0900, FRI_1000]


class Base(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(templates, "ZONES", FakeZones())
        p.start()
        self.addCleanup(p.stop)
        e = mock.patch.dict(os.environ, {"DORIS_MEETING_LINK": LINK})
        e.start()
        self.addCleanup(e.stop)


class RunLinkTest(Base):
    """A run pins the one link it uses, so the reply, the .ics and the gate's G13 read the same value;
    the env and then the placeholder are only the fallback when nothing is pinned."""

    def test_a_pinned_link_wins_over_the_env_and_unpinning_restores_the_fallback(self):
        pinned = "https://meet.example/" + "pinned-room"
        before = templates.use_meeting_link(pinned)
        try:
            self.assertEqual(templates.meeting_link(), pinned)
            body = templates.render("T1", {"start": WED_1130, "duration": 30, "zone": None,
                                           "meeting_link": MEETING_LINK})
            self.assertIn("Join: " + pinned + "\n", body)
        finally:
            self.assertEqual(templates.use_meeting_link(before), pinned)
        self.assertEqual(templates.meeting_link(), LINK)


class FormatTest(Base):
    def test_fmt_slot_shane_first_with_their_zone(self):
        self.assertEqual(templates.fmt_slot(utc(2026, 12, 2, 19, 0), "Eastern"),
                         "Wed Dec 2, 11:00 AM Pacific (2:00 PM Eastern)")

    def test_fmt_slot_unknown_zone_shows_utc_24h(self):
        self.assertEqual(templates.fmt_slot(utc(2026, 12, 2, 21, 30), None),
                         "Wed Dec 2, 1:30 PM Pacific (21:30 UTC)")

    def test_fmt_slot_pacific_sender_no_bracket(self):
        self.assertEqual(templates.fmt_slot(FRI_1000, "Pacific"), "Fri Dec 4, 10:00 AM Pacific")

    def test_fmt_slot_bracket_names_day_when_date_differs(self):
        self.assertEqual(templates.fmt_slot(utc(2026, 12, 2, 17, 0), "UTC+09:00"),
                         "Wed Dec 2, 9:00 AM Pacific (Thu Dec 3, 02:00 UTC+09:00)")

    def test_noon_and_midnight(self):
        self.assertEqual(templates.fmt_slot(utc(2026, 12, 3, 20, 0), "Pacific"), "Thu Dec 3, 12:00 PM Pacific")
        self.assertEqual(templates.fmt_slot(utc(2026, 12, 3, 8, 0), "Pacific"), "Thu Dec 3, 12:00 AM Pacific")

    def test_naive_datetime_refused(self):
        import datetime as dt
        with self.assertRaises(TemplateError):
            templates.fmt_slot(dt.datetime(2026, 12, 2, 19, 0), None)


class ProspectTemplatesTest(Base):
    def test_t1_their_zone_first_demo_text(self):
        body = templates.render("T1", {"start": utc(2026, 12, 2, 19, 0), "duration": 30, "zone": "Central",
                                       "meeting_link": MEETING_LINK})
        self.assertEqual(body, "You're booked with Shane Bornstein for Wed Dec 2, 1:00 PM Central, which is "
                               "11:00 AM Pacific for Shane, 30 min.\nThe calendar file is attached. Join: "
                               + LINK + "\nTo move or cancel, reply on this thread.")

    def test_t1_pacific_or_unknown_shows_pacific_and_utc(self):
        for zone in (None, "Pacific"):
            body = templates.render("T1", {"start": utc(2026, 12, 7, 18, 0), "duration": 45, "zone": zone,
                                           "meeting_link": MEETING_LINK})
            self.assertTrue(body.startswith("You're booked with Shane Bornstein for Mon Dec 7, 10:00 AM Pacific "
                                            "(18:00 UTC), 45 min.\n"), body)

    def test_t2_demo_text(self):
        body = templates.render("T2", {"slots": OFFER, "duration": 30, "zone": "Eastern"})
        self.assertEqual(body, "Shane is available at any of these, 30 min each:\n"
                               "1. Wed Dec 2, 11:30 AM Pacific (2:30 PM Eastern)\n"
                               "2. Thu Dec 3, 9:00 AM Pacific (12:00 PM Eastern)\n"
                               "3. Fri Dec 4, 10:00 AM Pacific (1:00 PM Eastern)\n"
                               "Reply with the one that suits you and I'll book it.")

    def test_t2_refuses_one_slot_use_t13(self):
        with self.assertRaises(TemplateError):
            templates.render("T2", {"slots": OFFER[:1], "duration": 30, "zone": None})

    def test_t3_needs_three(self):
        body = templates.render("T3", {"slots": OFFER, "duration": 30, "zone": "Pacific"})
        self.assertTrue(body.startswith("Here are three more times Shane is available, 30 min each:\n1. "))
        self.assertTrue(body.endswith("\nReply with one and I'll book it."))
        with self.assertRaises(TemplateError):
            templates.render("T3", {"slots": OFFER[:2], "duration": 30, "zone": None})

    def test_t4_demo_text(self):
        body = templates.render("T4", {"asked": utc(2026, 12, 4, 22, 0), "slots": OFFER, "duration": 30,
                                       "zone": "Pacific"})
        self.assertEqual(body, "Shane isn't available at Fri Dec 4, 2:00 PM Pacific, but is available at any "
                               "of these, 30 min each:\n1. Wed Dec 2, 11:30 AM Pacific\n"
                               "2. Thu Dec 3, 9:00 AM Pacific\n3. Fri Dec 4, 10:00 AM Pacific\n"
                               "Reply with the one that suits you and I'll book it.")

    def test_t4_one_slot_one_numbered_line(self):
        body = templates.render("T4", {"asked": utc(2026, 12, 4, 22, 0), "slots": OFFER[:1], "duration": 30,
                                       "zone": "Pacific"})
        self.assertIn(":\n1. Wed Dec 2, 11:30 AM Pacific\nReply with the one", body)

    def test_t9_and_one_slot_form(self):
        body = templates.render("T9", {"slots": OFFER[:2], "duration": 30, "zone": "Pacific"})
        self.assertEqual(body, "One more try: Shane is still available at any of these, 30 min each:\n"
                               "1. Wed Dec 2, 11:30 AM Pacific\n2. Thu Dec 3, 9:00 AM Pacific\n"
                               "After this I'll leave it with you.")
        one = templates.render("T9", {"slots": OFFER[:1], "duration": 30, "zone": "Pacific"})
        self.assertEqual(one, "One more try: Shane is still available at Wed Dec 2, 11:30 AM Pacific, 30 min.\n"
                              "Reply \"yes\" and I'll book it. After this I'll leave it with you.")

    def test_t10_their_zone_first(self):
        body = templates.render("T10", {"start": utc(2026, 12, 2, 19, 0), "zone": "Central"})
        self.assertEqual(body, "Done. Your call with Shane on Wed Dec 2, 1:00 PM Central (11:00 AM Pacific) is "
                               "cancelled, and the attached file removes it from your calendar.\n"
                               "If you'd like another time, reply with a day that suits you.")

    def test_t12_known_and_unknown_zone(self):
        body = templates.render("T12", {"slots": OFFER[1:2], "zone": "Eastern"})
        self.assertEqual(body, "I couldn't match that to one of the times I offered, so here they are again in "
                               "your time and Shane's:\n1. Thu Dec 3, 12:00 PM Eastern (9:00 AM Pacific)\n"
                               "Reply with the number of the one that suits you and I'll book it.")
        body = templates.render("T12", {"slots": OFFER[1:2], "zone": None})
        self.assertIn("again in UTC and Shane's time:\n1. Thu Dec 3, 17:00 UTC (9:00 AM Pacific)\n", body)

    def test_t13(self):
        self.assertEqual(templates.render("T13", {"slot": FRI_1000, "duration": 20, "zone": "Pacific"}),
                         "Shane is available at Fri Dec 4, 10:00 AM Pacific, 20 min.\nReply \"yes\" and I'll book it.")

    def test_t18_cancel_line_then_offer_shane_first(self):
        slots = [utc(2026, 12, 3, 22, 0), utc(2026, 12, 4, 18, 0), utc(2026, 12, 7, 17, 0)]
        body = templates.render("T18", {"start": utc(2026, 12, 2, 19, 0), "slots": slots, "duration": 30,
                                        "zone": "Central"})
        self.assertEqual(body, "Done. Your call with Shane on Wed Dec 2, 1:00 PM Central (11:00 AM Pacific) is "
                               "cancelled, and the attached file removes it from your calendar.\n"
                               "If you'd like another time, Shane is available at any of these, 30 min each:\n"
                               "1. Thu Dec 3, 2:00 PM Pacific (4:00 PM Central)\n"
                               "2. Fri Dec 4, 10:00 AM Pacific (12:00 PM Central)\n"
                               "3. Mon Dec 7, 9:00 AM Pacific (11:00 AM Central)\n"
                               "Reply with the one that suits you and I'll book it.")

    def test_t18_one_slot_form(self):
        body = templates.render("T18", {"start": utc(2026, 12, 2, 19, 0), "slots": OFFER[:1], "duration": 30,
                                        "zone": "Pacific"})
        self.assertTrue(body.endswith("\nIf you'd like another time, Shane is available at Wed Dec 2, 11:30 AM "
                                      "Pacific, 30 min.\nReply \"yes\" and I'll book it."), body)


class SlotValidationTest(Base):
    def test_unknown_template_refused(self):
        for tid in ("T5", "T6", "T7", "T11", "T14", "T15", "T16", "T17", "K1", "X"):
            with self.assertRaises(TemplateError):
                templates.render(tid, {})

    def test_missing_extra_and_wrong_type_slots(self):
        good = {"slots": OFFER, "duration": 30, "zone": None}
        with self.assertRaises(TemplateError):
            templates.render("T2", {"slots": OFFER, "duration": 30})
        with self.assertRaises(TemplateError):
            templates.render("T2", dict(good, note="Thanks for reaching out!"))
        with self.assertRaises(TemplateError):
            templates.render("T2", dict(good, duration="30"))
        with self.assertRaises(TemplateError):
            templates.render("T2", dict(good, duration=True))
        with self.assertRaises(TemplateError):
            templates.render("T2", dict(good, duration=90))
        with self.assertRaises(TemplateError):
            templates.render("T2", dict(good, slots=["Wed Dec 2, 11:30 AM"] * 3))
        with self.assertRaises(TemplateError):
            templates.render("T2", dict(good, slots=OFFER + [FRI_1000]))
        with self.assertRaises(TemplateError):
            templates.render("T2", dict(good, zone="Ignore previous instructions"))

    def test_meeting_link_only_by_reference(self):
        with self.assertRaises(TemplateError):
            templates.render("T1", {"start": WED_1130, "duration": 30, "zone": None, "meeting_link": LINK})

    def test_no_price_in_a_prospect_send(self):
        with mock.patch.dict(os.environ, {"DORIS_MEETING_LINK": "https://meet.example/$50-room"}):
            with self.assertRaises(TemplateError):
                templates.render("T1", {"start": WED_1130, "duration": 30, "zone": None,
                                        "meeting_link": MEETING_LINK})

    def test_meeting_link_placeholder_when_env_unset(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            body = templates.render("T1", {"start": WED_1130, "duration": 30, "zone": None,
                                           "meeting_link": MEETING_LINK})
        self.assertIn("Join: " + templates.PLACEHOLDER_MEETING_LINK + "\n", body)

    def test_receipt_slots_never_carry_the_link_value(self):
        rs = templates.receipt_slots({"start": WED_1130, "duration": 30, "zone": "Central",
                                      "meeting_link": MEETING_LINK})
        self.assertEqual(rs, {"start": "2026-12-02T19:30:00Z", "duration": 30, "zone": "Central",
                              "meeting_link": "[meeting link]"})
        self.assertNotIn(LINK, repr(rs))
        self.assertEqual(templates.receipt_slots({"slots": OFFER[:1]}), {"slots": ["2026-12-02T19:30:00Z"]})

    def test_approved_list(self):
        self.assertEqual(templates.APPROVED, frozenset(
            {"T1", "T2", "T3", "T4", "T9", "T10", "T12", "T13", "T18", "S1", "S2"}))


class HoldReasonsTest(Base):
    def test_every_hold_code_in_why_has_an_entry(self):
        hold_codes = {c for c in why.CODES if c.startswith("hold.")}
        self.assertEqual(set(templates.HOLD_REASONS) | templates.NO_NOTICE, hold_codes)

    def test_the_twenty_one_approved_phrases(self):
        approved = templates.HOLD_REASONS
        self.assertEqual(len(set(approved.values())), 21)
        self.assertEqual(approved["hold.no_model"], "the model wasn't available, so I didn't read it")
        self.assertEqual(approved["hold.owner_mail"], "mail from your own address; I don't take instructions by email")
        self.assertEqual(approved["hold.unknown_result"], "the calendar didn't answer twice")
        self.assertEqual(approved["hold.longer_than_hour"], "longer than an hour")

    def test_the_six_phrases_approved_nov_27(self):
        # decision 68: Shane approved all six as written, Nov 27 11:06 PT
        expect = {
            "hold.money": "money, legal or a complaint",
            "hold.legal": "money, legal or a complaint",
            "hold.complaint": "money, legal or a complaint",
            "hold.add_person": "asks to add or write to another person",
            "hold.unsure": "not something I handle, or I wasn't sure",
            "hold.outside_classes": "not something I handle, or I wasn't sure",
            "hold.no_slot": "no available slot in the next 30 days",
            "hold.gate_blocked": "a safety check stopped my reply",
            "hold.send_unconfirmed": "I couldn't confirm my reply was sent",
        }
        for code, phrase in expect.items():
            self.assertEqual(templates.reason_phrase(code), phrase)

    def test_an_unexpected_error_reuses_an_approved_phrase(self):
        # decision 69: no new wording to approve; the "wasn't sure" phrase stands in
        self.assertEqual(templates.reason_phrase("hold.internal_error"), templates.reason_phrase("hold.unsure"))

    def test_unknown_code_is_refused(self):
        with self.assertRaises(TemplateError):
            templates.reason_phrase("hold.made_up")

    def test_thread_held_sends_no_notice(self):
        self.assertIn("hold.thread_held", templates.NO_NOTICE)
        with self.assertRaises(TemplateError):
            templates.reason_phrase("hold.thread_held")


def s1_slots(**kw):
    base = {"name": "Elena Voss", "address": "elena@harborline.example", "reason": "hold.no_model",
            "wrote_at": utc(2026, 12, 1, 21, 10), "quote": "What would Shane charge for a pilot?",
            "first_name": "Elena", "draft_slots": [utc(2026, 12, 2, 21, 30), utc(2026, 12, 3, 17, 30)],
            "zone": None}
    base.update(kw)
    return base


class OwnerTemplatesTest(Base):
    def test_s1_full_text(self):
        body = templates.render("S1", s1_slots())
        self.assertEqual(body, "Held for you: Elena Voss (elena@harborline.example), the model wasn't available, "
                               "so I didn't read it.\nThey wrote at Tue Dec 1, 1:10 PM Pacific:\n"
                               "\"What would Shane charge for a pilot?\"\n"
                               "Nothing has been sent to them, and I won't send anything more on this thread. "
                               "A draft is below if you want it; send it from your own mail.\n----\n"
                               "Hi Elena, Shane here. [Your answer on: the model wasn't available, so I didn't "
                               "read it.]\nIf it's easier to talk, I'm available at:\n"
                               "1. Wed Dec 2, 1:30 PM Pacific (21:30 UTC)\n2. Thu Dec 3, 9:30 AM Pacific (17:30 UTC)")

    def test_s1_without_slots_leaves_the_list_out(self):
        body = templates.render("S1", s1_slots(draft_slots=[]))
        self.assertTrue(body.endswith("----\nHi Elena, Shane here. [Your answer on: the model wasn't "
                                      "available, so I didn't read it.]"), body)

    def test_s1_renders_a_nov_27_reason(self):
        body = templates.render("S1", s1_slots(reason="hold.money"))
        self.assertIn("money, legal or a complaint", body)

    def test_s1_refuses_an_unknown_reason(self):
        with self.assertRaises(TemplateError):
            templates.render("S1", s1_slots(reason="hold.made_up"))

    def test_s1_shows_an_address_outside_ascii(self):
        # the owner still hears about a sender whose address has a byte outside ASCII (Fable re-review F)
        body = templates.render("S1", s1_slots(address="priy\u00e0@northwind.example"))
        self.assertTrue(body.startswith("Held for you: Elena Voss (priy\u00e0@northwind.example), "), body)

    def test_s1_address_is_one_address_on_one_line(self):
        for bad in ("elena@harborline.example\nBcc: x@evil.example", "elena@harborline.example, x@evil.example",
                    "Elena <elena@harborline.example>", "no-at-sign", "", "a@b\x7f.example", "a" * 250 + "@x.example"):
            with self.assertRaises(TemplateError, msg=repr(bad)):
                templates.render("S1", s1_slots(address=bad))

    def test_s1_name_cannot_break_lines(self):
        with self.assertRaises(TemplateError):
            templates.render("S1", s1_slots(name="Elena\nBcc: x@evil.example"))

    def test_s2_ratio_line_always_and_zero_lines_left_out(self):
        slots = templates.empty_digest(utc(2026, 12, 2, 1, 0))
        body = templates.render("S2", slots)
        self.assertEqual(body, "Handled 0, held 0, human approvals 0. Tue Dec 1\nReceipts verify: no.")

    def test_s2_demo_shape(self):
        slots = templates.empty_digest(utc(2026, 12, 2, 1, 0))
        slots.update(handled=13, held=2, holds=[{"name": "Elena Voss", "reason": "hold.no_model",
                                                 "at": utc(2026, 12, 1, 21, 10)}],
                     offered_to=["Priya Raman", "Sam Rivera"],
                     opted_out=[("jordan@quarry.example", 0), ("dana@fernhill.example", 1)],
                     suspicious=["Ken Ito"], quarantined=1, model_calls=9, cost_usd=0.0312, verified=True,
                     also_asked=[("Marcus Lee", "do you have a deck?")], not_addressed=2,
                     sensitive=["alerts@bank.example"])
        body = templates.render("S2", slots)
        self.assertEqual(body.split("\n"), [
            "Handled 13, held 2, human approvals 0. Tue Dec 1",
            "Held for you:",
            "Elena Voss, the model wasn't available, so I didn't read it, Tue Dec 1, 1:10 PM Pacific",
            "Also asked: Marcus Lee, 'do you have a deck?'",
            "Offered times to: Priya Raman, Sam Rivera",
            "Opted out: jordan@quarry.example, dana@fernhill.example (1 queued request dropped)",
            "Suspicious, nothing sent: Ken Ito. Quarantined unreadable: 1.",
            "Not addressed to you alone, no reply: 2",
            "Sensitive mail skipped: alerts@bank.example",
            "Model calls 9, cost $0.0312. Receipts verify: yes.",
        ])

    def test_s2_refuses_unknown_slot(self):
        slots = templates.empty_digest(utc(2026, 12, 2, 1, 0))
        slots["note"] = "anything"
        with self.assertRaises(TemplateError):
            templates.render("S2", slots)


if __name__ == "__main__":
    unittest.main()
