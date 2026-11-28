"""Live mode in the loop: her settings from the env file alone, the live ledger's fresh start, the
live adapters under the network guard, and the meeting link and the model key from the env file.

The real LiveMail and LiveCalendar talk to test_live's fake IMAP and CalDAV servers and
fake_jmap's JMAP server on 127.0.0.1 (the adapters' default transports swapped for plain
connections to them), as in test_smoke. No test reads a real env file or reaches a real server.
"""
import base64
import contextlib
import datetime as dt
import email
import email.policy
import hashlib
import http.client
import imaplib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import traceback
import unittest
from unittest import mock

from doris import __main__ as cli
from doris import caps, compose, envfile, ledger as ledger_mod
from doris import loop, model as model_mod, netguard, paths, receipts, sentrecord, smoke, supervisor, templates
from doris.adapters import jmap, live
from doris.shapes import MailError, SendUnknown
from tests.unit.fake_jmap import TOKEN, FakeJMAP
from tests.unit.test_live import COLLECTION, DORIS, OWNER, PASSWORD, Box, FakeCalDAV, FakeIMAP, u

LINK = "https://meet.example/" + "live-loop-room"
API_KEY = "dummy-anthropic-key-for-live-loop-tests"
SETTINGS = (("DORIS_MAIL_ADDRESS", DORIS), ("DORIS_APP_PASSWORD", PASSWORD), ("DORIS_OWNER_ADDRESS", OWNER),
            ("DORIS_IMAP_HOST", "127.0.0.1"), ("DORIS_JMAP_TOKEN", TOKEN),
            ("DORIS_CALDAV_URL", "https://127.0.0.1" + COLLECTION), ("DORIS_MEETING_LINK", LINK),
            ("ANTHROPIC_API_KEY", API_KEY), ("DORIS_MODEL", ""))
VALUES = (PASSWORD, TOKEN, LINK, API_KEY, DORIS, OWNER)   # none of these is ever printed


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
        self.assertEqual(s.hosts, ("127.0.0.1", "api.fastmail.com"))
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
                 ("ANTHROPIC_API_KEY", None), ("DORIS_MODEL", "claude-without-a-price"),
                 ("DORIS_JMAP_TOKEN", None), ("DORIS_JMAP_TOKEN", "dummy-short-token"),
                 ("DORIS_JMAP_TOKEN", TOKEN[:20] + " " + TOKEN[20:]), ("DORIS_JMAP_TOKEN", TOKEN + "\u00e9")]
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


