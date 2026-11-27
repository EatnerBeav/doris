"""Screens 1 to 11: everything that runs in code before the model (screen 3 is cut).

Order (calendar-rulings-spec, "What runs before the model, in order"):
  1 decode, 2 seen before or her own, 4 stop scan  -> over the whole fetched batch first (run_batch),
  so a stop lands before anything else; then, per message, oldest first:
  5 sensitive, 6 owner mail, 7 no-reply kinds, 8 addressed to him (ruling 18), 9 oversize,
  10 hidden text, 11 count, never open (never settles)  -> run_message.

A screen that settles a message ends its path. Every settled ScreenResult carries a why from
why.py (screen.* or hold.*). Facts hold typed values only: counts, booleans, hashes, ids and
words from closed sets (why.RULE_WORDS or FACT_WORDS below), never text from the email.

Fail closed: an env callable (the ledger) that raises is never read as "not seen", "not
silenced" or "not hers"; the exception goes up to the loop. A message that cannot be decoded
is quarantined and the run goes on.
"""

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from . import caps, mail
from . import sensitive as _sensitive
from .shapes import DecodeError, Message, ScreenResult
from .why import why as _why

# ---------------------------------------------------------------------------------------
# Closed sets
# ---------------------------------------------------------------------------------------
STOP_WORDS = ("stop", "unsubscribe")                       # count only as a whole line
STOP_PHRASES = ("stop emailing me", "don't email me", "do not email me", "opt out", "no more emails",
                "take me off your list")                   # count anywhere in the sender's own text
REMOVE_ME = "remove me"                                    # a whole line (with or without please), or + from your/this list
STOP_LIST = STOP_WORDS + STOP_PHRASES + (REMOVE_ME,)

FACT_WORDS = frozenset(("1", "2", "else", "scan_error") + STOP_LIST + _sensitive.CHECKS + mail.HIDDEN_TOKENS + mail.DECODE_FAULTS)

_RECEIPT_METHODS = ("REPLY", "CANCEL", "COUNTER", "DECLINECOUNTER", "REFRESH")
_RECEIPT_SUBJECT = re.compile(
    r"^\s*(?:accepted|declined|tentative|tentatively accepted|updated invitation(?: with note)?|invitation updated"
    r"|cancell?ed(?: event| invitation)?|new time proposed)\s*:", re.I)


@dataclass
class ScreenEnv:
    """Callables the loop builds from the ledger. Each may raise; a raise is never read as False."""
    owner_addr: str
    doris_addr: str
    is_seen: Callable[[str], bool]                         # Message.key already in the ledger
    is_her_message_id: Callable[[str], bool]               # a Message-ID in her Sent ledger
    is_silenced: Callable[[str], bool]                     # address on the stop list
    addresses_written_on_thread: Callable[[str], Set[str]]  # thread key -> addresses she wrote to
    thread_of: Callable[[str], Optional[str]]              # one of her Message-IDs -> its thread key
    queued_count: Optional[Callable[[str], int]] = None    # sender address -> queued items in the ledger


@dataclass
class Screened:
    """One fetched item after screens 1, 2 and 4."""
    item: Any
    message: Optional[Message]
    results: List[ScreenResult] = field(default_factory=list)

    @property
    def settled(self) -> Optional[ScreenResult]:
        for r in self.results:
            if r.settled:
                return r
        return None


def _open(screen, name, **facts) -> ScreenResult:
    return ScreenResult(screen=screen, name=name, settled=False, facts=facts)


def _settle(screen, name, outcome, code, facts=None, **slots) -> ScreenResult:
    return ScreenResult(screen=screen, name=name, settled=True, outcome=outcome, why=_why(code, **slots),
                        hold_reason=code if outcome == "hold" else None, facts=dict(facts or {}))


# ---------------------------------------------------------------------------------------
# Addresses
# ---------------------------------------------------------------------------------------
def canonical_address(addr: str) -> str:
    """One mailbox, one spelling: lowercase, no +tag, no trailing dot."""
    a = (addr or "").strip().lower().rstrip(".")
    local, at, domain = a.rpartition("@")
    if not at:
        return a
    return local.split("+", 1)[0] + "@" + domain.rstrip(".")


def same_address(a: str, b: str) -> bool:
    return bool(a) and bool(b) and canonical_address(a) == canonical_address(b)


