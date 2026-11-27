"""The gate: the one choke point every send and every booking passes (the "check" step).

The safety claim (locked call 5): assume the model is fully compromised; the gate bounds the
worst it can do. Every rule is code, in this one file, in the order of the gate table in
calendar-rulings-spec.md ("The gate"), G1 to G17.

Fail closed, everywhere:
- every check runs, in table order, and records pass, fail, n/a or error on the `checked` line;
- a check that raises, a ctx callable that raises, or a missing ctx field is `error`, and blocks;
- a callable that answers anything but a plain True or False is `error`, and blocks;
- only an explicit pass from a check counts; a check that applies is never skipped.

The why is gate.pass (n = the checks that applied) or the first failure's code. G1 to G3 stop
her instead of holding one message, so their failure sets GateResult.halt_cause.

Pure: no I/O except through the ctx callables the loop builds. The helper modules (templates,
compose, slots, ics, sensitive, parser) are looked up inside the checks that use them, so a
missing or broken helper errors that check and blocks.
"""

import datetime as _dt
import importlib
import math
import re
from typing import Any, Dict

from . import caps, why
from .shapes import ACTION_KINDS, CLASSES, MODEL_SOURCES, CheckResult, GateResult, Slot

PASS, FAIL, NA, ERROR = "pass", "fail", "n/a", "error"

SENDS = ("send_reply", "send_hold_notice", "send_digest")
OWNER_SENDS = ("send_hold_notice", "send_digest")      # S1 and S2: the owner's address only

# Which template each kind of send may carry. A prospect never gets S1 or S2; the owner's
# notices are never a prospect template. (Cut templates T5, T6, T11, T14 to T17 and K1 to K3
# are on no list; T7 and T8 are never sent.)
TEMPLATES_FOR_KIND = {
    "send_reply": frozenset({"T1", "T2", "T3", "T4", "T9", "T10", "T12", "T13", "T18"}),
    "send_hold_notice": frozenset({"S1"}),
    "send_digest": frozenset({"S2"}),
}
CONFIRMATION = "T1"
NUDGE = "T9"                                            # answers no new message: it used no model
OFFER_TEMPLATES = frozenset({"T2", "T3", "T4", "T9", "T12", "T13", "T18"})   # list 1 to 3 offered times
NEW_OFFER_TEMPLATES = frozenset({"T2", "T3", "T4", "T13", "T18"})           # each one is an offer record
CANCEL_TEMPLATES = frozenset({"T10", "T18"})            # their .ics is the cancel (status cancelled)

# What slots.bookable may answer when a slot is not bookable (G9; why.py's words).
BOOKABLE_RULES = frozenset({"outside_hours", "under_24h", "over_30d", "holiday", "not_quarter_hour", "length"})
B_CONDITIONS = ("B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8", "B9")   # B10 is cut; B11 is the model's reading
BOOKING_PREFIX = "doris-"                               # her booking IDs (the calendar adapter deletes only these)
ICS_NEVER = frozenset({"ATTENDEE", "ORGANIZER"})        # locked call 3: so the calendar server mails nobody
GONE_STATES = frozenset({"cancelled", "gone", "removed"})            # her event is no longer there (G14)
CONFIRMABLE_STATES = frozenset({"written", "confirmed"})             # written and read back (G15)
WRITTEN_STATES = CONFIRMABLE_STATES | GONE_STATES                     # the gate let it be written (G6)
SENSITIVE_CHECKS = frozenset({"sender-domain", "subject-phrase", "one-time-code", "card-number",
                              "bank-routing", "bank-account", "ssn", "reset-or-login-alert"})
UTC = _dt.timezone.utc

# A bare address and nothing else: no display name, no second address, no whitespace or line break.
_BARE_ADDRESS = re.compile(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9-]+(\.[a-z0-9-]+)+")


class _Missing(Exception):
    """A required input is absent: the check cannot run (error, blocks)."""


class _OffContract(Exception):
    """A callable or helper answered outside its contract: the check cannot trust it (error, blocks)."""


# --- small, strict readers ---------------------------------------------------------------
def _need(ctx, name):
    """A ctx field the check cannot run without. Absent or None is an error, never a default."""
    try:
        value = getattr(ctx, name)
    except AttributeError:
        raise _Missing(name)
    if value is None:
        raise _Missing(name)
    return value


def _yes_no(value, what):
    """A plain True or False. Anything else (None, 0, "yes") is off contract."""
    if value is True or value is False:
        return value
    raise _OffContract(what)


def _field(ctx, name):
    """A ctx field that may be None (no answer, no message). Absent altogether is an error."""
    try:
        return getattr(ctx, name)
    except AttributeError:
        raise _Missing(name)


