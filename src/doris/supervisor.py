"""The kill switch: one runner per ledger, halt and resume, and the supervisor that kills
the worker mid-call (locked call 7, ruling 8, brief add A3).

How a stop works:
- A halt is a file on disk, read now, every time: `state/<mode>/HALT` (ours, written atomically
  by `request_stop`: the `stop` command, the watch page's Stop button, or the worker itself at
  the budget cap) or a STOP file (`state/<mode>/STOP` or `<repo>/STOP`, the user's, only read).
  Nothing in mail halts her.
- `run_supervised` runs the worker as its one child process, polls for a halt about every
  0.1 s and kills the child the moment one appears (Popen.kill: SIGKILL on POSIX,
  TerminateProcess on Windows). Only after the child is dead does it write the `halted`
  receipt line, chained from the child's last whole line: the cause, the call in flight and
  every queued item it dropped.
- SIGTERM or SIGHUP on the supervisor (a `kill`, a service manager stopping her, a closed terminal
  or ssh session; on Windows, Ctrl+Break) is a stop, as Ctrl+C is: it kills the worker, writes HALT
  (cause `signal`) and the halted line, which names the signal. A signal ignored when she starts
  (nohup) stays ignored.
- The worker's lifeline: its stdin is a pipe whose other end only the supervisor holds, and never
  writes to. However the supervisor ends (SIGKILL, a crash), the OS closes that end and the worker
  reads EOF: nothing can kill it mid-call any more and nobody will write its halted line. So it
  writes HALT (cause `supervisor_gone`), stops at its next halt check (G1 reads it at every effect),
  writes its own halted line and summary, and exits.
- A halt survives a restart: `run_supervised` refuses to start while one is in force, and only
  `resume` (the CLI) clears HALT. A halt from a STOP file is written to HALT too, so it stays
  in force after the file is gone. A STOP file is never deleted by code; the user deletes it.

One Doris per ledger (ruling 8): the worker takes `state/<mode>/ledger.lock` with acquire_lock()
when it starts and holds it for its life (G2 asks the worker). run_supervised only checks that
the lock is free before it starts the worker, and takes it again once the worker is dead, while
it writes the halted line. The OS frees the lock when a process dies, so a crash leaves no stale
lock. Worker exit codes: 3 = another Doris holds the lock (it printed LOCK_REFUSAL; no halted
line), 4 = it saw a halt itself (G1, or HALT written at the budget cap; the parent writes the
halted line, or, its supervisor gone, the worker wrote it). run_supervised returns 3 and 4 for the
same outcomes (see the EXIT_ codes).

Standard library only; no fork, no setsid; the one signal sent is the kill. Linux, macOS and
Windows: there SIGTERM cannot be caught and there is no SIGHUP, so those two are POSIX only; the
lifeline works on all three.
"""

import datetime as _dt
import hashlib
import json
import os
import re
import signal as _signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional

from . import paths
from .why import HALT_CAUSES

HALT_NAME = "HALT"
STOP_NAME = "STOP"
LOCK_NAME = "ledger.lock"

LOCK_REFUSAL = "another Doris is already running on this ledger"


class HaltFileError(RuntimeError):
    """A halt file exists but cannot be read. Callers treat it as a halt in force (fail closed)."""


# ------------------------------------------------------------------------------------------
# Where the files live
# ------------------------------------------------------------------------------------------
# Every function here takes an optional state_dir: a throwaway run (traps, redteam, tests) keeps
# its HALT, STOP and ledger.lock in its own folder. The default is the mode's fixed folder.
def _folder(mode, state_dir=None):
    return state_dir if state_dir else paths.state_dir(mode)


def halt_file(mode, state_dir=None):
    return os.path.join(_folder(mode, state_dir), HALT_NAME)


def stop_files(mode, state_dir=None):
    """Every place a STOP file halts this run, in the order they are checked. The repo-root STOP
    halts every mode and every throwaway run."""
    return [os.path.join(_folder(mode, state_dir), STOP_NAME), os.path.join(paths.REPO, STOP_NAME)]


def lock_file(mode, state_dir=None):
    return os.path.join(_folder(mode, state_dir), LOCK_NAME)


