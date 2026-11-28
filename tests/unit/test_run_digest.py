"""`python -m doris run`'s digest (templates.md, S2 digest): at her first pass at or after 5:00 PM
Pacific, that Pacific date's S2 to the owner, once, never two for one date.

Each test runs the real loop in this process as `run` does (until stopped, the digest on) on a mini
sandbox whose clock.txt is set to the evening. Her waits between passes take no time: each later
pass is one ten-minute cycle on the sandbox's advancing clock, and a stop comes in the wait after
the passes a test asks for (so every run here ends on its halted line and its summary).
"""

import email
import email.policy
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from doris import compose, digest, loop, paths, receipts, supervisor
from doris import ledger as ledger_mod
from doris.adapters import sandbox as sandbox_mod
from doris.shapes import MailError
from tests.unit.test_restart import Crash, add, mini_sandbox

OWNER = "whizz@fastmail.com"
DORIS = "doris.htm@fastmail.com"
ELENA = "11-elena-pricing.eml"         # held: money (and its notice goes to the owner)
ELENA_HELD = "Elena Voss, money, legal or a complaint, Tue Dec 1, %s PM Pacific"
TUE_440PM = "2026-12-02T00:40:00Z"     # Tue Dec 1, 4:40 PM Pacific
TUE_450PM = "2026-12-02T00:50:00Z"
TUE_7PM = "2026-12-02T03:00:00Z"
AT_TUE_5PM = "Wed, 02 Dec 2026 01:00:00 +0000"     # a Date header: Tue Dec 1, 5:00 PM Pacific
AT_WED_5PM = "Thu, 03 Dec 2026 01:00:00 +0000"
CAP = 400                              # passes: a run whose stop never comes ends here


def digest_mid(day):
    """Her Message-ID for one Pacific date's digest ("2026-12-01")."""
    return compose.new_message_id(DORIS, "digest:" + day, "S2", 1)


class NoWait(object):
    """loop._time and loop._sleep: each wait for her next cycle is one sleep that takes no time,
    after which waited() runs. wall is the files' clock (the sandbox adapter's): her waits pass on it
    as they would for real, so a mail written during one is older than two seconds at her next pass."""

    def __init__(self, waited):
        self.now, self.waited = 1000.0, waited

    def time(self):
        return self.now

    def wall(self):
        return time.time() + (self.now - 1000.0)

    def sleep(self, seconds):
        self.now += seconds
        self.waited()


