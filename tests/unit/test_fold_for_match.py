"""mail.fold_for_match: the one fold the stop scan (screens) and the sensitive screen match on.

It must give exactly what the per-character version it replaced gave (NFKC, then every format
character dropped) on every character there is, so screens 4 and 5 decide as before, only faster.
Every unusual character below is written as an escape: typed raw, an editor could drop it unseen.
"""

import os
import re
import sys
import unicodedata
import unittest

from doris import mail, paths, screens, sensitive


def reference(text):
    """The fold as it was written before: one category lookup per character."""
    text = unicodedata.normalize("NFKC", text or "")
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")


def old_stop_norm(text):
    """screens._norm as it was written before."""
    t = reference(text).lower()
    for a in screens._APOSTROPHES:
        t = t.replace(a, "")
    t = re.sub(r"[^\w\s]|_", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def old_sensitive_words(text):
    """sensitive._words as it was written before."""
    t = reference(text).lower()
    for a in sensitive._APOS:
        t = t.replace(a, "")
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


SAMPLES = [
    None, "", "plain ascii, no change\nsecond line",
    "st\u200bop", "S\u200dTOP\u200e", "stop\ufeff emailing\xad me", "\u202estop\u202c",
    "\uff53\uff54\uff4f\uff50 em\u2060ailing me", "\ufb01ne \u2126 \xbd \u2163",
    "cafe\u0301 na\xefve \U0001d400\U0001f600", "\u0600\u0601\u061c\u06dd\u070f\U000110bd",
    "\U000e0001\U000e0020\U000e007f tags", "\ud800 lone surrogate",
    "Don\u2019t email me", "remove\xa0me\u2028please", "STOP\r\n\u200bUnsubscribe\u200c",
]


def corpus():
    out = list(SAMPLES)
    inbox = os.path.join(paths.SANDBOX, "inbox")
    for name in sorted(os.listdir(inbox)):
        with open(os.path.join(inbox, name), "rb") as fh:
            out.append(fh.read().decode("utf-8", "replace"))
    return out


class FoldTest(unittest.TestCase):
    def test_the_samples_hold_what_they_say(self):
        self.assertTrue(all(ord(c) < 128 for c in open(__file__, encoding="utf-8").read()))
        self.assertEqual(unicodedata.category(SAMPLES[3][2]), "Cf")

    def test_every_code_point_folds_as_before(self):
        step = 4096
        for start in range(0, sys.maxunicode + 1, step):
            text = "".join(chr(cp) for cp in range(start, min(start + step, sys.maxunicode + 1)))
            with self.subTest(block=hex(start)):
                self.assertEqual(mail.fold_for_match(text), reference(text))

    def test_text_as_mail_carries_it_folds_as_before(self):
        for text in corpus():
            with self.subTest(text=repr(text)[:60]):
                self.assertEqual(mail.fold_for_match(text), reference(text))

    def test_the_stop_scan_and_the_sensitive_screen_read_as_before(self):
        for text in corpus():
            with self.subTest(text=repr(text)[:60]):
                self.assertEqual(screens._norm(text), old_stop_norm(text))
                self.assertEqual(sensitive._fold(text), reference(text))
                self.assertEqual(sensitive._words(text), old_sensitive_words(text))
        self.assertEqual(screens.stop_match("St\u200bop em\u2060ailing me, please"), "stop emailing me")
        self.assertEqual(screens.stop_match("S\u200bT\u200dO\u200eP"), "stop")


if __name__ == "__main__":
    unittest.main()
