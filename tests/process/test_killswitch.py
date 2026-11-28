"""Process tests for the kill switch: a real supervisor, a real child worker, a real kill.

Covers A3's `kill-names-in-flight` and `halt-lists-dropped`, ruling 8's one Doris per ledger,
a STOP file, the worker's budget-cap halt, and a restart refused until `resume`; SIGTERM and SIGHUP
on the supervisor (a stop, as Ctrl+C is), and a supervisor killed outright (its real worker stops
itself and writes its own halted line).
The worker is tests/process/fake_worker.py; it holds a "life" lock that the OS releases the
instant it dies, which is how these tests time stop-to-dead independently of the supervisor.

Each test runs twice: against the contract fakes in fake_worker.py, and against the real
doris.receipts and doris.ledger.
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from doris import paths, receipts, supervisor
from tests.process import fake_worker
from tests.unit.test_restart import mini_sandbox

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
        self.addCleanup(self.stop_left_running, box)    # runs before the paths go back to the defaults
        return box

    def stop_left_running(self, box):
        """A test that failed before finish() leaves her running: stop her here, in this test's own
        state folder, so no supervisor (or worker) outlives its test."""
        if box["thread"].is_alive():
            supervisor.request_stop(self.mode, "cli")
            box["thread"].join(WAIT_S)
        self.assertFalse(box["thread"].is_alive(), "the supervisor outlived its test")

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
            self.assertEqual(fake_worker.JsonLedger(self.ledger).db["sends"]["<s1@fastmail.com>"]["state"], "sending")


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


# A wrapper that starts what follows the way nohup does: SIGHUP ignored, then exec.
NOHUP = "import os, signal, sys; signal.signal(signal.SIGHUP, signal.SIG_IGN); os.execv(sys.argv[1], sys.argv[1:])"


@unittest.skipIf(os.name == "nt", "no SIGHUP on Windows, and SIGTERM there is TerminateProcess: nothing can catch it")
class SignalTest(Base):
    """SIGTERM or SIGHUP on the supervisor stops her the way Ctrl+C does: the worker is killed
    mid-call, the halted line names the signal, and the halt survives a restart until resume."""

    def start_supervisor(self, prefix=()):
        sup = subprocess.Popen(list(prefix) + [sys.executable, fake_worker.THIS_SCRIPT, "supervise"]
                               + self.opts("midcall"), start_new_session=True)   # a group of its own
        self.addCleanup(self.end_group, sup)
        self.wait_for(self.marker)
        return sup, self.life

    def end_group(self, sup):
        try:
            os.killpg(sup.pid, signal.SIGKILL)           # whatever of the group is left, a worker included
        except OSError:
            pass
        sup.wait()

    def assert_stopped_by(self, name, code, life):
        self.assertEqual(code, supervisor.EXIT_HALTED)
        self.wait_dead(life)
        halted = self.lines()[-1]
        self.assertEqual((halted["kind"], halted["payload"]["cause"], halted["payload"]["signal"]),
                         ("halted", "signal", name))
        self.assertEqual(halted["why"]["code"], "halt.signal")
        self.assertEqual(halted["payload"]["in_flight"]["target"], "doris-b2")
        self.assertTrue(halted["payload"]["in_flight"]["unconfirmed"])
        self.assertTrue(self.receipts_mod().verify(self.receipts).ok)
        self.assertEqual(supervisor.halt_cause(self.mode), "signal")
        code, said = self.run_now("quick")
        self.assertEqual(code, supervisor.EXIT_REFUSED_HALT)
        self.assertIn("halted (a stop signal reached the supervisor)", said[0])

    def test_sigterm_on_the_supervisor_kills_the_worker_and_is_a_stop_that_survives(self):
        sup, life = self.start_supervisor()
        os.kill(sup.pid, signal.SIGTERM)                 # `kill`, or a service manager stopping her
        self.assert_stopped_by("SIGTERM", sup.wait(WAIT_S), life)

    def test_sighup_on_the_supervisor_is_the_same_stop(self):
        sup, life = self.start_supervisor()
        os.kill(sup.pid, signal.SIGHUP)
        self.assert_stopped_by("SIGHUP", sup.wait(WAIT_S), life)

    def test_a_closed_terminal_hangs_up_the_whole_group_and_the_stop_is_still_written(self):
        sup, life = self.start_supervisor()
        os.killpg(sup.pid, signal.SIGHUP)                # the worker gets it too, and may die of it first
        self.assert_stopped_by("SIGHUP", sup.wait(WAIT_S), life)

    def test_a_hang_up_ignored_at_start_stays_ignored(self):
        sup, life = self.start_supervisor(prefix=[sys.executable, "-c", NOHUP])
        os.killpg(sup.pid, signal.SIGHUP)
        time.sleep(0.5)
        self.assertIsNone(sup.poll(), "started under nohup, she outlives the terminal")
        supervisor.request_stop(self.mode, "cli")
        self.assertEqual(sup.wait(WAIT_S), supervisor.EXIT_HALTED)
        self.wait_dead(life)
        self.assertEqual(self.lines()[-1]["payload"]["cause"], "cli")


def whole_lines(path):
    """The receipt lines written so far, read while the worker writes: whole lines only."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except (OSError, TypeError):
        return []
    lines = []
    for raw in data.split(b"\n")[:-1]:
        try:
            lines.append(json.loads(raw.decode("utf-8")))
        except ValueError:
            pass
    return lines


