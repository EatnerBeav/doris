"""A crash or a stop between two steps, then a restart on the same ledger (ruling 8, A3).

Each test kills run 1 at one point with an exception the loop cannot catch, then runs again on
the same state and sandbox, and checks what the prospect and the calendar end up with: nothing
twice, nothing booked again, nothing moved that the sender never asked to move.
"""

import datetime
import email
import email.policy
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from doris import ledger as ledger_mod
from doris import loop, paths, receipts, supervisor
from doris.adapters import sandbox as sandbox_mod
from doris.shapes import MailError

OWNER = "whizz@fastmail.com"
DORIS = "doris.htm@fastmail.com"


class Crash(BaseException):
    """A kill at this exact point: nothing in the loop catches it."""


def mini_sandbox(tmp, names):
    """A copy of the demo sandbox holding only these inbox files (and their facts)."""
    root = os.path.join(tmp, "sandbox")
    shutil.copytree(paths.SANDBOX, root, ignore=shutil.ignore_patterns("doris-*.ics", "staged-*.ics", "*.eml.sent"))
    inbox, outbox = os.path.join(root, "inbox"), os.path.join(root, "outbox")
    for name in os.listdir(inbox):
        if name.endswith(".eml") and name not in names:
            os.remove(os.path.join(inbox, name))
    with open(os.path.join(inbox, "facts.json"), encoding="utf-8") as fh:
        facts = json.load(fh)
    with open(os.path.join(inbox, "facts.json"), "w", encoding="utf-8") as fh:
        json.dump({k: v for k, v in facts.items() if k in names}, fh)
    for name in os.listdir(outbox):
        if name != ".gitkeep":
            os.remove(os.path.join(outbox, name))
    return root


def edit(root, name, change):
    """Change one inbox file of a mini sandbox in place: change(raw bytes) -> raw bytes."""
    path = os.path.join(root, "inbox", name)
    with open(path, "rb") as fh:
        raw = fh.read()
    with open(path, "wb") as fh:
        fh.write(change(raw))


def add(root, name, raw, facts):
    """One more inbox file for a mini sandbox, with its facts."""
    inbox = os.path.join(root, "inbox")
    with open(os.path.join(inbox, name), "wb") as fh:
        fh.write(raw)
    with open(os.path.join(inbox, "facts.json"), encoding="utf-8") as fh:
        known = json.load(fh)
    known[name] = facts
    with open(os.path.join(inbox, "facts.json"), "w", encoding="utf-8") as fh:
        json.dump(known, fh)


def cover_her_event(root):
    """The owner puts an event of his own on top of her booking (one booking in the folder)."""
    cal = os.path.join(root, "calendar")
    (name,) = [n for n in os.listdir(cal) if n.startswith("doris-")]
    with open(os.path.join(cal, name), "rb") as fh:
        lines = fh.read().decode("ascii").split("\r\n")
    start = next(l for l in lines if l.startswith("DTSTART")).split(":", 1)[1]
    end = next(l for l in lines if l.startswith("DTEND")).split(":", 1)[1]
    body = ("BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Team Whizz//Doris sandbox fixtures//EN",
            "BEGIN:VEVENT", "UID:owner-on-top", "DTSTAMP:20261127T180000Z", "DTSTART:" + start,
            "DTEND:" + end, "SUMMARY:Busy", "STATUS:CONFIRMED", "TRANSP:OPAQUE", "END:VEVENT", "END:VCALENDAR")
    with open(os.path.join(cal, "owner-on-top.ics"), "wb") as fh:
        fh.write(("\r\n".join(body) + "\r\n").encode("ascii"))


