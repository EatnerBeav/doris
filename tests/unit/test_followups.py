"""The follow-up (brief 6.2), its one nudge (templates.md T9) and ruling 17's gone-booking check.

Every test runs her whole loop on a small copy of the demo sandbox, then looks at what a judge
would: the mail in her Sent folder, the calendar, her ledger and the receipt lines.

- Filing: an offer files its nudge and a confirmation files its calendar check, each a dated entry
  in her ledger, named in a receipt line, on the message's closing line and in the terminal.
- `owner-deleted-holds` (calendar-rulings-spec, ruling 17): her booking is missing on two reads, the
  row is marked gone, one hold notice goes to the owner, and nothing goes to the prospect.
- The nudge: once, two business days after an offer nobody answered, with the offer's own times
  still available and never a new one; never a second; not at all once there is nobody to nudge.
- The readers: `replay`, the watch page and a live trace's timeline say each of these lines in
  plain words, and a booking found gone is never called a mail.
"""

import datetime
import json
import os
import unittest
from unittest import mock

from doris import caps, compose, digest, gate, livetrace, loop, receipts, replay, templates, watch
from doris import ledger as ledger_mod
from doris.adapters import sandbox as sandbox_mod
from doris.shapes import UTC, CalendarError, MailError
from tests.unit import test_restart as tr
from tests.unit.test_restart import DORIS, OWNER, Crash

PRIYA = "priya@northwind.example"
PRIYA_KEY = "20261201.0802.priya.quick-call@northwind.example"
MARCUS = tr.MARCUS
MARCUS_KEY = tr.MARCUS_KEY
DANA = "dana@harborlight.example"
NEXT_WEEK = b"Any chance we can talk next week? I'm on Eastern time."
THIS_WEEK = b"Any chance we can talk later this week? I'm on Eastern time."
DUE = "2026-12-03T16:10:00Z"            # two business days after her offer of Tue Dec 1, 8:10 AM Pacific
REMOVED = "you removed this booking from your calendar; I haven't told them"


def utc(text):
    return datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def set_clock(root, text):
    with open(os.path.join(root, "clock.txt"), "w") as fh:
        fh.write(text)


def answer(name, addr, mid, date, irt, root_key, text, subject="Re: Intro with Shane"):
    """One more mail from a sender on their thread, answering her message irt."""
    return ("From: %s <%s>\r\n"
            "To: Doris <doris.htm@fastmail.com>\r\n"
            "Subject: %s\r\n"
            "Date: %s\r\n"
            "Message-ID: <%s>\r\n"
            "In-Reply-To: %s\r\n"
            "References: <%s> %s\r\n"
            "MIME-Version: 1.0\r\n"
            "Content-Type: text/plain; charset=\"utf-8\"\r\n"
            "Content-Transfer-Encoding: 7bit\r\n\r\n%s\r\n"
            % (name, addr, subject, date, mid, irt, root_key, irt, text)).encode("ascii")


class Base(tr.Restart):
    def setUp(self):
        tr.Restart.setUp(self)
        self.said = []

    def run_once(self):
        self.runs += 1
        cfg = loop.RunConfig(mode="throwaway", run_name="run-%d" % self.runs, state_dir=self.state,
                             sandbox_root=self.root, owner_addr=OWNER, doris_addr=DORIS, model_source="stub",
                             clock_mode="advance", retry_wait_s=0, say=self.said.append)
        return loop.run(cfg)

    def run_at(self, clock):
        set_clock(self.root, clock)
        return self.run_once()

    def crash_once(self, target, attr, when):
        """One run that dies inside target.attr the first time when(...) is true."""
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

    def ledger(self):
        return ledger_mod.Ledger(os.path.join(self.state, "ledger.sqlite"))

    def followups(self):
        with self.ledger() as led:
            return [led._followup(r) for r in led._all("SELECT * FROM followups ORDER BY id")]

    @staticmethod
    def lines(summary, kind=None, **want):
        out = []
        for line in receipts.read_lines(summary.receipts_path):
            p = line["payload"]
            if (kind is None or line["kind"] == kind) and all(p.get(k) == v for k, v in want.items()):
                out.append(line)
        return out

    def body(self, to, template):
        (msg,) = [m for t, m in self.sent(to) if t == template]
        return msg.get_body(preferencelist=("plain",)).get_content().splitlines()

    def envelope(self, template):
        with open(os.path.join(self.root, "outbox", "envelope.jsonl"), encoding="ascii") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        return [r["envelope_to"] for r in rows if r["file"].endswith("-%s.eml" % template)]


# ---------------------------------------------------------------------------------------------
# Brief 6.2: the follow-up is filed, dated and named
# ---------------------------------------------------------------------------------------------
class Filing(Base):
    names = ("01-priya-request.eml", "02-marcus-request.eml")

    def test_an_offer_files_its_one_nudge_as_a_dated_entry_in_her_ledger(self):
        s = self.run_once()
        (fu,) = [f for f in self.followups() if f["kind"] == "nudge"]
        self.assertEqual((fu["state"], fu["sender"], fu["message_key"]), ("open", MARCUS, MARCUS_KEY))
        self.assertEqual(fu["due"], utc(DUE))
        (line,) = self.lines(s, "decided", step="follow_up", action="file_follow_up", new=True,
                             thread=MARCUS_KEY)
        self.assertEqual(line["subject"], MARCUS_KEY)
        self.assertEqual(line["why"]["code"], "followup.nudge_filed")
        # the receipt names the entry, its date and the offer's expiry (seven days after it went out)
        self.assertEqual(line["payload"]["follow_up"], {"id": fu["id"], "kind": "nudge", "due": DUE,
                                                        "offer_id": fu["offer_id"],
                                                        "expires": "2026-12-08T16:10:00Z"})
        self.assertIn("2026-12-03T16:10Z", line["why"]["text"])
        self.assertIn("expires at 2026-12-08T16:10Z", line["why"]["text"])

    def test_a_confirmation_files_the_check_that_her_booking_stays_on_the_calendar(self):
        s = self.run_once()
        (fu,) = [f for f in self.followups() if f["kind"] == "booking_check"]
        (event,) = self.events()
        self.assertEqual((fu["state"], fu["sender"], fu["booking_id"] + ".ics"), ("open", PRIYA, event))
        with self.ledger() as led:
            self.assertEqual(fu["due"], led.booking(fu["booking_id"])["slot"].start_utc)
        (line,) = self.lines(s, "decided", step="follow_up", action="file_follow_up", thread=PRIYA_KEY)
        self.assertEqual(line["why"]["code"], "followup.booking_filed")
        self.assertEqual(line["payload"]["follow_up"]["booking_id"], fu["booking_id"])

    def test_the_closing_line_and_the_terminal_name_the_follow_up(self):
        s = self.run_once()
        closing = {l["subject"]: l["payload"] for l in receipts.read_lines(s.receipts_path)
                   if l["kind"] == "decided" and "outcome" in l["payload"]}
        self.assertEqual(closing[MARCUS_KEY]["follow_up"]["kind"], "nudge")
        self.assertEqual(closing[MARCUS_KEY]["follow_up"]["due"], DUE)
        self.assertEqual(closing[PRIYA_KEY]["follow_up"]["kind"], "booking_check")
        (marcus,) = [l for l in self.said if "Marcus Lee: offer" in l]
        self.assertTrue(marcus.endswith(
            " Follow-up filed: one nudge Thu Dec 3, 8:10 AM Pacific if no reply by then."), marcus)
        (priya,) = [l for l in self.said if "Priya Raman: book" in l]
        self.assertTrue(priya.endswith(
            " Follow-up filed: this booking is checked on the calendar every cycle until it starts."), priya)

    def test_the_follow_up_line_comes_after_the_send_is_found_in_sent_and_before_the_closing_line(self):
        s = self.run_once()
        kinds = [(l["kind"], l["payload"].get("call") or l["payload"].get("step") or l["payload"].get("outcome"))
                 for l in receipts.read_lines(s.receipts_path) if l["subject"] == MARCUS_KEY]
        self.assertEqual(kinds[-6:-3], [("called", "sent_lookup"), ("result", None), ("decided", "follow_up")])
        self.assertEqual(kinds[-1], ("decided", "offer"))

    def test_filed_again_after_a_kill_it_is_the_same_entry(self):
        s2 = self.crash_then_run(loop._Run, "close", lambda self_, e, kind, outcome, *a, **k: outcome == "offer")
        self.assertEqual(len([f for f in self.followups() if f["kind"] == "nudge"]), 1)
        (line,) = self.lines(s2, "decided", step="follow_up", action="file_follow_up", thread=MARCUS_KEY)
        self.assertIs(line["payload"]["new"], False)
        self.assertEqual(self.templates_sent(MARCUS), ["T2"])

    def test_a_kill_before_the_follow_up_was_filed_files_it_on_the_restart(self):
        s2 = self.crash_then_run(loop._Run, "file_follow_up",
                                 lambda self_, e, template, *a, **k: template == "T1")
        (fu,) = [f for f in self.followups() if f["kind"] == "booking_check"]
        self.assertEqual(fu["state"], "open")
        (line,) = self.lines(s2, "decided", step="follow_up", action="file_follow_up", thread=PRIYA_KEY)
        self.assertIs(line["payload"]["new"], True)
        self.assertEqual(self.templates_sent(PRIYA), ["T1"])

    def test_filing_that_fails_is_an_error_line_and_the_sent_reply_still_closes_as_decided(self):
        with mock.patch.object(ledger_mod.Ledger, "file_followup", side_effect=ValueError("no room")):
            s = self.run_once()
        self.assertEqual(self.closing(s, "marcus"), ["offer"])
        self.assertEqual(self.closing(s, "priya"), ["book"])
        self.assertEqual(s.held, 0)
        errors = self.lines(s, "error", step="follow_up")
        self.assertEqual(sorted(l["payload"]["template"] for l in errors), ["T1", "T2"])

    def test_the_digest_counts_the_follow_ups_filed(self):
        s = self.run_once()
        slots = digest.build(receipts.read_lines(s.receipts_path), utc("2026-12-02T01:00:00Z"), verified=True)
        self.assertEqual(slots["follow_ups"], (1, 1, 0))
        self.assertIn("Follow-ups filed: 1 nudge, 1 booking check.", templates.render("S2", slots).split("\n"))


