"""mail.fold_for_match: the one fold the stop scan (screens) and the sensitive screen match on.

It must give exactly what the per-character version it replaced gave (NFKC, then every format
character dropped) on every character there is, so screens 4 and 5 decide as before, only faster.
"""

import os
import sys
import unicodedata
import unittest

from doris import mail, paths, screens, sensitive


def reference(text):
    """The fold as it was written before: one category lookup per character."""
    text = unicodedata.normalize("NFKC", text or "")
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")


class FoldTest(unittest.TestCase):
    def test_every_code_point_folds_as_before(self):
        step = 4096
        for start in range(0, sys.maxunicode + 1, step):
            text = "".join(chr(cp) for cp in range(start, min(start + step, sys.maxunicode + 1)))
            with self.subTest(block=hex(start)):
                self.assertEqual(mail.fold_for_match(text), reference(text))

    def test_text_as_mail_carries_it_folds_as_before(self):
        samples = [
            None, "", "plain ascii, no change\nsecond line",
            "st​op", "S‍TOP‎", "stop﻿ emailing­ me", "‮stop‬",
            "ｓｔｏｐ em⁠ailing me", "ﬁne Ω ½ Ⅳ",
            "café naïve \U0001d400\U0001f600", "؀؁؜۝܏\U000110bd",
            "\U000e0001\U000e0020\U000e007f tags", "\ud800 lone surrogate",
        ]
        inbox = os.path.join(paths.SANDBOX, "inbox")
        for name in sorted(os.listdir(inbox)):
            with open(os.path.join(inbox, name), "rb") as fh:
                samples.append(fh.read().decode("utf-8", "replace"))
        for text in samples:
            with self.subTest(text=repr(text)[:60]):
                self.assertEqual(mail.fold_for_match(text), reference(text))

    def test_the_stop_scan_and_the_sensitive_screen_use_it(self):
        self.assertEqual(sensitive._fold("one​-time c­ode"), reference("one​-time c­ode"))
        self.assertEqual(screens.stop_match("St​op em⁠ailing me, please"), "stop emailing me")
        self.assertEqual(screens.stop_match("S​T‍O‎P"), "stop")


if __name__ == "__main__":
    unittest.main()