# ------------------------------------------------------------------------------------------
# Reading a halt (G1 reads this too)
# ------------------------------------------------------------------------------------------
def _exists(path):
    """True when anything is at path (a dangling link included). Raises if it can't tell."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise HaltFileError("cannot check %s: %s" % (path, exc.__class__.__name__))
    return True


def _present_or_unknown(path):
    """For resume: a STOP path that cannot be checked counts as there (fail closed)."""
    try:
        return _exists(path)
    except HaltFileError:
        return True


def read_halt(mode, state_dir=None):
    """The HALT file's record ({"cause", "requested_utc"}), or None when there is no HALT.
    Raises HaltFileError when a HALT is there but cannot be read or is not one of ours."""
    path = halt_file(mode, state_dir)
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise HaltFileError("cannot read %s: %s" % (path, exc.__class__.__name__))
    try:
        record = json.loads(data.decode("utf-8"))
    except ValueError:
        raise HaltFileError("%s is not a halt record" % (path,))
    if not isinstance(record, dict) or record.get("cause") not in HALT_CAUSES:
        raise HaltFileError("%s names no listed halt cause" % (path,))
    return record


def halt_cause(mode, state_dir=None) -> Optional[str]:
    """The halt in force for this mode, read from disk now: HALT's cause, else "stop_file"
    when a STOP file is there, else None. Raises HaltFileError on a halt file it cannot read."""
    record = read_halt(mode, state_dir)
    if record is not None:
        return record["cause"]
    for path in stop_files(mode, state_dir):
        if _exists(path):
            return "stop_file"
    return None


# ------------------------------------------------------------------------------------------
# Asking for a stop, and resuming
# ------------------------------------------------------------------------------------------
def _utc_ms(epoch):
    stamp = _dt.datetime.fromtimestamp(epoch, _dt.timezone.utc)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (stamp.microsecond // 1000)


def request_stop(mode, cause="cli", state_dir=None):
    """Write HALT atomically (temp file, fsync, os.replace). The first cause stays: a second
    stop while one is in force changes nothing. Returns the cause in force."""
    if cause not in HALT_CAUSES:
        raise ValueError("unlisted halt cause %r" % (cause,))
    try:
        existing = read_halt(mode, state_dir)
    except HaltFileError:
        existing = None                     # unreadable: replace it with a readable one
    if existing is not None:
        return existing["cause"]
    folder = _folder(mode, state_dir)
    os.makedirs(folder, exist_ok=True)
    body = json.dumps({"cause": cause, "requested_utc": _utc_ms(time.time())}, sort_keys=True)
    fd, tmp = tempfile.mkstemp(prefix=HALT_NAME + ".", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(body.encode("ascii") + b"\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, halt_file(mode, state_dir))
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return cause


@dataclass
class ResumeResult:
    ok: bool                          # True when nothing is left in force and a run may start
    cleared: Optional[str]            # the cause cleared ("unreadable" for a broken HALT), or None
    stop_files: List[str] = field(default_factory=list)   # STOP files still there (never deleted)
    message: str = ""                 # one plain line for the CLI


def resume(mode, state_dir=None) -> ResumeResult:
    """CLI only. Clears HALT; never deletes a STOP file (it tells the user to). Refuses while a
    Doris holds this mode's ledger lock, so a stop on its way cannot be cancelled by a resume.
    A resume that fails changes nothing: while a STOP file is there (or can't be checked) HALT
    stays as it is, so deleting the STOP file alone never lets her run; a resume after it does.
    The next run reconciles before it reads new mail (ruling 8)."""
    held = acquire_lock(lock_file(mode, state_dir))
    if held is None:
        return ResumeResult(False, None, [], "Not resumed: a Doris is still running on this ledger; "
                                              "resume once it has stopped.")
    try:
        stops = [p for p in stop_files(mode, state_dir) if _present_or_unknown(p)]
        try:
            record = read_halt(mode, state_dir)
            cause = record["cause"] if record else None
        except HaltFileError:
            cause = "unreadable"
        cleared = None
        if cause is not None and not stops:
            os.remove(halt_file(mode, state_dir))
            cleared = cause
    finally:
        held.release()
    if stops:
        if cause is None:
            return ResumeResult(False, None, stops, "Not resumed: a STOP file is in force: %s. Delete it yourself "
                                                    "to let her run." % (", ".join(stops),))
        return ResumeResult(False, None, stops, "Not resumed: a STOP file is still in force: %s, so the halt (%s) "
                                                "stays. Delete the file yourself, then run `%s` again."
                            % (", ".join(stops), CAUSE_WORDS.get(cause, cause), _resume_command()))
    if cleared is None:
        return ResumeResult(True, None, [], "Nothing to resume: no halt is in force.")
    return ResumeResult(True, cleared, [], "Resumed: the halt (%s) is cleared. The next run reconciles "
                                           "first." % (CAUSE_WORDS.get(cleared, cleared),))


CAUSE_WORDS = {
    "cli": "stop on the command line",
    "stop_file": "a STOP file",
    "watch_button": "Stop pressed on the watch page",
    "budget_cap": "model spend reached the cap of the run",
    "signal": "a stop signal reached the supervisor",
    "supervisor_gone": "her supervisor is gone",
    "unreadable": "a halt file that could not be read",
}


# ------------------------------------------------------------------------------------------
# One Doris per ledger
# ------------------------------------------------------------------------------------------
class LockHandle:
    """An OS lock on the ledger's lock file. Keep a reference for as long as the lock must be
    held; the OS releases it when the process dies, so a crash never leaves a stale lock."""

    def __init__(self, path, fd):
        self.path = path
        self.fd = fd

    def fileno(self):
        return self.fd

    @property
    def held(self):
        """True until release(): what the worker's G2 asks ("does this process hold it")."""
        return self.fd is not None

    def release(self):
        if self.fd is None:
            return
        fd, self.fd = self.fd, None
        if os.name == "nt":
            import msvcrt
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        os.close(fd)                         # POSIX: closing the descriptor is the release


def acquire_lock(path) -> Optional[LockHandle]:
    """Take the ledger lock without waiting: a LockHandle, or None when another holder has it.
    fcntl.flock on POSIX, msvcrt.locking on Windows. Raises OSError when locking itself fails."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if _held_elsewhere(exc):
            return None
        raise
    return LockHandle(path, fd)


def _held_elsewhere(exc):
    import errno
    return isinstance(exc, BlockingIOError) or exc.errno in (errno.EACCES, errno.EAGAIN, getattr(errno, "EDEADLOCK", -1))


# ------------------------------------------------------------------------------------------
# What the kill cancelled
# ------------------------------------------------------------------------------------------
# Calls the server may already have accepted when the kill landed. They cannot be pulled back,
# so they are marked in the ledger and reconciled on resume (ruling 8, A3). Value: how to mark.
UNCONFIRMED_CALLS = {
    "send": ("set_send_state", "sending"),                    # cut off mid-send: never sent again; on resume
                                                              # looked up in Sent twice by Message-ID
    "calendar_create": ("set_booking_state", "unconfirmed"),  # looked up by booking ID
    "calendar_delete": ("set_booking_state", "unconfirmed"),
}


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def find_in_flight(lines) -> Optional[dict]:
    """The last `called` line that no `result` names by call_seq: {seq, call, target,
    unconfirmed} (plus message_id for a send that carries one), or None."""
    answered = set()
    for item in lines:
        if item.get("kind") == "result":
            answered.add(_int((item.get("payload") or {}).get("call_seq")))
    for item in reversed(lines):
        if item.get("kind") == "called" and _int(item.get("seq")) not in answered:
            payload = item.get("payload") or {}
            out = {"seq": item.get("seq"), "call": payload.get("call"), "target": payload.get("target"),
                   "unconfirmed": payload.get("call") in UNCONFIRMED_CALLS}
            if payload.get("call") == "send" and payload.get("message_id"):
                out["message_id"] = payload["message_id"]
            return out
    return None


def _row(row):
    """A ledger row as a dict, whatever shape the ledger returns (dict, sqlite3.Row, dataclass)."""
    if isinstance(row, dict):
        return row
    if hasattr(row, "keys"):
        return {k: row[k] for k in row.keys()}
    if hasattr(row, "__dataclass_fields__"):
        import dataclasses
        return dataclasses.asdict(row)
    return dict(getattr(row, "__dict__", {}))


def _detail(row):
    detail = row.get("detail", row.get("detail_json"))
    if isinstance(detail, (bytes, str)):
        try:
            detail = json.loads(detail)
        except ValueError:
            detail = None
    return detail if isinstance(detail, dict) else {}


# A Message-ID or thread key can hold the meeting link (Message-ID: <meet.example/room@x>), and the
# supervisor never loads the env, so it cannot find the link as the worker's receipts do
# (templates.redact_values). Every form of the link templates.has_link finds holds "/" or ":" (a live
# link is https: its path, or with no path its scheme's "://"), or "%" or "&" (an escaped form), so
# a key made only of the plain characters below is shown as written; any other key, non-ASCII
# included, is shown by its hash (review of 7e1a9fa, item 2).
_PLAIN_KEY = re.compile(r"[A-Za-z0-9.@_+=!#$'*?^`{|}~<>\[\]-]+")
_RAW_KEY = re.compile(r"sha256:[0-9a-f]{64}")        # mail.message_key of a mail with no Message-ID


def shown_key(key):
    """A ledger key as the supervisor's halted line shows it: as written when it cannot spell the
    meeting link, else "key-sha256:" and ledger.key_sha256 of it (so it can be matched in the ledger)."""
    if not isinstance(key, str) or _PLAIN_KEY.fullmatch(key) or _RAW_KEY.fullmatch(key):
        return key
    from .ledger import key_sha256
    try:
        digest = key_sha256(key)
    except (ValueError, UnicodeError):          # an empty key: never a reason to lose the list
        digest = hashlib.sha256(key.encode("utf-8", "surrogatepass")).hexdigest()
    return "key-sha256:" + digest


def _dropped(ledger, cause, run_id):
    """Every queued action (dropped now, in the ledger) and every fetched message not yet
    processed, each with its kind, thread Message-ID, message and template or booking ID; the
    keys as shown_key shows them."""
    out, listed = [], set()
    for raw in ledger.drop_queued(cause) or []:
        row = _row(raw)
        detail = _detail(row)
        message = row.get("message_key")
        listed.add(message)
        out.append({
            "kind": row.get("kind"),
            "thread": shown_key(row.get("thread_key") or detail.get("thread_key")
                                or ledger.thread_of_message(message)),
            "message": shown_key(message),
            "template": row.get("template") or detail.get("template"),
            "booking_id": row.get("booking_id") or detail.get("booking_id"),
        })
    for raw in ledger.unprocessed_messages(run_id) or []:
        row = _row(raw)
        key = row.get("key", row.get("message_key"))
        if key in listed:
            continue
        listed.add(key)
        out.append({"kind": "message", "thread": shown_key(row.get("thread_key") or ledger.thread_of_message(key)),
                    "message": shown_key(key), "template": None, "booking_id": None})
    return out


def _torn_files(receipts_path):
    folder, base = os.path.split(os.path.abspath(receipts_path))
    try:
        names = os.listdir(folder)
    except OSError:
        return set()
    return {n for n in names if n.startswith(base + ".torn-")}


def record_halt(mode, run_name, cause, receipts_path, ledger_path, *, receipts_module=None,
                open_ledger=None, run_id=None, stop_to_dead_ms=None, requested_utc=None,
                worker_exit=None, halt_file_unreadable=False, signal=None) -> dict:
    """Run only after the worker is dead. Marks the call in flight unconfirmed in the ledger,
    drops the queue, and writes the `halted` line chained from the worker's last whole line
    (the receipt writer moves a torn tail aside first). A stop signal is named in it ("SIGTERM").
    Returns the line written."""
    if receipts_module is None:
        from . import receipts as receipts_module
    if open_ledger is None:
        from .ledger import Ledger as open_ledger
    from .why import why as make_why

    run_id = run_id or run_name
    failure = None
    try:                                            # whole lines only: a torn tail is left out
        lines = receipts_module.read_lines(receipts_path) if os.path.exists(receipts_path) else []
    except Exception as exc:                        # the queue is still dropped; raised below
        lines, failure = [], exc
    in_flight = find_in_flight(lines)

    if not os.path.exists(ledger_path):
        dropped, ledger_state = [], "absent"        # nothing was ever queued; never create one here
    else:
        dropped, ledger_state = None, "unreadable"  # a failed read is never an empty queue
        ledger = None
        try:
            ledger = open_ledger(ledger_path)
            mark = UNCONFIRMED_CALLS.get((in_flight or {}).get("call"))
            if mark:
                key = in_flight.get("message_id") if in_flight["call"] == "send" else in_flight.get("target")
                if key:
                    getattr(ledger, mark[0])(key, mark[1])
            dropped = _dropped(ledger, cause, run_id)
            ledger_state = "ok"
        except Exception:
            pass
        finally:
            if ledger is not None:
                try:
                    ledger.close()
                except Exception:
                    pass

    # The ledger is settled first, so a receipt file that cannot take the line (a broken chain)
    # still leaves the queue dropped and the call in flight marked. Then the line, or the error.
    if failure is not None:
        raise failure
    torn_before = _torn_files(receipts_path)
    writer = receipts_module.ReceiptWriter(receipts_path, run_id, mode)   # moves a torn tail aside
    torn = sorted(_torn_files(receipts_path) - torn_before)

    payload = {
        "cause": cause,
        "in_flight": in_flight,
        "dropped": dropped,
        "ledger": ledger_state,
        "torn_tail": torn[-1] if torn else None,
        "stop_to_dead_ms": stop_to_dead_ms,
        "requested_utc": requested_utc,
        "worker_exit": worker_exit,
    }
    if halt_file_unreadable:
        payload["halt_file"] = "unreadable"
    if signal:
        payload["signal"] = signal
    return writer.write("halted", payload, subject="", why=make_why("halt." + cause))


# ------------------------------------------------------------------------------------------
# The supervisor
# ------------------------------------------------------------------------------------------
POLL_S = 0.1                 # how often the supervisor looks for a halt

# The worker's own exit codes (Wolf, 11:5x PT): 3 = it could not take the ledger lock (another
# Doris has it; it printed LOCK_REFUSAL), 4 = it saw a halt itself (G1, or HALT written at the
# budget cap). run_supervised returns the same numbers for the same outcomes.
WORKER_EXIT_LOCKED = 3
WORKER_EXIT_HALTED = 4

EXIT_LOCKED = 3              # not started: another Doris holds this ledger (no halted line)
EXIT_HALTED = 4              # the run was halted; the halted line is written
EXIT_REFUSED_HALT = 5        # not started: a halt is in force
EXIT_HALT_UNRECORDED = 6     # halted, but the halted line could not be written (said loudly)

RELOCK_S = 5.0               # how long the parent waits for the OS to free a dead worker's lock


@dataclass
class Stopped:
    cause: Optional[str]      # None: the worker ended on its own with no halt in force
    seen_wall: float          # time.time() when the halt was seen (or the worker ended)
    dead_wall: float          # time.time() when the worker was dead and reaped
    unreadable: bool = False  # the halt seen was a halt file that could not be read
    signal: Optional[str] = None   # cause "signal": which one ("SIGTERM")


# Stops that come as a signal to the supervisor, caught while the worker runs: SIGTERM (`kill`, a
# service manager), SIGHUP (a closed terminal or ssh session; POSIX only) and SIGBREAK (Ctrl+Break;
# Windows only). Ctrl+C is Python's own KeyboardInterrupt, caught in watch(). SIGKILL cannot be
# caught: the worker's lifeline covers that.
STOP_SIGNALS = ("SIGTERM", "SIGHUP", "SIGBREAK")


def _signal_name(number):
    try:
        return _signal.Signals(number).name
    except ValueError:
        return "signal %d" % (number,)


class StopSignals:
    """While the worker runs, the STOP_SIGNALS that this platform has are stops. The handler only
    notes the first one; watch() acts on it at its next poll (within POLL_S), so no signal lands in
    the middle of a write. Handlers go back as they were on the way out. A signal ignored when she
    starts (nohup) stays ignored, and off the main thread (where Python allows no handler) nothing
    is set."""

    def __init__(self):
        self.seen = None
        self._before = []

    def note(self, number, frame=None):
        if self.seen is None:
            self.seen = _signal_name(number)

    def __enter__(self):
        if threading.current_thread() is not threading.main_thread():
            return self
        for name in STOP_SIGNALS:
            number = getattr(_signal, name, None)
            if number is None:
                continue                                # not on this platform
            try:
                before = _signal.getsignal(number)
                if before == _signal.SIG_IGN:
                    continue                            # nohup: whoever started her asked for that
                _signal.signal(number, self.note)
            except (OSError, RuntimeError, ValueError):
                continue
            self._before.append((number, before))
        return self

    def __exit__(self, *exc):
        while self._before:
            number, before = self._before.pop()
            try:
                _signal.signal(number, _signal.SIG_DFL if before is None else before)
            except (OSError, RuntimeError, ValueError):
                pass
        return False


def _poll_halt(mode, state_dir=None):
    try:
        return halt_cause(mode, state_dir), False
    except HaltFileError:
        return "stop_file", True    # fail closed: a halt file is there, so she stops


def _kill(child):
    """Kill the worker if it lives (SIGKILL / TerminateProcess) and reap it."""
    while True:
        if child.poll() is None:
            try:
                child.kill()
            except OSError:
                pass                # it died between the poll and the kill
        try:
            child.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue


def watch(child, mode, poll_s=POLL_S, state_dir=None, signals=None) -> Stopped:
    """Poll for a halt about every poll_s seconds while the worker runs. On a halt, kill the
    worker at once and reap it. Ctrl+C here is a stop on the command line, and a stop signal that
    `signals` (StopSignals) caught is a stop too (cause "signal"): each is written to HALT after the
    kill, so it survives a restart like any other stop. A halt already on disk keeps its cause."""
    def heard():
        return signals.seen if signals is not None else None

    try:
        while True:
            cause, unreadable = _poll_halt(mode, state_dir)
            if cause or heard():
                break
            try:
                child.wait(timeout=poll_s)
            except subprocess.TimeoutExpired:
                continue
            # The worker ended by itself. At the budget cap it writes HALT before it exits; a closed
            # terminal's SIGHUP reaches the worker too, and may end it before this process looks.
            cause, unreadable = _poll_halt(mode, state_dir)
            if not cause and heard():
                break
            now = time.time()
            return Stopped(cause, now, now, unreadable)
    except KeyboardInterrupt:
        cause, unreadable = "cli", False
        seen = time.time()
        _kill(child)
        request_stop(mode, "cli", state_dir)
        return Stopped(cause, seen, time.time(), unreadable)
    seen = time.time()
    _kill(child)
    if cause:
        return Stopped(cause, seen, time.time(), unreadable)
    request_stop(mode, "signal", state_dir)            # as Ctrl+C: the kill first, then HALT
    return Stopped("signal", seen, time.time(), False, signal=heard())


def _say(text):
    try:
        print(text, flush=True)
    except (OSError, ValueError):
        pass                    # the terminal is gone (a hang-up, a closed pipe): the receipts keep the record


def _resume_command():
    return "py -m doris resume" if os.name == "nt" else "python3 -m doris resume"


def _halt_refusal(mode, state_dir=None):
    """One plain line when a halt is in force, else None."""
    try:
        record = read_halt(mode, state_dir)
        stops = [p for p in stop_files(mode, state_dir) if _exists(p)]
    except HaltFileError as exc:
        return ("Not started: a halt file can't be read (%s), so she stays stopped. Run `%s` to clear it."
                % (exc, _resume_command()))
    if record is not None:
        line = "Not started: Doris is halted (%s). Run `%s` to clear it" % (
            CAUSE_WORDS[record["cause"]], _resume_command())
        if stops:
            line += ", and delete %s" % (", ".join(stops),)
        return line + "."
    if stops:
        return "Not started: a STOP file is in force (%s). Delete it to let her run." % (", ".join(stops),)
    return None


def _requested_epoch(mode, cause, run_started, state_dir=None):
    """When the stop was asked: HALT's requested time, a STOP file's mtime, else None."""
    try:
        if cause == "stop_file":
            for path in stop_files(mode, state_dir):
                if _exists(path):
                    stamp = os.lstat(path).st_mtime
                    return stamp if stamp >= run_started else None
            return None
        record = read_halt(mode, state_dir)
        text = (record or {}).get("requested_utc", "")
        stamp = _dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=_dt.timezone.utc)
        return stamp.timestamp()
    except (HaltFileError, OSError, ValueError):
        return None


def stop_timing(mode, stopped, run_started, state_dir=None):
    """(when the stop was asked, as epoch seconds or None; ms from then to a dead worker).
    A request on disk later than the death (Ctrl+C writes HALT after the kill) or before the run
    started does not count: the time the supervisor saw the stop is used instead."""
    requested = _requested_epoch(mode, stopped.cause, run_started, state_dir)
    if requested is not None and not run_started <= requested <= stopped.dead_wall:
        requested = None
    origin = stopped.seen_wall if requested is None else requested
    return requested, max(0, int(round((stopped.dead_wall - origin) * 1000)))


def _iso(epoch):
    return _dt.datetime.fromtimestamp(epoch, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _spawn(worker_argv, cwd):
    """Start the one child worker. It takes the ledger lock itself and holds it for its life.
    Its stdin is its lifeline: a pipe whose other end only this process holds and never writes to,
    and LIFELINE_ENV tells it so (see Lifeline). Windows: a new process group, so Ctrl+C reaches
    only the supervisor, which kills the worker."""
    env = dict(os.environ)
    env[LIFELINE_ENV] = LIFELINE_STDIN
    kwargs = {"cwd": cwd or paths.SRC, "stdin": subprocess.PIPE, "env": env}
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    if flags:
        kwargs["creationflags"] = flags
    return subprocess.Popen([str(a) for a in worker_argv], **kwargs)


def _let_go(child):
    """Close this end of the worker's lifeline: only once the worker is dead."""
    pipe = getattr(child, "stdin", None)
    if pipe is not None:
        try:
            pipe.close()
        except OSError:
            pass


# ------------------------------------------------------------------------------------------
# The worker's lifeline
# ------------------------------------------------------------------------------------------
LIFELINE_ENV = "DORIS_LIFELINE"     # set by _spawn: the worker's stdin is its lifeline
LIFELINE_STDIN = "stdin"


class Lifeline:
    """In the worker: its end of the pipe whose other end only its supervisor holds, and never
    writes to. However the supervisor ends (SIGKILL, a crash), the OS closes that end and a read
    here returns nothing: then nothing can kill her mid-call any more, and nobody will write her
    halted line. So `gone` turns true and HALT is written at once (cause supervisor_gone); the run
    reads `gone` at every halt check and at G1, so she stops even when HALT cannot be written,
    and writes her own halted line and summary (loop.run). The folder HALT goes to is fixed when
    the lifeline is made: the one she was started with, whatever the paths say by the time it fires."""

    def __init__(self, fd, mode, state_dir=None):
        self.fd, self.mode = fd, mode
        try:
            self.folder = _folder(mode, state_dir)
        except ValueError:
            self.folder = None          # no folder to write HALT in: `gone` stops her all the same
        self.gone = False
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._watch, name="doris-lifeline", daemon=True)
        self.thread.start()
        return self

    def _watch(self):
        try:
            while os.read(self.fd, 1):
                pass                    # the supervisor writes nothing; were it to, it would mean nothing
        except OSError:
            pass                        # a lifeline that cannot be read holds nothing: gone (fail closed)
        self.gone = True
        if self.folder is None:
            return
        try:
            request_stop(self.mode, "supervisor_gone", self.folder)
        except Exception:
            pass                        # `gone` stops her all the same, and loop.run tries HALT again