class SupervisorGoneTest(unittest.TestCase):
    """The supervisor itself is killed outright (SIGKILL, TerminateProcess: nothing of it runs) while
    her real worker waits for its next cycle. Nothing can kill the worker mid-call any more, so it
    stops itself: its lifeline closes, it writes HALT and its own halted line, and it exits."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="doris-gone-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = mini_sandbox(self.tmp, ("01-priya-request.eml",))
        self.state = os.path.join(self.tmp, "state")
        self.sdir = os.path.join(self.state, "sandbox")
        self.pidfile = os.path.join(self.tmp, "worker.pid")
        self.log = os.path.join(self.tmp, "terminal.log")

    def doris_run(self):
        """`python -m doris run` in a process of its own, its terminal a file: no key, so no model call."""
        env = dict(os.environ, ANTHROPIC_API_KEY="")
        with open(self.log, "ab") as out:
            return subprocess.Popen([sys.executable, fake_worker.THIS_SCRIPT, "doris-run", "--sandbox", self.root,
                                     "--state", self.state, "--cycle-s", "3600", "--marker", self.pidfile],
                                    stdout=out, stderr=subprocess.STDOUT, env=env)

    def receipts_path(self):
        runs = os.path.join(self.sdir, "runs")
        names = sorted(os.listdir(runs)) if os.path.isdir(runs) else []
        return os.path.join(runs, names[0], "receipts.jsonl") if names else None

    def worker_alive(self):
        """The real worker holds the ledger lock for its life; the OS lets go of it when it dies."""
        held = supervisor.acquire_lock(os.path.join(self.sdir, "ledger.lock"))
        if held is None:
            return True
        held.release()
        return False

    def wait_until(self, done, what):
        deadline = time.monotonic() + WAIT_S
        while not done():
            if time.monotonic() > deadline:
                with open(self.log, encoding="utf-8", errors="replace") as fh:
                    self.fail("timed out waiting for %s; the terminal said:\n%s" % (what, fh.read()))
            time.sleep(0.02)

    def leave_nothing_running(self, sup):
        if sup.poll() is None:
            sup.kill()
            sup.wait()
        if os.path.exists(self.pidfile) and self.worker_alive():
            with open(self.pidfile) as fh:
                pid = int(fh.read())
            try:
                os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            except OSError:
                pass
            self.wait_until(lambda: not self.worker_alive(), "the worker to die")   # a grandchild: no wait() for it

    def test_a_killed_supervisor_leaves_a_worker_that_halts_itself_and_writes_its_own_halted_line(self):
        sup = self.doris_run()
        self.addCleanup(self.leave_nothing_running, sup)
        # Her first pass closes Priya's request; then she waits an hour for her next cycle.
        self.wait_until(lambda: any(l["kind"] in ("decided", "held") and "outcome" in l["payload"]
                                    for l in whole_lines(self.receipts_path())), "her first pass")
        self.assertTrue(self.worker_alive())
        sup.kill()                                       # nothing of the supervisor runs: no handler, no finally
        sup.wait(WAIT_S)
        self.wait_until(lambda: not self.worker_alive(), "the worker to stop itself")

        path = self.receipts_path()
        self.assertTrue(receipts.verify(path, finished=True).ok)
        lines = receipts.read_lines(path)
        halted, summary = lines[-2], lines[-1]
        self.assertEqual((halted["kind"], halted["payload"]["cause"]), ("halted", "supervisor_gone"))
        self.assertEqual(halted["why"]["code"], "halt.supervisor_gone")
        self.assertEqual((halted["payload"]["in_flight"], halted["payload"]["dropped"]), ("none", []))
        self.assertEqual((summary["kind"], summary["payload"]["halted"], summary["payload"]["handled"]),
                         ("summary", "supervisor_gone", 1))
        self.assertEqual(supervisor.halt_cause("sandbox", state_dir=self.sdir), "supervisor_gone")
        with open(self.log, encoding="utf-8") as fh:
            said = fh.read()
        self.assertIn("Halted: her supervisor is gone, so she stopped herself", said)

        again = self.doris_run()                          # she stays halted until the command line resumes her
        self.addCleanup(self.leave_nothing_running, again)
        self.assertEqual(again.wait(WAIT_S), supervisor.EXIT_REFUSED_HALT)


class KillMidCallRealTest(KillMidCallTest):
    backend = "real"


class RestartRealTest(RestartTest):
    backend = "real"


if __name__ == "__main__":
    unittest.main()
