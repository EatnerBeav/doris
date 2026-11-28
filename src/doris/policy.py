"""The policy table (decide). The model classifies; code decides; the model never picks an action.

`decide(facts) -> Plan` is one pure function, read top to bottom in the spec's order:
a held thread first, then the holds code finds on its own (decision 59: a hold found by
code beats every model class, and code can force a hold, never a send), then the
model's class, one row at a time. No I/O. The loop gathers the facts (screens, the
model's answer, the parser, the thread's state from the ledger) and acts on the plan
through the gate. Slots are computed by the loop from the calendar; `offer_form` picks
the template once their number is known, and `on_unavailable` turns a booking whose
slot failed its re-check into the offer the table gives for that case.
"""

import datetime as _dt
import re
from dataclasses import dataclass, field, replace
from typing import FrozenSet, List, Optional, Tuple

from .why import why as _why

ACTIONS = ("book", "offer", "resend", "cancel", "move", "file", "silent", "suppress", "hold", "close",
           "thread_held")

# Classes she would act on. Group mail and mail on her thread from an address she never
# wrote to hold these (rulings 15 and 18); what she would only file is filed.
FILE_ONLY = frozenset({"fyi", "injection_only", "auto_reply"})

HELD_CLASSES = {
    "money": "hold.money",
    "legal": "hold.legal",
    "complaint": "hold.complaint",
    "add_person": "hold.add_person",
    "unsure": "hold.unsure",
    "other": "hold.outside_classes",
}

# Ruling 5: the length rules that hold, and their reasons.
LENGTH_HOLDS = {
    "over_60": "hold.longer_than_hour",
    "open_ended": "hold.longer_than_hour",
    "all_day": "hold.all_day_recurring_multiple",
    "recurring": "hold.all_day_recurring_multiple",
    "multiple": "hold.all_day_recurring_multiple",
    "zero": "hold.all_day_recurring_multiple",
}


@dataclass
class ThreadState:
    """What the ledger knows about the thread this message is on."""
    thread_key: str
    own_thread: bool = False            # its reply headers name a Message-ID in her Sent ledger
    held: bool = False                  # a held thread stays Shane's: nothing more is sent on it
    offer_count: int = 0                # offer records on this thread, ever (two at most)
    open_offers: List[dict] = field(default_factory=list)   # to this sender; not used, closed, expired: id, slots...
    live_booking: Optional[dict] = None  # her standing booking on this thread: booking_id, slot, sender
    gone_booking: bool = False          # a booking here was marked gone (ruling 17)
    written_to: FrozenSet[str] = frozenset()   # addresses she has sent to on this thread
    offer_to: Optional[str] = None      # the address her open offer went to


@dataclass
class Facts:
    sender: str                         # the From address, lowercased
    branch: str                         # ruling 18: "1", "2" or "else"
    others_on_thread: int               # people on it besides the sender, the owner and Doris
    thread: ThreadState
    answer: object                      # shapes.ModelAnswer
    parse: object                       # shapes.ParseResult, or None when nothing was parsed
    model_agrees: bool                  # parser.model_agrees (B11)
    pick: object                        # shapes.PickResult when the thread has open offers, else None
    money_code: Optional[str]           # money_check() on the sender's own text: "currency", "word" or None
    reply_to_mismatch: bool             # a Reply-To that differs from From (ruling 15)
    # What this message already did before a kill or a stop (ruling 8, A3); the loop fills these
    # only for a message read again, from the ledger and Sent.
    own_hold: Optional[str] = None      # the hold code it was already held with
    own_reply: Optional[str] = None     # the template of its reply already in Sent
    own_reply_unknown: Optional[str] = None   # the template of its reply Sent could not be asked about
    own_booking: Optional[dict] = None  # its booking written down: slot, direct, offer_id, length_min
    arrived: Optional[_dt.datetime] = None   # when it reached her, as the mail server handed it over