class Restart(unittest.TestCase):
    names = ()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="doris-restart-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = mini_sandbox(self.tmp, self.names)
        self.state = os.path.join(self.tmp, "state")
        self.runs = 0

    def run_once(self):
        self.runs += 1
        cfg = loop.RunConfig(mode="throwaway", run_name="run-%d" % self.runs, state_dir=self.state,
                             sandbox_root=self.root, owner_addr=OWNER, doris_addr=DORIS, model_source="stub",
                             clock_mode="advance", retry_wait_s=0)
        return loop.run(cfg)

    def crash_then_run(self, target, attr, when, between=None):
        """Run 1 dies inside target.attr the first time when(...) is true; between() runs, if given
        (the world changes while she is down); run 2 goes clean."""
        real = getattr(target, attr)
        fired = []

        def wrapped(*args, **kwargs):
            if not fired and when(*args, **kwargs):
                fired.append(True)
                raise Crash(attr)
            return real(*args, **kwargs)

        with mock.patch.object(target, attr, wrapped):
            with self.assertRaises(Crash):
                self.run_once()
        self.assertTrue(fired)
        if between is not None:
            between()
        return self.run_once()

    def sent(self, to=None):
        out = []
        outbox = os.path.join(self.root, "outbox")
        for name in sorted(os.listdir(outbox)):
            if name.endswith(".eml"):
                with open(os.path.join(outbox, name), "rb") as fh:
                    m = email.message_from_bytes(fh.read(), policy=email.policy.default)
                if to is None or to in str(m["To"]):
                    out.append((name.split("-", 1)[1][:-4], m))
        return out

    def templates_sent(self, to):
        return [t for t, _ in self.sent(to)]

    def events(self):
        cal = os.path.join(self.root, "calendar")
        return sorted(n for n in os.listdir(cal) if n.startswith("doris-"))

    def closing(self, summary, who):
        lines = receipts.read_lines(summary.receipts_path)
        return [l["payload"]["outcome"] for l in lines
                if l["kind"] in ("decided", "held") and "outcome" in l["payload"] and who in l["subject"]]

    def offers(self):
        led = ledger_mod.Ledger(os.path.join(self.state, "ledger.sqlite"))
        try:
            return led._all("SELECT COUNT(*) AS n FROM offers")[0]["n"]
        finally:
            led.close()


class DirectBooking(Restart):
    names = ("01-priya-request.eml",)
    PRIYA = "priya@northwind.example"

    def assert_booked_once(self, s2):
        self.assertEqual(self.templates_sent(self.PRIYA), ["T1"])
        self.assertEqual(len(self.events()), 1, self.events())
        self.assertEqual(self.closing(s2, "priya"), ["book"])

    def test_killed_after_the_write_before_the_confirmation(self):
        s2 = self.crash_then_run(loop._Run, "reply", lambda self_, e, f, template, *a, **k: template == "T1")
        self.assert_booked_once(s2)

    def test_killed_after_the_confirmation_before_the_closing_line(self):
        s2 = self.crash_then_run(loop._Run, "close", lambda self_, e, kind, outcome, *a, **k: outcome == "book")
        self.assert_booked_once(s2)

    def test_killed_before_the_write_landed(self):
        s2 = self.crash_then_run(sandbox_mod.SandboxCalendar, "create_only", lambda *a, **k: True)
        self.assert_booked_once(s2)

    def test_a_create_the_kill_switch_marked_unconfirmed(self):
        # the supervisor marks the call in flight unconfirmed; the next run looks it up first
        real = sandbox_mod.SandboxCalendar.create_only

        def write_then_die(cal, booking_id, ics):
            real(cal, booking_id, ics)
            raise Crash("create_only")

        with mock.patch.object(sandbox_mod.SandboxCalendar, "create_only", write_then_die):
            with self.assertRaises(Crash):
                self.run_once()
        led = ledger_mod.Ledger(os.path.join(self.state, "ledger.sqlite"))
        try:
            (b,) = led.bookings_for_thread(led.unprocessed_messages()[0]["thread_key"])
            led.set_booking_state(b["booking_id"], "unconfirmed")
        finally:
            led.close()
        self.assert_booked_once(self.run_once())

    def test_an_event_put_on_top_after_the_confirmation_went_out_leaves_hers(self):
        # T1 is in Sent, so the prospect was told: the restart never takes her booking off (Fable re-review B)
        s2 = self.crash_then_run(loop._Run, "close", lambda self_, e, kind, outcome, *a, **k: outcome == "book",
                                 between=lambda: cover_her_event(self.root))
        self.assert_booked_once(s2)

    def test_an_event_put_on_top_before_the_confirmation_went_out_is_the_collision_path(self):
        # ruling 8: no confirmation went out yet, so an overlap found on restart takes hers off
        s2 = self.crash_then_run(loop._Run, "reply", lambda self_, e, f, template, *a, **k: template == "T1",
                                 between=lambda: cover_her_event(self.root))
        self.assertEqual(self.templates_sent(self.PRIYA), ["T4"])
        self.assertEqual(self.events(), [])
        self.assertEqual(self.closing(s2, "priya"), ["offer"])