class LongRun(unittest.TestCase):
    names = ()                         # the demo inbox files her mini sandbox starts with

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="doris-run-digest-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = mini_sandbox(self.tmp, self.names)
        self.state = os.path.join(self.tmp, "state")
        self.said = []
        self.n = 0

    def clock(self, iso):
        """Her sandbox clock starts here (clock.txt)."""
        with open(os.path.join(self.root, "clock.txt"), "w", encoding="ascii") as fh:
            fh.write(iso + "\n")

    def config(self, **kw):
        self.n += 1
        fields = dict(mode="throwaway", run_name="run-%d" % self.n, state_dir=self.state, sandbox_root=self.root,
                      owner_addr=OWNER, doris_addr=DORIS, model_source="stub", clock_mode="advance",
                      retry_wait_s=0, say=self.said.append)
        fields.update(kw)
        return loop.RunConfig(**fields)

    def run_once(self):
        """A finite run on the same state: one pass, no digest."""
        return loop.run(self.config())

    def elena_writes(self):
        """Elena's pricing question lands in the inbox (its Date is earlier: her next pass reads it)."""
        with open(os.path.join(paths.SANDBOX, "inbox", ELENA), "rb") as fh:
            add(self.root, ELENA, fh.read(), {"forwarded": "yes"})

    def run_until_stopped(self, passes=None, until=None, during=None):
        """One `run` (until stopped, the digest on). during(n) runs in the wait after pass n; a stop
        comes in the wait after pass `passes`, or after the first pass n for which until(n) is true."""
        waits = []

        def waited():
            waits.append(True)
            n = len(waits)
            if during is not None:
                during(n)
            if n == passes or (until is not None and until(n)) or n >= CAP:
                supervisor.request_stop("throwaway", "cli", state_dir=self.state)

        fake = NoWait(waited)
        with mock.patch.object(loop, "_time", fake.time), mock.patch.object(loop, "_sleep", fake.sleep), \
                mock.patch.object(sandbox_mod, "_wall", fake.wall):
            try:
                return loop.run(self.config(digest=True, cycles=None, cycle_s=1.0))
            finally:
                supervisor.resume("throwaway", state_dir=self.state)

    def digests(self):
        """Every digest in her Sent folder (the outbox), oldest first: (Date header, body lines)."""
        out = []
        outbox = os.path.join(self.root, "outbox")
        for name in sorted(os.listdir(outbox)):
            if name.endswith("-S2.eml"):
                with open(os.path.join(outbox, name), "rb") as fh:
                    m = email.message_from_bytes(fh.read(), policy=email.policy.default)
                out.append((str(m["Date"]), m.get_content().splitlines()))
        return out

    def digest_checks(self, summary):
        """The gate's checked lines for a digest, in one run's receipts."""
        return [l for l in receipts.read_lines(summary.receipts_path)
                if l["kind"] == "checked" and l["payload"]["action"] == "send_digest"]

    def send_state(self, day):
        """The ledger's state for that date's digest send, or None when it has none."""
        with ledger_mod.Ledger(os.path.join(self.state, "ledger.sqlite")) as db:
            row = db.send(digest_mid(day))
        return None if row is None else row["state"]


class DueAtFive(LongRun):
    def test_her_first_pass_from_five_pacific_sends_the_days_digest_and_none_before(self):
        self.clock(TUE_440PM)
        s = self.run_until_stopped(passes=4)              # 4:40, 4:50, 5:00, 5:10 PM
        self.assertEqual(s.halted, "cli")
        self.assertEqual(self.digests(), [(AT_TUE_5PM, [
            "Handled 0, held 0, human approvals 0. Tue Dec 1", "Receipts verify: yes.", "",
            "Doris, AI assistant to Shane Bornstein, Hire the Machines.",
            "I'm an AI, not a person. Reply \"stop\" to opt out."])])
        self.assertEqual(len(self.digest_checks(s)), 1)
        self.assertEqual(self.send_state("2026-12-01"), "confirmed")
        self.assertIn("Tue Dec 1, 5:00 PM Pacific  Digest sent to the owner: "
                      "Handled 0, held 0, human approvals 0.", self.said)    # the date is the line's own, once
        self.assertTrue(receipts.verify(s.receipts_path, finished=True).ok)

    def test_one_digest_for_each_pacific_date_however_many_passes(self):
        self.clock(TUE_440PM)
        s = self.run_until_stopped(passes=150)            # Tue 4:40 PM to Wed 5:30 PM
        self.assertEqual([(date, body[0]) for date, body in self.digests()],
                         [(AT_TUE_5PM, "Handled 0, held 0, human approvals 0. Tue Dec 1"),
                          (AT_WED_5PM, "Handled 0, held 0, human approvals 0. Wed Dec 2")])
        # the ledger answers first: no other pass composed or checked a digest
        self.assertEqual(len(self.digest_checks(s)), 2)

    def test_a_run_started_after_five_sends_that_days_digest_on_its_first_pass_once(self):
        self.clock(TUE_7PM)
        self.run_until_stopped(passes=3)                  # 7:00, 7:10, 7:20 PM
        self.assertEqual([(date, body[0]) for date, body in self.digests()],
                         [("Wed, 02 Dec 2026 03:00:00 +0000", "Handled 0, held 0, human approvals 0. Tue Dec 1")])


