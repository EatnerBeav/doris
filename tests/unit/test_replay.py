"""python -m doris replay: a finished run told step by step, built only from its receipts (decision 50).

replay runs verify's chain check first and refuses a broken chain in one line naming the first bad
seq. Then, for each message: what she saw, what she decided and why, each gate check and its verdict,
each call and what came back, and each read-back. Sensitive mail shows as skipped by design; a halted
run shows the halt, the call in flight and the dropped queue. It reads the receipts and nothing else:
no network, no model, no mail, no calendar, and it writes nothing anywhere.
"""

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from doris import loop, netguard, paths, receipts, replay, supervisor
from doris.adapters import sandbox as sandbox_mod
from tests import left_behind
from tests.unit.test_restart import DORIS, OWNER, add, mini_sandbox

RUN1 = "model-live-throwaway-traps-20261127T2207Z"      # Trap Pack run 1, model called live, committed
PRIYA_KEY = "20261201.0802.priya.quick-call@northwind.example"
MARCUS_PICK_KEY = "20261201.0940.marcus.pick@brightlane.example"
DEMO_NAMES = tuple(sorted(n for n in os.listdir(os.path.join(paths.SANDBOX, "inbox")) if n.endswith(".eml")))

SENSITIVE = (b"From: Alerts <alerts@bankish.example>\r\n"
             b"To: Shane Bornstein <whizz@fastmail.com>\r\n"
             b"Subject: Your verification code\r\n"
             b"Date: Tue, 01 Dec 2026 08:03:00 -0800\r\n"
             b"Message-ID: <20261201.0803.code@bankish.example>\r\n"
             b"MIME-Version: 1.0\r\n"
             b"Content-Type: text/plain; charset=\"utf-8\"\r\n"
             b"Content-Transfer-Encoding: 7bit\r\n\r\n"
             b"Your verification code is 482913. It expires soon.\r\n")


def forge(path, rows):
    """A chain written by hand, hashes and all, bypassing the writer's own checks: [(kind, subject, payload, why)]."""
    prev, out = receipts.GENESIS, []
    for seq, (kind, subject, payload, why) in enumerate(rows, 1):
        line = {"ts": "2026-12-01T16:00:00Z", "run_id": "model-stub-sandbox-forged", "mode": "sandbox", "seq": seq,
                "kind": kind, "subject": subject, "payload": payload, "prev": prev}
        if why is not None:
            line["why"] = why
        line["hash"] = prev = hashlib.sha256(prev.encode("ascii") + receipts.canonical(line)).hexdigest()
        out.append(receipts.canonical(line) + b"\n")
    with open(path, "wb") as fh:
        fh.write(b"".join(out))


def run_day(tmp, names, name="model-stub-throwaway-replay"):
    """A sandbox day over these inbox files, stub model, on throwaway state; returns its summary."""
    root = mini_sandbox(tmp, names)
    return root, loop.RunConfig(mode="throwaway", run_name=name, state_dir=os.path.join(tmp, "state"),
                                sandbox_root=root, owner_addr=OWNER, doris_addr=DORIS, model_source="stub",
                                clock_mode="advance", retry_wait_s=0, digest=True)


