"""The sandbox adapters: real .eml and .ics files on disk, the same interface and the same
failure modes as the live adapters. A failure raises; it is never read as empty, free or sent.

Layout (CONTRACTS.md): root/inbox (*.eml, facts.json), root/outbox (her sent mail: <n>-<template>.eml,
envelope.jsonl, keywords.json), root/calendar (one .ics per event, file name = UID + ".ics"),
root/clock.txt (the fixture's start instant, ISO UTC).

Faults (tests-and-judge-hours.md A.4) are armed per message with arm(name, **params) and cleared
with disarm(). Every exception a fault causes carries staged=True and fault=<name>; after each
call, last_call_fault names the fault that touched it (None when none did), and fault_log keeps
(call, fault) pairs, so receipts can label each one staged.
"""

import datetime as _dt
import email.parser
import email.utils
import json
import os
import re
import time
from typing import Dict, List, Optional

from .. import caps, ics, slots
from ..shapes import (AlreadyExists, CalendarError, CalendarTimeout, Event, InboundItem, MailError, NotHers,
                      SendUnknown)
from .base import CalendarAdapter, MailAdapter

UTC = _dt.timezone.utc
_DAY = _dt.timedelta(days=1)

CALENDAR_FAULTS = ("timeout-once", "timeout-twice", "read-fails", "slow-calendar", "lands-between",
                   "write-unknown-present", "write-unknown-absent", "lookup-fails", "remove-fails-once",
                   "remove-fails-always", "login-refused")
MAIL_FAULTS = ("sent-unconfirmed", "login-refused")
# A.4 faults that belong to the model, the budget or the process tests, not to an adapter.
OTHER_FAULTS = ("model-down", "model-garbage", "budget-cap", "kill-mid-call", "kill-after-write")

DEFAULT_SLOW_S = 5.0
# A .eml whose size or modified time changed in the last SETTLE_S seconds (a judge's copy still being
# written) waits for a later pass: a mail is known by its file name, so a half file read once is never
# read again whole.
SETTLE_S = 2.0
_wall = time.time                    # the clock files are dated by (tests move it)
DEFAULT_LANDS_MIN = 60
_MAX_PERIODS = 100000


def _staged(exc, fault, **extra):
    exc.staged = True
    exc.fault = fault
    for key, value in extra.items():
        setattr(exc, key, value)
    return exc


def _aware(value, what):
    if not isinstance(value, _dt.datetime) or value.tzinfo is None:
        raise ValueError("%s must be a timezone-aware datetime" % what)
    return value.astimezone(UTC)


def _moment(value, what):
    """A fault parameter instant: an aware datetime, or an ISO string as written in facts.json
    ("2026-12-07T18:00:00Z"; one with no offset is read as UTC)."""
    if isinstance(value, str):
        try:
            value = _dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("%s is not an ISO time: %r" % (what, value))
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
    return _aware(value, what)


def arm_fault(name, mail=None, calendar=None, **params):
    """Arm one A.4 fault on the adapter(s) it belongs to; returns the adapters armed.
    login-refused arms both: mail and calendar share her one account. Model, budget and
    process faults arm nothing here. An unknown name raises ValueError."""
    armed = []
    if name in MAIL_FAULTS and mail is not None:
        mail.arm(name, **params)
        armed.append(mail)
    if name in CALENDAR_FAULTS and calendar is not None:
        calendar.arm(name, **params)
        armed.append(calendar)
    if name not in MAIL_FAULTS and name not in CALENDAR_FAULTS and name not in OTHER_FAULTS:
        raise ValueError("unknown fault %r" % (name,))
    return armed


def disarm_all(*adapters):
    for adapter in adapters:
        if adapter is not None:
            adapter.disarm()