class NeverTwice(LongRun):
    def test_a_restart_the_same_evening_sends_nothing_more(self):
        self.clock(TUE_450PM)
        self.run_until_stopped(passes=3)                  # 4:50, 5:00 (the digest), 5:10 PM
        again = self.run_until_stopped(passes=3)          # restarted: 4:50, 5:00, 5:10 PM again
        self.assertEqual(len(self.digests()), 1)
        self.assertEqual(self.digest_checks(again), [])
        self.assertEqual([l for l in receipts.read_lines(again.receipts_path)
                          if l["kind"] == "called" and l["payload"]["call"] in ("send", "sent_lookup")], [])

    def test_a_finite_run_whose_days_digest_went_in_an_earlier_run_says_so_and_claims_no_send(self):
        self.clock(TUE_450PM)
        loop.run(self.config(digest=True))                # the demo's shape: one pass, then the day's digest
        self.assertEqual(len([l for l in self.said if "Digest sent to the owner: " in l]), 1, self.said)
        del self.said[:]
        again = loop.run(self.config(digest=True))        # the same day again on the same ledger
        self.assertEqual(len(self.digests()), 1)
        self.assertEqual([l for l in receipts.read_lines(again.receipts_path)
                          if l["kind"] == "called" and l["payload"]["call"] == "send"], [])
        self.assertEqual([l for l in self.said if "Digest" in l],
                         ["Tue Dec 1, 5:00 PM Pacific  Digest already sent to the owner earlier today; "
                          "not sent twice."])

    def test_a_kill_after_the_hand_over_before_the_record_sends_nothing_more(self):
        self.clock(TUE_450PM)
        real = sandbox_mod.SandboxMail.send

        def handed_over_then_killed(mail, raw, envelope, mid, template=None):
            real(mail, raw, envelope, mid, template=template)
            if template == "S2":
                raise Crash("killed after the server took the digest")

        with mock.patch.object(sandbox_mod.SandboxMail, "send", handed_over_then_killed):
            with self.assertRaises(Crash):
                self.run_until_stopped(passes=3)
        self.assertEqual(self.send_state("2026-12-01"), "sending")     # the hand-over never written down
        self.run_until_stopped(passes=3)                  # restarted: 4:50, 5:00, 5:10 PM
        self.assertEqual(len(self.digests()), 1)
        self.assertEqual(self.send_state("2026-12-01"), "confirmed")   # found in Sent before anything new

    def test_a_kill_inside_the_send_with_no_copy_in_sent_never_sends_that_digest(self):
        # review 2, finding 1: marked sending, the transport may have it; Sent shows none after two
        # looks, so it may have gone and that date's digest never goes again (the next one counts on
        # from the last digest the server took, so nothing it would have listed is lost)
        self.clock(TUE_450PM)

        def killed_inside_the_send(mail, raw, envelope, mid, template=None):
            raise Crash("killed inside the digest's send")

        with mock.patch.object(sandbox_mod.SandboxMail, "send", killed_inside_the_send):
            with self.assertRaises(Crash):
                self.run_until_stopped(passes=3)
        self.assertEqual(self.send_state("2026-12-01"), "sending")
        again = self.run_until_stopped(passes=3)          # restarted: 4:50, 5:00, 5:10 PM
        self.assertEqual(self.digests(), [])
        self.assertEqual(self.send_state("2026-12-01"), "unknown")
        self.assertEqual(self.digest_checks(again), [])
        self.assertIn("Tue Dec 1, 4:50 PM Pacific  S2 was cut off mid-send (a stop or a crash): no copy in Sent "
                      "after two looks. It may have gone, so it is never sent again.", self.said)

    def test_a_halt_stops_a_due_digest_at_the_gate_and_after_resume_it_goes_once(self):
        self.clock(TUE_450PM)
        real = digest.build

        def stop_comes_while_she_counts(*args, **kwargs):
            supervisor.request_stop("throwaway", "cli", state_dir=self.state)
            return real(*args, **kwargs)

        with mock.patch.object(digest, "build", stop_comes_while_she_counts):
            s = self.run_until_stopped(passes=4)
        checks = self.digest_checks(s)
        self.assertEqual(len(checks), 1)
        self.assertEqual((checks[0]["payload"]["passed"], checks[0]["payload"]["first_fail"]), (False, "G1"))
        self.assertEqual(s.halted, "cli")
        kinds = [l["kind"] for l in receipts.read_lines(s.receipts_path)]
        self.assertEqual(kinds[-3:], ["checked", "halted", "summary"])     # the gate stopped her there and then
        self.assertEqual(self.digests(), [])
        self.assertIsNone(self.send_state("2026-12-01"))                # nothing recorded, nothing sent
        self.run_until_stopped(passes=3)                  # resumed: 4:50, 5:00, 5:10 PM
        self.assertEqual([date for date, _ in self.digests()], [AT_TUE_5PM])