def replay_cli(argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = replay.main(argv)
    return code, out.getvalue()


def in_order(test, text, parts):
    """Each part appears in text, each after the one before it."""
    at = 0
    for part in parts:
        found = text.find(part, at)
        test.assertNotEqual(found, -1, "%r not found after position %d in:\n%s" % (part, at, text))
        at = found + len(part)


def blocks(text):
    """Each message's block of a replay: its title line up to the next title, item or run block."""
    out, current = [], None
    for line in text.split("\n"):
        if line.startswith(("Message ", "The run", "Item ")):
            current = [] if line.startswith("Message ") else None
            if current is not None:
                out.append(current)
        if current is not None:
            current.append(line)
    return ["\n".join(b).rstrip() for b in out]


def block(text, key):
    """The one message block naming this Message-ID (or holding this text, for mail named by a hash)."""
    found = [b for b in blocks(text) if ("Message-ID %s," % key) in b or (key.startswith(" ") and key in b)]
    assert len(found) == 1, "%d blocks for %r in:\n%s" % (len(found), key, text)
    return found[0]


class DemoDay(unittest.TestCase):
    """The demo's day, run here on throwaway state the way `demo` runs it, then replayed."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="doris-replay-")
        _root, cfg = run_day(cls.tmp, DEMO_NAMES)
        cls.summary = loop.run(cfg)
        cls.path = cls.summary.receipts_path
        cls.lines = receipts.read_lines(cls.path)
        cls.code, cls.out = replay_cli([cls.path])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, True)

    def test_the_run_is_whole_and_steps_through(self):
        self.assertTrue(self.summary.verify_ok)
        self.assertEqual(self.code, 0, self.out)
        self.assertTrue(self.out.startswith("Replay of run model-stub-throwaway-replay"), self.out[:300])
        self.assertIn("Chain: ok, %d lines" % len(self.lines), self.out)
        self.assertIn("built-in keyword stand-in (stub)", self.out)

    def test_every_message_gets_a_block_from_saw_to_its_closing_line(self):
        keys = []
        for line in self.lines:
            if line["kind"] == "observed" and line["subject"] and line["subject"] not in keys:
                keys.append(line["subject"])
        self.assertEqual(len(keys), 15)
        for n, key in enumerate(keys, 1):
            text = block(self.out, key)
            self.assertTrue(text.startswith("Message %d of 15" % n), text[:200])
            self.assertIn("  Saw ", text)
            self.assertRegex(text.rstrip().splitlines()[-1], r"^  (Closed|Held) ", text)

    def test_one_message_reads_as_a_person_would_tell_it(self):
        code, out = replay_cli([self.path, "--message", "<%s>" % PRIYA_KEY])
        self.assertEqual(code, 0, out)
        self.assertEqual(out.count("\nMessage "), 1, out)
        text = block(out, PRIYA_KEY)
        in_order(self, text, [
            "Message 1 of 15: Priya Raman <priya@northwind.example>",
            "  Saw       mail from Priya Raman <priya@northwind.example>, Message-ID " + PRIYA_KEY,
            "server ID 01-priya-request.eml", "received Tue Dec 1, 8:02 AM Pacific",
            "no screen stopped it", "0 links, 0 attachments, nothing opened", "[seq 4]",
            "  Asked     the model (stub) to class it -> answered", "[seq 5-6]",
            "  Classed   meeting_request", "[seq 7]",
            "  Decided   book. Policy row policy.book_clear_time: One clear time, parser and model agree, "
            "and it is available: book.", "[seq 8]",
            "  Called    calendar read Wed Dec 2, 11:00 AM Pacific to Wed Dec 2, 11:30 AM Pacific -> free",
            "  Checked   the gate before calendar_create (booking doris-", "passed:",
            "G1 pass", "G11 pass", "G12 pass", "not applicable: G5, G8, G14, G15", "[seq 13]",
            "  Called    calendar write, booking doris-", "-> written", "[seq 14-15]",
            "  Verified  read back booking doris-", "-> found, Wed Dec 2, 11:00 AM Pacific to "
            "Wed Dec 2, 11:30 AM Pacific, CONFIRMED, fields match", "[seq 16-17]",
            "  Verified  calendar re-read", "-> 1 event: hers (doris-", "[seq 18-19]",
            "  Checked   the gate before send_reply", "passed:", "[seq 20]",
            "  Called    send T1 to the sender, BCC the owner", "-> handed over", "[seq 21-22]",
            "  Verified  looked in Sent for T1", "-> found", "[seq 23-24]",
            "  Called    file it under doris-book -> ok", "[seq 25-26]",
            "  Closed    book, T1 sent. Why (policy.book_clear_time): One clear time, parser and model "
            "agree, and it is available: book.", "[seq 27]"])
        self.assertNotIn("The run\n", out)

    def test_the_calendar_timeout_and_its_retry_are_both_shown(self):
        text = block(self.out, MARCUS_PICK_KEY)
        in_order(self, text, ["Decided   book. Policy row policy.pick_books: They picked slot 2 of the open offer",
                              "  Called    calendar read", "-> failed: timeout",
                              "  Called    calendar read", "(attempt 2) -> free",
                              "  Closed    book, T1 sent", "A failed call was retried", "Marked recovered"])

    def test_the_collision_shows_her_event_and_the_other_one(self):
        text = block(self.out, "20261201.1030.sam.monday@ridgeway.example")
        in_order(self, text, ["  Verified  calendar re-read", "-> 2 events: hers (doris-", "and 1 other",
                              "  Checked   the gate before calendar_delete", "  Called    calendar delete",
                              "  Verified  read back booking", "-> not found",
                              "  Decided   offer. Policy row policy.collision_reoffer",
                              "  Closed    offer, T4 sent", "Marked recovered"])

    def test_a_hold_shows_the_notice_to_the_owner(self):
        text = block(self.out, "20261201.1310.elena.pricing@harborline.example")
        in_order(self, text, ["  Decided   hold. Policy row hold.money",
                              "  Checked   the gate before send_hold_notice", "passed:",
                              "  Called    send S1 to the owner", "-> handed over",
                              "  Verified  looked in Sent for S1", "-> found",
                              "  Held      hold:money. Why (hold.money): Held: money in the sender's own words."])

    def test_quarantined_and_screened_mail(self):
        text = block(self.out, "20261201.1230.unreadable@mail.invalid")
        in_order(self, text, ["  Saw       an unreadable message", "screen 1 (decode) settled it: quarantine",
                              "  Closed    quarantine. Why (screen.quarantine)"])
        self.assertNotIn("Asked", text)

    def test_the_run_block_has_the_inbox_reads_the_digest_and_the_summary(self):
        run = self.out[self.out.index("\nThe run\n"):]
        in_order(self, run, ["  Started   Tue Dec 1, 8:00 AM Pacific", "[seq 1]",
                             "  Read      the inbox", "every read ok",
                             "  Checked   the gate before send_digest", "passed:",
                             "  Called    send S2 to the owner", "-> handed over",
                             "  Verified  looked in Sent for S2", "-> found",
                             "  Ended     13 handled, 2 held, 0 human approvals"])

    def test_one_message_by_its_server_id(self):
        code, out = replay_cli([self.path, "--message", "01-priya-request.eml"])
        self.assertEqual(code, 0, out)
        self.assertIn("Message-ID " + PRIYA_KEY, out)
        self.assertEqual(out.count("\nMessage "), 1, out)

    def test_a_message_not_in_the_run_is_one_line(self):
        code, out = replay_cli([self.path, "--message", "nobody@nowhere.example"])
        self.assertEqual(code, 2)
        self.assertEqual(len(out.strip().splitlines()), 1, out)
        self.assertIn("No message nobody@nowhere.example in", out)

    def test_replay_writes_nothing(self):
        before = left_behind.tree(self.tmp)
        with open(self.path, "rb") as fh:
            data = fh.read()
        replay_cli([self.path])
        self.assertEqual(left_behind.tree(self.tmp), before)
        with open(self.path, "rb") as fh:
            self.assertEqual(fh.read(), data)


class BrokenChain(unittest.TestCase):
    """verify's check first: a changed, cut or torn file is refused in one line, nothing stepped through."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="doris-replay-")
        _root, cfg = run_day(cls.tmp, ("01-priya-request.eml", "02-marcus-request.eml"))
        cls.path = loop.run(cfg).receipts_path
        with open(cls.path, "rb") as fh:
            cls.data = fh.read()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, True)

    def copy(self, data):
        path = os.path.join(self.tmp, "copy-%s.jsonl" % self.id().rsplit(".", 1)[-1])
        with open(path, "wb") as fh:
            fh.write(data)
        self.addCleanup(os.remove, path)
        return path

    def refused(self, data, seq):
        code, out = replay_cli([self.copy(data)])
        self.assertEqual(code, 1, out)
        self.assertEqual(len(out.strip().splitlines()), 1, out)
        self.assertTrue(out.startswith("Refused: "), out)
        self.assertIn("broken at seq %d" % seq, out)
        self.assertNotIn("Saw", out)
        return out

    def test_a_changed_byte(self):
        self.assertIn(b'"from_name":"Priya Raman"', self.data)
        self.refused(self.data.replace(b'"from_name":"Priya Raman"', b'"from_name":"Priya Ramen"', 1), 4)

    def test_a_deleted_line(self):
        lines = self.data.split(b"\n")
        self.refused(b"\n".join(lines[:9] + lines[10:]), 10)

    def test_lines_cut_from_the_end(self):
        lines = self.data.split(b"\n")
        out = self.refused(b"\n".join(lines[:-3]) + b"\n", len(lines) - 2)
        self.assertIn("no closing line", out)

    def test_a_torn_tail(self):
        self.refused(self.data + b'{"seq": 99, "kind": "res', self.data.count(b"\n") + 1)

    def test_a_missing_file_is_refused_too(self):
        code, out = replay_cli([os.path.join(self.tmp, "nope", "receipts.jsonl")])
        self.assertEqual(code, 2, out)
        self.assertEqual(len(out.strip().splitlines()), 1, out)