_CONFUSABLE = str.maketrans({
    "\u0430": "a", "\u0435": "e", "\u043e": "o", "\u0440": "p", "\u0441": "c", "\u0443": "y", "\u0445": "x",
    "\u0456": "i", "\u0458": "j", "\u0455": "s", "\u0501": "d", "\u04bb": "h", "\u04cf": "l", "\u051b": "q",
    "\u051d": "w", "\u0432": "b", "\u043a": "k", "\u043c": "m", "\u043d": "h", "\u0442": "t", "\u0457": "i",
    "\u03b1": "a", "\u03bf": "o", "\u03bd": "v", "\u03c1": "p", "\u03c4": "t", "\u03b9": "i", "\u03ba": "k",
    "\u03c5": "u", "\u03b5": "e", "\u0131": "i", "0": "o", "1": "l", "|": "l",
})


def _skeleton(addr: str) -> str:
    a = unicodedata.normalize("NFKD", canonical_address(addr))
    a = "".join(ch for ch in a if not unicodedata.combining(ch) and unicodedata.category(ch) != "Cf")
    a = a.lower().translate(_CONFUSABLE)
    return a.replace("rn", "m").replace("vv", "w")


def lookalike(addr: str, target: str) -> bool:
    """True when addr is not target's mailbox but reads like it (homoglyphs, 1 for l, rn for m)."""
    return bool(addr) and not same_address(addr, target) and _skeleton(addr) == _skeleton(target)


def _mid_forms(mid: str) -> List[str]:
    m = (mid or "").strip()
    bare = m[1:-1] if m.startswith("<") and m.endswith(">") else m
    return [f for f in ("<" + bare + ">", bare) if bare]


def _is_her_mid(env: ScreenEnv, mid: str) -> bool:
    return any(env.is_her_message_id(f) for f in _mid_forms(mid))


# ---------------------------------------------------------------------------------------
# Screen 1: decode
# ---------------------------------------------------------------------------------------
def decode(item) -> Tuple[Optional[Message], ScreenResult]:
    """Parse one fetched item (an InboundItem, or raw bytes). Never raises for a bad message."""
    raw = item if isinstance(item, (bytes, bytearray)) else getattr(item, "raw", b"")
    raw = bytes(raw or b"")
    base = {"raw_size": len(raw), "raw_sha256": hashlib.sha256(raw).hexdigest()}
    try:
        msg = mail.parse_eml(raw)
    except DecodeError as exc:
        fault = getattr(exc, "fault", "structure")
        fault = fault if fault in mail.DECODE_FAULTS else "structure"
        return None, _settle(1, "decode", "quarantine", "screen.quarantine",
                             dict(base, quarantined=True, decode_fault=fault))
    except Exception:
        return None, _settle(1, "decode", "quarantine", "screen.quarantine",
                             dict(base, quarantined=True, decode_fault="parser_error"))
    return msg, _open(1, "decode", attachments=len(msg.attachments), **base)


# ---------------------------------------------------------------------------------------
# Screen 2: seen before, or her own
# ---------------------------------------------------------------------------------------
def seen_or_own(msg: Message, env: ScreenEnv) -> ScreenResult:
    if env.is_seen(msg.key):
        return _settle(2, "seen_or_own", "drop", "screen.duplicate", {"duplicate": True})
    from_doris = same_address(msg.from_addr, env.doris_addr)
    hers = bool(msg.message_id) and _is_her_mid(env, msg.message_id)
    if from_doris or hers:
        return _settle(2, "seen_or_own", "drop", "screen.own_mail", {"from_doris": from_doris, "her_message_id": hers})
    return _open(2, "seen_or_own", duplicate=False, from_doris=False, her_message_id=False)


# ---------------------------------------------------------------------------------------
# Screen 4: stop scan (ruling 9)
# ---------------------------------------------------------------------------------------
_APOSTROPHES = "'\u2019\u2018\u02bc`\u00b4\u2032"


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "")
    t = "".join(ch for ch in t if unicodedata.category(ch) != "Cf").lower()
    for a in _APOSTROPHES:
        t = t.replace(a, "")
    t = re.sub(r"[^\w\s]|_", " ", t)
    return re.sub(r"\s+", " ", t).strip()


_PHRASE_FORMS = [(p, _norm(p)) for p in STOP_PHRASES]
_HER_SIGNATURE_LINE = _norm('Reply "stop" to opt out.')