class SmokeOnlySettingsTest(EnvFolder):
    """WOLF_EMAIL and WOLF_JMAP_TOKEN, in the same env file, are the smoke's alone (its forward test
    from the wolf.whizz account): her loop never holds or reads them."""
    WOLF_TOKEN = "dummy-wolf-jmap-token-for-live-loop-tests-5151"

    def test_her_live_settings_hold_only_the_names_she_reads(self):
        path = self.write_env(WOLF_EMAIL="wolf.whizz@fastmail.com", WOLF_JMAP_TOKEN=self.WOLF_TOKEN,
                              SOMETHING_ELSE="dummy-other-value")
        s = loop.live_settings(path, "live")
        self.assertEqual(set(s.env), {name for name, value in SETTINGS if value is not None})
        self.assertNotIn(self.WOLF_TOKEN, repr(sorted(s.env.values())))


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
        self.imap, self.jmap, self.dav = FakeIMAP(), FakeJMAP().as_fastmail(), FakeCalDAV()
        for server in (self.imap, self.jmap, self.dav):
            self.addCleanup(server.stop)
        self.inbox = self.imap.boxes["INBOX"]
        self.sent = self.imap.boxes["Sent Items"] = Box(1700000002, special=("\\Sent",))
        self.jmap.on_deliver = lambda sender, rcpts, message: self.sent.add(message, self.clock)
        self.state = os.path.join(self.dir, "state", "live")
        self.prove_smoke(self.smoke_record())
        self.clock = u(2026, 12, 1, 16, 0)
        self.said, self.runs, self.posts = [], 0, []
        for patch in (
                mock.patch.object(live, "_imap_tls", lambda host, port, timeout: imaplib.IMAP4(
                    "127.0.0.1", self.imap.port, timeout=timeout)),
                mock.patch.object(jmap, "_https", lambda host, port, timeout: http.client.HTTPConnection(
                    "127.0.0.1", self.jmap.port, timeout=timeout)),
                mock.patch.object(live, "_https", lambda host, port, timeout: http.client.HTTPConnection(
                    "127.0.0.1", self.dav.port, timeout=timeout)),
                mock.patch.object(model_mod, "_urllib_post", self.post),
                mock.patch.object(loop, "_wall_now", lambda: self.clock),
                mock.patch.dict(os.environ, SHELL)):
            patch.start()
            self.addCleanup(patch.stop)

    def smoke_record(self, when=None):
        """The record `smoke --send-to` writes when it finds its copy in Sent: the smoke's own
        writer's bytes (sentrecord.encode), by default from an hour before her first cycle here."""
        return sentrecord.encode(1, 0.4, when or u(2026, 12, 1, 15, 0))

    def prove_smoke(self, data):
        """Leave data (bytes, or text as UTF-8) where the smoke writes its record; None: no record."""
        os.makedirs(self.state, exist_ok=True)
        path = sentrecord.path(self.state)
        if data is None:
            os.remove(path)
            return
        with open(path, "wb") as fh:
            fh.write(data.encode("utf-8") if isinstance(data, str) else data)

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
        self.assertEqual((self.imap.commands, self.jmap.requests, self.dav.requests), ([], [], []))

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
        self.assertEqual([rcpts[0] for _, rcpts, _ in self.jmap.delivered], [PROSPECT])

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
        self.prove_smoke(self.smoke_record())
        self.assertTrue(self.go(fresh=True).verify_ok)

    def test_only_the_record_as_the_smoke_writes_it_lets_a_live_run_start(self):
        """One format, the smoke's (sentrecord): a looser or other record is refused in one line
        naming what is wrong, before any ledger or connection."""
        good = self.smoke_record()
        cases = [
            ('{"sent_copy_found": true}', "its keys are not exactly look, sent_copy_found, ts, waited_s"),
            ('{"ts": "2026-12-01T15:00:00Z", "sent_copy_found": true, "look": 1, "waited_s": 0}', "not as the smoke"),
            (good.replace(b'"waited_s": 0.4', b'"waited_s": 0'), "waited_s is not seconds to 0.1"),
            (good.replace(b'"look": 1', b'"look": 3'), "look is not 1 or 2"),
            (good.replace(b'"sent_copy_found": true', b'"sent_copy_found": false'), "sent_copy_found is not true"),
            (good.replace(b'15:00:00Z', b'15:00:00'), "ts is not a UTC time to the second"),
            (good[:-2] + b', "to": "%s"}\n' % OWNER.encode("ascii"), "its keys are"),
            (good[:-1], "not as the smoke writes it"),
        ]
        for data, why in cases:
            with self.subTest(data=data):
                self.prove_smoke(data)
                with self.assertRaises(loop.Refused) as cm:
                    self.go(fresh=True)
                said = str(cm.exception)
                self.assertEqual(cm.exception.exit_code, 2)
                self.assertRegex(said, r"^Not started: the smoke check's record \(smoke-sent\.json\) is not as the "
                                       r"smoke writes it: [^\n]*\. Run `python -m doris smoke --send-to "
                                       r"wolf\.whizz@fastmail\.com` first\.\Z")
                self.assertIn(why, said)
                self.assert_no_value(said[:-len(loop.SMOKE_FIRST)])     # its last words name the test inbox
                self.assertFalse(os.path.exists(os.path.join(self.state, "ledger.sqlite")))
                self.nothing_contacted()
        self.prove_smoke(None)
        os.makedirs(sentrecord.path(self.state))                 # a record that cannot be read
        with self.assertRaises(loop.Refused) as cm:
            self.go(fresh=True)
        self.assertRegex(str(cm.exception), r"^Not started: the smoke check's record \(smoke-sent\.json\) "
                                            r"cannot be read: \w+\. Run `python -m doris smoke")
        self.nothing_contacted()

    def test_a_record_more_than_a_day_old_or_dated_ahead_refuses_before_anything_connects(self):
        """Fresh is from when the smoke wrote it until caps.SMOKE_RECORD_MAX_AGE (a day) after."""
        stale = ("Not started: the smoke check's record (smoke-sent.json) is more than 24 hours old. "
                 + loop.SMOKE_FIRST)
        ahead = "Not started: the smoke check's record (smoke-sent.json) is dated after now. " + loop.SMOKE_FIRST
        for clock, said in ((u(2026, 12, 2, 15, 0, 1), stale), (u(2026, 12, 9, 15, 0), stale),
                            (u(2026, 12, 1, 14, 59, 59), ahead)):
            with self.subTest(clock=clock):
                self.clock = clock
                with self.assertRaises(loop.Refused) as cm:
                    self.go(fresh=True)
                self.assertEqual((str(cm.exception), cm.exception.exit_code), (said, 2))
                self.assertFalse(os.path.exists(os.path.join(self.state, "ledger.sqlite")))
                self.nothing_contacted()
        self.clock = u(2026, 12, 2, 15, 0)                       # a day to the second after the smoke
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
        (_, rcpts, data), = self.jmap.delivered
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
                found += [(name, v) for v in (PASSWORD, TOKEN, API_KEY, LINK, SHELL["ANTHROPIC_API_KEY"])
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
        self.assertEqual(installed, [("127.0.0.1", "api.fastmail.com")] * 2
                         + [("127.0.0.1", "api.fastmail.com", "api.anthropic.com")])
        self.assertIsNone(netguard.active())


# ===========================================================================================
# The meeting link a sender writes reaches no receipt, recorded answer or kept copy (review 2, 3)
# ===========================================================================================
def priya_mail(n, body, cte="7bit", subject="Quick call this week?"):
    """A plain-text mail from Priya; body is bytes already in its transfer encoding (cte)."""
    return ("From: Priya Raman <%s>\r\nTo: Shane Bornstein <%s>\r\nSubject: %s\r\n"
            "Date: Tue, 01 Dec 2026 08:02:00 -0800\r\nMessage-ID: <live-link-%d@northwind.example>\r\n"
            "MIME-Version: 1.0\r\nContent-Type: text/plain; charset=\"utf-8\"\r\nContent-Transfer-Encoding: %s\r\n\r\n"
            % (PROSPECT, OWNER, subject, n, cte)).encode("ascii") + body


def room_in(data):
    """The made-up room in these bytes in any form: as written, defanged, in capitals, or wrapped by
    quoted-printable or a line break."""
    return b"live-loop-room" in re.sub(rb"=\r?\n|\s", b"", data).lower()


class MeetingLinkTest(LiveFixture):
    """The law: the meeting link never goes in a receipt. A sender may write it (a reply quoting her
    confirmation does); "[meeting link]" stands in its place in every receipt, recorded answer and kept
    copy, and a copy that would still show it once decoded is not kept."""

    def answer(self, cls, quote=None):
        def post(url, headers, body, timeout):
            a = model_answer(cls)
            fields = json.loads(a["content"][0]["text"])
            fields.update(side_question=quote is not None, side_question_quote=quote)
            a["content"][0]["text"] = json.dumps(fields)
            return 200, json.dumps(a).encode("utf-8")
        p = mock.patch.object(model_mod, "_urllib_post", post)
        p.start()
        self.addCleanup(p.stop)

    def files(self):
        for folder, _, names in os.walk(self.state):
            for name in names:
                with open(os.path.join(folder, name), "rb") as fh:
                    yield os.path.relpath(os.path.join(folder, name), self.state), fh.read()

    def observed(self, s):
        return dict((l["subject"], l["payload"]) for l in receipts.read_lines(s.receipts_path)
                    if l["kind"] == "observed" and "server_id" in l["payload"])

    def test_a_request_that_writes_the_link_books_and_nothing_she_keeps_carries_it(self):
        self.answer("meeting_request", quote="Is %s the right room?" % LINK)
        self.go(fresh=True, model_source="live")
        ask = ("Hi, could Shane do Wednesday, December 2 at 1:00 PM Central for 30 minutes? Is %s the right "
               "room? Or hxxps://meet[.]example/live-loop-room, or MEET.EXAMPLE/live-loop-room?\r\n\r\nPriya\r\n"
               % LINK)
        self.inbox.add(priya_mail(1, ask.encode("ascii")), u(2026, 12, 1, 16, 2))
        self.clock = u(2026, 12, 1, 16, 10)
        answers = os.path.join(self.state, "recorded-answers.jsonl")
        s = self.go(model_source="live", record_path=answers)
        self.assertEqual((s.handled, s.held, s.sends, s.bookings, s.verify_ok), (1, 0, 1, 1, True))
        (_, rcpts, data), = self.jmap.delivered
        self.assertIn(b"Join: " + LINK.encode("ascii"), data)               # the link still goes where it belongs
        self.assertEqual([name for name, data in self.files() if room_in(data)], [])
        self.assertNotIn("live-loop-room", "\n".join(self.said))
        (classified,) = [l["payload"] for l in receipts.read_lines(s.receipts_path) if l["kind"] == "classified"]
        self.assertEqual(json.loads(classified["raw"])["side_question_quote"], "Is [meeting link] the right room?")
        with open(answers, encoding="utf-8") as fh:
            self.assertIn("Is [meeting link] the right room?", json.loads(fh.read())["raw"])
        (seen,) = self.observed(s).values()
        self.assertEqual(seen["copy_redacted"], "meeting_link")
        with open(os.path.join(s.run_dir, seen["message_copy"]), "rb") as fh:
            self.assertEqual(fh.read().count(b"[meeting link]"), 3)

    def test_a_copy_keeps_its_bytes_loses_the_link_or_is_not_kept_when_only_decoding_shows_it(self):
        self.answer("fyi")
        self.go(fresh=True, model_source="live")
        mails = {
            1: priya_mail(1, b"Notes from today, nothing else.\r\n"),
            2: priya_mail(2, b"Notes from today.\r\nJoin: https://meet.example/live-=\r\nloop-room any time.\r\n",
                          cte="quoted-printable"),
            3: priya_mail(3, base64.b64encode(("Notes: the room is %s\r\n" % LINK).encode("ascii")) + b"\r\n",
                          cte="base64"),
            4: priya_mail(4, b"Notes from today.\r\n", subject="=?utf-8?b?%s?=" % base64.b64encode(
                ("Room: %s" % LINK).encode("ascii")).decode("ascii")),
        }
        for raw in mails.values():
            self.inbox.add(raw, u(2026, 12, 1, 16, 2))
        self.clock = u(2026, 12, 1, 16, 10)
        s = self.go(model_source="live")
        self.assertTrue(s.verify_ok)
        seen = self.observed(s)
        copies = os.path.join(s.run_dir, "messages")
        plain = seen["live-link-1@northwind.example"]
        self.assertNotIn("copy_redacted", plain)
        with open(os.path.join(s.run_dir, plain["message_copy"]), "rb") as fh:
            self.assertEqual(fh.read(), mails[1])                            # byte for byte, as before
        split = seen["live-link-2@northwind.example"]
        self.assertEqual(split["copy_redacted"], "meeting_link")
        with open(os.path.join(s.run_dir, split["message_copy"]), "rb") as fh:
            self.assertIn(b"Join: [meeting link] any time.", fh.read())
        for n in (3, 4):
            withheld = seen["live-link-%d@northwind.example" % n]
            self.assertNotIn("message_copy", withheld)
            self.assertEqual(withheld["copy_withheld"], "meeting_link")
            self.assertFalse(os.path.exists(os.path.join(copies, hashlib.sha256(mails[n]).hexdigest() + ".eml")))
        self.assertEqual(sorted(os.listdir(copies)), sorted(hashlib.sha256(mails[n]).hexdigest() + ".eml"
                                                            for n in (1, 2)))
        self.assertEqual([name for name, data in self.files() if room_in(data)], [])


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
        self.jmap.on_deliver = lambda sender, rcpts, message: self.held.append(message)
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
        self.assertEqual(send_states(self.state), ["handed_over", "handed_over"])     # decision 76: the server took both
        (held,) = [l["payload"] for l in receipts.read_lines(s.receipts_path) if l["kind"] == "held"]
        self.assertEqual(held["outcome"], "hold:send_unconfirmed")

    def test_a_copy_found_at_once_needs_no_wait(self):
        self.jmap.on_deliver = lambda sender, rcpts, message: self.sent.add(message, self.clock)
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
        self.assertEqual([rcpts for _, rcpts, _ in self.jmap.delivered if rcpts[0] == PROSPECT], [])
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
    """Decision 76: the server taking it (a JMAP submission created) is written down as handed_over
    before the Sent look, and nothing in that state is ever sent again, whatever the Sent look or a
    restart finds. Here, as Fastmail may, the server never files her mail in Sent."""

    def setUp(self):
        super().setUp()
        self.jmap.on_deliver = None
        patch = mock.patch.object(loop, "_sleep", lambda seconds: None)
        patch.start()
        self.addCleanup(patch.stop)

    def to(self, address):
        return [data for _, rcpts, data in self.jmap.delivered if rcpts[0] == address]

    def test_a_restart_right_after_the_handover_never_sends_the_reply_again(self):
        self.go(fresh=True)
        self.inbox.add(request(1), u(2026, 12, 1, 16, 2))
        self.clock = u(2026, 12, 1, 16, 10)
        real = live.LiveMail.find_in_sent
        fired = []

        def killed_at_the_first_look(mail, message_id):
            if not fired:
                fired.append(message_id)
                raise Crash("killed right after the handover, before the Sent look")
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


def held_outcomes(summary):
    return [l["payload"]["outcome"] for l in receipts.read_lines(summary.receipts_path) if l["kind"] == "held"]


class SendingTest(LiveFixture):
    """Review 2, finding 1: a send is marked sending in the ledger right before the transport is
    called, so a kill between the server taking it and its handed_over record can no longer leave it pending
    and send it twice. Cut off there, it may have gone: the next run looks for it in Sent twice (the
    wait between, live); found, it is done; not found, it is never sent again and the message is held
    for the owner. The transport's answer is read the same for any transport: MailError means it
    surely did not go (pending again), SendUnknown that it may have gone (unknown, before any look)."""

    def setUp(self):
        super().setUp()
        self.waits = []
        patch = mock.patch.object(loop, "_sleep", self.waits.append)
        patch.start()
        self.addCleanup(patch.stop)

    def to(self, address):
        return [data for _, rcpts, data in self.jmap.delivered if rcpts[0] == address]

    def killed_after_the_handover(self):
        """Her 4:10 cycle dies right after the server took T1, before handed_over is written down;
        the next run at 4:20."""
        self.go(fresh=True)
        self.inbox.add(request(1), u(2026, 12, 1, 16, 2))
        self.clock = u(2026, 12, 1, 16, 10)
        real = ledger_mod.Ledger.set_send_state

        def dies_before_the_record(db, message_id, state):
            if state == "handed_over":
                raise Crash("killed after the handover, before it was written down")
            return real(db, message_id, state)

        with mock.patch.object(ledger_mod.Ledger, "set_send_state", dies_before_the_record):
            with self.assertRaises(Crash):
                self.go()
        self.assertEqual(len(self.to(PROSPECT)), 1)
        self.assertEqual(send_states(self.state), ["sending"])          # never pending once the transport has it
        del self.waits[:]
        self.clock = u(2026, 12, 1, 16, 20)
        return self.go()

    def test_cut_off_after_the_handover_with_no_copy_in_sent_it_is_never_sent_again_and_held(self):
        self.jmap.on_deliver = None                                     # as Fastmail may: nothing filed in Sent
        s = self.killed_after_the_handover()
        self.assertEqual(len(self.to(PROSPECT)), 1)                     # one T1, never a second
        lookups = sent_lookups(s)
        self.assertEqual(lookups[:2], [(None, False), (caps.SENT_LOOK_WAIT_S, False)])   # reconcile: two looks
        self.assertEqual(self.waits[0], caps.SENT_LOOK_WAIT_S)
        self.assertEqual(held_outcomes(s), ["hold:send_unconfirmed"])
        self.assertEqual(len(self.to(OWNER)), 1)                        # its one notice
        self.assertEqual(send_states(self.state), ["unknown", "handed_over"])
        self.assertIn("Tue Dec 1, 8:20 AM Pacific  T1 was cut off mid-send (a stop or a crash): no copy in Sent "
                      "after two looks. It may have gone, so it is never sent again.", self.said)
        self.clock = u(2026, 12, 1, 16, 30)
        again = self.go()
        self.assertEqual((len(self.to(PROSPECT)), len(self.to(OWNER))), (1, 1))
        self.assertEqual(sent_lookups(again), [(None, False)])          # unknown: one look, no wait, nothing sent
        self.assertTrue(again.verify_ok)

    def test_cut_off_after_the_handover_her_copy_in_sent_settles_it_at_the_restart(self):
        s = self.killed_after_the_handover()
        self.assertEqual(len(self.to(PROSPECT)), 1)
        self.assertEqual(sent_lookups(s)[0], (None, True))
        self.assertEqual(send_states(self.state), ["confirmed"])
        self.assertEqual((s.handled, s.held, len(self.to(OWNER))), (1, 0, 0))

    def test_cut_off_after_the_handover_a_copy_filed_during_the_wait_is_found_on_the_second_look(self):
        delivered = []
        self.jmap.on_deliver = lambda sender, rcpts, message: delivered.append(message)

        def files_late(seconds):
            self.waits.append(seconds)
            for message in delivered:
                self.sent.add(message, self.clock)
            del delivered[:]

        with mock.patch.object(loop, "_sleep", files_late):
            s = self.killed_after_the_handover()
        self.assertEqual(sent_lookups(s)[:2], [(None, False), (caps.SENT_LOOK_WAIT_S, True)])
        self.assertEqual(send_states(self.state), ["confirmed"])
        self.assertEqual((len(self.to(PROSPECT)), held_outcomes(s)), (1, []))

    def test_a_refused_send_surely_did_not_go_and_is_pending_again(self):
        real = live.LiveMail.send

        def refuses_t1(mail, raw, envelope, message_id, template=None):
            if template == "T1":
                raise MailError("jmap send: 400")
            return real(mail, raw, envelope, message_id, template=template)

        with mock.patch.object(live.LiveMail, "send", refuses_t1):
            s = self.started_fresh_then_asked()
        self.assertEqual(self.to(PROSPECT), [])
        lines = receipts.read_lines(s.receipts_path)
        sends = set(l["seq"] for l in lines if l["kind"] == "called" and l["payload"]["call"] == "send")
        results = [l["payload"]["state"] for l in lines if l["kind"] == "result" and l["payload"]["call_seq"] in sends]
        self.assertEqual(results, ["refused", "handed_over"])          # T1 refused, then the hold notice
        self.assertEqual(send_states(self.state), ["pending", "confirmed"])
        self.assertEqual(held_outcomes(s), ["hold:send_unconfirmed"])

    def test_a_send_that_may_have_gone_is_unknown_before_any_look_and_never_sent_again(self):
        self.jmap.on_deliver = None
        real = live.LiveMail.send
        states_at_look = []
        real_look = live.LiveMail.find_in_sent

        def times_out_after_the_submission(mail, raw, envelope, message_id, template=None):
            real(mail, raw, envelope, message_id, template=template)
            if template == "T1":
                raise SendUnknown("jmap send: timeout")

        def look(mail, message_id):
            states_at_look.append(send_states(self.state)[0])
            return real_look(mail, message_id)

        with mock.patch.object(live.LiveMail, "send", times_out_after_the_submission), \
                mock.patch.object(live.LiveMail, "find_in_sent", look):
            s = self.started_fresh_then_asked()
        self.assertEqual(states_at_look[0], "unknown")
        self.assertEqual(send_states(self.state)[0], "unknown")
        self.assertEqual(held_outcomes(s), ["hold:send_unconfirmed"])
        self.clock = u(2026, 12, 1, 16, 20)
        self.go()
        self.assertEqual(len(self.to(PROSPECT)), 1)


class HaltBeforeTheCallTest(LiveFixture):
    """A halt that lands after a send is recorded pending and before the transport is called: G1
    passed a moment before, but she reads the halt again right before the JMAP call, so nothing
    goes, the send stays pending, no send line is left without its answer, and the run after
    `resume` sends it once."""

    def setUp(self):
        super().setUp()
        patch = mock.patch.object(loop, "_sleep", lambda seconds: None)
        patch.start()
        self.addCleanup(patch.stop)

    def halted_after_recording(self, template):
        """Her 4:10 cycle: a stop is asked for right after the send of `template` is recorded pending."""
        self.go(fresh=True)
        self.inbox.add(request(1), u(2026, 12, 1, 16, 2))
        self.clock = u(2026, 12, 1, 16, 10)
        real = ledger_mod.Ledger.record_send

        def recorded_then_stopped(db, message_id, kind, template_, *args, **kw):
            out = real(db, message_id, kind, template_, *args, **kw)
            if template_ == template:
                supervisor.request_stop("live", "cli", state_dir=self.state)
            return out

        with mock.patch.object(ledger_mod.Ledger, "record_send", recorded_then_stopped):
            return self.go()

    def test_a_halt_between_pending_and_the_call_sends_nothing_and_the_restart_sends_once(self):
        s = self.halted_after_recording("T1")
        self.assertEqual(s.halted, "cli")
        self.assertEqual(self.jmap.requests, [])                  # not even a session: the call never began
        self.assertEqual(send_states(self.state), ["pending"])
        lines = receipts.read_lines(s.receipts_path)
        self.assertEqual([l for l in lines if l["kind"] == "called" and l["payload"]["call"] == "send"], [])
        (halted,) = [l["payload"] for l in lines if l["kind"] == "halted"]
        self.assertEqual((halted["cause"], halted["in_flight"]), ("cli", "none"))
        self.assertTrue(s.verify_ok)
        self.assertTrue(supervisor.resume("live", state_dir=self.state).ok)
        self.clock = u(2026, 12, 1, 16, 20)
        again = self.go()
        self.assertEqual([rcpts for _, rcpts, _ in self.jmap.delivered], [[PROSPECT, OWNER]])   # once
        self.assertEqual(send_states(self.state), ["confirmed"])
        self.assertEqual((again.handled, again.held, again.sends), (1, 0, 1))
        self.assertTrue(again.verify_ok)


def send_results(summary):
    """The state each send's result line names, in order."""
    lines = receipts.read_lines(summary.receipts_path)
    sends = set(l["seq"] for l in lines if l["kind"] == "called" and l["payload"]["call"] == "send")
    return [l["payload"]["state"] for l in lines if l["kind"] == "result" and l["payload"]["call_seq"] in sends]


class WolfNeverReadTest(LiveFixture):
    """A whole live run with the smoke's wolf.whizz settings in her env file: every name her loop
    asks the env for is one she reads (live.ENV_NAMES, loop.ENV_NAMES); the wolf token is in none of
    her objects' reach and in nothing she writes."""

    def test_a_live_run_asks_the_env_for_her_names_alone(self):
        wolf_token = SmokeOnlySettingsTest.WOLF_TOKEN
        self.env_path = self.write_env(WOLF_EMAIL="wolf.whizz@fastmail.com", WOLF_JMAP_TOKEN=wolf_token)
        asked = []

        class Spy(dict):
            def __getitem__(self, name):
                asked.append(name)
                return dict.__getitem__(self, name)

            def get(self, name, default=None):
                asked.append(name)
                return dict.get(self, name, default)

            def __contains__(self, name):
                asked.append(name)
                return dict.__contains__(self, name)

            def __iter__(self):
                asked.append("(every name)")
                return dict.__iter__(self)

            def keys(self):
                asked.append("(every name)")
                return dict.keys(self)

            def items(self):
                asked.append("(every name)")
                return dict.items(self)

            def values(self):
                asked.append("(every name)")
                return dict.values(self)

        real_load = envfile.load
        with mock.patch.object(envfile, "load", lambda *a, **kw: Spy(real_load(*a, **kw))):
            s = self.started_fresh_then_asked(model_source="live")
        self.assertEqual((s.handled, s.sends), (1, 1))
        self.assertLessEqual(set(asked), set(live.ENV_NAMES) | set(loop.ENV_NAMES))
        for folder, _, names in os.walk(self.dir):
            for name in names:
                if name != "test.env":
                    with open(os.path.join(folder, name), "rb") as fh:
                        self.assertNotIn(wolf_token.encode("ascii"), fh.read(), name)


class JmapAnswerTest(LiveFixture):
    """The send states over the fake JMAP server's own answers, nothing in LiveMail patched: a
    submission created is handed_over, written down before the Sent look; one turned away (a 4xx,
    or not created) surely did not go and is pending again; no answer in time, a lost connection, a
    5xx or an answer it cannot read once the submission request went is unknown, written down
    before any look, and never sent again, whatever a restart finds. A fault is staged on her
    first submission (T1) only; the hold notice after it goes as normal."""

    def setUp(self):
        super().setUp()
        self.waits, self.at_look = [], []
        real_look = live.LiveMail.find_in_sent

        def look(mail, message_id):
            self.at_look.append(send_states(self.state)[0])
            return real_look(mail, message_id)

        for patch in (mock.patch.object(loop, "_sleep", self.waits.append),
                      mock.patch.object(live.LiveMail, "find_in_sent", look)):
            patch.start()
            self.addCleanup(patch.stop)

    def to(self, address):
        return [data for _, rcpts, data in self.jmap.delivered if rcpts[0] == address]

    def first_send(self, **faults):
        """faults (fault=, status=, not_created=) on the first submission request only."""
        sends = []

        def staged(kind):
            if kind != "send":
                return
            sends.append(kind)
            self.jmap.fault.pop("send", None)
            self.jmap.status.pop("send", None)
            self.jmap.not_created.clear()
            if len(sends) == 1:
                if "fault" in faults:
                    self.jmap.fault["send"] = faults["fault"]
                if "status" in faults:
                    self.jmap.status["send"] = faults["status"]
                if "not_created" in faults:
                    self.jmap.not_created["EmailSubmission/set"] = faults["not_created"]
        self.jmap.on_request = staged

    def restart(self):
        """The next run, ten minutes on: it must send nothing more."""
        before = len(self.jmap.delivered)
        self.clock += dt.timedelta(minutes=10)
        again = self.go()
        self.assertEqual(len(self.jmap.delivered), before)
        self.assertTrue(again.verify_ok)
        return again

    def test_a_submission_created_is_handed_over_before_the_look_then_confirmed(self):
        s = self.started_fresh_then_asked()
        self.assertEqual(send_results(s), ["handed_over"])
        self.assertEqual(self.at_look[0], "handed_over")
        self.assertEqual(send_states(self.state), ["confirmed"])
        self.assertEqual((len(self.to(PROSPECT)), s.handled, s.held), (1, 1, 0))
        self.restart()

    def refused_once(self, **fault):
        self.first_send(**fault)
        s = self.started_fresh_then_asked()
        self.assertEqual(self.to(PROSPECT), [])
        self.assertEqual(send_results(s), ["refused", "handed_over"])        # T1, then its hold notice
        self.assertEqual(send_states(self.state), ["pending", "confirmed"])
        self.assertEqual(held_outcomes(s), ["hold:send_unconfirmed"])
        self.assertEqual(len(self.to(OWNER)), 1)
        self.restart()
        self.assertEqual(send_states(self.state), ["pending", "confirmed"])

    def test_a_submission_the_server_turned_away_surely_did_not_go_and_is_pending_again(self):
        self.refused_once(status=400)

    def test_a_submission_not_created_surely_did_not_go_and_is_pending_again(self):
        self.refused_once(not_created="forbiddenToSend")

    def unknown_once(self, filed, **fault):
        """T1 goes, then no clear answer: unknown before any look. Her copy filed in Sent settles it;
        not filed, it is held and never sent again."""
        if not filed:
            self.jmap.on_deliver = lambda sender, rcpts, message: (None if rcpts[0] == PROSPECT
                                                                   else self.sent.add(message, self.clock))
        self.first_send(**fault)
        s = self.started_fresh_then_asked()
        self.assertEqual(send_results(s)[0], "unknown")
        self.assertEqual(self.at_look[0], "unknown")
        self.assertEqual(len(self.to(PROSPECT)), 1)
        if filed:
            self.assertEqual(send_states(self.state), ["confirmed"])
            self.assertEqual((s.handled, s.held, len(self.to(OWNER))), (1, 0, 0))
        else:
            self.assertEqual(send_states(self.state), ["unknown", "confirmed"])
            self.assertEqual(held_outcomes(s), ["hold:send_unconfirmed"])
            self.assertEqual(self.waits, [caps.SENT_LOOK_WAIT_S])           # two looks for T1, the wait between
        self.restart()
        self.assertEqual(len(self.to(PROSPECT)), 1)

    def test_no_answer_in_time_after_the_submission_is_unknown_and_never_sent_again(self):
        self.jmap.hang_s = 1.0
        short = mock.patch.object(jmap, "_https", lambda host, port, timeout: http.client.HTTPConnection(
            "127.0.0.1", self.jmap.port, timeout=0.3))
        short.start()
        self.addCleanup(short.stop)
        self.unknown_once(False, fault="hang")

    def test_an_answer_it_cannot_read_after_the_submission_is_unknown_and_never_sent_again(self):
        self.unknown_once(False, fault="garbage")

    def test_a_server_error_after_the_submission_is_unknown_and_never_sent_again(self):
        self.unknown_once(False, fault=502)

    def test_a_connection_lost_after_the_submission_is_unknown_and_never_sent_again(self):
        self.unknown_once(False, fault="drop")

    def test_an_unknown_send_whose_copy_is_in_sent_is_confirmed_by_the_look(self):
        self.unknown_once(True, fault="garbage")


class Steps(object):
    """A kill at the k-th step of a run (review 2's probe, as a test): before and after each ledger
    write, each mail and calendar call and each receipt line. where names the step it landed on."""
    LEDGER = ("record_fetch", "record_message", "set_message_status", "record_send", "reset_send", "set_send_state",
              "send_refused", "create_offer", "set_offer_slots", "use_offer", "close_offers", "record_booking",
              "set_booking_state", "record_hold", "enqueue_action", "start_action", "finish_action", "start_run",
              "end_run")
    MAIL = ("send", "find_in_sent", "file", "fetch_new")
    CALENDAR = ("query", "get", "create_only", "delete")
    SENDING = ("after Ledger.set_send_state(sending)", "before LiveMail.send")   # marked sending, not yet sent

    def __init__(self, k):
        self.k, self.n, self.where = k, 0, None

    def point(self, label):
        self.n += 1
        if self.n == self.k:
            self.where = label
            raise Crash(label)

    def wrap(self, cls, name):
        real = getattr(cls, name)

        def wrapped(obj, *args, **kwargs):
            label = "%s.%s" % (cls.__name__, name) + ("(%s)" % (args[1],) if name == "set_send_state" else "")
            self.point("before " + label)
            out = real(obj, *args, **kwargs)
            self.point("after " + label)
            return out
        return mock.patch.object(cls, name, wrapped)

    @contextlib.contextmanager
    def armed(self):
        patches = ([self.wrap(ledger_mod.Ledger, n) for n in self.LEDGER] + [self.wrap(live.LiveMail, n) for n in self.MAIL]
                   + [self.wrap(live.LiveCalendar, n) for n in self.CALENDAR] + [self.wrap(receipts.ReceiptWriter, "write")])
        for p in patches:
            p.start()
        try:
            yield self
        finally:
            for p in patches:
                p.stop()


class KillAtEveryStepTest(LiveFixture):
    """Review 2, finding 1: a kill at every step of a book-and-reply pass, then two more runs. Never
    two replies, two notices or two events; every chain verifies; no send is left pending or sending.
    With her copy filed in Sent, the prospect gets T1 unless the kill landed between its sending mark
    and the transport (then the owner is told instead); with none filed, the owner is told once."""
    FILE_SENT = True

    def setUp(self):
        super().setUp()
        if not self.FILE_SENT:
            self.jmap.on_deliver = None
        patch = mock.patch.object(loop, "_sleep", lambda seconds: None)
        patch.start()
        self.addCleanup(patch.stop)

    def to(self, address):
        return [data for _, rcpts, data in self.jmap.delivered if rcpts[0] == address]

    def fresh_world(self):
        """A live ledger started fresh at 4:00 PM (the first one's copy, after the first), nothing in
        any box, and Priya's request in at 4:02; her clock at 4:10."""
        state, fresh = os.path.join(self.dir, "state"), os.path.join(self.dir, "fresh-state")
        shutil.rmtree(state, True)
        for box in (self.inbox, self.sent):
            box.messages.clear()
        del self.jmap.delivered[:]
        del self.dav.requests[:]
        self.dav.resources.clear()
        if os.path.isdir(fresh):
            shutil.copytree(fresh, state)
        else:
            self.prove_smoke(self.smoke_record())
            self.clock = u(2026, 12, 1, 16, 0)
            self.go(fresh=True)
            shutil.copytree(state, fresh)
        self.inbox.add(request(1), u(2026, 12, 1, 16, 2))
        self.clock = u(2026, 12, 1, 16, 10)

    def test_a_kill_at_any_step_never_sends_or_books_twice(self):
        k = 0
        while True:
            k += 1
            self.fresh_world()
            with Steps(k).armed() as steps:
                try:
                    self.go()
                except Crash:
                    pass
            if steps.where is None:
                break                       # the pass ended before step k: every step has had its kill
            after = []
            for minute in (20, 30):
                self.clock = u(2026, 12, 1, 16, minute)
                after.append(self.go())
            to_p, to_o = len(self.to(PROSPECT)), len(self.to(OWNER))
            with self.subTest(k=k, where=steps.where):
                self.assertLessEqual(to_p, 1)                   # never two replies
                self.assertLessEqual(to_o, 1)                   # never two notices
                self.assertEqual(len([r for r in self.dav.requests if r[0] == "PUT"]), 1)
                self.assertEqual(len(self.dav.resources), 1)
                self.assertTrue(all(s.verify_ok for s in after))
                self.assertFalse(set(send_states(self.state)) & {"pending", "sending"}, send_states(self.state))
                if self.FILE_SENT:
                    self.assertEqual(to_p + to_o, 1)
                else:
                    self.assertGreaterEqual(to_p + to_o, 1)
                if to_p == 0 or (to_o == 0 and not self.FILE_SENT):
                    self.assertIn(steps.where, Steps.SENDING)
        self.assertGreater(k, 100)


class KillAtEveryStepNoSentCopyTest(KillAtEveryStepTest):
    FILE_SENT = False


class DigestKillTest(LiveFixture):
    """Review 2, finding 1, the digest's send: a kill at every step of a run that sends the day's
    digest (an empty inbox: S2 is its one send), then two more runs that would send it. Never two
    digests; none only when the kill landed between its sending mark and the transport."""
    FILE_SENT = True

    def setUp(self):
        super().setUp()
        if not self.FILE_SENT:
            self.jmap.on_deliver = None
        patch = mock.patch.object(loop, "_sleep", lambda seconds: None)
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_kill_at_any_step_of_the_digest_never_sends_it_twice(self):
        k = 0
        state, fresh = os.path.join(self.dir, "state"), os.path.join(self.dir, "fresh-state")
        while True:
            k += 1
            shutil.rmtree(state, True)
            self.sent.messages.clear()
            del self.jmap.delivered[:]
            if os.path.isdir(fresh):
                shutil.copytree(fresh, state)
            else:
                self.prove_smoke(self.smoke_record(u(2026, 12, 2, 0, 0)))
                self.clock = u(2026, 12, 2, 1, 0)                # Tue Dec 1, 5:00 PM Pacific
                self.go(fresh=True)
                shutil.copytree(state, fresh)
            self.clock = u(2026, 12, 2, 1, 10)
            with Steps(k).armed() as steps:
                try:
                    self.go(digest=True)
                except Crash:
                    pass
            if steps.where is None:
                break
            after = []
            for minute in (20, 30):
                self.clock = u(2026, 12, 2, 1, minute)
                after.append(self.go(digest=True))
            digests = len(self.jmap.delivered)
            with self.subTest(k=k, where=steps.where):
                self.assertLessEqual(digests, 1)
                self.assertTrue(all(s.verify_ok for s in after))
                self.assertFalse(set(send_states(self.state)) & {"pending", "sending"}, send_states(self.state))
                if digests == 0:
                    self.assertIn(steps.where, Steps.SENDING)
        self.assertGreater(k, 20)


class DigestKillNoSentCopyTest(DigestKillTest):
    FILE_SENT = False


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

    def fetches_failing(self, failing, then=None):
        """LiveMail.fetch_new, patched for the test: the calls numbered in `failing` (from 1) raise
        MailError, the others read the inbox; then(n) runs before call n. Returns the call log."""
        real, calls = live.LiveMail.fetch_new, []

        def fetch(mail):
            calls.append(len(calls) + 1)
            if then is not None:
                then(calls[-1])
            if calls[-1] in failing:
                raise MailError("imap fetch: timeout")
            return real(mail)

        patch = mock.patch.object(live.LiveMail, "fetch_new", fetch)
        patch.start()
        self.addCleanup(patch.stop)
        return calls

    def fetch_results(self, summary):
        """(ok, the wall-clock wait before it) for each mail_fetch, in order."""
        lines = receipts.read_lines(summary.receipts_path)
        calls = {l["seq"] for l in lines if l["kind"] == "called" and l["payload"]["call"] == "mail_fetch"}
        return [l["payload"]["ok"] for l in lines if l["kind"] == "result" and l["payload"]["call_seq"] in calls]

    def test_a_failed_inbox_read_is_an_error_line_and_her_next_cycle_reads_again(self):
        """Review finding 4: in a run until stopped, a read that fails is never an empty inbox and never
        the end: an error line, a wait for her next cycle, and the mail is read then."""
        self.go(fresh=True)
        self.inbox.add(request(1), u(2026, 12, 1, 16, 2))
        self.clock = u(2026, 12, 1, 16, 10)
        calls = self.fetches_failing({1})
        fake = FakeTime(1000.0, lambda: self.jmap.delivered and supervisor.request_stop("live", "cli",
                                                                                         state_dir=self.state))
        self.use(fake)
        s = self.go(cycles=None, cycle_s=600.0)
        self.assertEqual((s.read_failed, s.halted), (False, "cli"))
        self.assertEqual((s.handled, s.sends, s.bookings), (1, 1, 1))
        self.assertEqual(self.fetch_results(s)[:2], [False, True])     # the failure, then her next cycle's read
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(fake.slept[:1], [1.0])                      # she waited before reading again
        lines = receipts.read_lines(s.receipts_path)
        (error,) = [l["payload"] for l in lines if l["kind"] == "error"]
        self.assertEqual((error["step"], error["error"], error["failed_in_a_row"], error["ends_after"]),
                         ("fetch", "MailError: imap fetch: timeout", 1, caps.MAIL_READ_FAILURES_IN_A_ROW))
        self.assertEqual([l["kind"] for l in lines[-2:]], ["halted", "summary"])
        self.assertTrue(s.verify_ok)
        self.assertEqual(self.said[0], "Tue Dec 1, 8:10 AM Pacific  Inbox not read: MailError (error line written). "
                                       "She tries again at her next cycle (1 failed in a row; the run ends at %d)."
                         % caps.MAIL_READ_FAILURES_IN_A_ROW)

    def test_reads_failing_in_a_row_end_the_run_with_its_closing_line(self):
        n = caps.MAIL_READ_FAILURES_IN_A_ROW
        self.assertGreaterEqual(n, 3)
        self.go(fresh=True)
        calls = self.fetches_failing(set(range(1, 1000)))
        fake = FakeTime(1000.0)
        self.use(fake)
        s = self.go(cycles=None, cycle_s=600.0)                     # ends by itself: no stop comes
        self.assertEqual((s.read_failed, s.halted), (True, None))
        self.assertEqual(len(calls), n)
        self.assertAlmostEqual(sum(fake.slept), 200.0 + 600.0 * (n - 2))    # a whole cycle between two reads
        lines = receipts.read_lines(s.receipts_path)
        errors = [l["payload"] for l in lines if l["kind"] == "error"]
        self.assertEqual([(e["step"], e["failed_in_a_row"]) for e in errors], [("fetch", k) for k in range(1, n + 1)])
        self.assertEqual(lines[-1]["kind"], "summary")
        self.assertEqual((lines[-1]["payload"]["read_failed"], lines[-1]["payload"]["errors"]), (True, n))
        self.assertTrue(s.verify_ok)

    def test_a_good_read_starts_the_count_again(self):
        n = caps.MAIL_READ_FAILURES_IN_A_ROW
        self.go(fresh=True)
        failing = set(range(1, n)) | set(range(n + 1, 2 * n))       # n - 1 failures, one read, n - 1 again
        calls = self.fetches_failing(failing, then=lambda k: k == 2 * n and supervisor.request_stop(
            "live", "cli", state_dir=self.state))
        self.use(FakeTime(1000.0))
        s = self.go(cycles=None, cycle_s=600.0)
        self.assertEqual((s.read_failed, s.halted), (False, "cli"))
        self.assertEqual(len(calls), 2 * n)
        errors = [l["payload"]["failed_in_a_row"] for l in receipts.read_lines(s.receipts_path) if l["kind"] == "error"]
        self.assertEqual(errors, list(range(1, n)) * 2)
        self.assertTrue(s.verify_ok)

    def test_a_finite_run_still_ends_on_its_one_failed_read(self):
        self.go(fresh=True)
        self.fetches_failing({1})
        s = self.go(cycles=2, cycle_s=600.0)
        self.assertTrue(s.read_failed)
        self.assertEqual(self.fetch_results(s), [False])

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

    def owner_mail(self):
        return [email.message_from_bytes(data, policy=email.policy.default)
                for _, rcpts, data in self.jmap.delivered if rcpts == [OWNER]]

    def test_the_days_digest_goes_at_her_first_cycle_from_five_pacific_on_the_wall_clock(self):
        from doris import supervisor
        self.go(fresh=True)                                  # Tue Dec 1, 8:00 AM Pacific
        self.clock = u(2026, 12, 2, 0, 40)                   # 4:40 PM Pacific
        waits = []

        def ten_minutes_on():
            waits.append(True)
            self.clock += dt.timedelta(minutes=10)
            if len(waits) == 4:
                supervisor.request_stop("live", "cli", state_dir=self.state)

        self.use(FakeTime(1000.0, ten_minutes_on))
        s = self.go(cycles=None, cycle_s=1.0, digest=True)  # her cycles at 4:40, 4:50, 5:00, 5:10 PM
        self.assertEqual(s.halted, "cli")
        self.assertEqual([(str(m["Date"]), str(m["Subject"])) for m in self.owner_mail()],
                         [("Wed, 02 Dec 2026 01:00:00 +0000", "Handled 0, held 0, human approvals 0. Tue Dec 1")])

    def test_a_digest_due_just_before_midnight_keeps_its_date_when_it_goes_after(self):
        from doris import supervisor
        self.go(fresh=True)
        self.clock = u(2026, 12, 2, 7, 59, 59)               # Tue Dec 1, 11:59:59 PM Pacific: due, none sent
        real, turned = receipts.read_lines, []

        def slow_read(path):                                 # her receipts take two seconds: Wednesday now
            if not turned:
                turned.append(path)
                self.clock = u(2026, 12, 2, 8, 0, 1)
            return real(path)

        self.use(FakeTime(1000.0, lambda: supervisor.request_stop("live", "cli", state_dir=self.state)))
        with mock.patch.object(receipts, "read_lines", slow_read):
            self.go(cycles=None, cycle_s=1.0, digest=True)
        self.assertEqual([(str(m["Subject"]), str(m["Message-ID"])) for m in self.owner_mail()],
                         [("Handled 0, held 0, human approvals 0. Tue Dec 1",
                           compose.new_message_id(DORIS, "digest:2026-12-01", "S2", 1))])

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

        def no_token():
            return ["--env", self.write_env(DORIS_JMAP_TOKEN=None), "--fresh"]

        def bad_token():
            return ["--env", self.write_env(DORIS_JMAP_TOKEN="dummy-" + "\u00e9" * 40), "--fresh"]

        def no_proof():
            self.prove_smoke(None)
            return ["--env", self.write_env(), "--fresh"]

        def loose_proof():
            self.prove_smoke('{"sent_copy_found": true}')
            return ["--env", self.write_env(), "--fresh"]

        def stale_proof():
            self.prove_smoke(self.smoke_record(u(2026, 11, 30, 15, 59, 59)))     # a day and a second before
            return ["--env", self.write_env(), "--fresh"]

        def no_ledger():
            return ["--env", self.write_env()]

        def fresh_over_a_ledger():
            ledger_mod.start_fresh(ledger_path, u(2026, 11, 30, 12, 0))
            return ["--env", self.write_env(), "--fresh"]

        cases = ((no_env, "absent.env"), (no_owner, "DORIS_OWNER_ADDRESS"), (no_key, "ANTHROPIC_API_KEY"),
                 (no_token, "DORIS_JMAP_TOKEN"), (bad_token, "DORIS_JMAP_TOKEN"),
                 (no_proof, loop.SMOKE_REFUSAL),
                 (loose_proof, "Not started: the smoke check's record (smoke-sent.json) is not as the smoke writes "
                               "it: its keys are not exactly look, sent_copy_found, ts, waited_s. " + loop.SMOKE_FIRST),
                 (stale_proof, "Not started: the smoke check's record (smoke-sent.json) is more than 24 hours old. "
                               + loop.SMOKE_FIRST),
                 (no_ledger, loop.NO_LIVE_LEDGER),
                 (fresh_over_a_ledger, loop.FRESH_OVER_LEDGER))
        for setup, said in cases:
            with self.subTest(case=setup.__name__):
                self.prove_smoke(self.smoke_record())
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

    def test_a_smoke_that_finds_its_copy_lets_run_live_start_and_one_that_does_not_stops_it(self):
        """Both commands as a person types them, on one live state folder: the record the smoke
        writes is the one the run's preflight reads."""
        patch = mock.patch.object(smoke, "SENT_LOOK_WAIT_S", 0.0)       # her second look in Sent at once
        patch.start()
        self.addCleanup(patch.stop)
        self.prove_smoke(None)
        code, out = self.cli("smoke", "--env", self.env_path, "--send-to", "whizz@fastmail.com")
        self.assertEqual(code, 0, out)
        self.assertIn("\nsent copy record for a live run: ok (written)\n", out)
        self.clock = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)    # the run starts right after
        code, out = self.cli("run", "--live", "--fresh", "--env", self.env_path)
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.started), 1)
        self.jmap.on_deliver = lambda sender, rcpts, message: None          # no copy reaches Sent now
        code, out = self.cli("smoke", "--env", self.env_path, "--send-to", "whizz@fastmail.com")
        self.assertEqual(code, 1, out)
        code, out = self.cli("run", "--live", "--fresh", "--env", self.env_path)
        self.assertEqual((code, out.strip()), (2, loop.SMOKE_REFUSAL))
        self.assertEqual(len(self.started), 1)

    def test_a_halt_in_force_refuses_before_the_env_file_is_read(self):
        supervisor.request_stop("live", "cli")
        code, out = self.cli("run", "--live", "--fresh", "--env", os.path.join(self.dir, "absent.env"))
        self.assertEqual(code, supervisor.EXIT_REFUSED_HALT, out)
        self.assertNotIn("env file", out)
        self.assertEqual(self.started, [])

    def test_without_env_a_live_start_reads_the_one_default_the_smoke_reads(self):
        """paths.LIVE_ENV is the one default env file: `run --live` and `smoke` both read it."""
        self.assertEqual(paths.LIVE_ENV, "/root/doris-secrets/.env")
        with mock.patch.object(paths, "LIVE_ENV", self.env_path):
            code, out = self.cli("run", "--live", "--fresh")
        self.assertEqual(code, 0, out)
        (started,) = self.started
        with open(started["argv"][4], encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["env_path"], self.env_path)
        self.assertIn(self.env_path, out)

    def test_a_live_start_writes_a_config_that_names_the_env_file_and_holds_no_setting(self):
        # A relative --env, given from the folder that holds the file: on Windows CI the checkout and
        # the temp folder sit on different drives, with no relative path between them.
        here = os.getcwd()
        os.chdir(self.dir)
        try:
            given = os.path.relpath(self.env_path)
            code, out = self.cli("run", "--live", "--fresh", "--env", given)
            named = os.path.abspath(given)
        finally:
            os.chdir(here)
        self.assertFalse(os.path.isabs(given), given)
        self.assertTrue(os.path.samefile(named, self.env_path), named)
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
                         {"mode": "live", "state_dir": self.state, "env_path": named, "fresh": True,
                          "model_source": "live", "clock_mode": "fixed", "digest": True, "cycles": None,
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
            elif self.jmap.delivered and not stopped:
                stopped.append(supervisor.request_stop("live", "cli"))

        fake = FakeTime(1199.0, tick)
        for patch in (mock.patch.object(loop, "_time", fake.time), mock.patch.object(loop, "_sleep", fake.sleep)):
            patch.start()
            self.addCleanup(patch.stop)
        code, out = self.cli(cli.WORKER, started["argv"][4])
        self.assertEqual(code, supervisor.WORKER_EXIT_HALTED, out)
        self.assertEqual(len(stopped), 1)
        self.assertEqual([rcpts[0] for _, rcpts, _ in self.jmap.delivered], [PROSPECT])
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
