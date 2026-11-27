"""Live mode in the loop: her settings from the env file alone, the live ledger's fresh start, the
live adapters under the network guard, and the meeting link and the model key from the env file.

The real LiveMail and LiveCalendar talk to test_live's fake IMAP, SMTP and CalDAV servers on
127.0.0.1 (the adapters' default transports swapped for plain connections to them), as in
test_smoke. No test reads a real env file or reaches a real server.
"""
import os
import shutil
import tempfile
import traceback
import unittest
from unittest import mock

from doris import loop, model as model_mod
from tests.unit.test_live import COLLECTION, DORIS, OWNER, PASSWORD

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


if __name__ == "__main__":
    unittest.main()
