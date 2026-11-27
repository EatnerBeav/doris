"""The daily digest (S2), counted from receipt lines only: never fixed text.

build(receipt_lines, now, verified) returns the slots for templates.render("S2", ...). The
caller passes the period's lines (the day's, or one demo run's) and the result of
receipts.verify for them; an unknown verify result reads "no".

What it reads from each line (the loop writes these):
- `observed`, one per inbound message: `subject` = the message key; payload `from_addr`,
  `from_name`, `thread`. The ratio counts distinct observed subjects: held ones are held,
  every other one is handled.
- `held`: `subject` = the message key, why.code = hold.<reason>, `ts` = when the notice went.
- any line's why.code: screen.quarantine, screen.stop / policy.suppress_opt_out (payload
  `dropped`: a count or a list), screen.sensitive_skip, screen.not_addressed,
  policy.file_injection, policy.pick_plus_question (payload `question`: the code-copied text),
  the offer codes and the codes that close an offer (per thread: `payload.thread`, else the
  observed line's thread).
- `called` with payload call == "model", and the `result` whose payload `call_seq` names it
  (payload `cost_usd`).
- any payload with `human_approval: true` counts as a human approval (there are none in Doris).
"""

import datetime as _dt

from . import holds
from .shapes import UTC

OFFER_CODES = frozenset({
    "policy.offer_no_clear_time", "policy.offer_conflict", "policy.offer_one_slot", "policy.pick_resend",
    "policy.pick_before_offer",
    "policy.stale_pick_reoffer", "policy.decline_second_offer", "policy.move_cancel_offer",
    "policy.collision_reoffer",
})
CLOSE_CODES = frozenset({
    "policy.book_clear_time", "policy.pick_books", "policy.yes_books", "policy.pick_plus_question",
    "policy.no_thanks_close", "policy.cancel_own",
})
STOP_CODES = frozenset({"screen.stop", "policy.suppress_opt_out"})


def _code(line):
    w = line.get("why")
    return w.get("code") if isinstance(w, dict) else None


def _payload(line):
    p = line.get("payload")
    return p if isinstance(p, dict) else {}


def _ts(line, fallback):
    try:
        return _dt.datetime.strptime(line.get("ts", ""), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return fallback


def _add_once(items, value):
    if value and value not in items:
        items.append(value)


def build(receipt_lines, now, verified=None):
    """S2 slots counted from receipts: handled, held, human approvals and the listed lines."""
    from . import templates   # same owner; imported here so a broken template page never breaks counting
    slots = templates.empty_digest(now)
    senders = {}     # message key -> (address, name)
    threads = {}     # message key -> thread key
    observed, held = [], []
    offers = {}      # thread -> (address, name) while an offer is open, in the order offered
    model_seqs = set()
    cost = 0.0
    calls = 0
    quarantined, not_addressed = set(), set()

    def who(line):
        p = _payload(line)
        addr, name = senders.get(line.get("subject"), ("", ""))
        addr = p.get("from_addr") or addr or ""
        name = holds._name_line(p.get("from_name") or name or "") or addr
        return addr, name

    for line in receipt_lines:
        if not isinstance(line, dict):
            continue
        kind, subject, p, code = line.get("kind"), line.get("subject"), _payload(line), _code(line)
        if p.get("human_approval") is True:
            slots["approvals"] += 1
        if kind == "observed" and subject:
            _add_once(observed, subject)
            if p.get("from_addr") or p.get("from_name"):
                senders[subject] = (p.get("from_addr") or "", p.get("from_name") or "")
            if p.get("thread"):
                threads[subject] = p["thread"]
        if kind == "called" and p.get("call") == "model":
            calls += 1
            model_seqs.add(line.get("seq"))
        if kind == "result" and p.get("call_seq") in model_seqs:
            c = p.get("cost_usd")
            if isinstance(c, (int, float)) and not isinstance(c, bool) and c > 0:
                cost += c
        thread = p.get("thread") or threads.get(subject) or subject
        addr, name = who(line)
        if kind == "held" and subject:
            if subject not in held:
                held.append(subject)
                if code and code.startswith("hold.") and code not in templates.NO_NOTICE:
                    slots["holds"].append({"name": name or subject, "reason": code, "at": _ts(line, now)})
            offers.pop(thread, None)
        if code in OFFER_CODES:
            offers.pop(thread, None)
            offers[thread] = (addr, name)
        elif code in CLOSE_CODES:
            offers.pop(thread, None)
        if code in STOP_CODES and addr:
            dropped = p.get("dropped", 0)
            k = len(dropped) if isinstance(dropped, (list, tuple)) else dropped
            k = k if isinstance(k, int) and not isinstance(k, bool) and k > 0 else 0
            for i, (a, n) in enumerate(slots["opted_out"]):
                if a == addr:
                    slots["opted_out"][i] = (a, n + k)
                    break
            else:
                slots["opted_out"].append((addr, k))
            for t in [t for t, (a, _) in offers.items() if a == addr]:
                offers.pop(t)
        if code == "policy.file_injection":
            _add_once(slots["suspicious"], name)
        if code == "screen.quarantine" and subject:
            quarantined.add(subject)
        if code == "screen.not_addressed" and subject:
            not_addressed.add(subject)
        if code == "screen.sensitive_skip":
            _add_once(slots["sensitive"], addr)
        if code == "policy.pick_plus_question" and isinstance(p.get("question"), str):
            q = holds._name_line(p["question"], 200)
            if q and name:
                slots["also_asked"].append((name, q))

    slots["held"] = len(held)
    slots["handled"] = len([k for k in observed if k not in held])
    slots["offered_to"] = []
    for addr, name in offers.values():
        _add_once(slots["offered_to"], name)
    slots["quarantined"] = len(quarantined)
    slots["not_addressed"] = len(not_addressed)
    slots["model_calls"] = calls
    slots["cost_usd"] = round(cost, 6)
    slots["verified"] = verified is True
    return slots
