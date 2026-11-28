"""A time the sender wrote may start on any quarter hour (ruling 3, G9), all the way through.

She offers their own quarter-hour times back first, and a pick of one books it at exactly that
time. Only times the sender wrote count: a weekday written with one option never moves another
option's time to its day (D5), so she never offers a quarter hour nobody asked for.
"""

import datetime as dt
import os
import unittest

from doris import ledger as ledger_mod
from doris import receipts
from doris.adapters import sandbox as sandbox_mod
from doris.shapes import Slot
from tests.unit.test_restart import OWNER, Restart, add, expire_offers, owner_busy

UTC = dt.timezone.utc


def at(*args):
    return dt.datetime(*args, tzinfo=UTC)


def request(sender, key, text):
    name, addr = sender
    return ("From: %s <%s>\r\n"
            "To: Shane Bornstein <whizz@fastmail.com>\r\n"
            "Subject: Call next week?\r\n"
            "Date: Tue, 01 Dec 2026 08:30:00 -0800\r\n"
            "Message-ID: <%s>\r\n"
            "MIME-Version: 1.0\r\n"
            "Content-Type: text/plain; charset=\"utf-8\"\r\n"
            "Content-Transfer-Encoding: 7bit\r\n\r\n%s\r\n\r\n%s\r\n"
            % (name, addr, key, text, name.split()[0])).encode("ascii")


LENA = ("Lena Park", "lena@harborline.example")
LENA_KEY = "20261201.0830.lena.call@harborline.example"


def pick(offer_id, text, sign=True):
    return (b"From: Lena Park <lena@harborline.example>\r\n"
            b"To: Doris <doris.htm@fastmail.com>\r\n"
            b"Subject: Re: Call next week?\r\n"
            b"Date: Tue, 01 Dec 2026 09:30:00 -0800\r\n"
            b"Message-ID: <20261201.0930.lena.pick@harborline.example>\r\n"
            b"In-Reply-To: " + offer_id.encode("ascii") + b"\r\n"
            b"References: <" + LENA_KEY.encode("ascii") + b"> " + offer_id.encode("ascii") + b"\r\n"
            b"MIME-Version: 1.0\r\n"
            b"Content-Type: text/plain; charset=\"utf-8\"\r\n"
            b"Content-Transfer-Encoding: 7bit\r\n\r\n" + text + (b"\r\n\r\nLena\r\n" if sign else b"\r\n"))


class Written(Restart):
    names = ()
    key = LENA_KEY
    sender = LENA
    text = ""

    def setUp(self):
        super().setUp()
        add(self.root, "20-request.eml", request(self.sender, self.key, self.text), {"forwarded": "yes"})

    def offered(self):
        led = ledger_mod.Ledger(os.path.join(self.state, "ledger.sqlite"))
        try:
            (offer,) = led.open_offers("<%s>" % self.key, at(2026, 12, 1, 17, 0))
            return offer["slots"]
        finally:
            led.close()

    def off_her_grid(self, slots):
        """Offered starts that are not on the hour or half hour, Pacific (UTC-8 in December)."""
        return [s.start_utc for s in slots if (s.start_utc - dt.timedelta(hours=8)).minute not in (0, 30)]