@dataclass
class Plan:
    action: str
    outcome: str                        # the closing line's word (A.2)
    why: dict
    template: Optional[str] = None      # set for resend (T12) and cancel (T10); offers get theirs from offer_form
    hold_code: Optional[str] = None
    slot: Optional[object] = None       # shapes.Slot to book
    direct: bool = False                # a direct booking (G10 applies)
    offer_id: Optional[int] = None      # the offer a pick or yes books from
    offer_family: Optional[str] = None  # T2 | T3 | T4 | T18
    exclude_offered: bool = False       # times already offered on the thread never come back
    first: Tuple = ()                   # the sender's own bookable times, offered first
    derived: Tuple = ()                 # D5's times on a weekday written apart from them: on her grid, after
    asked: Optional[_dt.datetime] = None  # T4's [asked time]
    length_min: int = 30
    side_question: Optional[str] = None  # ruling 14: code-copied text for the digest's "Also asked" line
    booking_id: Optional[str] = None    # cancel and move
    pick_kind: Optional[str] = None


def _hold(code, **slots):
    return Plan(action="hold", outcome="hold:" + code[len("hold."):], why=_why(code, **slots), hold_code=code)


def _length(parse):
    return getattr(parse, "length_min", 30) or 30


def _own_readings(parse):
    """The sender's own placed readings that the parser kept, in order, for offering first. A derived
    reading (D5 moving a time onto a weekday written apart from it) is never the sender's own."""
    if parse is None:
        return ()
    return tuple(r.start_utc for r in parse.readings
                 if r.kept and r.start_utc is not None and not getattr(r, "derived", False))


def _derived_readings(parse):
    """D5's derived readings the parser kept: offered after the sender's own, on her own grid."""
    if parse is None:
        return ()
    return tuple(r.start_utc for r in parse.readings
                 if r.kept and r.start_utc is not None and getattr(r, "derived", False))


def _offer(parse, model_agrees, cond=None):
    """A new request she can't book directly: T2, with the bookable readings first."""
    if cond is None:
        if parse is not None and parse.verdict == "book" and not model_agrees:
            cond = "B11"
        else:
            cond = (parse.failed_condition if parse is not None else None) or "B7"
    return Plan(action="offer", outcome="offer", why=_why("policy.offer_no_clear_time", cond=cond),
                offer_family="T2", first=_own_readings(parse), derived=_derived_readings(parse),
                length_min=_length(parse))


def _new_request(f):
    """A meeting request, or anything that is one in effect (a pick with nothing open to pick from)."""
    parse = f.parse
    rule = getattr(parse, "length_rule", None)
    if rule in LENGTH_HOLDS:
        return _hold(LENGTH_HOLDS[rule], rule=rule)
    if f.thread.live_booking is not None and f.thread.live_booking.get("sender") == f.sender:
        return _move(f)
    if parse is not None and parse.verdict == "book" and f.model_agrees and parse.instant_utc is not None:
        start = parse.instant_utc
        return Plan(action="book", outcome="book", why=_why("policy.book_clear_time"),
                    slot=_slot(start, _length(parse)), direct=True, length_min=_length(parse))
    if f.thread.offer_count >= 2:
        return _hold("hold.third_offer")
    return _offer(parse, f.model_agrees)


def _slot(start, minutes):
    from .shapes import Slot
    return Slot(start, start + _dt.timedelta(minutes=minutes))


def _all_open_slots(thread):
    out = []
    for o in thread.open_offers:
        for s in o["slots"]:
            out.append((o["id"], s, o.get("length_min", 30)))
    return out


def _after_newest_offer(f):
    """True when the message reached her after her newest open offer to the sender last went out: its
    send time (set again whenever a restart sends it), or the time of its nudge when one went (a nudge
    lists the offer's times again, numbered anew once one is dropped; her ledger gives `nudged` only
    for a nudge the mail server took or one that may have gone, and for one in doubt that a T12
    settled it is when that T12 went). A time unknown is False: fail closed."""
    sent = [o.get("created") for o in f.thread.open_offers]
    sent += [o["nudged"] for o in f.thread.open_offers if o.get("nudged") is not None]
    if not sent or not all(_aware(s) for s in sent) or not _aware(f.arrived):
        return False
    return f.arrived > max(sent)


def _nudge_in_doubt(thread):
    """True when the nudge on an open offer to the sender may or may not have gone (its send was cut
    off by a kill, or its result is unknown), it left a time out, and the times have not gone again
    since. Whether they hold the offer's list or the nudge's is then not known, and the two number
    the times differently. Her ledger says so (ledger._offer): once the T12 below is in their hands
    the doubt is over, so its "reply with the number and I'll book it" holds."""
    return any(o.get("nudge") == "in_doubt" for o in thread.open_offers)


