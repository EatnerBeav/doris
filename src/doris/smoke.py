"""python -m doris smoke: every live adapter call once, against the real servers, before a live run.

Usage: python -m doris smoke [--env PATH] [--send-to ADDRESS]

  --env PATH         the env file, read with envfile (default /root/doris-secrets/.env)
  --send-to ADDRESS  also send one plain test message, only ever to wolf.whizz@fastmail.com or
                     whizz@fastmail.com (anything else is refused), then look for her copy in Sent;
                     and, with WOLF_EMAIL and WOLF_JMAP_TOKEN in the env file, the end-to-end test
                     (below). Without --send-to nothing is sent.

Each step prints one plain line: its name, then ok (with a count where it counts something), or
the failure's class name with the adapter's own text, which carries a step, a status code or an
exception's class name and never a value. No line names an address, a password, a folder, a
calendar or an event, with two exceptions: when the test event may still be on the calendar, a
line names its booking ID (SMOKE_BOOKING_PREFIX and the run's UTC stamp) so a person can find it,
and one line names any event under that prefix that is not in the smoke's own form (left alone).
The calendar's display name is read and never shown (it can be the owner's full name, and every
line goes to a trace kept in a public repo): its line says only "ok (name read)".

Steps, in order; each runs whatever the one before it did:
  imap login and inbox count                 LiveMail.inbox_count (read-only)
  sent folder found by its special-use flag  LiveMail.sent_folders (the \\Sent flag, never a name)
  imap fetch, last 7 days, read-only         LiveMail.fetch_new with since_utc a week back, counted
                                             (EXAMINE and BODY.PEEK: nothing about her mail changes)
  imap file newest INBOX message with the smoke keyword
                                             LiveMail.file: FILE_KEYWORD on the message that arrived
                                             last, the only change the smoke makes to her mail besides
                                             its own test request's. When the fetch found none (as
                                             smoke run 2's did: the smoke's own test requests are
                                             never handed over to it) or failed, a smoke with the
                                             end-to-end test runs this step there, on its test
                                             request (below); any other says it did not run
  jmap session, nothing sent                 LiveMail.check_send_login: her JMAP session (the token,
                                             its scopes, her account), mailboxes and identity
  caldav calendar display name               LiveCalendar.display_name: the calendar has one; never shown
  caldav query, next 7 days                  LiveCalendar.query, counted
  caldav test event left by an earlier run   every test event earlier smokes left (cut short, or
                                             a delete failed), found by its booking ID's prefix
                                             (LiveCalendar.booking_ids) and in the smoke's own form
                                             (its run's far-ahead slot, confirmed), is deleted
                                             first; nothing else is touched: anything else under
                                             the prefix is named on one more line, and it goes on
  caldav create test event                   30 minutes about 400 days ahead, at 11:00 UTC (the
                                             small hours in Pacific time), under a booking ID of
                                             its own: SMOKE_BOOKING_PREFIX and the run's UTC stamp,
                                             its trace folder's (a calendar can keep the name of
                                             an event deleted under it and refuse it a new one)
  caldav read test event back, times match   LiveCalendar.get, start and end compared
  caldav delete test event                   LiveCalendar.delete, up to CLEANUP_TRIES times; it runs
                                             in a finally, so a failed step or Ctrl-C still deletes
  caldav read after delete, gone             LiveCalendar.get is None
  caldav test event may remain               only when it was not seen gone: names the booking ID
With --send-to (without it, one line in their place says they did not run):
  jmap send one test message                 LiveMail.send over JMAP: signed, Auto-Submitted, no Bcc
  sent copy, look 1 (at once)                LiveMail.find_in_sent, as the loop looks right after a send
  sent copy, look 2 (after a short wait)     the same after SENT_LOOK_WAIT_S
  sent copy found                            which look found the copy first, and how long after the
                                             send; it counts as found when either look found it
  sent copy record for a live run            written only when the copy was found (see below)
The end-to-end test, with --send-to and WOLF_EMAIL and WOLF_JMAP_TOKEN (the wolf.whizz account, one
of the two test inboxes, and its API token: the smoke's alone, never her loop's), once the record
is written (her loop answers only after a smoke found its copy in Sent); else one line says it did
not run, and why:
  wolf sends one meeting request to the owner     over JMAP with wolf's token, from WOLF_EMAIL to
                                                  DORIS_OWNER_ADDRESS: a call next week, no exact time,
                                                  under a Message-ID the smoke mints (live.SMOKE_MID_MARK
                                                  + 32 hex: no live run ever reads it, marked or not)
  test request in her INBOX, look 1 and look 2    LiveMail.find_in_inbox by that Message-ID, each after
                                                  FORWARD_WAIT_S: the owner's forward to her
  test request marked as the smoke's own          LiveMail.file with SMOKE_TEST_KEYWORD
  imap file newest INBOX message with the smoke keyword
                                                  only when the fetch step found no message to file:
                                                  LiveMail.file with FILE_KEYWORD on the test request,
                                                  a step like any other (when the end-to-end test does
                                                  not run, a line says this step did not run either)
  her loop on the test request                    loop.run, live, the live model, on a throwaway ledger
                                                  (state/live/smoke/<UTC stamp>/, started before the
                                                  request was sent) and on that one message alone
                                                  (smoke_message_id); the live folder's HALT, STOP and
                                                  sent record are the ones it obeys, and the owner's
                                                  calendar is read only (calendar_read_only). Passes
                                                  when she handled it and sent one reply through the gate
  her reply confirmed in her Sent                 her reply's send is confirmed in that ledger: her own
                                                  two looks in Sent found her copy
  her reply in wolf's mailbox, look 1 and look 2  JmapSender.find_message with wolf's token, at once
                                                  and after REPLY_WAIT_S
A look that finds the message makes the next one "not needed". A step that cannot run because the
one before it failed is named, with the steps after it, in one line as not run.

The last line before the trace's says "ok, every step passed" only when every step ran and
passed. When every step that ran passed but one did not run, it says so and names it, with why.

Every smoke that contacted anything keeps its trace (livetrace), pass or fail, cut short
included: traces/live-smoke-<UTC stamp>/ with her loop's receipts (when it ran), manifest.json,
timeline.txt and terminal.log (every line printed); its last line names the folder. Outcome pass
only when every step ran and passed.

Every connection goes through the network guard, allowing only live.hosts(env) (and, for the
end-to-end test, the model's: loop.model_hosts()), and the host a JMAP session names for apiUrl
and uploadUrl once the sender checked it (jmap: below api.fastmail.com, https, port 443), for her
sender and wolf's alike, so a host the adapters reach without naming it fails the smoke. A smoke
is a live run's peer: it refuses to
start while a halt is in force for live (HALT, or a STOP file) and holds the live ledger lock
(state/live/ledger.lock) from before its first connection to after its last, so it never runs
beside a live run and no live run starts while it runs.

A live run starts only after a smoke found its --send-to copy in Sent: that smoke, and only
that one, writes state/live/smoke-sent.json, whole (a temp file, then os.replace), holding
exactly {"ts": UTC time, "sent_copy_found": true, "look": 1 or 2, "waited_s": seconds from the
end of the send to the end of the look that found it}: no address, ID or setting. The file and
its format are sentrecord's, which the live run's preflight reads, so the two agree; a run
takes the record for a day after it is written (caps.SMOKE_RECORD_MAX_AGE), then needs a new
smoke. Every smoke that runs removes the old record first, so one that does not find its copy,
runs without --send-to or is cut short leaves none; an old record it cannot remove stops it
before it contacts anything (exit 1). A smoke refused for a halt or the lock leaves the record
alone.

Exit 0 when every step that ran passed; 1 when any failed; 2 when the command line or the env
file is wrong; 3 when a live run holds the lock (or the lock cannot be taken); 5 when a halt is
in force. On 2, 3 and 5 nothing is contacted and no trace is kept.
"""

