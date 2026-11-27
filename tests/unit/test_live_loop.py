"""Live mode in the loop: her settings from the env file alone, the live ledger's fresh start, the
live adapters under the network guard, and the meeting link and the model key from the env file.

The real LiveMail and LiveCalendar talk to test_live's fake IMAP, SMTP and CalDAV servers on
127.0.0.1 (the adapters' default transports swapped for plain connections to them), as in
test_smoke. No test reads a real env file or reaches a real server.
"""
import contextlib
import email
import email.policy
import http.client
import imaplib
import io
import json
import os
import re
import shutil
import smtplib
import sys
import tempfile
import traceback
import unittest
from unittest import mock

from doris import __main__ as cli
from doris import caps, ledger as ledger_mod
from doris import loop, model as model_mod, netguard, paths, receipts, smoke, supervisor, templates
from doris.adapters import live
from tests.unit.test_live import COLLECTION, DORIS, OWNER, PASSWORD, Box, FakeCalDAV, FakeIMAP, FakeSMTP, u

LINK = "https://meet.example/" + "live-loop-room"
API_KEY = "dummy-anthropic-key-for-live-loop-tests"
SETTINGS = (("DORIS_MAIL_ADDRESS", DORIS), ("DORIS_APP_PASSWORD", PASSWORD), ("DORIS_OWNER_ADDRESS", OWNER),
            ("DORIS_IMAP_HOST", "127.0.0.1"), ("DORIS_SMTP_HOST", "127.0.0.1"),
            ("DORIS_CALDAV_URL", "https://127.0.0.1" + COLLECTION), ("DORIS_MEETING_LINK", LINK),
            ("ANTHROPIC_API_KEY", API_KEY), ("DORIS_MODEL", ""))
VALUES = (PASSWORD, LINK, API_KEY, DORIS, OWNER)          # none of these is ever printed


def env_text(**overrides):
    lines = ["# a test env file: every host is the fakes' 127.0.0.1"]
    for name, value in SETTINGS:
        value = overrides.pop(name, value)
        if value is not None:
            lines.append("%s=%s" % (name, value))
    lines += ["%s=%s" % item for item in overrides.items()]
    return "\n".join(lines) + "\n"