def _two_lists():
    """Fable review of PR #42, finding 2: a number or a bare yes names a time only by its place in a
    list, and while her nudge is in doubt there are two lists it could mean. They get the open times
    again (T12, no new offer record), never a booking. A time they write out still books."""
    return Plan(action="resend", outcome="offer", why=_why("policy.pick_resend", rule="ambiguous"), template="T12",
                pick_kind="ambiguous")


def _aware(t):
    return isinstance(t, _dt.datetime) and t.tzinfo is not None and t.utcoffset() is not None


def _before_offer(kind):
    """Fable review 2 of PR #3, finding B: a pick by number or a yes names a slot only by its place in an
    offer, so one that reached her before her newest offer went out cannot be answering that offer. It
    gets the open offer again (T12, no new offer record), never a booking."""
    return Plan(action="resend", outcome="offer", why=_why("policy.pick_before_offer"), template="T12",
                pick_kind=kind)


def _pick(f):
    t = f.thread
    if not t.open_offers:
        if t.live_booking is not None and t.live_booking.get("sender") == f.sender:
            return _move(f)            # a pick on a used offer while her booking stands is a move
        return _new_request(f)         # expired, or the booking was cancelled: a new request
    pk = f.pick
    if pk is not None and pk.kind == "matched_slot" and pk.slot_index is not None:
        offer = _offer_holding(t, pk.slot_index)
        if offer is not None and getattr(pk, "by", None) != "time":
            if _nudge_in_doubt(t):
                return _two_lists()
            if not _after_newest_offer(f):
                return _before_offer("matched_slot")
        if offer is not None:
            oid, s, length = offer
            code = "policy.pick_plus_question" if f.answer.side_question and f.answer.side_question_text else None
            plan = Plan(action="book", outcome="book",
                        why=_why(code) if code else _why("policy.pick_books", n=pk.slot_index + 1),
                        slot=s, offer_id=oid, length_min=length, pick_kind="matched_slot")
            if code:
                plan.side_question = f.answer.side_question_text
            return plan
    kind = pk.kind if pk is not None and pk.kind in ("none", "ambiguous", "near_miss") else "none"
    return Plan(action="resend", outcome="offer", why=_why("policy.pick_resend", rule=kind), template="T12",
                pick_kind=kind)


def _offer_holding(thread, index):
    """The open offer's slot at `index` (0-based), counted across the thread's open offers in order."""
    flat = _all_open_slots(thread)
    if 0 <= index < len(flat):
        return flat[index]
    return None


def _yes(f):
    t = f.thread
    flat = _all_open_slots(t)
    if len(flat) == 1 and _nudge_in_doubt(t):
        return _two_lists()             # a yes to the one time her nudge listed, or to the offer's several
    if len(flat) == 1 and not _after_newest_offer(f):
        return _before_offer("yes_single")
    if len(flat) == 1:
        oid, s, length = flat[0]
        return Plan(action="book", outcome="book", why=_why("policy.yes_books"), slot=s, offer_id=oid,
                    length_min=length, pick_kind="yes_single")
    if not flat:
        return _pick(f)                 # nothing open: a move while her booking stands, else a new request
    return Plan(action="resend", outcome="offer", why=_why("policy.pick_resend", rule="multiple"), template="T12",
                pick_kind="multiple")


def _decline(f):
    t = f.thread
    if t.offer_count >= 2:
        return _hold("hold.third_offer")
    if t.offer_count == 0:
        return _new_request(f)
    return Plan(action="offer", outcome="reoffer", why=_why("policy.decline_second_offer"), offer_family="T3",
                exclude_offered=True, length_min=_open_length(t))


def _open_length(thread):
    for o in thread.open_offers:
        return o.get("length_min", 30)
    return 30


def _cancel(f):
    b = f.thread.live_booking
    if b is None or b.get("sender") != f.sender:     # none, marked gone, or not this sender's
        return _hold("hold.not_in_ledger")
    return Plan(action="cancel", outcome="cancel", why=_why("policy.cancel_own"), template="T10",
                booking_id=b["booking_id"])


def _move(f):
    t = f.thread
    b = t.live_booking
    if b is None or b.get("sender") != f.sender:
        return _hold("hold.not_in_ledger")
    if t.offer_count >= 2:
        return _hold("hold.third_offer")       # nothing is cancelled: the booking stands
    # Offering needs no B11 (that is for booking straight away): the parser's kept readings go
    # first, and the loop's slot search keeps them only if they are available.
    return Plan(action="move", outcome="move", why=_why("policy.move_cancel_offer"), offer_family="T18",
                booking_id=b["booking_id"], first=_own_readings(f.parse), derived=_derived_readings(f.parse),
                exclude_offered=False, length_min=_length(f.parse))


