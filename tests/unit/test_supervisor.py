"""Unit tests for the kill switch (supervisor.py): halt files, resume, the one-runner lock,
the call in flight, the halted line and the poll loop.

Every test gets its own state/ and repo/ folders (paths.STATE and paths.REPO patched), so the
real state folder is never touched. receipts.py and ledger.py are faked here: the process tests
in tests/process/test_killswitch.py run the whole kill against a real child process.
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

    def test_a_stop_signal_and_a_supervisor_gone_are_causes_that_survive_like_the_rest(self):
        for cause in ("signal", "supervisor_gone"):
            with self.subTest(cause=cause):
                supervisor.request_stop(self.mode, cause)
                self.assertEqual(supervisor.halt_cause(self.mode), cause)
                res = supervisor.resume(self.mode)
                self.assertEqual((res.ok, res.cleared), (True, cause))
                self.assertIn(supervisor.CAUSE_WORDS[cause], res.message)

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
        self.assertTrue(os.path.exists(self.halt))          # a failed resume clears nothing
        self.assertEqual(res.stop_files, [stop])
        self.assertIn(stop, res.message)
        self.assertIn("delete", res.message.lower())
        self.assertEqual(supervisor.halt_cause(self.mode), "stop_file")

    def test_a_failed_resume_changes_nothing_so_deleting_the_stop_file_alone_never_starts_her(self):
        stop = self.touch(os.path.join(self.sdir, "STOP"))
        supervisor.request_stop(self.mode, "cli")
        with open(self.halt, "rb") as fh:
            before = fh.read()
        res = supervisor.resume(self.mode)
        self.assertEqual((res.ok, res.cleared, res.stop_files), (False, None, [stop]))
        self.assertTrue(res.message.startswith("Not resumed: "), res.message)
        self.assertIn("resume", res.message.replace("Not resumed", ""))   # says to resume again after
        with open(self.halt, "rb") as fh:
            self.assertEqual(fh.read(), before)               # HALT untouched, its first cause kept
        os.remove(stop)                                       # the user deletes the STOP file
        self.assertEqual(supervisor.halt_cause(self.mode), "cli")
        self.assertIn("halted (stop on the command line)", supervisor._halt_refusal(self.mode))
        res = supervisor.resume(self.mode)                    # only now does a resume clear it
        self.assertEqual((res.ok, res.cleared), (True, "cli"))
        self.assertIsNone(supervisor.halt_cause(self.mode))

    def test_a_stop_file_with_no_halt_is_named_and_nothing_is_written(self):
        stop = self.touch(os.path.join(self.repo, "STOP"))
        res = supervisor.resume(self.mode)
        self.assertEqual((res.ok, res.cleared, res.stop_files), (False, None, [stop]))
        self.assertFalse(os.path.exists(self.halt))
        self.assertIn(stop, res.message)

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

    def test_a_send_in_flight_is_marked_sending_by_its_message_id(self):
        rec = FakeReceipts([line(3, "called", call="send", target="T2", message_id="<s1@fastmail.com>")])
        led = FakeLedger()
        out = self.record("watch_button", rec, led)
        self.assertIn(("set_send_state", "<s1@fastmail.com>", "sending"), led.calls)
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

    def test_a_stop_signal_is_named_in_the_line(self):
        rec = FakeReceipts([line(3, "called", call="calendar_create", target="doris-b1")])
        led = FakeLedger()
        out = self.record("signal", rec, led, signal="SIGTERM")
        self.assertEqual(out["payload"]["cause"], "signal")
        self.assertEqual(out["payload"]["signal"], "SIGTERM")
        self.assertEqual(out["why"], {"code": "halt.signal",
                                      "text": "Halted: a stop signal (SIGTERM, SIGHUP or Ctrl+Break) reached the supervisor."})
        self.assertIn(("set_booking_state", "doris-b1", "unconfirmed"), led.calls)
        self.assertNotIn("signal", self.record("cli", FakeReceipts(), FakeLedger())["payload"])

    def test_a_key_that_could_spell_the_meeting_link_is_shown_by_its_hash(self):
        """Review of 7e1a9fa, item 2: a Message-ID or thread key can hold the link (Message-ID:
        <meet.example/live-loop-room@x>), and the supervisor never loads the env, so it cannot redact
        it as the worker's receipts do. A key that holds "/", ":", "%" or "&" (a character every form of
        an https link needs) or anything outside plain ASCII is shown by its hash (ledger.key_sha256)."""
        from doris.ledger import key_sha256

        def hashed(key):
            return "key-sha256:" + key_sha256(key)
        linked, escaped = "meet.example/live-loop-room@x.example", "<meet.example%2Flive-loop-room@x.example>"
        entity, wide = "meet.example&#47;live-loop-room@x.example", "meet.example／live-loop-room@x.example"
        schemed, thread = "https:meet.example@x.example", "<meet.example/live-loop-room/t1@x.example>"
        raw_key, gmail = "sha256:" + "ab" * 32, "CAHk-=wh+x_y.z!#$'*?^`{|}~@mail.gmail.com"
        led = FakeLedger(
            queued=[{"message_key": linked, "kind": "send_reply", "detail": {"template": "T2"}},
                    {"message_key": "m2@b.example", "kind": "send_reply", "detail": {"thread_key": escaped}}],
            unprocessed=[{"key": entity, "thread_key": wide, "status": "fetched"},
                         {"key": schemed, "thread_key": "t4@d.example", "status": "fetched"},
                         {"key": raw_key, "thread_key": None, "status": "fetched"},
                         {"key": gmail, "thread_key": "[t5]@e.example", "status": "processing"}],
            threads={linked: thread})
        out = self.record("cli", FakeReceipts(), led)
        self.assertEqual([(d["message"], d["thread"]) for d in out["payload"]["dropped"]], [
            (hashed(linked), hashed(thread)),
            ("m2@b.example", hashed(escaped)),
            (hashed(entity), hashed(wide)),
            (hashed(schemed), "t4@d.example"),
            (raw_key, None),                    # a mail with no Message-ID: already a hash
            (gmail, "[t5]@e.example"),          # plain Message-IDs as written
        ])
        self.assertEqual(hashed(thread), hashed(thread.strip("<>")))     # the ledger's own key form
        self.assertEqual(out["payload"]["dropped"][1]["template"], None)
        self.assertNotIn("live-loop-room", json.dumps(out, ensure_ascii=False))
        self.assertEqual(out["payload"]["ledger"], "ok")


class RecordHaltLinkTest(StateDirTest):
    """Review of 7e1a9fa, item 2, against the real ledger and receipt writer: the halted line the
    supervisor writes never shows the meeting link, whatever a dropped key holds."""

    ROOM = "https://meet.example/live-loop-room"
    LINKS = (ROOM, "https://meet.example/doris-sandbox-room", "https://zoom.us", "https://zoom.us?pwd=a1",
             "https://zoom.us/j/123?pwd=a&b=c", "https://u@meet.example:8443/room.x")

    def setUp(self):
        super().setUp()
        from doris import templates
        self.templates = templates
        before = templates.use_meeting_link(self.ROOM)
        self.addCleanup(templates.use_meeting_link, before)

    def test_the_halted_line_from_a_real_ledger_never_shows_the_link(self):
        import datetime
        from doris import receipts
        from doris.ledger import Ledger
        receipts_path = os.path.join(self.sdir, "runs", RUN, "receipts.jsonl")
        ledger_path = os.path.join(self.sdir, "ledger.sqlite")
        os.makedirs(os.path.dirname(receipts_path))
        at = datetime.datetime(2026, 11, 27, 19, 0, tzinfo=datetime.timezone.utc)
        with Ledger(ledger_path) as led:
            led.record_message("meet.example/live-loop-room@x.example", "<meet.example/live-loop-room@x.example>",
                               "priya@b.example", at, "processing", run_id=RUN)
            led.enqueue_action(RUN, "meet.example/live-loop-room@x.example", "send_reply", {"template": "T2"})
            led.record_message("m2@b.example", "HTTPS://MEET.EXAMPLE/live-loop-room", "sam@b.example", at, "fetched",
                               run_id=RUN)
            led.record_message("meet.example%2Flive-loop-room@x.example", "t3@c.example", "kim@c.example", at,
                               "fetched", run_id=RUN)
        out = supervisor.record_halt(self.mode, RUN, "cli", receipts_path, ledger_path)
        with open(receipts_path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertEqual([d["kind"] for d in out["payload"]["dropped"]], ["send_reply", "message", "message"])
        self.assertEqual(out["payload"]["dropped"][2]["thread"], "t3@c.example")
        self.assertNotIn("live-loop-room", text)
        self.assertFalse(self.templates.has_link(text))
        self.assertTrue(receipts.verify(receipts_path).ok)

    def forms(self, link):
        """Ways a key can spell the link: the ones RedactLinkTest covers, escaped, wrapped and embedded."""
        rest = link.split("://", 1)[1]
        ways = [link, link.upper(), "http://" + rest, "hxxps://" + rest, "hxxp://" + rest + "/",
                "https:\\/\\/" + rest.replace("/", "\\/"), "hxxps[://]" + rest.replace(".", "[.]"),
                rest.replace("@", "[@]"), link.replace("&", "&amp;"), link.replace("-", "%2D"),
                "".join("&#%d;" % ord(c) for c in link), "".join("%%%02X" % ord(c) for c in link),
                "\n ".join(link)]
        if "/" in rest.rstrip("/"):
            ways.append(rest)
        return [w for way in ways for w in (way, "<%s@x.example>" % way, "a" + way + "b")]

    def test_every_key_that_shows_the_link_is_hashed_and_every_key_shown_as_written_is_plain(self):
        """The claim the plain-key rule rests on, for links of every https shape: a key the redactors
        would touch is never shown as written, and the same keys with "/", ":", "%" and "&" turned into
        plain characters (shown as written) never spell the link."""
        t = self.templates
        for link in self.LINKS:
            t.use_meeting_link(link)
            found = plain = 0
            for key in self.forms(link):
                if not (t.has_link(key) or t.redact_link(key) != key):
                    continue
                found += 1
                shown = supervisor.shown_key(key)
                self.assertNotEqual(shown, key, (link, key))
                self.assertFalse(t.has_link(shown), (link, key))
                for c in "._-=+~":
                    flat = "".join(c if ch in "/:%&" else ch for ch in key).replace("\n ", "")
                    if supervisor.shown_key(flat) == flat:
                        plain += 1
                        self.assertFalse(t.has_link(flat), (link, flat))
                        self.assertEqual(t.redact_link(flat), flat, (link, flat))
            self.assertGreaterEqual(found, 30, link)
            self.assertGreaterEqual(plain, 100, link)


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

    def test_a_stop_signal_kills_the_worker_and_is_a_stop_that_survives_a_restart(self):
        signals = supervisor.StopSignals()            # not entered: the test plays the handler
        child = FakeChild(on_wait_number(2, lambda c: signals.note(signal.SIGTERM)))
        stopped = supervisor.watch(child, self.mode, poll_s=0.01, signals=signals)
        self.assertEqual((stopped.cause, stopped.signal), ("signal", "SIGTERM"))
        self.assertGreaterEqual(child.kills, 1)
        self.assertEqual(child.waits, 3)            # acted on at the very next poll
        self.assertEqual(supervisor.halt_cause(self.mode), "signal")

    @unittest.skipUnless(hasattr(signal, "SIGHUP"), "no SIGHUP on this platform")
    def test_a_hang_up_that_ended_the_worker_too_is_still_a_stop(self):
        # A closed terminal sends SIGHUP to the worker as well: it may be dead before the next poll.
        signals = supervisor.StopSignals()

        def hang_up(c):
            signals.note(signal.SIGHUP)
            c.returncode = -1
        stopped = supervisor.watch(FakeChild(on_wait_number(2, hang_up)), self.mode, poll_s=0.01, signals=signals)
        self.assertEqual((stopped.cause, stopped.signal), ("signal", "SIGHUP"))
        self.assertEqual(supervisor.halt_cause(self.mode), "signal")

    def test_a_halt_already_on_disk_keeps_its_cause_when_a_signal_comes_too(self):
        signals = supervisor.StopSignals()

        def both(c):
            supervisor.request_stop(self.mode, "watch_button")
            signals.note(signal.SIGTERM)
        stopped = supervisor.watch(FakeChild(on_wait_number(2, both)), self.mode, poll_s=0.01, signals=signals)
        self.assertEqual(stopped.cause, "watch_button")
        self.assertIsNone(stopped.signal)
        self.assertEqual(supervisor.halt_cause(self.mode), "watch_button")


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


# ------------------------------------------------------------------------------------------
# A stop signal on the supervisor, and a supervisor that is gone
# ------------------------------------------------------------------------------------------
@unittest.skipIf(os.name == "nt", "SIGTERM cannot be caught on Windows, and it has no SIGHUP")
class StopSignalsTest(unittest.TestCase):
    """While the worker runs, SIGTERM and SIGHUP on the supervisor are stops, as Ctrl+C is; the
    handlers before are put back after. (The process tests send real ones to a real supervisor.)"""

    def caught(self, number, before):
        handler = signal.getsignal(number)
        self.assertNotIn(handler, (signal.SIG_DFL, signal.SIG_IGN, None, before), "not caught: never raise it")
        return handler

    def test_term_and_hang_up_are_caught_while_the_worker_runs_and_put_back_after(self):
        numbers = (signal.SIGTERM, signal.SIGHUP)
        before = {n: signal.getsignal(n) for n in numbers}
        with supervisor.StopSignals() as signals:
            for n in numbers:
                self.caught(n, before[n])
            self.assertIsNone(signals.seen)
            signal.raise_signal(signal.SIGHUP)
            self.assertEqual(signals.seen, "SIGHUP")
            signal.raise_signal(signal.SIGTERM)
            self.assertEqual(signals.seen, "SIGHUP")      # the first one is the one named
        for n in numbers:
            self.assertEqual(signal.getsignal(n), before[n])

    def test_a_hang_up_ignored_at_start_stays_ignored(self):
        # Started under nohup: whoever started her asked for her to outlive the terminal.
        before = signal.signal(signal.SIGHUP, signal.SIG_IGN)
        self.addCleanup(signal.signal, signal.SIGHUP, signal.SIG_DFL if before is None else before)
        with supervisor.StopSignals():
            self.assertEqual(signal.getsignal(signal.SIGHUP), signal.SIG_IGN)
            self.caught(signal.SIGTERM, None)
        self.assertEqual(signal.getsignal(signal.SIGHUP), signal.SIG_IGN)

    def test_off_the_main_thread_nothing_is_set_and_nothing_fails(self):
        before = signal.getsignal(signal.SIGTERM)
        seen = {}

        def target():
            with supervisor.StopSignals() as signals:
                seen["handler"], seen["seen"] = signal.getsignal(signal.SIGTERM), signals.seen
        thread = threading.Thread(target=target)
        thread.start()
        thread.join(5)
        self.assertEqual(seen, {"handler": before, "seen": None})


class LifelineTest(StateDirTest):
    """The worker's tie to its supervisor: a pipe whose write end only the supervisor holds and
    never writes to. However the supervisor ends, the OS closes that end and the worker reads EOF."""

    def setUp(self):
        super().setUp()
        self.ends, self.lines = {}, []
        self.addCleanup(self.end_lines)           # after the path patches, so it runs before they are undone

    def pipe(self):
        self.ends.update(zip(("r", "w"), os.pipe()))
        return self.ends["r"]

    def started(self, line):
        self.lines.append(line)
        return line

    def end_lines(self):
        """No lifeline outlives its test: close the supervisor's end (it wakes a reader still
        waiting), wait for every lifeline thread to finish, and only then close the reader's end."""
        if "w" in self.ends:
            os.close(self.ends.pop("w"))
        for line in self.lines:
            if line.thread is not None:
                line.thread.join(5)
                self.assertFalse(line.thread.is_alive(), "a lifeline thread outlived its test")
        if "r" in self.ends:
            os.close(self.ends.pop("r"))

    def supervisor_dies(self):
        os.close(self.ends.pop("w"))

    def watched(self, line):
        line.thread.join(5)
        self.assertFalse(line.thread.is_alive(), "the lifeline never saw its end close")
        return line

    def test_while_the_supervisor_holds_its_end_nothing_happens(self):
        line = self.started(supervisor.Lifeline(self.pipe(), self.mode).start())
        line.thread.join(0.2)
        self.assertTrue(line.thread.is_alive())
        self.assertFalse(line.gone)
        self.assertIsNone(supervisor.halt_cause(self.mode))

    def test_when_the_supervisor_is_gone_she_writes_halt(self):
        line = self.started(supervisor.Lifeline(self.pipe(), self.mode).start())
        self.supervisor_dies()
        self.assertTrue(self.watched(line).gone)
        self.assertEqual(supervisor.halt_cause(self.mode), "supervisor_gone")

    def test_a_halt_already_in_force_keeps_its_cause(self):
        supervisor.request_stop(self.mode, "watch_button")
        line = self.started(supervisor.Lifeline(self.pipe(), self.mode).start())
        self.supervisor_dies()
        self.assertTrue(self.watched(line).gone)
        self.assertEqual(supervisor.halt_cause(self.mode), "watch_button")

    def test_a_halt_that_cannot_be_written_still_leaves_her_knowing(self):
        with mock.patch.object(supervisor, "request_stop", side_effect=OSError(28, "No space left on device")):
            line = self.started(supervisor.Lifeline(self.pipe(), self.mode).start())
            self.supervisor_dies()
            self.watched(line)
        self.assertTrue(line.gone)                  # the run reads it too (G1 and every halt check)
        self.assertIsNone(supervisor.halt_cause(self.mode))

    def test_halt_goes_to_the_folder_she_was_started_with_whatever_the_paths_say_later(self):
        # A lifeline once outlived its test: by the time it fired, paths.STATE was the repository's
        # again, and HALT landed in the real state/sandbox. Her folder is fixed when she starts.
        line = self.started(supervisor.Lifeline(self.pipe(), self.mode).start())
        elsewhere = os.path.join(self.tmp, "elsewhere")
        with mock.patch.object(paths, "STATE", elsewhere):
            self.supervisor_dies()
            self.watched(line)
        self.assertFalse(os.path.exists(elsewhere))
        self.assertEqual(supervisor.halt_cause(self.mode), "supervisor_gone")

    def test_a_state_folder_of_her_own(self):
        own = os.path.join(self.tmp, "own")
        line = self.started(supervisor.Lifeline(self.pipe(), "throwaway", own).start())
        self.supervisor_dies()
        self.watched(line)
        self.assertEqual(supervisor.halt_cause("throwaway", state_dir=own), "supervisor_gone")
        self.assertIsNone(supervisor.halt_cause(self.mode))

    def test_only_a_worker_its_supervisor_gave_one_watches_its_stdin(self):
        self.assertIsNone(supervisor.lifeline(self.mode, environ={}))
        self.assertIsNone(supervisor.lifeline(self.mode, environ={supervisor.LIFELINE_ENV: "1"}))
        line = self.started(supervisor.lifeline(self.mode, environ={supervisor.LIFELINE_ENV: supervisor.LIFELINE_STDIN},
                                                fd=self.pipe()))
        self.assertIsInstance(line, supervisor.Lifeline)
        self.supervisor_dies()
        self.watched(line)
        self.assertEqual(supervisor.halt_cause(self.mode), "supervisor_gone")

    def test_the_worker_is_started_with_its_lifeline_on_stdin_and_told_so(self):
        seen = os.path.join(self.tmp, "seen.json")
        code = ("import json, os, stat; json.dump({'env': os.environ.get(%r), 'pipe': stat.S_ISFIFO(os.fstat(0).st_mode)},"
                " open(%r, 'w'))" % (supervisor.LIFELINE_ENV, seen))
        child = supervisor._spawn([sys.executable, "-c", code], self.tmp)
        try:
            self.assertEqual(child.wait(20), 0)
        finally:
            supervisor._kill(child)
            supervisor._let_go(child)
        self.assertTrue(child.stdin.closed)
        with open(seen, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {"env": supervisor.LIFELINE_STDIN, "pipe": True})
        self.assertNotIn(supervisor.LIFELINE_ENV, os.environ)    # the supervisor's own environment is untouched

    def test_run_supervised_lets_go_of_its_end_once_the_worker_is_dead(self):
        children, real = [], supervisor._spawn

        def spawn(argv, cwd):
            children.append(real(argv, cwd))
            return children[-1]
        with mock.patch.object(supervisor, "_spawn", spawn):
            code = supervisor.run_supervised(self.mode, RUN, [sys.executable, "-c", "pass"],
                                             os.path.join(self.sdir, "r.jsonl"), os.path.join(self.sdir, "l.sqlite"),
                                             say=[].append, poll_s=0.01)
        self.assertEqual(code, 0)
        self.assertTrue(children[0].stdin.closed)


class SayTest(unittest.TestCase):
    def test_the_line_said_after_a_signal_names_it(self):
        line = {"why": {"code": "halt.signal",
                        "text": "Halted: a stop signal (SIGTERM, SIGHUP or Ctrl+Break) reached the supervisor."},
                "payload": {"cause": "signal", "signal": "SIGHUP", "in_flight": None, "dropped": []}}
        said = supervisor._summary(line, 3)
        self.assertTrue(said.startswith("Halted: a stop signal (SIGTERM, SIGHUP or Ctrl+Break) reached the supervisor. "
                                        "The signal was SIGHUP. The worker was dead 3 ms after the stop."), said)


    def test_a_line_to_a_terminal_that_is_gone_is_no_error(self):
        # A hang-up closes the terminal before the supervisor's last line: the receipts keep the record.
        class Gone:
            def write(self, text):
                raise OSError(5, "Input/output error")

            def flush(self):
                raise BrokenPipeError(32, "Broken pipe")
        with mock.patch.object(sys, "stdout", Gone()):
            supervisor._say("Halted: a stop signal (SIGTERM, SIGHUP or Ctrl+Break) reached the supervisor.")


if __name__ == "__main__":
    unittest.main()
