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
import tempfile
import unittest
from unittest import mock

from doris import compose, digest, loop, receipts, supervisor
from doris import ledger as ledger_mod
from doris.adapters import sandbox as sandbox_mod
from tests.unit.test_restart import Crash, mini_sandbox

OWNER = "whizz@fastmail.com"
DORIS = "doris.htm@fastmail.com"
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
    after which waited() runs."""

    def __init__(self, waited):
        self.now, self.waited = 1000.0, waited

    def time(self):
        return self.now

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
        with mock.patch.object(loop, "_time", fake.time), mock.patch.object(loop, "_sleep", fake.sleep):
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
                      "Handled 0, held 0, human approvals 0. Tue Dec 1", self.said)
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
        self.assertEqual(self.send_state("2026-12-01"), "pending")     # the hand-over never written down
        self.run_until_stopped(passes=3)                  # restarted: 4:50, 5:00, 5:10 PM
        self.assertEqual(len(self.digests()), 1)
        self.assertEqual(self.send_state("2026-12-01"), "confirmed")   # found in Sent before anything new

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
        self.assertEqual(self.digests(), [])
        self.assertIsNone(self.send_state("2026-12-01"))                # nothing recorded, nothing sent
        self.run_until_stopped(passes=3)                  # resumed: 4:50, 5:00, 5:10 PM
        self.assertEqual([date for date, _ in self.digests()], [AT_TUE_5PM])


if __name__ == "__main__":
    unittest.main()
