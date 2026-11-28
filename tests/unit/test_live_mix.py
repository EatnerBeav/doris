"""tests/tools/live_mix.py: the full live loop's test sender. Nothing here contacts anything: the mix
plays against a throwaway sandbox (her real loop, the stand-in model, the sandbox's busy times plus
the soak's booking).
"""

import datetime as dt
import importlib.util
import json
import os
import re
import sys
import unittest

from doris import screens
from doris.adapters import jmap

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOLS = os.path.join(REPO, "tests", "tools")
WOLF = "wolf.whizz@fastmail.com"
OWNER = "whizz@fastmail.com"
UTC = dt.timezone.utc


def load(name):
    if TOOLS not in sys.path:
        sys.path.insert(0, TOOLS)
    spec = importlib.util.spec_from_file_location(name, os.path.join(TOOLS, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mix = load("live_mix")


class Plan(unittest.TestCase):

    def test_no_email_in_the_plan_is_stop_mail(self):
        # one stop would silence wolf on the live ledger for good, and the overnight run sends from wolf
        for th in mix.MIX:
            for step in th.steps:
                self.assertIsNone(screens.stop_match(step.body.replace("{nth}", "first")), th.key)

    def test_its_dates_stay_clear_of_the_overnight_run_and_the_video_takes(self):
        # the overnight plan books Dec 7 to 18; the video keeps Thu Dec 3 and Fri Dec 4 for Rae
        for th in mix.MIX:
            for step in th.steps:
                for day in re.findall(r"December (\d+)|2026-12-(\d\d)", step.body):
                    self.assertIn(int(day[0] or day[1]), (1, 2), th.key)

    def test_every_email_is_one_plain_part_her_sender_check_takes(self):
        now = dt.datetime(2026, 11, 28, 16, 30, tzinfo=UTC)
        for th in mix.MIX:
            for i, step in enumerate(th.steps):
                mid = "<%032x@fastmail.com>" % (i + 1)
                raw = mix.od.build(WOLF, OWNER, step.subject or th.subject, step.body.replace("{nth}", "first"),
                                   mid, now, None if i == 0 else "<her@fastmail.com>", (), step.headers)
                jmap._checked(raw, [OWNER], mid, WOLF, "test")

    def test_the_rehearsal_meets_every_step_and_leaves_the_overnight_drip_as_it_was(self):
        log = []
        before = mix.od.Drip, mix.od.GAP_S
        tally = mix.rehearse(log.append)
        self.assertEqual([r for r in tally["rows"] if r["grade"] != "met"], [])
        self.assertEqual(tally["emails"], mix.od.planned_emails(mix.MIX))
        self.assertEqual(tally["answered_twice"], 0)
        self.assertEqual((mix.od.Drip, mix.od.GAP_S), before)
        text = json.dumps(log)
        for value in (WOLF, OWNER, "Hi Shane", "Intro call"):
            self.assertNotIn(value, text)


if __name__ == "__main__":
    unittest.main()