def _address(value):
    """Addresses compare lowercased, as mail.py stores them; nothing else is cleaned up."""
    if not isinstance(value, str):
        raise _OffContract("address")
    return value.lower()


def _helper(name):
    """A helper module, looked up at the moment of the check: missing or broken, the check errors."""
    return importlib.import_module("." + name, __package__)


def _aware(value):
    return isinstance(value, _dt.datetime) and value.tzinfo is not None and value.utcoffset() is not None


def _offered_times(action):
    """The times a reply offers, in the template's own slots as templates.render takes them:
    "slots" (a list of UTC start times) or, for T13, "slot" (one). None when it offers none."""
    if action.kind != "send_reply":
        return None
    if "slots" in action.slots:
        return action.slots["slots"]
    if "slot" in action.slots:
        return [action.slots["slot"]]
    return None


def _times_ok(times):
    """1 to 3 offered times (the cap in caps.py), each an aware datetime."""
    return (isinstance(times, (list, tuple)) and 1 <= len(times) <= caps.SLOTS_PER_OFFER
            and all(_aware(t) for t in times))


def _offer_length(action):
    """Each offered slot's length in minutes: the template's own "duration". T12 re-sends the open
    offer and states no length, so for it the loop puts the open offer's length_min in evidence."""
    if "duration" in action.slots:
        length = action.slots["duration"]
    elif "length_min" in action.evidence:
        length = action.evidence["length_min"]
    else:
        raise _Missing("length_min")
    if isinstance(length, bool) or not isinstance(length, int):
        raise _OffContract("length")
    return length


def _now(ctx):
    now = _need(ctx, "now")
    if not _aware(now):
        raise _OffContract("now")
    return now


def _span(action):
    """The interval a booking writes (and a cancel names)."""
    if not isinstance(action.span, Slot):
        raise _Missing("span")
    return action.span


def _booking_id(action):
    if not isinstance(action.booking_id, str) or not action.booking_id:
        raise _Missing("booking_id")
    return action.booking_id


def _iso(value):
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pass(**detail):
    return PASS, detail


def _fail(**detail):
    return FAIL, detail


# --- G1 to G3: everything -----------------------------------------------------------------
def _g1_not_halted(action, ctx):
    """G1. Not halted: no halt is in force (cli, stop_file, watch_button or budget_cap).
    Nothing in mail halts her. Re-read from disk at this moment, never cached (ruling 1)."""
    cause = _need(ctx, "halt_cause")()
    if cause is None:
        return _pass(halt="none")
    if cause not in why.HALT_CAUSES:
        raise _OffContract("halt_cause")
    return _fail(halt=cause, rule=cause)


def _g2_sole_runner(action, ctx):
    """G2. Sole runner: this process holds the ledger lock for its mode (ruling 8)."""
    if _yes_no(_need(ctx, "holds_lock")(), "holds_lock"):
        return _pass(lock="held")
    return _fail(lock="not_held")


def _g3_under_budget(action, ctx):
    """G3. Under budget: model spend so far is under the hard cap in the caps file. Reaching
    the cap stops the run with cause budget_cap; only the CLI resumes her (locked call 9)."""
    spend = _need(ctx, "spend_usd")()
    if isinstance(spend, bool) or not isinstance(spend, (int, float)) or not math.isfinite(spend) or spend < 0:
        raise _OffContract("spend_usd")
    detail = {"spend_usd": round(float(spend), 6), "cap_usd": caps.BUDGET_USD_PER_RUN}
    if spend < caps.BUDGET_USD_PER_RUN:
        return _pass(**detail)
    return _fail(**detail)


# --- G4 to G8: who may hear from her, how often, and in whose words --------------------------
def _g4_model_there(action, ctx):
    """G4. The model is there: anything that used the model has its parsed answer. While the
    model is down (its last call failed after the one retry) nothing goes to a prospect, nudges
    included. Hold notices and the digest still reach the owner (locked call 12)."""
    if not _yes_no(_need(ctx, "model_ok")(), "model_ok"):
        return _fail(model="down")
    if action.template == NUDGE:
        return _pass(model="up", answer="not_used")
    answer = _field(ctx, "model_answer")
    if (answer is None or answer.ok is not True or answer.cls not in CLASSES
            or answer.source not in MODEL_SOURCES):
        return _fail(model="up", answer="missing")
    return _pass(model="up", answer="parsed", source=answer.source)


def _g5_not_silenced(action, ctx):
    """G5. Not silenced: no recipient is on the stop list, read fresh now (ruling 9)."""
    recipients = [_address(a) for a in list(action.to) + list(action.bcc)]
    if not recipients:
        raise _Missing("recipients")
    silenced = _need(ctx, "silenced")
    for addr in recipients:
        if _yes_no(silenced(addr), "silenced"):
            return _fail(recipients=len(recipients), silenced=1)
    return _pass(recipients=len(recipients), silenced=0)