class _Faults(object):
    """Per-message fault state shared by both adapters."""

    known = ()
    params_for = {}    # fault -> the parameter names it takes

    def _init_faults(self):
        self.fault = None
        self.fault_params = {}
        self.last_call_fault = None
        self.fault_log = []
        self._counts = {}

    def arm(self, name, **params):
        """Arm one fault for the message about to run. Parameters may be written as strings, as
        they come from facts.json; an unknown fault, parameter or value raises ValueError."""
        if name not in self.known:
            raise ValueError("%s has no fault %r" % (type(self).__name__, name))
        unknown = set(params) - set(self.params_for.get(name, ()))
        if unknown:
            raise ValueError("fault %s takes no parameter %s" % (name, ", ".join(sorted(unknown))))
        for key, value in params.items():
            if key in ("start", "end"):
                _moment(value, key)
            elif key in ("seconds", "minutes", "sends"):
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    raise ValueError("fault parameter %s must be a number, not %r" % (key, value))
                if not number >= 0 or (key in ("minutes", "sends") and not number > 0):
                    raise ValueError("fault parameter %s is out of range: %r" % (key, value))
        if params.get("start") and params.get("end") and _moment(params["end"], "end") <= _moment(params["start"], "start"):
            raise ValueError("fault %s: end must be after start" % name)
        self.fault = name
        self.fault_params = dict(params)
        self._counts = {}

    def disarm(self):
        self.fault = None
        self.fault_params = {}
        self._counts = {}

    def _count(self, key):
        self._counts[key] = self._counts.get(key, 0) + 1
        return self._counts[key]

    def _mark(self, call):
        self.last_call_fault = self.fault
        self.fault_log.append((call, self.fault))


# ---------------------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------------------
class _CannotExpand(Exception):
    pass


_WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
_RRULE_KEYS = ("FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY", "WKST")


def _parse_rrule(text):
    parts = {}
    for piece in text.split(";"):
        if not piece.strip():
            continue
        key, sep, value = piece.partition("=")
        key = key.strip().upper()
        if not sep or key in parts or key not in _RRULE_KEYS:
            raise _CannotExpand(text)
        parts[key] = value.strip().upper()
    freq = parts.get("FREQ")
    if freq not in ("DAILY", "WEEKLY"):
        raise _CannotExpand(text)
    if "COUNT" in parts and "UNTIL" in parts:
        raise _CannotExpand(text)

    def positive(key):
        value = parts.get(key)
        if value is None:
            return None
        if not value.isdigit() or int(value) < 1:
            raise _CannotExpand(text)
        return int(value)

    interval = positive("INTERVAL") or 1
    count = positive("COUNT")
    until = None
    if "UNTIL" in parts:
        try:
            until = ics._when({}, parts["UNTIL"])
        except ValueError:
            raise _CannotExpand(text)
    byday = None
    if "BYDAY" in parts:
        codes = parts["BYDAY"].split(",")
        if not all(code in _WEEKDAYS for code in codes):
            raise _CannotExpand(text)   # a numbered day (1MO, -1FR) needs MONTHLY or YEARLY
        byday = {_WEEKDAYS[code] for code in codes}
    wkst = _WEEKDAYS.get(parts.get("WKST", "MO"))
    if wkst is None:
        raise _CannotExpand(text)
    return freq, interval, count, until, byday, wkst


def _within_until(occ, until):
    """occ is in the event's own kind; generous where the two kinds differ (more busy, never less)."""
    if until is None:
        return True
    slack = _dt.timedelta(hours=14)
    if isinstance(until, _dt.datetime):
        if isinstance(occ, _dt.datetime):
            if (occ.tzinfo is None) == (until.tzinfo is None):
                return occ <= until
            return occ.replace(tzinfo=None) <= until.replace(tzinfo=None) + slack
        return occ <= until.date() + _DAY
    # a DATE until
    if isinstance(occ, _dt.datetime):
        return occ.date() <= until + _DAY
    return occ <= until