# The closing word for a message whose reply already went out (its template says what it did).
REPLIED_OUTCOMES = {"T1": "book", "T2": "offer", "T13": "offer", "T4": "offer", "T12": "offer", "T3": "reoffer",
                    "T10": "cancel", "T18": "move"}


def _resume(f):
    """Ruling 8 and A3: a message read again after a kill or a stop keeps what it already did, before
    the thread or the model can say anything else. Its hold stands (one notice), a reply already in
    Sent ends it, and its booking is looked up by its ID, never planned or made again. None: nothing
    of its own is written down, so the table below decides."""
    if f.own_hold is not None:
        code = f.own_hold
        return Plan(action="hold", outcome="hold:" + code[len("hold."):], why=_why("policy.resume_hold"),
                    hold_code=code)
    if f.own_reply_unknown is not None:
        return _hold("hold.send_unconfirmed")       # it may have gone out: never sent again blind
    if f.own_reply is not None:
        return Plan(action="replied", outcome=REPLIED_OUTCOMES.get(f.own_reply, "offer"),
                    why=_why("policy.resume_sent", template=f.own_reply), template=f.own_reply)
    b = f.own_booking
    if b is not None:
        answer = f.answer
        question = (answer.side_question_text if answer is not None and answer.ok and answer.side_question
                    else None)
        return Plan(action="book", outcome="book", why=_why("policy.resume_booking"), slot=b["slot"],
                    direct=b["direct"] is True, offer_id=b["offer_id"], length_min=b["length_min"],
                    side_question=question)
    return None


def decide(f):
    """The policy table. Returns the one Plan for this message."""
    cls = f.answer.cls if f.answer is not None else "unsure"
    t = f.thread

    resumed = _resume(f)
    if resumed is not None:
        return resumed

    # A held thread stays Shane's: nothing more is sent on it, and no new notice. One thing is said
    # by name on the digest (ruling 17): a cancel or a move for a booking she found gone from the
    # calendar is "a cancel or move for a booking I don't have".
    if t.held:
        if t.gone_booking and f.answer is not None and f.answer.ok and cls in ("cancel", "move"):
            return Plan(action="thread_held", outcome="hold:not_in_ledger", why=_why("hold.not_in_ledger"),
                        hold_code="hold.not_in_ledger")
        return Plan(action="thread_held", outcome="hold:thread_held", why=_why("hold.thread_held"),
                    hold_code="hold.thread_held")

    # Holds code finds on its own beat every model class (decision 59).
    if f.reply_to_mismatch:
        return _hold("hold.reply_to_mismatch")
    if f.money_code:
        picked = bool(t.open_offers) and (cls in ("pick", "yes") or
                                          (f.pick is not None and f.pick.kind == "matched_slot"))
        return _hold("hold.money_with_pick" if picked else "hold.money")

    # No model answer: nothing she can act on.
    if f.answer is None or not f.answer.ok:
        return _hold("hold.no_model")

    # The model's opt-out is a second net behind the stop scan: silence, never a reply.
    if cls == "opt_out":
        return Plan(action="suppress", outcome="suppress", why=_why("policy.suppress_opt_out"))

    # Group mail and a new address on her own thread: held if she would act, filed if she would only file.
    # So is a sender with no offer of their own on a thread whose open offer went to someone else: an offer
    # or a booking of theirs there would lock that person out of it (Fable review 2 of PR #3, finding C).
    group = f.others_on_thread > 0
    new_address = (t.own_thread and f.sender not in t.written_to) or \
        (not t.open_offers and t.offer_to is not None and t.offer_to != f.sender)
    if group or new_address:
        if cls in FILE_ONLY:
            return _file(cls)
        if new_address and not group:
            return _hold("hold.new_address")
        return _hold("hold.group_mail")

    # The model's class, one row at a time.
    if cls in FILE_ONLY:
        return _file(cls)
    if cls in HELD_CLASSES:
        if cls in ("money", "legal", "complaint") and t.open_offers and f.pick is not None \
                and f.pick.kind == "matched_slot":
            return _hold("hold.money_with_pick")
        return _hold(HELD_CLASSES[cls])
    if cls == "meeting_request":
        if t.open_offers and f.pick is not None and f.pick.kind == "matched_slot":
            return _pick(f)            # a time that matches one offered slot is a pick
        return _new_request(f)
    if cls == "pick":
        return _pick(f)
    if cls == "yes":
        return _yes(f)
    if cls == "decline":
        return _decline(f)
    if cls == "no_thanks":
        return Plan(action="close", outcome="silent", why=_why("policy.no_thanks_close"))
    if cls == "cancel":
        return _cancel(f)
    if cls == "move":
        return _move(f)
    return _hold("hold.outside_classes")