import argparse
import datetime as _dt
import email.message
import email.policy
import email.utils
import os
import secrets
import time

from . import caps, compose, envfile, ics, ledger as ledger_mod, livetrace, loop, netguard, paths, receipts
from . import sentrecord, supervisor, templates
from .adapters import jmap, live
from .shapes import CalendarError, MailError, Slot

UTC = _dt.timezone.utc
SEND_TO_ALLOWED = ("wolf.whizz@fastmail.com", "whizz@fastmail.com")   # the two test inboxes; nothing else
QUERY_DAYS = 7
FETCH_DAYS = 7                                   # the fetch step reads the INBOX mail of the last week
FILE_KEYWORD = "doris-smoke"                     # what the file step adds to her newest INBOX message
FETCH_STEP = "imap fetch, last %d days, read-only" % FETCH_DAYS
FILE_STEP = "imap file newest INBOX message with the smoke keyword"
TEST_EVENT_AHEAD = _dt.timedelta(days=400)       # far past any booking horizon (caps.MAX_HORIZON)
TEST_EVENT_UTC_HOUR = 11
TEST_EVENT_LENGTH = _dt.timedelta(minutes=30)
# Each run's test event has a booking ID of its own, this prefix and the run's UTC stamp (it passes
# ics.is_booking_id; no real booking's "doris-" + 24 hex can start with it). One fixed ID did not do:
# smoke run 2's create was refused 412 AlreadyExists under the ID run 1 had created and deleted 45
# minutes before, while a read and a UID query found nothing there. A run that leaves its test event
# behind is cleaned up by the next run, which finds every event under the prefix and deletes it first
# (the first runs' one fixed ID, "doris-smoke-test", starts with it too), but only one in the smoke's
# own form (_smoke_form): anything else under the prefix is named and left alone.
SMOKE_BOOKING_PREFIX = "doris-smoke-"
LEGACY_SMOKE_BOOKING_ID = SMOKE_BOOKING_PREFIX + "test"
LEGACY_SMOKE_DAYS = (_dt.date(2026, 11, 27), _dt.date(2026, 11, 28))   # the UTC days the first runs used it
LEFT_ALONE_STEP = "caldav events under the smoke's prefix, not its own"
CLEANUP_TRIES = 3                                # the test event's delete, a short wait apart
CLEANUP_WAIT_S = caps.CALENDAR_RETRY_WAIT_S
SENT_LOOK_WAIT_S = caps.SENT_LOOK_WAIT_S         # between the two looks in Sent, as a live run waits
SEND_NOT_RUN = "jmap send one test message, then its copy in Sent"   # its line when there is no --send-to
TEST_TEXT = "A test message from Doris's live smoke check. Nothing to do and nothing to answer."

