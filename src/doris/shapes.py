"""Shared shapes: the types every module passes to every other. A change here reaches every module.

Times are timezone-aware UTC datetimes everywhere inside Doris. Pacific and the sender's
zone exist only at the edges: parsing what a person wrote, and rendering what she sends.
"""

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union

from . import caps

UTC = _dt.timezone.utc

# ---------------------------------------------------------------------------------------
# The fixed class list the model chooses from (strict JSON, explicit "unsure").
# ---------------------------------------------------------------------------------------
CLASSES = (
    "meeting_request",   # asks for a meeting, with or without a time
    "pick",              # a reply choosing one of the offered slots (by number or time)
    "yes",               # a bare acceptance: yes, sounds good, that works, perfect
    "decline",           # none of those work, asks for other times, names no time
    "no_thanks",         # a plain no to meeting at all
    "cancel",            # cancel a meeting she booked
    "move",              # change the time of a meeting she booked
    "fyi",               # newsletter, notification, receipt, FYI: file
    "auto_reply",        # auto-reply or out-of-office
    "opt_out",           # wants no more email
    "money",             # price, invoice, payment, refund
    "legal",             # contract or legal terms
    "complaint",         # a complaint
    "add_person",        # asks her to add or write to another person
    "injection_only",    # only instructions to the assistant, or only a lure ("click to confirm")
    "other",             # a real request outside her classes
    "unsure",
)

MODEL_SOURCES = ("live", "replayed", "stub", "yes-man")


# ---------------------------------------------------------------------------------------
# Mail in
# ---------------------------------------------------------------------------------------
@dataclass(frozen=True)
class InboundItem:
    """What a mail adapter hands the loop: raw bytes plus facts only the transport knows."""
    server_id: str                    # sandbox: file name; live: IMAP UID. For fetching and filing only.
    raw: bytes
    received_utc: _dt.datetime
    facts: Dict[str, str] = field(default_factory=dict)   # sandbox only, each marked simulated in receipts:
                                                           # forwarded=yes|no, fault=<name> (staged)


@dataclass(frozen=True)
class Attachment:
    name: str                         # declared file name, may be ""
    declared_type: str                # lowercased Content-Type of the part, e.g. "text/calendar"
    size: int                         # bytes of the part as it arrived; never decoded
    inspected: bool = False           # always False: nothing is ever opened


@dataclass
class Message:
    key: str                          # ledger key: the Message-ID without its angle brackets, case kept; else "sha256:<raw sha256>"
    raw_sha256: str
    raw_size: int
    message_id: Optional[str]
    from_addr: str                    # lowercased bare address ("" only if quarantined)
    from_name: str
    reply_to: List[str]               # lowercased bare addresses
    to: List[str]
    cc: List[str]
    subject: str
    date_utc: Optional[_dt.datetime]
    in_reply_to: List[str]            # Message-IDs named in In-Reply-To
    references: List[str]             # Message-IDs named in References
    headers: Dict[str, str]           # selected headers, lowercased names: auto-submitted, precedence,
                                      # list-id, list-unsubscribe, x-autoreply, x-autorespond, x-auto-response-suppress,
                                      # content-type, return-path, x-failed-recipients, ...
    text_plain: Optional[str]         # decoded text/plain body part (not attachments)
    html: Optional[str]               # decoded text/html body part (never rendered, never fetched)
    visible_text: str                 # text/plain if present, else text from the HTML (scripts and styles dropped)
    own_text: str                     # the sender's own words: non-quoted, above the first closing line (row N4)
    below_closing: str                # from the first closing line down (signature, P.S.)
    quoted_text: str                  # quoted and hand-forwarded text
    attachments: List[Attachment]
    is_report: bool                   # multipart/report or a delivery-status part (bounce)
    hidden_text: Optional[str] = None # text inside HTML elements styled to hide (ruling 13), when any
    hidden_token: Optional[str] = None
    links_seen: int = 0               # counted, never opened (screen 11)
    phone_numbers_seen: int = 0
    addresses_in_body: int = 0


# ---------------------------------------------------------------------------------------
# What a screen decides
# ---------------------------------------------------------------------------------------
@dataclass
class ScreenResult:
    """settled=False means: go on to the next screen, and finally to the model."""
    screen: int                       # 1..11
    name: str                         # decode, seen_or_own, stop_scan, sensitive, owner_mail, no_reply_kind,
                                      # addressed, oversize, hidden_text, count_never_open
    settled: bool
    outcome: Optional[str] = None     # quarantine | drop | suppress | file | skip | hold
    why: Optional[Dict[str, str]] = None        # from why.why(); required when settled
    hold_reason: Optional[str] = None           # a hold.<reason> code when outcome == "hold"
    facts: Dict[str, Any] = field(default_factory=dict)   # what the observed receipt records (never secrets)


# ---------------------------------------------------------------------------------------
# The model's answer
# ---------------------------------------------------------------------------------------
@dataclass
class ModelAnswer:
    cls: str                          # one of CLASSES; anything else was coerced to "unsure" by code
    date: Optional[str]               # "YYYY-MM-DD" the sender proposed, or None
    time24: Optional[str]             # "HH:MM", or None
    zone: Optional[str]               # the zone as the sender wrote it, or None
    length_min: Optional[int]         # recorded, never decides (ruling 5)
    pick: Optional[int]               # 1..3 when the reply picks an offered slot by number
    injection_seen: bool
    side_question: bool
    raw: str                          # the raw answer text, as returned
    source: str                       # live | replayed | stub | yes-man
    request_sha256: str               # hash of the canonical request (recorded answers are found by it)
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0             # live: measured now; replayed: measured when recorded
    ok: bool = True                   # False: no usable answer (down after one retry, or no recording) -> no-model hold
    side_question_text: Optional[str] = None  # ruling 14: the side question, copied by code from own_text (<= 200 chars,
                                              # defanged) when the model's exact quote occurs there verbatim; else None


