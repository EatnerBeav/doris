"""demo_sandbox.copy_committed, the copy every demo-day test runs on, from a sandbox a judge has used.

A judge who follows sandbox/README.md drops their own .eml into sandbox/inbox and adds a line for it to
inbox/facts.json; a demo leaves her sends, her booking and a staged event behind. The demo-day tests
must still run on the committed fixtures only: the demo's fifteen emails and their facts, the owner's
calendar, an empty outbox.

All of that is planted in a temporary copy of the sandbox, never in the repository's own.
"""

import json
import os
import shutil
import tempfile
import unittest

from tests.unit import demo_sandbox
from tests.unit.test_fixtures import NAMES

# A made-up sender and text, written the way sandbox/README.md asks: to the owner alone, its own
# Message-ID, a Date between the demo's last email (3:06 PM Pacific) and the 5:00 PM digest.
JUDGE_EML = (
    "From: Robin Vale <robin@judges-own.example>\r\n"
    "To: whizz@fastmail.com\r\n"
    "Subject: A walk-through for our team\r\n"
    "Date: Tue, 01 Dec 2026 15:40:00 -0800\r\n"
    "Message-ID: <judge.walkthrough.robin@judges-own.example>\r\n"
    "MIME-Version: 1.0\r\n"
    "Content-Type: text/plain; charset=\"utf-8\"\r\n"
    "Content-Transfer-Encoding: 7bit\r\n"
    "\r\n"
    "Could Shane show our team how this works, Thursday December 3 at 11:00 AM Pacific?\r\n"
    "\r\n"
    "Robin\r\n").encode("ascii")
JUDGE_NAME = "14-robin.eml"       # sorts after the demo's fifteen, like README's 14-yourname.eml


def write(path, data):
    with open(path, "wb") as fh:
        fh.write(data)


class ACopyOfASandboxAJudgeHasUsed(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="doris-copy-")
        self.addCleanup(shutil.rmtree, tmp, True)
        self.used = os.path.join(tmp, "used")
        shutil.copytree(demo_sandbox.SANDBOX, self.used)
        inbox = os.path.join(self.used, "inbox")
        write(os.path.join(inbox, JUDGE_NAME), JUDGE_EML)
        with open(os.path.join(inbox, "facts.json"), encoding="utf-8") as fh:
            facts = json.load(fh)
        facts[JUDGE_NAME] = {"forwarded": "yes"}
        with open(os.path.join(inbox, "facts.json"), "w", encoding="utf-8") as fh:
            json.dump(facts, fh, indent=2)
        calendar, outbox = os.path.join(self.used, "calendar"), os.path.join(self.used, "outbox")
        write(os.path.join(calendar, "doris-000000000000000000000000.ics"), b"BEGIN:VCALENDAR\r\n")
        write(os.path.join(calendar, "staged-lands-between-1.ics"), b"BEGIN:VCALENDAR\r\n")
        write(os.path.join(outbox, "001-T1.eml"), b"To: priya@northwind.example\r\n")
        self.copy = demo_sandbox.copy_committed(os.path.join(tmp, "copy"), source=self.used)

    def test_the_inbox_holds_the_demos_fifteen_emails_and_their_facts_only(self):
        self.assertIn(JUDGE_NAME, os.listdir(os.path.join(self.used, "inbox")))
        inbox = os.path.join(self.copy, "inbox")
        self.assertEqual(sorted(n for n in os.listdir(inbox) if n.endswith(".eml")), NAMES)
        with open(os.path.join(inbox, "facts.json"), encoding="utf-8") as fh:
            self.assertEqual(sorted(json.load(fh)), NAMES)

    def test_no_booking_staged_event_or_send_of_a_run(self):
        self.assertEqual(sorted(os.listdir(os.path.join(self.copy, "calendar"))),
                         demo_sandbox.owner_ics_files(os.path.join(demo_sandbox.SANDBOX, "calendar")))
        self.assertEqual(os.listdir(os.path.join(self.copy, "outbox")), [".gitkeep"])


if __name__ == "__main__":
    unittest.main()