class QuarterHourTimes(Written):
    # both written times are free on the sandbox calendar: Thu Dec 3 2:15 PM and Fri Dec 4 10:45 AM Pacific
    text = ("Hi, could Shane do Thursday, December 3 at 2:15 PM Pacific or Friday, December 4 at 10:45 AM "
            "Pacific for 30 minutes?")

    def test_her_offer_leads_with_exactly_the_quarter_hour_times_the_sender_wrote(self):
        s = self.run_once()
        self.assertEqual(self.templates_sent(LENA[1]), ["T2"])
        self.assertEqual(self.closing(s, "lena"), ["offer"])
        slots = self.offered()
        self.assertEqual(slots[:2], [Slot(at(2026, 12, 3, 22, 15), at(2026, 12, 3, 22, 45)),
                                     Slot(at(2026, 12, 4, 18, 45), at(2026, 12, 4, 19, 15))])
        self.assertEqual(self.off_her_grid(slots[2:]), [])
        self.assertEqual([t for t in self.templates_sent(OWNER) if t == "S1"], [])

    def test_a_pick_of_her_quarter_hour_time_books_exactly_that_time(self):
        self.run_once()
        (offer,) = [m for t, m in self.sent(LENA[1]) if t == "T2"]
        add(self.root, "21-lena-pick.eml", pick(offer["Message-ID"], b"The first one works."), {"forwarded": "no"})
        s = self.run_once()
        self.assertEqual(self.templates_sent(LENA[1]), ["T2", "T1"])
        self.assertEqual(self.closing(s, "lena"), ["book"])
        (event,) = self.events()
        with open(os.path.join(self.root, "calendar", event), "rb") as fh:
            ics = fh.read().decode("ascii")
        self.assertIn("DTSTART:20261203T221500Z", ics)
        self.assertIn("DTEND:20261203T224500Z", ics)


class ResendTheOpenOffer(Written):
    """A reply that names no one offered slot gets the same offer again (T12, ruling 6), her quarter
    hours included: the gate checks it at the open offer's length (Fable review of PR #3, finding 5)."""
    text = QuarterHourTimes.text

    def answer(self, text, sign=True):
        self.run_once()
        offer = self.offered()
        (t2,) = [m for t, m in self.sent(LENA[1]) if t == "T2"]
        add(self.root, "21-lena-reply.eml", pick(t2["Message-ID"], text, sign), {"forwarded": "no"})
        return offer, self.run_once()

    def assert_resent(self, offer, s, rule):
        self.assertEqual(self.templates_sent(LENA[1]), ["T2", "T12"])
        self.assertEqual(self.closing(s, "lena"), ["offer"])
        self.assertEqual(self.events(), [])
        self.assertEqual(self.offered(), offer)                 # the same offer, still open: no new record
        lines = receipts.read_lines(s.receipts_path)
        (close,) = [l for l in lines if l["kind"] == "decided" and "outcome" in l["payload"]]
        self.assertEqual(close["why"]["text"], "The reply does not match exactly one offered slot (%s): "
                                               "send the same offer again." % rule)
        (send,) = [l["payload"] for l in lines if l["kind"] == "called" and l["payload"]["call"] == "send"]
        self.assertEqual(send["slots"]["slots"], [receipts.iso(sl.start_utc) for sl in offer])
        # her quarter hours stay times the sender wrote: the open offer holds them (G9)
        (checked,) = [l["payload"] for l in lines if l["kind"] == "checked" and l["payload"]["template"] == "T12"]
        self.assertEqual(checked["checks"]["G9"], {"status": "pass", "checked": 3, "written": True})
        (t12,) = [m for t, m in self.sent(LENA[1]) if t == "T12"]
        lines = t12.get_body(("plain",)).get_content().splitlines()
        self.assertEqual(lines[1:3], ["1. Thu Dec 3, 22:15 UTC (2:15 PM Pacific)",
                                      "2. Fri Dec 4, 18:45 UTC (10:45 AM Pacific)"])

    def test_yes_to_her_three_slot_offer_gets_the_same_offer_again(self):
        offer, s = self.answer(b"Yes", sign=False)
        self.assert_resent(offer, s, "multiple")

    def test_a_near_miss_gets_the_same_offer_again(self):
        offer, s = self.answer(b"Thursday at 4:45 PM Pacific works for me.")
        self.assert_resent(offer, s, "near_miss")


