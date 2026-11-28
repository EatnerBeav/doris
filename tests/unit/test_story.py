"""The end of the demo: what Doris did for the owner, in plain words, from her receipts alone.

story.build(lines) reads receipt lines (already verified) and returns plain-text sections; story.render
turns them into the terminal's lines; story.lines(path) checks the chain first and returns the lines the
demo prints when its run ends. Nothing in them comes from a mail body or the model: names only, made
safe as her notices make them, and every claim backed by a line (a reply only when Sent found it, a
meeting only while her booking still stands).

The first class runs the real demo day once, in this process, on a copy of the committed sandbox with
the stub classifier (as test_demo does). The rest build small days by hand.
"""

import datetime as _dt
import os
import shutil
import tempfile
import unittest

from unittest import mock

from doris import loop, paths, receipts, story, templates, why
from tests.unit import demo_sandbox

OWNER = "whizz@fastmail.com"
DORIS = "doris.htm@fastmail.com"


def text_of(lines):
    return "\n".join(lines)


class TheDemoDay(unittest.TestCase):
    """The committed demo day, told from its receipts."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="doris-story-")
        sandbox = demo_sandbox.copy_committed(os.path.join(cls.tmp, "sandbox"))
        cfg = loop.RunConfig(mode="throwaway", run_name="model-stub-sandbox-test",
                             state_dir=os.path.join(cls.tmp, "state"), sandbox_root=sandbox, owner_addr=OWNER,
                             doris_addr=DORIS, model_source="stub", clock_mode="advance", digest=True)
        cls.summary = loop.run(cfg)
        cls.path = cls.summary.receipts_path
        cls.lines = receipts.read_lines(cls.path)
        cls.story = story.build(cls.lines)
        cls.printed = story.lines(cls.path, width=1000)
        cls.text = text_of(cls.printed)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_the_headline_counts_every_email_the_way_the_summary_does(self):
        s = self.summary
        self.assertEqual((s.handled, s.held, s.human_approvals), (13, 2, 0))
        self.assertEqual(self.story["headline"],
                         "15 emails came in: 13 handled by her (6 answered, 7 needed no reply), 2 held for Shane. "
                         "1 meeting on his calendar. 0 human approvals.")

    def test_needs_shane_lists_each_hold_with_its_approved_reason_and_when(self):
        needs = self.story["needs"]
        self.assertEqual(needs["title"], "Needs Shane (2): she sent no reply to these; for each, a notice with a "
                                         "draft reply went to him.")
        self.assertEqual(needs["rows"], [["1:10 PM", "Elena Voss", "money, legal or a complaint"],
                                         ["2:20 PM", "Marcus Lee", "asks to add or write to another person"]])

    def test_the_calendar_shows_only_the_booking_still_standing(self):
        cal = self.story["calendar"]
        self.assertEqual(cal["title"], "On Shane's calendar (1), as sandbox files")
        self.assertEqual(cal["rows"], [["Thu Dec 3, 9:00 AM Pacific, 30 min", "Marcus Lee"]])

    def test_each_thread_is_told_in_plain_words(self):
        threads = dict((who, what) for who, what in self.story["threads"]["rows"])
        self.assertEqual(threads["Priya Raman"],
                         "Asked for Wed Dec 2, 11:00 AM: booked. Asked to move: she cancelled that booking and "
                         "offered 3 new times.")
        self.assertEqual(threads["Marcus Lee"],
                         "No exact time: offered 3 times. Picked Thu Dec 3, 9:00 AM from her times: booked (the "
                         "calendar timed out once; she retried). Held for Shane: asks to add or write to another "
                         "person.")
        self.assertEqual(threads["Dana Okafor"],
                         "Fri Dec 4, 2:00 PM was taken: offered 3 other times. Auto-reply: filed. Opted out: she "
                         "won't write to them again (1 queued request dropped). Filed with no reply, since they "
                         "opted out.")
        self.assertEqual(threads["Sam Rivera"],
                         "Asked for Mon Dec 7, 10:00 AM; another event landed while she booked it, so she removed "
                         "hers and offered 3 other times.")
        self.assertEqual(threads["Ken Ito"], "Instructions or a lure only: filed, nothing done.")
        self.assertEqual(threads["Ops Weekly"], "List mail: filed.")
        self.assertEqual(threads["Jordan Pike"], "Opted out: she won't write to them again.")
        self.assertEqual(threads["Elena Voss"], "Held for Shane: money, legal or a complaint.")
        self.assertEqual(threads["(unreadable message)"], "Could not be decoded: quarantined; the run went on.")
        self.assertEqual(self.story["threads"]["title"], "Thread by thread (9)")
        self.assertEqual(self.story["threads"]["more"], "")

    def test_who_is_waiting_on_her_times_matches_the_digest(self):
        self.assertEqual(self.story["waiting"], "Waiting to pick one of her times: Priya Raman, Sam Rivera.")

    def test_the_digest_line_comes_from_its_send_receipt(self):
        self.assertEqual(self.story["digest"], "Digest to Shane at 5:00 PM: \"Handled 13, held 2, human approvals "
                                               "0.\" Found in her Sent folder.")

    def test_cost_and_gate(self):
        self.assertEqual(self.story["cost"], "Model calls 9, cost none (stub).")
        self.assertEqual(self.story["gate"], "The gate ran 14 times, once before each send or calendar write; "
                                             "0 blocked.")

    def test_the_scope_names_the_day_the_mode_and_the_zone(self):
        self.assertEqual(self.story["title"], "What Doris did for Shane")
        self.assertEqual(self.story["scope"], "Tue Dec 1, from her receipts. Times are Pacific.")
        self.assertEqual(self.story["state"], "finished")
        self.assertEqual(self.story["halt"], "")

    def test_the_printed_screen_reads_top_to_bottom(self):
        p = self.printed
        self.assertEqual(p[0], "")
        self.assertEqual(p[1], "What Doris did for Shane: Tue Dec 1, from her receipts. Times are Pacific.")
        self.assertEqual(p[2], self.story["headline"])
        self.assertIn("  1:10 PM  Elena Voss  money, legal or a complaint", p)
        self.assertIn("  Thu Dec 3, 9:00 AM Pacific, 30 min  Marcus Lee", p)
        self.assertIn("  Priya Raman           Asked for Wed Dec 2, 11:00 AM: booked. Asked to move: she cancelled "
                      "that booking and offered 3 new times.", p)
        self.assertEqual(p[-1], "Receipts: %d lines, chain verified." % len(self.lines))
        self.assertIn(self.story["digest"], p)
        self.assertIn(self.story["cost"] + " " + self.story["gate"], p)

    def test_no_mail_body_no_model_words_no_meeting_link(self):
        for words in ("charge", "three-month pilot", "cofounder", "ana@", "ana[@]", "full calendar", "supplier inbox",
                      "calendar-confirm", "meet.example", "[meeting link]", "raw", "side_question", "Ignore"):
            self.assertNotIn(words, self.text)

    def test_at_eighty_columns_every_line_fits_and_wraps_under_its_column(self):
        narrow = story.lines(self.path, width=80)
        self.assertTrue(all(len(line) <= 80 for line in narrow), [l for l in narrow if len(l) > 80])
        at = narrow.index("Thread by thread (9)")
        self.assertTrue(narrow[at + 1].startswith("  Priya Raman           Asked for Wed Dec 2, 11:00 AM: booked."))
        self.assertTrue(narrow[at + 2].startswith(" " * 24) and not narrow[at + 2].startswith(" " * 25), narrow[at + 2])
        self.assertFalse(any(line.lstrip().startswith(("AM", "PM")) for line in narrow), narrow)

    def test_a_throwaway_run_points_nowhere(self):
        self.assertNotIn("sandbox/outbox", self.text)


# --- small days, built by hand ----------------------------------------------------------------------
T0 = "2026-12-01T16:00:00Z"


class Day:
    """Receipt lines for a hand-built day, in the loop's shapes (only the fields the story reads)."""

    def __init__(self, mode="sandbox", source="stub", start=T0):
        self.mode = mode
        self.lines = []
        self.add("observed", {"step": "start", "mode": mode, "model_source": source, "clock": start}, ts=start)

    def add(self, kind, payload, subject="", why_=None, ts="2026-12-01T16:10:00Z"):
        line = {"seq": len(self.lines) + 1, "ts": ts, "run_id": "test", "mode": self.mode, "kind": kind,
                "subject": subject, "payload": payload}
        if why_ is not None:
            line["why"] = why_
        self.lines.append(line)
        return line["seq"]

    def mail(self, subject, name, addr, thread=None, ts="2026-12-01T16:10:00Z"):
        self.add("observed", {"from_name": name, "from_addr": addr, "thread": thread or subject}, subject, ts=ts)

    def call(self, subject, call, target, result, **payload):
        seq = self.add("called", dict(payload, call=call, target=target), subject)
        self.add("result", dict(result, call_seq=seq), subject)
        return seq

    def model(self, subject, cost=0.0, source="stub"):
        self.call(subject, "model", source, {"ok": True, "cost_usd": cost, "source": source}, source=source)

    def send(self, subject, template, slots, to="sender", found=True, mid=None, ts="2026-12-01T16:10:00Z",
             result=None):
        mid = mid or "<%s-%s@x>" % (subject, template)
        seq = self.add("called", {"call": "send", "target": template, "to": to, "message_id": mid, "slots": slots,
                                  "bcc": "owner" if to == "sender" else None}, subject, ts=ts)
        self.add("result", dict(result or {"ok": True, "state": "handed_over"}, call_seq=seq), subject, ts=ts)
        self.call(subject, "sent_lookup", template, {"ok": True, "found": found}, message_id=mid)

    def check(self, subject, action, passed=True):
        self.add("checked", {"action": action, "passed": passed, "first_fail": None if passed else "G6"}, subject,
                 why.why("gate.pass", n=17) if passed else why.why("gate.g6_recipient", rule="sender"))

    def close(self, subject, outcome, code, template="-", thread=None, held=False, slots=None, ts=None, **extra):
        payload = dict(extra, outcome=outcome, template=template, thread=thread or subject)
        self.add("held" if held else "decided", payload, subject, why.why(code, **(slots or {})),
                 ts=ts or "2026-12-01T16:10:00Z")

    def summary(self, **counts):
        self.add("summary", counts)

    def write(self, path):
        """The day as a real receipt file, chained by her own writer."""
        clock = iter(_dt.datetime.strptime(l["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)
                     for l in self.lines)
        w = receipts.ReceiptWriter(path, "test", "throwaway" if self.mode not in receipts.MODES else self.mode,
                                   clock=lambda: next(clock))
        for l in self.lines:
            w.write(l["kind"], l["payload"], subject=l["subject"], why=l.get("why"))


def booked_day(found=True, name="Priya Raman", source="stub", cost=0.0):
    d = Day(source=source)
    d.mail("m1", name, "priya@northwind.example")
    d.model("m1", cost=cost, source=source)
    d.check("m1", "calendar_create")
    d.call("m1", "calendar_create", "doris-aaa", {"ok": True, "state": "written"}, attempt=1)
    d.call("m1", "calendar_get", "doris-aaa", {"ok": True, "found": True, "booking_id": "doris-aaa",
                                               "start": "2026-12-02T19:00:00Z", "end": "2026-12-02T19:30:00Z"},
           attempt=1)
    d.check("m1", "send_reply")
    d.send("m1", "T1", {"start": "2026-12-02T19:00:00Z", "duration": 30, "zone": "Central"}, found=found)
    d.close("m1", "book", "policy.book_clear_time", template="T1", booking_id="doris-aaa")
    return d


class SmallDays(unittest.TestCase):
    def test_a_reply_sent_is_counted_only_when_sent_found_it(self):
        s = story.build(booked_day(found=False).lines)
        self.assertIn("1 email came in: 1 handled by her (0 answered, 0 needed no reply, 1 closed without a confirmed "
                      "reply), 0 held for Shane.", s["headline"])
        s = story.build(booked_day(found=True).lines)
        self.assertIn("(1 answered, 0 needed no reply)", s["headline"])

    def test_a_hostile_display_name_comes_out_as_text(self):
        evil = "\x1b[2J\x1b[31mEvil‮ Bob\r\nRoot: ok"
        printed = text_of(story.render(story.build(booked_day(name=evil).lines), width=1000))
        self.assertNotIn("\x1b", printed)
        self.assertNotIn("‮", printed)
        self.assertNotIn("\r", printed)
        self.assertFalse(any(line.startswith("Root") for line in printed.split("\n")))

    def test_cost_is_worded_as_the_terminal_words_it(self):
        self.assertEqual(story.build(booked_day(source="replayed", cost=0.0123).lines)["cost"],
                         "Model calls 1, cost $0.0123 (recorded).")
        self.assertEqual(story.build(booked_day(source="live", cost=0.0042).lines)["cost"],
                         "Model calls 1, cost $0.0042.")

    def test_a_human_approval_is_counted_never_assumed(self):
        d = booked_day()
        d.add("decided", {"human_approval": True, "step": "plan"}, "m1", why.why("policy.book_clear_time"))
        self.assertTrue(story.build(d.lines)["headline"].endswith("1 human approval."))

    def test_an_unlisted_close_shows_its_own_why_text(self):
        d = Day()
        d.mail("m1", "Lee Park", "lee@park.example")
        d.close("m1", "superseded", "policy.resume_hold")
        rows = story.build(d.lines)["threads"]["rows"]
        self.assertEqual(rows, [["Lee Park", why.CODES["policy.resume_hold"]]])

    def test_no_closing_line_reads_so_far(self):
        s = story.build(booked_day().lines)
        self.assertEqual(s["state"], "so far")
        self.assertEqual(s["scope"], "Tue Dec 1, so far, from her receipts. Times are Pacific.")

    def test_a_halted_run_says_what_stopped_it_and_what_was_cut_off(self):
        d = booked_day()
        d.add("halted", {"cause": "watch_button", "in_flight": {"seq": 7, "call": "calendar_query", "target": "span"},
                         "dropped": [{"kind": "send"}, {"kind": "booking"}]}, "", why.why("halt.watch_button"),
              ts="2026-12-01T18:31:00Z")
        s = story.build(d.lines)
        self.assertEqual(s["state"], "halted")
        self.assertEqual(s["halt"], "Halted at 10:31 AM: Stop pressed on the watch page. Cut off mid-call: "
                                    "calendar_query. 2 queued items dropped. Nothing more goes out until "
                                    "%s." % paths.command("resume"))
        self.assertEqual(s["scope"], "Tue Dec 1, up to the stop, from her receipts. Times are Pacific.")

    def test_a_long_run_caps_the_thread_list_and_says_how_many_more(self):
        d = Day()
        for n in range(story.MAX_THREADS + 3):
            d.mail("m%d" % n, "Sender %d" % n, "s%d@x.example" % n)
            d.close("m%d" % n, "file", "policy.file_fyi")
        s = story.build(d.lines)
        self.assertEqual(len(s["threads"]["rows"]), story.MAX_THREADS)
        self.assertEqual(s["threads"]["more"], "3 more threads: %s shows every step." % paths.command("replay"))

    def test_a_run_over_two_days_gives_full_dates(self):
        d = Day()
        d.mail("m1", "Ann Bell", "ann@bell.example", ts="2026-12-01T21:10:00Z")
        d.close("m1", "hold:money", "hold.money", held=True, ts="2026-12-01T21:10:00Z")
        d.mail("m2", "Bo Chan", "bo@chan.example", ts="2026-12-02T17:00:00Z")
        d.close("m2", "hold:money", "hold.money", held=True, ts="2026-12-02T17:00:00Z")
        s = story.build(d.lines)
        self.assertEqual(s["scope"], "Tue Dec 1 to Wed Dec 2, so far, from her receipts. Times are Pacific.")
        self.assertEqual([r[0] for r in s["needs"]["rows"]], ["Tue Dec 1, 1:10 PM", "Wed Dec 2, 9:00 AM"])

    def test_a_hold_without_a_notice_found_in_sent_says_so(self):
        d = Day()
        d.mail("m1", "Ann Bell", "ann@bell.example")
        d.send("m1", "S1", {"reason": "hold.money"}, to="owner", found=False)
        d.close("m1", "hold:money", "hold.money", held=True)
        needs = story.build(d.lines)["needs"]
        self.assertEqual(needs["title"], "Needs Shane (1): she sent no reply to these.")
        self.assertEqual(needs["rows"][0][2], "money, legal or a complaint (her notice to him was not found in Sent)")


class WhatTheReceiptsBack(unittest.TestCase):
    """Fable review of PR #18: each claim only as far as a line backs it, and no line can make it raise."""

    def test_odd_values_in_a_chain_that_verifies_never_raise(self):
        cases = {
            "a message ID that is a list": lambda d: d.call("m1", "send", "T2", {"ok": True}, to="sender",
                                                            message_id=["x"], slots={}),
            "a template that is a list": lambda d: d.add("decided", {"outcome": "offer", "template": ["T1"]}, "m1",
                                                         {"code": "policy.offer_no_clear_time", "text": "x"}),
            "a stamp in year one": lambda d: d.add("checked", {"passed": True}, "m1", ts="0001-01-01T00:00:00Z"),
            "a read-back in year one": lambda d: d.call("m1", "calendar_get", "doris-aaa",
                                                        {"ok": True, "found": True, "start": "0001-01-01T00:00:00Z",
                                                         "end": "0001-01-01T00:30:00Z"}),
            "an asked time in year one": lambda d: (
                d.send("m1", "T2", {"asked": "0001-01-01T00:00:00Z", "slots": ["a", "b"]}),
                d.close("m1", "offer", "policy.offer_conflict", template="T2", slots={"rule": "overlap"})),
            "a huge cost": lambda d: d.model("m1", cost=10 ** 400),
        }
        for what, add in cases.items():
            with self.subTest(what):
                d = booked_day()
                add(d)
                story.render(story.build(d.lines), 100)
        with self.subTest("a huge duration and no read-back"):
            d = Day()
            d.mail("m1", "Priya Raman", "priya@northwind.example")
            d.call("m1", "calendar_create", "doris-aaa", {"ok": True, "state": "written"})
            d.send("m1", "T1", {"start": "2026-12-02T19:00:00Z", "duration": 10 ** 30})
            d.close("m1", "book", "policy.book_clear_time", template="T1", booking_id="doris-aaa")
            story.render(story.build(d.lines), 100)

    def test_a_story_that_cannot_be_built_says_so_in_one_line(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "receipts.jsonl")
        booked_day().write(path)
        with mock.patch.object(story, "build", side_effect=RuntimeError("x")):
            out = story.lines(path, width=1000)
        self.assertEqual(out, ["", "No summary: it could not be built from these receipts (RuntimeError). Check "
                                   "them with %s and %s." % (paths.command("verify"), paths.command("replay"))])

    def test_a_send_whose_answer_was_lost_counts_once_sent_found_it(self):
        d = Day()
        d.mail("m1", "Priya Raman", "priya@northwind.example")
        d.send("m1", "T2", {"slots": ["a", "b", "c"]}, result={"ok": False, "state": "unknown"})
        d.close("m1", "offer", "policy.offer_no_clear_time", template="T2", slots={"cond": "B7"})
        d.send("", "S2", {"handled": 1, "held": 0, "approvals": 0}, to="owner", result={"ok": False, "state": "unknown"})
        s = story.build(d.lines)
        self.assertIn("1 handled by her (1 answered, 0 needed no reply)", s["headline"])
        self.assertTrue(s["digest"].endswith("Found in her Sent folder."), s["digest"])

    def test_a_send_never_looked_for_is_not_called_missing(self):
        d = Day()
        seq = d.add("called", {"call": "send", "target": "S2", "to": "owner", "message_id": "<d@x>",
                               "slots": {"handled": 0, "held": 0, "approvals": 0}})
        d.add("result", {"call_seq": seq, "ok": False, "state": "unknown"})
        self.assertTrue(story.build(d.lines)["digest"].endswith("Not confirmed in her Sent folder."))

    def test_a_booking_never_read_back_is_not_on_his_calendar(self):
        d = Day()
        d.mail("m1", "Priya Raman", "priya@northwind.example")
        d.call("m1", "calendar_create", "doris-aaa", {"ok": True, "state": "written"})
        d.call("m1", "calendar_get", "doris-aaa", {"ok": True, "found": False})
        d.close("m1", "hold:unknown_result", "hold.unknown_result", held=True)
        s = story.build(d.lines)
        self.assertIn("0 meetings on his calendar", s["headline"])
        self.assertEqual(s["calendar"]["rows"], [])

    def test_a_removal_that_failed_still_shows_the_meeting(self):
        d = Day()
        d.mail("m1", "Priya Raman", "priya@northwind.example")
        d.call("m1", "calendar_create", "doris-aaa", {"ok": True, "state": "written"})
        got = {"ok": True, "found": True, "start": "2026-12-02T19:00:00Z", "end": "2026-12-02T19:30:00Z"}
        d.call("m1", "calendar_get", "doris-aaa", got)
        d.call("m1", "calendar_delete", "doris-aaa", {"ok": True})
        d.call("m1", "calendar_get", "doris-aaa", got)
        d.close("m1", "hold:remove_failed", "hold.remove_failed", held=True)
        s = story.build(d.lines)
        self.assertIn("1 meeting on his calendar", s["headline"])
        self.assertEqual(s["calendar"]["rows"], [["Wed Dec 2, 11:00 AM Pacific, 30 min", "Priya Raman"]])
        self.assertNotIn("listed below", s["needs"]["rows"][0][2])

    def test_the_halt_line_claims_nothing_in_flight_only_when_the_line_says_none(self):
        missing = object()
        for flight, words in (("none", "Nothing was in flight."), (None, "Nothing was in flight."),
                              (missing, "What was in flight is not recorded.")):
            with self.subTest(flight):
                d = booked_day()
                payload = {"cause": "watch_button", "dropped": []}
                if flight is not missing:
                    payload["in_flight"] = flight          # the loop writes "none", the supervisor null
                d.add("halted", payload, "", why.why("halt.watch_button"), ts="2026-12-01T18:31:00Z")
                self.assertIn(words, story.build(d.lines)["halt"])

    def test_a_halt_followed_by_a_restart_is_not_the_end(self):
        d = booked_day()
        d.add("halted", {"cause": "watch_button", "in_flight": "none", "dropped": []}, "", why.why("halt.watch_button"))
        d.add("observed", {"step": "start", "mode": "sandbox", "model_source": "stub", "clock": T0})
        d.summary(handled=1)
        s = story.build(d.lines)
        self.assertEqual((s["state"], s["halt"]), ("finished", "Stopped at 8:10 AM: Stop pressed on the watch page. "
                                                           "Then resumed: this covers both runs."))

    def test_sensitive_mail_shows_no_name(self):
        d = Day()
        d.mail("m1", "Dr Lee", "lee@clinic.example")
        d.close("m1", "file", "screen.sensitive_skip")
        self.assertEqual(story.build(d.lines)["threads"]["rows"][0][0], "(sensitive mail, unread)")

    def test_an_inbox_read_that_failed_says_so(self):
        d = Day()
        d.call("", "mail_fetch", "inbox", {"ok": False, "error": "timeout"})
        self.assertEqual(story.build(d.lines)["headline"], "No new mail: her inbox read failed. 0 human approvals.")

    def test_no_reply_is_claimed_for_a_held_email_that_got_one(self):
        d = Day()
        d.mail("m1", "Ann Bell", "ann@bell.example")
        d.send("m1", "T2", {"slots": ["a", "b", "c"]})
        d.send("m1", "S1", {"reason": "hold.money"}, to="owner")
        d.close("m1", "hold:money", "hold.money", held=True)
        self.assertEqual(story.build(d.lines)["needs"]["title"], "Needs Shane (1)")

    def test_more_than_one_digest_names_the_last(self):
        d = Day()
        d.send("", "S2", {"handled": 0, "held": 0, "approvals": 0}, to="owner", mid="<d1@x>")
        d.send("", "S2", {"handled": 1, "held": 0, "approvals": 0}, to="owner", mid="<d2@x>")
        self.assertTrue(story.build(d.lines)["digest"].startswith(
            'Digest to Shane at 8:10 AM: "Handled 1, held 0, human approvals 0." Found in her Sent folder. (The last of 2.)'))


class AStopInTheDay(unittest.TestCase):
    """The README judge run (Sat Nov 28): a stop on the watch page, then resume and a second demo run."""

    def stopped(self, d, ts="2026-11-28T08:58:00Z"):
        """The supervisor's halted line: written on the wall clock, with its stop timing."""
        d.add("halted", {"cause": "watch_button", "in_flight": "none", "dropped": [], "stop_to_dead_ms": 140,
                         "requested_utc": ts, "worker_exit": 0}, "", why.why("halt.watch_button"), ts=ts)

    def test_a_stop_keeps_the_day_on_her_clock_and_says_the_real_time(self):
        d = booked_day()
        self.stopped(d)
        s = story.build(d.lines)
        self.assertEqual(s["scope"], "Tue Dec 1, up to the stop, from her receipts. Times are Pacific.")
        self.assertTrue(s["halt"].startswith("Halted Sat Nov 28 at 12:58 AM Pacific, real time, after her 8:10 AM "
                                             "step: Stop pressed on the watch page."), s["halt"])

    def test_a_command_is_never_split_across_lines(self):
        d = booked_day()
        self.stopped(d)
        cmd = paths.command("resume")
        for width in range(60, 101):
            printed = story.render(story.build(d.lines), width)
            self.assertTrue(any(cmd in line for line in printed), (width, printed))

    def test_an_opt_out_on_another_thread_ends_their_open_offer(self):
        d = Day()
        d.mail("m1", "Bob Ng", "bob@ng.example")
        d.send("m1", "T2", {"slots": ["a", "b", "c"]})
        d.close("m1", "offer", "policy.offer_no_clear_time", template="T2", slots={"cond": "B7"})
        d.mail("m2", "Bob Ng", "BOB@ng.example", thread="t2")
        d.close("m2", "suppress", "screen.stop", thread="t2", slots={"n": 0})
        self.assertEqual(story.build(d.lines)["waiting"], "")

    def test_a_pick_cut_off_by_a_stop_leaves_their_offer_open(self):
        d = Day()
        d.mail("m1", "Marcus Lee", "marcus@lee.example")
        d.send("m1", "T2", {"slots": ["a", "b", "c"]})
        d.close("m1", "offer", "policy.offer_no_clear_time", template="T2", slots={"cond": "B7"})
        d.mail("m2", "Marcus Lee", "marcus@lee.example", thread="m1")
        d.add("decided", {"step": "plan", "action": "book", "thread": "m1"}, "m2", why.why("policy.pick_books", n=2))
        d.add("called", {"call": "calendar_query", "target": "span"}, "m2")
        self.stopped(d)
        self.assertEqual(story.build(d.lines)["waiting"], "Waiting to pick one of her times: Marcus Lee.")

    def runs(self, *days):
        """Run folders under runs/, (name, Day, mtime) each; returns the last one's receipts path."""
        tmp = tempfile.mkdtemp(prefix="doris-story-runs-")
        self.addCleanup(shutil.rmtree, tmp, True)
        for name, day, when in days:
            os.makedirs(os.path.join(tmp, "runs", name))
            path = os.path.join(tmp, "runs", name, "receipts.jsonl")
            if day is None:
                open(path, "wb").close()
            else:
                day.write(path)
            os.utime(path, (when, when))
        return path

    def filed_day(self, mode="sandbox"):
        d = Day(mode=mode)
        d.mail("m3", "Ken Ito", "ken@ito.example")
        d.close("m3", "file", "policy.file_fyi")
        d.summary(handled=1, held=0, human_approvals=0)
        return d

    def test_a_run_that_halted_itself_then_wrote_its_summary_is_still_the_same_day(self):
        first = booked_day()
        first.add("halted", {"cause": "supervisor_gone", "in_flight": "none", "dropped": [], "summary": "follows"},
                  "", why.why("halt.supervisor_gone"))
        first.summary(handled=1, held=0, human_approvals=0)
        text = "\n".join(story.lines(self.runs(("r1", first, 1000), ("r2", self.filed_day(), 2000)), width=1000))
        self.assertIn("2 emails came in", text)
        self.assertIn("in 2 runs", text)

    def test_live_runs_are_never_joined_into_a_day(self):
        first = Day(mode="live")
        first.mail("m1", "Ann Bell", "ann@bell.example")
        first.close("m1", "file", "policy.file_fyi")
        self.stopped(first)
        second = self.filed_day(mode="live")
        text = "\n".join(story.lines(self.runs(("r1", first, 1000), ("r2", second, 2000)), width=1000))
        self.assertIn("Receipts: %d lines, chain verified." % len(second.lines), text)

    def test_runs_written_in_the_same_second_keep_their_order_by_name(self):
        first = booked_day()
        self.stopped(first)
        path = self.runs(("model-stub-sandbox-20261128T010000Z", first, 5000),
                         ("model-stub-sandbox-20261128T020000Z", self.filed_day(), 5000))
        self.assertIn("in 2 runs", "\n".join(story.lines(path, width=1000)))

    def test_an_empty_receipt_file_is_not_a_run(self):
        first = booked_day()
        self.stopped(first)
        path = self.runs(("r0", None, 500), ("r1", first, 1000), ("r2", self.filed_day(), 2000))
        text = "\n".join(story.lines(path, width=1000))
        self.assertIn("in 2 runs", text)
        self.assertIn("this covers both runs", text)

    def test_a_day_stopped_and_resumed_is_told_whole(self):
        tmp = tempfile.mkdtemp(prefix="doris-story-runs-")
        self.addCleanup(shutil.rmtree, tmp, True)
        runs = os.path.join(tmp, "runs")

        def run(name, day, when):
            os.makedirs(os.path.join(runs, name))
            path = os.path.join(runs, name, "receipts.jsonl")
            day.write(path)
            os.utime(path, (when, when))
            return path

        before = Day()                       # an earlier day that finished: not this day's
        before.mail("m0", "Old Mail", "old@x.example")
        before.close("m0", "file", "policy.file_fyi")
        before.summary(handled=1, held=0, human_approvals=0)
        run("a-finished", before, 1000)
        first = booked_day()                 # this day, up to the stop
        self.stopped(first)
        run("b-stopped", first, 2000)
        second = Day()                       # after resume: the rest of the day
        second.mail("m3", "Ken Ito", "ken@ito.example")
        second.check("m3", "send_reply")
        second.close("m3", "file", "policy.file_fyi")
        second.summary(handled=1, held=0, human_approvals=0)
        path = run("c-resumed", second, 3000)

        printed = story.lines(path, width=1000)
        text = "\n".join(printed)
        self.assertIn("2 emails came in: 2 handled by her (1 answered, 1 needed no reply), 0 held for Shane. "
                      "1 meeting on his calendar. 0 human approvals.", text)
        self.assertIn("Stopped Sat Nov 28 at 12:58 AM Pacific, real time, after her 8:10 AM step: Stop pressed on "
                      "the watch page. Then resumed: this covers both runs.", text)
        self.assertIn("The gate ran 3 times", text)
        self.assertNotIn("Old Mail", text)
        self.assertIn("Receipts: %d lines in 2 runs, each chain verified." % (len(first.lines) + len(second.lines)),
                      text)


class ABookingFoundGone(unittest.TestCase):
    def test_a_message_id_that_starts_with_booking_is_still_an_email(self):
        d = Day()
        d.mail("booking:x@evil.example", "Eve Mallory", "eve@evil.example")
        d.close("booking:x@evil.example", "hold:money", "hold.money", held=True)
        s = story.build(d.lines)
        self.assertTrue(s["headline"].startswith("1 email came in: 0 handled by her"), s["headline"])
        self.assertIn("1 held for Shane", s["headline"])
        self.assertNotIn("booking", s["headline"])
        self.assertEqual(s["needs"]["rows"][0][1], "Eve Mallory")

    """Ruling 17: a booking of hers found gone is held for Shane under subject booking:<id>; it is not an email."""

    def test_a_booking_he_removed_is_held_for_him_and_is_not_an_email(self):
        d = booked_day()
        d.call("booking:doris-aaa", "calendar_get", "doris-aaa", {"ok": True, "found": False})
        d.add("observed", {"step": "booking_check", "booking_id": "doris-aaa"}, "booking:doris-aaa")
        d.add("held", {"outcome": "hold:owner_removed", "template": "-"}, "booking:doris-aaa",
              why.why("hold.owner_removed"), ts="2026-12-01T18:00:00Z")
        d.send("booking:doris-aaa", "S1", {"reason": "hold.owner_removed"}, to="owner")
        s = story.build(d.lines)
        self.assertEqual(s["headline"], "1 email came in: 1 handled by her (1 answered, 0 needed no reply), 0 held "
                                        "for Shane. 0 meetings on his calendar. 1 booking he removed by hand is held "
                                        "for him. 0 human approvals.")
        self.assertEqual(s["needs"]["rows"], [["10:00 AM", "Priya Raman", templates.HOLD_REASONS["hold.owner_removed"]]])
        self.assertEqual(s["threads"]["rows"], [["Priya Raman", "Asked for Wed Dec 2, 11:00 AM: booked. Held for "
                                                 "Shane: %s." % templates.HOLD_REASONS["hold.owner_removed"]]])


class NothingNew(unittest.TestCase):
    def test_a_run_with_no_new_mail_says_so_in_one_line_and_shows_no_empty_sections(self):
        d = Day()
        d.call("", "mail_fetch", "inbox", {"ok": True, "count": 2})
        d.call("", "mail_fetch", "inbox", {"ok": True, "count": 15})
        d.summary(handled=0, held=0, human_approvals=0)
        s = story.build(d.lines)
        self.assertEqual(s["headline"], "No new mail: the inbox held 15 messages, none of them new to her. "
                                        "0 human approvals.")
        printed = story.render(s, width=1000)
        self.assertEqual(printed, ["", "What Doris did for Shane: Tue Dec 1, from her receipts. Times are Pacific.",
                                   s["headline"], "", "Model calls 0, cost none (stub). The gate ran 0 times, "
                                                      "once before each send or calendar write; 0 blocked."])


class FromTheFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="doris-story-file-")
        self.path = os.path.join(self.tmp, "receipts.jsonl")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_file_no_story(self):
        self.assertEqual(story.lines(self.path), [])

    def test_a_sandbox_run_points_to_her_mail_her_bookings_and_replay(self):
        d = booked_day()
        d.summary(handled=1, held=0, human_approvals=0)
        d.write(self.path)
        printed = story.lines(self.path, width=1000)
        self.assertEqual(printed[-2:], [
            "Receipts: %d lines, chain verified." % len(d.lines),
            "See for yourself: her sent mail in sandbox/outbox/, her bookings in sandbox/calendar/, every step "
            "with %s." % paths.command("replay")])

    def test_a_broken_chain_tells_no_story(self):
        d = booked_day()
        d.summary(handled=1, held=0, human_approvals=0)
        d.write(self.path)
        with open(self.path, "rb") as fh:
            data = fh.read()
        with open(self.path, "wb") as fh:
            fh.write(data.replace(b"Priya Raman", b"Priya Ramen"))
        printed = story.lines(self.path, width=1000)
        self.assertEqual(len(printed), 2)
        self.assertEqual(printed[0], "")
        self.assertTrue(printed[1].startswith("No summary: the receipt chain is BROKEN at line 2"), printed)
        self.assertIn(paths.command("verify"), printed[1])


if __name__ == "__main__":
    unittest.main()