def lifeline(mode, state_dir=None, environ=None, fd=0) -> Optional[Lifeline]:
    """In the worker: its lifeline, started, when run_supervised gave it one (LIFELINE_ENV), else
    None. A worker started any other way never reads its stdin."""
    environ = os.environ if environ is None else environ
    if environ.get(LIFELINE_ENV) != LIFELINE_STDIN:
        return None
    return Lifeline(fd, mode, state_dir).start()


def _summary(line, stopped_after_ms):
    payload = line.get("payload") or {}
    parts = [line.get("why", {}).get("text", "Halted.")]
    if payload.get("signal"):
        parts.append("The signal was %s." % (payload["signal"],))
    if stopped_after_ms is not None:
        parts.append("The worker was dead %d ms after the stop." % (stopped_after_ms,))
    flight = payload.get("in_flight")
    if flight:
        parts.append("In flight: %s %s (seq %s)%s." % (flight.get("call"), flight.get("target"), flight.get("seq"),
                                                     ", unconfirmed until resume reconciles it"
                                                     if flight.get("unconfirmed") else ""))
    else:
        parts.append("Nothing in flight.")
    dropped = payload.get("dropped")
    if dropped is None:
        parts.append("The ledger could not be read, so the dropped work is unknown.")
    else:
        parts.append("Dropped %d queued item(s)." % (len(dropped),))
    parts.append("Run `%s` to go on." % (_resume_command(),))
    return " ".join(parts)


