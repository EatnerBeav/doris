"""smoke.main: one pass over every live adapter call, one plain line per step, never a value.

The real adapters talk to test_live's fake IMAP, SMTP and CalDAV servers on 127.0.0.1 (the
adapters' default transports are swapped for plain connections to them), under the network
guard the smoke installs itself: the env file names 127.0.0.1 as every host. Every test has
its own state/ and repo/ folders (paths.STATE and paths.REPO patched), so the live halt, STOP
and lock files it reads and takes are never the real ones.
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

from doris import ics, netguard, paths, smoke, supervisor, templates
from doris.adapters import live
from doris.shapes import Slot
from tests.unit.test_live import COLLECTION, DORIS, PASSWORD, Box, FakeCalDAV, FakeIMAP, FakeSMTP, eml, u

ENV_TEXT = ("# a test env file: every host is the fakes' 127.0.0.1\n"
            "DORIS_MAIL_ADDRESS=" + DORIS + "\n"
            "DORIS_APP_PASSWORD='" + PASSWORD + "'\n"
            "DORIS_IMAP_HOST=127.0.0.1\n"
            "DORIS_SMTP_HOST=127.0.0.1\n"
            "DORIS_CALDAV_URL=https://127.0.0.1" + COLLECTION + "\n")

STEPS = ["imap login and inbox count", "sent folder found by its special-use flag", "smtp login, nothing sent",
         "caldav query, next 7 days", "caldav test event left by an earlier run", "caldav create test event",
         "caldav read test event back, times match", "caldav delete test event", "caldav read after delete, gone"]
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
        self.repo = os.path.join(self.dir, "repo")
        os.makedirs(self.repo)
        self.lines = []
        for patch in (
                mock.patch.object(paths, "STATE", os.path.join(self.dir, "state")),
                mock.patch.object(paths, "REPO", self.repo),
                mock.patch.object(live, "_imap_tls", lambda host, port, timeout: imaplib.IMAP4(
                    "127.0.0.1", self.imap.port, timeout=timeout)),
                mock.patch.object(live, "_smtp_tls", lambda host, port, timeout: smtplib.SMTP(
                    "127.0.0.1", self.smtp.port, local_hostname="[127.0.0.1]", timeout=timeout)),
                mock.patch.object(live, "_https", lambda host, port, timeout: http.client.HTTPConnection(
                    "127.0.0.1", self.dav.port, timeout=timeout)),
                mock.patch.object(smoke, "SENT_LOOK_WAIT_S", 0.0),
                mock.patch.object(smoke, "CLEANUP_WAIT_S", 0.0)):
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

    def test_the_test_event_is_far_ahead_under_one_fixed_booking_id_and_gone_after(self):
        """One fixed ID, so a run that leaves its event behind is cleaned up by the next one."""
        before = dt.datetime.now(dt.timezone.utc)
        for _ in range(2):
            self.lines.clear()
            self.assertEqual(self.run_smoke(), 0)
            self.assertEqual(self.result("caldav test event left by an earlier run"), "ok (none)")
        self.assertTrue(ics.is_booking_id(smoke.SMOKE_BOOKING_ID))
        # leftover check (a 404, confirmed by a UID query), create, read back, delete, read again
        self.assertEqual(self.dav.methods(), ["REPORT", "GET", "REPORT", "PUT", "GET", "GET", "DELETE", "GET",
                                              "REPORT"] * 2)
        puts = [r for r in self.dav.requests if r[0] == "PUT"]
        for _, path, _, body in puts:
            self.assertEqual(path, COLLECTION + smoke.SMOKE_BOOKING_ID + ".ics")
            self.assertTrue(ics.fields_ok(body, booking_id=smoke.SMOKE_BOOKING_ID))
            self.assertGreater(ics.read_event(body).start_utc, before + dt.timedelta(days=300))
        self.assertEqual(self.dav.resources, {})

    def leftover(self):
        start = u(2027, 12, 1, 11)
        self.dav.put(smoke.SMOKE_BOOKING_ID + ".ics", ics.build_event(
            smoke.SMOKE_BOOKING_ID, Slot(start, start + dt.timedelta(minutes=30)), templates.PLACEHOLDER_MEETING_LINK,
            "CONFIRMED", 0, u(2026, 11, 27), None))

    def test_a_test_event_left_by_an_earlier_run_is_removed_first(self):
        self.leftover()
        self.assertEqual(self.run_smoke(), 0)
        self.assertEqual(self.result("caldav test event left by an earlier run"), "ok (removed one)")
        self.assertEqual(self.result("caldav create test event"), "ok")
        self.assertEqual(self.dav.resources, {})
        self.assert_no_value()

    def test_a_delete_refused_once_is_tried_again(self):
        refusals = []

        def refuse_the_first_delete(method, name):
            if method == "DELETE":
                if refusals:
                    self.dav.status.pop("DELETE", None)
                else:
                    refusals.append(name)
                    self.dav.status["DELETE"] = 503
        self.dav.on_request = refuse_the_first_delete
        self.assertEqual(self.run_smoke(), 0)
        self.assertEqual(self.result("caldav delete test event"), "ok (on try 2)")
        self.assertEqual(self.result("caldav read after delete, gone"), "ok")
        self.assertEqual(self.dav.resources, {})

    def test_an_event_that_may_remain_is_named_by_its_booking_id_and_the_next_run_removes_it(self):
        self.dav.status["DELETE"] = 503
        self.assertEqual(self.run_smoke(), 1)
        self.assertEqual(self.result("caldav delete test event"),
                         "CalendarError (caldav delete: 503), on each of %d tries" % smoke.CLEANUP_TRIES)
        self.assertEqual(self.result("caldav read after delete, gone"), "FAILED (still there)")
        self.assertIn(smoke.SMOKE_BOOKING_ID, self.result("caldav test event may remain"))
        self.assertEqual(self.result("smoke"), "FAILED")
        del self.dav.status["DELETE"]
        self.lines.clear()
        self.assertEqual(self.run_smoke(), 0)
        self.assertEqual(self.result("caldav test event left by an earlier run"), "ok (removed one)")
        self.assertEqual(self.dav.resources, {})

    def test_an_interrupt_after_the_create_still_deletes_the_test_event(self):
        real_create = live.LiveCalendar.create_only

        def create_then_interrupt(cal, booking_id, data):
            real_create(cal, booking_id, data)
            raise KeyboardInterrupt
        with mock.patch.object(live.LiveCalendar, "create_only", create_then_interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_smoke()
        self.assertEqual(self.result("caldav delete test event"), "ok")
        self.assertEqual(self.result("caldav read after delete, gone"), "ok")
        self.assertEqual(self.dav.resources, {})
        self.assertIsNone(netguard.active())

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


class HaltAndLockTest(SmokeFixture):
    """A smoke is a live run's peer: a halt in force for live refuses it, and it holds the live
    lock from before its first connection to after its last, so no live run starts meanwhile."""

    def lock_path(self):
        return os.path.join(paths.state_dir("live"), supervisor.LOCK_NAME)

    def nothing_contacted(self):
        self.assertEqual((self.imap.commands, self.smtp.sessions, self.dav.requests), ([], [], []))

    def touch(self, path, data=b""):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)

    def test_a_halt_in_force_refuses_it_before_any_connection(self):
        supervisor.request_stop("live", "cli")
        self.assertEqual(self.run_smoke(), supervisor.EXIT_REFUSED_HALT)
        self.assertEqual(self.lines, ["smoke: refused: Doris is halted (stop on the command line)"])
        self.nothing_contacted()

    def test_a_stop_file_in_the_live_folder_or_the_repo_refuses_it(self):
        for folder in (paths.state_dir("live"), paths.REPO):
            path = os.path.join(folder, "STOP")
            self.touch(path)
            self.lines.clear()
            self.assertEqual(self.run_smoke(), supervisor.EXIT_REFUSED_HALT, folder)
            self.assertEqual(self.lines, ["smoke: refused: Doris is halted (a STOP file)"])
            os.remove(path)
        self.nothing_contacted()

    def test_a_halt_file_it_cannot_read_refuses_it(self):
        self.touch(supervisor.halt_file("live"), b"not a halt record")
        self.assertEqual(self.run_smoke(), supervisor.EXIT_REFUSED_HALT)
        self.assertEqual(self.lines, ["smoke: refused: a halt file cannot be read, so the halt stays in force"])
        self.nothing_contacted()

    def test_a_halt_in_another_mode_does_not_refuse_it(self):
        supervisor.request_stop("sandbox", "cli")
        self.assertEqual(self.run_smoke(), 0)

    def test_a_live_run_holding_the_lock_refuses_it_before_any_connection(self):
        held = supervisor.acquire_lock(self.lock_path())
        self.addCleanup(held.release)
        self.assertEqual(self.run_smoke(), supervisor.EXIT_LOCKED)
        self.assertEqual(self.lines, ["smoke: refused: another Doris is already running on this ledger"])
        self.nothing_contacted()

    def test_a_lock_it_cannot_take_refuses_it(self):
        with mock.patch.object(supervisor, "acquire_lock", side_effect=PermissionError(13, "denied")):
            self.assertEqual(self.run_smoke(), supervisor.EXIT_LOCKED)
        self.assertEqual(self.lines, ["smoke: refused: the live lock cannot be taken (PermissionError)"])
        self.nothing_contacted()

    def test_it_holds_the_live_lock_at_every_connection_and_lets_go_at_the_end(self):
        free = []

        def probe(*_):
            handle = supervisor.acquire_lock(self.lock_path())
            free.append(handle is not None)
            if handle is not None:
                handle.release()
        real_imap, real_smtp = live._imap_tls, live._smtp_tls
        self.smtp.on_deliver = lambda sender, recipients, message: self.sent.add(message, u(2026, 11, 27, 12))
        self.dav.on_request = probe
        with mock.patch.object(live, "_imap_tls", lambda *a: (probe(), real_imap(*a))[1]), \
                mock.patch.object(live, "_smtp_tls", lambda *a: (probe(), real_smtp(*a))[1]):
            self.assertEqual(self.run_smoke("--send-to", "whizz@fastmail.com"), 0)
        self.assertGreater(len(free), 10)
        self.assertFalse(any(free), "the lock was free during the smoke")
        again = supervisor.acquire_lock(self.lock_path())
        self.assertIsNotNone(again)
        again.release()

    def test_a_ctrl_c_lets_go_of_the_lock_too(self):
        with mock.patch.object(live.LiveCalendar, "query", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_smoke()
        again = supervisor.acquire_lock(self.lock_path())
        self.assertIsNotNone(again)
        again.release()


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
