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
import json
import os
import re
import shutil
import smtplib
import tempfile
import unittest
from unittest import mock

from doris import ics, loop, netguard, paths, sentrecord, smoke, supervisor, templates
from doris.adapters import live
from doris.shapes import MailError, Slot
from tests.unit.test_live import COLLECTION, DORIS, OWNER, PASSWORD, Box, FakeCalDAV, FakeIMAP, FakeSMTP, eml, u

ENV_TEXT = ("# a test env file: every host is the fakes' 127.0.0.1\n"
            "DORIS_MAIL_ADDRESS=" + DORIS + "\n"
            "DORIS_APP_PASSWORD='" + PASSWORD + "'\n"
            "DORIS_IMAP_HOST=127.0.0.1\n"
            "DORIS_SMTP_HOST=127.0.0.1\n"
            "DORIS_CALDAV_URL=https://127.0.0.1" + COLLECTION + "\n")

FETCH, FILE = "imap fetch, last 7 days, read-only", "imap file newest INBOX message with the smoke keyword"
NAME = "caldav calendar display name"
STEPS = ["imap login and inbox count", "sent folder found by its special-use flag", FETCH, FILE,
         "smtp login, nothing sent", NAME, "caldav query, next 7 days", "caldav test event left by an earlier run",
         "caldav create test event", "caldav read test event back, times match", "caldav delete test event",
         "caldav read after delete, gone"]
SEND_STEPS = ["smtp send one test message", "sent copy, look 1 (at once)", "sent copy, look 2 (after a short wait)",
              "sent copy found", "sent copy record for a live run"]
SEND_NOT_RUN = "smtp send one test message, then its copy in Sent"     # the line in their place without --send-to
READ_ONLY_IMAP = {"CAPABILITY", "LOGIN", "LOGOUT", "EXAMINE", "LIST", "UID SEARCH", "UID FETCH"}