def _no_reply_kind(msg):
    """Not list, bounce or auto mail (ruling 11), read from the headers in code. Screen 7 files
    these first; this is the gate's own reading of the standard headers."""
    headers = {str(k).lower(): str(v).strip().lower() for k, v in msg.headers.items()}
    if _yes_no(msg.is_report, "is_report") or headers.get("return-path") == "<>" or "x-failed-recipients" in headers:
        return "bounce"
    if (headers.get("auto-submitted", "no").split(";")[0].strip() != "no"
            or headers.get("precedence") == "auto_reply" or "x-autoreply" in headers or "x-autorespond" in headers):
        return "auto"
    if headers.get("precedence") in ("bulk", "list", "junk") or "list-id" in headers or "list-unsubscribe" in headers:
        return "list"
    return None


def _ruling18(msg, sender, owner, doris, own_thread):
    """Ruling 18, as Shane ruled it, on the message's own headers: if the owner is in To with only
    the sender, branch 1; else if she is in To on her own thread with only the sender, branch 2;
    anything else is never answered. The owner and Doris never count as someone else.
    Returns (branch, None) or (None, the rule word for why it is not answered)."""
    to = {_address(a) for a in msg.to}
    cc = {_address(a) for a in msg.cc}
    others = (to | cc) - {sender, owner, doris}
    if owner in to:
        return (1, None) if not others else (None, "multiple")
    if doris in to:
        if not own_thread:
            return None, ("forged_thread" if (msg.in_reply_to or msg.references) else "direct_new_thread")
        return (2, None) if not others else (None, "multiple")
    return None, ("cc_only" if (owner in cc or doris in cc) else "bcc")


def _addresses_she_wrote_to(action, ctx, facts):
    """On her own thread: the addresses she wrote to, the address her offer went to, and the
    sender of a booking of hers on it that was written. A record still at intent was made from a
    message the gate had not yet cleared, so it is no evidence of who she wrote to, and for a
    booking no record is (its own is at intent, and any other fails G12)."""
    found = {_address(a) for a in (facts.get("written_to") or ())}
    if facts["offer_to"] is not None:
        found.add(_address(facts["offer_to"]))
    if action.kind == "calendar_create":
        return found
    records = [_need(ctx, "live_booking_for_thread")(action.thread_key)]
    if action.booking_id:
        records.append(_need(ctx, "booking")(action.booking_id))
    for record in records:
        if record is not None and record["state"] in WRITTEN_STATES:
            found.add(_address(record["sender"]))
    return found


def _g6_right_recipient(action, ctx):
    """G6. Right recipient (rulings 11, 15, 18, locked call 9, forwarding). For a booking, move or
    cancel all of it is checked before the calendar write, so nothing is written for a message
    she could not answer."""
    owner = _address(_need(ctx, "owner_addr"))
    doris = _address(_need(ctx, "doris_addr"))
    to = [_address(a) for a in action.to]
    bcc = [_address(a) for a in action.bcc]
    # Never her own address.
    if doris in to or doris in bcc:
        return _fail(rule="self")
    # S1 and S2 go to the owner's address only.
    if action.kind in OWNER_SENDS:
        if action.recipient_source != "owner" or to != [owner] or bcc:
            return _fail(rule="owner", recipient_source=str(action.recipient_source))
        return _pass(recipient_source="owner")
    msg = _need(ctx, "message")
    facts = _need(ctx, "screen_facts")
    own_thread = _yes_no(facts["own_thread"], "own_thread")
    sender = _address(msg.from_addr)
    reply_to = [_address(a) for a in msg.reply_to]
    offer_to = None if facts["offer_to"] is None else _address(facts["offer_to"])
    detail = {"from": sender, "reply_to": reply_to, "offer_to": offer_to, "own_thread": own_thread}
    # The message being answered is the one in hand, from a bare address.
    if msg.key != action.message_key or action.recipient_source != "sender" or not _BARE_ADDRESS.fullmatch(sender):
        return _fail(rule="sender", **detail)
    if action.kind == "send_reply":
        # The one To address is the original From of the message being answered, on its thread.
        if to != [sender]:
            return _fail(rule="sender", **detail)
        # The one BCC is the owner's address from config, never from a message.
        if bcc != [owner]:
            return _fail(rule="owner", **detail)
    # Not her own address; not the owner's own mail (ruling 1); not list, bounce or auto mail.
    if sender == doris:
        return _fail(rule="self", **detail)
    if sender == owner:
        return _fail(rule="owner", **detail)
    kind = _no_reply_kind(msg)
    if kind:
        return _fail(rule=kind, **detail)
    # The message passes ruling 18, read here from its headers and agreed by the screens.
    branch, word = _ruling18(msg, sender, owner, doris, own_thread)
    detail.update(branch=branch if branch else "else", screens_branch=str(facts["branch"]))
    if branch is None:
        return _fail(rule=word, **detail)
    if str(facts["branch"]) not in ("1", "2"):
        return _fail(rule="none", **detail)
    # Reply-To is absent or identical to From (ruling 15).
    if any(a != sender for a in reply_to):
        return _fail(rule="sender", **detail)
    # A pick, and anything else answered on her own thread, comes from the address she wrote to.
    if own_thread:
        wrote_to = _addresses_she_wrote_to(action, ctx, facts)
        if wrote_to != {sender}:
            detail["wrote_to"] = "unknown" if not wrote_to else "another_address"
            return _fail(rule="sender", **detail)
    return _pass(recipient_source="sender", **detail)


