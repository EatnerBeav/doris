"""smoke.main: one pass over every live adapter call, one plain line per step, never a value.

The real adapters talk to test_live's fake IMAP, SMTP and CalDAV servers on 127.0.0.1 (the
adapters' default transports are swapped for plain connections to them), under the network
guard the smoke installs itself: the env file names 127.0.0.1 as every host.
"""
import base64
import datetime as dt
import email
import email.policy
import http.client
import imaplib
import os
import re
import shutil
import smtplib
import tempfile
import unittest
from unittest import mock

from doris import ics, netguard, smoke
from doris.adapters import live
from tests.unit.test_live import COLLECTION, DORIS, PASSWORD, Box, FakeCalDAV, FakeIMAP, FakeSMTP, eml, u

ENV_TEXT = ("# a test env file: every host is the fakes' 127.0.0.1\n"
            "DORIS_MAIL_ADDRESS=" + DORIS + "\n"
            "DORIS_APP_PASSWORD='" + PASSWORD + "'\n"
            "DORIS_IMAP_HOST=127.0.0.1\n"
            "DORIS_SMTP_HOST=127.0.0.1\n"
            "DORIS_CALDAV_URL=https://127.0.0.1" + COLLECTION + "\n")

STEPS = ["imap login and inbox count", "sent folder found by its special-use flag", "smtp login, nothing sent",
         "caldav query, next 7 days", "caldav create test event", "caldav read test event back, times match",
         "caldav delete test event", "caldav read after delete, gone"]
SEND_STEPS = ["smtp send one test message", "sent copy, look 1 (at once)", "sent copy, look 2 (after a short wait)"]
READ_ONLY_IMAP = {"CAPABILITY", "LOGIN", "LOGOUT", "EXAMINE", "LIST", "UID SEARCH", "UID FETCH"}


