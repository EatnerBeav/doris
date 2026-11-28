"""tests/tools/overnight_drip.py and tests/tools/keep_overnight.py: the overnight run's test sender
and the morning step that keeps its traces. Nothing here contacts anything: the drip plays its
whole night against a throwaway sandbox (her real loop, the stand-in model, a crash at the planned
restart), and against a fake JMAP server for wolf's own mailbox.
"""

import datetime as dt
import email
import email.policy
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

from doris import screens, templates
from doris.adapters import jmap
from doris.shapes import MailError, SendUnknown
from tests.unit import fake_jmap

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOLS = os.path.join(REPO, "tests", "tools")
WOLF = "wolf.whizz@fastmail.com"
OWNER = "whizz@fastmail.com"
UTC = dt.timezone.utc


def load(name):
    if TOOLS not in sys.path:
        sys.path.insert(0, TOOLS)
    spec = importlib.util.spec_from_file_location(name, os.path.join(TOOLS, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


drip = load("overnight_drip")
keep = load("keep_overnight")


def utc(*args):
    return dt.datetime(*args, tzinfo=UTC)


class Plan(unittest.TestCase):

    def test_the_night_stays_under_60_emails(self):
        self.assertLess(drip.planned_emails(), 60)
        self.assertLess(drip.MAX_EMAILS, 60)
        self.assertGreaterEqual(drip.MAX_EMAILS, drip.planned_emails())

    def test_no_email_in_the_plan_is_stop_mail(self):
        # one stop would silence wolf on the live ledger for good
        for th in drip.PLAN:
            for step in th.steps:
                self.assertIsNone(screens.stop_match(step.body.replace("{nth}", "first")), th.key)

    def test_every_email_is_one_plain_part_her_sender_check_takes(self):
        now = utc(2026, 11, 29, 4, 0)
        for th in drip.PLAN:
            for i, step in enumerate(th.steps):
                mid = "<%032x@fastmail.com>" % (i + 1)
                raw = drip.build(WOLF, OWNER, step.subject or th.subject, step.body.replace("{nth}", "first"), mid,
                                 now, None if i == 0 else "<her@fastmail.com>", (), step.headers)
                jmap._checked(raw, [OWNER], mid, WOLF, "test")      # raises when it would refuse

    def test_the_restart_threads_straddle_it(self):
        t18 = next(t for t in drip.PLAN if t.key == "t18")
        self.assertLess(t18.at_min, drip.RESTART_AT_MIN)
        self.assertGreater(t18.steps[1].not_before_min, drip.RESTART_AT_MIN)
        t19 = next(t for t in drip.PLAN if t.key == "t19")
        self.assertLess(abs(t19.at_min - drip.RESTART_AT_MIN), 5)


class Answers(unittest.TestCase):
    """kind_of reads every approved template a prospect can get, from the templates themselves."""

    def render(self, tid, **slots):
        return templates.render(tid, slots)

    def test_each_template(self):
        a, b, c = utc(2026, 12, 8, 18, 0), utc(2026, 12, 9, 18, 0), utc(2026, 12, 10, 18, 0)
        cases = [
            ("T1", dict(start=a, duration=30, zone=None, meeting_link=templates.MEETING_LINK), drip.BOOKED, 0),
            ("T1", dict(start=a, duration=30, zone="Eastern", meeting_link=templates.MEETING_LINK), drip.BOOKED, 0),
            ("T2", dict(slots=[a, b, c], duration=30, zone=None), drip.OFFER, 3),
            ("T3", dict(slots=[a, b, c], duration=30, zone=None), drip.OFFER, 3),
            ("T4", dict(asked=a, slots=[b, c], duration=30, zone="Pacific"), drip.OFFER, 2),
            ("T4", dict(asked=a, slots=[b], duration=30, zone="Pacific"), drip.OFFER, 1),
            ("T9", dict(slots=[a, b], duration=30, zone=None), drip.OFFER, 2),
            ("T9", dict(slots=[a], duration=30, zone=None), drip.OFFER, 1),
            ("T10", dict(start=a, zone=None), drip.CANCELLED, 0),
            ("T12", dict(slots=[a, b], zone=None), drip.OFFER, 2),
            ("T13", dict(slot=a, duration=30, zone=None), drip.OFFER, 1),
            ("T18", dict(start=a, slots=[b, c], duration=30, zone=None), drip.OFFER, 2),
            ("T18", dict(start=a, slots=[b], duration=30, zone=None), drip.OFFER, 1),
        ]
        for tid, slots, kind, n in cases:
            text = "Hi Wolf,\n\n" + self.render(tid, **slots) + "\n\nDoris"
            self.assertEqual(drip.kind_of(text), (kind, tid), tid)
            if kind == drip.OFFER:
                self.assertEqual(drip.slots_in(text), n, tid)

    def test_anything_else_is_other(self):
        self.assertEqual(drip.kind_of("Thanks for your note."), (drip.OTHER, "?"))


class Rehearsal(unittest.TestCase):
    """The whole plan on a throwaway sandbox: her loop, the stand-in model, the forced restart."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="doris-drip-test-")
        cls.log = []
        cls.tally, cls.restart = drip.rehearse(cls.log.append, workdir=cls.tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, True)

    def test_every_step_got_the_answer_it_expects(self):
        missed = [r for r in self.tally["rows"] if not r["ok"]]
        self.assertEqual(missed, [])
        self.assertEqual(self.tally["emails"], drip.planned_emails())
        self.assertIsNone(self.tally["stopped"])

    def test_nothing_was_answered_twice(self):
        self.assertEqual(self.tally["answered_twice"], 0)
        answers = [r["message_id"] for r in self.log if r["event"] == "answer"]
        self.assertEqual(len(answers), len(set(answers)))

    def test_her_run_crashed_at_the_restart_and_the_next_run_named_it(self):
        self.assertGreaterEqual(self.restart["crashed_at_min"], drip.RESTART_AT_MIN)
        self.assertTrue(self.restart["interrupted_named"])

    def test_one_booking_per_booked_thread_and_cancels_removed(self):
        booked_threads = {r["thread"] for r in self.tally["rows"] if r["expect"] == drip.BOOKED}
        cancelled = {"t01", "t22"}                         # the move (t15) leaves its new booking only
        self.assertEqual(len(self.restart["calendar"]), len(booked_threads - cancelled))

    def test_offers_never_overlap_between_threads(self):
        """Between an email that asks for an offer and her answer or the thread's next email, no
        other thread asks for one: a newer offer would close the older one."""
        asks = {(t.key, i) for t in drip.PLAN for i, s in enumerate(t.steps) if s.asks_offer}
        open_by = None
        for r in self.log:
            if r["event"] == "sent":
                if open_by is not None and r["thread"] != open_by and (r["thread"], r["step"]) in asks:
                    self.fail("%s asked for an offer while %s held one" % (r["thread"], open_by))
                if (r["thread"], r["step"]) in asks:
                    open_by = r["thread"]
                elif r["thread"] == open_by:
                    open_by = None if r["expect"] != drip.OFFER else open_by
            elif r["event"] == "answer" and r["thread"] == open_by and r["kind"] != drip.OFFER:
                open_by = None
            elif r["event"] == "thread_ended" and r["thread"] == open_by:
                open_by = None

    def test_the_log_holds_no_address_subject_or_body(self):
        text = json.dumps(self.log)
        for value in (WOLF, OWNER, drip.SB_DORIS, "Hi Shane", "Intro call"):
            self.assertNotIn(value, text)

    def test_keep_overnight_keeps_both_runs_and_judges_the_night(self):
        night = os.path.join(self.tmp, "night")
        os.makedirs(night)
        with open(os.path.join(night, "drip-log.jsonl"), "w", encoding="utf-8") as fh:
            for r in self.log + [dict({k: v for k, v in self.tally.items()}, event="tally")]:
                fh.write(json.dumps(r) + "\n")
        runs = os.path.join(self.tmp, "state", "runs")
        traces, status = os.path.join(self.tmp, "traces"), os.path.join(self.tmp, "status")
        argv = ["--night", night, "--started", "2026-11-29T04:00:00Z", "--killed-at", "2026-11-29T11:00:00Z",
                "--run1", os.path.join(runs, self.restart["crashed_run"]),
                "--run2", os.path.join(runs, self.restart["next_run"]), "--run2-started", "2026-11-29T11:00:30Z",
                "--run2-exit", "0", "--traces-root", traces, "--status-dir", status, "--rehearsal"]
        out = io.StringIO()
        with redirect_stdout(out):
            code = keep.main(argv)
        printed = out.getvalue()
        self.assertEqual(code, 0, printed)
        kept = [l[5:] for l in printed.splitlines() if l.startswith("KEEP ")]
        self.assertEqual(len(kept), 4, printed)
        (night_dir,) = [k for k in kept if "live-overnight-" in k]
        with open(os.path.join(night_dir, "overnight.json"), encoding="utf-8") as fh:
            o = json.load(fh)
        self.assertEqual(o["outcome"], "pass", o["checks"])
        self.assertEqual([r["outcome"] for r in o["runs"]], ["pass", "pass"])
        self.assertEqual(o["drip"]["emails"], drip.planned_emails())
        with open(os.path.join(kept[0], "manifest.json"), encoding="utf-8") as fh:
            killed = json.load(fh)
        self.assertEqual(killed["ended_by"], keep.ENDED_BY)
        self.assertFalse(killed["receipt_chain"]["ok"])            # no closing line: the kill came first
        self.assertTrue(killed["receipt_chain_to_the_kill"]["ok"])
        self.assertEqual(killed["started_utc"], "2026-11-29T04:00:00Z")
        self.assertTrue(os.path.isfile(os.path.join(status, "2026-11-28.md")))


class Drip(unittest.TestCase):

    class Transport(object):
        def __init__(self, fail=None):
            self.sent, self.fail = [], fail

        def send(self, raw, to_owner, message_id):
            if self.fail is not None:
                raise self.fail
            self.sent.append((to_owner, message_id))

        def answers(self):
            return []

    def make(self, transport, halted=None):
        log = []
        d = drip.Drip(transport, utc(2026, 11, 29, 4, 0), log.append, WOLF, OWNER, "doris.htm@fastmail.com",
                      halted=halted)
        return d, log

    def test_a_halt_ends_it_at_once_and_nothing_is_sent(self):
        t = self.Transport()
        d, log = self.make(t, halted=lambda: "cli")
        self.assertTrue(d.step(utc(2026, 11, 29, 4, 10)))
        self.assertEqual(t.sent, [])
        self.assertEqual(log[-1]["event"], "halted")

    def test_a_send_that_may_have_gone_is_never_sent_again(self):
        t = self.Transport(fail=SendUnknown("cut off"))
        d, log = self.make(t)
        d.step(utc(2026, 11, 29, 4, 10))
        self.assertEqual(d.stopped, "send unknown")
        self.assertTrue(d.step(utc(2026, 11, 29, 4, 30)))
        self.assertEqual([r["event"] for r in log].count("send_failed"), 1)

    def test_a_send_surely_not_sent_is_tried_again_then_the_thread_ends(self):
        t = self.Transport(fail=MailError("refused"))
        d, log = self.make(t)
        for minute in (10, 16, 22, 28):
            d.step(utc(2026, 11, 29, 4, minute))
        self.assertEqual(sum(1 for r in log if r["event"] == "send_failed" and r["thread"] == "t01"), drip.SEND_TRIES)
        self.assertEqual(d.threads[0].ended, "not sent after %d tries" % drip.SEND_TRIES)

    def test_one_email_every_five_minutes_at_most(self):
        t = self.Transport()
        d, _ = self.make(t)
        start = utc(2026, 11, 29, 4, 0)
        for minute in range(0, 120, 2):
            d.step(start + dt.timedelta(minutes=minute))
        self.assertLessEqual(len(t.sent), 120 // 5)
        self.assertTrue(all(to_owner for to_owner, _ in t.sent))     # no answers: only new threads went


class WolfMailbox(fake_jmap.FakeJMAP):
    """wolf's account, with the query the drip makes: from her, after a time."""

    def m_Email_query(self, args, call_id, created_ids):
        flt = args.get("filter") or {}
        if "from" not in flt:
            return super().m_Email_query(args, call_id, created_ids)
        ids = [eid for eid, em in self.emails.items()
               if flt["from"].lower() in fake_jmap.header_text(em["bytes"], "from").lower()]
        return [["Email/query", {"accountId": fake_jmap.ACCOUNT, "queryState": "q1", "canCalculateChanges": False,
                                 "position": 0, "ids": ids}, call_id]]

    def m_Email_get(self, args, call_id, created_ids):
        found = []
        for eid in args.get("ids") or []:
            m = email.message_from_bytes(self.emails[eid]["bytes"], policy=email.policy.default)
            body = m.get_body(preferencelist=("plain",))
            found.append({"id": eid, "messageId": fake_jmap.message_ids(self.emails[eid]["bytes"]),
                          "inReplyTo": [str(m["In-Reply-To"]).strip("<> ")] if m["In-Reply-To"] else None,
                          "from": [{"name": None, "email": email.utils.parseaddr(str(m["From"]))[1]}],
                          "textBody": [{"partId": "1"}], "bodyValues": {"1": {"value": body.get_content()}}})
        return [["Email/get", {"accountId": fake_jmap.ACCOUNT, "state": "e1", "list": found, "notFound": []}, call_id]]


class Live(unittest.TestCase):

    def setUp(self):
        self.box = fake_jmap.for_account(WOLF, "dummy-wolf-jmap-token-for-drip-tests-7171")
        self.box.__class__ = WolfMailbox
        self.addCleanup(self.box.stop)
        for patch in fake_jmap.dialling(self.box):
            patch.start()
            self.addCleanup(patch.stop)
        env = {"WOLF_EMAIL": WOLF, "WOLF_JMAP_TOKEN": "dummy-wolf-jmap-token-for-drip-tests-7171",
               "DORIS_OWNER_ADDRESS": OWNER, "DORIS_MAIL_ADDRESS": fake_jmap.DORIS}
        self.t = drip.LiveTransport(env, utc(2026, 11, 29, 4, 0))

    def mail(self, frm, irt, text):
        return ("From: %s\r\nTo: %s\r\nSubject: Re: x\r\nMessage-ID: <%s@fastmail.com>\r\nIn-Reply-To: %s\r\n"
                "Content-Type: text/plain; charset=utf-8\r\n\r\n%s\r\n"
                % (frm, WOLF, os.urandom(8).hex(), irt, text)).encode("utf-8")

    def test_it_reads_only_her_answers_with_their_text(self):
        self.box.deliver(self.mail(fake_jmap.DORIS, "<abc@fastmail.com>", "You're booked with Shane Bornstein"))
        self.box.deliver(self.mail("someone@example.com", "<abc@fastmail.com>", "You're booked with Shane"))
        found = self.t.answers()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].answers, "<abc@fastmail.com>")
        self.assertEqual(drip.kind_of(found[0].text)[0], drip.BOOKED)

    def test_it_sends_to_the_owner_or_to_her_and_nowhere_else(self):
        raw = drip.build(WOLF, OWNER, "s", "b", "<%s@fastmail.com>" % ("a" * 32), utc(2026, 11, 29, 4, 0))
        self.t.send(raw, True, "<%s@fastmail.com>" % ("a" * 32))
        raw = drip.build(WOLF, fake_jmap.DORIS, "s", "b", "<%s@fastmail.com>" % ("b" * 32), utc(2026, 11, 29, 4, 0),
                         "<her@fastmail.com>")
        self.t.send(raw, False, "<%s@fastmail.com>" % ("b" * 32))
        self.assertEqual([rcpts for _, rcpts, _ in self.box.delivered], [[OWNER], [fake_jmap.DORIS]])


if __name__ == "__main__":
    unittest.main()