SEND_CAPS = (("sends_hour", caps.SENDS_PER_HOUR), ("sends_run", caps.SENDS_PER_RUN),
             ("sends_thread_day", caps.SENDS_PER_THREAD_PER_DAY), ("sends_thread_ever", caps.SENDS_PER_THREAD_EVER))


def _g7_under_caps(action, ctx):
    """G7. Under caps: per hour, per run, per thread per day, per thread total, and two offers per
    thread, ever (ruling 6, locked call 9). Counts are what is already done, so this one must fit."""
    counts = _need(ctx, "counts")(action.thread_key)
    if action.kind == "send_reply":
        word, limits = "send", list(SEND_CAPS)
        if action.template in NEW_OFFER_TEMPLATES:
            limits.append(("offers_thread", caps.OFFERS_PER_THREAD_EVER))
    elif action.kind == "send_hold_notice":
        word, limits = "hold_notice", [("hold_notices_hour", caps.HOLD_NOTICES_PER_HOUR)]
    else:
        word, limits = "booking", [("calendar_writes_run", caps.CALENDAR_WRITES_PER_RUN)]
    seen = {}
    for name, limit in limits:
        count = counts[name]
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise _OffContract("counts." + name)
        if count >= limit:
            return _fail(rule=word, cap=name, count=count, limit=limit)
        seen[name] = count
    return _pass(**seen)


def _g8_approved_template(action, ctx):
    """G8. Approved template, locked signature (rulings 10, 11, locked calls 4 and 10): the template
    id is on the approved page; every slot is typed (the text is rendered again here, by code, from
    the slots); at most 3 times are listed; and the message is that text, then the two locked
    signature lines added by the adapter, compared byte for byte, and nothing else."""
    templates = _helper("templates")
    compose = _helper("compose")
    text = action.body_text
    if not isinstance(text, str):
        raise _Missing("body_text")
    template = action.template
    if template not in templates.APPROVED or template not in TEMPLATES_FOR_KIND[action.kind]:
        return _fail(template=str(template), approved=False)
    offered = _offered_times(action)
    if template in OFFER_TEMPLATES or offered is not None:
        if not _times_ok(offered):
            return _fail(template=template, offered="not_1_to_%d_times" % caps.SLOTS_PER_OFFER)
    try:
        rendered = templates.render(template, action.slots)
    except templates.TemplateError:
        return _fail(template=template, rendered=False)
    signature = compose.SIGNATURE
    if not isinstance(rendered, str) or not rendered or not isinstance(signature, str) or not signature:
        raise _OffContract("render")
    exact = (text.startswith(rendered) and text.endswith(signature)
             and len(text) >= len(rendered) + len(signature)
             and text[len(rendered):len(text) - len(signature)].strip() == ""
             and _yes_no(compose.signature_ok(text), "signature_ok"))
    if not exact:
        return _fail(template=template, body="not_the_approved_text")
    return _pass(template=template, offered=len(offered) if offered else 0, signature="locked")


# --- G9 to G13: what goes on the calendar ------------------------------------------------------
def _open_offer_slots(action, ctx):
    """The slots of the open offers on the action's thread that went to the sender of the message
    being answered (an offer is one sender's: same sender, same thread), from the ledger, as (start,
    end) pairs. Anything but a collection of Slots with aware times is off contract: the check cannot run."""
    sender = _address(_need(ctx, "message").from_addr)
    answer = _need(ctx, "open_offer_slots")(action.thread_key, sender)
    if not isinstance(answer, (set, frozenset, list, tuple)):
        raise _OffContract("open_offer_slots")
    pairs = set()
    for slot in answer:
        if not isinstance(slot, Slot) or not _aware(slot.start_utc) or not _aware(slot.end_utc):
            raise _OffContract("open_offer_slots")
        pairs.add((slot.start_utc, slot.end_utc))
    return pairs