class SmokeFixture(unittest.TestCase):

    def setUp(self):
        self.imap, self.smtp, self.dav = FakeIMAP(), FakeSMTP(), FakeCalDAV()
        for server in (self.imap, self.smtp, self.dav):
            self.addCleanup(server.stop)
        # INBOX: two messages received in the last hours (the fetch step reads the last 7 days)
        self.now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        self.inbox = self.imap.boxes["INBOX"]
        self.inbox.add(eml(1), self.now - dt.timedelta(hours=2))
        self.inbox.add(eml(2), self.now - dt.timedelta(hours=1))
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

    def deliver_to_sent(self, sender, recipients, message):
        self.sent.add(message, u(2026, 11, 27, 12))

    def late_copy(self, wait_s):
        """The copy reaches Sent only during the wait between the looks, which takes wait_s on a
        clock the smoke reads (smoke._clock)."""
        held, clock = [], [1000.0]
        self.smtp.on_deliver = lambda sender, recipients, message: held.append(message)

        def wait(seconds):
            clock[0] += wait_s
            while held:
                self.sent.add(held.pop(), u(2026, 11, 27, 12))
        return [mock.patch.object(smoke, "_sleep", wait), mock.patch.object(smoke, "_clock", lambda: clock[0])]

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
        self.smtp.on_deliver = self.deliver_to_sent
        self.assertEqual(self.run_smoke("--send-to", "whizz@fastmail.com"), 0)
        self.assertEqual([line.split(": ", 1)[0] for line in self.lines], STEPS + SEND_STEPS + ["smoke"])
        self.assertEqual(self.result("imap login and inbox count"), "ok (2 messages in INBOX)")
        self.assertEqual(self.result("sent folder found by its special-use flag"), "ok (1 folder)")
        self.assertEqual(self.result(NAME), 'ok ("Bookings with Doris")')
        self.assertEqual(self.result("caldav query, next 7 days"), "ok (0 events)")
        for step in STEPS + SEND_STEPS:
            self.assertTrue(self.result(step).startswith("ok"), self.lines)
        self.assertEqual(self.result("smoke"), "ok, every step passed")
        self.assert_no_value()

    def test_without_send_to_the_send_is_named_as_not_run_and_never_every_step_passed(self):
        self.assertEqual(self.run_smoke(), 0)
        self.assertEqual([line.split(": ", 1)[0] for line in self.lines], STEPS + [SEND_NOT_RUN, "smoke"])
        self.assertEqual(self.result(SEND_NOT_RUN), "not run (no --send-to)")
        self.assertEqual(self.result("smoke"), "every step that ran passed; not run: %s (no --send-to)" % SEND_NOT_RUN)
        self.assertFalse(any("every step passed" in line for line in self.lines), self.lines)
        self.assert_no_value()

    def test_a_failed_smoke_still_names_what_did_not_run(self):
        self.dav.status["PUT"] = 403
        self.assertEqual(self.run_smoke(), 1)
        self.assertEqual(self.result("smoke"), "FAILED; not run: %s (no --send-to)" % SEND_NOT_RUN)

    def test_without_send_to_nothing_is_sent_and_imap_writes_only_the_smoke_keyword(self):
        self.run_smoke()
        self.assertEqual(self.smtp.sessions[0]["auth"], DORIS)
        self.assertFalse({"MAIL", "RCPT", "DATA"} & set(self.smtp.verbs()))
        writes = [(verb, args) for verb, args in self.imap.commands if verb not in READ_ONLY_IMAP]
        self.assertEqual(writes, [("SELECT", '"INBOX"'), ("UID STORE", "2 +FLAGS (doris-smoke)")])
        self.assertEqual({uid: msg["flags"] for uid, msg in self.inbox.messages.items()},
                         {1: set(), 2: {"doris-smoke"}})       # \Seen untouched: the fetch only peeks

    def test_the_test_event_is_far_ahead_under_one_fixed_booking_id_and_gone_after(self):
        """One fixed ID, so a run that leaves its event behind is cleaned up by the next one."""
        before = dt.datetime.now(dt.timezone.utc)
        for _ in range(2):
            self.lines.clear()
            self.assertEqual(self.run_smoke(), 0)
            self.assertEqual(self.result("caldav test event left by an earlier run"), "ok (none)")
        self.assertTrue(ics.is_booking_id(smoke.SMOKE_BOOKING_ID))
        # display name, query, leftover check (a 404, confirmed by a UID query), create, read back, delete,
        # read again
        self.assertEqual(self.dav.methods(), ["PROPFIND", "REPORT", "GET", "REPORT", "PUT", "GET", "GET", "DELETE",
                                              "GET", "REPORT"] * 2)
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
        self.assertEqual(self.result("smoke"), "FAILED; not run: %s (no --send-to)" % SEND_NOT_RUN)
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
        self.assertEqual(self.result("smoke"), "FAILED; not run: %s (no --send-to)" % SEND_NOT_RUN)
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

    def test_the_default_env_file_is_the_one_a_live_run_reads_too(self):
        """Without --env the smoke reads paths.LIVE_ENV, the one default `run --live` reads too."""
        self.assertEqual(paths.LIVE_ENV, "/root/doris-secrets/.env")
        with mock.patch.object(paths, "LIVE_ENV", self.env_path):
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


class DisplayNameTest(SmokeFixture):
    """The calendar's display name is the one server text a line shows, so a person can see which
    calendar the URL names: as the server reports it, on one line, nothing in it able to move the
    cursor or turn text around, and cut when long."""

    def test_the_display_name_is_shown_as_the_server_reports_it(self):
        self.dav.display_name = "  Owner's \"bookings\" & Caf\u00e9  "
        self.assertEqual(self.run_smoke(), 0)
        self.assertEqual(self.result(NAME), 'ok ("  Owner\'s "bookings" & Caf\u00e9  ")')

    def test_a_line_end_or_a_direction_mark_in_it_is_shown_escaped_on_the_one_line(self):
        self.dav.display_name = "cal\nsmoke: ok, every step passed\r\t\u202edesrever"
        self.assertEqual(self.run_smoke(), 0)
        self.assertEqual(self.result(NAME), 'ok ("cal\\nsmoke: ok, every step passed\\r\\t\\u202edesrever")')
        self.assertEqual(len(self.lines), len(STEPS) + 2)
        self.assertFalse(any("\n" in line or "\r" in line or "\u202e" in line for line in self.lines))

    def test_a_long_display_name_is_cut(self):
        self.dav.display_name = "x" * 1000
        self.assertEqual(self.run_smoke(), 0)
        self.assertEqual(self.result(NAME), 'ok ("%s...")' % ("x" * smoke.DISPLAY_NAME_MAX))

    def test_a_calendar_that_reports_no_display_name_fails_that_step(self):
        self.dav.display_name = None
        self.assertEqual(self.run_smoke(), 1)
        self.assertEqual(self.result(NAME), "CalendarError (caldav display name: the calendar reports no display name)")
        self.assertEqual(self.result("caldav create test event"), "ok")