class SmokeFixture(unittest.TestCase):

    def setUp(self):
        self.imap, self.smtp, self.dav = FakeIMAP(), FakeSMTP(), FakeCalDAV()
        for server in (self.imap, self.smtp, self.dav):
            self.addCleanup(server.stop)
        self.imap.boxes["INBOX"].add(eml(1), u(2026, 11, 27, 9))
        self.imap.boxes["INBOX"].add(eml(2), u(2026, 11, 27, 10))
        self.sent = self.imap.boxes["Sent Items"] = Box(1700000002, special=("\\Sent",))
        self.dir = tempfile.mkdtemp(prefix="doris-smoke-test-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.env_path = self.write_env(ENV_TEXT)
        self.lines = []
        for patch in (
                mock.patch.object(live, "_imap_tls", lambda host, port, timeout: imaplib.IMAP4(
                    "127.0.0.1", self.imap.port, timeout=timeout)),
                mock.patch.object(live, "_smtp_tls", lambda host, port, timeout: smtplib.SMTP(
                    "127.0.0.1", self.smtp.port, local_hostname="[127.0.0.1]", timeout=timeout)),
                mock.patch.object(live, "_https", lambda host, port, timeout: http.client.HTTPConnection(
                    "127.0.0.1", self.dav.port, timeout=timeout)),
                mock.patch.object(smoke, "SENT_LOOK_WAIT_S", 0.0)):
            patch.start()
            self.addCleanup(patch.stop)

    def write_env(self, text):
        path = os.path.join(self.dir, ".env")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def run_smoke(self, *args):
        return smoke.main(["--env", self.env_path] + list(args), out=self.lines.append)

    def result(self, step):
        found = [line[len(step) + 2:] for line in self.lines if line.startswith(step + ": ")]
        self.assertEqual(len(found), 1, "one line for %r in %r" % (step, self.lines))
        return found[0]

    def assert_no_value(self):
        """No line carries an address, the password, a folder name, the calendar or an event."""
        text = "\n".join(self.lines)
        basic = base64.b64encode(("%s:%s" % (DORIS, PASSWORD)).encode()).decode()
        for value in (PASSWORD, basic, DORIS, "doris.htm", "whizz", "Sent Items", COLLECTION, "owner-shared-cal",
                      "127.0.0.1", "doris-"):
            self.assertNotIn(value, text)


class SmokeTest(SmokeFixture):

    def test_every_step_passes_against_working_servers_with_one_plain_line_each(self):
        self.assertEqual(self.run_smoke(), 0)
        self.assertEqual([line.split(": ", 1)[0] for line in self.lines], STEPS + ["smoke"])
        self.assertEqual(self.result("imap login and inbox count"), "ok (2 messages in INBOX)")
        self.assertEqual(self.result("sent folder found by its special-use flag"), "ok (1 folder)")
        self.assertEqual(self.result("caldav query, next 7 days"), "ok (0 events)")
        for step in STEPS:
            self.assertTrue(self.result(step).startswith("ok"), self.lines)
        self.assertEqual(self.result("smoke"), "ok, every step passed")
        self.assert_no_value()

    def test_without_send_to_nothing_is_sent_and_imap_is_only_read(self):
        self.run_smoke()
        self.assertEqual(self.smtp.sessions[0]["auth"], DORIS)
        self.assertFalse({"MAIL", "RCPT", "DATA"} & set(self.smtp.verbs()))
        self.assertLessEqual(set(self.imap.verbs()), READ_ONLY_IMAP)

    def test_the_test_event_is_far_ahead_under_a_new_real_format_booking_id_and_gone_after(self):
        before = dt.datetime.now(dt.timezone.utc)
        self.run_smoke()
        self.run_smoke()
        self.assertEqual(self.dav.methods(), ["REPORT", "PUT", "GET", "GET", "DELETE", "GET"] * 2)
        puts = [r for r in self.dav.requests if r[0] == "PUT"]
        names = [path[len(COLLECTION):-len(".ics")] for _, path, _, _ in puts]
        for name, (_, _, _, body) in zip(names, puts):
            self.assertRegex(name, r"^doris-[0-9a-f]{24}$")
            self.assertTrue(ics.fields_ok(body, booking_id=name))
            self.assertGreater(ics.read_event(body).start_utc, before + dt.timedelta(days=300))
        self.assertNotEqual(names[0], names[1])            # a run cut short leaves nothing in the next one's way
        self.assertEqual(self.dav.resources, {})

    def test_a_failing_step_names_its_class_and_status_the_rest_still_run_and_it_exits_1(self):
        self.dav.status["PUT"] = 403
        self.assertEqual(self.run_smoke(), 1)
        self.assertEqual(self.result("caldav create test event"), "CalendarError (caldav create: 403)")
        self.assertEqual(self.result("caldav read test event back, times match"), "FAILED (not found)")
        self.assertTrue(self.result("smtp login, nothing sent").startswith("ok"))
        self.assertTrue(self.result("caldav read after delete, gone").startswith("ok"))
        self.assertEqual(self.result("smoke"), "FAILED")
        self.assert_no_value()

    def test_an_event_read_back_with_other_times_fails(self):
        real_put = self.dav.put
        self.dav.put = lambda name, data: real_put(name, re.sub(rb"DTSTART:(\d{8})T\d{6}Z", rb"DTSTART:\1T000000Z", data))
        self.assertEqual(self.run_smoke(), 1)
        self.assertEqual(self.result("caldav read test event back, times match"), "FAILED (times differ)")

    def test_a_refused_login_fails_every_mail_step_without_a_secret(self):
        self.imap.password = self.smtp.password = "dummy-other-pass"
        self.assertEqual(self.run_smoke(), 1)
        self.assertTrue(self.result("imap login and inbox count").startswith("MailError (imap inbox count login"))
        self.assertTrue(self.result("smtp login, nothing sent").startswith("MailError (smtp login check login"))
        self.assert_no_value()

    def test_the_default_env_file_is_the_servers_secrets_file(self):
        self.assertEqual(smoke.DEFAULT_ENV, "/root/doris-secrets/.env")
        with mock.patch.object(smoke, "DEFAULT_ENV", self.env_path):
            self.assertEqual(smoke.main([], out=self.lines.append), 0)


class SendToTest(SmokeFixture):

    def deliver_to_sent(self, sender, recipients, message):
        self.sent.add(message, u(2026, 11, 27, 12))

    def test_sends_one_plain_signed_test_message_then_finds_it_in_sent(self):
        self.smtp.on_deliver = self.deliver_to_sent
        self.assertEqual(self.run_smoke("--send-to", "whizz@fastmail.com"), 0)
        self.assertEqual([line.split(": ", 1)[0] for line in self.lines], STEPS + SEND_STEPS + ["smoke"])
        (sender, recipients, data), = self.smtp.delivered
        self.assertEqual((sender, recipients), (DORIS, ["whizz@fastmail.com"]))
        msg = email.message_from_bytes(data, policy=email.policy.default)
        self.assertEqual((msg["From"], msg["To"], msg["Bcc"]), (DORIS, "whizz@fastmail.com", None))
        self.assertEqual(msg["Auto-Submitted"], "auto-generated")
        self.assertEqual(msg.get_content_type(), "text/plain")
        self.assertIn("I'm an AI, not a person.", msg.get_content())
        self.assertEqual(self.result("smtp send one test message"), "ok")
        self.assertEqual(self.result("sent copy, look 1 (at once)"), "ok (found)")
        self.assertEqual(self.result("sent copy, look 2 (after a short wait)"), "ok (found)")
        self.assert_no_value()

    def test_a_copy_that_appears_only_after_the_wait_passes_on_the_second_look(self):
        held = []
        self.smtp.on_deliver = lambda sender, recipients, message: held.append(message)
        late = lambda seconds: [self.sent.add(m, u(2026, 11, 27, 12)) for m in held]
        with mock.patch.object(smoke, "_sleep", late):
            self.assertEqual(self.run_smoke("--send-to", "wolf.whizz@fastmail.com"), 0)
        self.assertEqual(self.result("sent copy, look 1 (at once)"), "not found yet")
        self.assertEqual(self.result("sent copy, look 2 (after a short wait)"), "ok (found)")

    def test_a_copy_never_found_in_sent_fails_the_smoke(self):
        self.assertEqual(self.run_smoke("--send-to", "whizz@fastmail.com"), 1)
        self.assertEqual(len(self.smtp.delivered), 1)
        self.assertEqual(self.result("sent copy, look 1 (at once)"), "not found yet")
        self.assertEqual(self.result("sent copy, look 2 (after a short wait)"), "FAILED (not found on either look)")
        self.assertEqual(self.result("smoke"), "FAILED")

    def test_either_test_address_in_any_case_is_sent_to_in_its_own_spelling(self):
        self.smtp.on_deliver = self.deliver_to_sent
        self.assertEqual(self.run_smoke("--send-to", " Wolf.Whizz@FastMail.com "), 0)
        self.assertEqual(self.smtp.delivered[0][1], ["wolf.whizz@fastmail.com"])

    def test_any_other_address_is_refused_before_any_connection(self):
        for bad in ("someone@example.com", "whizz@fastmail.com.example.net", "whizz@fastmail.com, x@example.com",
                    "<whizz@fastmail.com>", ""):
            self.lines.clear()
            self.assertEqual(self.run_smoke("--send-to", bad), 2, bad)
            self.assertEqual(len(self.lines), 1)
            self.assertTrue(self.lines[0].startswith("smoke: refused --send-to"), self.lines)
        self.assertEqual((self.imap.commands, self.smtp.sessions, self.dav.requests), ([], [], []))


class SetupTest(SmokeFixture):

    def test_an_env_file_it_cannot_read_exits_2_before_any_connection(self):
        code = smoke.main(["--env", os.path.join(self.dir, "absent.env")], out=self.lines.append)
        self.assertEqual(code, 2)
        self.assertEqual(self.lines, ["env file: EnvFileError (cannot read the env file (FileNotFoundError))"])

    def test_a_setting_missing_from_the_env_exits_2_naming_the_setting(self):
        self.write_env(ENV_TEXT.replace("DORIS_CALDAV_URL=", "# DORIS_CALDAV_URL="))
        self.assertEqual(self.run_smoke(), 2)
        self.assertEqual(self.lines, ["config: ConfigError (DORIS_CALDAV_URL is not set)"])
        self.assertEqual((self.imap.commands, self.smtp.sessions, self.dav.requests), ([], [], []))

    def test_every_connection_goes_through_the_guard_allowing_only_the_adapters_hosts(self):
        installed = []
        real_install = netguard.install

        def spy(hosts=()):
            guard = real_install(hosts)
            installed.append((list(hosts), guard))
            return guard
        with mock.patch.object(netguard, "install", spy):
            self.assertEqual(self.run_smoke(), 0)
        (hosts, guard), = installed
        self.assertEqual(hosts, ["127.0.0.1"])
        self.assertGreaterEqual(len(guard.attempts), 3)
        self.assertEqual(guard.refused(), [])
        self.assertIsNone(netguard.active())

    def test_a_connection_to_a_host_the_adapters_do_not_name_is_refused_and_reported(self):
        def elsewhere(host, port, timeout):
            return imaplib.IMAP4("localhost", self.imap.port, timeout=timeout)
        with mock.patch.object(live, "_imap_tls", elsewhere):
            self.assertEqual(self.run_smoke(), 1)
        self.assertEqual(self.result("imap login and inbox count"), "MailError (imap inbox count connect: NetworkRefused)")
        self.assertEqual(self.result("network guard"), "FAILED (2 connections to a host the adapters do not name)")
        self.assertEqual(self.imap.commands, [])
        self.assertIsNone(netguard.active())


if __name__ == "__main__":
    unittest.main()
