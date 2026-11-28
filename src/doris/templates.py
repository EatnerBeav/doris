"""Every sentence Doris can send: the owner-approved templates, filled by code.

The model never writes a word that leaves the building. Code picks a template id from the
policy table and passes typed slots: aware UTC datetimes, ints, a zone id, names and
addresses from headers, and the meeting link by reference (MEETING_LINK). `render` returns
the exact approved text ("/" on the template page is a line break here) or raises
TemplateError. Source: templates.md (approved by Shane; T5, T6, T7, T8, T11, T14-T17, K1-K3 cut).

Times are rendered from UTC by code (templates.md, ruling 10):
- offers and the S1 draft (fmt_slot): Shane's zone first, the sender's zone in brackets
  when known and different, UTC when unknown: "Wed Dec 2, 11:00 AM Pacific (2:00 PM Eastern)".
- T1, T10, T12 and T18's cancelled slot (fmt_theirs): the sender's zone first, Pacific in
  brackets; unknown or Pacific shows Pacific and UTC.
- UTC and fixed offsets are written on the 24-hour clock ("21:30 UTC"), as demo day shows.
"""

import datetime as _dt
import html
import json
import os
import re
import unicodedata
import urllib.parse

from .shapes import UTC

ZONES = None   # tests put a fake here; otherwise zones.py is imported when first needed


class TemplateError(ValueError):
    """Not an approved template, a slot failed its type, or the text would break a rule."""


class _MeetingLinkRef(object):
    """The meeting link by reference: its value lives in .env and never in a receipt."""

    def __repr__(self):
        return "[meeting link]"


MEETING_LINK = _MeetingLinkRef()
MEETING_LINK_ENV = "DORIS_MEETING_LINK"
PLACEHOLDER_MEETING_LINK = "https://meet.example/doris-sandbox-room"   # sandbox only; live sets the env

APPROVED = frozenset({"T1", "T2", "T3", "T4", "T9", "T10", "T12", "T13", "T18", "S1", "S2"})
PROSPECT_TEMPLATES = frozenset(t for t in APPROVED if t.startswith("T"))
ICS_METHOD = {"T1": "PUBLISH", "T10": "CANCEL", "T18": "CANCEL"}   # templates that carry an .ics

# --- S1 hold reasons ------------------------------------------------------------------------
# The twenty-one approved phrases (templates.md "S1 hold reasons": fifteen Nov 25-26, six more
# Nov 27, decision 68), plus hold.unknown_result on the approved calendar phrase (decision 61) and
# hold.internal_error on the approved "wasn't sure" phrase (decision 69).
HOLD_REASONS = {
    "hold.owner_mail": "mail from your own address; I don't take instructions by email",
    "hold.oversize": "too long to read safely",
    "hold.hidden_text": "hidden text in the email",
    "hold.new_address": "a pick from a different address than the one I offered to",
    "hold.reply_to_mismatch": "the reply address differs from the sender",
    "hold.all_day_recurring_multiple": "all-day, recurring or several meetings",
    "hold.not_in_ledger": "a cancel or move for a booking I don't have",
    "hold.third_offer": "a third set of times would be needed",
    "hold.remove_failed": "I couldn't remove my booking after a clash; both events are listed below",
    "hold.calendar_down": "the calendar didn't answer twice",
    "hold.unknown_result": "the calendar didn't answer twice",
    "hold.money_with_pick": "money, legal or a complaint, with a pick",
    "hold.no_model": "the model wasn't available, so I didn't read it",
    "hold.group_mail": "more than one person is on the thread",
    "hold.owner_removed": "you removed this booking from your calendar; I haven't told them",
    "hold.longer_than_hour": "longer than an hour",
    # approved Nov 27 11:06 PT (decision 68)
    "hold.money": "money, legal or a complaint",
    "hold.legal": "money, legal or a complaint",
    "hold.complaint": "money, legal or a complaint",
    "hold.add_person": "asks to add or write to another person",
    "hold.unsure": "not something I handle, or I wasn't sure",
    "hold.outside_classes": "not something I handle, or I wasn't sure",
    "hold.no_slot": "no available slot in the next 30 days",
    "hold.gate_blocked": "a safety check stopped my reply",
    "hold.send_unconfirmed": "I couldn't confirm my reply was sent",
    # an unexpected error part way through one message (decision 69): an approved phrase, no new wording
    "hold.internal_error": "not something I handle, or I wasn't sure",
}
NO_NOTICE = frozenset({"hold.thread_held"})   # decision 3: later mail on a held thread goes to the digest only


