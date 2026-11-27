"""A crash or a stop between two steps, then a restart on the same ledger (ruling 8, A3).

Each test kills run 1 at one point with an exception the loop cannot catch, then runs again on
the same state and sandbox, and checks what the prospect and the calendar end up with: nothing
twice, nothing booked again, nothing moved that the sender never asked to move.
"""

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

    def crash_then_run(self, target, attr, when):
        """Run 1 dies inside target.attr the first time when(...) is true; run 2 goes clean."""
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
        supervisor.resume("throwaway", state_dir=self.state)
        s2 = self.run_once()
        self.assertEqual(self.templates_sent("priya@northwind.example"), ["T1"])
        self.assertEqual(self.templates_sent("marcus@brightlane.example"), ["T2"])
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(s2.handled, 2)


if __name__ == "__main__":
    unittest.main()
