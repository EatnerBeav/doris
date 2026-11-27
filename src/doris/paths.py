"""Fixed paths. The code finds sandbox/, traces/ and tests/ from its own location,
never from the working folder, so `cd doris/src && python3 -m doris demo` works on every system.

Each mode's ledger and receipt folder are fixed here (ruling 8): there is no setting
that points one mode's state at another's.
"""

import os

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(SRC)
SANDBOX = os.path.join(REPO, "sandbox")       # the demo's inbox, calendar and outbox (judges drop .eml files in sandbox/inbox/)
TRACES = os.path.join(REPO, "traces")         # committed real runs
TESTS = os.path.join(REPO, "tests")
TRAP_PACK = os.path.join(REPO, "tests", "trap-pack", "pack")   # the organizers' traps/ folder, as shipped
STATE = os.path.join(REPO, "state")           # gitignored: ledgers, locks, run folders

MODES = ("sandbox", "live", "throwaway")


def state_dir(mode):
    """The fixed state folder for a mode. Throwaway runs get a fresh sub-folder per run (see throwaway_dir)."""
    if mode not in MODES:
        raise ValueError("unknown mode %r" % (mode,))
    return os.path.join(STATE, mode)


def ledger_path(mode):
    if mode == "throwaway":
        raise ValueError("throwaway ledgers live in their own run folder")
    return os.path.join(state_dir(mode), "ledger.sqlite")


def runs_dir(mode):
    return os.path.join(state_dir(mode), "runs")