class Window(LongRun):
    """A digest counts every receipt line written after the last digest the server took (or, before
    the first, everything this mode's state recorded), across every run file of the mode."""
    names = ("05-opsweekly-newsletter.eml",)          # filed at her 4:50 PM pass, or in an earlier run

    def elena_after_the_first_digest(self, n):
        if self.digests() and not os.path.exists(os.path.join(self.root, "inbox", ELENA)):
            self.elena_writes()

    def test_mail_after_a_digest_counts_in_the_next_days_and_never_twice(self):
        self.clock(TUE_440PM)
        self.run_until_stopped(passes=150, during=self.elena_after_the_first_digest)
        (tue, tue_body), (wed, wed_body) = self.digests()
        self.assertEqual((tue, tue_body[0]), (AT_TUE_5PM, "Handled 1, held 0, human approvals 0. Tue Dec 1"))
        self.assertEqual((wed, wed_body[:3]), (AT_WED_5PM, ["Handled 0, held 1, human approvals 0. Wed Dec 2",
                                                            "Held for you:", ELENA_HELD % "5:10"]))

    def test_a_digest_the_server_refused_counts_from_the_last_one_it_took(self):
        self.clock(TUE_440PM)
        real, refused = sandbox_mod.SandboxMail.send, []

        def refuses_wednesdays_once(mail, raw, envelope, mid, template=None):
            if mid == digest_mid("2026-12-02") and not refused:
                refused.append(mid)
                raise MailError("refused: the server said no")
            return real(mail, raw, envelope, mid, template=template)

        with mock.patch.object(sandbox_mod.SandboxMail, "send", refuses_wednesdays_once):
            self.run_until_stopped(passes=150, during=self.elena_after_the_first_digest)
        self.assertEqual(refused, [digest_mid("2026-12-02")])
        (tue, _), (wed, wed_body) = self.digests()
        self.assertEqual((wed, wed_body[:3]), ("Thu, 03 Dec 2026 01:10:00 +0000",     # her next pass, 5:10 PM
                                               ["Handled 0, held 1, human approvals 0. Wed Dec 2",
                                                "Held for you:", ELENA_HELD % "5:10"]))

    def test_the_first_digest_counts_every_run_file_of_the_mode(self):
        self.run_once()                                   # Tue 8:40 AM: the newsletter, filed
        self.clock(TUE_450PM)
        self.elena_writes()
        self.run_until_stopped(passes=3)                  # 4:50, 5:00 (Elena held, then the digest), 5:10 PM
        (sent,) = self.digests()
        self.assertEqual(sent[1][:4], ["Handled 1, held 1, human approvals 0. Tue Dec 1", "Held for you:",
                                       ELENA_HELD % "5:00", "Model calls 1, cost $0.0000. Receipts verify: yes."])

    def test_receipts_verify_no_when_an_earlier_run_file_was_changed(self):
        first = self.run_once()
        with open(first.receipts_path, "rb") as fh:
            data = fh.read()
        with open(first.receipts_path, "wb") as fh:
            fh.write(data.replace(b'"ts":"2026-', b'"ts":"2027-', 1))     # one character: still JSON
        self.clock(TUE_450PM)
        self.run_until_stopped(passes=3)
        (sent,) = self.digests()
        self.assertEqual(sent[1][:2], ["Handled 1, held 0, human approvals 0. Tue Dec 1", "Receipts verify: no."])

    def test_receipts_verify_no_when_an_earlier_run_never_wrote_its_closing_line(self):
        with mock.patch.object(loop._Run, "finish", side_effect=Crash("killed before its summary")):
            with self.assertRaises(Crash):
                self.run_once()
        self.clock(TUE_450PM)
        self.run_until_stopped(passes=3)
        (sent,) = self.digests()
        self.assertEqual(sent[1][:2], ["Handled 1, held 0, human approvals 0. Tue Dec 1", "Receipts verify: no."])


