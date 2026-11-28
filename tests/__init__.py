"""The test suite. From the repository root:

    PYTHONPATH=src python3 -m unittest discover -s tests -t .

Importing this package, before any test module, points doris.paths.STATE and doris.paths.TRACES at
throwaway folders of the suite's own (removed at exit). Tests patch them again to folders of their
own; whatever still reaches a default (a test that forgot to, a thread that outlived its test's
patch) lands there, never in the repository's state/, where a HALT would stop the next `demo`, or
its traces/, which holds committed runs. It also marks the environment, so every process a test
starts, and every process those start, can be found again.

Discovery hands this package's load_tests the whole suite. It takes a picture of the working
tree before the first test and puts the checks of tests/left_behind.py after the last: no thread
or process a test started is still running, nothing landed in the throwaway state or traces folder,
and the working tree is as the suite found it (no HALT, no STOP, nothing new under state/).
"""

import atexit
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from doris import paths                 # noqa: E402  (src is on the path only now)
from tests import left_behind           # noqa: E402  (a module: discovery collects no tests from it here)

DEFAULT_STATE = tempfile.mkdtemp(prefix="doris-tests-state-")
paths.STATE = DEFAULT_STATE
atexit.register(shutil.rmtree, DEFAULT_STATE, True)
DEFAULT_TRACES = tempfile.mkdtemp(prefix="doris-tests-traces-")
paths.TRACES = DEFAULT_TRACES
atexit.register(shutil.rmtree, DEFAULT_TRACES, True)
os.environ[left_behind.MARK] = os.urandom(8).hex()


def load_tests(loader, standard_tests, pattern):
    checks = left_behind.NothingLeftBehind
    checks.at_start = left_behind.tree(left_behind.REPO)
    checks.default_state, checks.default_traces = DEFAULT_STATE, DEFAULT_TRACES
    checks.mark = os.environ[left_behind.MARK]
    standard_tests.addTests(loader.discover(start_dir=HERE, pattern=pattern))
    standard_tests.addTests(loader.loadTestsFromTestCase(checks))      # last, after every other test
    return standard_tests
