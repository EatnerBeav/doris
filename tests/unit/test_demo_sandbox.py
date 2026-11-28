"""demo_sandbox.copy_committed, the copy every demo-day test runs on, with a judge's own mail in sandbox/.

A judge who follows sandbox/README.md drops their own .eml into sandbox/inbox and adds a line for it to
inbox/facts.json, then may run the tests. The demo-day tests must still run on the committed fixtures
only, so the copy holds the demo's fifteen emails and their facts and nothing of the judge's.

The judge's mail is planted in the repository's own sandbox/inbox, as a judge would, under a name no
real file can have; when the test ends it is removed and facts.json is put back byte for byte.
"""

import json
import os
import shutil
import tempfile
import unittest

from doris import paths
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


class AJudgesOwnMailInTheSandbox(unittest.TestCase):

    def setUp(self):
        inbox = os.path.join(paths.SANDBOX, "inbox")
        facts_path = os.path.join(inbox, "facts.json")
        with open(facts_path, "rb") as fh:
            facts_raw = fh.read()

        def put_facts_back():
            with open(facts_path, "wb") as fh:
                fh.write(facts_raw)
        self.addCleanup(put_facts_back)

        # sorts after the demo's fifteen, like README's 14-yourname.eml, and is never a real file's name
        fd, path = tempfile.mkstemp(prefix="99-judge-", suffix=".eml", dir=inbox)
        self.addCleanup(os.remove, path)
        with os.fdopen(fd, "wb") as fh:
            fh.write(JUDGE_EML)
        self.planted = os.path.basename(path)
        facts = json.loads(facts_raw.decode("utf-8"))
        facts[self.planted] = {"forwarded": "yes"}
        with open(facts_path, "w", encoding="utf-8") as fh:
            json.dump(facts, fh, indent=2)

        self.tmp = tempfile.mkdtemp(prefix="doris-copy-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_the_copy_holds_the_demos_fifteen_emails_and_their_facts_only(self):
        self.assertIn(self.planted, os.listdir(os.path.join(paths.SANDBOX, "inbox")))
        copy = demo_sandbox.copy_committed(paths.SANDBOX, os.path.join(self.tmp, "sandbox"))
        inbox = os.path.join(copy, "inbox")
        self.assertEqual(sorted(n for n in os.listdir(inbox) if n.endswith(".eml")), NAMES)
        with open(os.path.join(inbox, "facts.json"), encoding="utf-8") as fh:
            self.assertEqual(sorted(json.load(fh)), NAMES)


if __name__ == "__main__":
    unittest.main()