def _written_starts(ctx, open_pairs):
    """The starts G9 reads as times the sender wrote: the parser's kept readings of the message
    being answered, and every start of an open offer to that sender on the thread (each passed G9
    when offered). A derived reading (D5 moving a time onto a weekday written apart from it) is no
    time the sender wrote; a reading that does not say it is not derived counts as derived."""
    starts = {start for start, _ in open_pairs}
    parse = _field(ctx, "parse")
    if parse is not None:
        starts.update(r.start_utc for r in parse.readings
                      if r.kept is True and getattr(r, "derived", None) is False and _aware(r.start_utc))
    return starts


def _g9_bookable_interval(action, ctx):
    """G9. Bookable interval (rulings 3, 5, 6): start and end inside Mon to Fri 9:00 AM to 5:00 PM
    Pacific, start 24 hours to 30 days from now, not on the holiday list, length allowed. A time the
    sender wrote may start on any quarter hour: a direct booking, the parser's kept readings offered
    back to them (never a derived one: a time a weekday written on its own took from another day), and
    a start an open offer to them on the thread already holds. A slot she offers on
    her own starts on the hour or half hour. A pick (any booking not marked direct) books only a slot
    an open offer to its sender on its thread holds, exactly as offered, and keeps the grid it was
    offered on. Every offered slot is checked, not just the first. The receipt says written for a
    direct booking only: a pick is checked on its offer's grid, but it is no time the sender wrote."""
    slots = _helper("slots")
    now = _now(ctx)
    if action.kind == "calendar_create":
        span = _span(action)
        if action.direct is not True:
            if (span.start_utc, span.end_utc) not in _open_offer_slots(action, ctx):
                return _fail(rule="not_offered", start=_iso(span.start_utc), written=False)
        to_check = [(span, True, action.direct is True)]
    else:
        offered = _offered_times(action)
        if not _times_ok(offered):
            raise _Missing("offered_times")
        length = _dt.timedelta(minutes=_offer_length(action))
        written = _written_starts(ctx, _open_offer_slots(action, ctx))
        to_check = [(Slot(start, start + length), start in written, start in written) for start in offered]
    for slot, as_written, said in to_check:
        ok, rule = slots.bookable(slot, now, as_written)
        if ok is True:
            continue
        if ok is False and rule in BOOKABLE_RULES:
            return _fail(rule=rule, start=_iso(slot.start_utc), written=said)
        raise _OffContract("bookable")
    return _pass(checked=len(to_check), written=to_check[0][2])


def _g10_direct_booking_evidence(action, ctx):
    """G10. Direct-booking evidence (ruling 2), for a direct booking only: the parser's verdict is
    "book", with B1 to B9 each recorded as passed (B10 is cut); the booking is at that one parsed
    instant; and the model's own answer gives the same date, 24-hour time, zone and instant (B11).
    The model's input holds no parser output by construction: model.build_request takes the
    message alone (the gate cannot see the request itself)."""
    parser = _helper("parser")
    parse = _need(ctx, "parse")
    answer = _need(ctx, "model_answer")
    span = _span(action)
    for cond in B_CONDITIONS:
        if parse.conditions.get(cond) is not True:
            return _fail(cond=cond)
    if parse.verdict != "book" or parse.failed_condition is not None:
        return _fail(cond=parse.failed_condition if parse.failed_condition in B_CONDITIONS else "B7",
                     verdict=str(parse.verdict))
    if parse.instant_utc is None or parse.instant_utc != span.start_utc:
        return _fail(cond="B7", instant="not_the_parsed_instant")
    agrees, _ = parser.model_agrees(parse, answer)
    if not _yes_no(agrees, "model_agrees"):
        return _fail(cond="B11", model="disagrees")
    return _pass(verdict="book", conditions="B1-B9", model="agrees", instant=_iso(span.start_utc))


def _blocks(event, span):
    """Ruling 3: busy is any event that overlaps the span and isn't cancelled. Marked free,
    tentative, all-day and repeating events all count; back to back is fine. An all-day event (and
    one in a zone she has no rule for) is busy all of its Pacific days. One she cannot place is busy."""
    if str(event.status).upper() == "CANCELLED":
        return False
    if event.kind == "timed" and _aware(event.start_utc) and _aware(event.end_utc):
        return event.start_utc < span.end_utc and event.end_utc > span.start_utc
    if (event.kind in ("all_day", "unknown_zone") and isinstance(event.start_date, _dt.date)
            and isinstance(event.end_date, _dt.date)):
        # Pacific midnight is 07:00 UTC in daylight time and 08:00 UTC in standard time: take the wider.
        first = _dt.datetime.combine(event.start_date, _dt.time(7, 0), tzinfo=UTC)
        after_last = _dt.datetime.combine(event.end_date, _dt.time(8, 0), tzinfo=UTC)
        return first < span.end_utc and after_last > span.start_utc
    return True