class NewerMailWhileDown(Restart):
    """A message a kill left part way, and a newer one from the same sender on its thread arrives
    before the restart: the first is finished before the newer one runs, never superseded with its
    booking left on the calendar unannounced."""
    names = ("01-priya-request.eml",)
    PRIYA = "priya@northwind.example"

    def test_a_booking_left_part_way_is_confirmed_before_the_newer_mail_moves_it(self):
        from tests.unit.test_failures import SameBatchFollowUp

        s2 = self.crash_then_run(loop._Run, "reply", lambda self_, e, f, template, *a, **k: template == "T1",
                                 between=lambda: add(self.root, "01b-priya-follow-up.eml",
                                                     SameBatchFollowUp.FOLLOW_UP, {"forwarded": "yes"}))
        self.assertEqual(self.templates_sent(self.PRIYA), ["T1", "T18"])
        self.assertEqual(self.closing(s2, "priya"), ["book", "move"])


class PickedOffer(Restart):
    """A pick from her offer books it; a kill before the confirmation or before the closing line,
    then the restart: T1 once and the same one event, never a fresh offer (Fable re-review A)."""
    names = ("02-marcus-request.eml", "07-marcus-pick.eml")
    MARCUS = "marcus@brightlane.example"

    def assert_booked_once(self, s2):
        self.assertEqual(self.templates_sent(self.MARCUS), ["T2", "T1"])
        self.assertEqual(len(self.events()), 1, self.events())
        self.assertEqual(self.offers(), 1)
        self.assertEqual(self.closing(s2, "marcus"), ["book"])

    def test_killed_after_the_write_before_the_confirmation(self):
        s2 = self.crash_then_run(loop._Run, "reply", lambda self_, e, f, template, *a, **k: template == "T1")
        self.assert_booked_once(s2)

    def test_killed_after_the_confirmation_before_the_closing_line(self):
        s2 = self.crash_then_run(loop._Run, "close", lambda self_, e, kind, outcome, *a, **k: outcome == "book")
        self.assert_booked_once(s2)


class HoldNotice(Restart):
    """A hold, then a kill before its notice went out or before the closing line: the owner is told
    once, for the reason the hold was made (Fable re-review E)."""
    names = ("11-elena-pricing.eml",)
    ELENA = "elena@harborline.example"

    def assert_told_once(self, s2):
        self.assertEqual([t for t in self.templates_sent(OWNER) if t == "S1"], ["S1"])
        self.assertEqual(self.sent(self.ELENA), [])
        self.assertEqual(self.closing(s2, "elena"), ["hold:money"])

    def test_killed_between_the_hold_and_its_notice(self):
        self.assert_told_once(self.crash_then_run(loop._Run, "notice", lambda *a, **k: True))

    def test_killed_after_the_notice_before_the_closing_line(self):
        self.assert_told_once(self.crash_then_run(
            loop._Run, "close", lambda self_, e, kind, outcome, *a, **k: outcome.startswith("hold:")))


class Offer(Restart):
    names = ("02-marcus-request.eml",)
    MARCUS = "marcus@brightlane.example"

    def test_killed_after_the_send_before_it_was_confirmed(self):
        s2 = self.crash_then_run(loop._Run, "look_in_sent", lambda *a, **k: True)
        self.assertEqual(self.templates_sent(self.MARCUS), ["T2"])
        self.assertEqual(self.offers(), 1)
        self.assertEqual(self.closing(s2, "marcus"), ["offer"])

    def test_killed_after_the_offer_was_written_down_before_the_send(self):
        s2 = self.crash_then_run(loop._Run, "deliver", lambda *a, **k: True)
        self.assertEqual(self.templates_sent(self.MARCUS), ["T2"])
        self.assertEqual(self.offers(), 1)
        self.assertEqual(self.closing(s2, "marcus"), ["offer"])

    def test_a_failed_sent_lookup_on_restart_never_sends_again(self):
        real = loop._Run.look_in_sent
        fired = []

        def die_once(*a, **k):
            if not fired:
                fired.append(True)
                raise Crash("look_in_sent")
            return real(*a, **k)

        with mock.patch.object(loop._Run, "look_in_sent", die_once):
            with self.assertRaises(Crash):
                self.run_once()

        def refused(*a, **k):
            raise MailError("lookup failed")

        with mock.patch.object(sandbox_mod.SandboxMail, "find_in_sent", refused):
            s2 = self.run_once()
        self.assertEqual(self.templates_sent(self.MARCUS), ["T2"])
        self.assertEqual(self.closing(s2, "marcus"), ["hold:send_unconfirmed"])


