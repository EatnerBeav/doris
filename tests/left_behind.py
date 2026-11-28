"""The suite's last checks: it left nothing behind.

tests/__init__.py's load_tests runs these after every other test (and takes the working tree's
picture before the first one). A test may start threads and processes and write files, but only
for itself: by the time the suite ends no thread or process a test started is still running,
nothing landed in the suite's throwaway default state or traces folder, and the working tree is as the suite
found it: no HALT, no STOP and nothing new under state/ (a HALT left there stops the next `demo`),
and no file anywhere else added, changed or removed.

Not named test_*.py, so discovery never loads it on its own: it only runs last, from load_tests.
"""

import os
import threading
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP = frozenset({"__pycache__", ".git"})
GRACE_S = 5.0                  # a thread or process still finishing gets this long, all told, before it counts as left
MARK = "DORIS_TEST_SUITE"      # in the environment of every process a test starts (set by tests/__init__.py)


def tree(root):
    """Every folder (a trailing separator, None) and file ((size, mtime_ns)) under root, by path
    from root. __pycache__ and .git are not the suite's business."""
    out = {}
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP)
        rel = os.path.relpath(folder, root)
        if rel != ".":
            out[rel + os.sep] = None
        for name in files:
            path = os.path.join(folder, name)
            try:
                st = os.lstat(path)
            except OSError:
                continue
            out[os.path.normpath(os.path.join(rel, name))] = (st.st_size, st.st_mtime_ns)
    return out


def changes(before, after):
    """(added, changed, removed) paths, each sorted."""
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(p for p in set(before) & set(after) if before[p] != after[p])
    return added, changed, removed


def marked(value, proc="/proc"):
    """{pid: command line} of every other process whose environment carries MARK=value: what the
    suite started, its children's children included, whoever reaped them since. Linux (/proc) only.
    A process that has exited shows no environment, so only the running ones are found."""
    want = ("%s=%s" % (MARK, value)).encode("utf-8")
    out = {}
    for name in os.listdir(proc):
        if not name.isdigit() or int(name) == os.getpid():
            continue
        try:
            with open(os.path.join(proc, name, "environ"), "rb") as fh:
                if want not in fh.read().split(b"\0"):
                    continue
            with open(os.path.join(proc, name, "cmdline"), "rb") as fh:
                out[int(name)] = fh.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        except OSError:
            continue                # gone meanwhile, or not ours to read
    return out


class NothingLeftBehind(unittest.TestCase):
    at_start = None          # the working tree before the first test (set by load_tests)
    default_state = None     # the suite's throwaway default state folder (tests/__init__.py)
    default_traces = None    # and its throwaway default traces folder
    mark = None              # MARK's value in this run (tests/__init__.py)

    def test_1_no_thread_a_test_started_is_still_running(self):
        # First: a thread still running could write after the checks below (a lifeline thread
        # that outlived its test wrote the repo's state/sandbox/HALT that way).
        main = threading.main_thread()
        deadline = time.monotonic() + GRACE_S
        for thread in threading.enumerate():
            if thread is not main:
                thread.join(max(0.0, deadline - time.monotonic()))
        left = sorted(t.name for t in threading.enumerate() if t is not main and t.is_alive())
        self.assertEqual(left, [], "threads a test started are still running")

    def test_2_no_process_a_test_started_is_still_running(self):
        # A worker whose supervisor a test killed could likewise write after the checks below.
        if self.mark is None or not os.path.isdir("/proc"):
            self.skipTest("no /proc here to find the processes the suite started")
        deadline = time.monotonic() + GRACE_S
        left = marked(self.mark)
        while left and time.monotonic() < deadline:
            time.sleep(0.05)
            left = marked(self.mark)
        self.assertEqual(left, {}, "processes a test started are still running")

    def test_3_nothing_landed_in_the_default_state_folder(self):
        if self.default_state is None:
            self.skipTest("the suite ran without its own default state folder")
        self.assertEqual(sorted(tree(self.default_state)), [],
                         "a test wrote in the default state folder instead of a folder of its own")

    def test_3b_nothing_landed_in_the_default_traces_folder(self):
        if self.default_traces is None:
            self.skipTest("the suite ran without its own default traces folder")
        self.assertEqual(sorted(tree(self.default_traces)), [],
                         "a test wrote a trace in the default traces folder instead of a folder of its own")

    def test_4_the_working_tree_is_as_the_suite_found_it(self):
        if self.at_start is None:
            self.skipTest("no picture of the working tree was taken before the first test")
        added, changed, removed = changes(self.at_start, tree(REPO))
        self.assertEqual({"added": added, "changed": changed, "removed": removed},
                         {"added": [], "changed": [], "removed": []},
                         "the suite left something in the working tree (state/ included: HALT, STOP, a ledger)")