class AnExpiredQuarterHourPick(Written):
    """Her pick of a quarter hour is killed before the write, and the offer expires before the restart:
    no open offer holds it now, so she offers fresh times (T4) because nothing offers it, the gate's own
    test, not because it is off the half hour (Fable review of PR #3, finding 6)."""
    text = QuarterHourTimes.text

    def test_the_restart_offers_fresh_times_because_no_open_offer_holds_it(self):
        self.run_once()
        (t2,) = [m for t, m in self.sent(LENA[1]) if t == "T2"]
        add(self.root, "21-lena-pick.eml", pick(t2["Message-ID"], b"The first one works."), {"forwarded": "no"})
        s = self.crash_then_run(sandbox_mod.SandboxCalendar, "create_only", lambda *a, **k: True,
                                between=lambda: expire_offers(self.state))
        self.assertEqual(self.templates_sent(LENA[1]), ["T2", "T4"])
        self.assertEqual(self.closing(s, "lena.pick"), ["reoffer"])
        self.assertEqual(self.events(), [])
        lines = receipts.read_lines(s.receipts_path)
        (close,) = [l for l in lines if l["kind"] == "decided" and l["payload"].get("outcome") == "reoffer"]
        self.assertEqual(close["why"]["text"],
                         "The picked slot is no longer bookable or available (not_offered): offer fresh times.")
        (send,) = [l["payload"] for l in lines if l["kind"] == "called" and l["payload"]["call"] == "send"]
        self.assertEqual(send["slots"]["asked"], "2026-12-03T22:15:00Z")


BOB = ("Bob Stone", "bob@elsewhere.example")


def to_the_owner_on_her_thread(sender, mid, text):
    """A reply to the owner alone that names Lena's request: it lands on her thread (ruling 18, branch 1)."""
    name, addr = sender
    return ("From: %s <%s>\r\n"
            "To: Shane Bornstein <whizz@fastmail.com>\r\n"
            "Subject: Re: Call next week?\r\n"
            "Date: Tue, 01 Dec 2026 09:30:00 -0800\r\n"
            "Message-ID: <%s>\r\n"
            "In-Reply-To: <%s>\r\n"
            "References: <%s>\r\n"
            "MIME-Version: 1.0\r\n"
            "Content-Type: text/plain; charset=\"utf-8\"\r\n"
            "Content-Transfer-Encoding: 7bit\r\n\r\n%s\r\n\r\n%s\r\n"
            % (name, addr, mid, LENA_KEY, LENA_KEY, text, name.split()[0])).encode("ascii")


class AnOfferIsOneSenders(Written):
    """Same sender, same thread (ruling 6; Fable review of PR #3, finding 3): someone else who names
    Lena's request and picks "the first one" never books the quarter hour she offered Lena. Nor does he
    get an offer of his own on her thread, which would lock her out of it: with an open offer there to
    Lena and none to him, he is held for the owner (Fable review 2 of PR #3, finding C)."""
    text = QuarterHourTimes.text

    def offer_rows(self):
        led = ledger_mod.Ledger(os.path.join(self.state, "ledger.sqlite"))
        try:
            return [(r["sender"], r["closed_reason"]) for r in led._all("SELECT sender, closed_reason FROM offers")]
        finally:
            led.close()

    def bob_picks(self):
        self.run_once()
        add(self.root, "21-bob-pick.eml",
            to_the_owner_on_her_thread(BOB, "20261201.0930.bob.pick@elsewhere.example", "The first one works."),
            {"forwarded": "yes"})
        return self.run_once()

    def test_a_pick_from_another_address_never_books_her_offer(self):
        s = self.bob_picks()
        self.assertEqual(self.events(), [])
        self.assertEqual(self.templates_sent(BOB[1]), [])
        self.assertEqual(self.closing(s, "bob"), ["hold:new_address"])
        self.assertEqual(self.templates_sent(OWNER), ["S1"])
        self.assertEqual(self.templates_sent(LENA[1]), ["T2"])
        # a hold keeps the whole thread the owner's (unchanged): her offer closes with it
        self.assertEqual(self.offer_rows(), [(LENA[1], "held")])

    def test_her_pick_after_that_stays_with_the_owner(self):
        # Holds are per thread, so Lena's thread is the owner's from Bob's hold on: her pick books nothing and
        # gets no reply and no second notice; the owner has Bob's notice and the digest lists the thread
        self.bob_picks()
        (t2,) = [m for t, m in self.sent(LENA[1]) if t == "T2"]
        add(self.root, "22-lena-pick.eml", pick(t2["Message-ID"], b"The first one works."), {"forwarded": "no"})
        s = self.run_once()
        self.assertEqual(self.closing(s, "lena.pick"), ["hold:thread_held"])
        self.assertEqual(self.events(), [])
        self.assertEqual(self.templates_sent(LENA[1]), ["T2"])
        self.assertEqual(self.templates_sent(OWNER), ["S1"])