# The end-to-end test: the wolf.whizz account's settings (the smoke's alone: her loop never reads them)
WOLF_ADDRESS, WOLF_TOKEN = "WOLF_EMAIL", "WOLF_JMAP_TOKEN"
ENV_NAMES = (WOLF_ADDRESS, WOLF_TOKEN)
E2E_NOT_RUN = "end-to-end test from wolf"        # its one line when it does not run
FORWARD_WAIT_S = 30.0                            # before each look for the request in her INBOX (the forward)
REPLY_WAIT_S = 30.0                              # before the second look for her reply in wolf's mailbox
SMOKE_LEDGER_BACK = _dt.timedelta(hours=1)       # the smoke ledger starts this long before the request is sent
REQUEST_SUBJECT = "Smoke test: a call next week?"
REQUEST_TEXT = ("Hi Shane, could we find 30 minutes next week to talk about the inbox agent? Any afternoon "
                "works for me.\n\nWolf\n\n(Doris's live smoke check: her reply is checked, then nothing else.)")

_OWN_ERRORS = (MailError, CalendarError, live.ConfigError, envfile.EnvFileError,
               loop.Refused)                      # texts built never to hold a value
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


class _Out(object):
    """The smoke's terminal: every line through say, and a tally of its steps, in order, for the
    last line and the trace: each that failed, each that did not run (with why), the last named."""

    def __init__(self, say):
        self.say = say
        self.failed, self.not_run, self.last = [], [], None

    def __call__(self, line):
        self.say(line)

    def step(self, name, text, failed=False):
        self.last = name
        if failed:
            self.failed.append(name)
        self.say("%s: %s" % (name, text))


def _step(out, name, call):
    """Run one step and print its one line. True when it passed."""
    try:
        note = call()
    except Exception as exc:      # every failure is a line, and the next step still runs
        out.step(name, _why(exc), failed=True)
        return False
    out.step(name, "ok%s" % (" (%s)" % note if note else ""))
    return True


def _not_run(out, name, why):
    """A step that did not run: its one line, and its name with why for the last line."""
    out.step(name, "not run (%s)" % (why,))
    out.not_run.append("%s (%s)" % (name, why))
    return out.not_run[-1]