def _g11_fresh_recheck(action, ctx):
    """G11. Fresh re-check (rulings 3, 4): a calendar read made at this moment succeeded and shows
    no overlapping event that isn't cancelled. A failed read is never "free": it errors and blocks."""
    span = _span(action)
    events = _need(ctx, "fresh_read")(span)
    if not isinstance(events, (list, tuple)):
        raise _OffContract("fresh_read")
    overlapping = sum(1 for event in events if _blocks(event, span))
    if overlapping:
        return _fail(read="ok", events=len(events), overlapping=overlapping)
    return _pass(read="ok", events=len(events), overlapping=0)


def _g12_ledger_first(action, ctx):
    """G12. Ledger first (rulings 4, 7, 8): her booking ID and the inbound Message-ID are in the
    ledger before the write; the write is create-only, so this ID was never written; and the
    thread has no other live booking."""
    booking_id = _booking_id(action)
    if not booking_id.startswith(BOOKING_PREFIX):
        return _fail(booking="not_her_id")
    record = _need(ctx, "booking")(booking_id)
    if record is None:
        return _fail(booking="not_in_ledger")
    if record["message_key"] != action.message_key or record["thread_key"] != action.thread_key:
        return _fail(booking="another_message")
    if record["state"] != "intent":
        return _fail(booking="already_" + str(record["state"]))
    live = _need(ctx, "live_booking_for_thread")(action.thread_key)
    if live is not None and live["booking_id"] != booking_id:
        return _fail(booking="thread_has_another")
    return _pass(booking="intent", create_only=True)


def _ics_property_names(data):
    """Property names of an .ics, after RFC 5545 unfolding (a line break then a space or tab)."""
    text = re.sub(r"\r?\n[ \t]", "", data.decode("utf-8"))
    return {re.split(r"[;:]", line, maxsplit=1)[0].strip().upper() for line in text.splitlines() if line.strip()}


def _g13_fixed_ics(action, ctx):
    """G13. Fixed .ics (ruling 10): the event written and the .ics attached carry only the fixed
    fields (start, end, the template title, her booking ID, the meeting link from .env as the
    location, and the format's housekeeping). The gate also reads, itself, that there is no
    attendee and no organizer (locked call 3), and that the file is this action's own booking, at
    its own time, confirmed or (for a cancel) cancelled."""
    ics = _helper("ics")
    room = _helper("templates").meeting_link()
    data = action.ics
    if not isinstance(data, bytes) or not data:
        raise _Missing("ics")
    if not _yes_no(ics.fields_ok(data, meeting_link=room), "fields_ok"):
        return _fail(fields="not_fixed")
    if _ics_property_names(data) & ICS_NEVER:
        return _fail(fields="attendee_or_organizer")
    event = ics.read_event(data)
    if event.uid != _booking_id(action):
        return _fail(fields="fixed", uid="not_this_booking")
    span = _span(action) if action.kind == "calendar_create" else action.span
    if span is not None and (event.start_utc != span.start_utc or event.end_utc != span.end_utc):
        return _fail(fields="fixed", uid="this_booking", times="not_this_span")
    status = "CANCELLED" if action.template in CANCEL_TEMPLATES else "CONFIRMED"
    if str(event.status).upper() != status:
        return _fail(fields="fixed", uid="this_booking", ics_status="not_" + status.lower())
    return _pass(fields="fixed", uid="this_booking", ics_status=status.lower())


# --- G14 to G17: her own events, read-backs, the hard rule, sensitive text -----------------------
def _g14_her_own_event(action, ctx):
    """G14. Her own event (ruling 7): for a cancel or a move, and her removal after a collision
    (ruling 4), the booking ID is hers, in her ledger, booked for this sender on this thread, and
    not already cancelled, gone or removed. Never anyone else's event."""
    booking_id = _booking_id(action)
    if not booking_id.startswith(BOOKING_PREFIX):
        return _fail(booking="not_her_id")
    sender = _address(_need(ctx, "message").from_addr)
    record = _need(ctx, "booking")(booking_id)
    if record is None:
        return _fail(booking="not_in_ledger")
    if record["thread_key"] != action.thread_key:
        return _fail(booking="another_thread")
    if _address(record["sender"]) != sender:
        return _fail(booking="another_sender")
    if record["state"] in GONE_STATES:
        return _fail(booking=str(record["state"]))
    return _pass(booking="hers", state=str(record["state"]))