class NudgeDue(unittest.TestCase):
    def due(self, sent):
        return loop.nudge_due(utc(sent)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_two_business_days_at_the_same_pacific_time(self):
        self.assertEqual(self.due("2026-12-01T16:10:00Z"), "2026-12-03T16:10:00Z")     # Tue -> Thu

    def test_a_weekend_does_not_count(self):
        self.assertEqual(self.due("2026-12-03T22:30:00Z"), "2026-12-07T22:30:00Z")     # Thu -> Mon
        self.assertEqual(self.due("2026-12-05T18:00:00Z"), "2026-12-08T18:00:00Z")     # Sat -> Tue

    def test_her_holidays_do_not_count(self):
        # Wed Dec 23: Thu Dec 24 and Fri Dec 25 are on her holiday list, then the weekend
        self.assertEqual(self.due("2026-12-23T17:00:00Z"), "2026-12-29T17:00:00Z")

    def test_the_pacific_clock_time_holds_across_a_clock_change(self):
        # Fri Mar 12 2027, 9:00 AM Pacific standard -> Tue Mar 16, 9:00 AM Pacific daylight
        self.assertEqual(self.due("2027-03-12T17:00:00Z"), "2027-03-16T16:00:00Z")


# ---------------------------------------------------------------------------------------------
# Ruling 17: one of her future bookings is gone from the calendar
# ---------------------------------------------------------------------------------------------
class GoneSetup(Base):
    """Priya's request is booked in a first run; the tests then take her event off the calendar."""
    names = ("01-priya-request.eml",)

    def setUp(self):
        Base.setUp(self)
        self.first = self.run_once()
        (self.event,) = self.events()
        self.bid = self.event[:-len(".ics")]
        self.subject = "booking:" + self.bid

    def owner_deletes(self):
        os.remove(os.path.join(self.root, "calendar", self.event))

    def booking(self):
        with self.ledger() as led:
            return led.booking(self.bid)

    def notices(self):
        return [m for t, m in self.sent(OWNER) if t == "S1"]


class GoneBooking(GoneSetup):
    def test_owner_deleted_holds(self):
        """calendar-rulings-spec, ruling 17: missing on two reads, the row marked gone, one hold
        notice to the owner, nothing to the prospect."""
        self.owner_deletes()
        s = self.run_once()
        reads = [l["payload"] for l in self.lines(s, "called", call="calendar_get", target=self.bid)]
        self.assertEqual([r["booking_check"] for r in reads], [1, 2])
        answers = [l["payload"] for l in self.lines(s, "result") if "found" in l["payload"]]
        self.assertEqual([a["found"] for a in answers][:2], [False, False])
        self.assertEqual(self.booking()["state"], "gone")
        (notice,) = self.notices()
        self.assertEqual(str(notice["To"]), OWNER)
        text = notice.get_body(preferencelist=("plain",)).get_content()
        self.assertIn("Held for you: Priya Raman (%s), %s." % (PRIYA, REMOVED), text)
        self.assertIn("Nothing has been sent to them", text)
        self.assertEqual(self.envelope("S1"), [[OWNER]])
        self.assertEqual(self.templates_sent(PRIYA), ["T1"])        # her confirmation from before; nothing since
        self.assertEqual((s.held, s.handled, s.hold_notices, s.sends, s.errors), (1, 0, 1, 0, 0))
        self.assertTrue(s.verify_ok)

    def test_its_receipts_say_what_she_saw_then_the_notice_then_the_one_held_line(self):
        self.owner_deletes()
        s = self.run_once()
        mine = [l for l in receipts.read_lines(s.receipts_path) if l["subject"] == self.subject]
        seen, held = mine[0], mine[-1]
        self.assertEqual(seen["kind"], "observed")
        self.assertEqual({k: seen["payload"][k] for k in ("step", "owner_deleted", "booking_id", "reads", "message",
                                                           "thread", "from_addr", "from_name", "recovered")},
                         {"step": "booking_check", "owner_deleted": True, "booking_id": self.bid, "reads": 2,
                          "message": PRIYA_KEY, "thread": PRIYA_KEY, "from_addr": PRIYA, "from_name": "Priya Raman",
                          "recovered": False})
        self.assertEqual(held["kind"], "held")
        self.assertEqual(held["why"]["code"], "hold.owner_removed")
        self.assertEqual((held["payload"]["outcome"], held["payload"]["booking_id"], held["payload"]["notice_sent"]),
                         ("hold:owner_removed", self.bid, True))
        self.assertEqual(held["payload"]["notice_message_id"],
                         compose.new_message_id(DORIS, self.subject, "S1", 1))
        between = [(l["kind"], l["payload"].get("call") or l["payload"].get("action")) for l in mine[1:-1]]
        self.assertIn(("checked", "send_hold_notice"), between)
        self.assertLess(between.index(("checked", "send_hold_notice")), between.index(("called", "send")))
        (check,) = [l for l in mine if l["kind"] == "checked"]
        self.assertIs(check["payload"]["passed"], True)
        self.assertNotIn("SUMMARY", json.dumps(mine))               # the booking ID, never a title

    def test_the_thread_is_the_owners_from_then_on_and_the_follow_up_is_closed(self):
        self.owner_deletes()
        self.run_once()
        with self.ledger() as led:
            self.assertTrue(led.thread_held(PRIYA_KEY))
            self.assertEqual(led.gone_unfinished(), [])
        (fu,) = self.followups()
        self.assertEqual((fu["state"], fu["outcome"]), ("done", "gone"))

    def test_the_terminal_says_it_in_one_plain_line(self):
        self.owner_deletes()
        self.said[:] = []
        self.run_once()
        (line,) = [l for l in self.said if "hold:owner_removed" in l]
        self.assertIn("Priya Raman: hold:owner_removed. Held: the owner removed one of her bookings; the prospect "
                      "was not told. Her booking for Wed Dec 2, 11:00 AM Pacific is gone from the calendar (missing "
                      "on two reads). Hold notice to the owner sent. Nothing sent to them.", line)

    def test_in_one_pass_over_a_sandbox_day_she_reads_it_at_each_later_cycle_of_her_clock(self):
        """`demo` is one pass over a whole day on her advancing clock: each step to her next cycle is
        a cycle, so the booking she made at 8:10 is read at 8:30 and again at 11:20."""
        for name in ("05-opsweekly-newsletter.eml", "09-ken-instruction.eml"):
            with open(os.path.join(tr.demo_sandbox.SANDBOX, "inbox", name), "rb") as fh:
                tr.add(self.root, name, fh.read(), {})
        set_clock(self.root, "2026-12-01T16:20:00Z")
        s = self.run_once()
        reads = self.lines(s, "called", call="calendar_get", target=self.bid)
        self.assertEqual([(l["subject"], l["payload"]["booking_check"]) for l in reads], [("", 1), ("", 1)])
        clocks = [l["ts"] for l in reads]
        self.assertEqual(clocks, ["2026-12-01T16:30:00Z", "2026-12-01T19:20:00Z"])
        self.assertEqual((self.booking()["state"], s.held, s.errors), ("confirmed", 0, 0))
        self.assertIn("her check once a cycle: 2 reads, each found its booking there (%s 2 times)" % self.bid,
                      replayed(s))

    def test_a_booking_still_there_gets_one_read_and_nothing_more(self):
        s = self.run_once()
        reads = self.lines(s, "called", call="calendar_get", target=self.bid)
        self.assertEqual([l["payload"]["booking_check"] for l in reads], [1])
        self.assertEqual(self.lines(s, "observed", step="booking_check"), [])
        self.assertEqual((self.booking()["state"], self.notices(), s.held), ("confirmed", [], 0))

    def test_an_edit_is_not_checked(self):
        path = os.path.join(self.root, "calendar", self.event)
        with open(path, "rb") as fh:
            data = fh.read()
        with open(path, "wb") as fh:                                # the owner moves it an hour later
            fh.write(data.replace(b"T190000Z", b"T200000Z").replace(b"T193000Z", b"T203000Z"))
        s = self.run_once()
        self.assertEqual((self.booking()["state"], self.notices(), s.held), ("confirmed", [], 0))

    def test_a_read_that_fails_is_never_missing(self):
        self.owner_deletes()
        with mock.patch.object(sandbox_mod.SandboxCalendar, "get", side_effect=CalendarError("down")):
            s = self.run_once()
        self.assertEqual((self.booking()["state"], self.notices(), s.held), ("confirmed", [], 0))
        self.assertEqual(self.lines(s, "observed", step="booking_check"), [])
        self.assertEqual(self.templates_sent(PRIYA), ["T1"])

    def test_missing_on_one_read_and_a_second_read_that_fails_is_not_gone(self):
        self.owner_deletes()
        real, calls = sandbox_mod.SandboxCalendar.get, []

        def missing_then_down(cal, booking_id):
            calls.append(booking_id)
            if len(calls) == 1:
                return real(cal, booking_id)
            raise CalendarError("down")

        with mock.patch.object(sandbox_mod.SandboxCalendar, "get", missing_then_down):
            s = self.run_once()
        self.assertEqual(len(calls), 3)                             # missing, then the read and its one retry
        self.assertEqual((self.booking()["state"], self.notices(), s.held), ("confirmed", [], 0))

    def test_missing_on_one_read_and_there_on_the_second_is_not_gone(self):
        real, calls = sandbox_mod.SandboxCalendar.get, []

        def missing_once(cal, booking_id):
            calls.append(booking_id)
            return None if len(calls) == 1 else real(cal, booking_id)

        with mock.patch.object(sandbox_mod.SandboxCalendar, "get", missing_once):
            s = self.run_once()
        self.assertEqual(len(calls), 2)
        self.assertEqual((self.booking()["state"], self.notices(), s.held), ("confirmed", [], 0))

    def test_the_two_reads_are_a_short_wait_apart(self):
        self.owner_deletes()
        waits = []
        self.runs += 1
        cfg = loop.RunConfig(mode="throwaway", run_name="run-%d" % self.runs, state_dir=self.state,
                             sandbox_root=self.root, owner_addr=OWNER, doris_addr=DORIS, model_source="stub",
                             clock_mode="advance")
        with mock.patch.object(loop, "_sleep", waits.append), mock.patch.object(loop.time, "sleep", waits.append):
            loop.run(cfg)
        self.assertIn(caps.RULING17_REREAD_WAIT_S, waits)
        self.assertEqual(self.booking()["state"], "gone")

    def test_it_runs_after_the_stop_scan_and_before_any_new_message_is_handled(self):
        self.owner_deletes()
        with open(os.path.join(tr.demo_sandbox.SANDBOX, "inbox", "02-marcus-request.eml"), "rb") as fh:
            tr.add(self.root, "02-marcus-request.eml", fh.read(), {"forwarded": "yes"})
        s = self.run_once()
        order = [(l["kind"], l["subject"]) for l in receipts.read_lines(s.receipts_path)
                 if l["kind"] in ("observed", "held") and l["subject"]]
        self.assertEqual(order, [("observed", self.subject), ("held", self.subject), ("observed", MARCUS_KEY)])
        self.assertEqual(self.templates_sent(MARCUS), ["T2"])       # the rest of the run goes on as ever

    def test_a_kill_after_her_ledger_marked_it_gone_is_finished_once(self):
        self.owner_deletes()
        self.crash_once(loop._Run, "deliver", lambda self_, e, mid, kind, template, *a, **k: template == "S1")
        self.assertEqual((self.booking()["state"], self.notices()), ("gone", []))
        s = self.run_once()
        self.assertEqual(len(self.notices()), 1)
        (seen,) = self.lines(s, "observed", step="booking_check")
        self.assertEqual((seen["payload"]["recovered"], seen["payload"]["reads"]), (True, 0))
        (held,) = self.lines(s, "held", booking_id=self.bid)
        self.assertEqual((held["payload"]["recovered"], held["payload"]["notice_sent"]), (True, True))
        self.assertEqual(self.lines(s, "called", call="calendar_get"), [])     # gone already: not read again
        s4 = self.run_once()                                        # and the run after that has nothing to do
        self.assertEqual((len(self.notices()), s4.held), (1, 0))

    def test_a_kill_after_the_notice_went_never_sends_it_twice(self):
        self.owner_deletes()
        self.crash_once(loop._Run, "look_in_sent", lambda self_, e, mid, template, *a, **k: template == "S1")
        self.assertEqual(len(self.notices()), 1)
        s = self.run_once()
        self.assertEqual(len(self.notices()), 1)
        (held,) = self.lines(s, "held", booking_id=self.bid)
        self.assertIs(held["payload"]["notice_sent"], True)
        self.assertEqual(self.lines(s, "called", call="send"), [])

    def test_past_the_hourly_cap_on_notices_the_hold_stands_and_the_digest_lists_it(self):
        self.owner_deletes()
        with mock.patch.object(caps, "HOLD_NOTICES_PER_HOUR", 0):
            s = self.run_once()
        self.assertEqual((self.booking()["state"], self.notices(), s.held), ("gone", [], 1))
        (held,) = self.lines(s, "held", booking_id=self.bid)
        self.assertEqual((held["payload"]["notice_message_id"], held["payload"]["notice_sent"],
                          held["payload"]["notice"]), (None, False, "none"))
        slots = digest.build(receipts.read_lines(s.receipts_path), utc("2026-12-02T01:00:00Z"), verified=True)
        self.assertEqual([(h["name"], h["reason"]) for h in slots["holds"]], [("Priya Raman", "hold.owner_removed")])
        self.assertEqual((slots["handled"], slots["held"]), (0, 1))

    def test_a_later_cancel_for_it_is_held_as_a_booking_she_does_not_have_with_no_second_notice(self):
        self.owner_deletes()
        self.run_once()
        (_, t1) = self.sent(PRIYA)[0]
        tr.add(self.root, "20-priya-cancel.eml",
               answer("Priya Raman", PRIYA, "20261201.1000.priya.cancel@northwind.example",
                      "Tue, 01 Dec 2026 10:00:00 -0800", str(t1["Message-ID"]), PRIYA_KEY,
                      "Please cancel our call.", subject="Re: Quick call"), {"forwarded": "no"})
        s = self.run_once()
        self.assertEqual(self.closing(s, "priya.cancel"), ["hold:not_in_ledger"])
        self.assertEqual(self.templates_sent(PRIYA), ["T1"])
        self.assertEqual(len(self.notices()), 1)
        slots = digest.build(receipts.read_lines(s.receipts_path), utc("2026-12-02T01:00:00Z"), verified=True)
        self.assertEqual([h["reason"] for h in slots["holds"]], ["hold.not_in_ledger"])

    def test_a_booking_recorded_before_follow_ups_were_kept_still_gets_its_notice(self):
        with self.ledger() as led:                                  # the live ledger's one booking is like this
            led._con.execute("DELETE FROM followups")
        self.owner_deletes()
        s = self.run_once()
        (notice,) = self.notices()
        text = notice.get_body(preferencelist=("plain",)).get_content()
        self.assertIn("Held for you: %s (%s), %s." % (PRIYA, PRIYA, REMOVED), text)
        self.assertIn("They wrote at Tue Dec 1, 8:02 AM Pacific:", text)
        self.assertEqual((self.booking()["state"], s.held, s.errors), ("gone", 1, 0))

    def test_a_bug_on_one_booking_is_an_error_line_and_the_run_goes_on(self):
        self.owner_deletes()
        with mock.patch.object(loop._Run, "gone_notice", side_effect=RuntimeError("boom")):
            s = self.run_once()
        (err,) = self.lines(s, "error", step="booking_check")
        self.assertEqual(err["subject"], self.subject)
        self.assertEqual(self.booking()["state"], "gone")           # her ledger step stands
        s3 = self.run_once()                                        # and her next pass finishes it
        self.assertEqual((len(self.notices()), s3.held), (1, 1))

    def test_a_step_that_failed_and_was_finished_later_is_never_named_as_dropped_by_a_stop(self):
        """Fable review of PR #42, finding 6: the failed try's queue row is finished (as an error),
        not left started for a later halted line to list under "dropped"."""
        self.owner_deletes()
        with mock.patch.object(loop._Run, "gone_notice", side_effect=RuntimeError("boom")):
            self.run_once()
        with self.ledger() as led:
            self.assertEqual(led.queued_actions(), [])
            self.assertEqual([(a["kind"], a["state"], a["outcome"]) for a in led.actions_for_message(PRIYA_KEY)
                              if a["kind"] == "booking_check"], [("booking_check", "finished", "error")])
        self.run_once()
        with self.ledger() as led:
            self.assertEqual(led.queued_actions(), [])
            self.assertEqual([(a["state"], a["outcome"]) for a in led.actions_for_message(PRIYA_KEY)
                              if a["kind"] == "booking_check"],
                             [("finished", "error"), ("finished", "hold:owner_removed")])

    def test_a_notice_the_mail_server_took_is_never_called_not_sent(self):
        """Fable review of PR #42, finding 5: handed over, and her copy not seen in Sent on two looks.
        The held line, the terminal and every reader say that, not "not sent"."""
        self.owner_deletes()
        self.said[:] = []
        with mock.patch.object(sandbox_mod.SandboxMail, "find_in_sent", lambda mail, mid: False):
            s = self.run_once()
        self.assertEqual(len(self.notices()), 1)                    # the owner has it
        (held,) = self.lines(s, "held", booking_id=self.bid)
        self.assertEqual((held["payload"]["notice"], held["payload"]["notice_sent"]), ("handed_over", False))
        (line,) = [l for l in self.said if "hold:owner_removed" in l]
        self.assertIn("Hold notice to the owner handed to the mail server (her copy is not in Sent yet). "
                      "Nothing sent to them.", line)
        self.assertIn("Hold notice to the owner: handed to the mail server (Message-ID %s); her copy was not seen "
                      "in Sent." % held["payload"]["notice_message_id"], replayed(s))
        self.assertIn("Held for the owner: hold:owner_removed. Hold notice handed to the mail server, not yet seen "
                      "in Sent; nothing sent to the prospect.", [r["what"] for r in watched(s)])
        self.assertIn("held hold:owner_removed: hold notice to the owner handed to the mail server (not seen in "
                      "Sent), nothing to the prospect", "\n".join(timeline(s)))
        for text in (replayed(s), " ".join(r["what"] for r in watched(s)), "\n".join(timeline(s))):
            self.assertNotIn("not sent", text)
            self.assertNotIn("No hold notice went", text)

    def test_the_held_line_says_which_way_the_notice_went(self):
        self.owner_deletes()
        s = self.run_once()
        (held,) = self.lines(s, "held", booking_id=self.bid)
        self.assertEqual((held["payload"]["notice"], held["payload"]["notice_sent"]), ("sent", True))

    def test_a_line_that_does_not_say_how_the_notice_went_is_read_as_not_recorded(self):
        line = {"seq": 9, "ts": "2026-12-01T16:10:00Z", "kind": "held", "subject": self.subject, "why": {},
                "payload": {"outcome": "hold:owner_removed", "step": "booking_check", "booking_id": self.bid}}
        self.assertEqual(watch.row(line)["what"], "Held for the owner: hold:owner_removed. Whether a hold notice went "
                                                  "is not recorded. Nothing sent to the prospect.")
        self.assertIn("hold notice to the owner not recorded, nothing to the prospect",
                      "\n".join(livetrace.timeline([line])))

    def test_the_notices_draft_never_offers_the_time_the_owner_just_removed(self):
        """Fable review of PR #42, finding 9."""
        self.owner_deletes()
        self.run_once()
        (notice,) = self.notices()
        draft = notice.get_body(preferencelist=("plain",)).get_content().split("----", 1)[1]
        self.assertNotIn("Wed Dec 2, 11:00 AM Pacific", draft)
        self.assertEqual([l[:2] for l in draft.splitlines() if l[:2] in ("1.", "2.", "3.")], ["1.", "2.", "3."])


class GoneCheckGoesOn(Base):
    """Fable review of PR #42, finding 4: a booking that cannot be read is never "missing", and it
    never ends the check for the bookings after it. Only a calendar that does not answer at all
    (two bookings in a row) ends the cycle's check. Each one is said once a run, loud."""
    names = ("01-priya-request.eml", "02-marcus-request.eml")
    NEXT_CYCLE = "2026-12-01T18:20:00Z"

    def setUp(self):
        Base.setUp(self)
        self.run_once()
        (_, t2) = self.sent(MARCUS)[0]
        tr.add(self.root, "20-marcus-pick.eml",
               answer("Marcus Lee", MARCUS, "20261201.1000.marcus.pick@brightlane.example",
                      "Tue, 01 Dec 2026 10:00:00 -0800", str(t2["Message-ID"]), MARCUS_KEY, "The first one works."),
               {"forwarded": "no"})
        self.run_once()
        with self.ledger() as led:
            self.first, self.second = [b["booking_id"] for b in led.future_bookings(utc("2026-12-01T18:10:00Z"))]
        self.said[:] = []

    def unreadable(self, bid):
        with open(os.path.join(self.root, "calendar", bid + ".ics"), "wb") as fh:
            fh.write(b"not a calendar file\r\n")

    def states(self):
        with self.ledger() as led:
            return led.booking(self.first)["state"], led.booking(self.second)["state"]

    def checked(self, summary):
        """The bookings her check read in this run, in order, one entry per read."""
        return [l["payload"]["target"] for l in self.lines(summary, "called", call="calendar_get")
                if "booking_check" in l["payload"]]

    def test_one_booking_that_cannot_be_read_does_not_stop_the_check_for_the_next(self):
        self.unreadable(self.first)
        os.remove(os.path.join(self.root, "calendar", self.second + ".ics"))       # the owner removes the later one
        s = self.run_at(self.NEXT_CYCLE)
        self.assertEqual(self.states(), ("confirmed", "gone"))                     # unread is never "missing"
        (seen,) = self.lines(s, "observed", step="booking_check")
        self.assertEqual(seen["payload"]["booking_id"], self.second)
        self.assertEqual(len([m for t, m in self.sent(OWNER) if t == "S1"]), 1)
        (err,) = self.lines(s, "error", step="booking_check")
        self.assertEqual((err["subject"], err["payload"]["booking_id"]), ("booking:" + self.first, self.first))
        (line,) = [l for l in self.said if "could not be read" in l]
        self.assertIn("Calendar check: her booking for Wed Dec 2, 11:00 AM Pacific could not be read on the calendar "
                      "(error line written). That is never \"missing\": she reads it again at her next cycle.", line)
        self.assertEqual((s.held, s.errors), (1, 1))
        self.assertTrue(s.verify_ok)

    def test_it_is_said_once_a_run_however_many_cycles_fail(self):
        self.unreadable(self.first)
        for name in ("05-opsweekly-newsletter.eml", "09-ken-instruction.eml"):     # mail at two later cycles
            with open(os.path.join(tr.demo_sandbox.SANDBOX, "inbox", name), "rb") as fh:
                tr.add(self.root, name, fh.read(), {})
        s = self.run_once()
        self.assertGreater(self.checked(s).count(self.first), 2)                   # read again at each cycle
        self.assertEqual(len(self.lines(s, "error", step="booking_check")), 1)
        self.assertEqual(len([l for l in self.said if "could not be read" in l]), 1)
        self.assertEqual(self.states(), ("confirmed", "confirmed"))

    def test_every_reader_calls_it_her_calendar_check_and_never_a_mail(self):
        self.unreadable(self.first)
        s = self.run_at(self.NEXT_CYCLE)
        text = replayed(s)
        self.assertIn("Item 1 of 1: her calendar check, no mail: her booking %s could not be checked, and it is not "
                      "called gone" % self.first, text)
        self.assertNotIn("Message-ID booking:", text)
        self.assertIn("is booking %s still there -> failed [" % self.first, text)   # said once, not "failed: failed"
        (row,) = [r for r in watched(s) if r["step"] == "error"]
        self.assertEqual(row["what"], "Her calendar check could not read her booking %s. That is never \"missing\": "
                                      "she reads it again at her next cycle." % self.first)
        self.assertIn("her calendar check (no mail): booking %s could not be read, never called missing, read again "
                      "next cycle" % self.first, "\n".join(timeline(s)))
        self.assertEqual(replay.BOOKING_KEY, ledger_mod.GONE_KEY)

    def test_a_calendar_that_answers_nothing_ends_the_cycles_check_after_two_in_a_row(self):
        with mock.patch.object(sandbox_mod.SandboxCalendar, "get", side_effect=CalendarError("down")):
            s = self.run_at(self.NEXT_CYCLE)
            self.assertEqual(sorted(set(self.checked(s))), sorted([self.first, self.second]))
            with mock.patch.object(caps, "RULING17_UNREAD_IN_A_ROW", 1):
                s2 = self.run_at("2026-12-01T18:30:00Z")
        self.assertEqual(set(self.checked(s2)), {self.first})                      # the next one is left for her next cycle
        self.assertEqual(self.states(), ("confirmed", "confirmed"))
        self.assertEqual(self.lines(s, "observed", step="booking_check") + self.lines(s2, "observed", step="booking_check"), [])
        self.assertEqual([m for t, m in self.sent(OWNER) if t == "S1"], [])


class GoneBookingPartWay(Base):
    """A booking whose own message a kill left part way is that message's to finish, never ruling 17's."""
    names = ("01-priya-request.eml",)

    def test_a_booking_left_part_way_is_not_read_by_the_check(self):
        s2 = self.crash_then_run(loop._Run, "reply", lambda self_, e, f, template, *a, **k: template == "T1")
        self.assertEqual(self.lines(s2, "observed", step="booking_check"), [])
        self.assertEqual([l["payload"].get("booking_check") for l in self.lines(s2, "called", call="calendar_get")
                          if "booking_check" in l["payload"]], [])
        self.assertEqual(self.templates_sent(PRIYA), ["T1"])

    def test_gone_at_the_restart_and_killed_again_it_is_still_held_as_removed_by_the_owner(self):
        """Her own message finds its booking gone on the restart and marks it; a second kill lands
        before the hold is written down. The next run holds it for the same true reason, never as
        "the model was unsure": one notice to the owner, nothing to the prospect."""
        self.crash_once(loop._Run, "reply", lambda self_, e, f, template, *a, **k: template == "T1")
        (event,) = self.events()
        os.remove(os.path.join(self.root, "calendar", event))
        self.crash_once(loop._Run, "hold_code", lambda self_, e, code, *a, **k: code == "hold.owner_removed")
        with self.ledger() as led:
            self.assertEqual(led.booking(event[:-len(".ics")])["state"], "gone")
        s3 = self.run_once()
        self.assertEqual(self.closing(s3, PRIYA_KEY), ["hold:owner_removed"])
        (notice,) = [m for t, m in self.sent(OWNER) if t == "S1"]
        self.assertIn(REMOVED, notice.get_body(preferencelist=("plain",)).get_content())
        self.assertEqual(self.templates_sent(PRIYA), [])
        self.assertEqual(self.lines(s3, "observed", step="booking_check"), [])
        self.assertEqual((s3.held, s3.errors), (1, 0))
        self.assertTrue(s3.verify_ok)


class HerOwnCancelPartWay(Base):
    """Her own cancel or move takes her event off the calendar because the sender asked. A kill, a
    newer mail or a calendar that stops answering part way never turns that into "you removed this
    booking" (Fable review of PR #42, finding 1): her ledger says she is taking the booking off
    before her delete goes, and ruling 17 reads only bookings she never started to take off."""
    names = ("01-priya-request.eml",)
    CANCEL = "Please cancel our call."
    MOVE = "Can we move our call to another time?"

    def setUp(self):
        Base.setUp(self)
        self.first = self.run_once()
        (self.event,) = self.events()
        self.bid = self.event[:-len(".ics")]
        (_, self.t1) = self.sent(PRIYA)[0]

    def priya_writes(self, text, name="20-priya-asks.eml", mid="20261201.1000.priya.asks@northwind.example",
                     date="Tue, 01 Dec 2026 10:00:00 -0800"):
        tr.add(self.root, name, answer("Priya Raman", PRIYA, mid, date, str(self.t1["Message-ID"]), PRIYA_KEY,
                                       text, subject="Re: Quick call"), {"forwarded": "no"})

    def booking_state(self):
        with self.ledger() as led:
            return led.booking(self.bid)["state"]

    def assert_never_called_removed_by_the_owner(self, *summaries):
        for s in summaries:
            self.assertEqual(self.lines(s, "observed", step="booking_check"), [])
        self.assertEqual([m for t, m in self.sent(OWNER)
                          if REMOVED in m.get_body(preferencelist=("plain",)).get_content()], [])

    def killed_after_her_delete(self, text, between=None):
        """The kill lands after her calendar delete and before her ledger says cancelled."""
        self.priya_writes(text)
        return self.crash_then_run(ledger_mod.Ledger, "set_booking_state",
                                   lambda db, bid, state: state == "cancelled", between=between)

    def test_a_kill_after_her_delete_finishes_the_cancel_and_never_blames_the_owner(self):
        s2 = self.killed_after_her_delete(self.CANCEL)
        self.assertEqual(self.templates_sent(PRIYA), ["T1", "T10"])
        self.assertEqual(self.templates_sent(OWNER), [])
        self.assert_never_called_removed_by_the_owner(s2)
        self.assertEqual((self.booking_state(), self.events()), ("cancelled", []))
        self.assertEqual(self.closing(s2, "priya.asks"), ["cancel"])
        self.assertEqual((s2.handled, s2.held, s2.errors), (1, 0, 0))
        self.assertTrue(s2.verify_ok)

    def test_a_kill_after_her_delete_finishes_the_move_and_never_blames_the_owner(self):
        s2 = self.killed_after_her_delete(self.MOVE)
        self.assertEqual(self.templates_sent(PRIYA), ["T1", "T18"])
        self.assertEqual(self.templates_sent(OWNER), [])
        self.assert_never_called_removed_by_the_owner(s2)
        self.assertEqual((self.booking_state(), self.events()), ("cancelled", []))
        self.assertEqual(self.closing(s2, "priya.asks"), ["move"])
        self.assertEqual((s2.handled, s2.held, s2.errors), (1, 0, 0))

    def test_her_ledger_says_she_is_taking_it_off_before_her_delete_goes(self):
        """Ledger first (A3): killed inside the calendar's delete, her ledger already says so, the
        check on the restart leaves that booking to its own message, and the cancel is finished."""
        self.priya_writes(self.CANCEL)
        self.crash_once(sandbox_mod.SandboxCalendar, "delete", lambda cal, booking_id: True)
        self.assertEqual((self.booking_state(), self.events()), ("cancelling", [self.event]))
        s3 = self.run_once()
        self.assertEqual(self.templates_sent(PRIYA), ["T1", "T10"])
        self.assert_never_called_removed_by_the_owner(s3)
        self.assertEqual((self.booking_state(), self.events()), ("cancelled", []))
        self.assertEqual((s3.handled, s3.held), (1, 0))

    def test_a_newer_mail_while_she_was_down_never_takes_the_place_of_her_half_done_cancel(self):
        """Killed after her delete; a newer mail from the same sender on the thread is in before the
        restart. The cancel is finished first (ruling 8), never superseded with her event already
        off the calendar and her ledger still calling the booking confirmed."""
        s2 = self.killed_after_her_delete(self.CANCEL, between=lambda: self.priya_writes(
            "FYI, no action needed on my side.", name="21-priya-fyi.eml",
            mid="20261201.1004.priya.fyi@northwind.example", date="Tue, 01 Dec 2026 10:04:00 -0800"))
        self.assertEqual(self.closing(s2, "priya.asks"), ["cancel"])
        self.assertEqual(self.closing(s2, "priya.fyi"), ["file"])
        self.assertEqual(self.templates_sent(PRIYA), ["T1", "T10"])
        self.assert_never_called_removed_by_the_owner(s2)
        self.assertEqual(self.booking_state(), "cancelled")

    def test_a_delete_that_landed_and_could_not_be_confirmed_is_never_called_the_owners(self):
        """Her delete lands and the calendar then stops answering: the cancel is held for the owner
        (calendar down). At her next cycles the event is missing, by her own hand: the check leaves
        that booking alone, so no notice says the owner removed it."""
        self.priya_writes(self.CANCEL)

        def landed_but_unconfirmed(run, e, bid):
            run.calendar.delete(bid)
            return False

        with mock.patch.object(loop._Run, "remove", landed_but_unconfirmed):
            s2 = self.run_once()
        self.assertEqual(self.closing(s2, "priya.asks"), ["hold:calendar_down"])
        self.assertEqual((self.booking_state(), self.events()), ("cancelling", []))
        s3 = self.run_at("2026-12-01T18:20:00Z")
        s4 = self.run_at("2026-12-01T18:30:00Z")
        self.assert_never_called_removed_by_the_owner(s2, s3, s4)
        self.assertEqual([l["payload"]["target"] for s in (s3, s4) for l in self.lines(s, "called", call="calendar_get")
                          if "booking_check" in l["payload"]], [])
        self.assertEqual(self.templates_sent(PRIYA), ["T1"])
        self.assertEqual(self.templates_sent(OWNER), ["S1"])        # the one notice: her cancel is held
        self.assertEqual((self.booking_state(), s3.held, s4.held, s4.errors), ("cancelling", 0, 0, 0))


# ---------------------------------------------------------------------------------------------
# The nudge (T9)
# ---------------------------------------------------------------------------------------------
class NudgeSetup(Base):
    """Marcus asks for next week and gets an offer (T2) of three times in a first run; nobody answers."""
    names = ("02-marcus-request.eml",)
    SLOTS = ("2026-12-07T17:00:00Z", "2026-12-07T17:30:00Z", "2026-12-07T18:00:00Z")   # Mon Dec 7, 9:00 to 10:30 AM Pacific

    def setUp(self):
        Base.setUp(self)
        tr.edit(self.root, "02-marcus-request.eml", lambda raw: raw.replace(THIS_WEEK, NEXT_WEEK))
        self.first = self.run_once()
        self.assertEqual(self.templates_sent(MARCUS), ["T2"])

    def offer(self):
        with self.ledger() as led:
            return led.offer(1)

    def busy(self, *starts):
        for i, start in enumerate(starts):
            end = (utc(start) + datetime.timedelta(minutes=30)).strftime("%Y%m%dT%H%M%SZ")
            tr.owner_busy(self.root, "owner-busy-test-%d" % i, utc(start).strftime("%Y%m%dT%H%M%SZ"), end)


class Nudge(NudgeSetup):
    def test_the_offer_holds_the_three_times_the_tests_below_use(self):
        self.assertEqual([s.start_utc for s in self.offer()["slots"]], [utc(t) for t in self.SLOTS])

    def test_one_nudge_goes_two_business_days_after_an_offer_nobody_answered(self):
        s = self.run_at(DUE)
        self.assertEqual(self.templates_sent(MARCUS), ["T2", "T9"])
        (_, t2), (_, t9) = self.sent(MARCUS)
        self.assertEqual(str(t9["To"]), MARCUS)
        self.assertEqual(str(t9["Subject"]), "Re: Intro with Shane")
        self.assertEqual(str(t9["In-Reply-To"]), "<%s>" % MARCUS_KEY)
        self.assertEqual(str(t9["Auto-Submitted"]), "auto-replied")
        self.assertEqual(self.envelope("T9"), [[MARCUS, OWNER]])    # the owner on blind copy, as on every reply
        self.assertEqual(self.body(MARCUS, "T9")[:5], [
            "One more try: Shane is still available at any of these, 30 min each:",
            "1. Mon Dec 7, 9:00 AM Pacific (12:00 PM Eastern)",
            "2. Mon Dec 7, 9:30 AM Pacific (12:30 PM Eastern)",
            "3. Mon Dec 7, 10:00 AM Pacific (1:00 PM Eastern)",
            "After this I'll leave it with you."])
        self.assertEqual((s.sends, s.handled, s.held, s.errors), (1, 0, 0, 0))
        self.assertTrue(s.verify_ok)

    def test_its_receipts_show_the_plan_the_gate_the_send_and_the_close(self):
        s = self.run_at(DUE)
        lines = receipts.read_lines(s.receipts_path)
        shape = [(l["kind"], l["payload"].get("call") or l["payload"].get("action")) for l in lines
                 if l["kind"] in ("decided", "checked", "called")]
        # her calendar read of the offer's own times, the plan, the gate, the send, her copy in Sent, the close
        self.assertEqual(shape, [("called", "calendar_query"), ("decided", "nudge"), ("checked", "send_reply"),
                                 ("called", "send"), ("called", "sent_lookup"), ("decided", "close_follow_up")])
        self.assertEqual([l["subject"] for l in lines], [""] * len(lines))     # run level: no message is in hand
        (plan,) = self.lines(s, "decided", action="nudge")
        self.assertEqual(plan["why"]["code"], "followup.nudge")
        self.assertEqual([k["start"] for k in plan["payload"]["kept"]], list(self.SLOTS))
        self.assertEqual(plan["payload"]["dropped"], [])
        (check,) = self.lines(s, "checked", action="send_reply", template="T9")
        self.assertIs(check["payload"]["passed"], True)
        self.assertLess(check["seq"], self.lines(s, "called", call="send")[0]["seq"])
        (send,) = self.lines(s, "called", call="send", target="T9")
        self.assertEqual((send["payload"]["to"], send["payload"]["bcc"]), ("sender", "owner"))
        (done,) = self.lines(s, "decided", action="close_follow_up")
        self.assertEqual(done["why"]["code"], "followup.nudge_done")

    def test_her_ledger_marks_the_offer_nudged_and_it_is_not_a_new_offer(self):
        before = self.offer()
        self.run_at(DUE)
        after = self.offer()
        self.assertEqual(after["nudged"], utc(DUE))
        self.assertEqual((after["created"], after["expires"], after["slots"]),
                         (before["created"], before["expires"], before["slots"]))
        self.assertEqual(self.offers(), 1)
        (fu,) = self.followups()
        self.assertEqual((fu["state"], fu["outcome"]), ("done", "nudged"))

    def test_the_terminal_says_the_nudge_went(self):
        self.said[:] = []
        self.run_at(DUE)
        (line,) = [l for l in self.said if "nudge" in l]
        self.assertTrue(line.startswith("Thu Dec 3, 8:10 AM Pacific  Marcus Lee: nudge. T9 sent (one more try). "), line)

    def test_never_a_second_nudge(self):
        self.run_at(DUE)
        s = self.run_at("2026-12-04T16:10:00Z")
        self.assertEqual(self.templates_sent(MARCUS), ["T2", "T9"])
        self.assertEqual(self.lines(s, "decided", step="nudge"), [])
        self.assertEqual(s.sends, 0)

    def test_nothing_goes_before_it_is_due(self):
        s = self.run_at("2026-12-03T16:00:00Z")
        self.assertEqual(self.templates_sent(MARCUS), ["T2"])
        (fu,) = self.followups()
        self.assertEqual(fu["state"], "open")
        self.assertEqual(self.lines(s, "decided"), [])

    def test_a_time_that_went_busy_is_dropped_and_the_rest_go_again(self):
        self.busy(self.SLOTS[0])
        s = self.run_at(DUE)
        self.assertEqual(self.body(MARCUS, "T9")[:4], [
            "One more try: Shane is still available at any of these, 30 min each:",
            "1. Mon Dec 7, 9:30 AM Pacific (12:30 PM Eastern)",
            "2. Mon Dec 7, 10:00 AM Pacific (1:00 PM Eastern)",
            "After this I'll leave it with you."])
        (plan,) = self.lines(s, "decided", action="nudge")
        self.assertEqual([d["start"] for d in plan["payload"]["dropped"]], [self.SLOTS[0]])
        self.assertEqual([s.start_utc for s in self.offer()["slots"]], [utc(t) for t in self.SLOTS[1:]])

    def test_one_time_left_gets_the_one_slot_form(self):
        self.busy(self.SLOTS[0], self.SLOTS[2])
        self.run_at(DUE)
        self.assertEqual(self.body(MARCUS, "T9")[:2], [
            "One more try: Shane is still available at Mon Dec 7, 9:30 AM Pacific (12:30 PM Eastern), 30 min.",
            "Reply \"yes\" and I'll book it. After this I'll leave it with you."])

    def test_no_time_left_no_nudge(self):
        self.busy(*self.SLOTS)
        s = self.run_at(DUE)
        self.assertEqual(self.templates_sent(MARCUS), ["T2"])
        (done,) = self.lines(s, "decided", action="close_follow_up")
        self.assertEqual((done["why"]["code"], done["payload"]["rule"]), ("followup.no_nudge", "no_time_left"))
        self.assertEqual(self.lines(s, "checked"), [])
        (fu,) = self.followups()
        self.assertEqual((fu["state"], fu["outcome"]), ("done", "no_time_left"))

    def test_a_time_now_under_a_day_away_is_dropped_too(self):
        # Fri Dec 4, 9:15 AM Pacific: Monday's 9:00 AM is still more than a day off; nothing was dropped.
        # Sun Dec 6, 9:10 AM Pacific: 9:00 AM Monday is under 24 hours away, 9:30 and 10:00 are not.
        s = self.run_at("2026-12-06T17:10:00Z")
        (plan,) = self.lines(s, "decided", action="nudge")
        self.assertEqual([d["start"] for d in plan["payload"]["dropped"]], [self.SLOTS[0]])
        self.assertEqual(self.body(MARCUS, "T9")[1], "1. Mon Dec 7, 9:30 AM Pacific (12:30 PM Eastern)")

    def test_an_offer_that_expired_closes_the_entry_with_no_nudge(self):
        s = self.run_at("2026-12-08T16:10:00Z")                     # she was stopped past the offer's seven days
        self.assertEqual(self.templates_sent(MARCUS), ["T2"])
        (done,) = self.lines(s, "decided", action="close_follow_up")
        self.assertEqual(done["payload"]["rule"], "expired")

    def test_a_held_thread_gets_no_nudge(self):
        with self.ledger() as led:
            led.record_hold(MARCUS_KEY, None, "hold.unsure", None, utc("2026-12-02T16:00:00Z"))
        s = self.run_at(DUE)
        self.assertEqual(self.templates_sent(MARCUS), ["T2"])
        (done,) = self.lines(s, "decided", action="close_follow_up")
        self.assertEqual(done["payload"]["rule"], "thread_held")

    def test_someone_who_opted_out_gets_no_nudge(self):
        with self.ledger() as led:
            led.silence(MARCUS, "stop", "some-other-thread@brightlane.example", utc("2026-12-02T16:00:00Z"))
        s = self.run_at(DUE)
        self.assertEqual(self.templates_sent(MARCUS), ["T2"])
        (done,) = self.lines(s, "decided", action="close_follow_up")
        self.assertEqual(done["payload"]["rule"], "opted_out")

    def test_the_gate_checks_the_nudge_like_any_send_and_a_block_sends_nothing(self):
        real = loop._Run.check

        def blocked(run, e, action, *a, **k):
            g = real(run, e, action, *a, **k)
            if action.template == "T9":
                g.passed, g.first_fail = False, "G9"
            return g

        with mock.patch.object(loop._Run, "check", blocked):
            s = self.run_at(DUE)
        self.assertEqual(self.templates_sent(MARCUS), ["T2"])
        (done,) = self.lines(s, "decided", action="close_follow_up")
        self.assertEqual((done["payload"]["rule"], done["payload"]["check"]), ("gate_blocked", "G9"))
        self.assertEqual(self.lines(s, "called", call="send"), [])

    def test_a_cap_that_never_frees_closes_the_entry_and_is_not_tried_every_pass(self):
        """Fable review of PR #42, finding 8: the thread's sends ever are used up. Waiting would try,
        and write, at every pass until the offer expires; the entry closes with no nudge instead."""
        with mock.patch.object(gate, "SEND_CAPS", (("sends_thread_ever", 1),)):   # her offer was the thread's one send
            s = self.run_at(DUE)
            s2 = self.run_at("2026-12-03T16:20:00Z")
        self.assertEqual(self.templates_sent(MARCUS), ["T2"])
        (check,) = self.lines(s, "checked", template="T9")
        self.assertEqual((check["payload"]["passed"], check["payload"]["first_fail"]), (False, "G7"))
        (done,) = self.lines(s, "decided", action="close_follow_up")
        self.assertEqual((done["payload"]["rule"], done["payload"]["check"]), ("gate_blocked", "G7"))
        self.assertEqual(self.lines(s, "decided", action="nudge_waits"), [])
        self.assertEqual(self.lines(s2, "decided") + self.lines(s2, "checked") + self.lines(s2, "called", call="calendar_query"), [])
        (fu,) = self.followups()
        self.assertEqual((fu["state"], fu["outcome"]), ("done", "gate_blocked"))

    def test_a_send_cap_makes_the_nudge_wait_and_it_goes_once_the_cap_frees(self):
        with mock.patch.object(gate, "SEND_CAPS", (("sends_hour", 0),)):          # the hour's sends are used up
            s = self.run_at(DUE)
        self.assertEqual(self.templates_sent(MARCUS), ["T2"])
        (check,) = self.lines(s, "checked", template="T9")
        self.assertEqual((check["payload"]["passed"], check["payload"]["first_fail"]), (False, "G7"))
        (waits,) = self.lines(s, "decided", action="nudge_waits")
        self.assertEqual((waits["why"]["code"], waits["payload"]["rule"]), ("followup.nudge_waits", "gate_blocked"))
        (fu,) = self.followups()
        self.assertEqual(fu["state"], "open")
        self.run_at("2026-12-03T16:20:00Z")
        self.assertEqual(self.templates_sent(MARCUS), ["T2", "T9"])

    def test_a_calendar_that_does_not_answer_makes_the_nudge_wait(self):
        with mock.patch.object(sandbox_mod.SandboxCalendar, "query", side_effect=CalendarError("down")):
            s = self.run_at(DUE)
        self.assertEqual(self.templates_sent(MARCUS), ["T2"])
        (waits,) = self.lines(s, "decided", action="nudge_waits")
        self.assertEqual(waits["payload"]["rule"], "calendar_down")
        self.run_at("2026-12-03T16:20:00Z")
        self.assertEqual(self.templates_sent(MARCUS), ["T2", "T9"])

    def test_a_mail_server_that_refuses_it_makes_the_nudge_wait_and_it_goes_once(self):
        with mock.patch.object(sandbox_mod.SandboxMail, "send", side_effect=MailError("refused")):
            s = self.run_at(DUE)
        self.assertEqual(self.templates_sent(MARCUS), ["T2"])
        (waits,) = self.lines(s, "decided", action="nudge_waits")
        self.assertEqual(waits["payload"]["rule"], "not_sent")
        self.run_at("2026-12-03T16:20:00Z")
        self.assertEqual(self.templates_sent(MARCUS), ["T2", "T9"])
        self.run_at("2026-12-03T16:30:00Z")
        self.assertEqual(self.templates_sent(MARCUS), ["T2", "T9"])

    def test_a_kill_before_the_send_sends_it_once_on_the_restart(self):
        set_clock(self.root, DUE)
        self.crash_once(ledger_mod.Ledger, "set_send_state", self.at_the_sending_mark("T9"))
        self.assertEqual(self.templates_sent(MARCUS), ["T2"])
        self.run_once()
        self.assertEqual(self.templates_sent(MARCUS), ["T2", "T9"])
        (fu,) = self.followups()
        self.assertEqual((fu["state"], fu["outcome"]), ("done", "nudged"))
        self.run_at("2026-12-03T16:30:00Z")
        self.assertEqual(self.templates_sent(MARCUS), ["T2", "T9"])

    def test_a_kill_after_the_send_never_sends_it_twice(self):
        set_clock(self.root, DUE)
        self.crash_once(loop._Run, "look_in_sent", lambda self_, e, mid, template, *a, **k: template == "T9")
        self.assertEqual(self.templates_sent(MARCUS), ["T2", "T9"])
        s = self.run_once()
        self.assertEqual(self.templates_sent(MARCUS), ["T2", "T9"])
        self.assertEqual(self.lines(s, "called", call="send"), [])
        (done,) = self.lines(s, "decided", action="close_follow_up")
        self.assertEqual(done["why"]["code"], "followup.nudge_done")

    def test_a_nudge_that_went_and_is_not_in_sent_is_an_error_line_and_never_goes_again(self):
        real = sandbox_mod.SandboxMail.find_in_sent
        with mock.patch.object(sandbox_mod.SandboxMail, "find_in_sent", lambda mail, mid: False):
            s = self.run_at(DUE)
        self.assertEqual(self.templates_sent(MARCUS), ["T2", "T9"])   # the server took it; Sent never showed it
        (err,) = self.lines(s, "error", step="nudge")
        self.assertEqual(err["payload"]["template"], "T9")
        (done,) = self.lines(s, "decided", action="close_follow_up")
        self.assertEqual(done["payload"]["rule"], "send_unconfirmed")
        self.assertIs(real, sandbox_mod.SandboxMail.find_in_sent)
        self.run_at("2026-12-03T16:30:00Z")
        self.assertEqual(self.templates_sent(MARCUS), ["T2", "T9"])

    def test_a_failure_after_the_nudge_went_never_adds_a_close_line_that_says_no_nudge(self):
        """Fable review of PR #42, finding 7: the nudge went and is in Sent, then the terminal write
        fails. One error line, and the entry's one closing line still says its nudge is in Sent."""
        class Terminal(list):
            def append(self, line):
                if "T9 sent" in line:
                    raise OSError("terminal gone")
                list.append(self, line)

        self.said = Terminal()
        s = self.run_at(DUE)
        self.assertEqual(self.templates_sent(MARCUS), ["T2", "T9"])
        (done,) = self.lines(s, "decided", action="close_follow_up")
        self.assertEqual(done["why"]["code"], "followup.nudge_done")
        (err,) = self.lines(s, "error", step="follow_up")
        self.assertEqual(err["payload"]["follow_up_kind"], "nudge")
        (fu,) = self.followups()
        self.assertEqual((fu["state"], fu["outcome"]), ("done", "nudged"))

    def test_a_failure_before_the_nudge_went_still_closes_the_entry_with_no_nudge(self):
        with mock.patch.object(compose, "reply_envelope", side_effect=RuntimeError("boom")):
            s = self.run_at(DUE)
        self.assertEqual(self.templates_sent(MARCUS), ["T2"])
        (done,) = self.lines(s, "decided", action="close_follow_up")
        self.assertEqual((done["why"]["code"], done["payload"]["rule"]), ("followup.no_nudge", "internal_error"))
        self.assertEqual(len(self.lines(s, "error", step="follow_up")), 1)

    def test_a_failure_between_the_send_and_the_close_leaves_the_entry_for_her_next_pass(self):
        """The nudge is in Sent and the entry is still open: it is not closed as "no nudge". Her next
        pass looks in Sent and closes it as sent."""
        real = loop._Run.close_follow_up
        failed = []

        def fail_once(run, fu, code, *a, **k):
            if code == "followup.nudge_done" and not failed:
                failed.append(True)
                raise RuntimeError("boom")
            return real(run, fu, code, *a, **k)

        with mock.patch.object(loop._Run, "close_follow_up", fail_once):
            s = self.run_at(DUE)
            self.assertEqual(self.lines(s, "decided", action="close_follow_up"), [])
            (fu,) = self.followups()
            self.assertEqual(fu["state"], "open")
            s2 = self.run_at("2026-12-03T16:20:00Z")
        self.assertEqual(self.templates_sent(MARCUS), ["T2", "T9"])
        (done,) = self.lines(s2, "decided", action="close_follow_up")
        self.assertEqual(done["why"]["code"], "followup.nudge_done")

    def test_a_halt_before_the_nudge_names_it_as_dropped_and_resume_sends_it_once(self):
        from doris import supervisor
        real = loop._Run.deliver

        def stop_then_deliver(run, e, mid, kind, template, *a, **k):
            if template == "T9":
                supervisor.request_stop("throwaway", "cli", state_dir=self.state)
            return real(run, e, mid, kind, template, *a, **k)

        set_clock(self.root, DUE)
        with mock.patch.object(loop._Run, "deliver", stop_then_deliver):
            s = self.run_once()
        self.assertEqual((s.halted, self.templates_sent(MARCUS)), ("cli", ["T2"]))
        (halted,) = self.lines(s, "halted")
        self.assertEqual([(d["kind"], d["template"], d["message"]) for d in halted["payload"]["dropped"]],
                         [("nudge", "T9", MARCUS_KEY)])
        supervisor.resume("throwaway", state_dir=self.state)
        self.run_once()
        self.assertEqual(self.templates_sent(MARCUS), ["T2", "T9"])


class NudgeThatDidNotGo(NudgeSetup):
    """Fable review of PR #42, finding 2. Her ledger writes the nudge down before the send: the times
    it lists, and when. That counts only once the mail server took the nudge. Until then the offer
    reads as it went out, so a number or a yes means the list the sender has, never one nobody
    received; and while the nudge may or may not have gone, no number and no bare yes books."""

    def setUp(self):
        NudgeSetup.setUp(self)
        (_, self.t2) = self.sent(MARCUS)[0]

    def marcus_writes(self, text):
        tr.add(self.root, "21-marcus-pick.eml",
               answer("Marcus Lee", MARCUS, "20261203.0815.marcus.pick@brightlane.example",
                      "Thu, 03 Dec 2026 08:15:00 -0800", str(self.t2["Message-ID"]), MARCUS_KEY, text),
               {"forwarded": "no"})

    def booked(self):
        with self.ledger() as led:
            return [b["slot"].start_utc for b in led.bookings_for_thread(MARCUS_KEY) if b["state"] == "confirmed"]

    def refused_nudge(self):
        with mock.patch.object(sandbox_mod.SandboxMail, "send", side_effect=MailError("refused")):
            s = self.run_at(DUE)
        (waits,) = self.lines(s, "decided", action="nudge_waits")
        self.assertEqual(waits["payload"]["rule"], "not_sent")

    def resent_because(self, summary):
        """The rule word of the one plan that sent the open times again (T12) and booked nothing."""
        (plan,) = self.lines(summary, "decided", step="plan")
        self.assertEqual((plan["payload"]["action"], plan["why"]["code"]), ("resend", "policy.pick_resend"))
        return plan["why"]["text"].split("(", 1)[1].split(")", 1)[0]

    def test_a_refused_nudge_leaves_the_offer_as_it_went_out(self):
        self.busy(self.SLOTS[0])
        self.refused_nudge()
        o = self.offer()
        self.assertEqual(([s.start_utc for s in o["slots"]], o["nudged"], o["nudge"]),
                         ([utc(t) for t in self.SLOTS], None, None))

    def test_a_number_after_a_refused_nudge_means_the_first_mails_list(self):
        """Input A: 9:00 went busy, the nudge was refused, and Marcus, who has only the first mail,
        answers "Number 2 works." before her next try. 2 is 9:30, as his mail says."""
        self.busy(self.SLOTS[0])
        self.refused_nudge()
        self.marcus_writes("Number 2 works.")
        s = self.run_once()
        self.assertEqual(self.booked(), [utc(self.SLOTS[1])])
        self.assertEqual(self.closing(s, "marcus.pick"), ["book"])
        self.assertEqual(self.templates_sent(MARCUS), ["T2", "T1"])        # he answered: the nudge never goes
        self.run_at("2026-12-03T16:30:00Z")
        self.assertEqual(self.templates_sent(MARCUS), ["T2", "T1"])

    def test_a_number_after_a_stop_at_the_nudge_means_the_first_mails_list(self):
        """Input B: the stop lands at the nudge, the reply arrives while she is stopped."""
        from doris import supervisor
        real = loop._Run.deliver

        def stop_then_deliver(run, e, mid, kind, template, *a, **k):
            if template == "T9":
                supervisor.request_stop("throwaway", "cli", state_dir=self.state)
            return real(run, e, mid, kind, template, *a, **k)

        self.busy(self.SLOTS[0])
        set_clock(self.root, DUE)
        with mock.patch.object(loop._Run, "deliver", stop_then_deliver):
            s = self.run_once()
        self.assertEqual((s.halted, self.templates_sent(MARCUS)), ("cli", ["T2"]))
        self.marcus_writes("Number 2 works.")
        supervisor.resume("throwaway", state_dir=self.state)
        self.run_once()
        self.assertEqual(self.booked(), [utc(self.SLOTS[1])])
        self.assertEqual(self.templates_sent(MARCUS), ["T2", "T1"])

    def test_a_yes_after_a_refused_nudge_is_still_a_yes_to_three_times(self):
        """Input C: 9:00 and 10:00 went busy, the nudge (one time left) was refused, and Marcus says
        "Yes." to the three times he has. A yes to more than one time books nothing."""
        self.busy(self.SLOTS[0], self.SLOTS[2])
        self.refused_nudge()
        self.marcus_writes("Yes.")
        s = self.run_once()
        self.assertEqual((self.booked(), self.events()), ([], []))
        self.assertEqual(self.templates_sent(MARCUS), ["T2", "T12"])
        self.assertEqual(self.resent_because(s), "multiple")

    def test_the_refused_nudge_goes_at_her_next_try_with_the_times_available_then(self):
        """What a refused try wrote down is no part of the offer: the next try reads the offer's own
        three times afresh, so a time that came free again is listed again."""
        self.busy(self.SLOTS[0])
        self.refused_nudge()
        os.remove(os.path.join(self.root, "calendar", "owner-busy-test-0.ics"))     # 9:00 is free again
        self.run_at("2026-12-03T16:20:00Z")
        self.assertEqual(self.templates_sent(MARCUS), ["T2", "T9"])
        self.assertEqual(self.body(MARCUS, "T9")[1:4], ["1. Mon Dec 7, 9:00 AM Pacific (12:00 PM Eastern)",
                                                        "2. Mon Dec 7, 9:30 AM Pacific (12:30 PM Eastern)",
                                                        "3. Mon Dec 7, 10:00 AM Pacific (1:00 PM Eastern)"])
        o = self.offer()
        self.assertEqual((len(o["slots"]), o["nudged"], o["nudge"]), (3, utc("2026-12-03T16:20:00Z"), "sent"))

    def cut_off_mid_send(self):
        """A kill inside the transport's send of the nudge: it may have gone. The restart finds no
        copy in Sent on two looks, so it stays unknown and is never sent again."""
        set_clock(self.root, DUE)
        self.crash_once(sandbox_mod.SandboxMail, "send", lambda mail, raw, envelope, mid, **k: True)
        with self.ledger() as led:
            (state,) = [r["state"] for r in led._all("SELECT state FROM sends WHERE template = 'T9'")]
        self.assertEqual(state, "sending")

    def test_a_number_while_the_nudge_may_have_gone_gets_the_times_again(self):
        self.busy(self.SLOTS[0])
        self.cut_off_mid_send()
        self.marcus_writes("Number 2 works.")
        s = self.run_once()
        self.assertEqual((self.booked(), self.events()), ([], []))          # 2 is 9:30 or 10:00: nobody can tell
        self.assertEqual(self.templates_sent(MARCUS), ["T2", "T12"])
        self.assertEqual(self.body(MARCUS, "T12")[1:3], ["1. Mon Dec 7, 17:30 UTC (9:30 AM Pacific)",
                                                         "2. Mon Dec 7, 18:00 UTC (10:00 AM Pacific)"])
        self.assertEqual(self.resent_because(s), "ambiguous")

    def marcus_answers_the_t12(self, text):
        (_, t12) = [(t, m) for t, m in self.sent(MARCUS) if t == "T12"][0]
        tr.add(self.root, "22-marcus-pick.eml",
               answer("Marcus Lee", MARCUS, "20261203.0900.marcus.again@brightlane.example",
                      "Thu, 03 Dec 2026 09:00:00 -0800", str(t12["Message-ID"]), MARCUS_KEY, text),
               {"forwarded": "no"})

    def test_once_the_times_went_again_a_number_books_as_that_mail_numbered_them(self):
        """T12 ends "Reply with the number of the one that suits you and I'll book it." Once the mail
        server took it he holds its list for sure, whatever became of the nudge: the doubt is over."""
        self.busy(self.SLOTS[0])
        self.cut_off_mid_send()
        self.assertEqual(self.offer()["nudge"], "in_doubt")
        self.marcus_writes("Number 2 works.")
        self.run_once()
        self.assertEqual((self.templates_sent(MARCUS), self.booked()), (["T2", "T12"], []))
        o = self.offer()
        self.assertEqual(([x.start_utc for x in o["slots"]], o["nudge"]), ([utc(t) for t in self.SLOTS[1:]], "settled"))
        self.marcus_answers_the_t12("Number 2 works.")
        s = self.run_once()
        self.assertEqual(self.booked(), [utc(self.SLOTS[2])])              # the T12's 2: 10:00
        self.assertEqual(self.templates_sent(MARCUS), ["T2", "T12", "T1"])
        self.assertEqual(self.closing(s, "marcus.again"), ["book"])

    def test_once_the_one_time_went_again_a_yes_books_it(self):
        self.busy(self.SLOTS[0], self.SLOTS[2])
        self.cut_off_mid_send()
        self.marcus_writes("Yes.")
        self.run_once()
        self.assertEqual((self.templates_sent(MARCUS), self.booked()), (["T2", "T12"], []))
        self.marcus_answers_the_t12("Yes.")
        self.run_once()
        self.assertEqual(self.booked(), [utc(self.SLOTS[1])])
        self.assertEqual(self.templates_sent(MARCUS), ["T2", "T12", "T1"])

    def test_a_nudge_in_doubt_that_left_no_time_out_never_holds_up_a_number(self):
        """Nothing went busy, so the nudge listed the offer's three times in the same order: a number
        means the same time on either list, and it books at once."""
        self.cut_off_mid_send()
        self.assertEqual(self.offer()["nudge"], "settled")
        self.marcus_writes("Number 2 works.")
        self.run_once()
        self.assertEqual(self.booked(), [utc(self.SLOTS[1])])
        self.assertEqual(self.templates_sent(MARCUS), ["T2", "T1"])

    def test_a_yes_while_the_nudge_may_have_gone_gets_the_time_again(self):
        self.busy(self.SLOTS[0], self.SLOTS[2])
        self.cut_off_mid_send()
        self.marcus_writes("Yes.")
        s = self.run_once()
        self.assertEqual((self.booked(), self.events()), ([], []))          # yes to three times, or to the one left
        self.assertEqual(self.templates_sent(MARCUS), ["T2", "T12"])
        self.assertEqual(self.resent_because(s), "ambiguous")
        self.assertEqual(self.body(MARCUS, "T12")[1:3], ["1. Mon Dec 7, 17:30 UTC (9:30 AM Pacific)",
                                                         "Reply with the number of the one that suits you and I'll book it."])


class NudgeAndTheirAnswer(Base):
    names = ("02-marcus-request.eml",)

    def setUp(self):
        Base.setUp(self)
        tr.edit(self.root, "02-marcus-request.eml", lambda raw: raw.replace(THIS_WEEK, NEXT_WEEK))
        self.run_once()
        (_, self.t2) = self.sent(MARCUS)[0]

    def marcus_writes(self, name, mid, date, irt, text):
        tr.add(self.root, name, answer("Marcus Lee", MARCUS, mid, date, irt, MARCUS_KEY, text), {"forwarded": "no"})

    def test_a_pick_closes_the_entry_and_no_nudge_ever_goes(self):
        self.marcus_writes("20-marcus-pick.eml", "20261201.1000.marcus.pick@brightlane.example",
                           "Tue, 01 Dec 2026 10:00:00 -0800", str(self.t2["Message-ID"]), "The first one works.")
        s = self.run_once()
        self.assertEqual(self.templates_sent(MARCUS), ["T2", "T1"])
        (done,) = self.lines(s, "decided", action="close_follow_up")
        self.assertEqual((done["why"]["code"], done["payload"]["rule"]), ("followup.no_nudge", "offer_closed"))
        self.run_at(DUE)
        self.assertEqual(self.templates_sent(MARCUS), ["T2", "T1"])
        (line,) = [l for l in self.said if l.split("  ", 1)[1].startswith("Follow-ups:")]
        self.assertIn("Follow-ups: 1 closed (offer answered or closed: 1). Still open: 1 booking check.", line)

    def test_anything_they_wrote_since_the_offer_means_no_nudge(self):
        # an auto-reply: filed with no answer, the offer stays open, and still nobody is nudged
        self.marcus_writes("20-marcus-away.eml", "20261201.1000.marcus.away@brightlane.example",
                           "Tue, 01 Dec 2026 10:00:00 -0800", str(self.t2["Message-ID"]),
                           "I am out of the office until Monday.")
        tr.edit(self.root, "20-marcus-away.eml",
                lambda raw: raw.replace(b"MIME-Version: 1.0", b"Auto-Submitted: auto-replied\r\nMIME-Version: 1.0"))
        s = self.run_once()
        with self.ledger() as led:
            self.assertEqual(len(led.open_offers(MARCUS_KEY, utc("2026-12-01T18:10:00Z"))), 1)
        (done,) = self.lines(s, "decided", action="close_follow_up")
        self.assertEqual(done["payload"]["rule"], "they_wrote")
        self.run_at(DUE)
        self.assertEqual(self.templates_sent(MARCUS), ["T2"])

    def test_a_stop_closes_the_entry(self):
        self.marcus_writes("20-marcus-stop.eml", "20261201.1000.marcus.stop@brightlane.example",
                           "Tue, 01 Dec 2026 10:00:00 -0800", str(self.t2["Message-ID"]), "stop")
        s = self.run_once()
        (done,) = self.lines(s, "decided", action="close_follow_up")
        self.assertEqual(done["why"]["code"], "followup.no_nudge")
        self.run_at(DUE)
        self.assertEqual(self.templates_sent(MARCUS), ["T2"])

    def test_a_number_after_the_nudge_books_the_time_the_nudge_gave_that_number(self):
        end = "20261207T173000Z"
        tr.owner_busy(self.root, "owner-busy-test", "20261207T170000Z", end)   # her first time went busy
        self.run_at(DUE)
        (_, t9) = self.sent(MARCUS)[1]
        self.marcus_writes("21-marcus-pick.eml", "20261203.0900.marcus.pick@brightlane.example",
                           "Thu, 03 Dec 2026 09:00:00 -0800", str(t9["Message-ID"]), "Number 1 works.")
        s = self.run_once()
        self.assertEqual(self.templates_sent(MARCUS), ["T2", "T9", "T1"])
        self.assertEqual(self.closing(s, "marcus.pick"), ["book"])
        with self.ledger() as led:
            (b,) = led.bookings_for_thread(MARCUS_KEY)
        self.assertEqual(b["slot"].start_utc, utc("2026-12-07T17:30:00Z"))     # the nudge's 1, the offer's 2

    def test_known_limit_a_number_written_after_the_nudge_is_read_on_the_nudges_list_whichever_mail_it_answers(self):
        # AGENT.md, "Limits we state": after a nudge that dropped a time, the numbers are the nudge's.
        # This reply answers the first offer mail, where "2" was 17:30 UTC; it is read as the nudge's 2.
        end = "20261207T173000Z"
        tr.owner_busy(self.root, "owner-busy-test", "20261207T170000Z", end)   # her first time went busy
        self.run_at(DUE)
        with self.ledger() as led:
            (offer,) = led.open_offers(MARCUS_KEY, utc("2026-12-03T16:20:00Z"))
        (nudge_1, nudge_2) = [s.start_utc for s in offer["slots"]]
        self.assertEqual(nudge_1, utc("2026-12-07T17:30:00Z"))                 # the first mail's 2
        self.marcus_writes("21-marcus-pick.eml", "20261203.0900.marcus.pick@brightlane.example",
                           "Thu, 03 Dec 2026 09:00:00 -0800", str(self.t2["Message-ID"]), "Number 2 works.")
        s = self.run_once()
        self.assertEqual(self.closing(s, "marcus.pick"), ["book"])
        with self.ledger() as led:
            (b,) = led.bookings_for_thread(MARCUS_KEY)
        self.assertEqual(b["slot"].start_utc, nudge_2)
        self.assertEqual(nudge_2, utc("2026-12-07T18:00:00Z"))
        # and what makes the limit bearable: her confirmation names the time she booked
        self.assertEqual(self.body(MARCUS, "T1")[0],
                         "You're booked with Shane Bornstein for Mon Dec 7, 10:00 AM Pacific (18:00 UTC), 30 min.")

    def test_a_number_written_before_the_nudge_and_read_after_it_gets_the_times_again(self):
        end = "20261207T173000Z"
        tr.owner_busy(self.root, "owner-busy-test", "20261207T170000Z", end)
        self.run_at(DUE)
        # written at 8:05 AM, five minutes before her nudge went; she reads it at her next cycle
        self.marcus_writes("21-marcus-pick.eml", "20261203.0805.marcus.pick@brightlane.example",
                           "Thu, 03 Dec 2026 08:05:00 -0800", str(self.t2["Message-ID"]), "Number 2 works.")
        s = self.run_at("2026-12-03T16:00:00Z")
        self.assertEqual(self.events(), [])                                    # "2" meant another time before her nudge
        self.assertEqual(self.templates_sent(MARCUS), ["T2", "T9", "T12"])
        self.assertEqual(self.closing(s, "marcus.pick"), ["offer"])


# ---------------------------------------------------------------------------------------------
# The readers: replay, the watch page and a live trace's timeline, on the same receipts
# ---------------------------------------------------------------------------------------------
def replayed(summary):
    return replay.render(receipts.read_lines(summary.receipts_path), summary.receipts_path)


def watched(summary):
    return [watch.row(line) for line in receipts.read_lines(summary.receipts_path)]


def timeline(summary):
    return livetrace.timeline(receipts.read_lines(summary.receipts_path))


NUDGE_ENTRY = "entry 1 in her ledger (the one nudge, due Thu Dec 3, 8:10 AM Pacific)"


class ReadersFiled(Base):
    names = ("02-marcus-request.eml",)

    def setUp(self):
        Base.setUp(self)
        self.s = self.run_once()

    def test_replay_says_the_follow_up_filed_and_names_it_on_the_closing_line(self):
        out = replayed(self.s)
        self.assertIn("  Follow-up filed: %s; the offer expires Tue Dec 8, 8:10 AM Pacific. Why (followup.nudge_filed): "
                      "Follow-up filed in her ledger: one nudge at 2026-12-03T16:10Z" % NUDGE_ENTRY, out)
        self.assertIn("offer times. Follow-up filed: %s. [seq " % NUDGE_ENTRY, out)
        self.assertNotIn("Decided   file_follow_up", out)

    def test_the_watch_page_says_it(self):
        (filed,) = [r for r in watched(self.s) if r["code"] == "followup.nudge_filed"]
        self.assertEqual(filed["what"], "Follow-up filed: one nudge, due Thu Dec 3, 8:10 AM Pacific.")
        (closed,) = [r["what"] for r in watched(self.s) if r["what"].startswith("Closed: ")]
        self.assertEqual(closed, "Closed: offer, T2. Follow-up filed: one nudge, due Thu Dec 3, 8:10 AM Pacific.")

    def test_the_live_timeline_says_it(self):
        (line,) = [t for t in timeline(self.s) if "follow-up" in t]
        self.assertTrue(line.endswith("follow-up filed: one nudge, due 2026-12-03T16:10:00Z"), line)


class ReadersGone(GoneSetup):
    def setUp(self):
        GoneSetup.setUp(self)
        self.still = self.run_once()                    # one cycle with her booking still there
        self.owner_deletes()
        self.s = self.run_once()
        self.who = "Priya Raman <%s>" % PRIYA

    def test_replay_calls_it_her_calendar_check_and_never_a_mail(self):
        out = replayed(self.s)
        self.assertIn("\nItem 1 of 1: her calendar check, no mail: her booking %s with %s is gone from the "
                      "calendar\n" % (self.bid, self.who), out)
        self.assertIn("  Saw       her booking %s (Wed Dec 2, 11:00 AM Pacific to Wed Dec 2, 11:30 AM Pacific, with %s) "
                      "is gone from the calendar: missing on 2 reads a short wait apart, so it is taken as removed by "
                      "the owner. It was booked for Message-ID %s. No mail was read for this: it is her own check of "
                      "the calendar." % (self.bid, self.who, PRIYA_KEY), out)
        self.assertNotIn("\nMessage ", out)
        self.assertNotIn("mail from", out)

    def test_replay_shows_her_two_reads_the_notice_and_the_one_held_line(self):
        out = replayed(self.s)
        first = "  Called    her calendar check: is booking %s still there -> missing" % self.bid
        again = ("  Called    her calendar check: is booking %s still there (read again, a short wait after it was "
                 "missing) -> missing" % self.bid)
        sent = "  Called    send S1 to the owner"
        held = ("  Held      hold:owner_removed. Why (hold.owner_removed): Held: the owner removed one of her bookings; "
                "the prospect was not told. Hold notice to the owner: sent (Message-ID <")
        for part in (first, again, sent, held):
            self.assertIn(part, out)
        self.assertLess(out.index(sent), out.index(held))
        self.assertNotIn("send S1 to the sender", out)
        self.assertNotIn(" to the sender", out)

    def test_replay_says_a_booking_still_there_is_there_and_that_an_edit_is_not_checked(self):
        out = replayed(self.still)
        self.assertIn("  Verified  her bookings are still on the calendar, her check once a cycle: 1 read, each found "
                      "its booking there (%s 1 time). An edit is not checked. [seq " % self.bid, out)
        self.assertNotIn("\nItem ", out)

    def test_the_watch_page_says_it(self):
        rows = watched(self.s)
        (saw,) = [r for r in rows if r["step"] == "calendar check"]
        self.assertEqual(saw["what"], "Her calendar check, no mail: her booking %s with %s is gone from the calendar "
                                      "(missing on 2 reads)." % (self.bid, self.who))
        self.assertEqual(saw["message"], self.subject)
        (held,) = [r for r in rows if r["step"] == "held"]
        self.assertEqual(held["what"], "Held for the owner: hold:owner_removed. Hold notice sent to the owner; nothing "
                                       "sent to the prospect.")
        self.assertEqual([r["what"] for r in rows if r["what"].startswith("Read a message")], [])
        reads = [r["what"] for r in rows if r["what"].startswith("calendar_get")]
        self.assertEqual(reads, [
            "calendar_get %s, her check that the booking is still on the calendar." % self.bid,
            "calendar_get %s, her check that the booking is still on the calendar (read again after a short wait)."
            % self.bid])

    def test_the_live_timeline_says_it(self):
        lines = timeline(self.s)
        said = [t.split("  ", 2)[2] for t in lines[1:]]
        self.assertIn("her calendar check (no mail): booking %s is gone from the calendar" % self.bid, said)
        self.assertIn("held hold:owner_removed: hold notice to the owner sent, nothing to the prospect", said)
        self.assertNotIn("message read", said)

    def test_every_reader_counts_it_as_one_held_item(self):
        lines = receipts.read_lines(self.s.receipts_path)
        want = {"messages": 1, "handled": 0, "held": 1, "human_approvals": 0, "sends": 1, "errors": 0}
        for counted in (replay._counted(lines), livetrace.counts(lines)):
            self.assertEqual({k: counted[k] for k in want}, want)
        self.assertEqual(loop.count_outcomes(lines), {"handled": 0, "held": 1, "human_approvals": 0})


class ReadersNudge(NudgeSetup):
    ABOUT = " for %s" % MARCUS

    def test_replay_says_the_nudge_due_who_it_goes_to_and_the_times_it_lists(self):
        out = replayed(self.run_at(DUE))
        due = ("  Follow-up due%s: %s. The nudge (T9) goes on thread %s with 3 of the offer's times still available "
               "(Mon Dec 7, 9:00 AM Pacific; Mon Dec 7, 9:30 AM Pacific; Mon Dec 7, 10:00 AM Pacific); dropped: none. "
               "Why (followup.nudge): Follow-up due: the offer went two business days with no answer"
               % (self.ABOUT, NUDGE_ENTRY, MARCUS_KEY))
        sent = "  Called    send T9 to the sender, BCC the owner"
        done = ("  Follow-up closed%s: %s. Why (followup.nudge_done): Follow-up closed: its one nudge is in Sent."
                % (self.ABOUT, NUDGE_ENTRY))
        for part in (due, sent, done):
            self.assertIn(part, out)
        self.assertLess(out.index(due), out.index(sent))
        self.assertLess(out.index(sent), out.index(done))
        self.assertNotIn("Decided   ", out)

    def test_replay_names_a_time_that_was_dropped(self):
        self.busy(self.SLOTS[0])
        out = replayed(self.run_at(DUE))
        self.assertIn("with 2 of the offer's times still available (Mon Dec 7, 9:30 AM Pacific; Mon Dec 7, 10:00 AM "
                      "Pacific); dropped: Mon Dec 7, 9:00 AM Pacific.", out)

    def test_replay_says_why_an_entry_closed_with_no_nudge(self):
        self.busy(*self.SLOTS)
        out = replayed(self.run_at(DUE))
        self.assertIn("  Follow-up closed%s: %s: no offered time still available. Why (followup.no_nudge): Follow-up "
                      "closed with no nudge (no_time_left)." % (self.ABOUT, NUDGE_ENTRY), out)

    def test_replay_says_a_nudge_that_waits_and_why(self):
        with mock.patch.object(sandbox_mod.SandboxCalendar, "query", side_effect=CalendarError("down")):
            s = self.run_at(DUE)
        self.assertIn("  Follow-up due%s, and the nudge waits (the calendar did not answer): %s. Why "
                      "(followup.nudge_waits): Follow-up due, but the nudge waits (calendar_down): she tries again at "
                      "her next cycle." % (self.ABOUT, NUDGE_ENTRY), replayed(s))
        self.assertIn("Follow-up due, and the nudge waits (the calendar did not answer). She tries again at her next "
                      "cycle.", [r["what"] for r in watched(s)])
        self.assertTrue(any(t.endswith("follow-up due, and the nudge waits (the calendar did not answer)")
                            for t in timeline(s)))

    def test_the_watch_page_says_it(self):
        what = [r["what"] for r in watched(self.run_at(DUE)) if r["what"].startswith("Follow-up")]
        self.assertEqual(what, ["Follow-up due: the one nudge (T9) goes with 3 of the offer's times still available.",
                                "Follow-up closed: one nudge, due Thu Dec 3, 8:10 AM Pacific."])

    def test_the_watch_page_says_why_an_entry_closed_with_no_nudge(self):
        self.busy(*self.SLOTS)
        what = [r["what"] for r in watched(self.run_at(DUE)) if r["what"].startswith("Follow-up")]
        self.assertEqual(what, ["Follow-up closed: one nudge, due Thu Dec 3, 8:10 AM Pacific (no offered time still "
                                "available)."])

    def test_the_live_timeline_says_it(self):
        said = [t.split("  ", 2)[2] for t in timeline(self.run_at(DUE))[1:]]
        self.assertEqual(said, ["run started: mode throwaway, model stub",
                                "follow-up due: the one nudge (T9) goes with 3 of the offer's times",
                                "gate passed", "send T9 to the sender: handed_over", "her copy in Sent: found",
                                "follow-up closed: one nudge", "summary: 0 handled, 0 held, 1 sent"])


class ReadersOddShapes(unittest.TestCase):
    """A forged or damaged follow-up line is shown as recorded: no reader crashes on it or trusts it."""
    ODD = [{"action": "file_follow_up"}, {"action": "close_follow_up", "follow_up": "x", "rule": ["a"], "sender": 7},
           {"action": "nudge", "kept": "soon", "dropped": [3, None], "follow_up": {"kind": [], "due": {}}},
           {"action": "nudge_waits", "rule": None, "follow_up": {"kind": "nudge", "due": "never"}},
           {"action": ["nudge"]}, {"action": "close_follow_up", "follow_up": {"kind": "booking_check", "booking_id": 5}}]

    def lines(self):
        why_ = {"code": "followup.no_nudge", "text": "x"}
        out = [{"seq": n + 1, "ts": "2026-12-03T16:10:00Z", "kind": "decided", "subject": "", "payload": p, "why": why_}
               for n, p in enumerate(self.ODD)]
        out.append({"seq": 90, "ts": "2026-12-03T16:10:00Z", "kind": "observed", "subject": "booking:x",
                    "payload": {"step": "booking_check", "slot": "soon", "reads": "two", "from_addr": ["a"]}})
        out.append({"seq": 91, "ts": "2026-12-03T16:10:00Z", "kind": "called", "subject": "",
                    "payload": {"call": "calendar_get", "target": {}, "booking_check": "x"}})
        out.append({"seq": 92, "ts": "2026-12-03T16:10:00Z", "kind": "result", "subject": "",
                    "payload": {"call_seq": 91, "ok": True, "found": "maybe"}})
        return out

    def test_replay_shows_every_one_and_never_crashes(self):
        out = replay.render(self.lines(), "receipts.jsonl")
        self.assertEqual(out.count("\n  Follow-up "), 5)
        self.assertIn("  Decided   ", out)              # an action that is not a word is no follow-up line
        self.assertIn("Item 1 of 1: her calendar check, no mail: her booking not recorded with", out)
        self.assertIn("not recorded whether found", out)
        self.assertNotIn("shown as recorded", out)

    def test_the_watch_page_and_the_timeline_never_crash(self):
        rows = [watch.row(line) for line in self.lines()]
        self.assertEqual(len(rows), len(self.lines()))
        self.assertIn("Decided.", [r["what"] for r in rows])
        self.assertEqual(len(livetrace.timeline(self.lines())), 1 + 6 + 1)


if __name__ == "__main__":
    unittest.main()
