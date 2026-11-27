"""envfile.load: a plain KEY=VALUE .env file, read without ever printing or echoing a value."""
import os
import shutil
import tempfile
import unittest
from unittest import mock

from doris import envfile, loop
from doris.adapters import live

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# A stand-in secret: long enough to matter, marked dummy so the secret scan knows it is not real.
VALUE = "dummy-value-7f3a9c"


class Fixture(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="doris-envfile-test-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def write(self, data):
        path = os.path.join(self.dir, ".env")
        with open(path, "wb") as fh:
            fh.write(data if isinstance(data, bytes) else data.encode("utf-8"))
        return path

    def load_text(self, text):
        return envfile.load(self.write(text))

    def refused(self, text):
        with self.assertRaises(envfile.EnvFileError) as cm:
            self.load_text(text)
        return str(cm.exception)


class ReadsTest(Fixture):
    def test_reads_key_value_pairs_as_strings(self):
        self.assertEqual(self.load_text("A=1\nDORIS_IMAP_HOST=imap.fastmail.com\n"),
                         {"A": "1", "DORIS_IMAP_HOST": "imap.fastmail.com"})

    def test_skips_comments_and_blank_lines(self):
        text = "# a comment\n\n   \n  # an indented comment\nA=1\n"
        self.assertEqual(self.load_text(text), {"A": "1"})

    def test_strips_one_pair_of_matching_quotes_and_keeps_the_inside_verbatim(self):
        text = "A=\"%s\"\nB='%s'\nC=\" spaced \"\nD=\"it's\"\nE='say \"hi\"'\n" % (VALUE, VALUE)
        self.assertEqual(self.load_text(text),
                         {"A": VALUE, "B": VALUE, "C": " spaced ", "D": "it's", "E": 'say "hi"'})

    def test_no_interpolation_and_equals_signs_kept(self):
        text = "A=$HOME\nB=${A}x\nC=a=b==c\n"
        self.assertEqual(self.load_text(text), {"A": "$HOME", "B": "${A}x", "C": "a=b==c"})

    def test_whitespace_around_name_and_unquoted_value_is_trimmed(self):
        self.assertEqual(self.load_text("  A = 1  \n"), {"A": "1"})

    def test_empty_value_is_kept_as_empty(self):
        self.assertEqual(self.load_text("A=\nB=''\n"), {"A": "", "B": ""})

    def test_crlf_line_endings_and_a_byte_order_mark(self):
        self.assertEqual(envfile.load(self.write(b"\xef\xbb\xbfA=1\r\nB=2\r\n")), {"A": "1", "B": "2"})

    def test_the_source_writes_the_byte_order_mark_as_an_escape(self):
        """An invisible U+FEFF in the source is one an editor can drop without anyone seeing it."""
        with open(envfile.__file__, "rb") as fh:
            self.assertFalse(b"\xef\xbb\xbf" in fh.read(), "a literal U+FEFF in the source")

    def test_never_touches_the_process_environment(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            before = dict(os.environ)
            self.load_text("DORIS_TEST_ONLY_NAME=%s\n" % VALUE)
            self.assertEqual(dict(os.environ), before)


class RefusesTest(Fixture):
    def test_export_is_not_part_of_the_format(self):
        message = self.refused("A=1\nexport B=%s\n" % VALUE)
        self.assertIn("line 2", message)
        self.assertIn("export", message)
        self.assertNotIn(VALUE, message)

    def test_a_line_without_equals_names_its_line_only(self):
        message = self.refused("A=1\n%s\n" % VALUE)
        self.assertIn("line 2", message)
        self.assertNotIn(VALUE, message)

    def test_a_bad_name_is_refused_without_echoing_it(self):
        message = self.refused("1BAD=%s\n" % VALUE)
        self.assertIn("line 1", message)
        self.assertNotIn("1BAD", message)
        self.assertNotIn(VALUE, message)

    def test_an_unbalanced_quote_is_refused(self):
        for text in ("A=\"%s\n" % VALUE, "A='%s\n" % VALUE, "A=%s\"\n" % VALUE, "A=\"\n"):
            message = self.refused(text)
            self.assertIn("line 1", message)
            self.assertNotIn(VALUE, message)

    def test_a_name_set_twice_is_refused(self):
        message = self.refused("A=1\nA=%s\n" % VALUE)
        self.assertIn("line 2", message)
        self.assertNotIn(VALUE, message)

    def test_a_missing_file_raises_envfile_error(self):
        with self.assertRaises(envfile.EnvFileError) as cm:
            envfile.load(os.path.join(self.dir, "absent.env"))
        self.assertIn("FileNotFoundError", str(cm.exception))

    def test_a_file_that_is_not_utf8_is_refused(self):
        with self.assertRaises(envfile.EnvFileError):
            envfile.load(self.write(b"A=\xff\xfe\n"))

    def test_envfile_error_is_a_value_error(self):
        self.assertTrue(issubclass(envfile.EnvFileError, ValueError))


class ExampleTest(unittest.TestCase):
    """The repo's .env.example: every name Doris reads, no value, one comment line each."""

    def setUp(self):
        self.path = os.path.join(REPO, ".env.example")

    def test_names_every_setting_with_no_value(self):
        values = envfile.load(self.path)
        self.assertLessEqual(set(live.ENV_NAMES) | set(loop.ENV_NAMES), set(values))
        self.assertEqual(set(values.values()), {""})

    def test_each_name_has_exactly_one_comment_line_above_it(self):
        with open(self.path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        names = [i for i, line in enumerate(lines) if line.strip() and not line.startswith("#")]
        self.assertTrue(names)
        for i in names:
            self.assertTrue(lines[i - 1].startswith("# "), lines[i])
            self.assertFalse(i >= 2 and lines[i - 2].startswith("#"), lines[i])


if __name__ == "__main__":
    unittest.main()