def _expand(v, span_start, span_end):
    """Occurrence starts of a DAILY or WEEKLY rule, in the event's own kind, from DTSTART
    up to the end of the span. Raises _CannotExpand for anything else."""
    freq, interval, count, until, byday, wkst = _parse_rrule(v.rrule)
    start = v.start
    if isinstance(start, _dt.datetime):
        base = start.date()
        if start.tzinfo is not None:
            limit = span_end
        else:
            limit = span_end.replace(tzinfo=None) + 2 * _DAY

        def make(day):
            return _dt.datetime.combine(day, start.timetz())
    else:
        base = start
        limit = span_end.date() + 2 * _DAY

        def make(day):
            return day

    produced = 0
    if freq == "WEEKLY":
        days = byday or {base.weekday()}
        if base.weekday() not in days:
            yield start          # DTSTART is always an occurrence; it does not use up COUNT
        order = sorted(days, key=lambda wd: (wd - wkst) % 7)
        week0 = base - _dt.timedelta(days=(base.weekday() - wkst) % 7)
        for k in range(_MAX_PERIODS):
            week = week0 + _dt.timedelta(weeks=k * interval)
            for wd in order:
                day = week + _dt.timedelta(days=(wd - wkst) % 7)
                if day < base:
                    continue
                occ = make(day)
                if occ >= limit or not _within_until(occ, until):
                    return
                yield occ
                produced += 1
                if count is not None and produced >= count:
                    return
        raise _CannotExpand(v.rrule)

    for k in range(_MAX_PERIODS):
        day = base + _dt.timedelta(days=k * interval)
        occ = make(day)
        if occ >= limit or not _within_until(occ, until):
            return
        if byday is None or day.weekday() in byday:
            yield occ
            produced += 1
            if count is not None and produced >= count:
                return
        elif k == 0:
            yield occ            # DTSTART outside BYDAY: still an occurrence, not counted
    raise _CannotExpand(v.rrule)


def _touches(ev, span_start, span_end):
    window = slots._busy_window(ev)
    if window is None:
        return True
    start, end = window
    if start == end:
        return span_start <= start < span_end
    return start < span_end and end > span_start