class SecondOffer(Restart):
    """A decline gets the second offer (T3); a kill between writing it down and sending it."""
    names = ("02-marcus-request.eml",)
    MARCUS = "marcus@brightlane.example"
    DECLINE = (b"From: Marcus Lee <marcus@brightlane.example>\r\n"
               b"To: Doris <doris.htm@fastmail.com>\r\n"
               b"Subject: Re: Intro call\r\n"
               b"Date: Tue, 01 Dec 2026 09:02:00 -0800\r\n"
               b"Message-ID: <20261201.0902.marcus.none-work@brightlane.example>\r\n"
               b"In-Reply-To: <603b929bb52de1bf420bfe2ada43e6ac@fastmail.com>\r\n"
               b"References: <20261201.0810.intro.marcus@brightlane.example>\r\n"
               b" <603b929bb52de1bf420bfe2ada43e6ac@fastmail.com>\r\n"
               b"MIME-Version: 1.0\r\n"
               b"Content-Type: text/plain; charset=\"utf-8\"\r\n"
               b"Content-Transfer-Encoding: 7bit\r\n\r\n"
               b"Sorry, none of those work for me. Do you have other times?\r\n\r\nMarcus\r\n")

    def setUp(self):
        super().setUp()
        add(self.root, "02b-marcus-decline.eml", self.DECLINE, {"forwarded": "no"})

    def test_killed_after_the_second_offer_was_written_down_before_the_send(self):
        s2 = self.crash_then_run(sandbox_mod.SandboxMail, "send",
                                 lambda cal, raw, envelope, mid, template=None: template == "T3")
        self.assertEqual(self.templates_sent(self.MARCUS), ["T2", "T3"])
        self.assertEqual(self.offers(), 2)
        self.assertEqual(self.closing(s2, "marcus"), ["reoffer"])


class DirectAtATimeOnceOffered(Restart):
    """A direct booking at a time a closed offer on the thread once held, killed before the write:
    the restart books it as the direct booking it was (Fable review of PR #3, finding 2)."""
    names = ("02-marcus-request.eml",)
    MARCUS = "marcus@brightlane.example"
    KEY = "20261201.0810.intro.marcus@brightlane.example"
    T2 = "<603b929bb52de1bf420bfe2ada43e6ac@fastmail.com>"

    def reply(self, date, mid, text):
        return ("From: Marcus Lee <marcus@brightlane.example>\r\n"
                "To: Doris <doris.htm@fastmail.com>\r\n"
                "Subject: Re: Intro call\r\n"
                "Date: %s\r\n"
                "Message-ID: <%s>\r\n"
                "In-Reply-To: %s\r\n"
                "References: <%s> %s\r\n"
                "MIME-Version: 1.0\r\n"
                "Content-Type: text/plain; charset=\"utf-8\"\r\n"
                "Content-Transfer-Encoding: 7bit\r\n\r\n%s\r\n\r\nMarcus\r\n"
                % (date, mid, self.T2, self.KEY, self.T2, text)).encode("ascii")

    def test_killed_before_the_write_it_books_as_direct_after_the_restart(self):
        self.run_once()
        led = ledger_mod.Ledger(os.path.join(self.state, "ledger.sqlite"))
        try:
            (offer,) = led._all("SELECT id FROM offers")
            first = led._all("SELECT start_utc FROM offer_slots WHERE offer_id = ? ORDER BY idx", (offer["id"],))[0]
        finally:
            led.close()
        start = datetime.datetime.strptime(first["start_utc"], "%Y-%m-%dT%H:%M:%SZ")
        local = start - datetime.timedelta(hours=8)                  # Pacific in December
        when = local.strftime("%A, %B ") + str(local.day) + local.strftime(" at %I:%M %p Pacific").replace(" 0", " ")
        add(self.root, "30-marcus-no.eml", self.reply("Tue, 01 Dec 2026 09:40:00 -0800",
                                                      "20261201.0940.marcus.no@brightlane.example", "No thanks."),
            {"forwarded": "no"})
        add(self.root, "31-marcus-direct.eml",
            self.reply("Tue, 01 Dec 2026 10:40:00 -0800", "20261201.1040.marcus.direct@brightlane.example",
                       "Changed my mind. Could Shane do %s for 30 minutes?" % when), {"forwarded": "no"})
        s3 = self.crash_then_run(sandbox_mod.SandboxCalendar, "create_only", lambda *a, **k: True)
        self.assertEqual(self.templates_sent(self.MARCUS)[-1], "T1")
        self.assertEqual(self.closing(s3, "marcus.direct"), ["book"])
        (event,) = self.events()
        with open(os.path.join(self.root, "calendar", event), "rb") as fh:
            self.assertIn("DTSTART:" + start.strftime("%Y%m%dT%H%M%SZ"), fh.read().decode("ascii"))