class TrapPackRun1(unittest.TestCase):
    """The committed trace of Trap Pack run 1 (model called live): one receipt file per item."""

    def test_the_committed_trace_steps_through_end_to_end(self):
        folder = os.path.join(paths.TRACES, RUN1)
        code, out = replay_cli([folder])
        self.assertEqual(code, 0, out[-2000:])
        items = sorted(n for n in os.listdir(folder) if os.path.isfile(os.path.join(folder, n, "receipts.jsonl")))
        self.assertEqual(len(items), 20)
        for item in items:
            self.assertIn("\nItem %s: " % item, out)
        self.assertEqual(out.count("\nMessage 1 of 1: "), 20)
        self.assertIn("model called live (claude-sonnet-5-5)", out)
        self.assertEqual(out.count("  Ended     "), 20)
        self.assertEqual(out.count("  Held      ") + out.count("  Closed    "), 20)

    def test_the_run_id_finds_it_under_traces(self):
        self.assertEqual(replay_cli([RUN1]), replay_cli([os.path.join(paths.TRACES, RUN1)]))

    def test_one_tampered_item_refuses_the_whole_trace(self):
        tmp = tempfile.mkdtemp(prefix="doris-replay-")
        self.addCleanup(shutil.rmtree, tmp, True)
        folder = os.path.join(tmp, RUN1)
        shutil.copytree(os.path.join(paths.TRACES, RUN1), folder)
        path = os.path.join(folder, "13-supplier-listing", "receipts.jsonl")
        with open(path, "rb") as fh:
            data = fh.read()
        lines = data.split(b"\n")
        lines[5] = lines[5].replace(b'"ok":true', b'"ok":false', 1)
        with open(path, "wb") as fh:
            fh.write(b"\n".join(lines))
        code, out = replay_cli([folder])
        self.assertEqual(code, 1, out)
        self.assertEqual(len(out.strip().splitlines()), 1, out)
        self.assertIn("13-supplier-listing", out)
        self.assertIn("broken at seq 6", out)


