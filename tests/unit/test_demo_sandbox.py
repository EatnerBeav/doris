"""demo_sandbox.copy_committed, the copy every demo-day test runs on, from a sandbox a judge has used.

A judge who follows sandbox/README.md drops their own .eml into sandbox/inbox and adds a line for it to
inbox/facts.json; a demo leaves her sends, her booking and a staged event behind. The demo-day tests
must still run on the committed fixtures only: the demo's fifteen emails and their facts, the owner's
calendar, an empty outbox. And the committed fixtures are exactly what git tracks under sandbox/.

All of that is planted in a temporary copy of the sandbox, never in the repository's own.
"""

import codecs
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from doris.adapters import sandbox as sandbox_adapter
from tests.unit import demo_sandbox, test_fixtures
from tests.unit.test_fixtures import DORIS, NAMES

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


def read(path):
    with open(path, "rb") as fh:
        return fh.read()


def generators_facts():
    """inbox/facts.json as tests/tools/build_demo_fixtures.py writes it."""
    spec = importlib.util.spec_from_file_location("build_demo_fixtures", demo_sandbox.GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with tempfile.TemporaryDirectory() as tmp:
        module.build(tmp)
        return read(os.path.join(tmp, "inbox", "facts.json"))


class ACopyOfASandboxAJudgeHasUsed(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="doris-copy-")
        self.addCleanup(shutil.rmtree, tmp, True)
        self.used = demo_sandbox.copy_committed(os.path.join(tmp, "used"))
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
        self.assertEqual(demo_sandbox.mail_names(inbox), NAMES)
        with open(os.path.join(inbox, "facts.json"), encoding="utf-8") as fh:
            self.assertEqual(sorted(json.load(fh)), NAMES)

    def test_no_booking_staged_event_or_send_of_a_run(self):
        self.assertEqual(sorted(os.listdir(os.path.join(self.copy, "calendar"))),
                         demo_sandbox.owner_ics_files(os.path.join(demo_sandbox.SANDBOX, "calendar")))
        self.assertEqual(os.listdir(os.path.join(self.copy, "outbox")), [".gitkeep"])


class TheTestsListFilesAsTheSandboxAdapterDoes(unittest.TestCase):
    """A judge's 14-Robin.EML is mail to Doris (the adapter takes .eml in any case, files only), so it is
    mail to the tests; the same for an owner's event saved as .ICS. Checked against the adapter's own
    listing of the same folders, so the two cannot drift apart."""

    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="doris-names-")
        self.addCleanup(shutil.rmtree, tmp, True)
        self.root = demo_sandbox.copy_committed(os.path.join(tmp, "sandbox"))
        self.inbox, self.calendar = os.path.join(self.root, "inbox"), os.path.join(self.root, "calendar")
        for name in ("14-Robin.EML", "15-sam.Eml", "16-notes.txt"):
            write(os.path.join(self.inbox, name), JUDGE_EML)
        os.mkdir(os.path.join(self.inbox, "17-saved.eml"))
        for name in ("Owner-Lunch.ICS", "doris-000000000000000000000000.ics", "staged-lands-between-1.ics",
                     "notes.txt"):
            write(os.path.join(self.calendar, name), b"BEGIN:VCALENDAR\r\n")
        os.mkdir(os.path.join(self.calendar, "archive.ics"))

    def test_mail(self):
        read = [item.server_id for item in sandbox_adapter.SandboxMail(self.root, DORIS).fetch_new()]
        self.assertEqual(read, NAMES + ["14-Robin.EML", "15-sam.Eml"])
        self.assertEqual(test_fixtures.judges_emails(self.inbox), read[len(NAMES):])
        self.assertEqual(demo_sandbox.mail_names(self.inbox), read)

    def test_calendar(self):
        listed = sandbox_adapter.SandboxCalendar(self.root)._names()     # the files query() reads
        self.assertIn("Owner-Lunch.ICS", listed)
        self.assertEqual(demo_sandbox.owner_ics_files(self.calendar),
                         [n for n in listed if not demo_sandbox.is_run_event(n)])


class ACopyOfASandboxWhoseFactsDoNotParse(unittest.TestCase):
    """A facts.json an editor broke (Notepad's UTF-8 byte order mark, a trailing comma) is the committed
    file's fault, which test_fixtures reports; every copy still gets the generator's facts.json, so the
    demo-day tests run as before."""

    def test_the_copy_holds_the_generators_facts_json(self):
        want = generators_facts()
        for label, broken in (("byte order mark", codecs.BOM_UTF8 + want),
                              ("trailing comma", want.rstrip()[:-1].rstrip() + b",\n}\n")):
            with self.subTest(label):
                tmp = tempfile.mkdtemp(prefix="doris-copy-")
                self.addCleanup(shutil.rmtree, tmp, True)
                used = demo_sandbox.copy_committed(os.path.join(tmp, "used"))
                write(os.path.join(used, "inbox", "facts.json"), broken)
                copy = demo_sandbox.copy_committed(os.path.join(tmp, "copy"), source=used)
                self.assertEqual(read(os.path.join(copy, "inbox", "facts.json")), want)