def lock_path_for(ledger_path):
    """The ledger's lock file: ledger.lock in the ledger's own folder (state/<mode>/ledger.lock)."""
    return os.path.join(os.path.dirname(os.path.abspath(ledger_path)), LOCK_NAME)


def wait_for_lock(path, timeout_s=RELOCK_S) -> Optional[LockHandle]:
    """acquire_lock, retried for up to timeout_s while another holder keeps it (the OS frees a
    dead process's lock promptly on POSIX; on Windows it can take a moment)."""
    deadline = time.monotonic() + timeout_s
    while True:
        held = acquire_lock(path)
        if held is not None or time.monotonic() >= deadline:
            return held
        time.sleep(0.01)


def run_supervised(mode, run_name, worker_argv, receipts_path, ledger_path, *, poll_s=POLL_S, say=None,
                   cwd=None, receipts_module=None, open_ledger=None, run_id=None, state_dir=None) -> int:
    """Run the worker as one child process under the kill switch. Refuses to start while a halt
    is in force or another Doris holds this ledger (one plain line, said). The worker takes the
    ledger lock itself and holds it for its life (G2 asks the worker); this process only checks
    that the lock is free before it starts the worker. On a halt: kill the worker mid-call; once
    it is dead, take the lock and write the halted line. From the worker's start to the halted
    line, SIGTERM and SIGHUP are stops (StopSignals), so a second one cannot cut that line short.
    Returns the worker's own exit code when it ends by itself with no halt, else one of the EXIT_
    codes above."""
    say = say or _say
    refusal = _halt_refusal(mode, state_dir)
    if refusal:
        say(refusal)
        return EXIT_REFUSED_HALT
    lock_path = lock_path_for(ledger_path)
    try:
        probe = acquire_lock(lock_path)
    except OSError as exc:
        say("Not started: the ledger lock %s could not be taken (%s)." % (lock_path, exc.__class__.__name__))
        return EXIT_LOCKED
    if probe is None:
        say("Not started: %s." % (LOCK_REFUSAL,))
        return EXIT_LOCKED
    probe.release()                          # free again: the worker takes it for its own life
    with StopSignals() as signals:
        return _supervise(mode, run_name, worker_argv, receipts_path, ledger_path, lock_path, signals, poll_s=poll_s,
                          say=say, cwd=cwd, receipts_module=receipts_module, open_ledger=open_ledger, run_id=run_id,
                          state_dir=state_dir)


