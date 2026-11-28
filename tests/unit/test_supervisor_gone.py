"""A worker whose supervisor is gone stops herself and writes her own halted line.

Under the kill switch the worker leaves the halted line to the supervisor. When the supervisor is
gone (killed with SIGKILL, crashed), nothing can kill her mid-call any more and nobody would write
that line, so the worker's lifeline (supervisor.Lifeline) writes HALT, every halt check and G1 read
it, and the worker writes the halted line and the summary herself.

The real loop runs in this process as the worker runs it (supervised, until stopped) on a mini
sandbox, with a stand-in for the lifeline; tests/process/test_killswitch.py kills a real supervisor.
"""

import contextlib
import io
import json
import os
import sys
import unittest
from unittest import mock

from doris import __main__ as cli
from doris import loop, receipts, supervisor
from tests.unit.test_run_digest import CAP, LongRun, NoWait

GONE_WHY = {"code": "halt.supervisor_gone",
            "text": "Halted: her supervisor is gone, so she stopped herself (fail closed)."}


class StandInLifeline(object):
    """supervisor.Lifeline as the run sees it: `gone` turns true when the supervisor's end closes."""

    def __init__(self):
        self.gone = False


class GoneBase(LongRun):
    names = ("01-priya-request.eml",)

    def setUp(self):
        super().setUp()
        self.lifeline = StandInLifeline()

    def halt_cause(self):
        return supervisor.halt_cause("throwaway", state_dir=self.state)

    def waits(self, during):
        """loop._time and loop._sleep for a run until stopped: during(n) runs in the wait after pass n."""
        count = []

        def waited():
            count.append(True)
            during(len(count))
            if len(count) >= CAP:                 # a run whose stop never comes ends here
                supervisor.request_stop("throwaway", "cli", state_dir=self.state)
        fake = NoWait(waited)
        return mock.patch.multiple(loop, _time=fake.time, _sleep=fake.sleep)

    def worker_run(self, during):
        """One run as the worker runs it under the kill switch: supervised, until stopped."""
        with self.waits(during):
            return loop.run(self.config(cycles=None, cycle_s=1.0, supervised=True, lifeline=self.lifeline))

    def gone(self, write_halt=True, then=None):
        def during(n):
            if n == 1:
                if then is not None:
                    then()
                self.lifeline.gone = True
                if write_halt:
                    supervisor.request_stop("throwaway", "supervisor_gone", state_dir=self.state)
        return during

    def closing_lines(self, s):
        lines = receipts.read_lines(s.receipts_path)
        self.assertTrue(receipts.verify(s.receipts_path, finished=True).ok)
        return lines[-2], lines[-1]


