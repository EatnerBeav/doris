"""The loop's own pure pieces (the sandbox cycle grid, booking IDs, the run summary counts), and
what the loop hands the gate, checked on a run over a fresh copy of the sandbox."""

import datetime as dt
import email
import email.policy
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from doris import caps, loop, paths, receipts, templates
from doris.shapes import Slot
from tests.unit import demo_sandbox

UTC = dt.timezone.utc


def t(h, m, day=1):
    return dt.datetime(2026, 12, day, h, m, tzinfo=UTC)


class CycleGrid(unittest.TestCase):
    """The sandbox polls on a ten-minute grid, like "her 3:10 PM cycle" in the demo."""

    def test_an_arrival_between_grid_points_waits_for_the_next_cycle(self):
        self.assertEqual(loop.next_cycle(t(16, 2)), t(16, 10))
        self.assertEqual(loop.next_cycle(t(23, 5)), t(23, 10))

    def test_an_arrival_on_a_grid_point_runs_in_that_cycle(self):
        self.assertEqual(loop.next_cycle(t(16, 10)), t(16, 10))

    def test_seconds_past_a_grid_point_wait_for_the_next(self):
        self.assertEqual(loop.next_cycle(t(16, 10) + dt.timedelta(seconds=1)), t(16, 20))

    def test_the_grid_rolls_over_the_hour_and_the_day(self):
        self.assertEqual(loop.next_cycle(t(23, 55)), t(0, 0, day=2))


class BookingId(unittest.TestCase):
    """Ruling 8: built from the inbound Message-ID and the slot, so the same message and slot give the same ID."""

    def setUp(self):
        self.slot = Slot(t(19, 0, day=2), t(19, 30, day=2))

    def test_prefixed_and_stable(self):
        a = loop.booking_id("priya.001@northwind.example", self.slot)
        self.assertTrue(a.startswith("doris-"))
        self.assertEqual(a, loop.booking_id("priya.001@northwind.example", self.slot))

    def test_another_slot_or_message_gives_another_id(self):
        a = loop.booking_id("priya.001@northwind.example", self.slot)
        other = Slot(t(19, 30, day=2), t(20, 0, day=2))
        self.assertNotEqual(a, loop.booking_id("priya.001@northwind.example", other))
        self.assertNotEqual(a, loop.booking_id("priya.002@northwind.example", self.slot))

    def test_safe_as_a_file_name_on_every_system(self):
        a = loop.booking_id("<odd/id:with*chars>@x.example", self.slot)
        self.assertRegex(a, r"^doris-[0-9a-f]{24}$")


class Counts(unittest.TestCase):
    """handled + held = every message that reached a closing line; approvals are counted, never assumed."""

    def lines(self, outcomes):
        out = [{"kind": "observed", "subject": "startup", "payload": {}}]
        for i, o in enumerate(outcomes):
            out.append({"kind": "held" if o.startswith("hold:") else "decided", "subject": "m%d" % i,
                        "payload": {"outcome": o}})
        return out

    def test_holds_are_held_and_everything_else_is_handled(self):
        c = loop.count_outcomes(self.lines(["book", "offer", "hold:money", "file", "quarantine", "hold:add_person"]))
        self.assertEqual((c["handled"], c["held"], c["human_approvals"]), (4, 2, 0))

    def test_plan_lines_are_not_closing_lines(self):
        lines = self.lines(["book"])
        lines.insert(1, {"kind": "decided", "subject": "m0", "payload": {"step": "plan"}})
        self.assertEqual(loop.count_outcomes(lines)["handled"], 1)

    def test_an_approval_line_would_be_counted(self):
        lines = self.lines(["book"]) + [{"kind": "approved", "subject": "m0", "payload": {}}]
        self.assertEqual(loop.count_outcomes(lines)["human_approvals"], 1)


def sandbox_run(tmp, say=None, clock_mode="advance"):
    """The demo day on a fresh copy of sandbox/ as committed, stub model, advancing clock; returns its receipt lines."""
    sandbox = demo_sandbox.copy_committed(os.path.join(tmp, "sandbox"))
    cfg = loop.RunConfig(mode="throwaway", run_name="model-stub-sandbox-test", state_dir=os.path.join(tmp, "state"),
                         sandbox_root=sandbox, owner_addr="whizz@fastmail.com", doris_addr="doris.htm@fastmail.com",
                         model_source="stub", clock_mode=clock_mode, say=say)
    return receipts.read_lines(loop.run(cfg).receipts_path)