def reason_phrase(code):
    """The approved S1 phrase for a hold code. Unknown codes raise."""
    if code in NO_NOTICE:
        raise TemplateError("%s sends no hold notice" % (code,))
    if code not in HOLD_REASONS:
        raise TemplateError("no hold reason for %r" % (code,))
    return HOLD_REASONS[code]


# --- formatting --------------------------------------------------------------------------------
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_ZONE_ID = re.compile(r"^(?:Pacific|Mountain|Central|Eastern|UTC|UK|UTC[+-]\d\d:\d\d)$")


def _zones():
    if ZONES is not None:
        return ZONES
    from . import zones
    return zones


def _aware(value, what="time"):
    if not isinstance(value, _dt.datetime) or value.tzinfo is None:
        raise TemplateError("%s must be an aware datetime" % (what,))
    return value.astimezone(UTC)


def _clock(local, zone_id):
    if zone_id.startswith("UTC"):
        return "%02d:%02d" % (local.hour, local.minute)
    hour = local.hour % 12 or 12
    return "%d:%02d %s" % (hour, local.minute, "AM" if local.hour < 12 else "PM")


def _day(local):
    return "%s %s %d" % (_DAYS[local.weekday()], _MONTHS[local.month - 1], local.day)


def _local(utc, zone_id):
    return _zones().to_local(_aware(utc), zone_id)


def _full(utc, zone_id):
    """'Wed Dec 2, 2:30 PM Eastern'."""
    local = _local(utc, zone_id)
    return "%s, %s %s" % (_day(local), _clock(local, zone_id), _zones().display(zone_id))


def _bracket(utc, zone_id, main_zone):
    """The bracketed second zone: time only, or with the day when its date differs."""
    local = _local(utc, zone_id)
    main = _local(utc, main_zone)
    clock = "%s %s" % (_clock(local, zone_id), _zones().display(zone_id))
    if local.date() != main.date():
        return "%s, %s" % (_day(local), clock)
    return clock


def fmt_pacific(utc):
    """Shane's zone alone: 'Tue Dec 1, 1:10 PM Pacific' (hold and wrote-at times to the owner)."""
    return _full(utc, "Pacific")


def fmt_slot(utc, zone_id):
    """Shane's zone first: 'Wed Dec 2, 11:00 AM Pacific (2:00 PM Eastern)'; unknown zone -> UTC."""
    _aware(utc)
    if zone_id == "Pacific":
        return _full(utc, "Pacific")
    second = zone_id if zone_id is not None else "UTC"
    return "%s (%s)" % (_full(utc, "Pacific"), _bracket(utc, second, "Pacific"))


def fmt_theirs(utc, zone_id):
    """The sender's zone first: 'Wed Dec 2, 1:00 PM Central (11:00 AM Pacific)'.
    Unknown or Pacific: 'Wed Dec 2, 11:00 AM Pacific (19:00 UTC)' (ruling 10, Wolf's reading)."""
    _aware(utc)
    if zone_id is None or zone_id == "Pacific":
        return "%s (%s)" % (_full(utc, "Pacific"), _bracket(utc, "UTC", "Pacific"))
    return "%s (%s)" % (_full(utc, zone_id), _bracket(utc, "Pacific", zone_id))


