"""Process tests for the kill switch: a real supervisor, a real child worker, a real kill.

Covers A3's `kill-names-in-flight` and `halt-lists-dropped`, ruling 8's one Doris per ledger,
a STOP file, the worker's budget-cap halt, and a restart refused until `resume`.
The worker is tests/process/fake_worker.py; it holds a "life" lock that the OS releases the
instant it dies, which is how these tests time stop-to-dead independently of the supervisor.

Each test runs twice: against the contract fakes in fake_worker.py, and against the real
doris.receipts and doris.ledger.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from doris import paths, supervisor
from tests.process import fake_worker

RUN = "model-stub-sandbox-20261127T1900Z"
WAIT_S = 20.0           # generous: a slow CI machine starting Python
HALT_CLI = {"code": "halt.cli", "text": "Halted: stop on the command line."}


def note(text):
    sys.stderr.write("\n    %s " % (text,))
    sys.stderr.flush()


class Base(unittest.TestCase):
    backend = "fake"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="doris-kill-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = os.path.join(self.tmp, "state")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        for name, value in (("STATE", self.state), ("REPO", self.repo)):
            patcher = mock.patch.object(paths, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.mode = "sandbox"
        self.sdir = os.path.join(self.state, self.mode)
        self.receipts = os.path.join(self.sdir, "runs", RUN, "receipts.jsonl")
        self.ledger = os.path.join(self.sdir, "ledger.sqlite")
        self.count = 0

    # -- plumbing ------------------------------------------------------------------------
    def fresh(self, name):
        self.count += 1
        return os.path.join(self.tmp, "%s-%d" % (name, self.count))

    def opts(self, scenario):
        """Options for one worker; sets self.life and self.marker for that worker."""
        self.life, self.marker = self.fresh("life.lock"), self.fresh("marker")
        return ["--scenario", scenario, "--receipts", self.receipts, "--ledger", self.ledger, "--run", RUN,
                "--mode", self.mode, "--state", self.state, "--repo", self.repo, "--life", self.life,
                "--marker", self.marker, "--backend", self.backend]

    def worker_argv(self, scenario):
        return [sys.executable, fake_worker.THIS_SCRIPT, "work"] + self.opts(scenario)

    def modules(self):
        if self.backend == "real":
            return {}                     # run_supervised imports doris.receipts and doris.ledger
        return {"receipts_module": fake_worker.FakeReceipts, "open_ledger": fake_worker.JsonLedger}

    def receipts_mod(self):
        if self.backend == "real":
            from doris import receipts
            return receipts
        return fake_worker.FakeReceipts

    def open_ledger(self):
        if self.backend == "real":
            from doris.ledger import Ledger
            return Ledger(self.ledger)
        return fake_worker.JsonLedger(self.ledger)

    def lines(self):
        return self.receipts_mod().read_lines(self.receipts)

    def run_now(self, scenario, **kw):
        said = []
        code = supervisor.run_supervised(self.mode, RUN, self.worker_argv(scenario), self.receipts, self.ledger,
                                         say=said.append, **dict(self.modules(), **kw))
        return code, said

    def start(self, scenario):
        box = {"said": []}
        argv = self.worker_argv(scenario)

        def target():
            box["code"] = supervisor.run_supervised(self.mode, RUN, argv, self.receipts, self.ledger,
                                                    say=box["said"].append, **self.modules())
        box["thread"] = threading.Thread(target=target, daemon=True)
        box["thread"].start()
        return box

    def finish(self, box):
        box["thread"].join(WAIT_S)
        self.assertFalse(box["thread"].is_alive(), "the supervisor did not return")
        return box["code"], box["said"]

    def wait_for(self, path):
        deadline = time.monotonic() + WAIT_S
        while not os.path.exists(path):
            if time.monotonic() > deadline:
                self.fail("timed out waiting for %s" % (path,))
            time.sleep(0.005)

    def wait_dead(self, life):
        """The moment the worker is dead: the OS has let go of its life lock."""
        deadline = time.monotonic() + WAIT_S
        while True:
            held = supervisor.acquire_lock(life)
            if held is not None:
                now = time.monotonic()
                held.release()
                return now
            if time.monotonic() > deadline:
                self.fail("the worker is still alive")
            time.sleep(0.002)

    def halt_mid_call(self, scenario="midcall", stop=None):
        """Start a supervised run and stop it while the worker waits on its slowed call.
        Returns (exit code, lines said, seconds from the stop to a dead worker)."""
        box = self.start(scenario)
        self.wait_for(self.marker)
        life = self.life
        started = time.monotonic()
        if stop is None:
            supervisor.request_stop(self.mode, "cli")
        else:
            stop()
        dead = self.wait_dead(life)
        code, said = self.finish(box)
        return code, said, dead - started


class KillMidCallTest(Base):
    def test_stop_mid_call_kills_the_worker_well_under_a_second(self):
        code, said, took = self.halt_mid_call()
        note("stop-to-dead, measured from outside: %.3f s; the halted line says %s ms"
             % (took, self.lines()[-1]["payload"]["stop_to_dead_ms"]))
        self.assertEqual(code, supervisor.EXIT_HALTED)
        self.assertLess(took, 1.0)
        self.assertLess(self.lines()[-1]["payload"]["stop_to_dead_ms"], 1000)

    def test_the_killed_call_never_finished(self):
        self.halt_mid_call()
        lines = self.lines()
        called = [ln for ln in lines if ln["kind"] == "called" and ln["payload"]["call"] == "calendar_create"]
        self.assertEqual(len(called), 1)
        answers = [ln for ln in lines if ln["kind"] == "result" and ln["payload"]["call_seq"] == called[0]["seq"]]
        self.assertEqual(answers, [])
        self.assertEqual(lines[-1]["kind"], "halted")

    def test_the_halted_line_names_the_call_in_flight_and_the_dropped_queue(self):
        code, said, _ = self.halt_mid_call()
        lines = self.lines()
        halted = lines[-1]
        called = [ln for ln in lines if ln["kind"] == "called" and ln["payload"]["call"] == "calendar_create"][0]
        self.assertEqual(halted["kind"], "halted")
        self.assertEqual(halted["why"], HALT_CLI)
        self.assertEqual(halted["payload"]["cause"], "cli")
        self.assertEqual(halted["payload"]["in_flight"],
                         {"seq": called["seq"], "call": "calendar_create", "target": "doris-b2", "unconfirmed": True})
        self.assertEqual(halted["payload"]["dropped"], [
            {"kind": "send_reply", "thread": "t1@a.example", "message": "m1@a.example",
             "template": "T4", "booking_id": None},
            {"kind": "calendar_create", "thread": "t2@b.example", "message": "m2@b.example",
             "template": None, "booking_id": "doris-b2"},
            {"kind": "message", "thread": "t3@c.example", "message": "m3@c.example",
             "template": None, "booking_id": None},
        ])
        self.assertEqual(len(said), 1)
        self.assertIn("calendar_create doris-b2", said[0])
        self.assertIn("Dropped 3", said[0])

    def test_the_ledger_marks_the_write_unconfirmed_and_holds_no_queue(self):
        self.halt_mid_call()
        led = self.open_ledger()
        try:
            self.assertEqual(led.booking("doris-b2")["state"], "unconfirmed")
            self.assertEqual(list(led.queued_actions()), [])
        finally:
            led.close()

    def test_the_chain_verifies_over_the_whole_file_after_the_halt(self):
        self.halt_mid_call()
        res = self.receipts_mod().verify(self.receipts)
        self.assertTrue(res.ok, res)
        lines = self.lines()
        self.assertEqual([ln["seq"] for ln in lines], list(range(1, len(lines) + 1)))
        self.assertEqual(lines[-1]["prev"], lines[-2]["hash"])

    def test_a_torn_tail_is_moved_aside_and_the_halt_chains_from_the_last_whole_line(self):
        self.halt_mid_call("torn")
        with open(self.receipts + ".torn-1", "rb") as fh:
            self.assertEqual(fh.read(), b'{"kind":"result","payload":{"call_seq":')
        halted = self.lines()[-1]
        self.assertEqual(halted["payload"]["torn_tail"], "receipts.jsonl.torn-1")
        self.assertEqual(halted["payload"]["in_flight"]["call"], "send")
        self.assertTrue(halted["payload"]["in_flight"]["unconfirmed"])
        self.assertTrue(self.receipts_mod().verify(self.receipts).ok)
        if self.backend == "fake":
            self.assertEqual(fake_worker.JsonLedger(self.ledger).db["sends"]["<s1@fastmail.com>"]["state"], "unknown")


class RestartTest(Base):
    def test_a_restart_is_refused_until_resume(self):
        self.halt_mid_call()
        code, said = self.run_now("quick")
        self.assertEqual(code, supervisor.EXIT_REFUSED_HALT)
        self.assertFalse(os.path.exists(self.marker), "a halted Doris must not start her worker")
        self.assertEqual(len(said), 1)
        self.assertIn("halted (stop on the command line)", said[0])
        self.assertTrue(supervisor.resume(self.mode).ok)
        code, said = self.run_now("quick")
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(self.marker))

    def test_a_worker_that_finishes_returns_its_code_and_writes_no_halt(self):
        code, said = self.run_now("quick")
        self.assertEqual((code, said), (0, []))
        self.assertNotIn("halted", [ln["kind"] for ln in self.lines()])
        self.assertIsNone(supervisor.halt_cause(self.mode))


class OneRunnerTest(Base):
    def test_two_supervisors_cannot_run_at_once(self):
        first = subprocess.Popen([sys.executable, fake_worker.THIS_SCRIPT, "supervise"] + self.opts("midcall"))
        try:
            self.wait_for(self.marker)
            code, said = self.run_now("quick")          # a second Doris on the same ledger
            self.assertEqual(code, supervisor.EXIT_LOCKED)
            self.assertEqual(said, ["Not started: another Doris is already running on this ledger."])
            self.assertFalse(os.path.exists(self.marker), "the second Doris must not start a worker")
            supervisor.request_stop(self.mode, "cli")
            self.assertEqual(first.wait(WAIT_S), supervisor.EXIT_HALTED)
        finally:
            if first.poll() is None:
                first.kill()
                first.wait()

    def test_the_worker_holds_the_lock_so_resume_waits_for_it(self):
        box = self.start("midcall")
        self.wait_for(self.marker)
        supervisor.request_stop(self.mode, "watch_button")
        early = supervisor.resume(self.mode)             # may land before or after the kill
        code, _ = self.finish(box)
        self.assertEqual(code, supervisor.EXIT_HALTED)
        if not early.ok:
            self.assertIn("running", early.message)
            self.assertEqual(supervisor.halt_cause(self.mode), "watch_button")

    def test_a_worker_that_loses_the_lock_race_is_reported_with_no_halted_line(self):
        code, said = self.run_now("lostrace")
        self.assertEqual(code, supervisor.EXIT_LOCKED)
        self.assertEqual(said, ["Not started: another Doris is already running on this ledger."])
        self.assertFalse(os.path.exists(self.receipts), "nothing is written for a run that never ran")

    def test_a_killed_doris_leaves_no_stale_lock(self):
        lock = os.path.join(self.sdir, "ledger.lock")
        marker = self.fresh("held")
        holder = subprocess.Popen([sys.executable, fake_worker.THIS_SCRIPT, "hold-lock",
                                   "--lock", lock, "--marker", marker])
        try:
            self.wait_for(marker)
            self.assertIsNone(supervisor.acquire_lock(lock))
        finally:
            holder.kill()                                # a crash: no cleanup runs
            holder.wait()
        deadline = time.monotonic() + 5
        again = supervisor.acquire_lock(lock)
        while again is None and time.monotonic() < deadline:
            time.sleep(0.01)
            again = supervisor.acquire_lock(lock)
        self.assertIsNotNone(again, "the dead holder's lock is stale")
        again.release()


class StopFileTest(Base):
    def test_a_stop_file_halts_mid_call_and_is_never_deleted(self):
        stop = os.path.join(self.repo, "STOP")

        def make_stop_file():
            with open(stop, "w") as fh:
                fh.write("stop\n")
        code, said, took = self.halt_mid_call(stop=make_stop_file)
        note("STOP file to dead worker: %.3f s" % (took,))
        self.assertEqual(code, supervisor.EXIT_HALTED)
        self.assertLess(took, 1.0)
        halted = self.lines()[-1]
        self.assertEqual(halted["payload"]["cause"], "stop_file")
        self.assertEqual(halted["why"], {"code": "halt.stop_file", "text": "Halted: a STOP file was found."})
        self.assertTrue(os.path.exists(stop))

        self.assertEqual(self.run_now("quick")[0], supervisor.EXIT_REFUSED_HALT)
        res = supervisor.resume(self.mode)
        self.assertFalse(res.ok)
        self.assertIn(stop, res.message)
        self.assertTrue(os.path.exists(stop), "code never deletes a user's STOP file")
        self.assertEqual(self.run_now("quick")[0], supervisor.EXIT_REFUSED_HALT)
        os.remove(stop)                                  # the user deletes it
        self.assertEqual(self.run_now("quick")[0], supervisor.EXIT_REFUSED_HALT)   # the failed resume changed nothing
        self.assertTrue(supervisor.resume(self.mode).ok)
        self.assertEqual(self.run_now("quick")[0], 0)


class CtrlCTest(Base):
    @unittest.skipIf(os.name == "nt", "Ctrl+C is a console event on Windows; covered by the unit test")
    def test_ctrl_c_on_the_supervisor_kills_the_worker_and_is_a_stop_that_survives(self):
        import signal
        sup = subprocess.Popen([sys.executable, fake_worker.THIS_SCRIPT, "supervise"] + self.opts("midcall"))
        try:
            self.wait_for(self.marker)
            life = self.life
            os.kill(sup.pid, signal.SIGINT)             # the supervisor alone, as a terminal would not
            self.assertEqual(sup.wait(WAIT_S), supervisor.EXIT_HALTED)
        finally:
            if sup.poll() is None:
                sup.kill()
                sup.wait()
        self.wait_dead(life)
        halted = self.lines()[-1]
        self.assertEqual(halted["payload"]["cause"], "cli")
        self.assertEqual(halted["payload"]["in_flight"]["target"], "doris-b2")
        self.assertEqual(supervisor.halt_cause(self.mode), "cli")


class GateHaltTest(Base):
    def test_a_worker_that_sees_a_halt_at_the_gate_exits_4_and_the_parent_writes_the_halted_line(self):
        # The supervisor's next look would see the same STOP file and kill a worker still exiting
        # (exit 1 on Windows, -9 here): a long poll lets the worker's own exit come first, every time.
        code, said = self.run_now("g1", poll_s=WAIT_S)
        self.assertEqual(code, supervisor.EXIT_HALTED)
        halted = self.lines()[-1]
        self.assertEqual(halted["kind"], "halted")
        self.assertEqual(halted["payload"]["cause"], "stop_file")
        self.assertEqual(halted["payload"]["worker_exit"], supervisor.WORKER_EXIT_HALTED)
        self.assertIsNone(halted["payload"]["in_flight"])
        self.assertTrue(self.receipts_mod().verify(self.receipts).ok)


class BudgetCapTest(Base):
    def test_the_workers_budget_cap_halt_is_recorded_and_survives_a_restart(self):
        code, said = self.run_now("budget")
        self.assertEqual(code, supervisor.EXIT_HALTED)
        halted = self.lines()[-1]
        self.assertEqual(halted["payload"]["cause"], "budget_cap")
        self.assertEqual(halted["why"]["code"], "halt.budget_cap")
        self.assertIsNone(halted["payload"]["in_flight"])
        self.assertEqual(self.run_now("quick")[0], supervisor.EXIT_REFUSED_HALT)


class KillMidCallRealTest(KillMidCallTest):
    backend = "real"


class RestartRealTest(RestartTest):
    backend = "real"


if __name__ == "__main__":
    unittest.main()