class SandboxCalendar(_Faults, CalendarAdapter):
    """CalendarAdapter over root/calendar: one .ics per event, file name = UID + ".ics".

    Reads UID, times, STATUS and TRANSP only, never a title. Writes create-only, and only her
    own fixed-field events; deletes only her own events by booking ID (the doris- prefix, and the
    file's UID must match).
    """

    known = CALENDAR_FAULTS
    params_for = {
        "timeout-once": ("seconds",), "timeout-twice": ("seconds",), "slow-calendar": ("seconds",),
        "write-unknown-present": ("seconds",), "write-unknown-absent": ("seconds",), "lookup-fails": ("seconds",),
        "lands-between": ("start", "end", "minutes"),
    }

    def __init__(self, root, timeout_s=1.5, sleep=None):
        self.root = root
        self.folder = os.path.join(root, "calendar")
        self.timeout_s = timeout_s
        self._sleep = sleep or time.sleep
        self._init_faults()

    # -- faults ---------------------------------------------------------------------------
    def _enter(self, call):
        """Apply the armed fault at the start of a call (after local checks)."""
        self.last_call_fault = None
        fault = self.fault
        if fault is None:
            return
        n = self._count("call")
        if fault == "login-refused":
            self._mark(call)
            raise _staged(CalendarError("staged: calendar login refused"), fault)
        if fault in ("timeout-once", "timeout-twice") and n <= (1 if fault == "timeout-once" else 2):
            self._mark(call)
            self._timeout(fault, call)
        if fault == "slow-calendar":
            seconds = float(self.fault_params.get("seconds", DEFAULT_SLOW_S))
            self._mark(call)
            if seconds >= caps.CALENDAR_TIMEOUT_S:
                self._timeout(fault, call, caps.CALENDAR_TIMEOUT_S)
            self._sleep(seconds)

    def _timeout(self, fault, call, seconds=None):
        if seconds is None:
            seconds = float(self.fault_params.get("seconds", self.timeout_s))
        self._sleep(seconds)
        raise _staged(CalendarTimeout("staged: calendar %s timed out after %.2f s" % (call, seconds)),
                      fault, waited_s=seconds)

    # -- files ----------------------------------------------------------------------------
    def _path(self, booking_id):
        if not os.path.isdir(self.folder):
            raise CalendarError("the sandbox calendar folder is missing: %s" % self.folder)
        return os.path.join(self.folder, booking_id + ".ics")

    def _names(self):
        if not os.path.isdir(self.folder):
            raise CalendarError("the sandbox calendar folder is missing: %s" % self.folder)
        try:
            names = os.listdir(self.folder)
        except OSError as exc:
            raise CalendarError("calendar read failed: %s" % exc)
        return sorted(n for n in names
                      if n.lower().endswith(".ics") and os.path.isfile(os.path.join(self.folder, n)))

    def _read(self, path):
        try:
            with open(path, "rb") as fh:
                return fh.read()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise CalendarError("calendar read failed: %s" % exc)

    def _write_new(self, path, data):
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        try:
            fd = os.open(path, flags, 0o644)
        except FileExistsError:
            raise AlreadyExists("booking ID already on the calendar: %s" % os.path.basename(path))
        except OSError as exc:
            raise CalendarError("calendar write failed: %s" % exc)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            raise CalendarError("calendar write failed: %s" % exc)

    # -- reads ----------------------------------------------------------------------------
    def query(self, start_utc, end_utc) -> List[Event]:
        """Every event overlapping [start, end), repeats expanded, cancelled ones included (with
        their status). An event or file it cannot read or expand is busy over the whole span."""
        start, end = _aware(start_utc, "start_utc"), _aware(end_utc, "end_utc")
        if end <= start:
            raise ValueError("the query span runs forward")
        self._enter("query")
        if self.fault == "read-fails":
            self._mark("query")
            raise _staged(CalendarError("staged: calendar read failed"), self.fault)
        found = []
        for name in self._names():
            raw = self._read(os.path.join(self.folder, name))
            if raw is None:
                continue
            try:
                vevents = ics.read_events(raw)
            except ValueError:
                found.append(self._whole_span(name[:-4], "", "", start, end))
                continue
            for v in vevents:
                found.extend(self._occurrences(v, start, end))
        found.sort(key=lambda e: (e.start_utc or start, e.uid))
        return found

    @staticmethod
    def _whole_span(uid, status, transp, start, end):
        return Event(uid=uid, status=status, transp=transp, kind="timed", start_utc=start, end_utc=end)

    def _occurrences(self, v, start, end):
        whole = [self._whole_span(v.uid, v.status, v.transp, start, end)]
        if v.rdate:
            return whole
        if v.rrule is None:
            ev = ics.to_event(v)
            return [ev] if _touches(ev, start, end) else []
        length = v.end - v.start
        try:
            starts = list(_expand(v, start, end))
        except _CannotExpand:
            return whole
        # Occurrences that end well before the span need no closer look (two days covers any zone).
        if isinstance(v.start, _dt.datetime):
            floor = start - 2 * _DAY if v.start.tzinfo is not None else start.replace(tzinfo=None) - 2 * _DAY
        else:
            floor = start.date() - 2 * _DAY
        out = []
        for occ in starts:
            if occ + length < floor:
                continue
            ev = ics.to_event(v, occ, occ + length)
            if _touches(ev, start, end):
                out.append(ev)
        return out

    def get(self, booking_id) -> Optional[Event]:
        """Her event by booking ID; None means confirmed absent. Raises on any failure."""
        if not ics.is_booking_id(booking_id):
            raise NotHers("not one of her booking IDs: %r" % (booking_id,))
        self._enter("get")
        if self.fault == "lookup-fails":
            self._mark("get")
            raise _staged(CalendarError("staged: lookup by booking ID failed"), self.fault)
        raw = self._read(self._path(booking_id))
        if raw is None:
            return None
        try:
            ev = ics.read_event(raw)
        except ValueError:
            raise CalendarError("her event %s cannot be read" % booking_id)
        if ev.uid != booking_id:
            raise CalendarError("the event filed as %s carries another UID" % booking_id)
        return ev

    # -- writes ---------------------------------------------------------------------------
    def create_only(self, booking_id, ics_bytes) -> None:
        """Create her event under its booking ID. AlreadyExists if the ID is there already;
        CalendarTimeout when the result is unknown. Refuses anything not hers or outside the
        fixed fields (G13) before any call."""
        if not ics.is_booking_id(booking_id):
            raise NotHers("not one of her booking IDs: %r" % (booking_id,))
        if not ics.fields_ok(ics_bytes, booking_id=booking_id):
            raise CalendarError("refused: the .ics is not her fixed form for %s" % booking_id)
        if b"\r\nMETHOD:" in bytes(ics_bytes):
            # A calendar resource carries no METHOD (RFC 4791 4.1); that line is for the attachment.
            raise CalendarError("refused: the calendar write carries no METHOD line")
        self._enter("create")
        fault = self.fault
        first = fault is not None and self._count("create") == 1
        path = self._path(booking_id)
        if os.path.exists(path):
            raise AlreadyExists("booking ID already on the calendar: %s" % booking_id)
        if fault == "lands-between" and first:
            self._land(ics.read_event(ics_bytes))
            self._mark("create")
        if fault == "write-unknown-absent" and first:
            self._mark("create")
            self._timeout(fault, "create")
        self._write_new(path, bytes(ics_bytes))
        if fault in ("write-unknown-present", "lookup-fails") and first:
            self._mark("create")
            self._timeout(fault, "create")

    def _land(self, hers):
        """Fault lands-between: another event (not hers) appears on the calendar now."""
        params = self.fault_params
        start = _moment(params["start"], "start") if params.get("start") else hers.start_utc
        if params.get("end"):
            end = _moment(params["end"], "end")
        else:
            end = start + _dt.timedelta(minutes=float(params.get("minutes", DEFAULT_LANDS_MIN)))
        n = 1
        while os.path.exists(os.path.join(self.folder, "staged-lands-between-%d.ics" % n)):
            n += 1
        uid = "staged-lands-between-%d" % n
        fmt = "%Y%m%dT%H%M%SZ"
        lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Doris sandbox//staged fault//EN",
                 "BEGIN:VEVENT", "UID:" + uid, "DTSTAMP:" + start.strftime(fmt),
                 "DTSTART:" + start.strftime(fmt), "DTEND:" + end.strftime(fmt),
                 "SUMMARY:Staged: an event lands between her check and her write",
                 "STATUS:CONFIRMED", "TRANSP:OPAQUE", "END:VEVENT", "END:VCALENDAR"]
        self._write_new(os.path.join(self.folder, uid + ".ics"), ("\r\n".join(lines) + "\r\n").encode("ascii"))

    def delete(self, booking_id) -> None:
        """Delete her own event by booking ID. Refuses any ID not starting with doris-, and a file
        whose UID is not that ID. An ID already absent is a no-op (the re-read confirms it)."""
        if not ics.is_booking_id(booking_id):
            raise NotHers("not one of her booking IDs: %r" % (booking_id,))
        self._enter("delete")
        fault = self.fault
        if fault == "remove-fails-always" or (fault == "remove-fails-once" and self._count("delete") == 1):
            self._mark("delete")
            raise _staged(CalendarError("staged: delete failed"), fault)
        path = self._path(booking_id)
        raw = self._read(path)
        if raw is None:
            return
        try:
            uids = {v.uid for v in ics.read_events(raw)}
        except ValueError:
            raise CalendarError("cannot read %s to confirm it is hers; not deleted" % booking_id)
        if uids != {booking_id}:
            raise NotHers("the event filed as %s is not hers; not deleted" % booking_id)
        try:
            os.remove(path)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise CalendarError("delete failed: %s" % exc)


