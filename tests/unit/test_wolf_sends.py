"""tests/tools/wolf_sends.py: one plain message from the wolf.whizz test account, over JMAP.

The tool's real JmapSender talks to fake_jmap's server for wolf's account on 127.0.0.1, reached
as Fastmail is reached: api.fastmail.com for the session, then the host it names
(phl.api.fastmail.com), through the network guard the tool installs itself (fake_jmap.dialling).
The env file is a throwaway one of each test's own; every value in it is made up.
"""
import base64
import contextlib
import datetime as dt
import email
import email.policy
import email.utils
import importlib.util
import io
import os
import re
import shutil
import tempfile
import unittest
from unittest import mock

from doris import netguard
from tests.unit import fake_jmap

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOL = os.path.join(REPO, "tests", "tools", "wolf_sends.py")

WOLF = "wolf.whizz@fastmail.com"
WOLF_TOKEN = "dummy-wolf-jmap-token-for-wolf-sends-tests-7171"
OWNER = "whizz@fastmail.com"
DORIS = fake_jmap.DORIS
HER_TOKEN = fake_jmap.TOKEN                     # her own token sits in the same file: the tool never uses it
SETTINGS = (("DORIS_MAIL_ADDRESS", DORIS), ("DORIS_JMAP_TOKEN", HER_TOKEN), ("DORIS_OWNER_ADDRESS", OWNER),
            ("WOLF_EMAIL", WOLF), ("WOLF_JMAP_TOKEN", WOLF_TOKEN))
VALUES = (WOLF_TOKEN, HER_TOKEN, WOLF, OWNER, DORIS)
SUBJECT = "Meeting about Hire the Machines"
BODY = ("Could we meet on Wednesday, December 2 at 10:00 AM Pacific for 30 minutes to talk about "
        "Hire the Machines? Wolf")
HER_MID = "<doris-0123456789abcdef01234567@fastmail.com>"     # one of her messages, as --in-reply-to names it
MID = re.compile(r"<[0-9a-f]{32}@fastmail\.com>\Z")          # a fresh Message-ID on wolf's own domain