_run_link = None     # the link a run pinned (use_meeting_link): live from its env file, the sandbox its own


def use_meeting_link(link):
    """Pin the one link this run uses, so the reply, the .ics and the gate's G13 all read the same
    value; None lets go. Returns what was pinned before, to hand back when the run ends."""
    global _run_link
    before, _run_link = _run_link, link
    return before


def meeting_link():
    """The one fixed meeting room: the link the run pinned; else the env, else the sandbox placeholder."""
    if _run_link is not None:
        return _run_link
    value = os.environ.get(MEETING_LINK_ENV, "").strip()
    return value or PLACEHOLDER_MEETING_LINK


# --- the link in what she keeps --------------------------------------------------------------------
# The law: the meeting link never goes in a receipt. A sender may write it (a reply quoting her T1
# does), so every receipt line, recorded model answer, kept copy of a mail and quoted text carries
# LINK_SHOWN in its place, in whatever form the sender, defanging or an encoding wrote it.
LINK_SHOWN = "[meeting link]"
_LINK_PATTERNS = {}       # (link, for_bytes) -> compiled pattern

# Characters a reader never sees (holds removes them before it counts a notice's characters, decision
# 61): Unicode's default-ignorable code points (zero-width, direction and format characters, the soft
# hyphen, fillers, variation selectors, tags).
INVISIBLES = ("\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f\u200b-\u200f\u202a-\u202e"
              "\u2060-\u206f\u3164\ufe00-\ufe0f\ufeff\uffa0\ufff0-\ufff8\U0001bca0-\U0001bca3"
              "\U0001d173-\U0001d17a\U000e0000-\U000e0fff")
# With the format characters (Cf) INVISIBLES leaves out, every Cf character Unicode 15.1 has.
_HIDDEN = re.compile("[" + INVISIBLES + "\u0600-\u0605\u06dd\u070f\u0890\u0891\u08e2\ufff9-\ufffb"
                     "\U000110bd\U000110cd\U00013430-\U0001343f]")
_TAG = re.compile(r"<[^<>]*>")      # an HTML tag or comment with no "<" or ">" inside: linear, whatever the text
_ESCAPES = re.compile(r"(?:%[0-9A-Fa-f]{2})+")


def _link_pattern(for_bytes):
    """The meeting link (meeting_link(): the run's pinned one) as a pattern, any case: its scheme
    http, https, hxxp or hxxps, written "://", ":\\/\\/" or "[://]", or left off when the link has a
    path; "." also as "[.]", "/" as "\\/", "@" as "[@]", "&" as "&amp;"; white space (a wrapped line)
    between any two characters. For a kept copy's bytes (for_bytes), a quoted-printable soft break
    ("=" at a line's end) may fall anywhere too, and "=" may be "=3D". None for a link too bare to
    find safely (no host)."""
    link = meeting_link()
    key = (link, for_bytes)
    if key not in _LINK_PATTERNS:
        m = re.match(r"([A-Za-z][A-Za-z0-9+.\-]*)://", link)
        scheme, rest = (m.group(1).lower(), link[m.end():]) if m else (None, link)
        rest = rest.rstrip("/")
        gap = r"(?:=\r?\n|\s)*" if for_bytes else r"\s*"
        alts = {".": (r"\.", r"\[\.\]"), "/": ("/", r"\\/"), "@": ("@", r"\[@\]"), "&": ("&", "&amp;", "&#38;"),
                "=": ("=3D", "=") if for_bytes else ("=",)}

        def atoms(text):
            return gap.join("(?:%s)" % "|".join(alts[c]) if c in alts else re.escape(c) for c in text)

        if not rest or "." not in rest.split("/")[0]:
            pattern = None
        else:
            body = atoms(rest) + "(?:%s(?:/|\\\\/))?" % gap
            if scheme in ("http", "https"):
                name = gap.join(("h", "[tx]", "[tx]", "p")) + "(?:%ss)?" % gap
            else:
                name = atoms(scheme) if scheme else None
            if name is not None:
                sep = "(?:%s|\\[://\\])" % gap.join((":", "(?:/|\\\\/)", "(?:/|\\\\/)"))
                head = "(?:%s%s%s%s)" % (name, gap, sep, gap)
                body = head + ("?" if "/" in rest or scheme is None else "") + body
            pattern = re.compile(body.encode("ascii") if for_bytes else body, re.IGNORECASE)
        _LINK_PATTERNS[key] = pattern
    return _LINK_PATTERNS[key]


