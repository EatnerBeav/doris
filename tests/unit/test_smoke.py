"""smoke.main: one pass over every live adapter call, one plain line per step, never a value.

The real adapters talk to test_live's fake IMAP and CalDAV servers and fake_jmap's JMAP servers
on 127.0.0.1 (the adapters' default transports are swapped for plain connections to them), under
the network guard the smoke installs itself: the env file names 127.0.0.1 as every host it can.
Her sends and wolf's go to api.fastmail.com and the host its session names (phl.api.fastmail.com),
through the guard under those names (fake_jmap.dialling), then to her account's fake or to
wolf.whizz's for the end-to-end test, by the token each request carries. Every test
has its own state/, repo/ and repo/traces/ folders (paths.STATE, paths.REPO and paths.TRACES
patched), so the live halt, STOP and lock files it reads and takes, and the traces it keeps, are
never the real ones. The model is the fake answer of a request with no exact time.
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
import tempfile
import unittest
from unittest import mock

from doris import caps, envfile, ics, ledger as ledger_mod, livetrace, loop, model as model_mod, netguard, paths, receipts
from doris import sentrecord, smoke, supervisor, templates
from doris.adapters import jmap, live
from doris.shapes import MailError, Slot
from tests.unit import fake_jmap
from tests.unit.fake_jmap import TOKEN, FakeJMAP
from tests.unit.test_live import COLLECTION, DORIS, OWNER, PASSWORD, Box, FakeCalDAV, FakeIMAP, eml, u

ENV_TEXT = ("# a test env file: every host is the fakes' 127.0.0.1\n"
            "DORIS_MAIL_ADDRESS=" + DORIS + "\n"
            "DORIS_APP_PASSWORD='" + PASSWORD + "'\n"
            "DORIS_IMAP_HOST=127.0.0.1\n"
            "DORIS_JMAP_TOKEN=" + TOKEN + "\n"
            "DORIS_CALDAV_URL=https://127.0.0.1" + COLLECTION + "\n")

FETCH, FILE = "imap fetch, last 7 days, read-only", "imap file newest INBOX message with the smoke keyword"
NAME = "caldav calendar display name"
STEPS = ["imap login and inbox count", "sent folder found by its special-use flag", FETCH, FILE,
         "jmap session, nothing sent", NAME, "caldav query, next 7 days", "caldav test event left by an earlier run",
         "caldav create test event", "caldav read test event back, times match", "caldav delete test event",
         "caldav read after delete, gone"]
SEND_STEPS = ["jmap send one test message", "sent copy, look 1 (at once)", "sent copy, look 2 (after a short wait)",
              "sent copy found", "sent copy record for a live run"]
SEND_NOT_RUN = "jmap send one test message, then its copy in Sent"     # the line in their place without --send-to
E2E_NOT_RUN = "end-to-end test from wolf"                              # the end-to-end test's, when it does not run
NO_SEND_TO = "%s (no --send-to); %s (no --send-to)" % (SEND_NOT_RUN, E2E_NOT_RUN)    # the last line's not-run part
READ_ONLY_IMAP = {"CAPABILITY", "LOGIN", "LOGOUT", "EXAMINE", "LIST", "UID SEARCH", "UID FETCH"}

# The end-to-end test: the wolf.whizz account (its own fake JMAP server) and her loop's settings
WOLF = "wolf.whizz@fastmail.com"
WOLF_TOKEN = "dummy-wolf-jmap-token-for-smoke-tests-6161"
WOLF_PASSWORD = "dummy-wolf-app-pass-6262"             # on wide's env file too; nothing reads it
API_KEY = "dummy-anthropic-key-for-smoke-tests-6363"
LINK = "https://meet.example/" + "smoke-e2e-room"
OWNER_CALENDAR = "dummy-owner-calendar-6464"            # on wide's env file too; nothing reads it
OWNER_FULL_NAME = "Dummy Owner-Fullname 6565"          # a display name as the owner's calendar may carry: never shown
E2E_TEXT = ENV_TEXT + ("DORIS_OWNER_ADDRESS=" + OWNER + "\n" "DORIS_MEETING_LINK=" + LINK + "\n"
                       "ANTHROPIC_API_KEY=" + API_KEY + "\n" "WOLF_EMAIL=" + WOLF + "\n"
                       "WOLF_JMAP_TOKEN=" + WOLF_TOKEN + "\n" "WOLF_APP_PASSWORD=" + WOLF_PASSWORD + "\n"
                       "OWNER_CALENDAR=" + OWNER_CALENDAR + "\n")
E2E_STEPS = ["wolf sends one meeting request to the owner", "test request in her INBOX, look 1 (after a wait)",
             "test request in her INBOX, look 2 (after another wait)", "test request marked as the smoke's own",
             "her loop on the test request", "her reply confirmed in her Sent",
             "her reply in wolf's mailbox, look 1 (at once)", "her reply in wolf's mailbox, look 2 (after a wait)"]


def no_time_answer():
    """The model's answer to the smoke's request: a meeting request with no exact time."""
    fields = {"cls": "meeting_request", "date": None, "time24": None, "zone": None, "length_min": 30,
              "pick": None, "injection_seen": False, "side_question": False, "side_question_quote": None}
    return {"content": [{"type": "text", "text": json.dumps(fields)}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 900, "output_tokens": 60}}


class SmokeFixture(unittest.TestCase):

    def setUp(self):
        self.imap, self.jmap, self.dav = FakeIMAP(), FakeJMAP().as_fastmail(), FakeCalDAV()
        self.wolf = fake_jmap.for_account(WOLF, WOLF_TOKEN)
        for server in (self.imap, self.jmap, self.dav, self.wolf):
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
        self.traces = os.path.join(self.repo, "traces")
        self.lines, self.posts, self.held = [], [], []
        self.wolf.on_deliver = self.forward
        self.forward_late = False
        for patch in (
                mock.patch.object(paths, "STATE", os.path.join(self.dir, "state")),
                mock.patch.object(paths, "REPO", self.repo),
                mock.patch.object(paths, "TRACES", self.traces),
                mock.patch.object(live, "_imap_tls", lambda host, port, timeout: imaplib.IMAP4(
                    "127.0.0.1", self.imap.port, timeout=timeout)),
                *fake_jmap.dialling(self.jmap, self.wolf),
                mock.patch.object(live, "_https", lambda host, port, timeout: http.client.HTTPConnection(
                    "127.0.0.1", self.dav.port, timeout=timeout)),
                mock.patch.object(model_mod, "_urllib_post", self.post),
                mock.patch.dict(os.environ, {"HTTPS_PROXY": "", "https_proxy": ""}),
                mock.patch.object(smoke, "SENT_LOOK_WAIT_S", 0.0),
                mock.patch.object(smoke, "FORWARD_WAIT_S", 0.0),
                mock.patch.object(smoke, "REPLY_WAIT_S", 0.0),
                mock.patch.object(smoke, "CLEANUP_WAIT_S", 0.0)):
            patch.start()
            self.addCleanup(patch.stop)

    def post(self, url, headers, body, timeout):
        self.posts.append((url, dict(headers), json.loads(body.decode("utf-8"))))
        return 200, json.dumps(no_time_answer()).encode("utf-8")

    def forward(self, sender, recipients, message):
        """wolf's mail to the owner reaches her INBOX: his forward to her (later, with forward_late)."""
        if OWNER in recipients:
            if self.forward_late:
                self.held.append(message)
            else:
                self.inbox.add(message, dt.datetime.now(dt.timezone.utc))

    def delivers(self, sender, recipients, message):
        """Her send: her copy filed in Sent, and wolf's copy in his inbox when he is a recipient."""
        self.sent.add(message, u(2026, 11, 27, 12))
        if WOLF in recipients:
            self.wolf.deliver(message)

    def end_to_end(self):
        """The env file with every setting the end-to-end test needs; her sends reach Sent and wolf."""
        self.write_env(E2E_TEXT)
        self.jmap.on_deliver = self.delivers

    def names(self):
        """The step name of each line, the trace's last line left out (it names a folder, with "/"
        between its parts on every OS)."""
        self.assertTrue(self.lines[-1].startswith("Live trace kept in traces/live-smoke-"), self.lines[-1])
        self.assertNotIn("\\", self.lines[-1])
        return [line.split(": ", 1)[0] for line in self.lines[:-1]]

    def trace(self):
        (name,) = os.listdir(self.traces)
        return os.path.join(self.traces, name)

    def manifest(self):
        with open(os.path.join(self.trace(), "manifest.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def deliver_to_sent(self, sender, recipients, message):
        self.sent.add(message, u(2026, 11, 27, 12))

    def late_copy(self, wait_s):
        """The copy reaches Sent only during the wait between the looks, which takes wait_s on a
        clock the smoke reads (smoke._clock)."""
        held, clock = [], [1000.0]
        self.jmap.on_deliver = lambda sender, recipients, message: held.append(message)

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
        for value in (PASSWORD, basic, TOKEN, DORIS, "doris.htm", "whizz", "Sent Items", COLLECTION,
                      "owner-shared-cal", "127.0.0.1", "doris-", WOLF_TOKEN, WOLF_PASSWORD, API_KEY, LINK, OWNER_CALENDAR):
            self.assertNotIn(value, text)


class SmokeTest(SmokeFixture):

    def test_every_step_passes_against_working_servers_with_one_plain_line_each(self):
        self.end_to_end()
        self.assertEqual(self.run_smoke("--send-to", "whizz@fastmail.com"), 0)
        self.assertEqual(self.names(), STEPS + SEND_STEPS + E2E_STEPS + ["smoke"])
        self.assertEqual(self.result("imap login and inbox count"), "ok (2 messages in INBOX)")
        self.assertEqual(self.result("sent folder found by its special-use flag"), "ok (1 folder)")
        self.assertEqual(self.result(NAME), "ok (name read)")
        self.assertEqual(self.result("caldav query, next 7 days"), "ok (0 events)")
        looks_after_a_find = {"test request in her INBOX, look 2 (after another wait)",
                              "her reply in wolf's mailbox, look 2 (after a wait)"}
        for step in STEPS + SEND_STEPS + E2E_STEPS:
            self.assertTrue(self.result(step).startswith("not needed (found on look 1)" if step in looks_after_a_find
                                                         else "ok"), self.lines)
        self.assertEqual(self.result("smoke"), "ok, every step passed")
        self.assertEqual(self.lines[-1], "Live trace kept in %s: outcome pass."
                         % os.path.relpath(self.trace(), self.repo).replace(os.sep, "/"))
        self.assert_no_value()

    def test_without_send_to_the_send_is_named_as_not_run_and_never_every_step_passed(self):
        self.assertEqual(self.run_smoke(), 0)
        self.assertEqual(self.names(), STEPS + [SEND_NOT_RUN, E2E_NOT_RUN, "smoke"])
        self.assertEqual(self.result(SEND_NOT_RUN), "not run (no --send-to)")
        self.assertEqual(self.result(E2E_NOT_RUN), "not run (no --send-to)")
        self.assertEqual(self.result("smoke"), "every step that ran passed; not run: " + NO_SEND_TO)
        self.assertFalse(any("every step passed" in line for line in self.lines), self.lines)
        self.assert_no_value()

    def test_a_failed_smoke_still_names_what_did_not_run(self):
        self.dav.status["PUT"] = 403
        self.assertEqual(self.run_smoke(), 1)
        self.assertEqual(self.result("smoke"), "FAILED; not run: " + NO_SEND_TO)

    def test_without_send_to_nothing_is_sent_and_imap_writes_only_the_smoke_keyword(self):
        self.run_smoke()
        self.assertEqual(self.jmap.kinds(), ["session", "setup"])          # her session, mailboxes, identity
        self.assertEqual(self.jmap.delivered, [])
        self.assertEqual({r[3]["authorization"] for r in self.jmap.requests}, {"Bearer " + TOKEN})
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
        self.assertEqual(self.result("smoke"), "FAILED; not run: " + NO_SEND_TO)
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
        self.assertTrue(self.result("jmap session, nothing sent").startswith("ok"))
        self.assertTrue(self.result("caldav read after delete, gone").startswith("ok"))
        self.assertEqual(self.result("smoke"), "FAILED; not run: " + NO_SEND_TO)
        self.assert_no_value()

    def test_an_event_read_back_with_other_times_fails(self):
        real_put = self.dav.put
        self.dav.put = lambda name, data: real_put(name, re.sub(rb"DTSTART:(\d{8})T\d{6}Z", rb"DTSTART:\1T000000Z", data))
        self.assertEqual(self.run_smoke(), 1)
        self.assertEqual(self.result("caldav read test event back, times match"), "FAILED (times differ)")

    def test_a_refused_login_fails_every_mail_step_without_a_secret(self):
        self.imap.password = "dummy-other-pass"
        self.jmap.token = "dummy-another-jmap-token-that-is-long-enough"
        self.assertEqual(self.run_smoke(), 1)
        self.assertTrue(self.result("imap login and inbox count").startswith("MailError (imap inbox count login"))
        self.assertEqual(self.result("jmap session, nothing sent"), "MailError (jmap session: 401)")
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
        self.assertEqual((self.imap.commands, self.jmap.requests, self.dav.requests), ([], [], []))

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
        real_imap, real_jmap = live._imap_tls, jmap._https
        self.jmap.on_deliver = lambda sender, recipients, message: self.sent.add(message, u(2026, 11, 27, 12))
        self.dav.on_request = probe
        with mock.patch.object(live, "_imap_tls", lambda *a: (probe(), real_imap(*a))[1]), \
                mock.patch.object(jmap, "_https", lambda *a: (probe(), real_jmap(*a))[1]):
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
    """The display name step proves the calendar URL names a calendar that has a name, and its line
    says only that a name was read: the name (the owner's calendar can carry the owner's full name)
    is in no line printed, so in no terminal.log, timeline or manifest of a trace kept in a public repo."""

    def test_the_line_says_only_that_a_name_was_read_and_no_trace_file_holds_the_name(self):
        self.dav.display_name = OWNER_FULL_NAME
        self.assertEqual(self.run_smoke(), 0)
        self.assertEqual(self.result(NAME), "ok (name read)")
        self.assertNotIn(OWNER_FULL_NAME, "\n".join(self.lines))
        for name in sorted(os.listdir(self.trace())):
            with open(os.path.join(self.trace(), name), "rb") as fh:
                data = fh.read()
            self.assertNotIn(OWNER_FULL_NAME.encode("utf-8"), data, name)
            self.assertNotIn(b"Dummy", data, name)

    def test_a_name_with_a_line_end_or_a_direction_mark_still_makes_one_plain_line(self):
        self.dav.display_name = "cal\nsmoke: ok, every step passed\r\t\u202edesrever"
        self.assertEqual(self.run_smoke(), 0)
        self.assertEqual(self.result(NAME), "ok (name read)")
        self.assertEqual(len(self.lines), len(STEPS) + 4)          # the two not run, the last line, the trace's
        self.assertFalse(any("desrever" in line or "\u202e" in line for line in self.lines))

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
                                               "7 days); %s" % (FILE, NO_SEND_TO))
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
        self.jmap.on_deliver = self.deliver_to_sent
        self.assertEqual(self.run_smoke("--send-to", "whizz@fastmail.com"), 0)
        self.assertEqual(self.names(), STEPS + SEND_STEPS + [E2E_NOT_RUN, "smoke"])
        self.assertEqual(self.result(E2E_NOT_RUN), "not run (no WOLF_EMAIL and WOLF_JMAP_TOKEN in the env file)")
        (sender, recipients, data), = self.jmap.delivered
        self.assertEqual((sender, recipients), (DORIS, ["whizz@fastmail.com"]))
        msg = email.message_from_bytes(data, policy=email.policy.default)
        self.assertEqual((msg["From"], msg["To"], msg["Bcc"]), (DORIS, "whizz@fastmail.com", None))
        self.assertEqual(msg["Auto-Submitted"], "auto-generated")
        self.assertEqual(msg.get_content_type(), "text/plain")
        self.assertIn("I'm an AI, not a person.", msg.get_content())
        self.assertEqual(self.result("jmap send one test message"), "ok")
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
        self.assertEqual(len(self.jmap.delivered), 1)
        self.assertEqual(self.result("sent copy, look 1 (at once)"), "not found yet")
        self.assertEqual(self.result("sent copy, look 2 (after a short wait)"), "not found")
        self.assertEqual(self.result("sent copy found"), "FAILED (not found on either look)")
        self.assertEqual(self.result("sent copy record for a live run"), "not written (the copy was not found)")
        self.assertEqual(self.result("smoke"), "FAILED; not run: %s (no WOLF_EMAIL and WOLF_JMAP_TOKEN in the env file)" % E2E_NOT_RUN)

    def test_a_look_that_cannot_tell_is_named_and_the_other_look_still_counts(self):
        self.jmap.on_deliver = self.deliver_to_sent
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
        self.jmap.on_deliver = self.deliver_to_sent
        self.assertEqual(self.run_smoke("--send-to", " Wolf.Whizz@FastMail.com "), 0)
        self.assertEqual(self.jmap.delivered[0][1], ["wolf.whizz@fastmail.com"])

    def test_any_other_address_is_refused_before_any_connection(self):
        for bad in ("someone@example.com", "whizz@fastmail.com.example.net", "whizz@fastmail.com, x@example.com",
                    "<whizz@fastmail.com>", ""):
            self.lines.clear()
            self.assertEqual(self.run_smoke("--send-to", bad), 2, bad)
            self.assertEqual(len(self.lines), 1)
            self.assertTrue(self.lines[0].startswith("smoke: refused --send-to"), self.lines)
        self.assertEqual((self.imap.commands, self.jmap.requests, self.dav.requests), ([], [], []))


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
        self.jmap.on_deliver = self.deliver_to_sent
        self.assertEqual(self.run_smoke("--send-to", "whizz@fastmail.com"), 0)
        now = dt.datetime.now(dt.timezone.utc)
        self.assertIsNone(loop.smoke_refusal(paths.state_dir("live"), now))
        self.assertEqual(self.preflight().doris_addr, DORIS)      # not refused

    def test_the_record_this_smoke_writes_goes_stale_for_a_live_run_a_day_later(self):
        self.jmap.on_deliver = self.deliver_to_sent
        self.assertEqual(self.run_smoke("--send-to", "whizz@fastmail.com"), 0)
        later = dt.datetime.now(dt.timezone.utc).replace(microsecond=0) + caps.SMOKE_RECORD_MAX_AGE
        with mock.patch.object(loop, "_wall_now", lambda: later + dt.timedelta(seconds=1)):
            with self.assertRaises(loop.Refused) as cm:
                self.preflight()
        self.assertEqual(str(cm.exception), "Not started: the smoke check's record (smoke-sent.json) is more than "
                                            "24 hours old. " + loop.SMOKE_FIRST)

    def test_after_a_smoke_that_did_not_find_its_copy_a_live_run_is_refused(self):
        os.makedirs(paths.state_dir("live"), exist_ok=True)
        sentrecord.write(self.path(), 1, 0.1, dt.datetime.now(dt.timezone.utc))      # an earlier smoke's, fresh
        self.assertIsNone(loop.smoke_refusal(paths.state_dir("live"), dt.datetime.now(dt.timezone.utc)))
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
        self.jmap.on_deliver = self.deliver_to_sent
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
        self.assertEqual((self.imap.commands, self.jmap.requests, self.dav.requests), ([], [], []))

    def test_a_record_it_cannot_write_fails_the_smoke_and_leaves_nothing_behind(self):
        self.jmap.on_deliver = self.deliver_to_sent
        with mock.patch.object(smoke.os, "replace", side_effect=PermissionError(13, "denied")):
            self.assertEqual(self.run_smoke("--send-to", "whizz@fastmail.com"), 1)
        self.assertEqual(self.result("sent copy found")[:2], "ok")
        self.assertEqual(self.result("sent copy record for a live run"), "PermissionError")
        self.assertEqual(self.result("smoke"), "FAILED; not run: %s (no WOLF_EMAIL and WOLF_JMAP_TOKEN in the env file)" % E2E_NOT_RUN)
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
        self.assertEqual((self.imap.commands, self.jmap.requests, self.dav.requests), ([], [], []))

    def test_a_jmap_token_missing_or_malformed_exits_2_naming_it_never_its_value(self):
        for line, said in (("", "DORIS_JMAP_TOKEN is not set"),
                           ("DORIS_JMAP_TOKEN=dummy-short-token\n", "DORIS_JMAP_TOKEN is too short to be an API token"),
                           ("DORIS_JMAP_TOKEN='%s x'\n" % TOKEN, "DORIS_JMAP_TOKEN has a space")):
            self.lines.clear()
            self.write_env(ENV_TEXT.replace("DORIS_JMAP_TOKEN=" + TOKEN + "\n", line))
            self.assertEqual(self.run_smoke("--send-to", "whizz@fastmail.com"), 2)
            (only,) = self.lines
            self.assertTrue(only.startswith("config: ConfigError (" + said), only)
            self.assertNotIn(TOKEN, only)
            self.assertNotIn("dummy-short-token", only)
        self.assertEqual((self.imap.commands, self.jmap.requests, self.dav.requests), ([], [], []))

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
        self.assertEqual(hosts, ["127.0.0.1", "api.fastmail.com"])
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


REQUEST_MID = re.compile(r"^<doris-smoke-test\.[0-9a-f]{32}@fastmail\.com>\Z")
SHA = "fedcba9876543210" * 2 + "fedcba98"


class EndToEndTest(SmokeFixture):
    """With --send-to and wolf's settings: wolf's request to the owner reaches her INBOX by his
    forward; her real loop answers it, live, on a throwaway ledger and on that one message; her reply
    is confirmed in her Sent and found in wolf's mailbox. Each step is one plain line."""

    def run_e2e(self):
        self.end_to_end()
        return self.run_smoke("--send-to", "whizz@fastmail.com")

    def request(self):
        (sender, rcpts, data), = self.wolf.delivered
        return sender, rcpts, email.message_from_bytes(data, policy=email.policy.default)

    def smoke_folder(self):
        (stamp,) = os.listdir(os.path.join(paths.state_dir("live"), "smoke"))
        return os.path.join(paths.state_dir("live"), "smoke", stamp)

    def test_wolfs_request_reaches_her_and_her_reply_reaches_wolf(self):
        self.assertEqual(self.run_e2e(), 0)
        sender, rcpts, msg = self.request()
        self.assertEqual((sender, rcpts), (WOLF, [OWNER]))                      # to the owner, never to her
        self.assertEqual((msg["From"], msg["To"], msg["Subject"]), (WOLF, OWNER, smoke.REQUEST_SUBJECT))
        self.assertRegex(msg["Message-ID"], REQUEST_MID)
        self.assertIsNone(msg["Auto-Submitted"])                                # a person's request, as she sees it
        self.assertEqual(msg.get_content_type(), "text/plain")
        (uid,) = [uid for uid, m in self.inbox.messages.items() if msg["Message-ID"].encode() in m["raw"]]
        self.assertIn(live.SMOKE_TEST_KEYWORD, self.inbox.messages[uid]["flags"])   # no live run reads it
        test_message, (her_sender, her_rcpts, her_data) = self.jmap.delivered
        self.assertEqual((her_sender, her_rcpts), (DORIS, [WOLF, OWNER]))      # the owner's copy on the envelope
        reply = email.message_from_bytes(her_data, policy=email.policy.default)
        self.assertEqual((reply["From"], reply["To"], reply["In-Reply-To"]), (DORIS, WOLF, msg["Message-ID"]))
        (copy,) = [e for e in self.wolf.emails.values() if "mb-inbox" in e["mailboxIds"]]   # his own: in Sent
        self.assertEqual(copy["bytes"], her_data)
        (url, headers, body), = self.posts                                       # the live model, her key
        self.assertEqual((headers["x-api-key"], body["model"]), (API_KEY, model_mod.DEFAULT_MODEL))
        self.assertEqual(self.result("her loop on the test request"),
                         "ok (1 handled, 0 held, 1 reply sent, 0 error lines, model cost $0.0024)")
        self.assertRegex(self.result("test request in her INBOX, look 1 (after a wait)"),
                         r"^ok \(found, \d+\.\d s after wolf's send\)\Z")
        self.assertRegex(self.result("her reply in wolf's mailbox, look 1 (at once)"),
                         r"^ok \(found in inbox, \d+\.\d s after her loop ended\)\Z")
        self.assert_no_value()

    def test_her_loop_runs_on_a_throwaway_ledger_and_touches_nothing_of_a_live_run(self):
        self.assertEqual(self.run_e2e(), 0)
        folder = self.smoke_folder()
        with ledger_mod.Ledger(os.path.join(folder, "ledger.sqlite"), create=False) as db:
            self.assertEqual([row[0] for row in db._all("SELECT state FROM sends")], ["confirmed"])
            self.assertLess(db.fresh_start(), dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=59))
        self.assertFalse(os.path.exists(os.path.join(paths.state_dir("live"), "ledger.sqlite")))
        (run_name,) = os.listdir(os.path.join(folder, "runs"))
        self.assertRegex(run_name, r"^model-live-live-smoke-\d{8}T\d{6}Z\Z")
        lines = receipts.read_lines(os.path.join(folder, "runs", run_name, "receipts.jsonl"))
        observed = [l for l in lines if l["kind"] == "observed" and l["payload"].get("step") != "start"]
        self.assertEqual(len(observed), 1)                                       # that one message alone
        self.assertEqual([r[1] for r in self.dav.requests if r[0] in ("PUT", "DELETE")],
                         [COLLECTION + smoke.SMOKE_BOOKING_ID + ".ics"] * 2)     # the round trip's own event only
        self.assertEqual({r[3]["authorization"] for r in self.jmap.requests}, {"Bearer " + TOKEN})
        self.assertEqual({r[3]["authorization"] for r in self.wolf.requests}, {"Bearer " + WOLF_TOKEN})
        fetches = [args for verb, args in self.imap.commands if verb == "UID SEARCH" and "HEADER" in args]
        self.assertTrue(any(args.startswith("SINCE") and "KEYWORD " + live.SMOKE_TEST_KEYWORD in args
                            for args in fetches), fetches)

    def test_a_later_live_run_on_its_own_ledger_never_reads_the_smokes_request(self):
        """Saturday's run, on a ledger of its own and reading from before the smoke, reads her INBOX
        as every live run does: the smoke's request is not among what it reads, with the smoke's mark
        or without it (a smoke cut short, or whose mark failed, before the keyword landed)."""
        self.assertEqual(self.run_e2e(), 0)
        _, _, request = self.request()
        self.assertTrue(request["Message-ID"].startswith("<" + live.SMOKE_MID_MARK), request["Message-ID"])
        later = live.LiveMail(envfile.load(self.env_path, names=loop._her_names()), DORIS)
        later.since_utc = self.now - dt.timedelta(days=1)
        for marked in (True, False):
            if not marked:
                for message in self.inbox.messages.values():
                    message["flags"].discard(live.SMOKE_TEST_KEYWORD)
            with self.subTest(marked=marked):
                got = later.fetch_new()
                self.assertEqual([item.raw for item in got], [eml(1), eml(2)])   # the request is not handed over

    def test_the_owners_forward_found_on_the_second_look(self):
        self.forward_late = True
        sleeps = []

        def wait(seconds):
            sleeps.append(seconds)
            if sleeps.count(7.0) == 2:                                           # during the second wait for it
                while self.held:
                    self.inbox.add(self.held.pop(), dt.datetime.now(dt.timezone.utc))
        with mock.patch.object(smoke, "FORWARD_WAIT_S", 7.0), mock.patch.object(smoke, "_sleep", wait):
            self.assertEqual(self.run_e2e(), 0)
        self.assertEqual([w for w in sleeps if w], [7.0, 7.0])
        self.assertEqual(self.result("test request in her INBOX, look 1 (after a wait)"), "not found yet")
        self.assertTrue(self.result("test request in her INBOX, look 2 (after another wait)").startswith("ok (found"))
        self.assertEqual(self.result("smoke"), "ok, every step passed")

    def test_a_forward_that_never_comes_fails_the_smoke_and_her_loop_never_runs(self):
        self.forward_late = True
        self.assertEqual(self.run_e2e(), 1)
        self.assertEqual(self.result("test request in her INBOX, look 2 (after another wait)"),
                         "FAILED (not found on either look)")
        rest = "test request marked as the smoke's own and the steps after it"
        self.assertEqual(self.result(rest), "not run (the request was not found in her INBOX)")
        self.assertEqual(self.names(), STEPS + SEND_STEPS + E2E_STEPS[:3] + [rest, "smoke"])
        self.assertEqual(self.posts, [])
        self.assertEqual(len(self.jmap.delivered), 1)                            # the --send-to test message only
        self.assertEqual(self.result("smoke"), "FAILED; not run: %s (the request was not found in her INBOX)" % rest)
        manifest = self.manifest()
        self.assertEqual((manifest["outcome"], manifest["first_failing_step"]),
                         ("fail", "test request in her INBOX, look 2 (after another wait)"))
        self.assertEqual(manifest["receipt_chain"]["reason"], "no loop ran")

    def test_a_reply_that_never_reaches_wolf_fails_the_smoke(self):
        self.end_to_end()
        self.jmap.on_deliver = lambda sender, recipients, message: self.sent.add(message, u(2026, 11, 27, 12))
        self.assertEqual(self.run_smoke("--send-to", "whizz@fastmail.com"), 1)
        self.assertEqual(self.result("her reply confirmed in her Sent"), "ok")
        self.assertEqual(self.result("her reply in wolf's mailbox, look 1 (at once)"), "not found yet")
        self.assertEqual(self.result("her reply in wolf's mailbox, look 2 (after a wait)"),
                         "FAILED (not found on either look)")
        self.assertEqual(self.manifest()["first_failing_step"], "her reply in wolf's mailbox, look 2 (after a wait)")

    def test_a_halt_that_lands_during_the_smoke_stops_her_loop_before_it_reads_anything(self):
        def forward_then_stop(sender, recipients, message):
            self.forward(sender, recipients, message)
            supervisor.request_stop("live", "cli")
        self.wolf.on_deliver = forward_then_stop
        self.assertEqual(self.run_e2e(), 1)
        self.assertEqual(self.result("her loop on the test request"),
                         "Refused (Halted (cli). Only `python -m doris resume` clears it.)")
        self.assertEqual(self.result("her reply confirmed in her Sent and the steps after it"),
                         "not run (her loop sent no reply)")
        self.assertEqual((self.posts, len(self.jmap.delivered)), ([], 1))

    def test_without_wolfs_settings_or_send_to_or_the_record_it_does_not_run_and_sends_nothing_from_wolf(self):
        no_wolf = E2E_TEXT.replace("WOLF_EMAIL=" + WOLF + "\n", "").replace("WOLF_JMAP_TOKEN=" + WOLF_TOKEN + "\n", "")
        bad_token = E2E_TEXT.replace(WOLF_TOKEN, "dummy-short")
        cases = (("no wolf", no_wolf, ["--send-to", "whizz@fastmail.com"], 0,
                  "no WOLF_EMAIL and WOLF_JMAP_TOKEN in the env file"),
                 ("no --send-to", bad_token, [], 0, "no --send-to"),       # not run, so not even checked
                 ("no record", E2E_TEXT, ["--send-to", "whizz@fastmail.com"], 1,
                  "no sent copy record from this smoke: her loop answers only after a smoke found its copy in Sent"))
        for case, text, args, code, why in cases:
            with self.subTest(case=case):
                self.lines.clear()
                self.write_env(text)
                self.jmap.on_deliver = None if case == "no record" else self.delivers
                self.assertEqual(self.run_smoke(*args), code, self.lines)
                self.assertEqual(self.result(E2E_NOT_RUN), "not run (%s)" % why)
                self.assertEqual((self.wolf.requests, self.posts), ([], []))
                self.assertFalse(os.path.exists(os.path.join(paths.state_dir("live"), "smoke")))

    def test_its_settings_are_checked_before_anything_is_contacted(self):
        def without(name):
            return "\n".join(l for l in E2E_TEXT.splitlines() if not l.startswith(name + "=")) + "\n"
        cases = ((without("WOLF_JMAP_TOKEN"), "config: ConfigError (WOLF_JMAP_TOKEN is not set)"),
                 (without("WOLF_EMAIL"), "config: ConfigError (WOLF_EMAIL is not set)"),
                 (E2E_TEXT.replace(WOLF_TOKEN, "dummy-short-wolf"),
                  "config: ConfigError (WOLF_JMAP_TOKEN is too short to be an API token"),
                 (E2E_TEXT.replace("WOLF_EMAIL=" + WOLF, "WOLF_EMAIL=someone@example.com"),
                  "config: ConfigError (WOLF_EMAIL must be one of the two test inboxes --send-to takes (her reply "
                  "goes there))"),
                 (E2E_TEXT.replace("WOLF_EMAIL=" + WOLF, "WOLF_EMAIL=" + OWNER),
                  "config: ConfigError (WOLF_EMAIL must be the test account's own address, not hers or the owner's)"),
                 (without("ANTHROPIC_API_KEY"), "config: ConfigError (ANTHROPIC_API_KEY is not set)"),
                 (without("DORIS_MEETING_LINK"), "config: ConfigError (DORIS_MEETING_LINK is not set)"))
        for text, said in cases:
            with self.subTest(said=said):
                self.lines.clear()
                self.write_env(text)
                self.assertEqual(self.run_smoke("--send-to", "whizz@fastmail.com"), 2)
                (only,) = self.lines
                self.assertTrue(only.startswith(said), only)
                self.assert_no_value()
                self.assertNotIn("dummy-short-wolf", only)
        self.assertEqual((self.imap.commands, self.jmap.requests, self.wolf.requests, self.dav.requests),
                         ([], [], [], []))
        self.assertFalse(os.path.exists(self.traces))

    def test_the_guard_allows_the_model_host_only_for_the_end_to_end_test(self):
        installed = []
        real_install = netguard.install

        def spy(hosts=()):
            guard = real_install(hosts)
            installed.append((list(hosts), guard))
            return guard
        with mock.patch.object(netguard, "install", spy):
            self.assertEqual(self.run_e2e(), 0)
        smoke_hosts, guard = installed[0]
        self.assertEqual(smoke_hosts, ["127.0.0.1", "api.fastmail.com", "api.anthropic.com"])
        self.assertEqual(list(installed[1][0]), ["127.0.0.1", "api.fastmail.com", "api.anthropic.com"])  # her loop's
        self.assertEqual(guard.refused(), [])
        self.assertIsNone(netguard.active())

    def test_both_senders_reach_the_host_their_session_names_only_once_checked(self):
        """Fastmail's real session names apiUrl and uploadUrl on the account's own host below
        api.fastmail.com (phl.api.fastmail.com). The smoke's guard and her loop's allow it only once a
        sender checked its session: wolf's (his request, then the looks for her reply) and hers (the
        smoke's send, then her loop's reply). Each connection meets the guards under its host's name."""
        installed = []
        real_install = netguard.install

        def spy(hosts=()):
            installed.append(real_install(hosts))
            return installed[-1]
        with mock.patch.object(netguard, "install", spy):
            self.assertEqual(self.run_e2e(), 0)
        smoke_guard, loop_guard = installed
        for guard in installed:
            self.assertNotIn("phl.api.fastmail.com", guard.allowed)
            self.assertEqual(guard.admitted, {"phl.api.fastmail.com"})
            self.assertEqual(guard.refused(), [])
        fastmail = [(a["host"], a["port"]) for a in smoke_guard.attempts
                    if a["call"] == "create_connection" and a["host"] != "127.0.0.1"]
        self.assertEqual(fastmail[0], ("api.fastmail.com", 443))
        self.assertEqual(set(fastmail), {("api.fastmail.com", 443), ("phl.api.fastmail.com", 443)})
        self.assertEqual(self.wolf.kinds().count("send"), 1)                 # his request went
        self.assertIn("find", self.wolf.kinds())                            # and his looks for her reply
        self.assertEqual(self.jmap.kinds().count("send"), 2)                 # the smoke's test message, her reply

    def test_a_session_naming_a_host_outside_api_fastmail_com_fails_its_step_and_allows_nothing(self):
        self.jmap.as_fastmail("https://evil-api.fastmail.com.attacker")
        installed = []
        real_install = netguard.install

        def spy(hosts=()):
            installed.append(real_install(hosts))
            return installed[-1]
        with mock.patch.object(netguard, "install", spy):
            self.assertEqual(self.run_smoke(), 1)
        self.assertEqual(self.result("jmap session, nothing sent"),
                         "MailError (jmap session: apiUrl is not on api.fastmail.com or a host below it, "
                         "over the session's own scheme and port)")
        (guard,) = installed
        self.assertEqual(guard.admitted, set())
        self.assertEqual(set(self.jmap.kinds()), {"session"})