# ------------------------------------------------------------------------------------------------
# The steps
# ------------------------------------------------------------------------------------------------
def check(mail, calendar, out, send_to=None, now=None, record=None, e2e=None, e2e_off=None):
    """Every step against these adapters, one line each (out: an _Out). (True when every step that
    ran passed, [each step that did not run, named with why]). With send_to and record (a path),
    the sent copy record is written there when the copy was found; then, with e2e (_EndToEnd), the
    end-to-end test runs once that record is written. e2e_off: why it does not run, without e2e.
    When the fetch finds no message to file, a smoke with e2e files its test request instead."""
    now = now or _dt.datetime.now(UTC)
    passed = [
        _step(out, "imap login and inbox count", lambda: _count(mail.inbox_count(), "message") + " in INBOX"),
        _step(out, "sent folder found by its special-use flag", lambda: _count(len(mail.sent_folders()), "folder")),
    ]
    fetched = []

    def fetch():
        mail.since_utc = now - _dt.timedelta(days=FETCH_DAYS)
        fetched.extend(mail.fetch_new())
        return _count(len(fetched), "message")
    passed.append(_step(out, FETCH_STEP, fetch))
    file_later = None                            # why the file step waits for the end-to-end test's request
    if passed[-1] and fetched:
        newest = fetched[-1]                     # fetch_new hands them over oldest first, by arrival
        passed.append(_step(out, FILE_STEP, lambda: mail.file(newest.server_id, FILE_KEYWORD)))
    else:
        why = "no INBOX message in the last %d days" % FETCH_DAYS if passed[-1] else "the fetch failed"
        if e2e is None:
            _not_run(out, FILE_STEP, why)
        else:
            file_later = why                     # the smoke's own requests are never fetched: often none
    passed += [
        _step(out, "jmap session, nothing sent", mail.check_send_login),
        _step(out, "caldav calendar display name", lambda: _name_read(calendar.display_name())),
        _step(out, "caldav query, next 7 days", lambda: _query(calendar, now)),
    ]
    passed += _calendar_round_trip(calendar, out, now)
    written = False
    if send_to is None:
        _not_run(out, SEND_NOT_RUN, "no --send-to")
    else:
        flags, found = _send_and_look(mail, out, send_to, now)
        passed += flags
        if record is not None:
            written = _record_step(out, record, found) == "written"
    if e2e is None:
        _not_run(out, E2E_NOT_RUN, e2e_off or "no --send-to")
    elif not written:
        _not_run(out, E2E_NOT_RUN, "no sent copy record from this smoke: her loop answers only after a smoke "
                                   "found its copy in Sent")
        if file_later is not None:
            _not_run(out, FILE_STEP, file_later + ", and the end-to-end test did not run")
    else:
        _end_to_end(e2e, mail, out, file_request=file_later is not None)
    return all(passed) and not out.failed, list(out.not_run)


def _name_read(name):
    """The display name step's note: that the calendar has a name (display_name raises when it has
    none), never the name, which can be the owner's full name and would reach a public trace."""
    return "name read"


def _query(calendar, now):
    events = calendar.query(now, now + _dt.timedelta(days=QUERY_DAYS))
    note = _count(len(events), "event")
    whole = getattr(calendar, "last_query_whole_span", 0)
    if whole:
        note += ", %d read as busy over the whole span (unreadable or not expanded)" % whole
    return note


def smoke_booking_id(started):
    """The booking ID of the test event of a smoke started then: SMOKE_BOOKING_PREFIX and the UTC
    stamp, its trace folder's (livetrace.STAMP)."""
    return SMOKE_BOOKING_PREFIX + started.astimezone(UTC).strftime(livetrace.STAMP)


def _test_slot(started):
    """The far-ahead slot a smoke started then gives its test event."""
    day = (started + TEST_EVENT_AHEAD).date()
    start = _dt.datetime(day.year, day.month, day.day, TEST_EVENT_UTC_HOUR, tzinfo=UTC)
    return Slot(start, start + TEST_EVENT_LENGTH)


