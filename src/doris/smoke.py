"""python -m doris smoke: every live adapter call once, against the real servers, before a live run.

Usage: python -m doris smoke [--env PATH] [--send-to ADDRESS]

  --env PATH         the env file, read with envfile (default /root/doris-secrets/.env)
  --send-to ADDRESS  also send one plain test message, only ever to wolf.whizz@fastmail.com or
                     whizz@fastmail.com (anything else is refused), then look for her copy in Sent

Each step prints one plain line: its name, then ok (with a count where it counts something), or
the failure's class name with the adapter's own text, which carries a step, a status code or an
exception's class name and never a value. No line names an address, a password, a folder, a
calendar or an event, with one exception: when the test event may still be on the calendar, a
line names its booking ID (a fixed one, SMOKE_BOOKING_ID) so a person can find it.

Steps, in order; each runs whatever the one before it did:
  imap login and inbox count                 LiveMail.inbox_count (read-only)
  sent folder found by its special-use flag  LiveMail.sent_folders (the \\Sent flag, never a name)
  smtp login, nothing sent                   LiveMail.check_smtp_login
  caldav query, next 7 days                  LiveCalendar.query, counted
  caldav test event left by an earlier run   a test event an earlier smoke left (cut short, or its
                                             delete failed) is deleted first
  caldav create test event                   30 minutes about 400 days ahead, at 11:00 UTC (the
                                             small hours in Pacific time), always under the one
                                             fixed booking ID SMOKE_BOOKING_ID
  caldav read test event back, times match   LiveCalendar.get, start and end compared
  caldav delete test event                   LiveCalendar.delete, up to CLEANUP_TRIES times; it runs
                                             in a finally, so a failed step or Ctrl-C still deletes
  caldav read after delete, gone             LiveCalendar.get is None
  caldav test event may remain               only when it was not seen gone: names the booking ID
With --send-to (without it, one line in their place says they did not run):
  smtp send one test message                 LiveMail.send: signed, Auto-Submitted, no Bcc
  sent copy, look 1 (at once)                LiveMail.find_in_sent, as the loop looks right after a send
  sent copy, look 2 (after a short wait)     the same after SENT_LOOK_WAIT_S
  sent copy found                            which look found the copy first, and how long after the
                                             send; it counts as found when either look found it
  sent copy record for a live run            written only when the copy was found (see below)

The last line says "ok, every step passed" only when every step ran and passed. When every step
that ran passed but one did not run, it says so and names it, with why.

Every connection goes through the network guard, allowing only live.hosts(env), so a host the
adapters reach without naming it fails the smoke. A smoke is a live run's peer: it refuses to
start while a halt is in force for live (HALT, or a STOP file) and holds the live ledger lock
(state/live/ledger.lock) from before its first connection to after its last, so it never runs
beside a live run and no live run starts while it runs.

A live run starts only after a smoke found its --send-to copy in Sent: that smoke, and only
that one, writes state/live/smoke-sent.json, whole (a temp file, then os.replace), holding
exactly {"ts": UTC time, "sent_copy_found": true, "look": 1 or 2, "waited_s": seconds from the
end of the send to the end of the look that found it}: no address, ID or setting. Every smoke
that runs removes the old record first, so one that does not find its copy, runs without
--send-to or is cut short leaves none; an old record it cannot remove stops it before it
contacts anything (exit 1). A smoke refused for a halt or the lock leaves the record alone.

Exit 0 when every step that ran passed; 1 when any failed; 2 when the command line or the env
file is wrong; 3 when a live run holds the lock (or the lock cannot be taken); 5 when a halt is
in force. On 2, 3 and 5 nothing is contacted.
"""

import argparse
import datetime as _dt
import email.message
import email.policy
import email.utils
import json
import os
import secrets
import tempfile
import time

from . import caps, compose, envfile, ics, netguard, paths, supervisor, templates
from .adapters import live
from .shapes import CalendarError, MailError, Slot