class EnvFolder(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="live-loop-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.env_path = self.write_env()

    def write_env(self, **overrides):
        path = os.path.join(self.dir, "test.env")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(env_text(**overrides))
        return path

    def assert_no_value(self, text, *more):
        for value in VALUES + more:
            self.assertNotIn(value, text)


class LiveSettingsTest(EnvFolder):
    """loop.live_settings: everything live needs, from the env file alone, checked before a connection."""

    def test_every_setting_comes_from_the_env_file_and_never_from_the_environment(self):
        elsewhere = {"DORIS_MEETING_LINK": "https://meet.example/" + "shell-room", "DORIS_MODEL": "claude-other",
                     "ANTHROPIC_API_KEY": "dummy-shell-key-not-this-one", "DORIS_OWNER_ADDRESS": "other@example.com"}
        with mock.patch.dict(os.environ, elsewhere):
            s = loop.live_settings(self.env_path, "live")
        self.assertEqual((s.owner_addr, s.doris_addr, s.meeting_link), (OWNER, DORIS, LINK))
        self.assertEqual(s.hosts, ("127.0.0.1",))
        self.assertEqual(s.model_id, model_mod.DEFAULT_MODEL)
        self.assertEqual(s.env["ANTHROPIC_API_KEY"], API_KEY)

    def test_the_model_id_is_the_env_files_when_it_names_one(self):
        priced = sorted(m for m in loop.caps.MODEL_PRICES_USD_PER_MTOK if m != model_mod.DEFAULT_MODEL)[0]
        s = loop.live_settings(self.write_env(DORIS_MODEL=priced), "live")
        self.assertEqual(s.model_id, priced)

    def test_a_missing_or_bad_setting_refuses_naming_the_setting_never_its_value(self):
        cases = [("DORIS_OWNER_ADDRESS", None), ("DORIS_OWNER_ADDRESS", "Shane <%s>" % OWNER),
                 ("DORIS_OWNER_ADDRESS", DORIS.upper()), ("DORIS_MAIL_ADDRESS", None),
                 ("DORIS_APP_PASSWORD", None), ("DORIS_CALDAV_URL", "http://127.0.0.1" + COLLECTION),
                 ("DORIS_IMAP_PORT", "99999"), ("DORIS_MEETING_LINK", None),
                 ("DORIS_MEETING_LINK", "http://meet.example/" + "plain-room"),
                 ("DORIS_MEETING_LINK", "https://meet.example/" + "café-room"),
                 ("DORIS_MEETING_LINK", "https://meet.example/" + "two words"),
                 ("ANTHROPIC_API_KEY", None), ("DORIS_MODEL", "claude-without-a-price")]
        for name, value in cases:
            with self.subTest(name=name, value=value):
                with self.assertRaises(loop.Refused) as cm:
                    loop.live_settings(self.write_env(**{name: value}), "live")
                text = str(cm.exception)
                self.assertEqual(cm.exception.exit_code, 2)
                self.assertIn(name, text)
                self.assertEqual(len(text.splitlines()), 1, text)
                printed = "".join(traceback.format_exception(loop.Refused, cm.exception, cm.exception.__traceback__))
                self.assert_no_value(printed, *([value] if value else []))

    def test_without_the_live_model_no_key_is_needed(self):
        s = loop.live_settings(self.write_env(ANTHROPIC_API_KEY=None), "stub")
        self.assertIsNone(s.model_id)

    def test_an_env_file_that_cannot_be_read_refuses_naming_the_file(self):
        missing = os.path.join(self.dir, "absent.env")
        with self.assertRaises(loop.Refused) as cm:
            loop.live_settings(missing, "live")
        self.assertEqual(cm.exception.exit_code, 2)
        self.assertIn(missing, str(cm.exception))
        self.assertIn("env file", str(cm.exception))

    def test_the_settings_never_show_a_value(self):
        s = loop.live_settings(self.env_path, "live")
        self.assert_no_value(repr(s) + str(s))


# ===========================================================================================
# A live run against the fakes
# ===========================================================================================
SHELL = {"DORIS_MEETING_LINK": "https://meet.example/" + "shell-room", "ANTHROPIC_API_KEY": "dummy-shell-key-unused",
         "DORIS_MODEL": "claude-opus-5-5", "DORIS_OWNER_ADDRESS": "shell-owner@example.com",
         "HTTPS_PROXY": "", "https_proxy": ""}
ASK = ("Hi, could Shane do Wednesday, December 2 at 1:00 PM Central for 30 minutes? "
       "I'd like to talk about automating our supplier inbox.")
PROSPECT = "priya@northwind.example"


def request(n, sender="Priya Raman <%s>" % PROSPECT, date="Tue, 01 Dec 2026 08:02:00 -0800"):
    return ("From: %s\r\nTo: Shane Bornstein <%s>\r\nSubject: Quick call this week?\r\nDate: %s\r\n"
            "Message-ID: <live-%d@northwind.example>\r\nMIME-Version: 1.0\r\n"
            "Content-Type: text/plain; charset=\"utf-8\"\r\nContent-Transfer-Encoding: 7bit\r\n\r\n"
            "%s\r\n\r\nPriya\r\n" % (sender, OWNER, date, n, ASK)).encode("ascii")


def model_answer(cls="meeting_request"):
    fields = {"cls": cls, "date": "2026-12-02", "time24": "13:00", "zone": "Central", "length_min": 30,
              "pick": None, "injection_seen": False, "side_question": False, "side_question_quote": None}
    return {"content": [{"type": "text", "text": json.dumps(fields)}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 900, "output_tokens": 60}}


class LiveFixture(EnvFolder):
    def setUp(self):
        super().setUp()
        self.imap, self.smtp, self.dav = FakeIMAP(), FakeSMTP(), FakeCalDAV()
        for server in (self.imap, self.smtp, self.dav):
            self.addCleanup(server.stop)
        self.inbox = self.imap.boxes["INBOX"]
        self.sent = self.imap.boxes["Sent Items"] = Box(1700000002, special=("\\Sent",))
        self.smtp.on_deliver = lambda sender, rcpts, message: self.sent.add(message, self.clock)
        self.state = os.path.join(self.dir, "state", "live")
        self.prove_smoke('{"ts": "2026-12-01T15:00:00Z", "sent_copy_found": true, "look": 1, "waited_s": 0}')
        self.clock = u(2026, 12, 1, 16, 0)
        self.said, self.runs, self.posts = [], 0, []
        for patch in (
                mock.patch.object(live, "_imap_tls", lambda host, port, timeout: imaplib.IMAP4(
                    "127.0.0.1", self.imap.port, timeout=timeout)),
                mock.patch.object(live, "_smtp_tls", lambda host, port, timeout: smtplib.SMTP(
                    "127.0.0.1", self.smtp.port, local_hostname="[127.0.0.1]", timeout=timeout)),
                mock.patch.object(live, "_https", lambda host, port, timeout: http.client.HTTPConnection(
                    "127.0.0.1", self.dav.port, timeout=timeout)),
                mock.patch.object(model_mod, "_urllib_post", self.post),
                mock.patch.object(loop, "_wall_now", lambda: self.clock),
                mock.patch.dict(os.environ, SHELL)):
            patch.start()
            self.addCleanup(patch.stop)

    def prove_smoke(self, text):
        """What `smoke --send-to` leaves in the live state folder when it finds its copy in Sent."""
        os.makedirs(self.state, exist_ok=True)
        path = os.path.join(self.state, "smoke-sent.json")
        if text is None:
            os.remove(path)
            return
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def post(self, url, headers, body, timeout):
        self.posts.append((url, dict(headers), json.loads(body.decode("utf-8"))))
        return 200, json.dumps(model_answer()).encode("utf-8")

    def go(self, **kw):
        self.runs += 1
        fields = dict(mode="live", run_name="model-%s-live-%d" % (kw.get("model_source", "stub"), self.runs),
                      state_dir=self.state, env_path=self.env_path, model_source="stub", say=self.said.append)
        fields.update(kw)
        return loop.run(loop.RunConfig(**fields))

    def ledger_start(self):
        with ledger_mod.Ledger(os.path.join(self.state, "ledger.sqlite"), create=False) as db:
            return db.fresh_start()

    def start_line(self, summary):
        (line,) = [l for l in receipts.read_lines(summary.receipts_path)
                   if l["kind"] == "observed" and l["payload"].get("step") == "start"]
        return line["payload"]

    def nothing_contacted(self):
        self.assertEqual((self.imap.commands, self.smtp.sessions, self.dav.requests), ([], [], []))

    def started_fresh_then_asked(self, **kw):
        """A fresh start at 4:00 PM UTC with an empty inbox; Priya's request lands at 4:02; her 4:10 cycle."""
        self.go(fresh=True, **kw)
        self.inbox.add(request(1), u(2026, 12, 1, 16, 2))
        self.clock = u(2026, 12, 1, 16, 10)
        return self.go(**kw)


class LiveStartTest(LiveFixture):
    """Ruling 8: live won't start without its ledger; a fresh start reads only mail after it."""

    def test_with_no_live_ledger_a_run_refuses_unless_told_to_start_fresh(self):
        with self.assertRaises(loop.Refused) as cm:
            self.go()
        self.assertEqual(cm.exception.exit_code, 2)
        self.assertIn("--fresh", str(cm.exception))
        self.assertFalse(os.path.exists(os.path.join(self.state, "ledger.sqlite")))
        self.nothing_contacted()

    def test_a_fresh_start_records_its_time_and_the_old_mail_is_never_read(self):
        self.inbox.add(request(1, sender="Old Asker <old@example.com>", date="Tue, 01 Dec 2026 07:00:00 -0800"),
                       u(2026, 12, 1, 15, 0))
        first = self.go(fresh=True)
        self.assertEqual(self.ledger_start(), u(2026, 12, 1, 16, 0))
        self.assertEqual((first.handled, first.held, first.sends), (0, 0, 0))
        start = self.start_line(first)
        self.assertEqual((start["mode"], start["fresh_start"], start["mail_since"]),
                         ("live", True, "2026-12-01T16:00:00Z"))
        self.inbox.add(request(2), u(2026, 12, 1, 16, 2))
        self.clock = u(2026, 12, 1, 16, 10)
        second = self.go()
        self.assertEqual((second.handled, second.sends, second.bookings), (1, 1, 1))
        self.assertEqual(self.start_line(second)["fresh_start"], False)
        for s in (first, second):
            self.assertTrue(s.verify_ok)
            with open(s.receipts_path, encoding="utf-8") as fh:
                self.assertNotIn("live-1@northwind.example", fh.read())
        self.assertEqual([rcpts[0] for _, rcpts, _ in self.smtp.delivered], [PROSPECT])

    def test_fresh_with_a_live_ledger_already_there_refuses_and_keeps_its_start(self):
        self.go(fresh=True)
        self.clock = u(2026, 12, 1, 16, 10)
        with self.assertRaises(loop.Refused) as cm:
            self.go(fresh=True)
        self.assertEqual(cm.exception.exit_code, 2)
        self.assertIn("already", str(cm.exception))
        self.assertEqual(self.ledger_start(), u(2026, 12, 1, 16, 0))

    def test_a_bad_setting_refuses_before_any_ledger_or_connection(self):
        self.env_path = self.write_env(DORIS_OWNER_ADDRESS=None)
        with self.assertRaises(loop.Refused) as cm:
            self.go(fresh=True)
        self.assertEqual(cm.exception.exit_code, 2)
        self.assertIn("DORIS_OWNER_ADDRESS", str(cm.exception))
        self.assertFalse(os.path.exists(os.path.join(self.state, "ledger.sqlite")))
        self.nothing_contacted()

    def test_no_live_run_starts_until_a_smoke_found_her_sent_copy(self):
        for proof in (None, "not json", '{"sent_copy_found": false}', '{"sent_copy_found": "yes"}', "[true]"):
            with self.subTest(proof=proof):
                self.prove_smoke(proof)
                with self.assertRaises(loop.Refused) as cm:
                    self.go(fresh=True)
                self.assertEqual(cm.exception.exit_code, 2)
                self.assertEqual(len(str(cm.exception).splitlines()), 1)
                self.assertIn("`python -m doris smoke --send-to wolf.whizz@fastmail.com` first", str(cm.exception))
                self.assertFalse(os.path.exists(os.path.join(self.state, "ledger.sqlite")))
                self.nothing_contacted()
        self.prove_smoke('{"sent_copy_found": true}')
        self.assertTrue(self.go(fresh=True).verify_ok)

    def test_a_live_config_carries_no_addresses_and_no_sandbox(self):
        for extra in ({"owner_addr": OWNER}, {"doris_addr": DORIS}, {"sandbox_root": self.dir}, {"env_path": None}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.go(fresh=True, **extra)
        self.assertFalse(os.path.exists(os.path.join(self.state, "ledger.sqlite")))


class LiveRunTest(LiveFixture):

    def test_a_request_is_booked_and_confirmed_with_the_env_files_link_the_gate_checks(self):
        s = self.started_fresh_then_asked()
        self.assertEqual((s.handled, s.held, s.sends, s.bookings), (1, 0, 1, 1))
        (put,) = [r for r in self.dav.requests if r[0] == "PUT"]
        self.assertIn(b"LOCATION:" + LINK.encode("ascii"), put[3])
        (_, rcpts, data), = self.smtp.delivered
        self.assertEqual(rcpts[0], PROSPECT)
        msg = email.message_from_bytes(data, policy=email.policy.default)
        texts = "\n".join(p.get_content() for p in msg.walk() if p.get_content_maintype() == "text")
        self.assertIn("Join: " + LINK, texts)
        self.assertIn("LOCATION:" + LINK, texts)
        self.assertNotIn(SHELL["DORIS_MEETING_LINK"], texts + put[3].decode("ascii"))
        checked = [l["payload"] for l in receipts.read_lines(s.receipts_path) if l["kind"] == "checked"]
        self.assertTrue(checked and all(c["passed"] for c in checked), checked)
        self.assertEqual(templates.meeting_link(), SHELL["DORIS_MEETING_LINK"])   # the run let go of its link

    def test_the_live_model_takes_its_key_and_model_from_the_env_file(self):
        self.env_path = self.write_env(DORIS_MODEL="claude-haiku-5-5")
        s = self.started_fresh_then_asked(model_source="live")
        self.assertEqual((s.handled, s.bookings, s.model_calls), (1, 1, 1))
        (url, headers, body), = self.posts
        self.assertEqual((headers["x-api-key"], body["model"]), (API_KEY, "claude-haiku-5-5"))
        self.assertEqual(self.start_line(s)["model"], "claude-haiku-5-5")

    def test_no_secret_and_no_link_reach_a_receipt_the_terminal_or_the_state_folder(self):
        self.started_fresh_then_asked(model_source="live")
        found = []
        for folder, _, names in os.walk(self.state):
            for name in names:
                with open(os.path.join(folder, name), "rb") as fh:
                    data = fh.read()
                found += [(name, v) for v in (PASSWORD, API_KEY, LINK, SHELL["ANTHROPIC_API_KEY"])
                          if v.encode("utf-8") in data]
        self.assertEqual(found, [])
        self.assert_no_value("\n".join(self.said))

    def test_the_guard_allows_the_env_files_hosts_and_the_model_host_only_when_the_model_is_live(self):
        installed = []
        real_install = netguard.install

        def spy(hosts=()):
            installed.append(tuple(hosts))
            return real_install(hosts)

        with mock.patch.object(netguard, "install", spy):
            self.started_fresh_then_asked()
            self.go(model_source="live")
        self.assertEqual(installed, [("127.0.0.1",)] * 2 + [("127.0.0.1", "api.anthropic.com")])
        self.assertIsNone(netguard.active())


def sent_lookups(summary):
    """(the wait before it, found) for each look in Sent, in order."""
    lines = receipts.read_lines(summary.receipts_path)
    calls = {l["seq"]: l["payload"] for l in lines if l["kind"] == "called" and l["payload"]["call"] == "sent_lookup"}
    return [(calls[l["payload"]["call_seq"]].get("after_wait_s"), l["payload"].get("found"))
            for l in lines if l["kind"] == "result" and l["payload"].get("call_seq") in calls]


def send_states(state_dir):
    with ledger_mod.Ledger(os.path.join(state_dir, "ledger.sqlite"), create=False) as db:
        return [row[0] for row in db._all("SELECT state FROM sends ORDER BY rowid")]


class SentLookTest(LiveFixture):
    """Ruling 8: after a send she looks for her copy in Sent, and once more; live waits
    caps.SENT_LOOK_WAIT_S between the two. Absent both times, the send is unknown."""

    def setUp(self):
        super().setUp()
        self.waits, self.held, self.file_late = [], [], False
        self.smtp.on_deliver = lambda sender, rcpts, message: self.held.append(message)
        patch = mock.patch.object(loop, "_sleep", self.wait)
        patch.start()
        self.addCleanup(patch.stop)

    def wait(self, seconds):
        self.waits.append(seconds)
        if self.file_late:                      # the server files her copy while she waits
            for message in self.held:
                self.sent.add(message, self.clock)
            del self.held[:]

    def test_the_wait_is_five_seconds(self):
        self.assertEqual(caps.SENT_LOOK_WAIT_S, 5.0)

    def test_a_copy_filed_late_is_found_on_the_second_look_after_the_wait(self):
        self.file_late = True
        s = self.started_fresh_then_asked()
        self.assertEqual(self.waits, [caps.SENT_LOOK_WAIT_S])
        self.assertEqual(sent_lookups(s), [(None, False), (caps.SENT_LOOK_WAIT_S, True)])
        self.assertEqual(send_states(self.state), ["confirmed"])
        self.assertEqual((s.handled, s.held, s.sends), (1, 0, 1))

    def test_a_copy_never_filed_is_unknown_after_both_looks_and_held_as_before(self):
        s = self.started_fresh_then_asked()
        # the T1 and then the hold notice (S1) it leads to: each looked for twice, a wait between
        self.assertEqual(self.waits, [caps.SENT_LOOK_WAIT_S] * 2)
        self.assertEqual(sent_lookups(s), [(None, False), (caps.SENT_LOOK_WAIT_S, False)] * 2)
        self.assertEqual(send_states(self.state), ["handed_over", "handed_over"])     # decision 76: SMTP took both
        (held,) = [l["payload"] for l in receipts.read_lines(s.receipts_path) if l["kind"] == "held"]
        self.assertEqual(held["outcome"], "hold:send_unconfirmed")

    def test_a_copy_found_at_once_needs_no_wait(self):
        self.smtp.on_deliver = lambda sender, rcpts, message: self.sent.add(message, self.clock)
        s = self.started_fresh_then_asked()
        self.assertEqual(self.waits, [])
        self.assertEqual(sent_lookups(s), [(None, True)])


class ReadBackTest(LiveFixture):
    """After the create she reads her event back: unless its start, end and status are what she
    wrote, she holds, and nothing goes to the prospect."""

    def held_when_the_server_keeps(self, change):
        real_put = self.dav.put
        self.dav.put = lambda name, data: real_put(name, change(data))
        s = self.started_fresh_then_asked()
        self.assertEqual([rcpts for _, rcpts, _ in self.smtp.delivered if rcpts[0] == PROSPECT], [])
        lines = receipts.read_lines(s.receipts_path)
        (got,) = [l["payload"] for l in lines if l["kind"] == "result" and "fields_match" in l["payload"]]
        self.assertFalse(got["fields_match"])
        (error,) = [l["payload"] for l in lines if l["kind"] == "error"]
        self.assertEqual(error["step"], "readback")
        (held,) = [l["payload"] for l in lines if l["kind"] == "held"]
        self.assertEqual(held["outcome"], "hold:internal_error")
        self.assertEqual((s.handled, s.held, s.sends, s.bookings, s.hold_notices), (0, 1, 0, 0, 1))

    def test_other_times_and_cancelled(self):
        self.held_when_the_server_keeps(lambda data: re.sub(
            rb"DTEND:\d{8}T\d{6}Z", b"DTEND:20261202T203000Z", re.sub(
                rb"DTSTART:\d{8}T\d{6}Z", b"DTSTART:20261202T200000Z", data)).replace(
            b"STATUS:CONFIRMED", b"STATUS:CANCELLED"))

    def test_only_cancelled(self):
        self.held_when_the_server_keeps(lambda data: data.replace(b"STATUS:CONFIRMED", b"STATUS:CANCELLED"))

    def test_only_a_later_end(self):
        self.held_when_the_server_keeps(lambda data: re.sub(rb"DTEND:\d{8}T\d{6}Z", b"DTEND:20261202T194500Z", data))


class Crash(BaseException):
    """A kill at this exact point: nothing in the loop catches it."""


class HandedOverTest(LiveFixture):
    """Decision 76: SMTP's 250 is written down as handed_over before the Sent look, and nothing in
    that state is ever sent again, whatever the Sent look or a restart finds. Here, as Fastmail may,
    the server never files her SMTP mail in Sent."""

    def setUp(self):
        super().setUp()
        self.smtp.on_deliver = None
        patch = mock.patch.object(loop, "_sleep", lambda seconds: None)
        patch.start()
        self.addCleanup(patch.stop)

    def to(self, address):
        return [data for _, rcpts, data in self.smtp.delivered if rcpts[0] == address]

    def test_a_restart_right_after_the_250_never_sends_the_reply_again(self):
        self.go(fresh=True)
        self.inbox.add(request(1), u(2026, 12, 1, 16, 2))
        self.clock = u(2026, 12, 1, 16, 10)
        real = live.LiveMail.find_in_sent
        fired = []

        def killed_at_the_first_look(mail, message_id):
            if not fired:
                fired.append(message_id)
                raise Crash("killed right after the 250, before the Sent look")
            return real(mail, message_id)

        with mock.patch.object(live.LiveMail, "find_in_sent", killed_at_the_first_look):
            with self.assertRaises(Crash):
                self.go()
        self.assertEqual(send_states(self.state), ["handed_over"])
        self.clock = u(2026, 12, 1, 16, 20)
        s = self.go()
        self.assertEqual(len(self.to(PROSPECT)), 1)                  # one T1, never a second
        (held,) = [l["payload"] for l in receipts.read_lines(s.receipts_path) if l["kind"] == "held"]
        self.assertEqual(held["outcome"], "hold:send_unconfirmed")   # the closing for a copy never found
        self.assertEqual(len(self.to(OWNER)), 1)                     # and its one notice

    def test_three_runs_in_one_day_send_one_digest(self):
        for n, minute in enumerate((0, 10, 20)):
            self.clock = u(2026, 12, 1, 16, minute)
            s = self.go(fresh=(n == 0), digest=True)
            self.assertTrue(s.verify_ok)
        self.assertEqual(len(self.to(OWNER)), 1)
        self.assertEqual(send_states(self.state), ["handed_over"])


class FakeTime(object):
    """The wall clock for loop._time and loop._sleep: a sleep moves it on, then runs the hook."""

    def __init__(self, start, hook=None):
        self.now, self.slept, self.hook = start, [], hook

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds
        if self.hook is not None:
            self.hook()


class LongRunTest(LiveFixture):
    """`run`: a pass, then a wait for her next cycle, then another, until stopped."""

    def use(self, fake):
        for patch in (mock.patch.object(loop, "_time", fake.time), mock.patch.object(loop, "_sleep", fake.sleep)):
            patch.start()
            self.addCleanup(patch.stop)

    def test_mail_that_lands_while_she_waits_is_read_on_her_next_cycle(self):
        self.go(fresh=True)
        landed = []

        def arrive():
            if not landed:
                landed.append(self.inbox.add(request(1), u(2026, 12, 1, 16, 2)))
                self.clock = u(2026, 12, 1, 16, 10)

        self.use(FakeTime(1000.0, arrive))
        s = self.go(cycles=2, cycle_s=600.0)
        self.assertEqual((s.handled, s.sends, s.bookings), (1, 1, 1))
        lines = receipts.read_lines(s.receipts_path)
        fetches = [l for l in lines if l["kind"] == "called" and l["payload"]["call"] == "mail_fetch"]
        (observed,) = [l for l in lines if l["kind"] == "observed" and l["payload"].get("from_addr") == PROSPECT]
        self.assertLess(fetches[0]["seq"], observed["seq"])
        self.assertEqual(s.verify_ok, True)

    def test_the_wait_runs_to_her_next_cycle_on_the_grid_in_short_steps(self):
        self.go(fresh=True)
        fake = FakeTime(1000.25)
        self.use(fake)
        self.go(cycles=2, cycle_s=600.0)
        self.assertAlmostEqual(sum(fake.slept), 1200.0 - 1000.25)
        self.assertLessEqual(max(fake.slept), loop.HALT_POLL_S)

    def test_a_halt_while_she_waits_ends_a_run_that_would_go_on_until_stopped(self):
        from doris import supervisor
        self.go(fresh=True)
        fake = FakeTime(1000.0, lambda: supervisor.request_stop("live", "cli", state_dir=self.state))
        self.use(fake)
        s = self.go(cycles=None, cycle_s=600.0)
        self.assertEqual(s.halted, "cli")
        self.assertEqual(len(fake.slept), 1)                  # seen at the very next look
        kinds = [l["kind"] for l in receipts.read_lines(s.receipts_path)]
        self.assertEqual(kinds[-2:], ["halted", "summary"])
        self.assertTrue(s.verify_ok)

    def test_a_run_needs_at_least_one_pass_and_a_positive_cycle(self):
        for extra in ({"cycles": 0}, {"cycle_s": 0}, {"cycle_s": -1.0}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.go(fresh=True, **extra)


class RunCommandTest(LiveFixture):
    """`python -m doris run --live`: a start that cannot go says why in one line before anything is
    made; one that can writes a config naming the env file and holding no setting, for the worker
    under the kill switch, which loads the env file itself."""

    def setUp(self):
        super().setUp()
        self.started = []
        for patch in (mock.patch.object(paths, "STATE", os.path.join(self.dir, "state")),
                      mock.patch.object(supervisor, "run_supervised", self.supervised)):
            patch.start()
            self.addCleanup(patch.stop)

    def supervised(self, mode, run_name, worker_argv, receipts_path, ledger_path, **kw):
        self.started.append({"mode": mode, "run_name": run_name, "argv": worker_argv,
                             "receipts": receipts_path, "ledger": ledger_path})
        return 0

    def cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def test_a_live_start_that_cannot_go_says_why_in_one_line_and_makes_nothing(self):
        ledger_path = os.path.join(self.state, "ledger.sqlite")

        def no_env():
            return ["--env", os.path.join(self.dir, "absent.env"), "--fresh"]

        def no_owner():
            return ["--env", self.write_env(DORIS_OWNER_ADDRESS=None), "--fresh"]

        def no_key():
            return ["--env", self.write_env(ANTHROPIC_API_KEY=None), "--fresh"]

        def no_proof():
            self.prove_smoke(None)
            return ["--env", self.write_env(), "--fresh"]

        def no_ledger():
            return ["--env", self.write_env()]

        def fresh_over_a_ledger():
            ledger_mod.start_fresh(ledger_path, u(2026, 11, 30, 12, 0))
            return ["--env", self.write_env(), "--fresh"]

        cases = ((no_env, "absent.env"), (no_owner, "DORIS_OWNER_ADDRESS"), (no_key, "ANTHROPIC_API_KEY"),
                 (no_proof, loop.SMOKE_REFUSAL), (no_ledger, loop.NO_LIVE_LEDGER),
                 (fresh_over_a_ledger, loop.FRESH_OVER_LEDGER))
        for setup, said in cases:
            with self.subTest(case=setup.__name__):
                self.prove_smoke('{"sent_copy_found": true}')
                code, out = self.cli("run", "--live", *setup())
                self.assertEqual(code, 2, out)
                self.assertEqual(len(out.strip().splitlines()), 1, out)
                self.assertTrue(out.startswith("Not started: "), out)
                if said.startswith("Not started: "):        # one of the fixed lines, word for word
                    self.assertEqual(out.strip(), said)
                else:
                    self.assertIn(said, out)
                    self.assert_no_value(out)
                self.assertEqual(self.started, [])
                self.assertFalse(os.path.exists(os.path.join(self.state, "runs")))
                if setup is not fresh_over_a_ledger:
                    self.assertFalse(os.path.exists(ledger_path))
        self.nothing_contacted()

    def test_a_halt_in_force_refuses_before_the_env_file_is_read(self):
        supervisor.request_stop("live", "cli")
        code, out = self.cli("run", "--live", "--fresh", "--env", os.path.join(self.dir, "absent.env"))
        self.assertEqual(code, supervisor.EXIT_REFUSED_HALT, out)
        self.assertNotIn("env file", out)
        self.assertEqual(self.started, [])

    def test_the_default_env_file_is_the_smokes(self):
        self.assertEqual(cli.LIVE_ENV, smoke.DEFAULT_ENV)
        self.assertEqual(cli.LIVE_ENV, "/root/doris-secrets/.env")

    def test_a_live_start_writes_a_config_that_names_the_env_file_and_holds_no_setting(self):
        code, out = self.cli("run", "--live", "--fresh", "--env", os.path.relpath(self.env_path))
        self.assertEqual(code, 0, out)
        (started,) = self.started
        self.assertEqual(started["mode"], "live")
        self.assertRegex(started["run_name"], r"^model-live-live-\d{8}T\d{6}Z$")
        self.assertEqual(started["argv"][:4], [sys.executable, "-m", "doris", cli.WORKER])
        self.assertEqual(started["ledger"], os.path.join(self.state, "ledger.sqlite"))
        run_dir = os.path.join(self.state, "runs", started["run_name"])
        self.assertEqual(started["receipts"], os.path.join(run_dir, "receipts.jsonl"))
        with open(started["argv"][4], encoding="utf-8") as fh:
            text = fh.read()
        config = json.loads(text)
        self.assertEqual({k: config.get(k) for k in ("mode", "state_dir", "env_path", "fresh", "model_source",
                                                     "clock_mode", "digest", "cycles", "cycle_s")},
                         {"mode": "live", "state_dir": self.state, "env_path": self.env_path, "fresh": True,
                          "model_source": "live", "clock_mode": "fixed", "digest": False, "cycles": None,
                          "cycle_s": loop.CYCLE.total_seconds()})
        for name in ("sandbox_root", "owner_addr", "doris_addr", "model_id", "answers_path"):
            self.assertIsNone(config.get(name), name)
        self.assert_no_value(text)
        self.assertTrue(out.startswith("Live. "), out)
        self.assertIn(model_mod.DEFAULT_MODEL, out)
        self.assertIn("until `python -m doris stop`", out)
        self.assert_no_value(out)
        self.assertFalse(os.path.exists(os.path.join(self.state, "ledger.sqlite")))   # the worker starts it
        self.nothing_contacted()

    def test_the_worker_runs_that_config_live_and_waits_for_each_next_cycle_until_stopped(self):
        self.cli("run", "--live", "--fresh", "--env", self.env_path)
        (started,) = self.started
        arrived, stopped = [], []

        def tick():                # her first wait: Priya writes; the wait after her reply: a stop
            if not arrived:
                arrived.append(self.inbox.add(request(1), u(2026, 12, 1, 16, 2)))
                self.clock = u(2026, 12, 1, 16, 10)
            elif self.smtp.delivered and not stopped:
                stopped.append(supervisor.request_stop("live", "cli"))

        fake = FakeTime(1199.0, tick)
        for patch in (mock.patch.object(loop, "_time", fake.time), mock.patch.object(loop, "_sleep", fake.sleep)):
            patch.start()
            self.addCleanup(patch.stop)
        code, out = self.cli(cli.WORKER, started["argv"][4])
        self.assertEqual(code, supervisor.WORKER_EXIT_HALTED, out)
        self.assertEqual(len(stopped), 1)
        self.assertEqual([rcpts[0] for _, rcpts, _ in self.smtp.delivered], [PROSPECT])
        (url, headers, body), = self.posts
        self.assertEqual((headers["x-api-key"], body["model"]), (API_KEY, model_mod.DEFAULT_MODEL))
        self.assertEqual(self.ledger_start(), u(2026, 12, 1, 16, 0))
        lines = receipts.read_lines(started["receipts"])
        (start,) = [l["payload"] for l in lines if l["kind"] == "observed" and l["payload"].get("step") == "start"]
        self.assertEqual((start["mode"], start["fresh_start"], start["model"]), ("live", True, model_mod.DEFAULT_MODEL))
        fetches = [l for l in lines if l["kind"] == "called" and l["payload"]["call"] == "mail_fetch"]
        self.assertGreaterEqual(len(fetches), 2)
        self.assertTrue(receipts.verify(started["receipts"]).ok)     # the parent writes the halted line
        self.assert_no_value(out)


if __name__ == "__main__":
    unittest.main()