def _smoke_form(booking_id, ev):
    """True when the event under booking_id is a smoke's own test event as ics.build_event wrote it
    (calendar.get reads its times and status): a confirmed timed event at the far-ahead test slot of
    the run its ID names, doris-smoke-<UTC stamp>, or of one of the first runs' days for their one
    fixed ID, doris-smoke-test. The leftover step deletes nothing else."""
    if ev is None or ev.kind != "timed" or ev.status != "CONFIRMED":
        return False
    if booking_id == LEGACY_SMOKE_BOOKING_ID:
        starts = [_dt.datetime(d.year, d.month, d.day, tzinfo=UTC) for d in LEGACY_SMOKE_DAYS]
    else:
        try:
            starts = [_dt.datetime.strptime(booking_id[len(SMOKE_BOOKING_PREFIX):], livetrace.STAMP).replace(tzinfo=UTC)]
        except ValueError:
            return False
        if smoke_booking_id(starts[0]) != booking_id:
            return False
    return any((ev.start_utc, ev.end_utc) == (slot.start_utc, slot.end_utc)
               for slot in (_test_slot(started) for started in starts))


def _calendar_round_trip(calendar, out, now):
    """Delete every test event earlier runs left in the smoke's own form (naming anything else under
    the prefix), create this run's far ahead, read it back; then, in
    a finally (a failed step or Ctrl-C included), delete it with retries and read again to see it
    gone. Unless it was seen gone, one more line names its booking ID."""
    booking_id = smoke_booking_id(now)
    slot = _test_slot(now)
    event = ics.build_event(booking_id, slot, templates.PLACEHOLDER_MEETING_LINK, "CONFIRMED", 0, now, None)
    others = []

    def leftover():
        found = calendar.booking_ids(SMOKE_BOOKING_PREFIX)
        left = [earlier for earlier in found if _smoke_form(earlier, calendar.get(earlier))]
        others.extend(earlier for earlier in found if earlier not in left)
        for earlier in left:
            calendar.delete(earlier)
        if any(calendar.get(earlier) is not None for earlier in left):
            raise _Failed("still there")
        return "none" if not left else "removed one" if len(left) == 1 else "removed %d" % len(left)

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
        if others:                     # not the smoke's to delete: named, and the smoke goes on
            out.step(LEFT_ALONE_STEP, "left alone: " + ", ".join(others))
        passed.append(_step(out, "caldav create test event", lambda: calendar.create_only(booking_id, event)))
        passed.append(_step(out, "caldav read test event back, times match", read_back))
    finally:
        try:
            passed.append(_delete_with_retries(calendar, out, booking_id))
            seen_gone = _step(out, "caldav read after delete, gone", gone)
            passed.append(seen_gone)
        finally:
            if not seen_gone:
                out.step("caldav test event may remain", "booking ID %s (the next smoke deletes it first)" % booking_id)
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
            out.step("caldav delete test event", "%s, on each of %d tries" % (_why(exc), CLEANUP_TRIES), failed=True)
            return False
        out.step("caldav delete test event", "ok" + ("" if attempt == 1 else " (on try %d)" % attempt))
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
    sent = _step(out, "jmap send one test message", lambda: mail.send(raw, [send_to], message_id, template="smoke"))
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
        out.step(name, line)
    if found is not None:
        out.step("sent copy found", "ok (look %d, %.1f s after the send)" % found)
    elif seen == [False, False]:
        out.step("sent copy found", "FAILED (not found on either look)", failed=True)
    else:
        out.step("sent copy found", "FAILED (not found; a look could not tell)", failed=True)
    return [sent, found is not None], found


def _record_step(out, path, found):
    """The sent copy record for a live run, written when the copy was found: "written", "not
    written" (a copy not found has already failed its own step) or "failed" (the write failed)."""
    name = "sent copy record for a live run"
    if found is None:
        out.step(name, "not written (the copy was not found)")
        return "not written"
    look, waited = found

    def write():
        sentrecord.write(path, look, waited, _dt.datetime.now(UTC))     # whole, in the one format
        return "written"
    return "written" if _step(out, name, write) else "failed"


def _look(mail, message_id):
    """True found, False not found, or the failure as printed."""
    try:
        return mail.find_in_sent(message_id) is True
    except Exception as exc:
        return _why(exc)


