"""A time the sender wrote may start on any quarter hour (ruling 3, G9), all the way through.

She offers their own quarter-hour times back first, and a pick of one books it at exactly that
time. Only times the sender wrote count: a weekday written with one option never moves another
option's time to its day (D5), so she never offers a quarter hour nobody asked for.
"""

import datetime as dt
import os
import unittest

from doris import ledger as ledger_mod
from doris.shapes import Slot
from tests.unit.test_restart import OWNER, Restart, add

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


def pick(offer_id, text):
    return (b"From: Lena Park <lena@harborline.example>\r\n"
            b"To: Doris <doris.htm@fastmail.com>\r\n"
            b"Subject: Re: Call next week?\r\n"
            b"Date: Tue, 01 Dec 2026 09:30:00 -0800\r\n"
            b"Message-ID: <20261201.0930.lena.pick@harborline.example>\r\n"
            b"In-Reply-To: " + offer_id.encode("ascii") + b"\r\n"
            b"References: <" + LENA_KEY.encode("ascii") + b"> " + offer_id.encode("ascii") + b"\r\n"
            b"MIME-Version: 1.0\r\n"
            b"Content-Type: text/plain; charset=\"utf-8\"\r\n"
            b"Content-Transfer-Encoding: 7bit\r\n\r\n" + text + b"\r\n\r\nLena\r\n")


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
    Lena's request and picks "the first one" never books the quarter hour she offered Lena."""
    text = QuarterHourTimes.text

    def open_offers(self):
        led = ledger_mod.Ledger(os.path.join(self.state, "ledger.sqlite"))
        try:
            return led.open_offers("<%s>" % self.key, at(2026, 12, 1, 17, 0))
        finally:
            led.close()

    def test_a_pick_from_another_address_never_books_her_offer(self):
        self.run_once()
        add(self.root, "21-bob-pick.eml",
            to_the_owner_on_her_thread(BOB, "20261201.0930.bob.pick@elsewhere.example", "The first one works."),
            {"forwarded": "yes"})
        s = self.run_once()
        self.assertEqual(self.events(), [])
        # nothing was offered to Bob, so "the first one" is a new request of his own: his own times
        self.assertEqual(self.templates_sent(BOB[1]), ["T2"])
        self.assertEqual(self.closing(s, "bob"), ["offer"])
        lena, bob = self.open_offers()
        self.assertEqual((lena["sender"], bob["sender"]), (LENA[1], BOB[1]))
        self.assertEqual(lena["slots"][:2], [Slot(at(2026, 12, 3, 22, 15), at(2026, 12, 3, 22, 45)),
                                             Slot(at(2026, 12, 4, 18, 45), at(2026, 12, 4, 19, 15))])
        self.assertEqual(self.off_her_grid(bob["slots"]), [])
        self.assertEqual(self.templates_sent(LENA[1]), ["T2"])


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


if __name__ == "__main__":
    unittest.main()
