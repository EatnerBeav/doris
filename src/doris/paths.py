"""Fixed paths. The code finds sandbox/, traces/ and tests/ from its own location,
never from the working folder, so `cd doris/src && python3 -m doris demo` works on every system.

Each mode's ledger and receipt folder are fixed here (ruling 8): there is no setting
that points one mode's state at another's.
"""

import os
import sys

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(SRC)
SANDBOX = os.path.join(REPO, "sandbox")       # the demo's inbox, calendar and outbox (judges drop .eml files in sandbox/inbox/)
TRACES = os.path.join(REPO, "traces")         # committed real runs
TESTS = os.path.join(REPO, "tests")
TRAP_PACK = os.path.join(REPO, "tests", "trap-pack", "pack")   # the organizers' traps/ folder, as shipped
STATE = os.path.join(REPO, "state")           # gitignored: ledgers, locks, run folders
# The env file `run --live` and `smoke` read unless --env names another: one default for both.
# It lives only on the server, never in the repository.
LIVE_ENV = "/root/doris-secrets/.env"

MODES = ("sandbox", "live", "throwaway")
SHOWN_FROM = "DORIS_SHOWN_FROM"    # set for her worker only: the folder the command was run in (shown)


def command(sub=""):
    """A command as the person can type it again: `py -m doris <sub>` on Windows (the launcher the README
    uses there; the interpreter's own name is python.exe whichever way it was started), elsewhere the
    name of the interpreter running now (python3, python3.9, python)."""
    exe = "py" if os.name == "nt" else (os.path.basename(sys.executable or "") or "python3")
    return ("%s -m doris %s" % (exe, sub)).rstrip()


def shown(path):
    """A path to print. Inside the repository, the path from the folder the command was run in (a judge
    runs from src/, so it pastes from there; her worker is told that folder in DORIS_SHOWN_FROM);
    anywhere else, or when no relative path exists (Windows, another drive), the whole path."""
    try:
        if os.path.relpath(path, REPO).startswith(".."):
            return path
        return os.path.relpath(path, os.environ.get(SHOWN_FROM) or os.getcwd())
    except (ValueError, OSError):
        return path


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