def redact_link(text):
    """text with every form of the meeting link replaced by "[meeting link]". When the link still
    shows as a reader sees it (has_link: in fullwidth letters, or with a zero-width space or a soft
    hyphen in it), text is folded as a reader sees it and the link replaced there; when it shows only
    once HTML entities or %-escapes are undone or HTML tags taken out (https://meet.example/room%2D1),
    the whole of text is "[meeting link]", since those forms have no edges to cut at. Not a str: as
    it is."""
    rx = _link_pattern(False) if isinstance(text, str) else None
    if rx is None:
        return text
    out = rx.sub(LINK_SHOWN, text)
    if not has_link(out):
        return out
    out = rx.sub(LINK_SHOWN, _as_seen(out))
    return LINK_SHOWN if has_link(out) else out


def redact_json_text(text):
    """A model's raw answer (JSON text) with the link replaced where it stands, as redact_link does;
    where redact_link would fold the text or blank it whole instead (a link only a reader's view or an
    unescaped form shows), each string in the JSON goes through redact_link (redact_values), so the
    answer keeps its shape and a replay reads the same answer, with only the string that showed the
    link changed. Text that is not a JSON object or list is as redact_link leaves it, and one that
    still shows the link is "[meeting link]"."""
    rx = _link_pattern(False) if isinstance(text, str) else None
    if rx is None:
        return text
    out = redact_link(text)
    if out == rx.sub(LINK_SHOWN, text):
        return out
    try:
        obj = json.loads(text)
    except ValueError:
        return out
    if not isinstance(obj, (dict, list)):
        return out
    again = json.dumps(redact_values(obj), ensure_ascii=False)
    return LINK_SHOWN if has_link(again) else again