class FetchAndFileTest(SmokeFixture):
    """fetch_new over the last 7 days, read-only; then file adds the smoke keyword to her newest
    INBOX message (by the time it arrived), and to nothing else."""

    def test_the_fetch_counts_the_last_7_days_and_the_newest_by_arrival_gets_the_keyword(self):
        self.inbox.add(eml(3), self.now - dt.timedelta(days=9))          # outside the window
        self.inbox.add(eml(4), self.now - dt.timedelta(hours=5))         # a higher UID, an older arrival
        self.assertEqual(self.run_smoke(), 0)
        self.assertEqual(self.result(FETCH), "ok (3 messages)")
        self.assertEqual(self.result(FILE), "ok")
        flagged = {uid: msg["flags"] for uid, msg in self.inbox.messages.items() if msg["flags"]}
        self.assertEqual(flagged, {2: {"doris-smoke"}})                   # UID 2 arrived last
        self.assert_no_value()

    def test_with_no_message_in_the_last_7_days_the_file_step_says_it_did_not_run(self):
        self.inbox.messages.clear()
        self.inbox.add(eml(5), self.now - dt.timedelta(days=9))
        self.assertEqual(self.run_smoke(), 0)
        self.assertEqual(self.result(FETCH), "ok (0 messages)")
        self.assertEqual(self.result(FILE), "not run (no INBOX message in the last 7 days)")
        self.assertEqual(self.result("smoke"), "every step that ran passed; not run: %s (no INBOX message in the last "
                                               "7 days); %s (no --send-to)" % (FILE, SEND_NOT_RUN))
        self.assertNotIn("UID STORE", self.imap.verbs())

    def test_a_failed_fetch_fails_the_smoke_and_nothing_is_filed(self):
        self.imap.fault["UID FETCH"] = "no"
        self.assertEqual(self.run_smoke(), 1)
        self.assertTrue(self.result(FETCH).startswith("MailError (imap fetch"), self.lines)
        self.assertEqual(self.result(FILE), "not run (the fetch failed)")
        self.assertTrue(self.result("smoke").startswith("FAILED; not run: %s (the fetch failed)" % FILE))
        self.assertNotIn("UID STORE", self.imap.verbs())

    def test_a_keyword_the_server_did_not_keep_fails_the_file_step(self):
        self.imap.store_ignored = True
        self.assertEqual(self.run_smoke(), 1)
        self.assertEqual(self.result(FILE), "MailError (imap file: the server did not keep the keyword)")
        self.assert_no_value()