def load_tool():
    spec = importlib.util.spec_from_file_location("wolf_sends", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WolfSendsTest(unittest.TestCase):

    def setUp(self):
        self.tool = load_tool()
        self.wolf = fake_jmap.for_account(WOLF, WOLF_TOKEN)
        self.addCleanup(self.wolf.stop)
        self.dir = tempfile.mkdtemp(prefix="doris-wolf-sends-test-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.env_path = self.write_env()
        self.lines = []
        for patch in fake_jmap.dialling(self.wolf):
            patch.start()
            self.addCleanup(patch.stop)

    def write_env(self, **changes):
        """The env file, with a setting changed (a value) or left out (None)."""
        lines = ["# a test env file: every value is made up"]
        for name, value in SETTINGS:
            value = changes.get(name, value)
            if value is not None:
                lines.append("%s=%s" % (name, value))
        path = os.path.join(self.dir, "test.env")
        with open(path, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        return path

    def run_tool(self, *argv, env=None):
        args = ["--env", env or self.env_path]
        if "--subject" not in argv:
            args += ["--subject", SUBJECT]
        if "--body" not in argv and "--body-file" not in argv:
            args += ["--body", BODY]
        with contextlib.redirect_stderr(io.StringIO()) as err:
            code = self.tool.main(args + list(argv), out=self.lines.append)
        self.stderr = err.getvalue()
        return code

    def delivered(self):
        """The one message wolf's account submitted: (envelope sender, recipients, parsed message)."""
        self.assertEqual(len(self.wolf.delivered), 1)
        sender, rcpts, raw = self.wolf.delivered[0]
        return sender, rcpts, email.message_from_bytes(raw, policy=email.policy.default)

    def printed(self):
        return "\n".join(self.lines) + self.stderr

    def assert_no_value(self):
        text = self.printed()
        for value in VALUES + (base64.b64encode(WOLF_TOKEN.encode()).decode(),):
            self.assertNotIn(value, text)

    def assert_nothing_contacted(self):
        self.assertEqual(self.wolf.requests, [])
        self.assertEqual(self.wolf.delivered, [])

    # -- what it sends -------------------------------------------------------------------------
    def test_it_sends_one_plain_message_from_wolf_to_the_owner(self):
        before = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        self.assertEqual(self.run_tool(), 0)
        sender, rcpts, msg = self.delivered()
        self.assertEqual((sender, rcpts), (WOLF, [OWNER]))
        self.assertEqual((msg["From"], msg["To"], msg["Subject"]), (WOLF, OWNER, SUBJECT))
        self.assertEqual(msg.get_content_type(), "text/plain")
        self.assertEqual(msg.get_content().strip(), BODY)
        self.assertIsNone(msg["Auto-Submitted"])                       # a person wrote it
        self.assertIsNone(msg["In-Reply-To"])
        self.assertIsNone(msg["References"])
        self.assertIsNone(msg["Bcc"])
        mid = msg["Message-ID"]
        self.assertRegex(mid, MID)
        self.assertNotIn("doris-smoke-test", mid.lower())             # a live run reads it like a prospect's
        sent_at = email.utils.parsedate_to_datetime(msg["Date"])
        self.assertTrue(before <= sent_at <= dt.datetime.now(dt.timezone.utc), msg["Date"])
        self.assertEqual(self.wolf.kinds().count("send"), 1)
        self.assertEqual({r[3]["authorization"] for r in self.wolf.requests}, {"Bearer " + WOLF_TOKEN})
        (line,) = self.lines
        self.assertIn(mid, line)
        self.assertIn("Sent copy found", line)
        self.assert_no_value()

    def test_each_send_gets_a_fresh_message_id(self):
        self.assertEqual(self.run_tool(), 0)
        self.assertEqual(self.run_tool(), 0)
        mids = [email.message_from_bytes(raw, policy=email.policy.default)["Message-ID"]
                for _, _, raw in self.wolf.delivered]
        self.assertEqual(len(mids), 2)
        self.assertNotEqual(mids[0], mids[1])

    def test_the_body_can_come_from_a_file(self):
        path = os.path.join(self.dir, "body.txt")
        text = "Hi Shane,\n\nCould we meet on Wednesday, December 2 at 10:00 AM Pacific?\n\nWolf"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        self.assertEqual(self.run_tool("--body-file", path), 0)
        _, _, msg = self.delivered()
        self.assertEqual(msg.get_content().strip().splitlines(), text.splitlines())

    def test_in_reply_to_answers_doris_on_her_thread(self):
        self.assertEqual(self.run_tool("--subject", "Re: " + SUBJECT, "--in-reply-to", HER_MID), 0)
        sender, rcpts, msg = self.delivered()
        self.assertEqual((sender, rcpts), (WOLF, [DORIS]))
        self.assertEqual((msg["From"], msg["To"], msg["Subject"]), (WOLF, DORIS, "Re: " + SUBJECT))
        self.assertEqual((msg["In-Reply-To"], msg["References"]), (HER_MID, HER_MID))
        self.assertIsNone(msg["Auto-Submitted"])
        self.assertRegex(msg["Message-ID"], MID)
        self.assertNotEqual(msg["Message-ID"], HER_MID)
        self.assertEqual(len(self.lines), 1)
        self.assert_no_value()

    def test_in_reply_to_takes_an_id_without_its_angle_brackets(self):
        self.assertEqual(self.run_tool("--in-reply-to", HER_MID.strip("<>")), 0)
        _, _, msg = self.delivered()
        self.assertEqual((msg["In-Reply-To"], msg["References"]), (HER_MID, HER_MID))

    def test_the_guard_allows_api_fastmail_com_then_the_host_its_session_names(self):
        installed = []
        real_install = netguard.install

        def spy(hosts=()):
            installed.append((list(hosts), real_install(hosts)))
            return installed[-1][1]
        with mock.patch.object(netguard, "install", spy):
            self.assertEqual(self.run_tool(), 0)
        ((hosts, guard),) = installed
        self.assertEqual(hosts, ["api.fastmail.com"])
        self.assertEqual(guard.admitted, {"phl.api.fastmail.com"})
        self.assertEqual(guard.refused(), [])
        dialled = {(a["host"], a["port"]) for a in guard.attempts if a["call"] == "create_connection"}
        self.assertEqual(dialled, {("api.fastmail.com", 443), ("phl.api.fastmail.com", 443)})
        self.assertIsNone(netguard.active())                            # uninstalled once it is done

    def test_a_session_naming_a_host_outside_api_fastmail_com_sends_nothing(self):
        self.wolf.as_fastmail("https://evil-api.fastmail.com.attacker")
        self.assertEqual(self.run_tool(), 1)
        self.assertEqual(set(self.wolf.kinds()), {"session"})
        self.assertIn("not sent", self.lines[0])
        self.assertIsNone(netguard.active())

    # -- the copy in wolf's Sent ---------------------------------------------------------------
    def test_a_copy_not_found_in_sent_is_said_plainly_and_still_exits_0(self):
        self.wolf.fail_update = "serverFail"            # the submission went; his copy stays in Drafts
        self.assertEqual(self.run_tool(), 0)
        (line,) = self.lines
        self.assertIn("Sent copy not found", line)
        self.assertNotIn("Sent copy found", line)
        self.assertEqual(self.wolf.kinds().count("send"), 1)
        self.assertEqual(self.wolf.kinds().count("find"), 1)            # one look
        self.assert_no_value()

    def test_a_look_that_cannot_tell_is_said_plainly_and_still_exits_0(self):
        self.wolf.status["find"] = 500                  # the look's request turned away; the send went
        self.assertEqual(self.run_tool(), 0)
        (line,) = self.lines
        self.assertIn("could not tell", line)
        self.assertEqual(self.wolf.kinds().count("send"), 1)
        self.assert_no_value()

    # -- what it refuses -----------------------------------------------------------------------
    def test_it_refuses_another_recipient(self):
        cases = (("--to", "someone@example.com"),
                 ("--to", DORIS),                                   # hers only with --in-reply-to
                 ("--to", WOLF),
                 ("--to", OWNER, "--in-reply-to", HER_MID))         # a reply goes to her alone
        for argv in cases:
            with self.subTest(argv=argv):
                self.lines.clear()
                self.assertEqual(self.run_tool(*argv), 2)
                (line,) = self.lines
                self.assertIn("refused --to", line)
                self.assert_nothing_contacted()
                self.assert_no_value()

    def test_to_naming_the_address_it_goes_to_anyway_is_taken(self):
        self.assertEqual(self.run_tool("--to", OWNER.upper()), 0)
        sender, rcpts, _ = self.delivered()
        self.assertEqual(rcpts, [OWNER])

    def test_it_refuses_a_missing_or_broken_token_naming_the_setting_never_a_value(self):
        cases = ((None, "WOLF_JMAP_TOKEN is not set"),
                 ("dummy-short-wolf", "WOLF_JMAP_TOKEN is too short to be an API token"))
        for token, said in cases:
            with self.subTest(said=said):
                self.lines.clear()
                self.assertEqual(self.run_tool(env=self.write_env(WOLF_JMAP_TOKEN=token)), 2)
                (line,) = self.lines
                self.assertIn(said, line)
                self.assertNotIn("dummy-short-wolf", line)
                self.assert_nothing_contacted()
                self.assert_no_value()

    def test_it_refuses_wolf_email_that_is_not_the_test_account_s_own(self):
        cases = ((dict(WOLF_EMAIL=OWNER), "WOLF_EMAIL must be the test account's own address"),
                 (dict(DORIS_MAIL_ADDRESS=WOLF), "WOLF_EMAIL must be the test account's own address"),
                 (dict(WOLF_EMAIL=DORIS), "WOLF_EMAIL must be one of the two test inboxes"),
                 (dict(WOLF_EMAIL="someone@example.com"), "WOLF_EMAIL must be one of the two test inboxes"),
                 (dict(WOLF_EMAIL=None), "WOLF_EMAIL is not set"),
                 (dict(DORIS_OWNER_ADDRESS=None), "DORIS_OWNER_ADDRESS is not set"),
                 (dict(DORIS_MAIL_ADDRESS=None), "DORIS_MAIL_ADDRESS is not set"))
        for changes, said in cases:
            with self.subTest(changes=changes):
                self.lines.clear()
                self.assertEqual(self.run_tool(env=self.write_env(**changes)), 2)
                (line,) = self.lines
                self.assertIn(said, line)
                self.assert_nothing_contacted()
                self.assert_no_value()

    def test_bad_arguments_exit_2_and_send_nothing(self):
        body_file = os.path.join(self.dir, "body.txt")
        with open(body_file, "w") as fh:
            fh.write(BODY)
        cases = (("--subject", ""),
                 ("--subject", "two\nlines"),
                 ("--body", "   "),
                 ("--body", BODY, "--body-file", body_file),
                 ("--body-file", os.path.join(self.dir, "missing.txt")),
                 ("--in-reply-to", "not a message id"),
                 ("--in-reply-to", "<a@b> <c@d>"))
        for argv in cases:
            with self.subTest(argv=argv):
                self.assertEqual(self.run_tool(*argv), 2)
                self.assert_nothing_contacted()
        self.assertEqual(self.run_tool(env=os.path.join(self.dir, "no-such.env")), 2)
        self.assert_nothing_contacted()
        self.assert_no_value()

    # -- a send that did not go, or may have -----------------------------------------------------
    def test_a_refused_send_exits_1_not_sent(self):
        self.wolf.not_created["EmailSubmission/set"] = "forbiddenFrom"
        self.assertEqual(self.run_tool(), 1)
        (line,) = self.lines
        self.assertIn("not sent", line)
        self.assertNotIn("may have gone", line)
        self.assertEqual(self.wolf.delivered, [])
        self.assertNotIn("find", self.wolf.kinds())
        self.assert_no_value()

    def test_an_unknown_send_exits_1_may_have_gone(self):
        self.wolf.fault["send"] = 500                   # the request went in full; the answer is a 5xx
        self.assertEqual(self.run_tool(), 1)
        (line,) = self.lines
        self.assertIn("may have gone", line)
        self.assertNotIn("not sent", line)
        _, _, msg = self.delivered()
        self.assertIn(msg["Message-ID"], line)          # so a person can look for it before sending again
        self.assertEqual(self.wolf.kinds().count("send"), 1)            # never sent again
        self.assert_no_value()

    def test_nothing_printed_contains_the_token(self):
        self.wolf.token = "dummy-other-account-token-for-a-401-answer-0000"    # wolf's token turned away
        self.assertEqual(self.run_tool(), 1)
        self.wolf.token = WOLF_TOKEN
        self.wolf.fault["send"] = "drop"
        self.assertEqual(self.run_tool(), 1)
        self.wolf.fault.clear()
        self.assertEqual(self.run_tool("--in-reply-to", HER_MID), 0)
        self.assertEqual(len(self.lines), 3)
        self.assertNotIn(WOLF_TOKEN, self.printed())
        self.assertNotIn(HER_TOKEN, self.printed())
        self.assert_no_value()


if __name__ == "__main__":
    unittest.main()