class FiniteRunWholeDay(LongRun):
    """The demo's digest (a finite run with the digest on) counts the whole day as the daily digest
    does: every run of the mode since the last digest the server took. Stopped part way, resumed and
    run again, its first line is the day's, as the end-of-day summary's is, never only what the run
    after the stop did."""
    names = ("01-priya-request.eml", "02-marcus-request.eml", "05-opsweekly-newsletter.eml", ELENA)

    def stopped_part_way(self):
        """One demo-shaped run that a stop ends right after Marcus's offer closes."""
        real = loop._Run.close

        def close_then_stop(run, e, kind, outcome, *a, **k):
            out = real(run, e, kind, outcome, *a, **k)
            if outcome == "offer":
                supervisor.request_stop("throwaway", "cli", state_dir=self.state)
            return out

        with mock.patch.object(loop._Run, "close", close_then_stop):
            first = loop.run(self.config(digest=True))
        self.assertEqual(first.halted, "cli")
        supervisor.resume("throwaway", state_dir=self.state)
        return first

    def test_a_day_run_in_one_go_and_a_day_stopped_and_resumed_report_the_same_digest(self):
        whole = loop.run(self.config(digest=True))
        ((_, in_one_go),) = self.digests()
        self.assertEqual((whole.handled, whole.held), (3, 1))
        self.assertEqual(in_one_go[0], "Handled 3, held 1, human approvals 0. Tue Dec 1")
        self.setUp()                                      # the same day again from nothing, stopped part way
        first = self.stopped_part_way()
        self.assertEqual(self.digests(), [])              # a halted run sends no digest
        second = loop.run(self.config(digest=True))
        self.assertEqual((first.handled + second.handled, first.held + second.held), (3, 1))
        self.assertLess(second.handled, 3)                # the run after the stop did only the rest of the day
        ((_, resumed),) = self.digests()
        self.assertEqual(resumed, in_one_go)
        self.assertIn("Digest sent to the owner: Handled 3, held 1, human approvals 0.", self.said[-1])

    def test_it_lists_what_the_stopped_run_did_and_verifies_both_runs(self):
        self.stopped_part_way()
        loop.run(self.config(digest=True))
        ((_, body),) = self.digests()
        self.assertIn("Offered times to: Marcus Lee", body)                  # offered before the stop
        self.assertIn("Follow-ups filed: 1 nudge, 1 booking check.", body)  # filed before the stop
        self.assertIn(ELENA_HELD % "1:10", body)                             # held after the resume
        self.assertTrue([l for l in body if l.endswith("Receipts verify: yes.")], body)

    def test_a_message_read_again_after_the_stop_counts_once(self):
        real = loop._Run.reply

        def stop_before_the_offer(run, e, f, template, *a, **k):
            if template == "T2":
                supervisor.request_stop("throwaway", "cli", state_dir=self.state)
            return real(run, e, f, template, *a, **k)

        with mock.patch.object(loop._Run, "reply", stop_before_the_offer):
            first = loop.run(self.config(digest=True))    # Marcus was observed, never closed: read again after
        self.assertEqual(first.halted, "cli")
        supervisor.resume("throwaway", state_dir=self.state)
        loop.run(self.config(digest=True))
        ((_, body),) = self.digests()
        self.assertEqual(body[0], "Handled 3, held 1, human approvals 0. Tue Dec 1")

    def test_receipts_of_an_earlier_run_that_cannot_be_read_send_no_digest_and_say_so(self):
        first = self.stopped_part_way()
        os.remove(first.receipts_path)
        second = loop.run(self.config(digest=True))
        self.assertEqual(self.digests(), [])
        self.assertEqual([l["payload"]["step"] for l in receipts.read_lines(second.receipts_path)
                          if l["kind"] == "error"], ["digest"])
        self.assertTrue([l for l in self.said if "Digest not sent: " in l], self.said)