class SupervisorGone(GoneBase):
    def test_her_supervisor_gone_she_writes_her_own_halted_line_and_summary(self):
        s = self.worker_run(self.gone())
        self.assertEqual(s.halted, "supervisor_gone")
        halted, summary = self.closing_lines(s)
        self.assertEqual((halted["kind"], halted["payload"]["cause"]), ("halted", "supervisor_gone"))
        self.assertEqual(halted["why"], GONE_WHY)
        self.assertEqual((halted["payload"]["in_flight"], halted["payload"]["dropped"]), ("none", []))
        self.assertEqual((summary["kind"], summary["payload"]["halted"]), ("summary", "supervisor_gone"))
        self.assertEqual(summary["payload"]["handled"], 1)          # Priya's request, on her first pass
        self.assertEqual(self.halt_cause(), "supervisor_gone")      # it survives a restart: only resume clears it

    def test_a_halt_her_lifeline_could_not_write_stops_her_all_the_same_and_she_writes_it(self):
        s = self.worker_run(self.gone(write_halt=False))
        self.assertEqual(s.halted, "supervisor_gone")
        halted, summary = self.closing_lines(s)
        self.assertEqual(halted["why"], GONE_WHY)
        self.assertEqual(self.halt_cause(), "supervisor_gone")

    def test_a_halt_that_cannot_be_written_at_all_is_said_in_her_halted_line(self):
        with mock.patch.object(supervisor, "request_stop", side_effect=OSError(28, "No space left on device")):
            s = self.worker_run(self.gone(write_halt=False))
        self.assertEqual(s.halted, "supervisor_gone")
        halted, summary = self.closing_lines(s)
        self.assertEqual((halted["payload"]["cause"], halted["payload"]["halt_file"]), ("supervisor_gone", "unwritten"))
        self.assertIsNone(self.halt_cause())                        # she stopped all the same

    def test_any_halt_once_her_supervisor_is_gone_is_written_by_her(self):
        # `stop` lands, and the supervisor that would have killed her and written the line is gone.
        s = self.worker_run(self.gone(write_halt=False,
                                      then=lambda: supervisor.request_stop("throwaway", "cli", state_dir=self.state)))
        self.assertEqual(s.halted, "cli")
        halted, summary = self.closing_lines(s)
        self.assertEqual((halted["payload"]["cause"], halted["why"]["code"]), ("cli", "halt.cli"))
        self.assertEqual(self.halt_cause(), "cli")                  # the first cause stays

    def test_with_her_supervisor_there_a_halt_is_left_to_it(self):
        def stop(n):
            if n == 1:
                supervisor.request_stop("throwaway", "cli", state_dir=self.state)
        with self.assertRaises(loop.Halted):
            self.worker_run(stop)
        (run,) = os.listdir(os.path.join(self.state, "runs"))
        lines = receipts.read_lines(os.path.join(self.state, "runs", run, "receipts.jsonl"))
        self.assertNotIn("halted", [l["kind"] for l in lines])     # the supervisor writes it, once she is dead

    def test_every_halt_check_and_g1_read_her_lifeline(self):
        r = loop._Run(self.config(supervised=True, lifeline=self.lifeline))
        self.assertIsNone(r.halt_cause())
        self.lifeline.gone = True
        self.assertEqual(r.halt_cause(), "supervisor_gone")         # G1 calls this at every effect
        with self.assertRaises(loop.Halted) as caught:
            r.check_halt()
        self.assertEqual(caught.exception.cause, "supervisor_gone")
        supervisor.request_stop("throwaway", "watch_button", state_dir=self.state)
        self.assertEqual(r.halt_cause(), "watch_button")            # a halt on disk is named as it is


class WorkerSaysSo(GoneBase):
    def test_the_worker_exits_halted_and_says_she_stopped_herself(self):
        fields = dict(mode="throwaway", run_name="run-gone", state_dir=self.state, sandbox_root=self.root,
                      owner_addr="whizz@fastmail.com", doris_addr="doris.htm@fastmail.com", model_source="stub",
                      clock_mode="advance", retry_wait_s=0, cycles=None, cycle_s=1.0)
        path = os.path.join(self.tmp, "config.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(fields, fh)
        out = io.StringIO()
        with self.waits(self.gone()), mock.patch.object(supervisor, "lifeline", return_value=self.lifeline) as given:
            with contextlib.redirect_stdout(out):
                code = cli.worker([path])
        given.assert_called_once_with("throwaway", self.state)
        self.assertEqual(code, supervisor.WORKER_EXIT_HALTED)
        last = out.getvalue().splitlines()[-1]
        self.assertTrue(last.startswith("Halted: her supervisor is gone, so she stopped herself and wrote the halted "
                                        "line. 1 handled, 0 held before it."), last)
        self.assertIn("chain verified", last)
        self.assertIn("doris resume", last)


class TerminalGone(unittest.TestCase):
    def test_a_line_to_a_terminal_that_is_gone_is_no_error(self):
        # Her supervisor's terminal or pipe may be gone with it: a line she cannot say never stops her
        # before her halted line (the receipts keep the record).
        class Gone(object):
            def write(self, text):
                raise BrokenPipeError(32, "Broken pipe")

            def flush(self):
                raise OSError(5, "Input/output error")
        with mock.patch.object(sys, "stdout", Gone()):
            cli._say("Halted: her supervisor is gone, so she stopped herself and wrote the halted line.")
