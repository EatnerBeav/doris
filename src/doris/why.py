"""The closed list of whys (brief add A2).

Every decided, checked, held and halted receipt line carries why = {code, text}.
Code writes the why from this one list. Its slots are typed and filled by code: a count,
a time, a check id, a condition id, a template id or a word from a closed set. Never a
word from an email and never model text, so no email can put words in a why.

Hold codes are `hold.<reason>`, one per reason on the approved S1 list, so the two lists
cannot drift. The S1 wording itself lives in templates.py.
"""

import datetime as _dt
import re

# Words a slot may take when it is not a number, a time or an id.
RULE_WORDS = frozenset({
    "overlap", "outside_hours", "under_24h", "over_30d", "holiday", "not_quarter_hour", "length",
    "over_60", "open_ended", "all_day", "recurring", "multiple", "zero",
    "calendar_receipt", "bounce", "list", "auto", "self",
    "cc_only", "bcc", "direct_new_thread", "forged_thread",
    "cli", "stop_file", "watch_button", "budget_cap",
    "sender", "owner", "none", "matched_slot", "ambiguous", "near_miss", "yes_single",
    "send", "booking", "cancel", "move", "digest", "hold_notice",
})
_ID = re.compile(r"^(?:[GBTSNHQDLZ][0-9]{1,2}|T[0-9]{1,2}[a-z]?)$")