# ---------------------------------------------------------------------------------------
# Mail
# ---------------------------------------------------------------------------------------
_KEYWORD = re.compile(r"^[A-Za-z0-9$_-]{1,64}$")
_TEMPLATE = re.compile(r"[^A-Za-z0-9_-]")


def _norm_mid(value) -> str:
    return str(value or "").strip().strip("<>").strip()


def _headers(raw):
    return email.parser.BytesHeaderParser().parsebytes(raw)


def _plain_name(name) -> bool:
    return (isinstance(name, str) and name not in ("", ".", "..") and "/" not in name and "\\" not in name
            and os.path.basename(name) == name)


class SandboxMail(_Faults, MailAdapter):
    """MailAdapter over root/inbox and root/outbox (her Sent folder). Never deletes or moves."""

    known = MAIL_FAULTS
    params_for = {"sent-unconfirmed": ("sends",)}

    def __init__(self, root, doris_addr, clock=None):
        self.root = root
        self.doris_addr = doris_addr
        self.clock = clock
        self.inbox = os.path.join(root, "inbox")
        self.outbox = os.path.join(root, "outbox")
        self._hidden = set()
        self._fixture_clock = None
        self._looks = None           # name -> ((size, mtime_ns), when it last changed), from her last look
        self._init_faults()

    def _enter(self, call):
        self.last_call_fault = None
        if self.fault == "login-refused":
            self._mark(call)
            raise _staged(MailError("staged: mail login refused"), self.fault)

    # -- fetch ----------------------------------------------------------------------------
    def _facts(self) -> Dict[str, Dict[str, str]]:
        path = os.path.join(self.inbox, "facts.json")
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "rb") as fh:
                data = json.loads(fh.read().decode("utf-8"))
        except (OSError, ValueError) as exc:
            raise MailError("inbox/facts.json cannot be read: %s" % exc)
        ok = isinstance(data, dict) and all(
            isinstance(k, str) and isinstance(v, dict)
            and all(isinstance(a, str) and isinstance(b, str) for a, b in v.items())
            for k, v in data.items())
        if not ok:
            raise MailError("inbox/facts.json must map file names to string facts")
        return data

    def _clock_txt(self):
        if self._fixture_clock is None:
            path = os.path.join(self.root, "clock.txt")
            try:
                with open(path, "r") as fh:
                    text = fh.read().strip()
                moment = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
            except (OSError, ValueError) as exc:
                raise MailError("no Date header and clock.txt cannot be read: %s" % exc)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=UTC)
            self._fixture_clock = moment.astimezone(UTC)
        return self._fixture_clock

    def _received(self, raw):
        try:
            value = _headers(raw).get("Date")
            moment = email.utils.parsedate_to_datetime(str(value)) if value is not None else None
        except (TypeError, ValueError, IndexError):
            moment = None
        if moment is None:
            return self._clock_txt()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return moment.astimezone(UTC)

    def _inbox(self):
        """(file name, raw bytes, received_utc) for every .eml, oldest first by file name, less any
        whose size or modified time changed in the last SETTLE_S seconds (it is read next pass). What
        is there at her first look is read as it is; after that a file's own modified time says when
        it was written, and a size that changed under the same modified time says now."""
        if not os.path.isdir(self.inbox):
            raise MailError("the sandbox inbox is missing: %s" % self.inbox)
        try:
            names = sorted(n for n in os.listdir(self.inbox)
                           if n.lower().endswith(".eml") and os.path.isfile(os.path.join(self.inbox, n)))
            now, before, looks, out = _wall(), self._looks, {}, []
            for name in names:
                path = os.path.join(self.inbox, name)
                st = os.stat(path)
                sig = (st.st_size, st.st_mtime_ns)
                last = before.get(name) if before is not None else None
                if before is None:
                    changed = None
                elif last is not None and last[0] == sig:
                    changed = last[1]
                elif last is not None and last[0][1] == sig[1]:
                    changed = now
                else:
                    changed = min(st.st_mtime, now)
                looks[name] = (sig, changed)
                if changed is not None and now - changed < SETTLE_S:
                    continue
                with open(path, "rb") as fh:
                    raw = fh.read()
                out.append((name, raw, self._received(raw)))
            self._looks = looks
            return out
        except OSError as exc:
            raise MailError("inbox read failed: %s" % exc)

    def fetch_new(self) -> List[InboundItem]:
        """Every .eml in the inbox (received at or before clock(), when a clock is set), oldest
        first by file name. The ledger decides which ones are new (server_id_seen)."""
        self._enter("mail_fetch")
        facts = self._facts() if os.path.isdir(self.inbox) else {}
        now = _aware(self.clock(), "clock()") if self.clock is not None else None
        return [InboundItem(server_id=name, raw=raw, received_utc=received, facts=dict(facts.get(name, {})))
                for name, raw, received in self._inbox() if now is None or received <= now]

    def next_arrival(self) -> Optional[_dt.datetime]:
        """The received_utc of the earliest .eml received after clock(): the next instant the
        demo's simulated clock should reach. None when nothing is pending or there is no clock."""
        if self.clock is None:
            return None
        now = _aware(self.clock(), "clock()")
        later = [received for _, _, received in self._inbox() if received > now]
        return min(later) if later else None

    # -- send -----------------------------------------------------------------------------
    def send(self, raw, envelope_to, message_id, template=None) -> None:
        """Hand one composed message to the 'server': it lands in the outbox (her Sent folder) as
        <n>-<template>.eml, and its envelope goes to outbox/envelope.jsonl. Refuses a message
        whose Message-ID or From is not what she says it is."""
        self._enter("send")
        if not isinstance(raw, (bytes, bytearray)) or not raw:
            raise MailError("refused: nothing to send")
        if not envelope_to or not all(isinstance(a, str) and "@" in a for a in envelope_to):
            raise MailError("refused: no recipients")
        headers = _headers(bytes(raw))
        if not _norm_mid(message_id) or _norm_mid(headers.get("Message-ID")) != _norm_mid(message_id):
            raise MailError("refused: the message's Message-ID is not the one recorded")
        sender = email.utils.parseaddr(str(headers.get("From") or ""))[1].lower()
        if sender != (self.doris_addr or "").lower():
            raise MailError("refused: From is not her address")
        name = None
        try:
            os.makedirs(self.outbox, exist_ok=True)
            name, fd = self._next_file(template)
            with os.fdopen(fd, "wb") as fh:
                fh.write(bytes(raw))
                fh.flush()
                os.fsync(fh.fileno())
            with open(os.path.join(self.outbox, "envelope.jsonl"), "ab") as fh:
                row = {"file": name, "message_id": str(message_id), "envelope_to": list(envelope_to)}
                fh.write(json.dumps(row, sort_keys=True).encode("ascii") + b"\n")
        except OSError as exc:
            if name is not None:     # it may be in Sent: look there, never resend blind
                raise SendUnknown("send outcome unknown: %s" % exc)
            raise MailError("send failed: %s" % exc)
        if self.fault == "sent-unconfirmed" and self._count("send") <= float(self.fault_params.get("sends", 1)):
            self._hidden.add(_norm_mid(message_id))
            self._mark("send")

    def _next_file(self, template):
        tag = _TEMPLATE.sub("", str(template)) if template else "mail"
        tag = tag or "mail"
        taken = [int(n.split("-", 1)[0]) for n in os.listdir(self.outbox)
                 if n.lower().endswith(".eml") and n.split("-", 1)[0].isdigit()]
        n = max(taken) + 1 if taken else 1
        while True:
            name = "%03d-%s.eml" % (n, tag)
            try:
                fd = os.open(os.path.join(self.outbox, name),
                             os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o644)
            except FileExistsError:
                n += 1
                continue
            return name, fd

    def find_in_sent(self, message_id) -> bool:
        """True when her Sent folder (the outbox) holds a message with this Message-ID."""
        self._enter("sent_lookup")
        target = _norm_mid(message_id)
        if target and target in self._hidden:
            self.last_call_fault = "sent-unconfirmed"
            self.fault_log.append(("sent_lookup", "sent-unconfirmed"))
            return False
        if not target or not os.path.isdir(self.outbox):
            return False
        try:
            names = sorted(n for n in os.listdir(self.outbox) if n.lower().endswith(".eml"))
            for name in names:
                with open(os.path.join(self.outbox, name), "rb") as fh:
                    if _norm_mid(_headers(fh.read()).get("Message-ID")) == target:
                        return True
        except OSError as exc:
            raise MailError("Sent lookup failed: %s" % exc)
        return False

    # -- file -----------------------------------------------------------------------------
    def file(self, server_id, keyword) -> None:
        """Mark her copy with a keyword in outbox/keywords.json. Never moves or deletes mail."""
        self._enter("mail_file")
        if not isinstance(keyword, str) or not _KEYWORD.match(keyword):
            raise ValueError("a keyword is one word of letters, digits, - or _")
        if not _plain_name(server_id) or not os.path.isfile(os.path.join(self.inbox, server_id)):
            raise MailError("no such message in the inbox: %r" % (server_id,))
        path = os.path.join(self.outbox, "keywords.json")
        try:
            os.makedirs(self.outbox, exist_ok=True)
            data = {}
            if os.path.exists(path):
                with open(path, "rb") as fh:
                    data = json.loads(fh.read().decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("keywords.json is not an object")
            words = set(data.get(server_id, []))
            words.add(keyword)
            data[server_id] = sorted(words)
            tmp = path + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(json.dumps(data, sort_keys=True, indent=1).encode("ascii"))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except (OSError, ValueError) as exc:
            raise MailError("filing failed: %s" % exc)