def redact_values(value):
    """A receipt payload with redact_link applied to every string in it; dict keys stay as they are."""
    if isinstance(value, str):
        return redact_link(value)
    if isinstance(value, dict):
        return dict((k, redact_values(v)) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return [redact_values(v) for v in value]
    return value


def redact_link_bytes(data):
    """(bytes, how many) with every form of the meeting link in a mail's bytes replaced, a
    quoted-printable soft break inside it too. What only decoding shows (base64, an encoded header)
    is left for has_link to find."""
    rx = _link_pattern(True)
    if rx is None:
        return bytes(data), 0
    return rx.subn(LINK_SHOWN.encode("ascii"), bytes(data))


def _as_seen(text):
    """text as a reader sees it: compatibility forms folded (NFKC: fullwidth letters and signs to
    ASCII), invisible and format characters dropped."""
    if text.isascii():
        return text
    return _HIDDEN.sub("", unicodedata.normalize("NFKC", text))


def _unquote(text):
    """urllib.parse.unquote(text) (%-escapes undone as UTF-8, a bad sequence replaced), in one pass over
    the escapes alone: unquote walks every run of ASCII, seconds on a binary part's bytes."""
    if "%" not in text:
        return text
    return _ESCAPES.sub(lambda m: bytes.fromhex(m.group(0).replace("%", "")).decode("utf-8", "replace"), text)


def _link_views(text):
    """Each way has_link reads text, once: as it is and with HTML entities and %-escapes undone; each
    of those as a reader sees it (_as_seen), and that with its HTML tags taken out."""
    unescaped = html.unescape(text)
    given = []
    for view in (text, unescaped, _unquote(text), _unquote(unescaped)):
        seen = _as_seen(view)
        for v in (view, seen, _TAG.sub("", seen)):
            if not any(v is w or v == w for w in given):
                given.append(v)
                yield v


def has_link(text):
    """True when the meeting link still shows in text: as it is, once HTML entities or %-escapes are
    undone, or as a reader sees it: written with a zero-width space or a soft hyphen, in fullwidth
    letters, or split by an empty HTML tag (review of 7e1a9fa, item 3). The last check on a kept copy,
    run on each of its decoded parts, and on each string redact_link leaves."""
    rx = _link_pattern(False)
    if rx is None or not isinstance(text, str):
        return False
    return any(rx.search(v) for v in _link_views(text))


# --- slot checks ---------------------------------------------------------------------------------
_ADDR = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_PRICE = re.compile(r"[$\u20ac\u00a3\u00a5]\s?\d|\d\s?[$\u20ac\u00a3\u00a5]|\b(?:USD|EUR|GBP|CAD|AUD)\s?\d|\d\s?(?:USD|EUR|GBP|CAD|AUD)\b",
                    re.IGNORECASE)


def _int(v, lo, hi, what):
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise TemplateError("%s must be an int from %d to %d" % (what, lo, hi))
    return v


def _duration(v):
    return _int(v, 5, 60, "duration")


def _zone(v):
    if v is None:
        return None
    if not isinstance(v, str) or not _ZONE_ID.match(v):
        raise TemplateError("zone must be a zone id or None")
    return v


def _times(v, lo, hi):
    if not isinstance(v, (list, tuple)) or not lo <= len(v) <= hi:
        raise TemplateError("slots must list %d to %d times" % (lo, hi))
    return [_aware(t, "slot") for t in v]


def line_ok(v, limit=200):
    """True when v can stand as one line of header text (a name) of at most `limit` characters."""
    return (isinstance(v, str) and bool(v.strip()) and len(v) <= limit
            and not any(ord(c) < 32 or ord(c) == 127 for c in v))


def _line(v, what, limit=200):
    """One line of header text (a name): no line breaks, no control characters."""
    if not isinstance(v, str) or not v.strip() or len(v) > limit:
        raise TemplateError("%s must be a short non-empty string" % (what,))
    if not line_ok(v, limit):
        raise TemplateError("%s may not carry control characters or line breaks" % (what,))
    return v


def _addr(v, what="address"):
    if not isinstance(v, str) or not _ADDR.match(v):
        raise TemplateError("%s must be a bare email address" % (what,))
    return v


# An address shown to the owner in a notice: body text, never a header. One address on one line,
# like _ADDR, but it takes letters outside ASCII, so a sender whose address has one is still named
# in the notice (Fable re-review F), and a code in it may be blanked to REDACTED (holds.shown_address).
# No control or format characters (a bidi override).
_SHOWN_ADDR = re.compile(r'^[^\s<>()\[\]\\,;:"@]+@[^\s<>()\[\]\\,;:"@]+\.[^\s<>()\[\]\\,;:"@.]+$')

# What a notice shows in place of a code, key or token from the mail (decision 77), and in place of
# a whole value it cannot show safely (an address too long, or one the line can't carry).
REDACTED = "[redacted]"


def shown_address_ok(v):
    """True when v can stand as the address a notice shows: one address on one line, or REDACTED."""
    return isinstance(v, str) and (v == REDACTED or (
        len(v) <= 254 and bool(_SHOWN_ADDR.match(v.replace(REDACTED, "redacted")))
        and not any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in v)))


def _shown_addr(v, what="address"):
    if not shown_address_ok(v):
        raise TemplateError("%s must be one email address on one line" % (what,))
    return v