# ------------------------------------------------------------------------------------------------
# The end-to-end test: wolf's request, the owner's forward, her loop, her reply in wolf's mailbox
# ------------------------------------------------------------------------------------------------
class _EndToEnd(object):
    """What the end-to-end test needs, all checked before anything is contacted, and what it leaves
    for the trace: her loop's run name and receipts once it ran. Its repr shows no value."""

    def __init__(self, wolf, owner_addr, env_path, live_dir, model_id, started):
        self.wolf, self.owner_addr, self.env_path = wolf, owner_addr, env_path
        self.live_dir, self.model_id, self.started = live_dir, model_id, started
        self.run_name = self.receipts_path = None

    def __repr__(self):
        return "<the smoke's end-to-end test>"


def end_to_end_settings(env, env_path, live_dir, started):
    """The end-to-end test's settings: wolf's sender (WOLF_EMAIL, one of the two test inboxes, and
    WOLF_JMAP_TOKEN) and her loop's live settings (loop.settings_from, the model live: what her loop
    checks again when it starts). ConfigError naming the setting, never its value."""
    wolf = jmap.JmapSender(env, (env.get(WOLF_ADDRESS) or "").strip(), address_name=WOLF_ADDRESS,
                           token_name=WOLF_TOKEN)
    if wolf.from_addr.lower() not in SEND_TO_ALLOWED:
        raise live.ConfigError("%s must be one of the two test inboxes --send-to takes (her reply goes there)"
                               % WOLF_ADDRESS)
    settings = loop.settings_from(loop.her_env(env), "live")
    if wolf.from_addr.lower() in (settings.owner_addr.lower(), settings.doris_addr.lower()):
        raise live.ConfigError("%s must be the test account's own address, not hers or the owner's" % WOLF_ADDRESS)
    return _EndToEnd(wolf, settings.owner_addr, env_path, live_dir, settings.model_id, started)


def _request(wolf_addr, owner_addr, message_id, now):
    """wolf's meeting request to the owner: a call next week, no exact time (so she offers times and
    books nothing), plain text, as a person writes it (no Auto-Submitted: she answers no machine)."""
    msg = email.message.EmailMessage(policy=email.policy.SMTP)
    msg["From"] = wolf_addr
    msg["To"] = owner_addr
    msg["Subject"] = REQUEST_SUBJECT
    msg["Date"] = email.utils.format_datetime(now.astimezone(UTC).replace(microsecond=0))
    msg["Message-ID"] = message_id
    msg.set_content(REQUEST_TEXT)
    return msg.as_bytes()


def _new_folder(parent, base):
    """parent/base, made new (base-2, base-3... when it is taken)."""
    for n in range(1, 100):
        path = os.path.join(parent, base if n == 1 else "%s-%d" % (base, n))
        try:
            os.makedirs(path)
            return path
        except FileExistsError:
            continue
    raise FileExistsError("no free folder for %s" % (base,))


def _looks(out, name, look, waits, labels, found_text):
    """Up to two looks for one message, each after its wait: one line each, the second "not needed"
    when the first found it. look() -> a true answer when found. The answer, else None (and the
    second look's line FAILED)."""
    whys = []
    for n in (1, 2):
        step = "%s, look %d (%s)" % (name, n, labels[n - 1])
        if waits[n - 1]:
            _sleep(waits[n - 1])
        try:
            answer, why = look(), None
        except Exception as exc:
            answer, why = None, _why(exc)
        if answer:
            out.step(step, "ok (%s)" % (found_text(answer),))
            if n == 1:
                out.step("%s, look 2 (%s)" % (name, labels[1]), "not needed (found on look 1)")
            return answer
        whys.append(why)
        if n == 1:
            out.step(step, why or "not found yet")
        elif why:
            out.step(step, "%s; FAILED (not found)" % (why,), failed=True)
        else:
            out.step(step, "FAILED (not found on either look%s)" % ("" if whys[0] is None else "; look 1 could not tell"),
                     failed=True)
    return None


