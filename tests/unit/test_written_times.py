"""A time the sender wrote may start on any quarter hour (ruling 3, G9), all the way through.

She offers their own quarter-hour times back first, and a pick of one books it at that time. A pick
books only what an open offer on the thread holds.
"""

import datetime as dt
import os
import unittest

from doris import ledger as ledger_mod
from tests.unit.test_restart import OWNER, Restart, add

LENA = "lena@harborline.example"
REQUEST = (b"From: Lena Park <lena@harborline.example>\r\n"
           b"To: Shane Bornstein <whizz@fastmail.com>\r\n"
           b"Subject: Call next week?\r\n"
           b"Date: Tue, 01 Dec 2026 08:30:00 -0800\r\n"
           b"Message-ID: <20261201.0830.lena.call@harborline.example>\r\n"
           b"MIME-Version: 1.0\r\n"
           b"Content-Type: text/plain; charset=\"utf-8\"\r\n"
           b"Content-Transfer-Encoding: 7bit\r\n\r\n"
           b"Hi, could Shane do Thursday, December 3 at 10:15 AM Pacific or Friday, December 4 at 2:45 PM "
           b"Pacific for 30 minutes?\r\n\r\nLena\r\n")


def pick(offer_id, text):
    return (b"From: Lena Park <lena@harborline.example>\r\n"
            b"To: Doris <doris.htm@fastmail.com>\r\n"
            b"Subject: Re: Call next week?\r\n"
            b"Date: Tue, 01 Dec 2026 09:30:00 -0800\r\n"
            b"Message-ID: <20261201.0930.lena.pick@harborline.example>\r\n"
            b"In-Reply-To: " + offer_id.encode("ascii") + b"\r\n"
            b"References: <20261201.0830.lena.call@harborline.example> " + offer_id.encode("ascii") + b"\r\n"
            b"MIME-Version: 1.0\r\n"
            b"Content-Type: text/plain; charset=\"utf-8\"\r\n"
            b"Content-Transfer-Encoding: 7bit\r\n\r\n" + text + b"\r\n\r\nLena\r\n")


class QuarterHourTimes(Restart):
    names = ()

    def setUp(self):
        super().setUp()
        add(self.root, "20-lena-request.eml", REQUEST, {"forwarded": "yes"})

    def offer(self):
        (msg,) = [m for t, m in self.sent(LENA) if t == "T2"]
        return msg

    def offered_slots(self):
        led = ledger_mod.Ledger(os.path.join(self.state, "ledger.sqlite"))
        try:
            (offer,) = led.open_offers("<20261201.0830.lena.call@harborline.example>",
                                       dt.datetime(2026, 12, 1, 17, 0, tzinfo=dt.timezone.utc))
            return offer["slots"]
        finally:
            led.close()

    def test_her_offer_leads_with_the_quarter_hour_times_the_sender_wrote(self):
        s = self.run_once()
        self.assertEqual(self.templates_sent(LENA), ["T2"])
        self.assertEqual(self.closing(s, "lena"), ["offer"])
        body = self.offer().get_body(("plain",)).get_content()
        self.assertLess(body.index("10:15"), body.index("2:45"), body)
        self.assertEqual([t for t in self.templates_sent(OWNER) if t == "S1"], [])

    def test_a_pick_of_her_quarter_hour_time_books_it_at_that_time(self):
        self.run_once()
        first = self.offered_slots()[0]
        self.assertIn(first.start_utc.minute, (15, 45))      # one of the sender's own quarter-hour times
        add(self.root, "21-lena-pick.eml", pick(self.offer()["Message-ID"], b"The first one works."),
            {"forwarded": "no"})
        s = self.run_once()
        self.assertEqual(self.templates_sent(LENA), ["T2", "T1"])
        self.assertEqual(self.closing(s, "lena"), ["book"])
        (event,) = self.events()
        with open(os.path.join(self.root, "calendar", event), "rb") as fh:
            ics = fh.read().decode("ascii")
        self.assertIn("DTSTART:" + first.start_utc.strftime("%Y%m%dT%H%M%SZ"), ics)
        self.assertIn("DTEND:" + first.end_utc.strftime("%Y%m%dT%H%M%SZ"), ics)


if __name__ == "__main__":
    unittest.main()