class OnlyTimesTheSenderWrote(Written):
    # both written times are busy; Thursday's 10:15 on Friday and Friday's 2:45 on Thursday are free
    text = ("Hi, could Shane do Thursday, December 3 at 10:15 AM Pacific or Friday, December 4 at 2:45 PM "
            "Pacific for 30 minutes?")

    def test_she_never_offers_one_options_time_on_another_options_day(self):
        self.run_once()
        self.assertEqual(self.templates_sent(LENA[1]), ["T2"])
        slots = self.offered()
        self.assertEqual(self.off_her_grid(slots), [])
        self.assertNotIn(at(2026, 12, 4, 18, 15), [s.start_utc for s in slots])
        self.assertNotIn(at(2026, 12, 3, 22, 45), [s.start_utc for s in slots])


class AWeekdayWithItsOwnDate(Written):
    key = "20261201.0830.omar.call@tidewater.example"
    sender = ("Omar Diaz", "omar@tidewater.example")
    text = "I am out Monday December 7. Could Shane do Wednesday, December 9 at 3:15 PM Pacific?"

    def test_the_day_she_is_told_is_out_never_gets_the_written_time(self):
        self.run_once()
        self.assertEqual(self.templates_sent(self.sender[1]), ["T2"])
        slots = self.offered()
        self.assertEqual(slots[0], Slot(at(2026, 12, 9, 23, 15), at(2026, 12, 9, 23, 45)))
        self.assertNotIn(at(2026, 12, 7, 23, 15), [s.start_utc for s in slots])


def answer_her(sender, key, offer_id, mid, text):
    """The sender answers her message offer_id on their thread."""
    name, addr = sender
    return ("From: %s <%s>\r\n"
            "To: Doris <doris.htm@fastmail.com>\r\n"
            "Subject: Re: Call next week?\r\n"
            "Date: Tue, 01 Dec 2026 09:30:00 -0800\r\n"
            "Message-ID: <%s>\r\n"
            "In-Reply-To: %s\r\n"
            "References: <%s> %s\r\n"
            "MIME-Version: 1.0\r\n"
            "Content-Type: text/plain; charset=\"utf-8\"\r\n"
            "Content-Transfer-Encoding: 7bit\r\n\r\n%s\r\n\r\n%s\r\n"
            % (name, addr, mid, offer_id, key, offer_id, text, name.split()[0])).encode("ascii")


