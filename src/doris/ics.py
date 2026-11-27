"""Her .ics (ruling 10) and reading any event as times and status only (ruling 3).

build_event writes only the fixed fields: start and end in UTC, the fixed title, her booking ID,
the meeting link as the location, a timestamp, a sequence number, a status (confirmed or
cancelled) and, for an attachment only, the METHOD line. No organizer, no attendees, no
description, nothing the sender wrote. fields_ok (G13) accepts exactly that form and nothing
else. read_event / read_events read UID, DTSTART, DTEND, DURATION, STATUS, TRANSP and the repeat
fields of any event; a title, description, location or attendee is never read, kept or returned.
"""

import datetime as _dt
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .shapes import Event, Slot

UTC = _dt.timezone.utc
_DAY = _dt.timedelta(days=1)

TITLE = "Call with Shane Bornstein (Hire the Machines)"
PRODID = "-//Hire the Machines//Doris//EN"
BOOKING_PREFIX = "doris-"
STATUSES = ("CONFIRMED", "CANCELLED")
# PUBLISH: a plain entry the prospect can add, no RSVP. CANCEL: the cancel .ics (T10, T18).
# None: the calendar write itself (a CalDAV resource carries no METHOD).
METHODS = ("PUBLISH", "CANCEL")
MAX_LINE_OCTETS = 75
MAX_LINK_CHARS = 500