class SendToTest(SmokeFixture):

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
        self.assertRegex(self.result("sent copy found"), r"^ok \(look 1, \d+\.\d s after the send\)\Z")
        self.assert_no_value()

    def test_a_copy_that_appears_only_after_the_wait_passes_on_the_second_look(self):
        patches = self.late_copy(5.0)
        with patches[0], patches[1]:
            self.assertEqual(self.run_smoke("--send-to", "wolf.whizz@fastmail.com"), 0)
        self.assertEqual(self.result("sent copy, look 1 (at once)"), "not found yet")
        self.assertEqual(self.result("sent copy, look 2 (after a short wait)"), "ok (found)")
        self.assertEqual(self.result("sent copy found"), "ok (look 2, 5.0 s after the send)")

    def test_a_copy_never_found_in_sent_fails_the_smoke(self):
        self.assertEqual(self.run_smoke("--send-to", "whizz@fastmail.com"), 1)
        self.assertEqual(len(self.smtp.delivered), 1)
        self.assertEqual(self.result("sent copy, look 1 (at once)"), "not found yet")
        self.assertEqual(self.result("sent copy, look 2 (after a short wait)"), "not found")
        self.assertEqual(self.result("sent copy found"), "FAILED (not found on either look)")
        self.assertEqual(self.result("sent copy record for a live run"), "not written (the copy was not found)")
        self.assertEqual(self.result("smoke"), "FAILED")

    def test_a_look_that_cannot_tell_is_named_and_the_other_look_still_counts(self):
        self.smtp.on_deliver = self.deliver_to_sent
        cannot_tell = MailError("imap sent lookup: a message the search named could not be read; cannot tell")
        for answers, found in (([cannot_tell, True], "ok (look 2, "), ([True, cannot_tell], "ok (look 1, "),
                               ([cannot_tell, False], "FAILED (not found; a look could not tell)")):
            self.lines.clear()
            with mock.patch.object(live.LiveMail, "find_in_sent", side_effect=answers):
                self.assertEqual(self.run_smoke("--send-to", "whizz@fastmail.com"), 0 if found.startswith("ok") else 1)
            looks = [self.result("sent copy, look 1 (at once)"), self.result("sent copy, look 2 (after a short wait)")]
            self.assertIn("MailError (imap sent lookup: a message the search named could not be read; cannot tell)",
                          looks)
            self.assertTrue(self.result("sent copy found").startswith(found), self.lines)

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