UTC = _dt.timezone.utc
DEFAULT_ENV = "/root/doris-secrets/.env"
SEND_TO_ALLOWED = ("wolf.whizz@fastmail.com", "whizz@fastmail.com")   # the two test inboxes; nothing else
QUERY_DAYS = 7
TEST_EVENT_AHEAD = _dt.timedelta(days=400)       # far past any booking horizon (caps.MAX_HORIZON)
TEST_EVENT_UTC_HOUR = 11
TEST_EVENT_LENGTH = _dt.timedelta(minutes=30)
# One fixed booking ID (it passes ics.is_booking_id; no real booking's "doris-" + 24 hex can be it):
# a run that leaves its test event behind is cleaned up by the next run, which deletes it first.
SMOKE_BOOKING_ID = "doris-smoke-test"
CLEANUP_TRIES = 3                                # the test event's delete, a short wait apart
CLEANUP_WAIT_S = caps.CALENDAR_RETRY_WAIT_S
SENT_LOOK_WAIT_S = 5.0                           # between the two looks in Sent
SEND_NOT_RUN = "smtp send one test message, then its copy in Sent"   # its line when there is no --send-to
SENT_RECORD = "smoke-sent.json"                  # in state/live: what a live run needs before it starts
TEST_TEXT = "A test message from Doris's live smoke check. Nothing to do and nothing to answer."

_OWN_ERRORS = (MailError, CalendarError, live.ConfigError, envfile.EnvFileError)   # texts built never to hold a value
_sleep = time.sleep
_clock = time.monotonic


class _Failed(Exception):
    """A step ran and found something wrong. Its text is one of the fixed phrases here."""


def _why(exc):
    """A failure as printed: our own error classes with their text, anything else by class name."""
    if isinstance(exc, _Failed):
        return "FAILED (%s)" % exc
    if isinstance(exc, _OWN_ERRORS):
        return "%s (%s)" % (type(exc).__name__, exc)
    return type(exc).__name__