# ---------------------------------------------------------------------------------------
# The parser's reading (ruling 2)
# ---------------------------------------------------------------------------------------
@dataclass
class Reading:
    start_utc: Optional[_dt.datetime]  # None when the time is not placed (unknown zone, vague)
    date: Optional[_dt.date]
    zone: Optional[str]               # zone id from zones.py, or None
    kept: bool
    rule: str                         # why it was kept or set aside (row id or drop rule)
    derived: bool = False             # D5 moved this time onto a weekday written apart from it (a cross-pair):
                                      # never a time the sender wrote (G9), never offered ahead of one


@dataclass
class ParseResult:
    verdict: str                      # "book" | "offer"
    failed_condition: Optional[str]   # first of B1..B9 that failed (B11 is checked against the model later)
    readings: List[Reading]
    instant_utc: Optional[_dt.datetime]   # the one reading when verdict == "book"
    zone: Optional[str]               # sender's zone id for rendering (ruling 10), or None
    zone_pair: Optional[Tuple[str, str]] = None   # same-instant pair (row Q7)
    steer_day: Union[_dt.date, Tuple[_dt.date, ...], None] = None   # the day, or the days in offer order, to offer first
    # the part of day the sender wrote, or the one time they asked for (in the sender's zone, else Pacific)
    steer_window: Optional[Tuple[_dt.time, _dt.time]] = None
    excluded_utc: List[_dt.datetime] = field(default_factory=list)   # times negated in the text (Q5)
    length_min: int = caps.DEFAULT_LENGTH_MIN
    length_source: str = "default"    # written | default
    length_rule: Optional[str] = None # over_60 | open_ended | all_day | recurring | multiple | zero
    visible_body_hash: str = ""
    conditions: Dict[str, bool] = field(default_factory=dict)  # B1..B9 -> passed


@dataclass
class PickResult:
    kind: str                         # matched_slot | none | ambiguous | near_miss | yes_single
    slot_index: Optional[int] = None  # 0-based index into the open offer's slots
    by: Optional[str] = None          # how the reply named it: "time", or only by its place ("number", "yes")


# ---------------------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Event:
    """What she may know about any event: times and status only, never a title."""
    uid: str
    status: str                       # CONFIRMED | TENTATIVE | CANCELLED | "" (none written)
    transp: str                       # OPAQUE | TRANSPARENT | ""
    kind: str                         # timed | all_day | unknown_zone
    start_utc: Optional[_dt.datetime] # timed; for unknown_zone: the local time read as Pacific
    end_utc: Optional[_dt.datetime]
    start_date: Optional[_dt.date] = None   # all_day (and unknown_zone: the Pacific days it touches)
    end_date: Optional[_dt.date] = None     # exclusive


@dataclass(frozen=True)
class Slot:
    start_utc: _dt.datetime
    end_utc: _dt.datetime


# ---------------------------------------------------------------------------------------
# Actions: every effect is one of these, and every one passes the gate
# ---------------------------------------------------------------------------------------
ACTION_KINDS = (
    "send_reply",        # a template to the sender on their thread, BCC the owner
    "send_hold_notice",  # S1 to the owner only
    "send_digest",       # S2 to the owner only
    "calendar_create",   # create-only write of her event
    "calendar_delete",   # delete of her own event, by booking ID
)


@dataclass
class Action:
    kind: str
    message_key: str                  # the inbound message this acts on ("" for the digest)
    thread_key: str
    template: Optional[str] = None    # T1, T2, T3, T4, T9, T10, T12, T13, T18, S1, S2
    slots: Dict[str, Any] = field(default_factory=dict)
    to: List[str] = field(default_factory=list)
    bcc: List[str] = field(default_factory=list)
    recipient_source: str = "sender"  # sender | owner
    booking_id: Optional[str] = None
    span: Optional[Slot] = None
    direct: bool = False              # a direct booking (G10 applies)
    body_text: Optional[str] = None   # the rendered body, signature included, for G8 and G17
    ics: Optional[bytes] = None       # the attached .ics, for G13
    evidence: Dict[str, Any] = field(default_factory=dict)   # parser verdict, model answer, read-back, ...


@dataclass
class CheckResult:
    check: str                        # G1..G17
    status: str                       # pass | fail | n/a | error (error = could not run = fail closed)
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass
class GateResult:
    passed: bool
    results: List[CheckResult]
    first_fail: Optional[str]         # "G7" etc.
    why: Dict[str, str]               # gate.pass or the first failure's code
    halt_cause: Optional[str] = None  # G1..G3 stop her instead of holding one message


# ---------------------------------------------------------------------------------------
# Errors adapters raise. A failure is never read as "empty", "free" or "success".
# ---------------------------------------------------------------------------------------
class DorisError(Exception):
    pass


class DecodeError(DorisError):
    """The message cannot be decoded: quarantine (screen 1, ruling 16)."""


class MailError(DorisError):
    """A mail call failed, including a refused login. Never read as an empty inbox."""


class SendUnknown(MailError):
    """The send's outcome is unknown: look in Sent before anything else; never resend blind."""


class CalendarError(DorisError):
    """A calendar call failed. A failed read is never 'available'."""


class CalendarTimeout(CalendarError):
    """A calendar call timed out. For a write, the result is unknown: look up by booking ID."""


class AlreadyExists(CalendarError):
    """A create-only write found that booking ID already on the server."""


class NotHers(CalendarError):
    """The booking ID or the event is not one of hers: refused before any call is made."""


class ModelError(DorisError):
    pass