def _g15_clean_readback(action, ctx):
    """G15. Clean read-back (ruling 4): after the write her event was read back, matched, and nothing
    else overlapped it. Until then no confirmation is sent, and a booking marked
    collision_unresolved is never confirmed, restart included."""
    booking_id = _booking_id(action)
    if not _yes_no(_need(ctx, "readback_clean")(booking_id), "readback_clean"):
        return _fail(readback="not_clean")
    record = _need(ctx, "booking")(booking_id)
    if record is None or record["thread_key"] != action.thread_key:
        return _fail(readback="clean", booking="not_in_ledger")
    if record["state"] not in CONFIRMABLE_STATES:
        return _fail(readback="clean", booking=str(record["state"]))
    return _pass(readback="clean", booking=str(record["state"]))


def _g16_nothing_opened_nobody_new(action, ctx):
    """G16. Nothing opened, nobody new (the hard rule): every attachment of this message is listed
    "not inspected", with no exception; the action is one of the fixed kinds, so it goes to her
    mail server or her calendar server and nowhere else; and every recipient is the sender or the
    owner, never an address or a number taken from a body. A calendar write has no recipients."""
    if action.kind not in ACTION_KINDS:
        return _fail(kind="not_a_fixed_kind")
    host = "mail" if action.kind in SENDS else "calendar"
    msg = _field(ctx, "message")
    attachments = list(msg.attachments) if msg is not None else []
    links = msg.links_seen if msg is not None else 0
    if isinstance(links, bool) or not isinstance(links, int):
        raise _OffContract("links_seen")
    detail = {"opened": sum(1 for a in attachments if a.inspected is not False), "attachments": len(attachments),
              "links_seen": links, "host": host, "recipient_source": str(action.recipient_source)}
    if detail["opened"]:
        return _fail(**detail)
    allowed = set()
    if host == "mail":
        allowed.add(_address(_need(ctx, "owner_addr")))
        if msg is not None:
            allowed.add(_address(msg.from_addr))
    recipients = [_address(a) for a in list(action.to) + list(action.bcc)]
    if action.recipient_source not in ("sender", "owner") or any(r not in allowed for r in recipients):
        return _fail(nobody_new=False, **detail)
    return _pass(**detail)