def _her_loop(e2e, folder, request_mid):
    """Her loop, live, on the smoke's one request: the summary, checked for one reply sent."""
    stamp = e2e.started.astimezone(UTC).strftime(livetrace.STAMP)
    e2e.run_name = "model-live-live-smoke-%s" % stamp
    run_dir = os.path.join(folder, "runs", e2e.run_name)
    e2e.receipts_path = os.path.join(run_dir, "receipts.jsonl")
    s = loop.run(loop.RunConfig(
        mode="live", run_name=e2e.run_name, state_dir=folder, env_path=e2e.env_path, model_source="live",
        record_path=os.path.join(run_dir, "recorded-answers.jsonl"), clock_mode="fixed", digest=False, cycles=1,
        live_dir=e2e.live_dir, smoke_message_id=request_mid, calendar_read_only=True))
    said = "%d handled, %d held, %s sent, %s, model cost $%.4f" % (
        s.handled, s.held, _count(s.sends, "reply"), _count(s.errors, "error line"), s.cost_usd)
    if s.halted:
        raise _Failed("halted: %s; %s" % (supervisor.CAUSE_WORDS.get(s.halted, s.halted), said))
    if s.read_failed or not s.verify_ok or s.handled != 1 or s.sends != 1 or s.errors:
        raise _Failed(said + ("" if s.verify_ok else "; her receipt chain does not verify"))
    return said


def _reply_mid(receipts_path):
    """The Message-ID of her one reply to the sender, from her receipts."""
    mids = [l["payload"].get("message_id") for l in receipts.read_lines(receipts_path)
            if l["kind"] == "called" and l["payload"].get("call") == "send" and l["payload"].get("to") == "sender"]
    if len(mids) != 1 or not isinstance(mids[0], str):
        raise _Failed("her receipts name %s to the sender" % _count(len(mids), "send"))
    return mids[0]


def _confirmed(ledger_path, mid):
    with ledger_mod.Ledger(ledger_path, create=False) as db:
        row = db.send(mid)
    if row is None:
        raise _Failed("her ledger has no record of it")
    if row["state"] != "confirmed":
        raise _Failed("her reply is %s, not confirmed in her Sent" % (row["state"],))


def _end_to_end(e2e, mail, out, file_request=False):
    """wolf's request to the owner; found in her INBOX (the owner's forward) and marked as the smoke's
    own; with file_request (the fetch step found no message to file), the file step on it; her loop on
    it; her reply confirmed in her Sent and found in wolf's mailbox. One line a step; once a step
    fails, the steps that need it are named in one line as not run (her loop needs no file step)."""
    now = _dt.datetime.now(UTC)
    request_mid = "<%s%s@%s>" % (live.SMOKE_MID_MARK, secrets.token_hex(16), e2e.wolf.from_addr.rsplit("@", 1)[-1])
    stamp = e2e.started.astimezone(UTC).strftime(livetrace.STAMP)
    box = {}

    def send():
        # Her throwaway ledger first, its start before the request exists: her loop reads mail from then.
        box["folder"] = _new_folder(os.path.join(e2e.live_dir, "smoke"), stamp)
        ledger_mod.start_fresh(os.path.join(box["folder"], "ledger.sqlite"), now - SMOKE_LEDGER_BACK)
        e2e.wolf.send(_request(e2e.wolf.from_addr, e2e.owner_addr, request_mid, now), [e2e.owner_addr], request_mid)
        box["sent_at"] = _clock()
        return "no exact time asked"
    if not _step(out, "wolf sends one meeting request to the owner", send):
        return _not_run(out, "test request in her INBOX and the steps after it", "wolf's send failed")
    found = _looks(out, "test request in her INBOX", lambda: mail.find_in_inbox(request_mid),
                   (FORWARD_WAIT_S, FORWARD_WAIT_S), ("after a wait", "after another wait"),
                   lambda ids: "found, %.1f s after wolf's send" % (_clock() - box["sent_at"]))
    if not found:
        return _not_run(out, "test request marked as the smoke's own and the steps after it",
                        "the request was not found in her INBOX")

    def mark():
        for server_id in found:
            mail.file(server_id, live.SMOKE_TEST_KEYWORD)
        return None if len(found) == 1 else "%d copies" % len(found)
    marked = _step(out, "test request marked as the smoke's own", mark)

    def file_it():
        mail.file(found[-1], FILE_KEYWORD)
        return "the test request"
    if file_request:
        _step(out, FILE_STEP, file_it)
    if not marked:
        return _not_run(out, "her loop on the test request and the steps after it",
                        "the request is not marked as the smoke's own, so no loop may read it")
    if not _step(out, "her loop on the test request", lambda: _her_loop(e2e, box["folder"], request_mid)):
        return _not_run(out, "her reply confirmed in her Sent and the steps after it", "her loop sent no reply")
    reply = {}

    def confirmed():
        reply["mid"] = _reply_mid(e2e.receipts_path)
        _confirmed(os.path.join(box["folder"], "ledger.sqlite"), reply["mid"])
    _step(out, "her reply confirmed in her Sent", confirmed)
    if "mid" not in reply:
        return _not_run(out, "her reply in wolf's mailbox", "her receipts name no reply")
    ended = _clock()
    _looks(out, "her reply in wolf's mailbox", lambda: e2e.wolf.find_message(reply["mid"]),
           (0.0, REPLY_WAIT_S), ("at once", "after a wait"),
           lambda roles: "found in %s, %.1f s after her loop ended" % (", ".join(roles), _clock() - ended))
    return None