def _stop_where(text: str) -> Tuple[Optional[str], bool]:
    lines = (text or "").split("\n")
    for line in lines:
        n = _norm(line)
        if n in STOP_WORDS:
            return n, True
        if n in ("remove me", "please remove me", "remove me please"):
            return REMOVE_ME, True
    flat = " " + _norm(text or "").replace(_HER_SIGNATURE_LINE, " ") + " "
    for phrase, form in _PHRASE_FORMS:
        if " " + form + " " in flat:
            return phrase, False
    if " remove me from your list " in flat or " remove me from this list " in flat:
        return REMOVE_ME, False
    return None, False


def stop_match(text: str) -> Optional[str]:
    """The stop-list entry matched in the sender's own text, or None (ruling 9).

    Case and punctuation do not matter (punctuation is a space, apostrophes are dropped). Single
    words count only as a whole line; phrases anywhere, across a wrapped line. Pass only the
    sender's own text (Message.own_text): quoted text and her signature never count.
    """
    return _stop_where(text)[0]


def stop_scan(msgs: Sequence[Message], env: ScreenEnv) -> Dict[str, ScreenResult]:
    """Screen 4 over the whole batch, before any one message is processed.

    Returns a ScreenResult for every message, keyed by Message.key; settled=False means go on.
    A stop silences that sender: the stop message is settled `suppress` (why screen.stop, with the
    number of queued items dropped: the ledger's queue for that sender plus the sender's other
    messages in this batch) and every other message of theirs in the batch is filed. Mail from an
    address already silenced is filed. The owner's address is never silenced (ruling 1 only).
    """
    stoppers: Dict[str, Tuple[str, str, bool]] = {}
    for m in msgs:
        if same_address(m.from_addr, env.owner_addr):
            continue
        c = canonical_address(m.from_addr)
        if c in stoppers:
            continue
        phrase, whole = _stop_where(m.own_text)
        if phrase:
            stoppers[c] = (m.key, phrase, whole)

    out: Dict[str, ScreenResult] = {}
    for m in msgs:
        if same_address(m.from_addr, env.owner_addr):
            out[m.key] = _open(4, "stop_scan", from_owner=True, silenced=False)
            continue
        if env.is_silenced(m.from_addr):
            out[m.key] = _settle(4, "stop_scan", "file", "screen.silenced_sender",
                                 {"silenced": True, "stop_in_batch": False})
            continue
        c = canonical_address(m.from_addr)
        if c not in stoppers:
            out[m.key] = _open(4, "stop_scan", from_owner=False, silenced=False)
            continue
        skey, phrase, whole = stoppers[c]
        if m.key != skey:
            out[m.key] = _settle(4, "stop_scan", "file", "screen.silenced_sender",
                                 {"silenced": True, "stop_in_batch": True})
            continue
        in_batch = sum(1 for o in msgs if o.key != m.key and canonical_address(o.from_addr) == c)
        counted = env.queued_count is not None
        queued = int(env.queued_count(m.from_addr)) if counted else 0
        n = queued + in_batch
        out[m.key] = _settle(4, "stop_scan", "suppress", "screen.stop",
                             {"stop_match": phrase, "stop_whole_line": whole, "queued_dropped": n,
                              "queued_counted": counted, "silenced": True}, n=n)
    return out


# ---------------------------------------------------------------------------------------
# Screen 5: sensitive mail
# ---------------------------------------------------------------------------------------
def sensitive(msg: Message) -> ScreenResult:
    """Filed unread on a match: the facts keep only the check name and the message hash
    (the loop adds the bare sender address). A scan that cannot run files it unread too."""
    try:
        check = _sensitive.scan_message(msg)
    except Exception:
        check = "scan_error"
    if check:
        return _settle(5, "sensitive", "skip", "screen.sensitive_skip",
                       {"sensitive": True, "check": check, "message_hash": msg.raw_sha256})
    return _open(5, "sensitive", sensitive=False)


# ---------------------------------------------------------------------------------------
# Screen 6: owner mail (ruling 1)
# ---------------------------------------------------------------------------------------
def is_auto_submitted(msg: Message) -> bool:
    h = msg.headers
    auto = h.get("auto-submitted", "").strip().lower()
    if auto and not auto.startswith("no"):
        return True
    if "x-autoreply" in h or "x-autorespond" in h:
        return True
    return h.get("precedence", "").strip().lower() == "auto_reply"


