"""The demo sandbox as committed, without what a run leaves in it.

`python -m doris demo` runs on the repository's own sandbox/ and leaves its output there for a judge
to open (.gitignore lists it): her sends in outbox/, her bookings as calendar/doris-*.ics, and the
event a staged fault puts on the calendar as calendar/staged-*.ics. A test that checks the committed
fixtures, or runs the demo day on a copy of them, goes through here, so it sees the same files after
any number of demos (and a Trap Pack run) as in a fresh clone.

Standard library only, and no import of doris: test_fixtures.py checks the fixtures without it.
"""

import fnmatch
import os
import shutil

# What a run writes into calendar/ beside the owner's committed events: her bookings, staged events.
RUN_CALENDAR = ("doris-*.ics", "staged-*.ics")
# outbox/ is committed empty but for this file; everything else in it is a run's.
OUTBOX_KEEP = ".gitkeep"


def is_run_event(name):
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in RUN_CALENDAR)


def owner_ics_files(calendar):
    """The owner's committed events in a calendar/ folder: every .ics a run did not write."""
    return sorted(f for f in os.listdir(calendar) if f.endswith(".ics") and not is_run_event(f))


def copy_committed(source, dest):
    """Copy the sandbox at source to dest as committed: no bookings or staged events in calendar/,
    nothing in outbox/ but .gitkeep. Returns dest."""
    def run_output(folder, names):
        rel = os.path.relpath(folder, source)
        if rel == "calendar":
            return {n for n in names if is_run_event(n)}
        if rel == "outbox":
            return {n for n in names if n != OUTBOX_KEEP}
        return set()
    shutil.copytree(source, dest, ignore=run_output)
    return dest