class SentRecordTest(SmokeFixture):
    """state/live/smoke-sent.json, which a live run needs before it starts: written whole (a temp
    file, then os.replace) only by a smoke whose --send-to copy was found in Sent, and holding
    nothing but when, that it was found, by which look and after how long. Any other smoke that
    runs removes an old one first; a smoke refused before it runs leaves it alone. The file and
    its format are sentrecord's, which the live preflight reads too."""

    KEYS = {"ts", "sent_copy_found", "look", "waited_s"}

    def path(self):
        return sentrecord.path(paths.state_dir("live"))

    def old_record(self):
        os.makedirs(paths.state_dir("live"), exist_ok=True)
        sentrecord.write(self.path(), 1, 0.1, dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc))

    def preflight(self):
        """`run --live`'s checks before it makes anything (loop.live_preflight, as the command line
        makes them), on what this smoke left in the live state folder, with a whole live env file."""
        env = self.write_env(ENV_TEXT + "DORIS_OWNER_ADDRESS=" + OWNER + "\n"
                             "DORIS_MEETING_LINK=https://meet.example/smoke-room\n"
                             "ANTHROPIC_API_KEY=dummy-key-for-the-preflight\n")
        return loop.live_preflight(paths.state_dir("live"), env, True)

    def test_the_record_this_smoke_writes_is_one_a_live_run_takes(self):
        self.smtp.on_deliver = self.deliver_to_sent
        self.assertEqual(self.run_smoke("--send-to", "whizz@fastmail.com"), 0)
        self.assertIsNone(loop.smoke_refusal(paths.state_dir("live")))
        self.assertEqual(self.preflight().doris_addr, DORIS)      # not refused

    def test_after_a_smoke_that_did_not_find_its_copy_a_live_run_is_refused(self):
        self.old_record()
        self.assertIsNone(loop.smoke_refusal(paths.state_dir("live")))
        self.assertEqual(self.run_smoke("--send-to", "whizz@fastmail.com"), 1)
        with self.assertRaises(loop.Refused) as cm:
            self.preflight()
        self.assertEqual(str(cm.exception), loop.SMOKE_REFUSAL)

    def record(self):
        with open(self.path(), encoding="utf-8") as fh:
            text = fh.read()
        for value in (DORIS, "whizz", PASSWORD, "doris-", "@", "<", COLLECTION, "127.0.0.1"):
            self.assertNotIn(value, text)
        return json.loads(text)

    def files(self):
        return sorted(os.listdir(paths.state_dir("live")))

    def test_a_copy_found_writes_when_which_look_and_the_wait_and_nothing_else(self):
        self.smtp.on_deliver = self.deliver_to_sent
        before = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        self.assertEqual(self.run_smoke("--send-to", "whizz@fastmail.com"), 0)
        record = self.record()
        self.assertEqual(set(record), self.KEYS)
        self.assertIs(record["sent_copy_found"], True)
        self.assertEqual(record["look"], 1)
        self.assertIsInstance(record["waited_s"], float)
        self.assertGreaterEqual(record["waited_s"], 0.0)
        self.assertRegex(record["ts"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\Z")
        stamp = dt.datetime.strptime(record["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
        self.assertTrue(before <= stamp <= dt.datetime.now(dt.timezone.utc), record["ts"])
        self.assertEqual(self.result("sent copy record for a live run"), "ok (written)")
        self.assertEqual(self.files(), ["ledger.lock", "smoke-sent.json"])      # no temp file left beside it
        self.assert_no_value()

    def test_a_copy_found_on_the_second_look_records_look_2_and_the_wait(self):
        patches = self.late_copy(5.0)
        with patches[0], patches[1]:
            self.assertEqual(self.run_smoke("--send-to", "whizz@fastmail.com"), 0)
        record = self.record()
        self.assertEqual((record["look"], record["waited_s"]), (2, 5.0))

    def test_a_copy_not_found_removes_an_old_record_and_writes_none(self):
        self.old_record()
        self.assertEqual(self.run_smoke("--send-to", "whizz@fastmail.com"), 1)
        self.assertEqual(self.files(), ["ledger.lock"])

    def test_a_smoke_without_send_to_removes_an_old_record(self):
        self.old_record()
        self.assertEqual(self.run_smoke(), 0)
        self.assertEqual(self.files(), ["ledger.lock"])

    def test_a_smoke_cut_short_leaves_no_record(self):
        self.old_record()
        with mock.patch.object(live.LiveCalendar, "query", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_smoke("--send-to", "whizz@fastmail.com")
        self.assertEqual(self.files(), ["ledger.lock"])

    def test_a_smoke_refused_by_a_halt_or_the_lock_leaves_the_record_alone(self):
        self.old_record()
        supervisor.request_stop("live", "cli")
        self.assertEqual(self.run_smoke(), supervisor.EXIT_REFUSED_HALT)
        self.assertEqual(self.record()["ts"], "2026-11-01T00:00:00Z")
        os.remove(supervisor.halt_file("live"))
        held = supervisor.acquire_lock(os.path.join(paths.state_dir("live"), supervisor.LOCK_NAME))
        self.addCleanup(held.release)
        self.assertEqual(self.run_smoke(), supervisor.EXIT_LOCKED)
        self.assertEqual(self.record()["ts"], "2026-11-01T00:00:00Z")

    def test_an_old_record_it_cannot_remove_stops_it_before_any_connection(self):
        os.makedirs(self.path())                 # a folder where the record goes: it cannot be removed
        self.assertEqual(self.run_smoke("--send-to", "whizz@fastmail.com"), 1)
        self.assertEqual(len(self.lines), 1, self.lines)
        self.assertRegex(self.lines[0], r"^smoke: FAILED: the old sent-copy record cannot be removed \(\w+\)\Z")
        self.assertEqual((self.imap.commands, self.smtp.sessions, self.dav.requests), ([], [], []))

    def test_a_record_it_cannot_write_fails_the_smoke_and_leaves_nothing_behind(self):
        self.smtp.on_deliver = self.deliver_to_sent
        with mock.patch.object(smoke.os, "replace", side_effect=PermissionError(13, "denied")):
            self.assertEqual(self.run_smoke("--send-to", "whizz@fastmail.com"), 1)
        self.assertEqual(self.result("sent copy found")[:2], "ok")
        self.assertEqual(self.result("sent copy record for a live run"), "PermissionError")
        self.assertEqual(self.result("smoke"), "FAILED")
        self.assertEqual(self.files(), ["ledger.lock"])


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
        # inbox count, sent folders and the fetch each tried once; the file step never ran
        self.assertEqual(self.result("network guard"), "FAILED (3 connections to a host the adapters do not name)")
        self.assertEqual(self.result(FILE), "not run (the fetch failed)")
        self.assertEqual(self.imap.commands, [])
        self.assertIsNone(netguard.active())


if __name__ == "__main__":
    unittest.main()