class AWeekdayWrittenOnItsOwn(Written):
    """A time counts as written only for the day it was written with (Fable review 2 of PR #3, finding
    A): "I am out Monday" never gets Wednesday's 3:15 PM, offered or booked. The calendar is empty but
    for Wed Dec 9, 3:00 to 4:00 PM Pacific."""
    key = "20261201.0830.omar.call@tidewater.example"
    sender = ("Omar Diaz", "omar@tidewater.example")
    text = "I am out Monday. Could Shane do Wednesday, December 9 at 3:15 PM Pacific?"

    def setUp(self):
        super().setUp()
        cal = os.path.join(self.root, "calendar")
        for name in os.listdir(cal):
            os.remove(os.path.join(cal, name))
        owner_busy(self.root, "owner-wed", "20261209T230000Z", "20261210T000000Z")

    def test_monday_never_gets_the_written_quarter_hour_offered_or_booked(self):
        s = self.run_once()
        self.assertEqual(self.templates_sent(self.sender[1]), ["T2"])
        # Wednesday's 3:15 PM is busy: the day he named fills the offer, on her grid
        self.assertEqual([sl.start_utc for sl in self.offered()],
                         [at(2026, 12, 9, 17, 0), at(2026, 12, 9, 17, 30), at(2026, 12, 9, 18, 0)])
        lines = receipts.read_lines(s.receipts_path)
        (checked,) = [l["payload"] for l in lines if l["kind"] == "checked" and l["payload"]["template"] == "T2"]
        self.assertEqual(checked["checks"]["G9"], {"status": "pass", "checked": 3, "written": False})
        (t2,) = [m for t, m in self.sent(self.sender[1]) if t == "T2"]
        add(self.root, "21-omar-pick.eml",
            answer_her(self.sender, self.key, t2["Message-ID"], "20261201.0930.omar.pick@tidewater.example",
                       "The first one works."), {"forwarded": "no"})
        s = self.run_once()
        self.assertEqual(self.templates_sent(self.sender[1]), ["T2", "T1"])
        self.assertEqual(self.closing(s, "omar.pick"), ["book"])
        (event,) = self.events()
        with open(os.path.join(self.root, "calendar", event), "rb") as fh:
            ics = fh.read().decode("ascii")
        self.assertIn("DTSTART:20261209T170000Z", ics)
        self.assertNotIn("20261207T231500Z", ics)


class FourWeekdaysWrittenOnTheirOwn(Restart):
    """The review's four mails on an empty calendar (finding A): each offer leads with the time the sender
    wrote, on the day they wrote it with, and a weekday written on its own never gets that quarter hour."""
    MAILS = (
        (("Omar Diaz", "omar@tidewater.example"), "20261201.0830.omar.call@tidewater.example",
         "I am out Monday. Could Shane do Wednesday, December 9 at 3:15 PM Pacific?",
         [at(2026, 12, 9, 23, 15), at(2026, 12, 9, 17, 0), at(2026, 12, 9, 17, 30)]),
        (("Ruth Okafor", "ruth@fernhill.example"), "20261201.0831.ruth.call@fernhill.example",
         "I can't do Thursday. Friday at 10:15 AM Pacific works.",
         [at(2026, 12, 4, 18, 15), at(2026, 12, 4, 17, 0), at(2026, 12, 4, 17, 30)]),
        (("Theo Grant", "theo@quarrylane.example"), "20261201.0832.theo.call@quarrylane.example",
         "Tuesday is bad, Wednesday December 9 at 3:15 PM Pacific or Thursday at 9:45 AM Pacific.",
         [at(2026, 12, 9, 23, 15), at(2026, 12, 3, 17, 45), at(2026, 12, 9, 17, 0)]),
        (("Ines Moreau", "ines@saltmarsh.example"), "20261201.0833.ines.call@saltmarsh.example",
         "I am travelling Thursday and Friday. Monday December 7 at 1:15 PM Pacific?",
         [at(2026, 12, 7, 21, 15), at(2026, 12, 7, 17, 0), at(2026, 12, 7, 17, 30)]),
    )

    def setUp(self):
        super().setUp()
        cal = os.path.join(self.root, "calendar")
        for name in os.listdir(cal):
            os.remove(os.path.join(cal, name))
        for i, (sender, key, text, _) in enumerate(self.MAILS):
            add(self.root, "2%d-request.eml" % i, request(sender, key, text), {"forwarded": "yes"})

    def test_each_offer_leads_with_the_senders_own_time_and_no_other_day_gets_it(self):
        self.run_once()
        led = ledger_mod.Ledger(os.path.join(self.state, "ledger.sqlite"))
        try:
            for sender, key, text, expected in self.MAILS:
                with self.subTest(text=text):
                    self.assertEqual(self.templates_sent(sender[1]), ["T2"])
                    (offer,) = led.open_offers("<%s>" % key, at(2026, 12, 1, 17, 0))
                    self.assertEqual([s.start_utc for s in offer["slots"]], expected)
        finally:
            led.close()
        self.assertEqual(self.events(), [])


if __name__ == "__main__":
    unittest.main()
