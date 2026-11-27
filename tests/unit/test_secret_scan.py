"""The secret scan blocks planted keys and never prints them."""
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout

import secret_scan

# Fake keys are built at runtime so this file itself never holds a key shape.
FAKE_ANTHROPIC = "sk-" + "ant-" + "api03-" + "Zq7" * 12
FAKE_GITHUB = "gh" + "p_" + "aB3dE5" * 6
FAKE_AWS = "AK" + "IA" + "QWERTYUIOP234567"
FAKE_PASSWORD = "q8w7e6r5" + "t4y3u2i1"


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