def _text(v, what, limit):
    if not isinstance(v, str) or len(v) > limit:
        raise TemplateError("%s must be a string of at most %d characters" % (what, limit))
    if any((ord(c) < 32 and c not in "\n\t") or ord(c) == 127 for c in v):
        raise TemplateError("%s may not carry control characters" % (what,))
    return v


def _need(slots, keys):
    if not isinstance(slots, dict):
        raise TemplateError("slots must be a dict")
    missing = set(keys) - set(slots)
    extra = set(slots) - set(keys)
    if missing or extra:
        raise TemplateError("slots differ from the template: missing %s, extra %s"
                            % (sorted(missing), sorted(extra)))


def _numbered(lines):
    return ["%d. %s" % (i + 1, s) for i, s in enumerate(lines)]


def _min(n):
    return "%d min" % n


# --- the prospect templates --------------------------------------------------------------------
_PICK_LINE = "Reply with the one that suits you and I'll book it."
_YES_LINE = "Reply \"yes\" and I'll book it."
_CANCEL_LINE = ("Done. Your call with Shane on %s is cancelled, and the attached file removes it "
                "from your calendar.")


def _t1(s):
    _need(s, ("start", "duration", "zone", "meeting_link"))
    start, d, z = _aware(s["start"]), _duration(s["duration"]), _zone(s["zone"])
    if s["meeting_link"] is not MEETING_LINK:
        raise TemplateError("the meeting link is filled by reference (templates.MEETING_LINK) only")
    if z is None or z == "Pacific":
        first = "You're booked with Shane Bornstein for %s, %s." % (fmt_theirs(start, z), _min(d))
    else:
        theirs, pac = _local(start, z), _local(start, "Pacific")
        pac_text = _clock(pac, "Pacific") if pac.date() == theirs.date() else "%s, %s" % (
            _day(pac), _clock(pac, "Pacific"))
        first = "You're booked with Shane Bornstein for %s, which is %s Pacific for Shane, %s." % (
            _full(start, z), pac_text, _min(d))
    return [first, "The calendar file is attached. Join: %s" % meeting_link(),
            "To move or cancel, reply on this thread."]


def _offer(s, lo, hi, head):
    _need(s, ("slots", "duration", "zone"))
    times, d, z = _times(s["slots"], lo, hi), _duration(s["duration"]), _zone(s["zone"])
    return [head % _min(d)] + _numbered([fmt_slot(t, z) for t in times])


def _t2(s):
    return _offer(s, 2, 3, "Shane is available at any of these, %s each:") + [_PICK_LINE]


def _t3(s):
    return _offer(s, 3, 3, "Here are three more times Shane is available, %s each:") + [
        "Reply with one and I'll book it."]


def _t4(s):
    _need(s, ("asked", "slots", "duration", "zone"))
    asked, z = _aware(s["asked"], "asked"), _zone(s["zone"])
    rest = dict((k, s[k]) for k in ("slots", "duration", "zone"))
    head = "Shane isn't available at %s, but is available at any of these, %%s each:" % fmt_slot(asked, z)
    return _offer(rest, 1, 3, head) + [_PICK_LINE]


def _t9(s):
    _need(s, ("slots", "duration", "zone"))
    times = _times(s["slots"], 1, 3)
    if len(times) == 1:
        return ["One more try: Shane is still available at %s, %s." % (
                    fmt_slot(times[0], _zone(s["zone"])), _min(_duration(s["duration"]))),
                _YES_LINE + " After this I'll leave it with you."]
    return _offer(s, 2, 3, "One more try: Shane is still available at any of these, %s each:") + [
        "After this I'll leave it with you."]


def _t10(s):
    _need(s, ("start", "zone"))
    return [_CANCEL_LINE % fmt_theirs(_aware(s["start"]), _zone(s["zone"])),
            "If you'd like another time, reply with a day that suits you."]


