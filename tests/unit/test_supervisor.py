"""Unit tests for the kill switch (supervisor.py): halt files, resume, the one-runner lock,
the call in flight, the halted line and the poll loop.

Every test gets its own state/ and repo/ folders (paths.STATE and paths.REPO patched), so the
real state folder is never touched. receipts.py and ledger.py are faked here: the process tests
in tests/process/test_killswitch.py run the whole kill against a real child process.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from doris import paths, supervisor


class StateDirTest(unittest.TestCase):
    mode = "sandbox"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="doris-sup-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = os.path.join(self.tmp, "state")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        for name, value in (("STATE", self.state), ("REPO", self.repo)):
            patcher = mock.patch.object(paths, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.sdir = os.path.join(self.state, self.mode)
        self.halt = os.path.join(self.sdir, "HALT")

    def touch(self, path, data=b""):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        return path


# ------------------------------------------------------------------------------------------
# Halt files: HALT (ours, written atomically) and STOP (the user's, only ever read)
# ------------------------------------------------------------------------------------------
class HaltCauseTest(StateDirTest):
    def test_no_halt_when_nothing_is_on_disk(self):
        self.assertIsNone(supervisor.halt_cause(self.mode))

    def test_each_stop_cause_round_trips(self):
        for cause in ("cli", "watch_button", "budget_cap"):
            with self.subTest(cause=cause):
                supervisor.request_stop(self.mode, cause)
                self.assertEqual(supervisor.halt_cause(self.mode), cause)
                os.remove(self.halt)

    def test_default_cause_is_cli(self):
        supervisor.request_stop(self.mode)
        self.assertEqual(supervisor.halt_cause(self.mode), "cli")

    def test_unlisted_cause_is_refused_and_nothing_written(self):
        with self.assertRaises(ValueError):
            supervisor.request_stop(self.mode, "email")
        self.assertFalse(os.path.exists(self.halt))

    def test_the_first_cause_stays(self):
        supervisor.request_stop(self.mode, "budget_cap")
        supervisor.request_stop(self.mode, "cli")
        self.assertEqual(supervisor.halt_cause(self.mode), "budget_cap")

    def test_halt_goes_through_a_temp_file_and_replace(self):
        # A write in place would leave a HALT (maybe half written) when the replace fails.
        with mock.patch.object(supervisor.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                supervisor.request_stop(self.mode, "cli")
        self.assertFalse(os.path.exists(self.halt))
        self.assertEqual(os.listdir(self.sdir), [])

    def test_halt_records_when_the_stop_was_asked(self):
        supervisor.request_stop(self.mode, "watch_button")
        with open(self.halt, "rb") as fh:
            body = json.loads(fh.read().decode("utf-8"))
        self.assertEqual(body["cause"], "watch_button")
        self.assertRegex(body["requested_utc"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")

    def test_garbage_halt_fails_closed(self):
        self.touch(self.halt, b"\xff\xfe not json")
        with self.assertRaises(supervisor.HaltFileError):
            supervisor.halt_cause(self.mode)

    def test_empty_halt_fails_closed(self):
        self.touch(self.halt)
        with self.assertRaises(supervisor.HaltFileError):
            supervisor.halt_cause(self.mode)

    def test_halt_with_an_unlisted_cause_fails_closed(self):
        self.touch(self.halt, json.dumps({"cause": "email"}).encode("ascii"))
        with self.assertRaises(supervisor.HaltFileError):
            supervisor.halt_cause(self.mode)

    def test_halt_that_is_a_folder_fails_closed(self):
        os.makedirs(self.halt)
        with self.assertRaises(supervisor.HaltFileError):
            supervisor.halt_cause(self.mode)

    def test_stop_file_in_the_mode_folder(self):
        self.touch(os.path.join(self.sdir, "STOP"))
        self.assertEqual(supervisor.halt_cause(self.mode), "stop_file")

    def test_stop_file_at_the_repo_root(self):
        self.touch(os.path.join(self.repo, "STOP"))
        self.assertEqual(supervisor.halt_cause(self.mode), "stop_file")

    def test_stop_file_of_another_mode_does_not_halt_this_one(self):
        self.touch(os.path.join(self.state, "live", "STOP"))
        self.assertIsNone(supervisor.halt_cause("sandbox"))
        self.assertEqual(supervisor.halt_cause("live"), "stop_file")

    @unittest.skipIf(os.name == "nt", "symlinks need extra rights on Windows")
    def test_dangling_stop_symlink_still_halts(self):
        os.makedirs(self.sdir)
        os.symlink(os.path.join(self.tmp, "nowhere"), os.path.join(self.sdir, "STOP"))
        self.assertEqual(supervisor.halt_cause(self.mode), "stop_file")

    def test_halt_cause_wins_over_a_stop_file(self):
        supervisor.request_stop(self.mode, "watch_button")
        self.touch(os.path.join(self.repo, "STOP"))
        self.assertEqual(supervisor.halt_cause(self.mode), "watch_button")


class OwnStateDirTest(StateDirTest):
    """Throwaway runs (traps, redteam, tests) keep HALT, STOP and ledger.lock in their own folder
    (state_dir=...); the repo-root STOP still halts every mode."""

    def setUp(self):
        super().setUp()
        self.own = os.path.join(self.tmp, "throwaway", "run-1")

    def test_paths_sit_in_the_given_folder(self):
        self.assertEqual(supervisor.halt_file("throwaway", state_dir=self.own), os.path.join(self.own, "HALT"))
        self.assertEqual(supervisor.lock_file("throwaway", state_dir=self.own), os.path.join(self.own, "ledger.lock"))
        self.assertEqual(supervisor.stop_files("throwaway", state_dir=self.own),
                         [os.path.join(self.own, "STOP"), os.path.join(self.repo, "STOP")])

    def test_a_stop_in_its_folder_halts_it_and_nothing_else(self):
        supervisor.request_stop("throwaway", "budget_cap", state_dir=self.own)
        self.assertTrue(os.path.exists(os.path.join(self.own, "HALT")))
        self.assertEqual(supervisor.halt_cause("throwaway", state_dir=self.own), "budget_cap")
        self.assertEqual(supervisor.read_halt("throwaway", state_dir=self.own)["cause"], "budget_cap")
        self.assertIsNone(supervisor.halt_cause("throwaway"))
        self.assertIsNone(supervisor.halt_cause(self.mode))

    def test_a_stop_file_in_its_folder_halts_it(self):
        self.touch(os.path.join(self.own, "STOP"))
        self.assertEqual(supervisor.halt_cause("throwaway", state_dir=self.own), "stop_file")
        self.assertIsNone(supervisor.halt_cause(self.mode))

    def test_the_repo_root_stop_still_halts_it(self):
        self.touch(os.path.join(self.repo, "STOP"))
        self.assertEqual(supervisor.halt_cause("throwaway", state_dir=self.own), "stop_file")

    def test_resume_clears_its_halt_and_waits_for_its_lock(self):
        supervisor.request_stop("throwaway", "cli", state_dir=self.own)
        held = supervisor.acquire_lock(os.path.join(self.own, supervisor.LOCK_NAME))
        self.assertFalse(supervisor.resume("throwaway", state_dir=self.own).ok)
        held.release()
        res = supervisor.resume("throwaway", state_dir=self.own)
        self.assertTrue(res.ok)
        self.assertEqual(res.cleared, "cli")
        self.assertIsNone(supervisor.halt_cause("throwaway", state_dir=self.own))

    def test_run_supervised_refuses_on_a_halt_in_its_folder(self):
        supervisor.request_stop("throwaway", "cli", state_dir=self.own)
        marker = os.path.join(self.tmp, "worker-started")
        said = []
        code = supervisor.run_supervised("throwaway", RUN, [sys.executable, "-c", "open(%r, 'w').close()" % (marker,)],
                                         os.path.join(self.own, "receipts.jsonl"), os.path.join(self.own, "ledger.sqlite"),
                                         say=said.append, state_dir=self.own)
        self.assertEqual(code, supervisor.EXIT_REFUSED_HALT)
        self.assertFalse(os.path.exists(marker))

    def test_run_supervised_halts_on_a_stop_file_in_its_folder(self):
        stop = os.path.join(self.own, "STOP")
        argv = [sys.executable, "-c", "open(%r, 'w').close(); import time; time.sleep(30)" % (stop,)]
        said = []
        code = supervisor.run_supervised("throwaway", RUN, argv, os.path.join(self.own, "receipts.jsonl"),
                                         os.path.join(self.own, "ledger.sqlite"), say=said.append, poll_s=0.01,
                                         state_dir=self.own, receipts_module=FakeReceipts(),
                                         open_ledger=lambda path: FakeLedger())
        self.assertEqual(code, supervisor.EXIT_HALTED)
        self.assertEqual(supervisor.read_halt("throwaway", state_dir=self.own)["cause"], "stop_file")
        self.assertIsNone(supervisor.read_halt("throwaway"))


class ResumeTest(StateDirTest):
    def test_resume_clears_the_halt(self):
        supervisor.request_stop(self.mode, "cli")
        res = supervisor.resume(self.mode)
        self.assertTrue(res.ok)
        self.assertEqual(res.cleared, "cli")
        self.assertIsNone(supervisor.halt_cause(self.mode))

    def test_resume_never_deletes_a_stop_file(self):
        stop = self.touch(os.path.join(self.repo, "STOP"))
        supervisor.request_stop(self.mode, "stop_file")
        res = supervisor.resume(self.mode)
        self.assertFalse(res.ok)
        self.assertTrue(os.path.exists(stop))
        self.assertFalse(os.path.exists(self.halt))
        self.assertEqual(res.stop_files, [stop])
        self.assertIn(stop, res.message)
        self.assertIn("delete", res.message.lower())
        self.assertEqual(supervisor.halt_cause(self.mode), "stop_file")

    def test_resume_with_nothing_to_clear(self):
        res = supervisor.resume(self.mode)
        self.assertTrue(res.ok)
        self.assertIsNone(res.cleared)

    def test_resume_clears_an_unreadable_halt(self):
        self.touch(self.halt, b"garbage")
        res = supervisor.resume(self.mode)
        self.assertTrue(res.ok)
        self.assertEqual(res.cleared, "unreadable")
        self.assertIsNone(supervisor.halt_cause(self.mode))

    def test_resume_refuses_while_a_doris_holds_the_lock(self):
        supervisor.request_stop(self.mode, "cli")
        held = supervisor.acquire_lock(os.path.join(self.sdir, "ledger.lock"))
        self.addCleanup(held.release)
        res = supervisor.resume(self.mode)
        self.assertFalse(res.ok)
        self.assertIn("running", res.message)
        self.assertEqual(supervisor.halt_cause(self.mode), "cli")


class LockTest(StateDirTest):
    def test_a_second_holder_is_refused_until_release(self):
        path = os.path.join(self.sdir, "ledger.lock")
        first = supervisor.acquire_lock(path)
        self.assertIsNotNone(first)
        self.assertIsNone(supervisor.acquire_lock(path))
        first.release()
        again = supervisor.acquire_lock(path)
        self.assertIsNotNone(again)
        again.release()

    def test_held_says_whether_this_process_still_holds_it(self):
        handle = supervisor.acquire_lock(os.path.join(self.sdir, "ledger.lock"))
        self.assertTrue(handle.held)
        handle.release()
        self.assertFalse(handle.held)


# ------------------------------------------------------------------------------------------
# The call in flight (call pairing, CONTRACTS "added 10:35 PT")
# ------------------------------------------------------------------------------------------
def line(seq, kind, **payload):
    return {"seq": seq, "kind": kind, "payload": payload}


class InFlightTest(unittest.TestCase):
    def test_a_called_line_with_no_result_is_in_flight(self):
        lines = [line(1, "observed"),
                 line(2, "called", call="model", target="model-x"),
                 line(3, "result", call_seq=2),
                 line(4, "called", call="calendar_create", target="doris-abc")]
        self.assertEqual(supervisor.find_in_flight(lines),
                         {"seq": 4, "call": "calendar_create", "target": "doris-abc", "unconfirmed": True})

    def test_every_call_answered_means_none(self):
        lines = [line(1, "called", call="model", target="model-x"), line(2, "result", call_seq=1)]
        self.assertIsNone(supervisor.find_in_flight(lines))

    def test_no_lines_means_none(self):
        self.assertIsNone(supervisor.find_in_flight([]))

    def test_a_result_for_another_call_answers_only_that_call(self):
        lines = [line(1, "called", call="calendar_query", target="doris-q"),
                 line(2, "called", call="mail_file", target="T2"),
                 line(3, "result", call_seq=1)]
        self.assertEqual(supervisor.find_in_flight(lines),
                         {"seq": 2, "call": "mail_file", "target": "T2", "unconfirmed": False})

    def test_the_last_unanswered_call_is_the_one_in_flight(self):
        lines = [line(1, "called", call="calendar_get", target="doris-a"),
                 line(2, "called", call="model", target="model-x")]
        self.assertEqual(supervisor.find_in_flight(lines)["seq"], 2)

    def test_a_read_in_flight_is_not_unconfirmed(self):
        for call in ("calendar_query", "calendar_get", "model", "mail_fetch", "sent_lookup"):
            with self.subTest(call=call):
                got = supervisor.find_in_flight([line(5, "called", call=call, target="x")])
                self.assertFalse(got["unconfirmed"])

    def test_a_send_in_flight_is_unconfirmed_and_keeps_its_message_id(self):
        lines = [line(9, "called", call="send", target="T4", message_id="<abc@fastmail.com>")]
        self.assertEqual(supervisor.find_in_flight(lines),
                         {"seq": 9, "call": "send", "target": "T4", "unconfirmed": True,
                          "message_id": "<abc@fastmail.com>"})


# ------------------------------------------------------------------------------------------
# The halted line, written by the parent after the child is dead (fakes for receipts/ledger)
# ------------------------------------------------------------------------------------------
class FakeReceipts:
    """Stands in for receipts.py: read_lines returns the lines given; each writer keeps what it wrote.
    torn=True makes the writer move a torn tail aside the way the real one does."""

    def __init__(self, lines=(), torn=False):
        self.lines = list(lines)
        self.torn = torn
        self.writers = []

    def ReceiptWriter(self, path, run_id, mode, clock=None):
        if self.torn:
            with open(path + ".torn-1", "wb") as fh:
                fh.write(b'{"seq": 7, "kind": "res')
        writer = _FakeWriter(path, run_id, mode, len(self.lines))
        self.writers.append(writer)
        return writer

    def read_lines(self, path):
        return list(self.lines)


class _FakeWriter:
    def __init__(self, path, run_id, mode, seq):
        self.path, self.run_id, self.mode, self.seq = path, run_id, mode, seq
        self.written = []

    def write(self, kind, payload, subject="", why=None):
        self.seq += 1
        out = {"seq": self.seq, "kind": kind, "subject": subject, "payload": payload, "why": why}
        self.written.append(out)
        return out


class FakeLedger:
    def __init__(self, queued=(), unprocessed=(), threads=None):
        self.queued = list(queued)
        self.unprocessed = list(unprocessed)
        self.threads = dict(threads or {})
        self.calls = []
        self.closed = False

    def drop_queued(self, cause):
        self.calls.append(("drop_queued", cause))
        out, self.queued = self.queued, []
        return out

    def unprocessed_messages(self, run_id=None):
        self.calls.append(("unprocessed_messages", run_id))
        return list(self.unprocessed)

    def thread_of_message(self, key):
        return self.threads.get(key)

    def set_booking_state(self, booking_id, state):
        self.calls.append(("set_booking_state", booking_id, state))

    def set_send_state(self, message_id, state):
        self.calls.append(("set_send_state", message_id, state))

    def close(self):
        self.closed = True


RUN = "model-stub-sandbox-20261127T1900Z"


class RecordHaltTest(StateDirTest):
    def setUp(self):
        super().setUp()
        self.receipts = os.path.join(self.sdir, "runs", RUN, "receipts.jsonl")
        self.ledger_path = os.path.join(self.sdir, "ledger.sqlite")
        self.touch(self.receipts)
        self.touch(self.ledger_path)

    def record(self, cause, rec, led, **kw):
        return supervisor.record_halt(self.mode, RUN, cause, self.receipts, self.ledger_path,
                                      receipts_module=rec, open_ledger=lambda path: led, **kw)

    def test_halted_line_names_the_call_in_flight_and_every_dropped_item(self):
        rec = FakeReceipts([line(1, "observed"), line(2, "called", call="calendar_create", target="doris-b1")])
        led = FakeLedger(
            queued=[{"id": 7, "message_key": "m1@a.example", "kind": "send_reply", "detail": {"template": "T4"}},
                    {"id": 8, "message_key": "m2@b.example", "kind": "calendar_create",
                     "detail": json.dumps({"booking_id": "doris-b1"})}],
            unprocessed=[{"key": "m3@c.example", "thread_key": "t3@c.example", "status": "fetched"},
                         {"key": "m1@a.example", "thread_key": "t1@a.example", "status": "processing"}],
            threads={"m1@a.example": "t1@a.example", "m2@b.example": "t2@b.example"})
        out = self.record("cli", rec, led)

        writer = rec.writers[0]
        self.assertEqual((writer.path, writer.run_id, writer.mode), (self.receipts, RUN, "sandbox"))
        self.assertEqual(writer.written, [out])
        self.assertEqual(out["kind"], "halted")
        self.assertEqual(out["why"], {"code": "halt.cli", "text": "Halted: stop on the command line."})
        payload = out["payload"]
        self.assertEqual(payload["cause"], "cli")
        self.assertEqual(payload["in_flight"],
                         {"seq": 2, "call": "calendar_create", "target": "doris-b1", "unconfirmed": True})
        self.assertEqual(payload["dropped"], [
            {"kind": "send_reply", "thread": "t1@a.example", "message": "m1@a.example",
             "template": "T4", "booking_id": None},
            {"kind": "calendar_create", "thread": "t2@b.example", "message": "m2@b.example",
             "template": None, "booking_id": "doris-b1"},
            {"kind": "message", "thread": "t3@c.example", "message": "m3@c.example",
             "template": None, "booking_id": None},
        ])
        self.assertEqual(payload["ledger"], "ok")
        self.assertIn(("drop_queued", "cli"), led.calls)
        self.assertIn(("unprocessed_messages", RUN), led.calls)
        self.assertIn(("set_booking_state", "doris-b1", "unconfirmed"), led.calls)
        self.assertTrue(led.closed)

    def test_a_send_in_flight_is_marked_unknown_by_its_message_id(self):
        rec = FakeReceipts([line(3, "called", call="send", target="T2", message_id="<s1@fastmail.com>")])
        led = FakeLedger()
        out = self.record("watch_button", rec, led)
        self.assertIn(("set_send_state", "<s1@fastmail.com>", "unknown"), led.calls)
        self.assertTrue(out["payload"]["in_flight"]["unconfirmed"])
        self.assertEqual(out["why"]["code"], "halt.watch_button")

    def test_a_calendar_delete_in_flight_is_marked_unconfirmed(self):
        rec = FakeReceipts([line(3, "called", call="calendar_delete", target="doris-d1")])
        led = FakeLedger()
        self.record("cli", rec, led)
        self.assertIn(("set_booking_state", "doris-d1", "unconfirmed"), led.calls)

    def test_a_read_in_flight_marks_nothing(self):
        rec = FakeReceipts([line(3, "called", call="model", target="model-x")])
        led = FakeLedger()
        out = self.record("budget_cap", rec, led)
        self.assertFalse([c for c in led.calls if c[0].startswith("set_")])
        self.assertEqual(out["payload"]["in_flight"]["call"], "model")
        self.assertEqual(out["why"]["code"], "halt.budget_cap")

    def test_nothing_in_flight_and_nothing_queued(self):
        rec = FakeReceipts([line(1, "called", call="model", target="m"), line(2, "result", call_seq=1)])
        out = self.record("stop_file", rec, FakeLedger())
        self.assertIsNone(out["payload"]["in_flight"])
        self.assertEqual(out["payload"]["dropped"], [])
        self.assertEqual(out["why"]["code"], "halt.stop_file")

    def test_no_ledger_yet_means_nothing_was_queued_and_none_is_created(self):
        os.remove(self.ledger_path)
        opened = []
        out = supervisor.record_halt(self.mode, RUN, "cli", self.receipts, self.ledger_path,
                                     receipts_module=FakeReceipts(), open_ledger=opened.append)
        self.assertEqual(opened, [])
        self.assertFalse(os.path.exists(self.ledger_path))
        self.assertEqual(out["payload"]["dropped"], [])
        self.assertEqual(out["payload"]["ledger"], "absent")

    def test_a_ledger_that_cannot_be_read_is_never_an_empty_queue(self):
        def broken(path):
            raise OSError("database disk image is malformed")
        out = supervisor.record_halt(self.mode, RUN, "cli", self.receipts, self.ledger_path,
                                     receipts_module=FakeReceipts(), open_ledger=broken)
        self.assertIsNone(out["payload"]["dropped"])
        self.assertEqual(out["payload"]["ledger"], "unreadable")

    def test_a_torn_tail_moved_aside_is_named(self):
        out = self.record("cli", FakeReceipts(torn=True), FakeLedger())
        self.assertEqual(out["payload"]["torn_tail"], "receipts.jsonl.torn-1")

    def test_no_torn_tail_means_none(self):
        out = self.record("cli", FakeReceipts(), FakeLedger())
        self.assertIsNone(out["payload"]["torn_tail"])

    def test_the_stop_timing_is_carried(self):
        out = self.record("cli", FakeReceipts(), FakeLedger(), stop_to_dead_ms=87,
                          requested_utc="2026-11-27T19:00:00Z", worker_exit=-9)
        self.assertEqual(out["payload"]["stop_to_dead_ms"], 87)
        self.assertEqual(out["payload"]["requested_utc"], "2026-11-27T19:00:00Z")
        self.assertEqual(out["payload"]["worker_exit"], -9)

    def test_a_broken_receipt_chain_still_drops_the_queue_and_marks_the_call(self):
        class Tampered(FakeReceipts):
            def ReceiptWriter(self, path, run_id, mode, clock=None):
                raise ValueError("receipt chain broken at seq 2; nothing written past it")
        rec = Tampered([line(2, "called", call="calendar_create", target="doris-b1")])
        led = FakeLedger(queued=[{"message_key": "m1@a.example", "kind": "send_reply", "detail": {"template": "T4"}}])
        with self.assertRaises(ValueError):
            self.record("cli", rec, led)
        self.assertIn(("drop_queued", "cli"), led.calls)
        self.assertIn(("set_booking_state", "doris-b1", "unconfirmed"), led.calls)

    def test_no_receipt_file_yet_means_nothing_in_flight(self):
        os.remove(self.receipts)

        class Strict(FakeReceipts):
            def read_lines(self, path):
                raise FileNotFoundError(path)           # the real read_lines: a missing file raises
        out = self.record("cli", Strict(), FakeLedger())
        self.assertIsNone(out["payload"]["in_flight"])

    def test_an_unreadable_halt_file_is_said_in_the_line(self):
        out = self.record("stop_file", FakeReceipts(), FakeLedger(), halt_file_unreadable=True)
        self.assertEqual(out["payload"]["halt_file"], "unreadable")


# ------------------------------------------------------------------------------------------
# The poll loop, driven with a fake child (the process tests use a real one)
# ------------------------------------------------------------------------------------------
class FakeChild:
    """wait() times out while the child lives; on_wait(child) runs on each wait while alive."""

    def __init__(self, on_wait=None):
        self.on_wait = on_wait
        self.returncode = None
        self.waits = 0
        self.kills = 0

    def wait(self, timeout=None):
        self.waits += 1
        if self.returncode is None and self.on_wait:
            self.on_wait(self)
        if self.returncode is not None:
            return self.returncode
        if timeout is None:
            raise AssertionError("wait() with no timeout on a live child would hang")
        raise subprocess.TimeoutExpired("fake-worker", timeout)

    def poll(self):
        return self.returncode

    def kill(self):
        self.kills += 1
        self.returncode = -9


def on_wait_number(n, action):
    def hook(child):
        if child.waits == n:
            action(child)
    return hook


class WatchTest(StateDirTest):
    def watch(self, child):
        return supervisor.watch(child, self.mode, poll_s=0.01)

    def test_a_worker_that_ends_alone_is_not_a_halt(self):
        child = FakeChild(on_wait_number(3, lambda c: setattr(c, "returncode", 0)))
        stopped = self.watch(child)
        self.assertIsNone(stopped.cause)
        self.assertEqual(child.kills, 0)

    def test_a_halt_seen_while_waiting_kills_the_worker(self):
        child = FakeChild(on_wait_number(2, lambda c: supervisor.request_stop(self.mode, "watch_button")))
        stopped = self.watch(child)
        self.assertEqual(stopped.cause, "watch_button")
        self.assertGreaterEqual(child.kills, 1)
        self.assertEqual(child.returncode, -9)
        self.assertEqual(child.waits, 3)            # seen on the very next poll

    def test_a_stop_file_mid_run_kills_the_worker(self):
        child = FakeChild(on_wait_number(2, lambda c: self.touch(os.path.join(self.repo, "STOP"))))
        stopped = self.watch(child)
        self.assertEqual(stopped.cause, "stop_file")
        self.assertGreaterEqual(child.kills, 1)

    def test_a_budget_cap_written_by_the_worker_before_it_exits(self):
        def cap_and_exit(c):
            supervisor.request_stop(self.mode, "budget_cap")
            c.returncode = 0
        stopped = self.watch(FakeChild(on_wait_number(2, cap_and_exit)))
        self.assertEqual(stopped.cause, "budget_cap")

    def test_an_unreadable_halt_mid_run_fails_closed(self):
        child = FakeChild(on_wait_number(2, lambda c: self.touch(self.halt, b"\x00")))
        stopped = self.watch(child)
        self.assertEqual(stopped.cause, "stop_file")
        self.assertTrue(stopped.unreadable)
        self.assertGreaterEqual(child.kills, 1)

    def test_ctrl_c_is_a_stop_on_the_command_line_that_survives_a_restart(self):
        def interrupt(c):
            raise KeyboardInterrupt
        child = FakeChild(on_wait_number(2, interrupt))
        stopped = self.watch(child)
        self.assertEqual(stopped.cause, "cli")
        self.assertGreaterEqual(child.kills, 1)
        self.assertEqual(supervisor.halt_cause(self.mode), "cli")


# ------------------------------------------------------------------------------------------
# run_supervised refuses before any worker starts
# ------------------------------------------------------------------------------------------
class StopTimingTest(StateDirTest):
    def test_stop_to_dead_runs_from_the_request_written_in_halt(self):
        supervisor.request_stop(self.mode, "cli")
        requested = supervisor._requested_epoch(self.mode, "cli", 0)
        stopped = supervisor.Stopped("cli", requested + 0.050, requested + 0.080)
        self.assertEqual(supervisor.stop_timing(self.mode, stopped, 0)[1], 80)

    def test_a_halt_written_after_the_kill_counts_from_when_it_was_seen(self):
        # Ctrl+C: the worker is killed first and HALT written after, so HALT's time is too late.
        stopped = supervisor.Stopped("cli", 1000.0, 1000.040)
        with mock.patch.object(supervisor, "_requested_epoch", return_value=1000.5):
            requested, after_ms = supervisor.stop_timing(self.mode, stopped, 0)
        self.assertIsNone(requested)
        self.assertEqual(after_ms, 40)


class UncheckableStopTest(StateDirTest):
    def lstat_refused_for_stop(self):
        real = os.lstat

        def fake(path, *a, **kw):
            if os.path.basename(path) == "STOP":
                raise PermissionError(13, "Permission denied", path)
            return real(path, *a, **kw)
        return mock.patch.object(supervisor.os, "lstat", side_effect=fake)

    def test_halt_cause_fails_closed(self):
        with self.lstat_refused_for_stop():
            with self.assertRaises(supervisor.HaltFileError):
                supervisor.halt_cause(self.mode)

    def test_run_refuses_with_a_line_not_a_traceback(self):
        said = []
        with self.lstat_refused_for_stop():
            code = supervisor.run_supervised(self.mode, RUN, [sys.executable, "-c", "raise SystemExit(0)"],
                                             os.path.join(self.sdir, "r.jsonl"), os.path.join(self.sdir, "l.sqlite"),
                                             say=said.append)
        self.assertEqual(code, supervisor.EXIT_REFUSED_HALT)
        self.assertEqual(len(said), 1)
        self.assertIn("can't be read", said[0])

    def test_resume_counts_it_as_still_in_force(self):
        with self.lstat_refused_for_stop():
            res = supervisor.resume(self.mode)
        self.assertFalse(res.ok)
        self.assertEqual(len(res.stop_files), 2)


class HaltRecordingTest(StateDirTest):
    """run_supervised with a tiny real worker that drops a STOP file and exits 4 (G1 saw it)."""

    def run_g1_worker(self, **patches):
        stop = os.path.join(self.repo, "STOP")
        argv = [sys.executable, "-c", "open(%r, 'w').close(); raise SystemExit(4)" % (stop,)]
        said = []
        patches.setdefault("POLL_S", 0.01)
        with mock.patch.multiple(supervisor, **patches):
            code = supervisor.run_supervised(self.mode, RUN, argv, os.path.join(self.sdir, "r.jsonl"),
                                             os.path.join(self.sdir, "ledger.sqlite"), say=said.append,
                                             poll_s=0.01, receipts_module=FakeReceipts(), open_ledger=lambda path: FakeLedger())
        return code, said

    def test_the_lock_is_held_while_the_halted_line_is_written(self):
        lock_path = os.path.join(self.sdir, "ledger.lock")
        seen = {}

        def fake_record(*args, **kwargs):
            other = supervisor.acquire_lock(lock_path)
            seen["held_by_supervisor"] = other is None
            if other is not None:
                other.release()
            return {"why": {"code": "halt.stop_file", "text": "Halted: a STOP file was found."},
                    "payload": {"in_flight": None, "dropped": []}}
        code, said = self.run_g1_worker(record_halt=fake_record)
        self.assertEqual(code, supervisor.EXIT_HALTED)
        self.assertTrue(seen["held_by_supervisor"])
        free = supervisor.acquire_lock(lock_path)       # and let go afterwards
        self.assertIsNotNone(free)
        free.release()

    def test_a_ledger_another_doris_took_after_the_kill_is_never_written_through(self):
        def must_not_record(*args, **kwargs):
            raise AssertionError("the halted line was written without the lock")
        code, said = self.run_g1_worker(record_halt=must_not_record, wait_for_lock=lambda path: None)
        self.assertEqual(code, supervisor.EXIT_HALT_UNRECORDED)
        self.assertIn("another Doris holds the ledger", said[-1])

    def test_a_stop_file_halt_is_written_to_halt_so_it_outlives_the_file(self):
        code, said = self.run_g1_worker()
        self.assertEqual(code, supervisor.EXIT_HALTED)
        os.remove(os.path.join(self.repo, "STOP"))     # the user deletes it
        self.assertEqual(supervisor.halt_cause(self.mode), "stop_file")


class RefusalTest(StateDirTest):
    def run_it(self):
        self.marker = os.path.join(self.tmp, "worker-started")
        said = []
        code = supervisor.run_supervised(
            self.mode, RUN, [sys.executable, "-c", "open(%r, 'w').close()" % (self.marker,)],
            os.path.join(self.sdir, "runs", RUN, "receipts.jsonl"), os.path.join(self.sdir, "ledger.sqlite"),
            say=said.append)
        self.assertFalse(os.path.exists(self.marker), "the worker must not start")
        self.assertEqual(len(said), 1, said)
        return code, said[0]

    def test_refuses_while_halted_and_says_why(self):
        supervisor.request_stop(self.mode, "cli")
        code, said = self.run_it()
        self.assertEqual(code, supervisor.EXIT_REFUSED_HALT)
        self.assertIn("halted (stop on the command line)", said)
        self.assertIn("resume", said)

    def test_refuses_while_a_stop_file_is_there_and_names_it(self):
        stop = self.touch(os.path.join(self.repo, "STOP"))
        code, said = self.run_it()
        self.assertEqual(code, supervisor.EXIT_REFUSED_HALT)
        self.assertIn(stop, said)
        self.assertIn("Delete", said)
        self.assertTrue(os.path.exists(stop))

    def test_refuses_on_a_halt_file_it_cannot_read(self):
        self.touch(self.halt, b"garbage")
        code, said = self.run_it()
        self.assertEqual(code, supervisor.EXIT_REFUSED_HALT)
        self.assertIn("can't be read", said)

    def test_refuses_when_another_doris_holds_the_lock(self):
        held = supervisor.acquire_lock(os.path.join(self.sdir, "ledger.lock"))
        self.addCleanup(held.release)
        code, said = self.run_it()
        self.assertEqual(code, supervisor.EXIT_LOCKED)
        self.assertIn("another Doris is already running on this ledger", said)


class RelockTest(StateDirTest):
    """After the worker dies the parent takes the ledger lock before it writes the halted line,
    waiting a moment for the OS to let go of the dead worker's lock."""

    def test_a_free_lock_is_taken(self):
        held = supervisor.wait_for_lock(os.path.join(self.sdir, "ledger.lock"), 0.05)
        self.assertIsNotNone(held)
        held.release()

    def test_a_lock_someone_else_keeps_is_given_up_on(self):
        path = os.path.join(self.sdir, "ledger.lock")
        other = supervisor.acquire_lock(path)
        self.addCleanup(other.release)
        self.assertIsNone(supervisor.wait_for_lock(path, 0.05))

    def test_the_lock_file_sits_next_to_the_ledger(self):
        self.assertEqual(supervisor.lock_path_for(os.path.join(self.sdir, "ledger.sqlite")),
                         os.path.join(self.sdir, "ledger.lock"))


if __name__ == "__main__":
    unittest.main()