def _strings(value):
    """Every piece of text in a slot value, inside lists, tuples and dicts too."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)


def _check_name(found):
    """Only the name of the sensitive check that matched goes on the receipt, never the text."""
    return found if found in SENSITIVE_CHECKS else "match"


def _g17_nothing_sensitive(action, ctx):
    """G17. Nothing sensitive (the sensitive-mail rule): the message being acted on passes the
    sensitive screen again, here, and every piece of outgoing text (the body, and each template
    slot: hold-notice quotes, digest lines) passes the same pattern checks. A match blocks."""
    sensitive = _helper("sensitive")
    msg = _field(ctx, "message")
    if msg is not None:
        found = sensitive.scan_message(msg)
        if found is not None:
            return _fail(message=_check_name(found))
    texts = []
    if action.kind in SENDS:
        if not isinstance(action.body_text, str):
            raise _Missing("body_text")
        texts = [action.body_text] + list(_strings(action.slots))
    for text in texts:
        found = sensitive.scan_text(text)
        if found is not None:
            return _fail(message="clean" if msg is not None else "none", outgoing=_check_name(found))
    return _pass(message="clean" if msg is not None else "none", outgoing="clean", texts=len(texts))


# --- what each check applies to, in the table's own words -----------------------------------
def _everything(action):
    return True


def _sends_to_prospects_and_bookings(action):
    return action.kind in ("send_reply", "calendar_create")


def _sends(action):
    return action.kind in SENDS


def _sends_bookings_moves_cancels(action):
    return action.kind in ACTION_KINDS


def _capped_sends_and_bookings(action):
    # The caps file sets no cap on the digest, and a delete is not a booking: n/a, never a free pass.
    return action.kind in ("send_reply", "send_hold_notice", "calendar_create")


def _bookings_and_offered_slots(action):
    return action.kind == "calendar_create" or (
        action.kind == "send_reply" and (action.template in OFFER_TEMPLATES or _offered_times(action) is not None))


def _direct_bookings(action):
    # Only an explicit False makes a booking a pick: anything else gets the direct-booking evidence check.
    return action.kind == "calendar_create" and action.direct is not False


def _bookings(action):
    return action.kind == "calendar_create"


def _bookings_and_cancels(action):
    # The event written, and the .ics a confirmation or a cancel carries.
    return action.kind == "calendar_create" or action.ics is not None


def _cancels_and_moves(action):
    return action.kind == "calendar_delete"


def _confirmations(action):
    return action.kind == "send_reply" and action.template == CONFIRMATION


def _sends_and_bookings(action):
    return action.kind in SENDS or action.kind == "calendar_create"


# The table: id, applies to, the rule. check() walks it top to bottom.
TABLE = (
    ("G1", _everything, _g1_not_halted),
    ("G2", _everything, _g2_sole_runner),
    ("G3", _everything, _g3_under_budget),
    ("G4", _sends_to_prospects_and_bookings, _g4_model_there),
    ("G5", _sends, _g5_not_silenced),
    ("G6", _sends_bookings_moves_cancels, _g6_right_recipient),
    ("G7", _capped_sends_and_bookings, _g7_under_caps),
    ("G8", _sends, _g8_approved_template),
    ("G9", _bookings_and_offered_slots, _g9_bookable_interval),
    ("G10", _direct_bookings, _g10_direct_booking_evidence),
    ("G11", _bookings, _g11_fresh_recheck),
    ("G12", _bookings, _g12_ledger_first),
    ("G13", _bookings_and_cancels, _g13_fixed_ics),
    ("G14", _cancels_and_moves, _g14_her_own_event),
    ("G15", _confirmations, _g15_clean_readback),
    ("G16", _everything, _g16_nothing_opened_nobody_new),
    ("G17", _sends_and_bookings, _g17_nothing_sensitive),
)

# Each check's why code, and the slot its detail fills (why.py; slots are typed words or ids).
WHY_CODES = {
    "G1": ("gate.g1_halted", "rule"),
    "G2": ("gate.g2_not_sole_runner", None),
    "G3": ("gate.g3_budget", None),
    "G4": ("gate.g4_model_missing", None),
    "G5": ("gate.g5_silenced", None),
    "G6": ("gate.g6_recipient", "rule"),
    "G7": ("gate.g7_cap", "rule"),
    "G8": ("gate.g8_template", None),
    "G9": ("gate.g9_not_bookable", "rule"),
    "G10": ("gate.g10_evidence", "cond"),
    "G11": ("gate.g11_overlap", None),
    "G12": ("gate.g12_ledger", None),
    "G13": ("gate.g13_ics", None),
    "G14": ("gate.g14_not_hers", None),
    "G15": ("gate.g15_readback", None),
    "G16": ("gate.g16_opened", None),
    "G17": ("gate.g17_sensitive", None),
}

STOPPING_CHECKS = ("G1", "G2", "G3")


def _run_one(check_id, applies, rule, action, ctx):
    """One row of the table. Any exception, or an answer that is not a plain pass or fail, is error."""
    try:
        if not applies(action):
            return CheckResult(check_id, NA)
        status, detail = rule(action, ctx)
        if status not in (PASS, FAIL) or not isinstance(detail, dict):
            raise _OffContract(check_id)
        return CheckResult(check_id, status, detail)
    except _Missing as exc:
        return CheckResult(check_id, ERROR, {"missing": str(exc)})
    except _OffContract as exc:
        return CheckResult(check_id, ERROR, {"error": "off_contract", "what": str(exc)})
    except Exception as exc:  # fail closed: a check that cannot run blocks
        return CheckResult(check_id, ERROR, {"error": type(exc).__name__})


def _why_for(first, applied):
    if first is None:
        return why.why("gate.pass", n=applied)
    if first.status == ERROR:
        return why.why("gate.check_failed_to_run", check=first.check)
    code, slot = WHY_CODES[first.check]
    try:
        return why.why(code, **({slot: first.detail[slot]} if slot else {}))
    except (KeyError, why.WhyError):  # a detail the why list cannot carry: still blocked, named by check
        return why.why("gate.check_failed_to_run", check=first.check)


def _halt_cause(first):
    """G1 to G3 stop her. The cause is a halt cause when one is known (G1's own, G3's budget_cap);
    otherwise the id of the check that could not confirm she may go on."""
    if first is None or first.check not in STOPPING_CHECKS:
        return None
    if first.status == FAIL and first.check == "G1":
        return first.detail["halt"]
    if first.status == FAIL and first.check == "G3":
        return "budget_cap"
    return first.check


def check(action, ctx) -> GateResult:
    """Run G1 to G17 in table order on one action. Every check runs and is recorded; the action
    passes only when every check that applies passed."""
    results = [_run_one(check_id, applies, rule, action, ctx) for check_id, applies, rule in TABLE]
    first = next((r for r in results if r.status in (FAIL, ERROR)), None)
    applied = sum(1 for r in results if r.status == PASS)
    return GateResult(
        passed=first is None and applied > 0,
        results=results,
        first_fail=first.check if first else None,
        why=_why_for(first, applied),
        halt_cause=_halt_cause(first),
    )


def receipt_payload(result: GateResult) -> Dict[str, Any]:
    """The `checked` line's payload: every check's status and detail, in table order (JSON-safe)."""
    checks = {}
    for r in result.results:
        entry = dict(r.detail)
        entry["status"] = r.status
        checks[r.check] = entry
    return {"passed": result.passed, "first_fail": result.first_fail, "halt_cause": result.halt_cause,
            "checks": checks}