def git(*args):
    # No line-ending conversion (Git for Windows turns it on), as the repository's `* -text` has it.
    # And never git's own GIT_DIR and the like from a hook: only the repository named with -C.
    return subprocess.run(["git", "-c", "core.autocrlf=false"] + list(args), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, check=True, env=demo_sandbox.git_env())


class GitTracksTheCommittedSandboxAndNothingElse(unittest.TestCase):
    """Every test copies only the files demo_sandbox says are committed, so a file committed under
    sandbox/ beside them (a stray .eml, a judge's mail added by mistake) would reach every clone with
    no test reading it. Here git and demo_sandbox must agree. Outside a git checkout (a `git archive`
    copy) the check stands aside; CI runs it where it cannot: `python -m tests.unit.demo_sandbox`."""

    def test_git_ls_files_sandbox_is_the_committed_set(self):
        tracked = demo_sandbox.git_sandbox_files()
        if tracked is None:
            self.skipTest("git cannot list sandbox/ here: no git, or not the top of a git checkout")
        problem = demo_sandbox.sandbox_drift(tracked)
        if problem:
            self.fail(problem)

    def test_a_stray_file_is_named_and_a_copy_in_another_checkout_stands_aside(self):
        if shutil.which("git") is None:
            self.skipTest("no git")
        tmp = tempfile.mkdtemp(prefix="doris-git-")
        self.addCleanup(shutil.rmtree, tmp, True)
        git("init", "-q", tmp)
        demo_sandbox.copy_committed(os.path.join(tmp, "sandbox"))
        write(os.path.join(tmp, "sandbox", "inbox", JUDGE_NAME), JUDGE_EML)
        git("-C", tmp, "add", "sandbox")
        self.assertEqual(demo_sandbox.sandbox_drift(demo_sandbox.git_sandbox_files(tmp)),
                         "committed under sandbox/ but not in demo_sandbox.committed_files(), so no test "
                         "reads it: inbox/" + JUDGE_NAME)
        git("-C", tmp, "rm", "-q", "--cached", "sandbox/clock.txt")
        self.assertIn("in demo_sandbox.committed_files() but not committed: clock.txt",
                      demo_sandbox.sandbox_drift(demo_sandbox.git_sandbox_files(tmp)))
        # An archive copy unpacked inside some other checkout: git answers for that checkout, not this.
        copy = demo_sandbox.copy_committed(os.path.join(tmp, "unpacked", "sandbox"))
        self.assertIsNone(demo_sandbox.git_sandbox_files(os.path.dirname(copy)))

    def test_gits_own_variables_never_point_the_check_at_another_repository(self):
        # A pre-commit hook or a linked worktree runs the suite with GIT_DIR, GIT_INDEX_FILE and the
        # like set for the repository being committed. The check and this test's own git calls must
        # still answer for, and write only to, the repository they name.
        if shutil.which("git") is None:
            self.skipTest("no git")
        tmp = tempfile.mkdtemp(prefix="doris-git-")
        self.addCleanup(shutil.rmtree, tmp, True)
        ours, other = os.path.join(tmp, "ours"), os.path.join(tmp, "other")
        for repo in (ours, other):
            git("init", "-q", repo)
            demo_sandbox.copy_committed(os.path.join(repo, "sandbox"))
        git("-C", other, "add", "sandbox")
        before = read(os.path.join(other, ".git", "index"))
        hostile = {"GIT_DIR": os.path.join(other, ".git"), "GIT_WORK_TREE": other,
                   "GIT_INDEX_FILE": os.path.join(other, ".git", "index"), "GIT_PREFIX": "",
                   "GIT_OBJECT_DIRECTORY": os.path.join(other, ".git", "objects"),
                   "GIT_COMMON_DIR": os.path.join(other, ".git")}
        with mock.patch.dict(os.environ, hostile):
            write(os.path.join(ours, "sandbox", "inbox", JUDGE_NAME), JUDGE_EML)
            git("-C", ours, "add", "sandbox")
            listed = demo_sandbox.git_sandbox_files(ours)
        self.assertEqual(read(os.path.join(other, ".git", "index")), before)
        self.assertIn("inbox/" + JUDGE_NAME, listed)
        self.assertIn("reads it: inbox/" + JUDGE_NAME, demo_sandbox.sandbox_drift(listed))


if __name__ == "__main__":
    unittest.main()