def owner_mail(msg: Message, env: ScreenEnv) -> ScreenResult:
    """All mail from the owner's address is held, "stop" included; auto-submitted owner mail is filed.
    A lookalike of the owner's address is held the same way (never answered as a prospect)."""
    is_owner = same_address(msg.from_addr, env.owner_addr)
    look = (not is_owner) and lookalike(msg.from_addr, env.owner_addr)
    if not (is_owner or look):
        return _open(6, "owner_mail", from_owner=False, lookalike=False)
    facts = {"from_owner": is_owner, "lookalike": look}
    if is_auto_submitted(msg):
        return _settle(6, "owner_mail", "file", "screen.owner_auto_filed", dict(facts, auto=True))
    return _settle(6, "owner_mail", "hold", "hold.owner_mail", dict(facts, auto=False))


# ---------------------------------------------------------------------------------------
# Screen 7: no-reply kinds (ruling 11), on headers
# ---------------------------------------------------------------------------------------
def _kind(msg: Message, env: ScreenEnv) -> Optional[str]:
    h = msg.headers
    if same_address(msg.from_addr, env.doris_addr):
        return "self"
    local = msg.from_addr.rpartition("@")[0]
    if (msg.is_report or "x-failed-recipients" in h or local in ("mailer-daemon", "postmaster")):
        return "bounce"
    cal = [a for a in msg.attachments
           if a.declared_type in ("text/calendar", "application/ics") or a.name.lower().endswith(".ics")]
    if cal:
        methods = set(m.strip().upper() for m in h.get("calendar-method", "").split(",") if m.strip())
        if methods & set(_RECEIPT_METHODS) or _RECEIPT_SUBJECT.match(msg.subject or ""):
            return "calendar_receipt"
    if is_auto_submitted(msg) or h.get("return-path", "").strip() == "<>":
        return "auto"
    if ("list-id" in h or "list-unsubscribe" in h or "list-post" in h
            or h.get("precedence", "").strip().lower() in ("bulk", "list", "junk")):
        return "list"
    return None


def no_reply_kind(msg: Message, env: ScreenEnv) -> ScreenResult:
    rule = _kind(msg, env)
    if rule:
        return _settle(7, "no_reply_kind", "file", "screen.no_reply_kind", {"no_reply_kind": rule}, rule=rule)
    return _open(7, "no_reply_kind", no_reply_kind=None)


# ---------------------------------------------------------------------------------------
# Screen 8: addressed to him (ruling 18), on headers
# ---------------------------------------------------------------------------------------
def addressed(msg: Message, env: ScreenEnv) -> ScreenResult:
    """Branch 1: the owner in To, nobody else but the sender. Branch 2: she is in To, on her own
    thread (a reply header names a Message-ID in her Sent ledger), nobody else but the sender.
    Filed here: neither in To (cc_only / bcc), and mail to her on no thread of hers
    (direct_new_thread / forged_thread). Group mail (branch else with the owner or her in To)
    goes on to the policy table, which holds or files it."""
    owner_in_to = any(same_address(a, env.owner_addr) for a in msg.to)
    doris_in_to = any(same_address(a, env.doris_addr) for a in msg.to)
    named = list(msg.in_reply_to) + list(msg.references)
    hers = [mid for mid in named if _is_her_mid(env, mid)]
    own_thread = bool(hers)
    others = set()
    for a in list(msg.to) + list(msg.cc):
        if same_address(a, msg.from_addr) or same_address(a, env.owner_addr) or same_address(a, env.doris_addr):
            continue
        others.add(canonical_address(a))
    n_others = len(others)
    if owner_in_to and n_others == 0:
        branch = "1"
    elif doris_in_to and own_thread and n_others == 0:
        branch = "2"
    else:
        branch = "else"
    new_address = False
    if own_thread:
        written: Set[str] = set()
        for mid in hers:
            for f in _mid_forms(mid):
                tk = env.thread_of(f)
                if tk:
                    written |= set(canonical_address(w) for w in env.addresses_written_on_thread(tk))
        new_address = canonical_address(msg.from_addr) not in written
    facts = {"owner_in_to": owner_in_to, "doris_in_to": doris_in_to, "own_thread": own_thread,
             "others_on_thread": n_others, "branch": branch, "new_address": new_address}

    if not owner_in_to and not doris_in_to:
        in_cc = any(same_address(a, env.owner_addr) or same_address(a, env.doris_addr) for a in msg.cc)
        rule = "cc_only" if in_cc else "bcc"
        return _settle(8, "addressed", "file", "screen.not_addressed", facts, rule=rule)
    if doris_in_to and not owner_in_to and not own_thread:
        rule = "forged_thread" if named else "direct_new_thread"
        return _settle(8, "addressed", "file", "screen.not_addressed", facts, rule=rule)
    return ScreenResult(screen=8, name="addressed", settled=False, facts=facts)