def _t12(s):
    _need(s, ("slots", "zone"))
    times, z = _times(s["slots"], 1, 3), _zone(s["zone"])
    if z is None or z == "Pacific":
        head = "in UTC and Shane's time:"
        lines = ["%s (%s)" % (_full(t, "UTC"), _bracket(t, "Pacific", "UTC")) for t in times]
    else:
        head = "in your time and Shane's:"
        lines = [fmt_theirs(t, z) for t in times]
    return (["I couldn't match that to one of the times I offered, so here they are again " + head]
            + _numbered(lines)
            + ["Reply with the number of the one that suits you and I'll book it."])


def _t13(s):
    _need(s, ("slot", "duration", "zone"))
    return ["Shane is available at %s, %s." % (fmt_slot(_aware(s["slot"], "slot"), _zone(s["zone"])),
                                                _min(_duration(s["duration"]))),
            _YES_LINE]


def _t18(s):
    _need(s, ("start", "slots", "duration", "zone"))
    start, z, d = _aware(s["start"]), _zone(s["zone"]), _duration(s["duration"])
    times = _times(s["slots"], 1, 3)
    first = _CANCEL_LINE % fmt_theirs(start, z)
    if len(times) == 1:   # T13's one-slot form (templates.md T18 row)
        return [first, "If you'd like another time, Shane is available at %s, %s." % (fmt_slot(times[0], z), _min(d)),
                _YES_LINE]
    return ([first, "If you'd like another time, Shane is available at any of these, %s each:" % _min(d)]
            + _numbered([fmt_slot(t, z) for t in times]) + [_PICK_LINE])


# --- the owner templates -----------------------------------------------------------------------
S1_KEYS = ("name", "address", "reason", "wrote_at", "quote", "first_name", "draft_slots", "zone")


def _s1(s):
    _need(s, S1_KEYS)
    reason = reason_phrase(s["reason"])
    z = _zone(s["zone"])
    lines = [
        "Held for you: %s (%s), %s." % (_line(s["name"], "name"), _shown_addr(s["address"]), reason),
        "They wrote at %s:" % fmt_pacific(_aware(s["wrote_at"], "wrote_at")),
        "\"%s\"" % _text(s["quote"], "quote", 400),
        "Nothing has been sent to them, and I won't send anything more on this thread. "
        "A draft is below if you want it; send it from your own mail.",
        "----",
        "Hi %s, Shane here. [Your answer on: %s.]" % (_line(s["first_name"], "first_name", 80), reason),
    ]
    times = _times(s["draft_slots"], 0, 3)
    if times:
        lines.append("If it's easier to talk, I'm available at:")
        lines.extend(_numbered([fmt_slot(t, z) for t in times]))
    return lines


S2_KEYS = ("date", "handled", "held", "approvals", "holds", "offered_to", "suspicious", "quarantined",
           "also_asked", "opted_out", "not_addressed", "sensitive", "read_failures", "model_calls", "cost_usd",
           "verified")


def empty_digest(now):
    """S2 slots with every count at zero (digest.build fills them from receipts)."""
    return {"date": now, "handled": 0, "held": 0, "approvals": 0, "holds": [], "offered_to": [],
            "suspicious": [], "quarantined": 0, "also_asked": [], "opted_out": [], "not_addressed": 0,
            "sensitive": [], "read_failures": 0, "model_calls": 0, "cost_usd": 0.0, "verified": False}


def _names(v, what, check=_line):
    if not isinstance(v, (list, tuple)):
        raise TemplateError("%s must be a list" % (what,))
    return [check(x, what) for x in v]