CODES = {
    # --- screens: settled before the model ---------------------------------------------
    "screen.quarantine": "Quarantined: the message could not be decoded. The run goes on.",
    "screen.duplicate": "Dropped: this Message-ID already ran once.",
    "screen.own_mail": "Dropped: her own mail (her address or a Message-ID she sent).",
    "screen.silenced_sender": "Filed: this sender opted out earlier. No reply.",
    "screen.stop": "Opt-out matched in the sender's own text: sender silenced, {n} queued item(s) dropped, no reply.",
    "screen.sensitive_skip": "Sensitive mail: filed unread, no model call, no reply.",
    "screen.no_reply_kind": "No reply to this kind of mail ({rule}): filed.",
    "screen.not_addressed": "Not addressed to the owner alone or to her on her own thread ({rule}): filed, no reply.",
    "screen.owner_auto_filed": "An auto-reply from the owner's address: filed.",
    # --- policy table rows ----------------------------------------------------------------
    "policy.book_clear_time": "One clear time, parser and model agree, and it is available: book.",
    "policy.offer_no_clear_time": "No single clear time (condition {cond} not met): offer times.",
    "policy.offer_conflict": "The time they wrote is not bookable or not available ({rule}): offer other times.",
    "policy.offer_one_slot": "Only one slot is available: offer it.",
    "policy.pick_books": "They picked slot {n} of the open offer: book it.",
    "policy.pick_resend": "The reply does not match exactly one offered slot ({rule}): send the same offer again.",
    "policy.yes_books": "A yes to an open offer holding one slot: book it.",
    "policy.stale_pick_reoffer": "The picked slot is no longer bookable or available ({rule}): offer fresh times.",
    "policy.decline_second_offer": "They declined the first offer: second offer with times not offered before.",
    "policy.no_thanks_close": "A plain no: offer closed, no reply.",
    "policy.cancel_own": "Cancel of her own booking on this thread: remove it, confirm, reply.",
    "policy.move_cancel_offer": "Move: cancel her booking, confirm the removal, offer fresh times.",
    "policy.collision_reoffer": "Another event landed during the booking: her event removed, offer fresh times.",
    "policy.file_fyi": "Newsletter, notification or FYI: file, no reply.",
    "policy.file_injection": "Only instructions or a lure, no real request: file, no reply.",
    "policy.silent_auto": "Auto-reply or out-of-office: no reply.",
    "policy.suppress_opt_out": "The model read an opt-out: sender silenced, no reply.",
    "policy.superseded": "A newer message on this thread waits: this one is superseded.",
    "policy.pick_plus_question": "A pick plus a question: book the pick, the question goes on the digest.",
    # --- gate -------------------------------------------------------------------------------
    "gate.pass": "All {n} checks that apply passed.",
    "gate.g1_halted": "Blocked by G1: a halt is in force ({rule}).",
    "gate.g2_not_sole_runner": "Blocked by G2: this process does not hold the ledger lock.",
    "gate.g3_budget": "Blocked by G3: model spend reached the cap.",
    "gate.g4_model_missing": "Blocked by G4: the model's answer is missing.",
    "gate.g5_silenced": "Blocked by G5: the recipient opted out.",
    "gate.g6_recipient": "Blocked by G6: the recipient or the addressing is not allowed ({rule}).",
    "gate.g7_cap": "Blocked by G7: a cap is reached ({rule}).",
    "gate.g8_template": "Blocked by G8: not an approved template, a slot failed, or the signature differs.",
    "gate.g9_not_bookable": "Blocked by G9: the interval is not bookable ({rule}).",
    "gate.g10_evidence": "Blocked by G10: direct-booking evidence missing or the model disagrees ({cond}).",
    "gate.g11_overlap": "Blocked by G11: the calendar shows an overlapping event, or the fresh read failed.",
    "gate.g12_ledger": "Blocked by G12: booking ID not in the ledger first, or the thread has another live booking.",
    "gate.g13_ics": "Blocked by G13: the .ics carries a field outside the fixed list.",
    "gate.g14_not_hers": "Blocked by G14: that booking is not hers for this sender on this thread.",
    "gate.g15_readback": "Blocked by G15: no clean read-back for this booking yet.",
    "gate.g16_opened": "Blocked by G16: something was opened, or the recipient or host is not allowed.",
    "gate.g17_sensitive": "Blocked by G17: sensitive content in the message or the outgoing text.",
    "gate.check_failed_to_run": "Blocked: check {check} could not run, so it fails closed.",
    # --- holds: one code per approved S1 reason (wording in templates.py) ----------------
    "hold.owner_mail": "Held: mail from the owner's address is never an instruction.",
    "hold.oversize": "Held: too long to read safely.",
    "hold.hidden_text": "Held: hidden text in the email.",
    "hold.new_address": "Held: a pick or request on her thread from an address she did not write to.",
    "hold.reply_to_mismatch": "Held: the reply address differs from the sender.",
    "hold.all_day_recurring_multiple": "Held: all-day, recurring or several meetings ({rule}).",
    "hold.not_in_ledger": "Held: a cancel or move for a booking she does not have.",
    "hold.third_offer": "Held: a third set of times would be needed.",
    "hold.remove_failed": "Held: her booking could not be removed after a clash.",
    "hold.calendar_down": "Held: the calendar didn't answer twice.",
    "hold.money_with_pick": "Held: money, legal or a complaint, with a pick.",
    "hold.no_model": "Held: the model wasn't available, so she didn't read it.",
    "hold.group_mail": "Held: more than one person is on the thread.",
    "hold.owner_removed": "Held: the owner removed one of her bookings; the prospect was not told.",
    "hold.longer_than_hour": "Held: longer than an hour ({rule}).",
    "hold.money": "Held: money in the sender's own words.",
    "hold.legal": "Held: legal or contract terms in the sender's own words.",
    "hold.complaint": "Held: a complaint.",
    "hold.add_person": "Held: asks to add or write to another person.",
    "hold.unsure": "Held: the model was unsure.",
    "hold.outside_classes": "Held: a request outside her classes.",
    "hold.no_slot": "Held: no available slot in the next 30 days.",
    "hold.unknown_result": "Held: a write's result is unknown and the lookup failed.",
    "hold.send_unconfirmed": "Held: a send could not be confirmed in Sent.",
    "hold.internal_error": "Held: an unexpected error stopped her part way; the error line before this names it.",
    "hold.gate_blocked": "Held: the gate blocked the action ({check}); nothing was sent.",
    "hold.thread_held": "Held thread: it stays the owner's, nothing more is sent on it.",
    # --- halts ----------------------------------------------------------------------------
    "halt.cli": "Halted: stop on the command line.",
    "halt.stop_file": "Halted: a STOP file was found.",
    "halt.watch_button": "Halted: Stop pressed on the watch page.",
    "halt.budget_cap": "Halted: model spend reached the cap of the run.",
    "halt.unreadable": "Halted: a halt file is there but can't be read, so she stops (fail closed).",
}

HALT_CAUSES = ("cli", "stop_file", "watch_button", "budget_cap")


class WhyError(ValueError):
    pass


def _slot_text(value):
    if isinstance(value, bool):
        raise WhyError("a why slot cannot be a bool")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, _dt.datetime):
        if value.tzinfo is None:
            raise WhyError("a why time must be timezone-aware")
        return value.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    if isinstance(value, str) and (value in RULE_WORDS or _ID.match(value)):
        return value
    raise WhyError("a why slot takes a count, a time, an id or a listed word, not %r" % (value,))


def why(code, **slots):
    """Return {"code", "text"} for a listed code, slots typed and filled by code."""
    if code not in CODES:
        raise WhyError("unlisted why code %r" % (code,))
    filled = {k: _slot_text(v) for k, v in slots.items()}
    try:
        text = CODES[code].format(**filled)
    except KeyError as exc:
        raise WhyError("why %s needs slot %s" % (code, exc))
    return {"code": code, "text": text}
