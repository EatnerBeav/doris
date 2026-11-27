"""Live mode in the loop: her settings from the env file alone, the live ledger's fresh start, the
live adapters under the network guard, and the meeting link and the model key from the env file.

The real LiveMail and LiveCalendar talk to test_live's fake IMAP, SMTP and CalDAV servers on
127.0.0.1 (the adapters' default transports swapped for plain connections to them), as in
test_smoke. No test reads a real env file or reaches a real server.
"""
import email
import email.policy
import http.client
import imaplib
import json
import os
import shutil
import smtplib
import tempfile
import traceback
import unittest
from unittest import mock

from doris import caps, ledger as ledger_mod
from doris import loop, model as model_mod, netguard, receipts, templates
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
        self.assertEqual(send_states(self.state), ["unknown", "unknown"])
        (held,) = [l["payload"] for l in receipts.read_lines(s.receipts_path) if l["kind"] == "held"]
        self.assertEqual(held["outcome"], "hold:send_unconfirmed")

    def test_a_copy_found_at_once_needs_no_wait(self):
        self.smtp.on_deliver = lambda sender, rcpts, message: self.sent.add(message, self.clock)
        s = self.started_fresh_then_asked()
        self.assertEqual(self.waits, [])
        self.assertEqual(sent_lookups(s), [(None, True)])


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


if __name__ == "__main__":
    unittest.main()
