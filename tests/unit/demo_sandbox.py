"""The demo sandbox as committed, without what a run or a judge adds to it.

`python -m doris demo` runs on the repository's own sandbox/ and leaves its output there for a judge
to open (.gitignore lists it): her sends in outbox/, her bookings as calendar/doris-*.ics, and the
event a staged fault puts on the calendar as calendar/staged-*.ics. A judge who follows
sandbox/README.md adds their own .eml to inbox/ and a line for it to inbox/facts.json. A test that
checks the committed fixtures, or runs the demo day on a copy of them, goes through here, so it sees
the same files after any number of demos, a Trap Pack run or a judge's own mail as in a fresh clone.

The committed set is what the fixtures generator writes (tests/tools/build_demo_fixtures.py, which
test_fixtures.py checks against the committed files byte for byte) and the hand-written README.md,
never what happens to be on disk.

Standard library only, and no import of doris: test_fixtures.py checks the fixtures without it.
"""

import fnmatch
import functools
import importlib.util
import json
import os
import shutil
import tempfile

TESTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SANDBOX = os.path.join(os.path.dirname(TESTS), "sandbox")      # the repository's own, as doris.paths has it
GENERATOR = os.path.join(TESTS, "tools", "build_demo_fixtures.py")
HAND_WRITTEN = ("README.md",)              # committed beside the generated fixtures, not generated
FACTS = "inbox/facts.json"

# What a run writes into calendar/ beside the owner's committed events: her bookings, staged events.
RUN_CALENDAR = ("doris-*.ics", "staged-*.ics")


@functools.lru_cache(maxsize=None)
def committed_files():
    """Every committed file of the sandbox, as "/"-separated paths under sandbox/."""
    spec = importlib.util.spec_from_file_location("build_demo_fixtures", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with tempfile.TemporaryDirectory() as tmp:
        written = module.build(tmp)
    return tuple(sorted(set(written) | set(HAND_WRITTEN)))


def committed_emails():
    """The demo's emails: file names in inbox/, in arrival order."""
    return sorted(rel.split("/", 1)[1] for rel in committed_files()
                  if rel.startswith("inbox/") and rel.endswith(".eml"))


def is_run_event(name):
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in RUN_CALENDAR)


def owner_ics_files(calendar):
    """The owner's committed events in a calendar/ folder: every .ics a run did not write."""
    return sorted(f for f in os.listdir(calendar) if f.endswith(".ics") and not is_run_event(f))


def copy_committed(dest, source=SANDBOX):
    """Copy the committed sandbox at source (the repository's own by default) to dest: the committed
    files and nothing else, so no booking, staged event or send of a run and no judge's own mail.
    inbox/facts.json keeps only the demo's emails' lines, laid out as the generator writes it.
    Reads source, never writes it. Returns dest."""
    emails = set(committed_emails())
    for rel in committed_files():
        src = os.path.join(source, *rel.split("/"))
        out = os.path.join(dest, *rel.split("/"))
        os.makedirs(os.path.dirname(out), exist_ok=True)
        if rel == FACTS:
            with open(src, "rb") as fh:
                facts = json.loads(fh.read().decode("utf-8"))
            kept = {name: value for name, value in facts.items() if name in emails}
            with open(out, "wb") as fh:
                fh.write((json.dumps(kept, indent=2, sort_keys=True) + "\n").encode("ascii"))
        else:
            shutil.copyfile(src, out)
    return dest