class SmokeTraceTest(SmokeFixture):
    """Every smoke that contacted anything keeps traces/live-smoke-<UTC stamp>/, pass or fail, cut
    short included: her loop's receipts as written (when it ran), manifest.json, timeline.txt and
    terminal.log, every line printed. A smoke refused before anything was contacted keeps none."""

    def setUp(self):
        super().setUp()
        os.makedirs(os.path.join(self.repo, ".git"))
        with open(os.path.join(self.repo, ".git", "HEAD"), "w", encoding="ascii") as fh:
            fh.write(SHA + "\n")

    def read(self, name):
        with open(os.path.join(self.trace(), name), "rb") as fh:
            return fh.read()

    def test_a_smoke_that_passed_keeps_her_loops_receipts_as_written_and_every_line(self):
        self.end_to_end()
        self.assertEqual(self.run_smoke("--send-to", "whizz@fastmail.com"), 0)
        folder = self.trace()
        self.assertRegex(os.path.basename(folder), r"^live-smoke-\d{8}T\d{6}Z\Z")
        self.assertEqual(sorted(os.listdir(folder)), ["manifest.json", "receipts.jsonl", "terminal.log", "timeline.txt"])
        smoke_dir = os.path.join(paths.state_dir("live"), "smoke")
        (stamp,) = os.listdir(smoke_dir)
        self.assertEqual("live-smoke-" + stamp, os.path.basename(folder))       # one start, one stamp
        run_name = "model-live-live-smoke-" + stamp
        with open(os.path.join(smoke_dir, stamp, "runs", run_name, "receipts.jsonl"), "rb") as fh:
            self.assertEqual(self.read("receipts.jsonl"), fh.read())
        manifest = self.manifest()
        self.assertEqual({k: manifest[k] for k in ("mode", "kind", "commit", "model", "cost_usd", "outcome",
                                                     "first_failing_step", "run_name")},
                         {"mode": "live", "kind": "smoke", "commit": SHA, "model": model_mod.DEFAULT_MODEL,
                          "cost_usd": 0.0024, "outcome": "pass", "first_failing_step": None, "run_name": run_name})
        self.assertEqual(manifest["counts"], {"messages": 1, "model_calls": 1, "sends": 1, "errors": 0, "handled": 1,
                                              "held": 0, "human_approvals": 0})
        self.assertTrue(manifest["receipt_chain"]["ok"])
        timeline = self.read("timeline.txt").decode("utf-8")
        for said in ("  wolf sends one meeting request to the owner: ok", "her loop's receipts (run %s):" % run_name,
                     "send T", "her copy in Sent: found", "summary: 1 handled, 0 held, 1 sent"):
            self.assertIn(said, timeline)
        self.assertEqual(self.read("terminal.log").decode("utf-8"), "".join(line + "\n" for line in self.lines))
        self.assertEqual(self.lines[-1], "Live trace kept in traces/%s: outcome pass." % os.path.basename(folder))

    def test_a_smoke_that_failed_keeps_its_trace_naming_the_first_failing_step(self):
        self.dav.status["PUT"] = 403
        self.assertEqual(self.run_smoke(), 1)
        manifest = self.manifest()
        self.assertEqual((manifest["kind"], manifest["outcome"], manifest["first_failing_step"]),
                         ("smoke", "fail", "caldav create test event"))
        self.assertEqual(manifest["receipt_chain"], {"ok": None, "lines": 0, "reason": "no loop ran"})
        self.assertEqual(sorted(os.listdir(self.trace())), ["manifest.json", "terminal.log", "timeline.txt"])
        self.assertEqual(self.lines[-1], "Live trace kept in traces/%s: outcome fail (first failing step: caldav "
                                         "create test event)." % os.path.basename(self.trace()))

    def test_a_smoke_where_every_step_that_ran_passed_but_one_did_not_run_is_a_fail(self):
        self.assertEqual(self.run_smoke(), 0)
        self.assertEqual(self.manifest()["first_failing_step"], "not run: %s (no --send-to)" % SEND_NOT_RUN)

    def test_a_smoke_cut_short_keeps_its_trace_too(self):
        with mock.patch.object(live.LiveCalendar, "query", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_smoke()
        manifest = self.manifest()
        self.assertEqual((manifest["outcome"], manifest["first_failing_step"]),
                         ("fail", "cut short after the step: caldav calendar display name"))
        self.assertEqual(self.read("terminal.log").decode("utf-8"), "".join(line + "\n" for line in self.lines))

    def test_a_smoke_refused_before_anything_was_contacted_keeps_none(self):
        supervisor.request_stop("live", "cli")
        self.assertEqual(self.run_smoke(), supervisor.EXIT_REFUSED_HALT)
        os.remove(supervisor.halt_file("live"))
        held = supervisor.acquire_lock(os.path.join(paths.state_dir("live"), supervisor.LOCK_NAME))
        self.assertEqual(self.run_smoke(), supervisor.EXIT_LOCKED)
        held.release()
        self.write_env(ENV_TEXT.replace("DORIS_CALDAV_URL=", "# DORIS_CALDAV_URL="))
        self.assertEqual(self.run_smoke(), 2)
        os.makedirs(sentrecord.path(paths.state_dir("live")))           # an old record it cannot remove
        self.write_env(ENV_TEXT)
        self.assertEqual(self.run_smoke(), 1)
        self.assertFalse(os.path.exists(self.traces))
        self.assertEqual((self.imap.commands, self.jmap.requests, self.dav.requests), ([], [], []))


class TokenScanTest(SmokeFixture):
    """The brief's (6) and scope D: a whole smoke, end-to-end test included, with a made-up value for
    every setting wide's env file holds. No token, password, key, link or calendar value reaches any
    file the smoke writes (state and trace alike), the lines it prints, or a request's body; the
    trace's own files name no address either."""

    def test_no_planted_value_reaches_a_file_a_line_or_a_request_body(self):
        self.end_to_end()
        self.assertEqual(self.run_smoke("--send-to", "whizz@fastmail.com"), 0)
        secrets = [PASSWORD, TOKEN, WOLF_TOKEN, WOLF_PASSWORD, API_KEY, LINK, OWNER_CALENDAR, COLLECTION]
        secrets += [base64.b64encode(v.encode()).decode() for v in (PASSWORD, TOKEN, WOLF_TOKEN, API_KEY)]
        secrets.append(base64.b64encode(("%s:%s" % (DORIS, PASSWORD)).encode()).decode())
        found = []
        for folder, _, names in os.walk(self.dir):
            for name in names:
                path = os.path.join(folder, name)
                if path == self.env_path:
                    continue
                with open(path, "rb") as fh:
                    data = fh.read()
                found += [(os.path.relpath(path, self.dir), v) for v in secrets if v.encode() in data]
                if os.path.dirname(path) == self.trace() and name != "receipts.jsonl":
                    found += [(os.path.relpath(path, self.dir), v) for v in (DORIS, OWNER, WOLF) if v.encode() in data]
        text = "\n".join(self.lines)
        found += [("printed", v) for v in secrets + [DORIS, OWNER, WOLF] if v in text]
        for server in (self.jmap, self.wolf):
            for kind, method, path, headers, body in server.requests:
                found += [(kind, v) for v in secrets if v.encode() in body]
        self.assertEqual(found, [])
        self.assertGreater(len(os.listdir(self.trace())), 3)


if __name__ == "__main__":
    unittest.main()
