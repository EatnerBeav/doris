"""The demo sandbox as committed, without what a run or a judge adds to it.

`python -m doris demo` runs on the repository's own sandbox/ and leaves its output there for a judge
to open (.gitignore lists it): her sends in outbox/, her bookings as calendar/doris-*.ics, and the
event a staged fault puts on the calendar as calendar/staged-*.ics. A judge who follows
sandbox/README.md adds their own .eml to inbox/ and a line for it to inbox/facts.json. A test that
checks the committed fixtures, or runs the demo day on a copy of them, goes through here, so it sees
the same files after any number of demos, a Trap Pack run or a judge's own mail as in a fresh clone.

The committed set is what the fixtures generator writes (tests/tools/build_demo_fixtures.py, which
test_fixtures.py checks against the committed files byte for byte) and the hand-written README.md,
never what happens to be on disk. It is also exactly what git tracks under sandbox/: run as
`python -m tests.unit.demo_sandbox` from the repository root, this module checks that (CI does).

Standard library only, and no import of doris: test_fixtures.py checks the fixtures without it.
"""

import fnmatch
import functools
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile

TESTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(TESTS)
SANDBOX = os.path.join(REPO, "sandbox")      # the repository's own, as doris.paths has it
GENERATOR = os.path.join(TESTS, "tools", "build_demo_fixtures.py")
HAND_WRITTEN = ("README.md",)              # committed beside the generated fixtures, not generated
FACTS = "inbox/facts.json"

# What a run writes into calendar/ beside the owner's committed events: her bookings, staged events.
RUN_CALENDAR = ("doris-*.ics", "staged-*.ics")


@functools.lru_cache(maxsize=None)
def _built():
    """The generator's build: {"/"-separated path under sandbox/: bytes}. Read it, never change it."""
    spec = importlib.util.spec_from_file_location("build_demo_fixtures", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    built = {}
    with tempfile.TemporaryDirectory() as tmp:
        for rel in module.build(tmp):
            with open(os.path.join(tmp, *rel.split("/")), "rb") as fh:
                built[rel] = fh.read()
    return built


def committed_files():
    """Every committed file of the sandbox, as "/"-separated paths under sandbox/."""
    return tuple(sorted(set(_built()) | set(HAND_WRITTEN)))


def _files_named(folder, extension):
    """The files in folder whose name ends in extension in any case, sorted: the sandbox adapter's
    rule for its inbox (.eml) and calendar (.ics) (doris/adapters/sandbox.py, SandboxMail._inbox and
    SandboxCalendar._names; test_demo_sandbox checks the two agree)."""
    return sorted(n for n in os.listdir(folder)
                  if n.lower().endswith(extension) and os.path.isfile(os.path.join(folder, n)))


def mail_names(inbox):
    """The mail in an inbox/ folder, in the order Doris reads it: a judge's 14-Robin.EML is mail as
    much as 14-robin.eml."""
    return _files_named(inbox, ".eml")


def is_run_event(name):
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in RUN_CALENDAR)


def owner_ics_files(calendar):
    """The owner's committed events in a calendar/ folder: every .ics a run did not write."""
    return [f for f in _files_named(calendar, ".ics") if not is_run_event(f)]


def copy_committed(dest, source=SANDBOX):
    """Copy the committed sandbox at source (the repository's own by default) to dest: the committed
    files and nothing else, so no booking, staged event or send of a run and no judge's own mail.
    inbox/facts.json is the generator's own build, never source's: a judge's line for their mail, or
    a facts.json an editor saved with a byte order mark or a trailing comma, stays out of every copy
    (test_fixtures.py checks the committed file itself). Reads source, never writes it. Returns dest."""
    for rel in committed_files():
        out = os.path.join(dest, *rel.split("/"))
        os.makedirs(os.path.dirname(out), exist_ok=True)
        if rel == FACTS:
            with open(out, "wb") as fh:
                fh.write(_built()[FACTS])
        else:
            shutil.copyfile(os.path.join(source, *rel.split("/")), out)
    return dest


# The variables that point git at a repository other than the one named (`git rev-parse
# --local-env-vars`). A pre-commit hook or a linked worktree sets some of them for the repository
# being committed, so every git call here runs without them and answers only for the folder it names.
GIT_LOCAL_ENV = ("GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
                 "GIT_OBJECT_DIRECTORY", "GIT_DIR", "GIT_WORK_TREE", "GIT_IMPLICIT_WORK_TREE", "GIT_GRAFT_FILE",
                 "GIT_INDEX_FILE", "GIT_NO_REPLACE_OBJECTS", "GIT_REPLACE_REF_BASE", "GIT_PREFIX",
                 "GIT_SHALLOW_FILE", "GIT_COMMON_DIR")


def git_env():
    """os.environ without GIT_LOCAL_ENV, for running git on a folder named with -C."""
    return {k: v for k, v in os.environ.items() if k not in GIT_LOCAL_ENV}


def git_sandbox_files(repo=REPO):
    """What git tracks under repo's sandbox/ (`git ls-files sandbox`), as sorted "/"-separated paths
    under sandbox/. None when git cannot say for repo: git is not installed, or repo is not the top
    of a git checkout (a `git archive` copy, even one unpacked inside some other checkout)."""
    def git(*args):
        return subprocess.run(["git", "-C", repo] + list(args), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=60, env=git_env())
    try:
        prefix = git("rev-parse", "--show-prefix")       # "" at the top of a checkout
        if prefix.returncode != 0 or prefix.stdout.strip():
            return None
        listed = git("ls-files", "-z", "--", "sandbox")
    except OSError:
        return None
    if listed.returncode != 0:
        return None
    paths = listed.stdout.decode("utf-8", "surrogateescape").split("\0")
    return tuple(sorted(p[len("sandbox/"):] for p in paths if p))


def sandbox_drift(tracked):
    """How the files git tracks under sandbox/ differ from committed_files(), in words; "" if not."""
    committed = set(committed_files())
    problems = []
    extra = sorted(set(tracked) - committed)
    missing = sorted(committed - set(tracked))
    if extra:
        problems.append("committed under sandbox/ but not in demo_sandbox.committed_files(), so no test "
                        "reads it: " + ", ".join(extra))
    if missing:
        problems.append("in demo_sandbox.committed_files() but not committed: " + ", ".join(missing))
    return "\n".join(problems)


def main():
    tracked = git_sandbox_files()
    if tracked is None:
        print("sandbox check: git cannot list sandbox/ in %s (no git, or not the top of a git checkout)"
              % REPO, file=sys.stderr)
        return 2
    problem = sandbox_drift(tracked)
    if problem:
        print("sandbox check: " + problem.replace("\n", "\nsandbox check: "), file=sys.stderr)
        print("Remove the file from git, or have tests/tools/build_demo_fixtures.py write it (or list it in "
              "HAND_WRITTEN in tests/unit/demo_sandbox.py) and test it.", file=sys.stderr)
        return 1
    print("sandbox check: git tracks the %d files of the committed sandbox under sandbox/, and no others"
          % len(tracked))
    return 0


if __name__ == "__main__":
    sys.exit(main())