def _s2(s):
    _need(s, S2_KEYS)
    n = _int(s["handled"], 0, 10 ** 6, "handled")
    h = _int(s["held"], 0, 10 ** 6, "held")
    a = _int(s["approvals"], 0, 10 ** 6, "approvals")
    day = _day(_local(_aware(s["date"], "date"), "Pacific"))
    lines = ["Handled %d, held %d, human approvals %d. %s" % (n, h, a, day)]
    if not isinstance(s["holds"], (list, tuple)):
        raise TemplateError("holds must be a list")
    if s["holds"]:
        lines.append("Held for you:")
        for item in s["holds"]:
            if not isinstance(item, dict) or set(item) != {"name", "reason", "at"}:
                raise TemplateError("each hold is {name, reason, at}")
            lines.append("%s, %s, %s" % (_line(item["name"], "name"), reason_phrase(item["reason"]),
                                         fmt_pacific(_aware(item["at"], "at"))))
    for pair in s["also_asked"]:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise TemplateError("also_asked holds (name, question) pairs")
        lines.append("Also asked: %s, '%s'" % (_line(pair[0], "name"), _line(pair[1], "question", 200)))
    offered = _names(s["offered_to"], "offered_to")
    if offered:
        lines.append("Offered times to: " + ", ".join(offered))
    opted = []
    for pair in s["opted_out"]:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise TemplateError("opted_out holds (address, dropped) pairs")
        k = _int(pair[1], 0, 10 ** 6, "dropped")
        opted.append(_shown_addr(pair[0]) + (" (%d queued request dropped)" % k if k else ""))
    if opted:
        lines.append("Opted out: " + ", ".join(opted))
    bits = []
    suspicious = _names(s["suspicious"], "suspicious")
    if suspicious:
        bits.append("Suspicious, nothing sent: %s." % ", ".join(suspicious))
    q = _int(s["quarantined"], 0, 10 ** 6, "quarantined")
    if q:
        bits.append("Quarantined unreadable: %d." % q)
    if bits:
        lines.append(" ".join(bits))
    k = _int(s["not_addressed"], 0, 10 ** 6, "not_addressed")
    if k:
        lines.append("Not addressed to you alone, no reply: %d" % k)
    sensitive = _names(s["sensitive"], "sensitive", _shown_addr)
    if sensitive:
        lines.append("Sensitive mail skipped: " + ", ".join(sensitive))
    failed = _int(s["read_failures"], 0, 10 ** 6, "read_failures")
    if failed:
        lines.append("Failed inbox reads: %d." % failed)     # decision 89: a run until stopped keeps trying
    c = _int(s["model_calls"], 0, 10 ** 6, "model_calls")
    cost = s["cost_usd"]
    if isinstance(cost, bool) or not isinstance(cost, (int, float)) or cost < 0:
        raise TemplateError("cost_usd must be a non-negative number")
    if not isinstance(s["verified"], bool):
        raise TemplateError("verified must be a bool")
    verify = "Receipts verify: %s." % ("yes" if s["verified"] else "no")
    lines.append(("Model calls %d, cost $%.4f. %s" % (c, cost, verify)) if c else verify)
    return lines


_RENDER = {"T1": _t1, "T2": _t2, "T3": _t3, "T4": _t4, "T9": _t9, "T10": _t10, "T12": _t12,
           "T13": _t13, "T18": _t18, "S1": _s1, "S2": _s2}


def render(template_id, slots):
    """The exact approved text for template_id, filled from typed slots; raises TemplateError."""
    if template_id not in APPROVED or template_id not in _RENDER:
        raise TemplateError("not an approved template: %r" % (template_id,))
    text = "\n".join(_RENDER[template_id](slots))
    if template_id in PROSPECT_TEMPLATES and _PRICE.search(text):
        raise TemplateError("a price can never go to a prospect")
    return text


def receipt_slots(slots):
    """The slots as a receipt records them: JSON-safe, datetimes as ISO UTC, the link by name only."""
    def conv(v):
        if v is MEETING_LINK:
            return "[meeting link]"
        if isinstance(v, _dt.datetime):
            return _aware(v).strftime("%Y-%m-%dT%H:%M:%SZ")
        if isinstance(v, dict):
            return dict((k, conv(x)) for k, x in v.items())
        if isinstance(v, (list, tuple)):
            return [conv(x) for x in v]
        return v
    return conv(slots)