def _file(cls):
    if cls == "auto_reply":
        return Plan(action="silent", outcome="silent", why=_why("policy.silent_auto"))
    if cls == "injection_only":
        return Plan(action="file", outcome="file", why=_why("policy.file_injection"))
    return Plan(action="file", outcome="file", why=_why("policy.file_fyi"))


def offer_form(family, n_slots):
    """(template, hold_code) for an offer once the number of available slots is known.

    No slot in the 30-day window is a hold. One slot is still offered: T2 and T3 become
    T13; T4 and T18 keep their own one-slot forms. When T3 would list two slots, T2 is sent.
    """
    if n_slots <= 0:
        return None, "hold.no_slot"
    if family in ("T2", "T3") and n_slots == 1:
        return "T13", None
    if family == "T3" and n_slots == 2:
        return "T2", None
    return family, None


def on_unavailable(plan, rule, thread):
    """A booking whose slot failed its re-check at the moment of booking (G9 or G11).

    A direct booking becomes T4 with other times. A picked slot that slipped inside 24 hours
    or went busy gets T4 with fresh times, which is a new offer. Either way a third offer on
    the thread is a hold.
    """
    if thread.offer_count >= 2:
        return _hold("hold.third_offer")
    if plan.direct:
        return Plan(action="offer", outcome="offer", why=_why("policy.offer_conflict", rule=rule),
                    offer_family="T4", asked=plan.slot.start_utc if plan.slot else None,
                    length_min=plan.length_min)
    return Plan(action="offer", outcome="reoffer", why=_why("policy.stale_pick_reoffer", rule=rule),
                offer_family="T4", exclude_offered=True, asked=plan.slot.start_utc if plan.slot else None,
                length_min=plan.length_min)


def on_collision(plan, thread):
    """Another event landed between her re-check and her write (rulings 4 and 8). Her own event
    is already removed; she offers fresh times with T4, naming the time that was asked for.
    A picked slot's times are never offered again. A third offer on the thread is a hold."""
    if thread.offer_count >= 2:
        return _hold("hold.third_offer")
    return Plan(action="offer", outcome="offer" if plan.direct else "reoffer",
                why=_why("policy.collision_reoffer"), offer_family="T4", exclude_offered=not plan.direct,
                asked=plan.slot.start_utc if plan.slot else None, length_min=plan.length_min)


# --- the code money check (ruling 14) -----------------------------------------------------
_CODES = ("USD", "GBP", "EUR", "CAD", "AUD", "NZD", "JPY", "CNY", "INR", "CHF", "SEK", "NOK", "DKK",
          "MXN", "BRL", "ZAR", "SGD", "HKD")
_CODE_ALT = "|".join(_CODES)
_CURRENCY = re.compile(
    r"[$£€¥₹]\s?\d"            # $500, £18.40, € 40, ¥2000, ₹100
    r"|\d\s?[$£€¥₹]"            # 40€
    r"|\b(?:" + _CODE_ALT + r")\s?\d"               # USD 200
    r"|\d\s?(?:" + _CODE_ALT + r")\b",              # 18.40 GBP, 300eur
    re.IGNORECASE)
_WORDS = re.compile(
    r"\b(?:invoice[sd]?|refund(?:s|ed|ing)?|chargebacks?|lawsuits?|attorneys?|subpoena(?:s|ed)?)\b",
    re.IGNORECASE)


def money_check(text):
    """The code check behind the model's money, legal and complaint classes (ruling 14).

    Run on the sender's own text above their signature only. A currency sign or code next
    to a number, or one of the words invoice, refund, chargeback, lawsuit, attorney or
    subpoena, holds the email. Returns "currency", "word" or None.
    """
    if not text:
        return None
    if _CURRENCY.search(text):
        return "currency"
    if _WORDS.search(text):
        return "word"
    return None