class FailsClosed(LongRun):
    """Receipts or a ledger that cannot answer: no digest, an error line, and her next pass goes on."""
    names = ("05-opsweekly-newsletter.eml",)

    def assert_no_digest_and_the_run_went_on(self, s, error):
        self.assertEqual(self.digests(), [])
        errors = [(l["ts"], l["payload"]["step"], l["payload"]["error"].split(":")[0])
                  for l in receipts.read_lines(s.receipts_path) if l["kind"] == "error"]
        self.assertEqual(errors, [("2026-12-02T01:00:00Z", "digest", error),     # 5:00 PM
                                  ("2026-12-02T01:10:00Z", "digest", error)])    # and again at her next pass
        self.assertIn("Tue Dec 1, 5:00 PM Pacific  Digest not sent: %s (error line written). She tries again "
                      "at her next pass." % error, self.said)
        self.assertEqual(s.halted, "cli")                 # ended by the stop, not by the failure
        self.assertTrue(receipts.verify(s.receipts_path, finished=True).ok)

    def test_receipts_that_cannot_be_read_send_no_digest_and_the_run_goes_on(self):
        first = self.run_once()                           # Tue 8:40 AM: the newsletter, filed
        with open(first.receipts_path, "rb") as fh:
            lines = fh.read().split(b"\n")
        lines[1] = b"not a receipt line"
        with open(first.receipts_path, "wb") as fh:
            fh.write(b"\n".join(lines))
        self.clock(TUE_450PM)
        s = self.run_until_stopped(passes=3, during=lambda n: n == 2 and self.elena_writes())
        self.assert_no_digest_and_the_run_went_on(s, "ValueError")
        held = [l["payload"]["outcome"] for l in receipts.read_lines(s.receipts_path) if l["kind"] == "held"]
        self.assertEqual(held, ["hold:money"])            # Elena, read at the pass after the failure

    def test_a_ledger_that_cannot_answer_sends_no_digest_and_the_run_goes_on(self):
        self.clock(TUE_450PM)
        real = ledger_mod.Ledger.send

        def cannot_answer(db, message_id):
            if ledger_mod.norm_id(message_id) == ledger_mod.norm_id(digest_mid("2026-12-01")):
                raise sqlite3.OperationalError("disk I/O error")
            return real(db, message_id)

        with mock.patch.object(ledger_mod.Ledger, "send", cannot_answer):
            s = self.run_until_stopped(passes=3)
        self.assert_no_digest_and_the_run_went_on(s, "OperationalError")

    def test_a_ledger_that_cannot_record_the_send_sends_no_digest_and_the_run_goes_on(self):
        self.clock(TUE_450PM)
        real = ledger_mod.Ledger.record_send

        def cannot_record(db, message_id, kind, *args):
            if kind == "send_digest":
                raise sqlite3.OperationalError("database or disk is full")
            return real(db, message_id, kind, *args)

        with mock.patch.object(ledger_mod.Ledger, "record_send", cannot_record):
            s = self.run_until_stopped(passes=3)
        self.assert_no_digest_and_the_run_went_on(s, "OperationalError")


if __name__ == "__main__":
    unittest.main()
