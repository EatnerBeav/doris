"""Every path a run writes, and every path in the repository, fits Windows' default path limit from
any clone folder up to about 115 characters.

Windows refuses a path of 260 characters or more (MAX_PATH, the drive and the final NUL included)
unless LongPathsEnabled is set, which it is not by default. The clone folder comes first in every
path a run opens: C:\\Users\\<name>\\AppData\\Local\\Temp\\doris-fresh-<stamp> was 63 characters on
Shane's PC, where `traps` failed 19 of 20 on a 264-character path to a message copy. So no path a run
writes under the repository, and no committed path, may be longer than BUDGET characters from the
repository root. Measured on any system: the layout is the same on all of them.
"""
import contextlib
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from doris import __main__ as cli
from doris import caps, paths, traps
from tests.unit import demo_sandbox

BUDGET = 140        # characters from the repository root: 259 - 140 - 1 leaves a clone folder up to 118
STAMP = "20261128T0146Z"


def written(root):
    """Every folder and file under root, as "/"-separated paths relative to it."""
    found = []
    for folder, dirs, files in os.walk(root):
        for name in dirs + files:
            found.append(os.path.relpath(os.path.join(folder, name), root).replace(os.sep, "/"))
    return found


class RunPathsTest(unittest.TestCase):
    """The Trap Pack and the demo day, each laid out in a temporary folder as they are in the
    repository (state/, traces/, tests/trap-pack/, sandbox/)."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="doris-paths-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def assert_within_budget(self, found):
        longest = max(found, key=len)
        self.assertLessEqual(len(longest), BUDGET, "%d characters: %s" % (len(longest), longest))

    def test_a_trap_pack_run_writes_no_path_longer_than_the_budget(self):
        # Twice in one minute: the second run's name, and its state folder's, carry "-2".
        for _ in range(2):
            traps.run_traps(paths.TRAP_PACK, model_source="stub",
                            state_root=os.path.join(self.root, "state", "throwaway"),
                            traces_root=os.path.join(self.root, "traces"),
                            results_path=os.path.join(self.root, "tests", "trap-pack", "RESULTS.md"), stamp=STAMP)
        # and a judge's run, its report and traces under state/throwaway/traces/ (the default)
        with mock.patch.object(paths, "STATE", os.path.join(self.root, "state")), \
                mock.patch.object(paths, "TRACES", os.path.join(self.root, "traces")), \
                mock.patch.object(paths, "TESTS", os.path.join(self.root, "tests")):
            traps.run_traps(paths.TRAP_PACK, model_source="stub", stamp=STAMP)
        found = written(self.root)
        self.assertTrue([p for p in found if p.startswith("state/throwaway/traces/") and p.endswith("/RESULTS.md")])
        self.assertGreater(len([p for p in found if "/messages/" in p]), 30)    # her copies are among them
        self.assert_within_budget(found)
        # the same runs under the longest model source name a traps run takes (replayed, four more letters)
        self.assertEqual(max(traps.TRAPS_MODEL_SOURCES, key=len), "replayed")
        self.assert_within_budget([p.replace("stub", "replayed") for p in found])

    def test_the_demo_day_writes_no_path_longer_than_the_budget(self):
        sandbox = demo_sandbox.copy_committed(os.path.join(self.root, "sandbox"))
        for target, name, value in ((paths, "SANDBOX", sandbox), (paths, "STATE", os.path.join(self.root, "state")),
                                    (caps, "CALENDAR_TIMEOUT_S", 0.3), (caps, "CALENDAR_RETRY_WAIT_S", 0.0)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "", "DORIS_MODEL": ""}), contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["demo"]), 0, out.getvalue())
        found = written(self.root)
        self.assertTrue([p for p in found if p.startswith("state/sandbox/runs/") and "/messages/" in p], found)
        self.assert_within_budget(found)


class CommittedPathsTest(unittest.TestCase):

    def test_every_committed_path_is_within_the_budget(self):
        try:
            listed = subprocess.run(["git", "-C", paths.REPO, "ls-files", "-z"], stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=60, env=demo_sandbox.git_env())
        except OSError:
            self.skipTest("git is not installed")
        top = subprocess.run(["git", "-C", paths.REPO, "rev-parse", "--show-prefix"], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, timeout=60, env=demo_sandbox.git_env())
        if listed.returncode != 0 or top.returncode != 0 or top.stdout.strip():
            self.skipTest("not the top of a git checkout (a `git archive` copy, say)")
        found = [p for p in listed.stdout.decode("utf-8", "surrogateescape").split("\0") if p]
        self.assertGreater(len(found), 100)
        longest = max(found, key=len)
        self.assertLessEqual(len(longest), BUDGET, "%d characters: %s" % (len(longest), longest))


if __name__ == "__main__":
    unittest.main()