class WhatTheGateIsHanded(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="doris-loop-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_the_booking_being_checked_is_not_counted_against_the_write_cap(self):
        # G7 counts what is already done: with a cap of one, the run's first write fits, the second doesn't
        with mock.patch.object(caps, "CALENDAR_WRITES_PER_RUN", 1):
            lines = sandbox_run(self.tmp)
        creates = [l["payload"] for l in lines if l["kind"] == "checked" and l["payload"]["action"] == "calendar_create"]
        self.assertTrue(creates[0]["passed"], creates[0]["checks"]["G7"])
        self.assertEqual(creates[1]["first_fail"], "G7")



class TheRunEnds(unittest.TestCase):
    def test_a_fixed_clock_run_ends_once_nothing_new_is_fetched(self):
        # the sandbox hands over the same mail on every fetch; the ledger is what makes it old
        tmp = tempfile.mkdtemp(prefix="doris-loop-")
        self.addCleanup(shutil.rmtree, tmp, True)
        done = []
        t = threading.Thread(target=lambda: done.append(sandbox_run(tmp, clock_mode="fixed")), daemon=True)
        t.start()
        t.join(60)
        self.assertTrue(done, "the run never ended")


def sent_and_booked_text(sandbox):
    """Every text part she sent (decoded) and every calendar file of hers, as one string."""
    texts = []
    outbox = os.path.join(sandbox, "outbox")
    for name in sorted(os.listdir(outbox)):
        if name.endswith(".eml"):
            with open(os.path.join(outbox, name), "rb") as fh:
                msg = email.message_from_bytes(fh.read(), policy=email.policy.default)
            texts += [p.get_content() for p in msg.walk() if p.get_content_maintype() == "text"]
    calendar = os.path.join(sandbox, "calendar")
    for name in sorted(os.listdir(calendar)):
        if name.startswith("doris-"):
            with open(os.path.join(calendar, name), encoding="utf-8") as fh:
                texts.append(fh.read())
    return "\n".join(texts)


class TheMeetingLink(unittest.TestCase):
    def test_a_sandbox_run_keeps_its_own_fixed_link_whatever_the_environment_says(self):
        tmp = tempfile.mkdtemp(prefix="doris-loop-")
        self.addCleanup(shutil.rmtree, tmp, True)
        elsewhere = "https://meet.example/" + "not-for-the-sandbox"
        with mock.patch.dict(os.environ, {"DORIS_MEETING_LINK": elsewhere}):
            lines = sandbox_run(tmp)
            self.assertEqual(templates.meeting_link(), elsewhere)      # the run let go of its link
        text = sent_and_booked_text(os.path.join(tmp, "sandbox"))
        self.assertIn("Join: " + templates.PLACEHOLDER_MEETING_LINK, text)
        self.assertIn("LOCATION:" + templates.PLACEHOLDER_MEETING_LINK, text)
        self.assertNotIn(elsewhere, text)
        (summary,) = [l["payload"] for l in lines if l["kind"] == "summary"]
        self.assertEqual((summary["handled"], summary["held"]), (13, 2))   # G13 passed every booking


class TheSentLooks(unittest.TestCase):
    def test_the_sandbox_looks_twice_with_no_wait_between(self):
        from tests.unit.test_restart import DORIS, OWNER, mini_sandbox
        tmp = tempfile.mkdtemp(prefix="doris-loop-")
        self.addCleanup(shutil.rmtree, tmp, True)
        root = mini_sandbox(tmp, ("01-priya-request.eml",))
        with open(os.path.join(root, "inbox", "facts.json"), "w", encoding="utf-8") as fh:
            fh.write('{"01-priya-request.eml": {"fault": "sent-unconfirmed"}}')
        waits = []
        cfg = loop.RunConfig(mode="throwaway", run_name="model-stub-sandbox-test", state_dir=os.path.join(tmp, "state"),
                             sandbox_root=root, owner_addr=OWNER, doris_addr=DORIS, clock_mode="advance")
        with mock.patch.object(loop, "_sleep", waits.append):
            s = loop.run(cfg)
        self.assertEqual(waits, [])
        lines = receipts.read_lines(s.receipts_path)
        looks = [l["payload"] for l in lines if l["kind"] == "called" and l["payload"]["call"] == "sent_lookup"]
        self.assertEqual([l["target"] for l in looks], ["T1", "T1", "S1"])   # the hidden T1 twice; its notice once
        self.assertFalse([l for l in looks if "after_wait_s" in l])


class ALongSandboxRun(unittest.TestCase):
    def test_a_later_pass_steps_her_clock_one_cycle_and_reads_the_new_mail(self):
        from tests.unit.test_restart import DORIS, OWNER, mini_sandbox
        tmp = tempfile.mkdtemp(prefix="doris-loop-")
        self.addCleanup(shutil.rmtree, tmp, True)
        root = mini_sandbox(tmp, ("01-priya-request.eml",))
        with open(os.path.join(paths.SANDBOX, "inbox", "02-marcus-request.eml"), "rb") as fh:
            marcus = fh.read()
        marcus = b"".join(l for l in marcus.splitlines(True) if not l.lower().startswith(b"date:"))
        dropped = []

        def drop_in(seconds):           # a judge drops an email (no Date) in while she waits
            if not dropped:
                dropped.append(seconds)
                with open(os.path.join(root, "inbox", "14-judge.eml"), "wb") as fh:
                    fh.write(marcus)

        cfg = loop.RunConfig(mode="throwaway", run_name="model-stub-sandbox-test", state_dir=os.path.join(tmp, "state"),
                             sandbox_root=root, owner_addr=OWNER, doris_addr=DORIS, clock_mode="advance",
                             cycles=2, cycle_s=0.01)
        with mock.patch.object(loop, "_sleep", drop_in):
            s = loop.run(cfg)
        self.assertEqual(s.handled, 2)
        observed = [l for l in receipts.read_lines(s.receipts_path) if l["kind"] == "observed" and "from_addr" in l["payload"]]
        self.assertEqual([l["payload"]["clock"] for l in observed], ["2026-12-01T16:10:00Z", "2026-12-01T16:20:00Z"])


class TheTerminalLines(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="doris-loop-")
        self.addCleanup(shutil.rmtree, tmp, True)
        self.said = []
        sandbox_run(tmp, say=self.said.append)

    def test_a_staged_fault_is_labelled_on_its_line(self):
        (pick,) = [l for l in self.said if "Marcus Lee: book" in l]
        self.assertIn("Staged fault timeout-once: retried once.", pick)
        (sam,) = [l for l in self.said if "Sam Rivera" in l]
        self.assertIn("Staged fault lands-between: recovered.", sam)

    def test_the_stub_shows_no_model_cost(self):
        self.assertFalse([l for l in self.said if "Model cost" in l])


if __name__ == "__main__":
    unittest.main()