def _supervise(mode, run_name, worker_argv, receipts_path, ledger_path, lock_path, signals, *, poll_s, say, cwd,
               receipts_module, open_ledger, run_id, state_dir) -> int:
    """run_supervised from the worker's start: watch it, and on a halt write the halted line."""
    started = time.time()
    child = _spawn(worker_argv, cwd)
    try:
        stopped = watch(child, mode, poll_s, state_dir, signals)
    finally:
        _kill(child)                         # never leave a worker behind a supervisor that leaves
        _let_go(child)                       # its lifeline, closed only now that it is dead
    if child.returncode == WORKER_EXIT_LOCKED:
        say("Not started: %s." % (LOCK_REFUSAL,))   # it lost a race for the lock: it never ran
        return EXIT_LOCKED
    if stopped.cause is None:
        return child.returncode
    requested, after_ms = stop_timing(mode, stopped, started, state_dir)
    if stopped.cause == "stop_file" and not stopped.unreadable:
        request_stop(mode, "stop_file", state_dir)      # the halt survives the STOP file's removal; resume clears it

    # The worker is dead, so its lock is free: hold it while the ledger and receipts are touched,
    # so no second Doris and no resume can slip in before the halted line is down.
    lock = wait_for_lock(lock_path)
    if lock is None:
        say("Halted (%s), and the halt stays in force, but another Doris holds the ledger now, so the "
            "halted receipt line was not written." % (CAUSE_WORDS[stopped.cause],))
        return EXIT_HALT_UNRECORDED
    try:
        line = record_halt(mode, run_name, stopped.cause, receipts_path, ledger_path,
                           receipts_module=receipts_module, open_ledger=open_ledger, run_id=run_id,
                           stop_to_dead_ms=after_ms, requested_utc=_iso(requested) if requested else None,
                           worker_exit=child.returncode, halt_file_unreadable=stopped.unreadable,
                           signal=stopped.signal)
    except Exception as exc:
        say("Halted (%s), and the halt stays in force, but the halted receipt line could not be "
            "written: %s: %s" % (CAUSE_WORDS[stopped.cause], exc.__class__.__name__, exc))
        return EXIT_HALT_UNRECORDED
    finally:
        lock.release()
    say(_summary(line, after_ms))
    return EXIT_HALTED