class SensitiveMail(unittest.TestCase):
    def test_it_shows_as_skipped_by_design_and_nothing_of_it_is_read(self):
        tmp = tempfile.mkdtemp(prefix="doris-replay-")
        self.addCleanup(shutil.rmtree, tmp, True)
        root, cfg = run_day(tmp, ("01-priya-request.eml",))
        add(root, "01b-code.eml", SENSITIVE, {"forwarded": "yes"})
        s = loop.run(cfg)
        code, out = replay_cli([s.receipts_path])
        self.assertEqual(code, 0, out)
        text = block(out, " Sensitive mail")
        in_order(self, text, ["Sensitive mail from alerts@bankish.example", "skipped by design",
                              "filed unread, no model call, no reply", "(check: subject-phrase)",
                              "  Called    file it under doris-skip -> ok",
                              "  Closed    skip. Why (screen.sensitive_skip)"])
        self.assertNotIn("Asked", text)
        self.assertNotIn("verification", out)
        self.assertNotIn("482913", out)


class HaltedRun(unittest.TestCase):
    def test_a_stop_mid_send_shows_the_halt_the_call_in_flight_and_what_was_dropped(self):
        tmp = tempfile.mkdtemp(prefix="doris-replay-")
        self.addCleanup(shutil.rmtree, tmp, True)
        _root, cfg = run_day(tmp, ("01-priya-request.eml", "02-marcus-request.eml"))
        real = sandbox_mod.SandboxMail.send

        def stop_at_the_offer(self_, raw, envelope_to, message_id, template=None):
            if template == "T2":
                raise loop.Halted("cli")
            return real(self_, raw, envelope_to, message_id, template=template)

        with mock.patch.object(sandbox_mod.SandboxMail, "send", stop_at_the_offer):
            s = loop.run(cfg)
        self.assertEqual(s.halted, "cli")
        code, out = replay_cli([s.receipts_path])
        self.assertEqual(code, 0, out)
        sends = [l for l in receipts.read_lines(s.receipts_path)
                 if l["kind"] == "called" and l["payload"]["call"] == "send" and l["payload"]["target"] == "T2"]
        seq = sends[0]["seq"]
        in_order(self, out, ["  Called    send T2 to the sender", "-> no answer recorded: in flight when the run "
                             "stopped [seq %d]" % seq,
                             "\nThe run\n",
                             "  Halted    Halted: stop on the command line.",
                             "In flight: send T2 [seq %d], for Message-ID 20261201.0810.intro.marcus@brightlane.example"
                             % seq, "Dropped: ",
                             "  Ended     "])

    def test_a_halt_the_kill_switch_wrote_with_nothing_queued(self):
        tmp = tempfile.mkdtemp(prefix="doris-replay-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "receipts.jsonl")
        w = receipts.ReceiptWriter(path, "model-stub-sandbox-halt", "sandbox")
        w.write("observed", {"step": "start", "mode": "sandbox", "model_source": "stub", "model": "stub",
                             "simulated": True, "clock": "2026-12-01T16:00:00Z"})
        w.write("called", {"call": "mail_fetch", "target": "inbox"})
        supervisor.record_halt("sandbox", "model-stub-sandbox-halt", "stop_file", path,
                               os.path.join(tmp, "no-ledger.sqlite"), stop_to_dead_ms=120)
        code, out = replay_cli([path])
        self.assertEqual(code, 0, out)
        in_order(self, out, ["  Read      the inbox", "no answer recorded",
                             "  Halted    Halted: a STOP file was found.",
                             "In flight: mail_fetch inbox [seq 2]",
                             "Dropped: nothing (no ledger: nothing was ever queued)",
                             "Stopped 120 ms after the stop was asked for"])


class HostileText(unittest.TestCase):
    """Whatever a sender put in a field is shown as text: no terminal control, no fake lines."""

    def test_control_and_direction_characters_are_shown_escaped(self):
        tmp = tempfile.mkdtemp(prefix="doris-replay-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "receipts.jsonl")
        w = receipts.ReceiptWriter(path, "model-stub-sandbox-hostile", "sandbox")
        w.write("observed", {"step": "start", "mode": "sandbox", "model_source": "stub", "model": "stub"})
        key = "evil\r\n  Closed    book@x.example"
        w.write("observed", {"server_id": "a\x1b[2Jb", "from_name": "Eve‮evil\x07", "from_addr": "eve@x.example",
                             "received": "2026-12-01T16:00:00Z", "screens": [], "links_seen": 0, "opened": 0,
                             "attachments": [{"name": "x\x00.pdf"}]}, subject=key)
        w.write("decided", {"outcome": "file", "template": "-"}, subject=key,
                why={"code": "policy.file_fyi", "text": "Newsletter, notification or FYI: file, no reply."})
        w.write("summary", {"handled": 1, "held": 0, "human_approvals": 0})
        code, out = replay_cli([path])
        self.assertEqual(code, 0, out)
        for raw in ("\x1b", "‮", "\x07", "\x00", "\r"):
            self.assertNotIn(raw, out)
        self.assertIn("\\x1b[2J", out)
        self.assertIn("\\u202e", out)
        self.assertNotIn("\n  Closed    book@x.example", out)
        self.assertEqual(len([l for l in out.splitlines() if l.startswith("  Closed")]), 1, out)

    def test_an_odd_payload_never_crashes_the_step_through(self):
        # A file re-chained by hand (the hashes recomputed, as anyone can) passes the chain check
        # whatever its payloads hold: the step-through shows them as recorded and never crashes.
        tmp = tempfile.mkdtemp(prefix="doris-replay-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "receipts.jsonl")
        why = {"code": "halt.cli", "text": "Halted: stop on the command line."}
        forge(path, [
            ("observed", "", {"step": "start", "clock": 7, "model_source": ["x"]}, None),
            ("observed", "m@x.example", {"screens": "none", "received": "yesterday", "attachments": 3}, None),
            ("called", "m@x.example", {"call": "calendar_query", "target": "span", "start": None}, None),
            ("result", "m@x.example", {"call_seq": 3, "events": "many", "ok": "yes"}, None),
            ("called", "m@x.example", {"call": "teleport", "target": None}, None),
            ("result", "m@x.example", {"call_seq": "5"}, None),
            ("checked", "m@x.example", {"checks": ["G1"], "passed": None}, why),
            ("decided", "m@x.example", {"outcome": ["x"], "slot": "noon"}, "just because"),
            ("held", "m@x.example", {}, why),
            ("error", "torn-tail", {"torn_bytes": "x"}, None),
            ("halted", "", {"cause": 5, "in_flight": "elsewhere", "dropped": "lots", "stop_to_dead_ms": True}, why)])
        self.assertTrue(receipts.verify(path, finished=True).ok)
        code, out = replay_cli([path])
        self.assertEqual(code, 0, out)
        self.assertIn("teleport", out)
        self.assertIn("Halted: stop on the command line.", out)


class FindingTheRun(unittest.TestCase):
    """A receipts file, a run folder, a trace folder, a run ID, or (none given) the latest sandbox run."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="doris-replay-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        state, traces = os.path.join(self.tmp, "state"), os.path.join(self.tmp, "traces")
        self.patches = [mock.patch.object(paths, "STATE", state), mock.patch.object(paths, "TRACES", traces)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def make(self, folder, run_id, mtime=None):
        os.makedirs(folder)
        path = os.path.join(folder, "receipts.jsonl")
        w = receipts.ReceiptWriter(path, run_id, "sandbox")
        w.write("observed", {"step": "start", "mode": "sandbox", "model_source": "stub", "model": "stub"})
        w.write("summary", {"handled": 0, "held": 0, "human_approvals": 0})
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def test_a_run_id_under_the_sandbox_state(self):
        self.make(os.path.join(paths.runs_dir("sandbox"), "model-stub-sandbox-1"), "model-stub-sandbox-1")
        code, out = replay_cli(["model-stub-sandbox-1"])
        self.assertEqual(code, 0, out)
        self.assertIn("Replay of run model-stub-sandbox-1", out)

    def test_a_run_id_under_the_live_state(self):
        self.make(os.path.join(paths.runs_dir("live"), "model-live-live-1"), "model-live-live-1")
        self.assertEqual(replay_cli(["model-live-live-1"])[0], 0)

    def test_a_run_folder(self):
        folder = os.path.join(self.tmp, "somewhere")
        self.make(folder, "model-stub-sandbox-2")
        self.assertEqual(replay_cli([folder])[0], 0)

    def test_an_unknown_run_id_is_one_line(self):
        code, out = replay_cli(["model-stub-sandbox-nope"])
        self.assertEqual(code, 2)
        self.assertEqual(len(out.strip().splitlines()), 1, out)
        self.assertIn("No run model-stub-sandbox-nope", out)

    def test_a_run_id_is_a_name_never_a_path(self):
        code, out = replay_cli([os.path.join("..", "no-such-folder", "model-stub-sandbox-1")])
        self.assertEqual(code, 2)
        self.assertEqual(len(out.strip().splitlines()), 1, out)

    def test_a_run_id_in_two_places_is_refused(self):
        self.make(os.path.join(paths.runs_dir("sandbox"), "model-stub-sandbox-3"), "model-stub-sandbox-3")
        self.make(os.path.join(paths.TRACES, "model-stub-sandbox-3"), "model-stub-sandbox-3")
        code, out = replay_cli(["model-stub-sandbox-3"])
        self.assertEqual(code, 2)
        self.assertIn("is in 2 places", out)

    def test_no_argument_means_the_latest_sandbox_run(self):
        runs = paths.runs_dir("sandbox")
        self.make(os.path.join(runs, "model-stub-sandbox-old"), "model-stub-sandbox-old", mtime=1000000000)
        self.make(os.path.join(runs, "model-stub-sandbox-new"), "model-stub-sandbox-new", mtime=2000000000)
        code, out = replay_cli([])
        self.assertEqual(code, 0, out)
        self.assertIn("Replay of run model-stub-sandbox-new", out)

    def test_no_argument_and_no_run(self):
        code, out = replay_cli([])
        self.assertEqual(code, 2)
        self.assertIn("python -m doris demo", out)
        self.assertFalse(os.path.exists(paths.STATE))


class ReadsOnlyTheReceipts(unittest.TestCase):
    def test_it_loads_no_model_mail_calendar_or_loop_code(self):
        code = ("import sys; import doris.replay; "
                "print(' '.join(sorted(m for m in sys.modules if m.startswith('doris'))))")
        env = dict(os.environ, PYTHONPATH=paths.SRC)
        out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True).stdout
        loaded = set(out.split())
        self.assertIn("doris.replay", loaded)
        for name in ("doris.loop", "doris.model", "doris.mail", "doris.adapters", "doris.adapters.live",
                     "doris.adapters.sandbox", "doris.gate", "doris.supervisor", "doris.ledger"):
            self.assertNotIn(name, loaded)

    def test_no_host_is_allowed_while_it_runs(self):
        tmp = tempfile.mkdtemp(prefix="doris-replay-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "receipts.jsonl")
        w = receipts.ReceiptWriter(path, "model-stub-sandbox-net", "sandbox")
        w.write("observed", {"step": "start", "mode": "sandbox", "model_source": "stub", "model": "stub"})
        w.write("summary", {"handled": 0, "held": 0, "human_approvals": 0})
        tried = []
        real = receipts.verify

        def verify_and_dial(*args, **kwargs):
            import socket
            try:
                socket.create_connection(("api.anthropic.com", 443), timeout=1)
            except netguard.NetworkRefused:
                tried.append("refused")
            return real(*args, **kwargs)

        with mock.patch.object(receipts, "verify", verify_and_dial):
            code, _out = replay_cli([path])
        self.assertEqual((code, tried), (0, ["refused"]))


if __name__ == "__main__":
    unittest.main()
