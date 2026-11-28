"""digest.py: the S2 slots, counted from receipt lines only."""
import unittest
from unittest import mock

from doris import digest, templates, why
from tests.unit.voice_fakes import FakeZones, utc

NOW = utc(2026, 12, 2, 1, 0)   # Tue Dec 1, 5:00 PM Pacific


class Receipts(object):
    """Builds receipt-shaped dicts (the fields digest.py reads)."""

    def __init__(self):
        self.lines = []

    def add(self, kind, subject, payload=None, code=None, ts="2026-12-01T16:00:00Z", **slots):
        line = {"ts": ts, "run_id": "r", "mode": "sandbox", "seq": len(self.lines) + 1, "kind": kind,
                "subject": subject, "payload": payload or {}, "prev": "x", "hash": "y"}
        if code:
            line["why"] = why.why(code, **slots)
        self.lines.append(line)
        return line["seq"]

    def observed(self, key, addr, name, thread=None):
        self.add("observed", key, {"from_addr": addr, "from_name": name, "thread": thread or key})

    def model_call(self, key, cost):
        seq = self.add("called", key, {"call": "model", "target": "claude-sonnet-5-5"})
        self.add("result", key, {"call_seq": seq, "ok": True, "cost_usd": cost})


def demo_day():
    r = Receipts()
    r.observed("1", "priya@northwind.example", "Priya Raman", "t-priya")
    r.model_call("1", 0.003)
    r.add("decided", "1", {"thread": "t-priya"}, "policy.book_clear_time")
    r.observed("2", "marcus@brightlane.example", "Marcus Lee", "t-marcus")
    r.model_call("2", 0.003)
    r.add("decided", "2", {"thread": "t-marcus"}, "policy.offer_no_clear_time", cond="B5")
    r.observed("3", "dana@fernhill.example", "Dana Okafor", "t-dana")
    r.model_call("3", 0.003)
    r.add("decided", "3", {}, "policy.offer_conflict", rule="overlap")
    r.observed("4", "dana@fernhill.example", "Dana Okafor", "t-dana")
    r.add("decided", "4", {}, "screen.no_reply_kind", rule="auto")
    r.observed("5", "news@opsweekly.example", "Ops Weekly")
    r.add("decided", "5", {}, "screen.no_reply_kind", rule="list")
    r.observed("6", "jordan@quarry.example", "Jordan Pike")
    r.add("decided", "6", {"dropped": 0}, "screen.stop", n=0)
    r.observed("7", "marcus@brightlane.example", "Marcus Lee", "t-marcus")
    r.model_call("7", 0.003)
    r.add("decided", "7", {}, "policy.pick_plus_question")
    r.observed("8", "priya@northwind.example", "Priya Raman", "t-priya")
    r.model_call("8", 0.003)
    r.add("decided", "8", {}, "policy.move_cancel_offer")
    r.observed("8a", "sam@ridgeway.example", "Sam Rivera", "t-sam")
    r.model_call("8a", 0.003)
    r.add("decided", "8a", {}, "policy.book_clear_time")
    r.add("decided", "8a", {}, "policy.collision_reoffer")
    r.observed("9", "ken@tallyhq.example", "Ken Ito")
    r.model_call("9", 0.003)
    r.add("decided", "9", {}, "policy.file_injection")
    r.observed("10", "", "")
    r.add("decided", "10", {}, "screen.quarantine")
    r.observed("11", "elena@harborline.example", "Elena Voss", "t-elena")
    r.model_call("11", 0.003)
    r.add("held", "11", {}, "hold.no_model", ts="2026-12-01T21:10:00Z")
    r.observed("12", "marcus@brightlane.example", "Marcus Lee", "t-marcus")
    r.model_call("12", 0.003)
    r.add("held", "12", {}, "hold.calendar_down", ts="2026-12-01T22:20:00Z")
    r.observed("13a", "dana@fernhill.example", "Dana Okafor", "t-dana")
    r.observed("13b", "dana@fernhill.example", "Dana Okafor", "t-dana")
    r.add("decided", "13b", {"dropped": 1}, "screen.stop", n=1)
    return r


class BuildTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(templates, "ZONES", FakeZones())
        p.start()
        self.addCleanup(p.stop)

    def test_demo_day_counts(self):
        s = digest.build(demo_day().lines, NOW, verified=True)
        self.assertEqual((s["handled"], s["held"], s["approvals"]), (13, 2, 0))
        self.assertEqual(s["offered_to"], ["Priya Raman", "Sam Rivera"])
        self.assertEqual(s["opted_out"], [("jordan@quarry.example", 0), ("dana@fernhill.example", 1)])
        self.assertEqual(s["suspicious"], ["Ken Ito"])
        self.assertEqual(s["quarantined"], 1)
        self.assertEqual(s["model_calls"], 9)
        self.assertAlmostEqual(s["cost_usd"], 0.027)
        self.assertEqual([h["name"] for h in s["holds"]], ["Elena Voss", "Marcus Lee"])
        self.assertEqual(s["holds"][0]["at"], utc(2026, 12, 1, 21, 10))
        self.assertTrue(s["verified"])
        body = templates.render("S2", s)
        self.assertTrue(body.startswith("Handled 13, held 2, human approvals 0. Tue Dec 1\nHeld for you:\n"))
        self.assertTrue(body.endswith("Model calls 9, cost $0.0270. Receipts verify: yes."))

    def test_verify_unknown_reads_no(self):
        self.assertFalse(digest.build([], NOW)["verified"])
        self.assertEqual(templates.render("S2", digest.build([], NOW)),
                         "Handled 0, held 0, human approvals 0. Tue Dec 1\nReceipts verify: no.")

    def test_failed_inbox_reads_are_the_error_lines_of_the_fetch_step(self):
        """Decision 89: each failed read of a run until stopped is one error line, step fetch; other
        error lines are no failed read."""
        r = Receipts()
        for k in (1, 2):
            r.add("error", "", {"step": "fetch", "error": "MailError: imap fetch: timeout", "failed_in_a_row": k,
                                "tries_every_s": 600.0})
        r.add("error", "", {"step": "digest", "error": "MailError: jmap send: 503", "template": "S2"})
        r.observed("1", "a@x.example", "A")
        r.add("error", "1", {"step": "model", "error": "ModelError: timeout"})
        s = digest.build(r.lines, NOW)
        self.assertEqual(s["read_failures"], 2)
        self.assertEqual(templates.render("S2", s).split("\n")[1:], ["Failed inbox reads: 2.", "Receipts verify: no."])
        self.assertEqual(digest.build(demo_day().lines, NOW)["read_failures"], 0)

    def test_human_approvals_are_counted_not_typed(self):
        r = Receipts()
        r.observed("1", "a@x.example", "A")
        r.add("decided", "1", {"human_approval": True}, "policy.file_fyi")
        self.assertEqual(digest.build(r.lines, NOW)["approvals"], 1)

    def test_also_asked_is_the_copied_question_defanged(self):
        r = Receipts()
        r.observed("1", "marcus@brightlane.example", "Marcus Lee")
        r.add("decided", "1", {"question": "Is www.deck.example\nready?"}, "policy.pick_plus_question")
        r.add("decided", "1", {}, "policy.pick_plus_question")
        self.assertEqual(digest.build(r.lines, NOW)["also_asked"], [("Marcus Lee", "Is www[.]deck[.]example ready?")])

    def test_sensitive_and_not_addressed(self):
        r = Receipts()
        r.observed("1", "alerts@bank.example", "Bank")
        r.add("decided", "1", {}, "screen.sensitive_skip")
        r.observed("2", "x@y.example", "X")
        r.add("decided", "2", {}, "screen.not_addressed", rule="cc_only")
        s = digest.build(r.lines, NOW)
        self.assertEqual(s["sensitive"], ["alerts@bank.example"])
        self.assertEqual(s["not_addressed"], 1)
        self.assertEqual(s["handled"], 2)

    def test_hostile_display_name_one_line_defanged(self):
        r = Receipts()
        r.observed("1", "ken@tallyhq.example", "Ken\nvisit evil.example")
        r.add("decided", "1", {}, "policy.file_injection")
        self.assertEqual(digest.build(r.lines, NOW)["suspicious"], ["Ken visit evil[.]example"])

    def test_a_code_in_a_sender_address_or_message_key_never_reaches_the_digest(self):
        # Fable review of PR #4, finding 2: the address fallback, and a hold with no name showed its key.
        from doris import traps
        bad = "agw-canary-7qxz@quill.example.com"
        r = Receipts()
        r.observed("<AGW-CANARY-8VVM@x.example>", bad, "")
        r.add("held", "<AGW-CANARY-8VVM@x.example>", {}, "hold.no_model", ts="2026-12-01T21:10:00Z")
        r.add("held", "<AGW-CANARY-K4JJ@x.example>", {}, "hold.no_model", ts="2026-12-01T21:20:00Z")
        r.observed("2", bad, "")
        r.add("decided", "2", {}, "policy.file_injection")
        r.observed("3", bad, "")
        r.add("decided", "3", {"dropped": 0}, "screen.stop", n=0)
        r.observed("4", bad, "")
        r.add("decided", "4", {}, "screen.sensitive_skip")
        r.observed("5", bad, "")
        r.add("decided", "5", {"question": "Can you send it?"}, "policy.pick_plus_question")
        r.observed("6", bad, "", "t-6")
        r.add("decided", "6", {"thread": "t-6"}, "policy.offer_no_clear_time", cond="B5")
        s = digest.build(r.lines, NOW, verified=True)
        shown = "[redacted]@quill.example.com"
        self.assertEqual([h["name"] for h in s["holds"]], [shown, "(sender unknown)"])
        self.assertEqual((s["suspicious"], s["opted_out"], s["sensitive"], s["offered_to"]),
                         ([shown], [(shown, 0)], [shown], [shown]))
        self.assertEqual(s["also_asked"], [(shown, "Can you send it?")])
        body = templates.render("S2", s)
        self.assertEqual(traps.find_tokens(body), [])

    def test_a_code_in_capitals_reaches_no_line_of_the_digest(self):
        # Second Fable review of PR #4, H1: the name, the address and the "Also asked" line.
        from doris import traps
        from tests.unit.test_holds import CAPS_TOKENS
        for token in CAPS_TOKENS:
            addr = "billing+%s@evil.example" % token
            for name in ("", "Ann ref=" + token):
                r = Receipts()
                r.observed("1", addr, name)
                r.add("held", "1", {}, "hold.no_model", ts="2026-12-01T21:10:00Z")
                r.observed("2", addr, name)
                r.add("decided", "2", {"dropped": 1}, "screen.stop", n=1)
                r.observed("3", addr, name)
                r.add("decided", "3", {}, "screen.sensitive_skip")
                r.observed("4", addr, name)
                r.add("decided", "4", {"question": "Also is x/%s still ok?" % token}, "policy.pick_plus_question")
                r.observed("5", addr, name)
                r.add("decided", "5", {}, "policy.file_injection")
                r.observed("6", addr, name, "t-6")
                r.add("decided", "6", {"thread": "t-6"}, "policy.offer_no_clear_time", cond="B5")
                body = templates.render("S2", digest.build(r.lines, NOW, verified=True))
                self.assertIn("Also asked: ", body)
                self.assertEqual(traps.find_tokens(body), [], body)

    def test_every_address_the_reader_takes_reaches_the_digest(self):
        # Second Fable review of PR #4, M1: one such sender lost the whole digest.
        from tests.unit.test_holds import ODD_ADDRESSES, read_from
        for addr in ODD_ADDRESSES:
            got = read_from(addr)
            for name in ("", "Ann Lee"):
                r = Receipts()
                r.observed("1", "ok@fine.example", "Ok Fine")
                r.add("held", "1", {}, "hold.no_model", ts="2026-12-01T21:10:00Z")
                r.observed("2", got, name)
                r.add("held", "2", {}, "hold.no_model", ts="2026-12-01T21:20:00Z")
                r.observed("3", got, name)
                r.add("decided", "3", {"dropped": 1}, "screen.stop", n=1)
                r.observed("4", got, name)
                r.add("decided", "4", {}, "screen.sensitive_skip")
                r.observed("5", got, name)
                r.add("decided", "5", {"question": "Is parking free?"}, "policy.pick_plus_question")
                r.observed("6", got, name)
                r.add("decided", "6", {}, "policy.file_injection")
                r.observed("7", got, name, "t-7")
                r.add("decided", "7", {"thread": "t-7"}, "policy.offer_no_clear_time", cond="B5")
                body = templates.render("S2", digest.build(r.lines, NOW, verified=True))
                self.assertIn("Opted out: [redacted] (1 queued request dropped)", body, addr)
                self.assertIn("Sensitive mail skipped: [redacted]", body, addr)
                self.assertIn("Ok Fine, ", body)

    def test_held_thread_counted_but_sends_no_line(self):
        r = Receipts()
        r.observed("1", "a@x.example", "A")
        r.add("held", "1", {}, "hold.thread_held")
        s = digest.build(r.lines, NOW)
        self.assertEqual((s["held"], s["holds"]), (1, []))

    def test_a_model_call_with_no_result_costs_nothing_yet(self):
        r = Receipts()
        r.add("called", "1", {"call": "model", "target": "m"})
        s = digest.build(r.lines, NOW)
        self.assertEqual((s["model_calls"], s["cost_usd"]), (1, 0.0))

    def test_partial_lines_do_not_crash(self):
        s = digest.build([{"kind": "observed"}, {"kind": "held", "subject": "k", "why": None}, {}], NOW)
        self.assertEqual(s["held"], 1)


if __name__ == "__main__":
    unittest.main()
