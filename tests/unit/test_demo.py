"""The skeleton's done-when test: the demo day runs end to end on the sandbox.

Runs the real loop, in this process, on a fresh copy of sandbox/ with the stub classifier,
and checks the outcome demo-day.md fixes: handled 13, held 2, human approvals 0; six sends
to prospects, each with the owner's blind copy; two hold notices and the digest to the
owner; three .ics files; a chain that verifies; a why on every decision.
"""

import email
import email.policy
import os
import shutil
import tempfile
import unittest

from doris import loop, paths, receipts, why

OWNER = "whizz@fastmail.com"
DORIS = "doris.htm@fastmail.com"


class DemoDay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="doris-demo-")
        cls.sandbox = os.path.join(cls.tmp, "sandbox")
        shutil.copytree(paths.SANDBOX, cls.sandbox,
                        ignore=shutil.ignore_patterns("doris-*.ics", "*.eml.sent", "keywords.json"))
        outbox = os.path.join(cls.sandbox, "outbox")
        for name in os.listdir(outbox):
            if name != ".gitkeep":
                os.remove(os.path.join(outbox, name))
        cfg = loop.RunConfig(mode="throwaway", run_name="model-stub-sandbox-test", state_dir=os.path.join(cls.tmp, "state"),
                             sandbox_root=cls.sandbox, owner_addr=OWNER, doris_addr=DORIS, model_source="stub",
                             clock_mode="advance", digest=True)
        cls.summary = loop.run(cfg)
        cls.lines = receipts.read_lines(cls.summary.receipts_path)
        cls.sent = []
        for name in sorted(os.listdir(outbox)):
            if name.endswith(".eml"):
                with open(os.path.join(outbox, name), "rb") as fh:
                    cls.sent.append(email.message_from_bytes(fh.read(), policy=email.policy.default))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_the_ratio(self):
        s = self.summary
        self.assertEqual((s.handled, s.held, s.human_approvals), (13, 2, 0))

    def test_six_sends_to_prospects_each_with_the_owners_blind_copy(self):
        prospect = [m for m in self.sent if OWNER not in str(m["To"])]
        self.assertEqual(len(prospect), 6, [str(m["To"]) for m in self.sent])
        for m in prospect:
            self.assertEqual(m["Auto-Submitted"], "auto-replied")

    def test_two_hold_notices_and_the_digest_go_to_the_owner_only(self):
        owner = [m for m in self.sent if str(m["To"]).strip().lower() == OWNER]
        self.assertEqual(len(owner), 3)

    def test_three_ics_files(self):
        n = sum(1 for m in self.sent for part in m.walk() if part.get_content_type() == "text/calendar")
        self.assertEqual(n, 3)

    def test_the_chain_verifies(self):
        self.assertTrue(receipts.verify(self.summary.receipts_path).ok)

    def test_every_decision_carries_a_listed_why(self):
        for line in self.lines:
            if line["kind"] in ("decided", "checked", "held", "halted"):
                self.assertIn("why", line, line)
                self.assertIn(line["why"]["code"], why.CODES, line)

    def test_every_called_line_has_its_result(self):
        called = {l["seq"] for l in self.lines if l["kind"] == "called"}
        answered = {l["payload"].get("call_seq") for l in self.lines if l["kind"] == "result"}
        self.assertEqual(called - answered, set())

    def test_nothing_is_ever_sent_to_an_address_from_a_body(self):
        for m in self.sent:
            for hdr in ("To", "Cc", "Bcc"):
                self.assertNotIn("tallyhq.example", str(m.get(hdr, "")).replace("ken@tallyhq.example", ""))
                self.assertNotIn("ana@brightlane.example", str(m.get(hdr, "")))


if __name__ == "__main__":
    unittest.main()