# ------------------------------------------------------------------------------------------------
# The command
# ------------------------------------------------------------------------------------------------
def _parser():
    ap = argparse.ArgumentParser(prog="python -m doris smoke",
                                 description="Every live adapter call once, against the real servers.")
    ap.add_argument("--env", default=paths.LIVE_ENV, metavar="PATH", help="the env file (default %(default)s)")
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
    started = livetrace.now()
    term = livetrace.Printed(out=out or _print)      # every line printed, kept with its time, for the trace
    out = _Out(term)
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
    state = paths.state_dir("live")
    e2e, e2e_off = None, None
    try:
        hosts = live.hosts(env)
        mail = live.LiveMail(env, env.get(live.ENV_ADDRESS, ""))
        calendar = live.LiveCalendar(env)
        if send_to is None:
            e2e_off = "no --send-to"
        elif not any((env.get(name) or "").strip() for name in ENV_NAMES):
            e2e_off = "no %s and %s in the env file" % ENV_NAMES
        else:
            e2e = end_to_end_settings(env, os.path.abspath(args.env), state, started)
            hosts = list(hosts) + [h for h in loop.model_hosts() if h not in hosts]
    except live.ConfigError as exc:
        out("config: " + _why(exc))
        return 2
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
    contacted, finished = False, False
    try:
        record = sentrecord.path(state)
        try:
            os.remove(record)                  # an old record never outlives the smoke that follows it
        except FileNotFoundError:
            pass
        except OSError as exc:
            out("smoke: FAILED: the old sent-copy record cannot be removed (%s)" % type(exc).__name__)
            return 1
        guard = netguard.install(hosts)
        try:
            contacted = True
            ok, not_run = check(mail, calendar, out, send_to=send_to, now=started, record=record, e2e=e2e,
                                e2e_off=e2e_off)
        finally:
            guard.uninstall()
        finished = True
    finally:
        lock.release()
        if contacted and not finished:         # cut short (Ctrl-C): the trace is kept all the same
            _keep_trace(term, started, "fail", "cut short after the step: %s" % (out.last,), e2e)
    refused = guard.refused_count()
    if refused:
        out.step("network guard", "FAILED (%s to a host the adapters do not name)" % _count(refused, "connection"),
                 failed=True)
        ok = False
    verdict = "FAILED" if not ok else "every step that ran passed" if not_run else "ok, every step passed"
    out("smoke: " + verdict + ("; not run: " + "; ".join(not_run) if not_run else ""))
    if ok and not not_run:
        _keep_trace(term, started, "pass", None, e2e)
    else:
        _keep_trace(term, started, "fail", out.failed[0] if out.failed else "not run: %s" % (not_run[0],), e2e)
    return 0 if ok else 1


def _keep_trace(term, started, outcome, first_failing, e2e):
    """traces/live-smoke-<stamp>/ (livetrace): her loop's receipts when it ran, the manifest, the
    timeline and every line printed. A trace that cannot be written is said, loudly, never raised."""
    try:
        livetrace.write("smoke", started, livetrace.now(), outcome, first_failing, printed=term,
                        model_id=e2e.model_id if e2e is not None else None,
                        receipts_path=e2e.receipts_path if e2e is not None else None,
                        extra={"run_name": e2e.run_name if e2e is not None else None})
    except Exception as exc:
        term("Live trace NOT kept: %s." % type(exc).__name__)


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