_CAL_FIELDS = ("VERSION", "PRODID", "METHOD")
_EVENT_FIELDS = ("UID", "DTSTAMP", "DTSTART", "DTEND", "SUMMARY", "LOCATION", "STATUS", "SEQUENCE")
_BOOKING_ID = re.compile(r"^doris-[A-Za-z0-9][A-Za-z0-9._@-]{0,190}$")
_UTC_STAMP = re.compile(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$")
_SEQUENCE = re.compile(r"^\d{1,9}$")
_NAME = re.compile(r"^[A-Z][A-Z0-9-]*$")


def is_booking_id(value) -> bool:
    """Her booking IDs: the doris- prefix and a file-name-safe tail (it is also the file name)."""
    return isinstance(value, str) and bool(_BOOKING_ID.match(value)) and ".." not in value


# ---------------------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------------------
def _utc(value, what):
    if not isinstance(value, _dt.datetime) or value.tzinfo is None:
        raise ValueError("%s must be a timezone-aware datetime" % what)
    return value.astimezone(UTC)


def _stamp(value) -> str:
    return value.strftime("%Y%m%dT%H%M%SZ")


def _link_ok(link) -> bool:
    return (isinstance(link, str) and 0 < len(link) <= MAX_LINK_CHARS
            and all(0x20 <= ord(c) <= 0x7E for c in link))


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")


def _fold(line: str) -> bytes:
    data = line.encode("ascii")
    parts = [data[:MAX_LINE_OCTETS]]
    rest = data[MAX_LINE_OCTETS:]
    while rest:
        parts.append(b" " + rest[:MAX_LINE_OCTETS - 1])
        rest = rest[MAX_LINE_OCTETS - 1:]
    return b"\r\n".join(parts) + b"\r\n"


def build_event(booking_id: str, slot: Slot, meeting_link: str, status: str, sequence: int,
                dtstamp: _dt.datetime, method: Optional[str]) -> bytes:
    """Her event as .ics bytes: the fixed fields only, CRLF, folded at 75 octets.

    method=None for the calendar write; "PUBLISH" (a plain entry) or "CANCEL" (with status
    CANCELLED) for the attachment. Raises ValueError on anything outside the fixed form.
    """
    if not is_booking_id(booking_id):
        raise ValueError("not one of her booking IDs: %r" % (booking_id,))
    start = _utc(slot.start_utc, "slot start")
    end = _utc(slot.end_utc, "slot end")
    if start.second or start.microsecond or end.second or end.microsecond or end <= start:
        raise ValueError("a slot runs forward on whole minutes")
    if not _link_ok(meeting_link):
        raise ValueError("the meeting link must be one line of printable ASCII")
    if not isinstance(status, str) or status.upper() not in STATUSES:
        raise ValueError("status is CONFIRMED or CANCELLED")
    status = status.upper()
    if isinstance(sequence, bool) or not isinstance(sequence, int) or not 0 <= sequence <= 999999999:
        raise ValueError("sequence is a whole number from 0")
    stamp = _utc(dtstamp, "dtstamp").replace(microsecond=0)
    if method is not None:
        if not isinstance(method, str) or method.upper() not in METHODS:
            raise ValueError("method is PUBLISH, CANCEL or None")
        method = method.upper()
        if method == "CANCEL" and status != "CANCELLED":
            raise ValueError("a CANCEL .ics carries STATUS:CANCELLED")

    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:" + PRODID]
    if method:
        lines.append("METHOD:" + method)
    lines += [
        "BEGIN:VEVENT",
        "UID:" + booking_id,
        "DTSTAMP:" + _stamp(stamp),
        "DTSTART:" + _stamp(start),
        "DTEND:" + _stamp(end),
        "SUMMARY:" + _escape(TITLE),
        "LOCATION:" + _escape(meeting_link),
        "STATUS:" + status,
        "SEQUENCE:%d" % sequence,
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    raw = b"".join(_fold(line) for line in lines)
    if not fields_ok(raw, meeting_link=meeting_link, booking_id=booking_id, slot=Slot(start, end)):
        raise ValueError("built .ics failed its own field check")
    return raw


# ---------------------------------------------------------------------------------------
# G13: exactly the fixed fields, nothing else
# ---------------------------------------------------------------------------------------
def _strict_props(lines, allowed) -> Optional[Dict[str, str]]:
    props = {}
    for line in lines:
        name, sep, value = line.partition(":")
        if not sep or not _NAME.match(name) or name not in allowed or name in props:
            return None
        props[name] = value
    return props


def _strict_stamp(value) -> Optional[_dt.datetime]:
    m = _UTC_STAMP.match(value)
    if not m:
        return None
    return _dt.datetime(*(int(g) for g in m.groups()), tzinfo=UTC)


def _strict_unescape(value) -> Optional[str]:
    out, i = [], 0
    while i < len(value):
        c = value[i]
        if c == "\\":
            if i + 1 < len(value) and value[i + 1] in "\\;,":
                out.append(value[i + 1])
                i += 2
                continue
            return None
        if c in ";,":
            return None
        out.append(c)
        i += 1
    return "".join(out)


def _fields_ok(ics_bytes, meeting_link, booking_id, slot) -> bool:
    if not isinstance(ics_bytes, (bytes, bytearray)):
        return False
    try:
        text = bytes(ics_bytes).decode("ascii")
    except UnicodeDecodeError:
        return False
    if not text.endswith("\r\n"):
        return False
    logical = []
    for line in text[:-2].split("\r\n"):
        if not line or len(line) > MAX_LINE_OCTETS or any(ord(c) < 0x20 or ord(c) == 0x7F for c in line):
            return False
        if line[0] == " ":
            if not logical:
                return False
            logical[-1] += line[1:]
        else:
            logical.append(line)
    if len(logical) < 4 or logical[0] != "BEGIN:VCALENDAR" or logical[-1] != "END:VCALENDAR":
        return False
    body = logical[1:-1]
    if body.count("BEGIN:VEVENT") != 1 or body[-1] != "END:VEVENT":
        return False
    split = body.index("BEGIN:VEVENT")
    cal = _strict_props(body[:split], _CAL_FIELDS)
    ev = _strict_props(body[split + 1:-1], _EVENT_FIELDS)
    if cal is None or ev is None:
        return False
    if set(cal) - {"METHOD"} != {"VERSION", "PRODID"} or set(ev) != set(_EVENT_FIELDS):
        return False
    if cal["VERSION"] != "2.0" or cal["PRODID"] != PRODID:
        return False
    method = cal.get("METHOD")
    if method is not None and method not in METHODS:
        return False
    if not is_booking_id(ev["UID"]) or ev["SUMMARY"] != TITLE:
        return False
    if ev["STATUS"] not in STATUSES or (method == "CANCEL" and ev["STATUS"] != "CANCELLED"):
        return False
    if not _SEQUENCE.match(ev["SEQUENCE"]):
        return False
    start, end, stamp = (_strict_stamp(ev[k]) for k in ("DTSTART", "DTEND", "DTSTAMP"))
    if start is None or end is None or stamp is None or end <= start:
        return False
    link = _strict_unescape(ev["LOCATION"])
    if link is None or not _link_ok(link):
        return False
    if meeting_link is not None and link != meeting_link:
        return False
    if booking_id is not None and ev["UID"] != booking_id:
        return False
    if slot is not None and (start != _utc(slot.start_utc, "slot start") or end != _utc(slot.end_utc, "slot end")):
        return False
    return True


def fields_ok(ics_bytes, meeting_link: Optional[str] = None, booking_id: Optional[str] = None,
              slot: Optional[Slot] = None) -> bool:
    """G13: True only for her fixed form (as build_event writes it): the fixed properties, each
    once, no parameters, one VEVENT and no other component, CRLF, lines of at most 75 octets,
    printable ASCII. With meeting_link, booking_id or slot, those values must match exactly.
    Never raises."""
    try:
        return _fields_ok(ics_bytes, meeting_link, booking_id, slot)
    except Exception:   # a check that cannot run fails closed
        return False


# ---------------------------------------------------------------------------------------
# Reading any event: times and status only
# ---------------------------------------------------------------------------------------
_READ = ("UID", "DTSTART", "DTEND", "DURATION", "STATUS", "TRANSP", "RRULE", "RDATE", "RECURRENCE-ID")
_TOKEN = re.compile(r"[A-Za-z0-9-]+")
_DATE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_DATETIME = re.compile(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(Z?)$")
_DURATION = re.compile(r"^([+-])?P(?:(\d+)W|(?=\d|T\d)(?:(\d+)D)?(?:T(?=\d)(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?)$")


@dataclass(frozen=True)
class VEvent:
    """One VEVENT as read: no title, no description, no people.

    start/end: aware UTC datetimes (kind timed), dates with an exclusive end (all_day), or naive
    local datetimes (unknown_zone: a TZID or a floating time).
    """
    uid: str
    status: str
    transp: str
    kind: str
    start: object
    end: object
    rrule: Optional[str] = None
    rdate: bool = False
    recurrence_id: bool = False


def _logical_lines(data) -> List[str]:
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError("an .ics is bytes")
    text = bytes(data).decode("utf-8", errors="replace").lstrip("﻿")
    out = []   # type: List[str]
    for line in re.split(r"\r\n|\n|\r", text):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        elif line.strip():
            out.append(line)
    return out


def _content_line(line) -> Tuple[str, Dict[str, str], str]:
    m = _TOKEN.match(line)
    if not m:
        raise ValueError("not a content line")
    name, i, n = m.group(0).upper(), m.end(), len(line)
    params = {}
    while i < n and line[i] == ";":
        m = _TOKEN.match(line, i + 1)
        if not m or m.end() >= n or line[m.end()] != "=":
            raise ValueError("bad parameter")
        pname, i = m.group(0).upper(), m.end() + 1
        values = []
        while True:
            if i < n and line[i] == '"':
                j = line.find('"', i + 1)
                if j < 0:
                    raise ValueError("unterminated quote")
                values.append(line[i + 1:j])
                i = j + 1
            else:
                j = i
                while j < n and line[j] not in ";:,":
                    j += 1
                values.append(line[i:j])
                i = j
            if i < n and line[i] == ",":
                i += 1
                continue
            break
        params[pname] = ",".join(values)
    if i >= n or line[i] != ":":
        raise ValueError("no value")
    return name, params, line[i + 1:]


def _when(params, value):
    value = value.strip()
    if params.get("VALUE", "").upper() == "DATE" or _DATE.match(value):
        m = _DATE.match(value)
        if not m:
            raise ValueError("bad date")
        return _dt.date(*(int(g) for g in m.groups()))
    m = _DATETIME.match(value)
    if not m:
        raise ValueError("bad date-time")
    moment = _dt.datetime(*(int(g) for g in m.groups()[:6]))
    if m.group(7) == "Z" and "TZID" not in params:
        return moment.replace(tzinfo=UTC)
    return moment   # a TZID (no zone database) or a floating time: unknown zone


def _duration(value) -> _dt.timedelta:
    m = _DURATION.match(value.strip())
    if not m or m.group(1) == "-":
        raise ValueError("bad duration")
    weeks, days, hours, minutes, seconds = (int(g) if g else 0 for g in m.groups()[1:])
    return _dt.timedelta(weeks=weeks, days=days, hours=hours, minutes=minutes, seconds=seconds)


def _kind(value) -> str:
    if not isinstance(value, _dt.datetime):
        return "all_day"
    return "timed" if value.tzinfo is not None else "unknown_zone"


def _vevent(props) -> VEvent:
    def one(name):
        values = props.get(name)
        if not values:
            return None
        if len(values) > 1 and name in ("DTSTART", "DTEND", "DURATION"):
            raise ValueError("%s given twice" % name)
        return values[0]

    if one("DTSTART") is None:
        raise ValueError("no DTSTART")
    start = _when(*one("DTSTART"))
    kind = _kind(start)
    if one("DTEND") is not None:
        end = _when(*one("DTEND"))
        if _kind(end) != kind:
            raise ValueError("DTSTART and DTEND differ in kind")
    elif one("DURATION") is not None:
        span = _duration(one("DURATION")[1])
        if kind == "all_day" and span % _DAY:
            raise ValueError("an all-day duration is whole days")
        end = start + span
    else:
        end = start + _DAY if kind == "all_day" else start
    if end < start:
        raise ValueError("ends before it starts")
    if kind == "all_day" and end == start:
        end = start + _DAY
    rrules = [v for _, v in props.get("RRULE", [])]
    return VEvent(
        uid=(one("UID") or ({}, ""))[1].strip(),
        status=(one("STATUS") or ({}, ""))[1].strip().upper(),
        transp=(one("TRANSP") or ({}, ""))[1].strip().upper(),
        kind=kind, start=start, end=end,
        rrule="|".join(v.strip() for v in rrules) if rrules else None,
        rdate="RDATE" in props,
        recurrence_id="RECURRENCE-ID" in props,
    )


def read_events(ics_bytes) -> List[VEvent]:
    """Every VEVENT in the bytes (repeats not expanded). Lenient on line endings, folding and
    case. Raises ValueError when the calendar or any event in it cannot be read."""
    stack = []      # type: List[str]
    events = []     # type: List[VEvent]
    props = {}      # type: Dict[str, list]
    for line in _logical_lines(ics_bytes):
        name, params, value = _content_line(line)
        if name == "BEGIN":
            comp = value.strip().upper()
            if comp == "VEVENT":
                if "VEVENT" in stack:
                    raise ValueError("nested VEVENT")
                props = {}
            stack.append(comp)
        elif name == "END":
            comp = value.strip().upper()
            if not stack or stack[-1] != comp:
                raise ValueError("unbalanced BEGIN/END")
            stack.pop()
            if comp == "VEVENT":
                events.append(_vevent(props))
        elif stack and stack[-1] == "VEVENT" and name in _READ:
            props.setdefault(name, []).append((params, value))
    if stack:
        raise ValueError("unterminated component")
    if not events:
        raise ValueError("no VEVENT")
    return events


def _zones():
    from . import zones
    return zones


def to_event(v: VEvent, start=None, end=None) -> Event:
    """The Event for a VEVENT, or for one occurrence of it (start/end in the VEVENT's kind)."""
    start = v.start if start is None else start
    end = v.end if end is None else end
    if v.kind == "timed":
        return Event(uid=v.uid, status=v.status, transp=v.transp, kind="timed", start_utc=start, end_utc=end)
    zones = _zones()
    if v.kind == "all_day":
        midnight = _dt.time(0)
        return Event(uid=v.uid, status=v.status, transp=v.transp, kind="all_day",
                     start_utc=zones.to_utc(_dt.datetime.combine(start, midnight), "Pacific"),
                     end_utc=zones.to_utc(_dt.datetime.combine(end, midnight), "Pacific"),
                     start_date=start, end_date=end)
    # unknown_zone: the local time read as Pacific; busy for every Pacific day it touches.
    if end > start and end.time() == _dt.time(0):
        end_date = end.date()
    else:
        end_date = end.date() + _DAY
    return Event(uid=v.uid, status=v.status, transp=v.transp, kind="unknown_zone",
                 start_utc=zones.to_utc(start, "Pacific"), end_utc=zones.to_utc(end, "Pacific"),
                 start_date=start.date(), end_date=end_date)


def read_event(ics_bytes) -> Event:
    """The first event in the bytes as times and status only. Round-trips build_event.
    Raises ValueError when it cannot be read."""
    return to_event(read_events(ics_bytes)[0])
