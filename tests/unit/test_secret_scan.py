"""The secret scan blocks planted keys and never prints them."""
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

import secret_scan

# Fake keys are built at runtime so this file itself never holds a key shape.
FAKE_ANTHROPIC = "sk-" + "ant-" + "api03-" + "Zq7" * 12
FAKE_GITHUB = "gh" + "p_" + "aB3dE5" * 6
FAKE_AWS = "AK" + "IA" + "QWERTYUIOP234567"
FAKE_PASSWORD = "q8w7e6r5" + "t4y3u2i1"
FAKE_TOKEN = "fmu1-" + "a7Kq2Zr9" * 4


class SecretScanTest(unittest.TestCase):
    def scan(self, text, env_values=(), terms=()):
        return list(secret_scan.scan_text(text, list(env_values), list(terms)))

    def test_planted_anthropic_key_blocked(self):
        self.assertEqual(self.scan("ANTHROPIC_API_KEY=" + FAKE_ANTHROPIC), [(1, "anthropic_key")])

    def test_github_and_aws_blocked(self):
        self.assertEqual(self.scan("x\n" + FAKE_GITHUB)[0], (2, "github_token"))
        self.assertEqual(self.scan(FAKE_AWS)[0][1], "aws_access_key")

    def test_secret_assignment_blocked_placeholder_allowed(self):
        self.assertEqual(self.scan('app_password = "' + FAKE_PASSWORD + '"'), [(1, "secret_assignment")])
        self.assertEqual(self.scan("ANTHROPIC_API_KEY=your-key-here-replace-me"), [])
        self.assertEqual(self.scan("password = os.environ.get('DORIS_APP_PASSWORD')"), [])

    def test_env_value_blocked(self):
        self.assertEqual(self.scan("join at https://meet.example/room-42", ["https://meet.example/room-42"]), [(1, "env_value")])

    def test_private_term_blocked_case_insensitive(self):
        self.assertEqual(self.scan("Hello Secret Project", terms=["secret project"]), [(1, "private_term_1")])

    def test_env_file_reader_skips_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "env")
            with open(path, "w") as fh:
                fh.write("# comment\nDORIS_MODEL=claude-model-x\nDORIS_IMAP_HOST=imap.example.com\n"
                         "DORIS_EMAIL=doris@example.com\nDORIS_APP_" + "PASSWORD='" + FAKE_PASSWORD + "'\nSHORT=abc\n")
            self.assertEqual(secret_scan.read_env_values([path]), [FAKE_PASSWORD])

    def scan_files(self, env_text, files):
        """main() over these files, with only this .env read (never the machine's own) and no terms."""
        with tempfile.TemporaryDirectory() as tmp:
            env = os.path.join(tmp, "env")
            with open(env, "w", encoding="utf-8") as fh:
                fh.write(env_text)
            paths = []
            for name, text in files:
                paths.append(os.path.join(tmp, name))
                with open(paths[-1], "w", encoding="utf-8") as fh:
                    fh.write(text)
            out = io.StringIO()
            with mock.patch.object(secret_scan, "DEFAULT_ENV_FILES", ()), redirect_stdout(out), \
                    mock.patch.dict(os.environ, {"DORIS_ENV_FILE": env, "DORIS_SCAN_TERMS": os.path.join(tmp, "none")}):
                code = secret_scan.main(paths)
            return code, out.getvalue(), secret_scan.read_env_values([env])

    def test_a_plain_address_is_a_setting_under_any_name(self):
        """Smoke run 2's trace was blocked: a stray OWNER_CALENDAR (read by no code) held the owner's
        address, and wolf's address, in the receipts by design, holds it. An address is a setting."""
        owner = "kestrel@mailhost.example"
        code, out, values = self.scan_files(
            "OWNER_CALENDAR=%s\nSTRAY_NOTE='Kestrel.Owner+cal@Mail-Host.example'\nDORIS_EMAIL=doris@mailhost.example\n"
            % owner, [("receipts.jsonl", '{"kind":"observed","payload":{"from_addr":"wolf.%s"}}\n' % owner)])
        self.assertEqual((code, values), (0, []), out)
        self.assertIn("clean (0 .env value(s)", out)

    def test_an_address_shaped_value_under_a_key_that_names_a_secret_still_blocks(self):
        """L2: the plain-address exemption let IMAP_PASSWORD=<an address-shaped value> through. It
        holds only when the key names no secret (PASS, TOKEN, SECRET, KEY, in any case)."""
        shaped = FAKE_PASSWORD + "@mailhost.example"
        for key in ("IMAP_" + "PASSWORD", "WOLF_JMAP_" + "TOKEN", "app_" + "secret", "Api_" + "Key", "IMAP_PASS"):
            with self.subTest(key=key):
                code, out, values = self.scan_files("%s=%s\n" % (key, shaped), [("trace.txt", "login " + shaped + "\n")])
                self.assertEqual(values, [shaped])
                self.assertEqual(code, 1, out)
                self.assertIn(" env_value", out)
                self.assertNotIn(shaped, out)

    def test_a_value_that_is_not_a_plain_address_still_blocks_under_any_name(self):
        url = "imaps://kestrel:" + FAKE_PASSWORD + "@imap.mailhost.example"
        env = "OWNER_CALENDAR=%s\nSTRAY_KEY=%s\nDORIS_IMAP_URL=%s\nNOTE=%s@mailhost.example\n" % (
            FAKE_TOKEN, FAKE_PASSWORD + "x", url, FAKE_PASSWORD + " x")
        lines = ["token " + FAKE_TOKEN, "pass " + FAKE_PASSWORD + "x", "url " + url,
                 "note " + FAKE_PASSWORD + " x@mailhost.example"]
        code, out, values = self.scan_files(env, [("trace-%d.txt" % i, line + "\n") for i, line in enumerate(lines)])
        self.assertEqual(values, [FAKE_TOKEN, FAKE_PASSWORD + "x", url, FAKE_PASSWORD + " x@mailhost.example"])
        self.assertEqual(code, 1, out)
        self.assertEqual(out.count(" env_value"), 4, out)
        for value in values:
            self.assertNotIn(value, out)

    def test_forbidden_file_names(self):
        self.assertEqual(secret_scan.scan_blob("sub/.env", b"", [], []), [("sub/.env", 0, "forbidden_file")])
        self.assertEqual(secret_scan.scan_blob(".env.example", b"ANTHROPIC_API_KEY=\n", [], []), [])

    def test_main_never_prints_the_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "leak.txt")
            with open(path, "w") as fh:
                fh.write("token " + FAKE_ANTHROPIC + "\n")
            out = io.StringIO()
            with redirect_stdout(out):
                code = secret_scan.main([path])
            self.assertEqual(code, 1)
            self.assertNotIn(FAKE_ANTHROPIC, out.getvalue())
            self.assertIn("anthropic_key", out.getvalue())


if __name__ == "__main__":
    unittest.main()