def _count(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def _step(out, name, call):
    """Run one step and print its one line. True when it passed."""
    try:
        note = call()
    except Exception as exc:      # every failure is a line, and the next step still runs
        out("%s: %s" % (name, _why(exc)))
        return False
    out("%s: ok%s" % (name, " (%s)" % note if note else ""))
    return True


def _not_run(out, name, why):
    """A step that did not run: its one line, and its name with why for the last line."""
    out("%s: not run (%s)" % (name, why))
    return "%s (%s)" % (name, why)


# ------------------------------------------------------------------------------------------------
# The steps
# ------------------------------------------------------------------------------------------------
def check(mail, calendar, out, send_to=None, now=None, record=None):
    """Every step against these adapters, one line each. (True when every step that ran passed,
    [each step that did not run, named with why]). With send_to and record (a path), the sent
    copy record is written there when the copy was found."""
    now = now or _dt.datetime.now(UTC)
    not_run = []
    passed = [
        _step(out, "imap login and inbox count", lambda: _count(mail.inbox_count(), "message") + " in INBOX"),
        _step(out, "sent folder found by its special-use flag", lambda: _count(len(mail.sent_folders()), "folder")),
        _step(out, "smtp login, nothing sent", mail.check_smtp_login),
        _step(out, "caldav query, next 7 days", lambda: _query(calendar, now)),
    ]
    passed += _calendar_round_trip(calendar, out, now)
    if send_to is None:
        not_run.append(_not_run(out, SEND_NOT_RUN, "no --send-to"))
    else:
        flags, found = _send_and_look(mail, out, send_to, now)
        passed += flags
        if record is not None:
            passed.append(_record_step(out, record, found))
    return all(passed), not_run


def _query(calendar, now):
    events = calendar.query(now, now + _dt.timedelta(days=QUERY_DAYS))
    note = _count(len(events), "event")
    whole = getattr(calendar, "last_query_whole_span", 0)
    if whole:
        note += ", %d read as busy over the whole span (unreadable or not expanded)" % whole
    return note


def _calendar_round_trip(calendar, out, now):
    """Delete any test event an earlier run left, create one far ahead, read it back; then, in a
    finally (a failed step or Ctrl-C included), delete it with retries and read again to see it
    gone. Unless it was seen gone, one more line names its booking ID."""
    booking_id = SMOKE_BOOKING_ID
    day = (now + TEST_EVENT_AHEAD).date()
    start = _dt.datetime(day.year, day.month, day.day, TEST_EVENT_UTC_HOUR, tzinfo=UTC)
    slot = Slot(start, start + TEST_EVENT_LENGTH)
    event = ics.build_event(booking_id, slot, templates.PLACEHOLDER_MEETING_LINK, "CONFIRMED", 0, now, None)

    def leftover():
        if calendar.get(booking_id) is None:
            return "none"
        calendar.delete(booking_id)
        if calendar.get(booking_id) is not None:
            raise _Failed("still there")
        return "removed one"

    def read_back():
        ev = calendar.get(booking_id)
        if ev is None:
            raise _Failed("not found")
        if (ev.start_utc, ev.end_utc) != (slot.start_utc, slot.end_utc):
            raise _Failed("times differ")

    def gone():
        if calendar.get(booking_id) is not None:
            raise _Failed("still there")

    passed, seen_gone = [], False
    try:
        passed.append(_step(out, "caldav test event left by an earlier run", leftover))
        passed.append(_step(out, "caldav create test event", lambda: calendar.create_only(booking_id, event)))
        passed.append(_step(out, "caldav read test event back, times match", read_back))
    finally:
        try:
            passed.append(_delete_with_retries(calendar, out, booking_id))
            seen_gone = _step(out, "caldav read after delete, gone", gone)
            passed.append(seen_gone)
        finally:
            if not seen_gone:
                out("caldav test event may remain: booking ID %s (the next smoke deletes it first)" % booking_id)
    return passed


def _delete_with_retries(calendar, out, booking_id):
    """The test event's delete, tried up to CLEANUP_TRIES times a short wait apart. One line."""
    for attempt in range(1, CLEANUP_TRIES + 1):
        try:
            calendar.delete(booking_id)
        except Exception as exc:
            if attempt < CLEANUP_TRIES:
                _sleep(CLEANUP_WAIT_S)
                continue
            out("caldav delete test event: %s, on each of %d tries" % (_why(exc), CLEANUP_TRIES))
            return False
        out("caldav delete test event: ok" + ("" if attempt == 1 else " (on try %d)" % attempt))
        return True


def _test_message(doris_addr, to_addr, message_id, now):
    """One plain, signed test message from her to one test inbox. No Bcc: the envelope is the To."""
    msg = email.message.EmailMessage(policy=email.policy.SMTP)
    msg["From"] = doris_addr
    msg["To"] = to_addr
    msg["Subject"] = "Doris live smoke test"
    msg["Date"] = email.utils.format_datetime(now.astimezone(UTC).replace(microsecond=0))
    msg["Message-ID"] = message_id
    msg["Auto-Submitted"] = "auto-generated"      # RFC 3834: no machine answers it
    msg.set_content(compose.with_signature(TEST_TEXT))
    return msg.as_bytes()


def _send_and_look(mail, out, send_to, now):
    """Send one test message, then look for her copy in Sent at once and after a short wait.
    ([the send passed, the copy was found], (the look that found it first, seconds from the end
    of the send to the end of that look) or None)."""
    message_id = "<%s@%s>" % (secrets.token_hex(16), mail.doris_addr.rsplit("@", 1)[-1])
    raw = _test_message(mail.doris_addr, send_to, message_id, now)
    sent = _step(out, "smtp send one test message", lambda: mail.send(raw, [send_to], message_id, template="smoke"))
    after_send = _clock()
    found = None
    seen = []
    for look, name in ((1, "sent copy, look 1 (at once)"), (2, "sent copy, look 2 (after a short wait)")):
        if look == 2:
            _sleep(SENT_LOOK_WAIT_S)
        result = _look(mail, message_id)
        seen.append(result)
        if result is True:
            if found is None:
                found = (look, _clock() - after_send)
            line = "ok (found)"
        elif result is False:
            line = "not found yet" if look == 1 else "not found"
        else:
            line = result                              # the failure, as printed
        out("%s: %s" % (name, line))
    if found is not None:
        out("sent copy found: ok (look %d, %.1f s after the send)" % found)
    elif seen == [False, False]:
        out("sent copy found: FAILED (not found on either look)")
    else:
        out("sent copy found: FAILED (not found; a look could not tell)")
    return [sent, found is not None], found


def _record_step(out, path, found):
    """The sent copy record for a live run, written when the copy was found. True unless the
    write failed (a copy not found has already failed its own step)."""
    name = "sent copy record for a live run"
    if found is None:
        out(name + ": not written (the copy was not found)")
        return True
    look, waited = found
    record = {"ts": _dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "sent_copy_found": True,
              "look": look, "waited_s": round(waited, 1)}

    def write():
        _write_whole(path, json.dumps(record, sort_keys=True).encode("ascii") + b"\n")
        return "written"
    return _step(out, name, write)


def _write_whole(path, data):
    """Write data to path whole or not at all: a temp file beside it, flushed to disk, then
    os.replace. On any failure no temp file is left and the error is raised."""
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", suffix=".tmp", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _look(mail, message_id):
    """True found, False not found, or the failure as printed."""
    try:
        return mail.find_in_sent(message_id) is True
    except Exception as exc:
        return _why(exc)


# ------------------------------------------------------------------------------------------------
# The command
# ------------------------------------------------------------------------------------------------
def _parser():
    ap = argparse.ArgumentParser(prog="python -m doris smoke",
                                 description="Every live adapter call once, against the real servers.")
    ap.add_argument("--env", default=DEFAULT_ENV, metavar="PATH", help="the env file (default %(default)s)")
    ap.add_argument("--send-to", metavar="ADDRESS",
                    help="also send one test message to %s" % " or ".join(SEND_TO_ALLOWED))
    return ap


def _test_inbox(value):
    """The allowed spelling of a test inbox, or None for anything else."""
    value = value.strip().lower()
    return value if value in SEND_TO_ALLOWED else None


def _print(line):
    print(line, flush=True)


def main(argv, out=None) -> int:
    out = out or _print
    args = _parser().parse_args(argv)
    send_to = None
    if args.send_to is not None:
        send_to = _test_inbox(args.send_to)
        if send_to is None:
            out("smoke: refused --send-to: it takes only %s" % " or ".join(SEND_TO_ALLOWED))
            return 2
    try:
        env = envfile.load(args.env)
    except envfile.EnvFileError as exc:
        out("env file: " + _why(exc))
        return 2
    try:
        hosts = live.hosts(env)
        mail = live.LiveMail(env, env.get(live.ENV_ADDRESS, ""))
        calendar = live.LiveCalendar(env)
    except live.ConfigError as exc:
        out("config: " + _why(exc))
        return 2
    state = paths.state_dir("live")
    halted = _halt_in_force(state)
    if halted:
        out("smoke: refused: " + halted)
        return supervisor.EXIT_REFUSED_HALT
    try:
        lock = supervisor.acquire_lock(os.path.join(state, supervisor.LOCK_NAME))
    except OSError as exc:
        out("smoke: refused: the live lock cannot be taken (%s)" % type(exc).__name__)
        return supervisor.EXIT_LOCKED
    if lock is None:
        out("smoke: refused: " + supervisor.LOCK_REFUSAL)
        return supervisor.EXIT_LOCKED
    try:
        record = os.path.join(state, SENT_RECORD)
        try:
            os.remove(record)                  # an old record never outlives the smoke that follows it
        except FileNotFoundError:
            pass
        except OSError as exc:
            out("smoke: FAILED: the old sent-copy record cannot be removed (%s)" % type(exc).__name__)
            return 1
        guard = netguard.install(hosts)
        try:
            ok, not_run = check(mail, calendar, out, send_to=send_to, record=record)
        finally:
            guard.uninstall()
    finally:
        lock.release()
    refused = guard.refused_count()
    if refused:
        out("network guard: FAILED (%s to a host the adapters do not name)" % _count(refused, "connection"))
        ok = False
    verdict = "FAILED" if not ok else "every step that ran passed" if not_run else "ok, every step passed"
    out("smoke: " + verdict + ("; not run: " + "; ".join(not_run) if not_run else ""))
    return 0 if ok else 1


def _halt_in_force(state):
    """Why the smoke may not start, when a halt is in force for live; else None. A halt file it
    cannot read counts as a halt (fail closed), as it does for a run."""
    try:
        cause = supervisor.halt_cause("live", state_dir=state)
    except (supervisor.HaltFileError, OSError, ValueError):
        return "a halt file cannot be read, so the halt stays in force"
    if cause is None:
        return None
    return "Doris is halted (%s)" % supervisor.CAUSE_WORDS.get(cause, "a halt")