# ---------------------------------------------------------------------------------------
# Screen 9: oversize
# ---------------------------------------------------------------------------------------
def oversize(msg: Message) -> ScreenResult:
    """Decoded text parts over caps.MAX_TEXT_CHARS, or raw over caps.MAX_RAW_BYTES: held.
    Attachments never count."""
    text_chars = len(msg.text_plain or "") + len(msg.html or "")
    over_text = text_chars > caps.MAX_TEXT_CHARS
    over_raw = msg.raw_size > caps.MAX_RAW_BYTES
    facts = {"text_chars": text_chars, "raw_bytes": msg.raw_size, "over_text": over_text, "over_raw": over_raw}
    if over_text or over_raw:
        return _settle(9, "oversize", "hold", "hold.oversize", facts)
    return _open(9, "oversize", **facts)


# ---------------------------------------------------------------------------------------
# Screen 10: hidden text (ruling 13)
# ---------------------------------------------------------------------------------------
def hidden_text(msg: Message) -> ScreenResult:
    """Held with the hidden text quoted (it stays on Message.hidden_text for the S1 quote).
    The facts name the token that fired and the character count, never the text."""
    text = msg.hidden_text or ""
    if text.strip():
        token = msg.hidden_token if msg.hidden_token in mail.HIDDEN_TOKENS else None
        return _settle(10, "hidden_text", "hold", "hold.hidden_text",
                       {"hidden_text": True, "hidden_token": token, "hidden_chars": len(text)})
    return _open(10, "hidden_text", hidden_text=False)


# ---------------------------------------------------------------------------------------
# Screen 11: count, never open (the hard rule)
# ---------------------------------------------------------------------------------------
def count_never_open(msg: Message) -> ScreenResult:
    """Never settles. The observed receipt lists the attachments themselves (msg.attachments,
    each inspected: false); these facts are the counts."""
    return _open(11, "count_never_open", links_seen=msg.links_seen, phone_numbers_seen=msg.phone_numbers_seen,
                 addresses_in_body=msg.addresses_in_body, attachments=len(msg.attachments),
                 attachments_inspected=sum(1 for a in msg.attachments if a.inspected), opened=0)


# ---------------------------------------------------------------------------------------
# Pipelines
# ---------------------------------------------------------------------------------------
def run_batch(items: Sequence[Any], env: ScreenEnv) -> List[Screened]:
    """Screens 1, 2 and 4 over every fetched item, in order. A second copy of a Message-ID in the
    same batch is dropped as a duplicate. Items that settle stop; the rest go on to run_message."""
    out: List[Screened] = []
    keys: Set[str] = set()
    for it in items:
        msg, r1 = decode(it)
        s = Screened(item=it, message=msg, results=[r1])
        out.append(s)
        if r1.settled:
            continue
        if msg.key in keys:
            r2 = _settle(2, "seen_or_own", "drop", "screen.duplicate", {"duplicate": True, "in_batch": True})
        else:
            r2 = seen_or_own(msg, env)
        keys.add(msg.key)
        s.results.append(r2)
    open_ones = [s for s in out if s.settled is None]
    stops = stop_scan([s.message for s in open_ones], env)
    for s in open_ones:
        s.results.append(stops[s.message.key])
    return out


def run_message(msg: Message, env: ScreenEnv) -> List[ScreenResult]:
    """Screens 5 to 11 for one message, in order, stopping at the first that settles."""
    steps = (
        lambda: sensitive(msg),
        lambda: owner_mail(msg, env),
        lambda: no_reply_kind(msg, env),
        lambda: addressed(msg, env),
        lambda: oversize(msg),
        lambda: hidden_text(msg),
        lambda: count_never_open(msg),
    )
    results: List[ScreenResult] = []
    for step in steps:
        r = step()
        results.append(r)
        if r.settled:
            break
    return results