class CancelAndMove(Restart):
    """Her booking came off the calendar, then the kill: the reply goes on, never "a booking she
    does not have", and nothing is booked or removed twice."""
    names = ("01-priya-request.eml", "08-priya-move.eml")
    PRIYA = "priya@northwind.example"

    def test_a_move_killed_after_her_booking_came_off_before_the_reply(self):
        s2 = self.crash_then_run(loop._Run, "reply", lambda self_, e, f, template, *a, **k: template == "T18")
        self.assertEqual(self.templates_sent(self.PRIYA), ["T1", "T18"])
        self.assertEqual(self.events(), [])
        self.assertEqual(self.closing(s2, "priya"), ["move"])

    def test_a_move_killed_after_the_reply_before_the_closing_line(self):
        s2 = self.crash_then_run(loop._Run, "close", lambda self_, e, kind, outcome, *a, **k: outcome == "move")
        self.assertEqual(self.templates_sent(self.PRIYA), ["T1", "T18"])
        self.assertEqual(self.events(), [])
        self.assertEqual(self.offers(), 1)
        self.assertEqual(self.closing(s2, "priya"), ["move"])

    def test_a_cancel_killed_after_her_booking_came_off_before_the_reply(self):
        edit(self.root, "08-priya-move.eml", lambda r: r.replace(
            b"Something came up. Could we move to Thursday December 3 at 4:00 PM Central instead?",
            b"Something came up, so I need to cancel our call. Sorry about that."))
        s2 = self.crash_then_run(loop._Run, "reply", lambda self_, e, f, template, *a, **k: template == "T10")
        self.assertEqual(self.templates_sent(self.PRIYA), ["T1", "T10"])
        self.assertEqual(self.events(), [])
        self.assertEqual(self.closing(s2, "priya"), ["cancel"])


class HaltThenResume(Restart):
    """A stop while one message runs: resume, and the next run reads it again through the whole
    loop (calendar-rulings-spec, resume never runs a dropped action)."""
    names = ("01-priya-request.eml", "02-marcus-request.eml")

    def test_messages_a_halt_dropped_are_read_again_after_resume(self):
        real = loop._Run.classify

        def stop_then_read(run, e):
            supervisor.request_stop("throwaway", "cli", state_dir=self.state)
            return real(run, e)

        with mock.patch.object(loop._Run, "classify", stop_then_read):
            s1 = self.run_once()
        self.assertEqual(s1.halted, "cli")
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.events(), [])
        lines = receipts.read_lines(s1.receipts_path)
        self.assertEqual([l["kind"] for l in lines[-2:]], ["halted", "summary"])
        self.assertTrue(receipts.verify(s1.receipts_path, finished=True).ok)
        with open(s1.receipts_path, "rb") as fh:
            kept = fh.read().splitlines(True)[:-1]          # the summary cut off the end
        with open(s1.receipts_path, "wb") as fh:
            fh.write(b"".join(kept))
        self.assertFalse(receipts.verify(s1.receipts_path, finished=True).ok)
        supervisor.resume("throwaway", state_dir=self.state)
        s2 = self.run_once()
        self.assertEqual(self.templates_sent("priya@northwind.example"), ["T1"])
        self.assertEqual(self.templates_sent("marcus@brightlane.example"), ["T2"])
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(s2.handled, 2)


if __name__ == "__main__":
    unittest.main()
