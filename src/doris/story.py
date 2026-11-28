"""What Doris did for the owner, in plain words, from her receipts alone.

    story.lines(receipts_path)   the lines the demo prints when its run ends (with the day's earlier runs)
    story.build(lines)           the story as plain-text sections, from receipt lines that verified
    story.render(story)          those sections as terminal lines

The demo's last screen, and the same section on the watch page: what needs the owner and why, which of
her bookings still stand on his calendar, each thread in a sentence or two, who is waiting to pick one of
her times, the digest, the model's cost, the gate's count and 0 human approvals.

Every claim is a receipt line's. A reply counts as answered only when her lookup found it in Sent; a
meeting is on the calendar only while her booking stands (written and read back, never removed after);
a hold's reason is the approved S1 phrase for its why code (what the owner already sees in her notice and
the digest), else the why text; approvals are counted, never assumed. Wording comes from a fixed table
keyed by the closing line's outcome and why code; any code the table lacks shows its own why text, which
code writes from the closed list in why.py. Names are the only words from an email, made safe as her
notices make them (holds.sender_name) and escaped for the terminal as replay escapes every value. No mail
body, quote, subject or model word is read here, and nothing is written anywhere.
"""

import datetime as _dt
import os
import re
import shutil
import sys
import textwrap

from . import digest, holds, paths, receipts, replay, templates, why as why_mod

OWNER = "Shane"                 # as every template names him
MAX_THREADS = 25                # a long live run has many; the rest are counted and pointed to replay
NAME_WIDTH = 24                 # the name column on the terminal; longer names are cut there
WIDTH = 100                     # the terminal's width when it can't be read (a pipe, a file, CI)
CALENDAR_CALLS = ("calendar_query", "calendar_create", "calendar_get", "calendar_delete")
UNREADABLE = "(unreadable message)"
BOOKING = "booking:"                             # the subject of a check of one of her bookings (ruling 17)
SENSITIVE = "(sensitive mail, unread)"           # as the loop's own line shows it
MAX_COST = 1e6                                   # a recorded cost past this is no cost the story adds up
MAX_MINUTES = 24 * 60
# S1 phrases that point at the notice's own layout, said here as the story shows it
STORY_REASONS = {"hold.remove_failed": "I couldn't remove my booking after a clash; both events are on the calendar"}
_TS = "%Y-%m-%dT%H:%M:%SZ"
_UTC = _dt.timezone.utc
_RULE = re.compile(r"\(([a-z0-9_]+)\)")


# --- reading the lines -------------------------------------------------------------------------------
def _payload(line):
    p = line.get("payload")
    return p if isinstance(p, dict) else {}


def _code(line):
    w = line.get("why") if isinstance(line, dict) else None
    return w.get("code") if isinstance(w, dict) and isinstance(w.get("code"), str) else None


def _why_text(line):
    w = line.get("why") if isinstance(line, dict) else None
    return w.get("text") if isinstance(w, dict) and isinstance(w.get("text"), str) else ""


def _rule(line):
    """The rule word a why text carries in brackets ("... (overlap): ..."), when it is one of why.py's."""
    m = _RULE.search(_why_text(line))
    return m.group(1) if m and m.group(1) in why_mod.RULE_WORDS else None


def _int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _time(value):
    if not isinstance(value, str):
        return None
    try:
        t = _dt.datetime.strptime(value, _TS).replace(tzinfo=_UTC)
        _pacific(t)                                 # a stamp that can't be shown in Pacific is no stamp
        return t
    except (ValueError, OverflowError):
        return None


def _pacific(t):
    return templates._local(t, "Pacific")          # noqa: SLF001 - the templates' own conversion


def _clock(t):
    """'1:10 PM' (Pacific)."""
    local = _pacific(t)
    return "%d:%02d %s" % (local.hour % 12 or 12, local.minute, "AM" if local.hour < 12 else "PM")


def _day(t):
    return templates._day(_pacific(t))              # noqa: SLF001 - 'Tue Dec 1'


def _full(t):
    """'Wed Dec 2, 11:00 AM' (Pacific)."""
    return "%s, %s" % (_day(t), _clock(t))


def _plural(n, one, many=None):
    return "%d %s" % (n, one if n == 1 else (many or one + "s"))


def _sentence(text):
    text = text.strip()
    if not text:
        return ""
    text = text[0].upper() + text[1:]
    return text if text.endswith(".") else text + "."


def _who(p):
    name, addr = p.get("from_name"), p.get("from_addr")
    name = name if isinstance(name, str) else ""
    addr = addr if isinstance(addr, str) else ""
    if not name and not addr:
        return UNREADABLE
    return holds.sender_name(name, addr)


class _Message:
    def __init__(self, subject, who, thread):
        self.subject, self.who, self.thread = subject, who, thread
        self.close = None
        self.replies = []        # (send line, found in Sent: True, False or None)
        self.notices = []
        self.failed = []         # error words of calendar calls that came back failed
        self.booking = False     # a check of one of her bookings (ruling 17), not an email


# --- the story --------------------------------------------------------------------------------------
def build(lines):
    """The story of one run's receipt lines (pass only lines that verified), as plain-text sections:
    title, scope, state, headline, halt, needs {title, rows}, calendar {title, rows}, threads {title,
    rows, more}, waiting, digest, cost, gate, mode, quiet (no mail read). Every value but quiet is a str or a
    list of them."""
    lines = [l for l in lines if isinstance(l, dict)]
    start = next((l for l in lines if l.get("kind") == "observed" and _payload(l).get("step") == "start"), None)
    sp = _payload(start) if start else {}
    mode = sp.get("mode") if isinstance(sp.get("mode"), str) else (lines[0].get("mode") if lines else None)
    mode = mode if isinstance(mode, str) else None
    source = sp.get("model_source")

    calls, results, lookups = {}, {}, {}   # keyed (run, seq): each run of a day numbers its lines from 1
    run = 0
    for line in lines:
        kind, p = line.get("kind"), _payload(line)
        run += _starts(line)
        if kind == "called" and _int(line.get("seq")) is not None:
            calls[run, line["seq"]] = line
        elif kind == "result" and _int(p.get("call_seq")) is not None:
            results[run, p["call_seq"]] = line
            called = calls.get((run, p["call_seq"]))
            cp = _payload(called) if called else {}
            if cp.get("call") == "sent_lookup" and isinstance(cp.get("message_id"), str):
                lookups[cp["message_id"]] = (p.get("found") is True) if p.get("ok") is True else None

    messages, threads = {}, []          # subject -> _Message; thread keys in the order first seen
    by_thread = {}
    bookings = {}                       # booking ID -> facts, in the order first written
    model_calls, cost, checks, blocked, approvals = 0, 0.0, 0, 0, 0
    digests, halted, summary, stamps = [], None, None, []
    stops = []                          # (halted line, her last step's time before it), for stops resumed after
    after = None                        # her last step's time, on the day's clock
    offers = {}                         # thread -> message whose times are open to its sender, in offer order
    runs, last = 0, None
    inbox = None                        # how many messages her last inbox read saw
    inbox_failed = False                # her last inbox read came back failed

    def message(subject, line=None):
        m = messages.get(subject)
        if m is None:
            p = _payload(line) if line is not None else {}
            thread = p.get("thread") if isinstance(p.get("thread"), str) else subject
            who, check = _who(p), subject.startswith(BOOKING) or p.get("step") == "booking_check"
            if check:                   # told on the thread of the email that made the booking, under its name
                made = messages.get(bookings.get(subject[len(BOOKING):], {}).get("subject"))
                thread, who = (made.thread, made.who) if made is not None else (subject, "(one of her bookings)")
            m = messages[subject] = _Message(subject, who, thread)
            m.booking = check
            if thread not in by_thread:
                by_thread[thread] = []
                threads.append(thread)
            by_thread[thread].append(m)
        return m

    for line in lines:
        kind, p, subject = line.get("kind"), _payload(line), line.get("subject")
        subject = subject if isinstance(subject, str) else ""
        runs += _starts(line)
        t = _time(line.get("ts"))
        if t is not None and not _wall(line, mode):     # a stop's real time is not a time of her day
            stamps.append(t)
            if kind != "halted":
                last = t
        if p.get("human_approval") is True or kind == "approved":
            approvals += 1
        if kind == "observed" and p.get("step") != "start" and subject:
            message(subject, line)
        elif kind == "called":
            call = p.get("call")
            res = _payload(results.get((runs, line.get("seq")), {}))
            if call == "mail_fetch" and res:
                inbox_failed = res.get("ok") is not True
                if not inbox_failed and _int(res.get("count")) is not None:
                    inbox = res["count"]
            if call == "model":
                model_calls += 1
                c = res.get("cost_usd")
                if isinstance(c, (int, float)) and not isinstance(c, bool) and 0 < c < MAX_COST:
                    cost += c
            elif call == "send":
                mid = p.get("message_id")
                found = lookups.get(mid) if isinstance(mid, str) else None
                if p.get("target") == "S2":
                    digests.append((line, found))
                elif subject in messages:
                    m = messages[subject]
                    (m.notices if p.get("to") == "owner" else m.replies).append((line, found))
            elif call in ("calendar_create", "calendar_get", "calendar_delete") and isinstance(p.get("target"), str):
                bid = p["target"]
                if res.get("ok") is True:
                    b = _booking(bookings, bid, subject)
                    if call == "calendar_create":
                        b["written"], b["subject"] = True, subject
                    elif call == "calendar_delete":
                        b["removed"] = True
                    elif res.get("found") is True:  # read back: there, whatever a delete said before
                        b["start"], b["end"] = _time(res.get("start")), _time(res.get("end"))
                        b["there"], b["removed"] = True, False
                    elif res.get("found") is False:
                        b["there"] = False
            if call in CALENDAR_CALLS and res and res.get("ok") is not True and subject in messages:
                messages[subject].failed.append(res.get("error") if isinstance(res.get("error"), str) else "")
        elif kind == "checked":
            checks += 1
            if p.get("passed") is not True:
                blocked += 1
        elif kind in ("decided", "held") and "outcome" in p and subject:
            m = message(subject, line)
            m.close = line
            _offer_state(offers, m)
            if p.get("outcome") == "book" and isinstance(p.get("booking_id"), str):
                b = _booking(bookings, p["booking_id"], subject)
                b["written"] = b["booked"] = True
        elif kind == "halted":
            halted, after = line, last
        elif _starts(line) and halted is not None:       # a start after the stop: resumed, so not the end
            stops.append((halted, after))
            halted = None
        elif kind == "summary":
            summary = line

    for m in messages.values():
        if _code(m.close) == "screen.sensitive_skip":
            m.who = SENSITIVE
    state = "halted" if halted is not None else "finished" if summary is not None else "so far"
    one_day = len({_day(t) for t in stamps}) <= 1
    when = (lambda t: _clock(t)) if one_day else _full

    # the counts: emails, then her bookings' checks that were held apart
    emails = [m for m in messages.values() if not m.booking]
    held_all = [m for m in messages.values() if m.close is not None and m.close.get("kind") == "held"]
    held = [m for m in held_all if not m.booking]
    closed = [m for m in emails if m.close is not None and m.close.get("kind") == "decided"]
    gone = len(held_all) - len(held)
    answered = noreply = unconfirmed = 0
    for m in closed:
        template = _payload(m.close).get("template")
        if isinstance(template, str) and template in templates.PROSPECT_TEMPLATES:
            if any(found for _, found in m.replies) or _code(m.close) == "policy.resume_sent":
                answered += 1
            else:
                unconfirmed += 1
        else:
            noreply += 1
    # a booking stands once read back there (or closed as booked with no read-back in this file), and
    # not removed since; a read-back that found it gone, or a removal not undone by one, takes it off
    standing = [(bid, b) for bid, b in bookings.items() if b["written"] and not b["removed"]
                and (b["there"] is True or (b["booked"] and b["there"] is None))]
    handled = "%d handled by her (%d answered, %d needed no reply%s)" % (
        len(closed), answered, noreply, ", %d closed without a confirmed reply" % unconfirmed if unconfirmed else "")
    unfinished = len(emails) - len(closed) - len(held)
    approved = _plural(approvals, "human approval")
    if messages:
        headline = "%s came in: %s, %d held for %s%s. %s on his calendar.%s %s." % (
            _plural(len(emails), "email"), handled, len(held), OWNER,
            ", %d not finished" % unfinished if unfinished else "", _plural(len(standing), "meeting"),
            " %s he removed by hand %s held for him." % (_plural(gone, "booking"), "is" if gone == 1 else "are")
            if gone else "", approved)
    elif inbox:
        headline = "No new mail: the inbox held %s, none of them new to her. %s." % (_plural(inbox, "message"), approved)
    else:
        headline = "No new mail: %s. %s." % ("her inbox read failed" if inbox_failed else "the inbox was empty"
                                            if inbox == 0 else "no inbox read is recorded", approved)

    # needs the owner
    rows, all_noticed = [], True
    for m in held_all:
        code = _code(m.close)
        reason = _hold_reason(m.close)
        if any(found for _, found in m.notices):
            pass
        elif any(found is False for _, found in m.notices):
            reason += " (her notice to him was not found in Sent)"
            all_noticed = False
        elif m.notices:
            reason += " (her notice to him was not confirmed in Sent)"
            all_noticed = False
        else:
            reason += " (no notice to him in this run)"
            all_noticed = False
        t = _time(m.close.get("ts"))
        rows.append([when(t) if t else "?", m.who, reason])
    title = "Needs %s (%d)" % (OWNER, len(held_all))
    if held_all and not any(m.replies for m in held_all):     # these emails, not their threads: Marcus got times before
        title += ": she sent no reply to these"
        if all_noticed:
            title += "; for each, a notice with a draft reply went to him"
        title += "."
    needs = {"title": title, "rows": rows}

    # on the calendar
    cal_rows = []
    for bid, b in standing:
        start, end = b["start"], b["end"]
        m = messages.get(b["subject"])
        if start is None and m is not None:
            start, end = _booked_slot(m)
        if start is not None:
            span = "%s Pacific" % _full(start)
            if end is not None and end > start:
                span += ", %d min" % ((end - start).total_seconds() // 60)
        else:
            span = "a booking whose time is not recorded"
        cal_rows.append([span, m.who if m is not None else "?"])
    calendar = {"title": "On %s's calendar (%d)%s" % (OWNER, len(standing), "" if mode == "live" else ", as sandbox files"),
                "rows": cal_rows}

    # thread by thread
    thread_rows = []
    for key in threads[:MAX_THREADS]:
        msgs = by_thread[key]
        first = msgs[0].who
        said, booked = [], False
        for m in msgs:
            text, booked = _told(m, booked)
            if m.who != first:
                text = "%s: %s" % (m.who, text[0].lower() + text[1:] if text else text)
            said.append(_sentence(text))
        thread_rows.append([first, " ".join(s for s in said if s)])
    extra = len(threads) - MAX_THREADS
    threads_out = {"title": "Thread by thread (%d)" % len(threads), "rows": thread_rows,
                   "more": "%s: %s shows every step." % (_plural(extra, "more thread"), paths.command("replay"))
                   if extra > 0 else ""}

    # waiting on a pick: threads whose last closing line offered times that her lookup found in Sent (a pick
    # read but cut off by a stop leaves the offer open: only a closing line closes it)
    offered = []
    for m in offers.values():
        if any(found for _, found in m.replies) and m.who not in offered:
            offered.append(m.who)
    waiting = "Waiting to pick one of her times: %s." % ", ".join(offered) if offered else ""

    # the digest
    digest_line = ""
    if digests:
        line, found = digests[-1]
        ds = _payload(line).get("slots")
        ds = ds if isinstance(ds, dict) else {}
        t = _time(line.get("ts"))
        counts = [_int(ds.get(k)) for k in ("handled", "held", "approvals")]
        said = ("\"Handled %d, held %d, human approvals %d.\"" % tuple(counts) if None not in counts
                else "its counts are not recorded")
        digest_line = "Digest to %s%s: %s %s" % (
            OWNER, " at %s" % when(t) if t else "", said,
            "Found in her Sent folder." if found else "Not found in her Sent folder." if found is False
            else "Not confirmed in her Sent folder.")
        if len(digests) > 1:
            digest_line += " (The last of %d.)" % len(digests)

    # cost, gate, halt
    cost_words = "none (stub)" if source == "stub" else "$%.4f%s" % (cost, " (recorded)" if source == "replayed" else "")
    cost_line = "Model calls %d, cost %s." % (model_calls, cost_words)
    gate = "The gate ran %s, once before each send or calendar write; %d blocked." % (_plural(checks, "time"),
                                                                                     blocked)
    said = [_stop_words(line, at, when, mode) for line, at in stops]
    halt = ""
    if said:
        halt = " Then resumed. ".join(said) + " Then resumed: this covers %s." % (
            "both runs" if runs == 2 else "all %d runs" % runs)
    if halted is not None:
        halt = (halt + " " if halt else "") + _halt_words(halted, after, when, mode)

    dates = sorted({_day(t): t for t in stamps}.items(), key=lambda kv: kv[1])
    span = dates[0][0] if len(dates) == 1 else "%s to %s" % (dates[0][0], dates[-1][0]) if dates else ""
    state_words = {"finished": "", "halted": "up to the stop, ", "so far": "so far, "}[state]
    scope = "%s%sfrom her receipts. Times are Pacific." % (span + ", " if span else "", state_words)
    return {"title": "What Doris did for %s" % OWNER, "scope": scope, "state": state, "headline": headline,
            "halt": halt, "needs": needs, "calendar": calendar, "threads": threads_out, "waiting": waiting,
            "digest": digest_line, "cost": cost_line, "gate": gate, "mode": mode or "", "quiet": not messages}


def _starts(line):
    """1 for a run's start line (each run of a day begins with one), else 0."""
    return 1 if line.get("kind") == "observed" and _payload(line).get("step") == "start" else 0


def _wall(line, mode):
    """A halted line the supervisor wrote: stamped by the wall clock, while a sandbox day runs on its own."""
    p = _payload(line)
    return line.get("kind") == "halted" and mode != "live" and ("stop_to_dead_ms" in p or "requested_utc" in p)


OPENS = ("offer", "reoffer", "move")                    # a closing line that leaves times open to the sender
SHUTS = ("book", "cancel", "suppress")                  # one that ends them (as does any hold)


def _offer_state(offers, m):
    p, code = _payload(m.close), _code(m.close)
    outcome = p.get("outcome") if isinstance(p.get("outcome"), str) else ""
    if outcome in OPENS:
        offers.pop(m.thread, None)
        offers[m.thread] = m
    elif outcome in SHUTS or outcome.startswith("hold:") or code in digest.CLOSE_CODES or code in digest.STOP_CODES:
        offers.pop(m.thread, None)


def _booking(bookings, bid, subject):
    return bookings.setdefault(bid, {"subject": subject, "start": None, "end": None, "written": False,
                                     "removed": False, "there": None, "booked": False})


def _hold_reason(close):
    code = _code(close)
    return STORY_REASONS.get(code) or templates.HOLD_REASONS.get(code) or _why_text(close)


def _booked_slot(m):
    """A booking's start and end from her confirmation's own slots, when no read-back recorded them."""
    for line, _found in m.replies:
        s = _payload(line).get("slots")
        if isinstance(s, dict) and _payload(line).get("target") == "T1":
            start = _time(s.get("start"))
            d = _int(s.get("duration"))
            d = d if d is not None and 0 < d <= MAX_MINUTES else None
            try:
                return start, (start + _dt.timedelta(minutes=d)) if start and d else None
            except OverflowError:
                return start, None
    return None, None


def _reply_slots(m):
    for line, _found in reversed(m.replies):
        s = _payload(line).get("slots")
        if isinstance(s, dict):
            return s
    return {}


OFFER_RULES = {"overlap": "taken", "outside_hours": "outside Shane's hours", "under_24h": "less than 24 hours away",
               "over_30d": "more than 30 days out", "holiday": "a holiday", "not_quarter_hour": "not on a quarter hour",
               "length": "too long"}
NO_REPLY_KINDS = {"auto": "Auto-reply: filed", "list": "List mail: filed", "bounce": "A bounce: filed",
                  "calendar_receipt": "A calendar receipt: filed", "self": "Her own mail: filed"}
FIXED = {
    "policy.file_fyi": "Newsletter or FYI: filed",
    "policy.file_injection": "Instructions or a lure only: filed, nothing done",
    "policy.silent_auto": "Auto-reply: no reply needed",
    "policy.no_thanks_close": "Said no thanks: her offer closed, no reply",
    "screen.not_addressed": "Not addressed to Shane alone: filed, no reply",
    "screen.sensitive_skip": "Sensitive mail: filed unread, no model, no reply",
    "screen.owner_auto_filed": "An auto-reply from Shane's address: filed",
    "screen.silenced_sender": "Filed with no reply, since they opted out",
    "screen.duplicate": "A repeat of a message already handled: dropped",
    "screen.own_mail": "Her own mail: dropped",
    "screen.quarantine": "Could not be decoded: quarantined; the run went on",
    "policy.superseded": "A newer message of theirs was answered instead",
    "policy.pick_resend": "Their reply didn't match one of her times: she sent the same times again",
    "policy.pick_before_offer": "Their reply came before her newest times: she sent those times again",
}


def _told(m, booked):
    """(what happened to one message, in the owner's words; whether its thread has now shown a booking)."""
    if m.close is None:
        return "Read; not finished", booked
    p, code = _payload(m.close), _code(m.close)
    outcome = p.get("outcome") if isinstance(p.get("outcome"), str) else ""
    s = _reply_slots(m)
    times = len(s.get("slots")) if isinstance(s.get("slots"), list) else 0
    asked = _time(s.get("asked"))
    start = _time(s.get("start"))
    if outcome.startswith("hold:"):
        return "Held for %s: %s" % (OWNER, _hold_reason(m.close)), booked
    if outcome == "book":
        at = _full(start) if start else "the time they chose"
        head = {"policy.book_clear_time": "Asked for %s: booked", "policy.pick_books": "Picked %s from her times: booked",
                "policy.yes_books": "Said yes to %s: booked",
                "policy.pick_plus_question": "Picked %s: booked; their question is on the digest",
                "policy.resume_booking": "Booked %s, finished after a restart"}.get(code, "Booked %s") % at
        if p.get("retried") is True and m.failed:
            head += " (%s)" % _retried(m.failed)
        return head, True
    if outcome in ("offer", "reoffer"):
        n = _plural(times, "time")
        if code == "policy.offer_no_clear_time":
            return "No exact time: offered %s" % n, booked
        if code == "policy.offer_conflict":
            what = "%s was" % _full(asked) if asked else "The time they asked for was"
            return "%s %s: offered %s" % (what, OFFER_RULES.get(_rule(m.close), "not bookable"),
                                          _plural(times, "other time")), booked
        if code == "policy.collision_reoffer":
            what = "Asked for %s; another" % _full(asked) if asked else "Another"
            return "%s event landed while she booked it, so she removed hers and offered %s" % (
                what, _plural(times, "other time")), booked
        if code == "policy.stale_pick_reoffer":
            return "The time they picked was no longer bookable: offered %s" % _plural(times, "fresh time"), booked
        if code == "policy.decline_second_offer":
            return "Said none of her times worked: offered %s" % _plural(times, "new one"), booked
        if code == "policy.offer_one_slot":
            return "Only one time was free: offered it", booked
        if code in FIXED:
            return FIXED[code], booked
        return "Offered %s" % n, booked
    if outcome in ("move", "cancel"):
        which = "that booking" if booked else ("their %s booking" % _full(start) if start else "their booking")
        if outcome == "move":
            return "Asked to move: she cancelled %s and offered %s" % (which, _plural(times, "new time")), False
        return "Asked to cancel: she cancelled %s and confirmed it" % which, False
    if code in ("screen.stop", "policy.suppress_opt_out"):
        dropped = p.get("dropped")
        k = len(dropped) if isinstance(dropped, list) else _int(dropped) or 0
        return "Opted out: she won't write to them again%s" % (
            " (%s dropped)" % _plural(k, "queued request") if k else ""), booked
    if code == "screen.no_reply_kind" and _rule(m.close) in NO_REPLY_KINDS:
        return NO_REPLY_KINDS[_rule(m.close)], booked
    if code in FIXED:
        return FIXED[code], booked
    return _why_text(m.close) or outcome or "Closed", booked


def _retried(errors):
    n = len(errors)
    times = "once" if n == 1 else "twice" if n == 2 else "%d times" % n
    if all(e == "timeout" for e in errors):
        return "the calendar timed out %s; she retried" % times
    return "a calendar call failed %s; she retried" % times


def _when_stopped(line, at, when, mode):
    """' at 10:31 AM', or for a stop stamped by the wall clock in a sandbox day, ' Sat Nov 28 at 12:58 AM
    Pacific, real time, after her 2:20 PM step'."""
    t = _time(line.get("ts"))
    if t is None:
        return ""
    if not _wall(line, mode):
        return " at %s" % when(t)
    return " %s at %s Pacific, real time%s" % (_day(t), _clock(t), ", after her %s step" % when(at) if at else "")


def _stop_text(line):
    text = _why_text(line) or "Halted."
    return _sentence(text[len("Halted: "):] if text.startswith("Halted: ") else text)


def _stop_words(line, at, when, mode):
    """A stop the day was resumed after."""
    return "Stopped%s: %s" % (_when_stopped(line, at, when, mode), _stop_text(line))


def _halt_words(line, at, when, mode):
    p = _payload(line)
    out = ["Halted%s: %s" % (_when_stopped(line, at, when, mode), _stop_text(line))]
    flight = p.get("in_flight")
    if isinstance(flight, dict):
        call = flight.get("call") if isinstance(flight.get("call"), str) else "a call"
        out.append("Cut off mid-call: %s." % call)
    elif flight == "none" or ("in_flight" in p and flight is None):    # the loop writes "none", the supervisor null
        out.append("Nothing was in flight.")
    else:
        out.append("What was in flight is not recorded.")
    dropped = p.get("dropped")
    if isinstance(dropped, list):
        out.append("%s dropped." % _plural(len(dropped), "queued item") if dropped else "Nothing queued was dropped.")
    else:
        out.append("The dropped work is unknown.")
    out.append("Nothing more goes out until %s." % paths.command("resume"))
    return " ".join(out)


# --- the terminal ------------------------------------------------------------------------------------
def _shown(text):
    return replay.shown(text, 2000)


def _cell(text, width):
    text = _shown(text)
    return text if len(text) <= width else text[:width - 3] + "..."


_GLUE = re.compile(r"(\d) (AM|PM)\b")
_COMMAND = re.compile(r"\S+ -m doris \w+")


def _wrap(text, width, indent=0):
    """One line, wrapped at width; continuation lines start under the first one's text (indent). A time
    keeps its AM or PM on its line, and a command stays whole, so it can be copied. The text is escaped
    already, so it holds no NUL of its own."""
    glued = _COMMAND.sub(lambda m: m.group(0).replace(" ", "\0"), _GLUE.sub("\\1\0\\2", text))
    out = textwrap.wrap(glued, width=max(width, indent + 20), subsequent_indent=" " * indent, break_on_hyphens=False)
    return [line.replace("\0", " ") for line in out] or [""]


def _table(rows, widths, width):
    """Rows of cells: every cell but the last padded to its column (cut at its width), the last one wrapped
    under itself."""
    if not rows:
        return ["  None."]
    cells = [[_cell(c, w) for c, w in zip(row[:-1], widths)] + [_shown(row[-1])] for row in rows]
    pads = [max(len(r[i]) for r in cells) for i in range(len(widths))]
    out = []
    for r in cells:
        head = "  " + "".join(c.ljust(pads[i]) + "  " for i, c in enumerate(r[:-1]))
        out += _wrap(head + r[-1], width, len(head))
    return out


def _width():
    """The terminal's width (72 to 120 columns), or WIDTH when the output is not a terminal."""
    try:
        if sys.stdout is not None and sys.stdout.isatty():
            return min(max(shutil.get_terminal_size((WIDTH, 24)).columns - 1, 72), 120)
    except (AttributeError, OSError, ValueError):
        pass
    return WIDTH


def render(story, width=None):
    """The story as terminal lines, every value escaped (replay.shown), so a name can't move the cursor
    or start a line of its own, and wrapped at width (the terminal's, by default)."""
    width = width or _width()

    def para(text, indent=0):
        return _wrap(_shown(text), width, indent)

    out = [""] + para("%s: %s" % (story["title"], story["scope"])) + para(story["headline"])
    if story["halt"]:
        out += para(story["halt"])
    if not story["quiet"]:                  # a run that read no mail has nothing to list
        out += [""] + para(story["needs"]["title"]) + _table(story["needs"]["rows"], (24, NAME_WIDTH), width)
        out += [""] + para(story["calendar"]["title"]) + _table(story["calendar"]["rows"], (48,), width)
        out += [""] + para(story["threads"]["title"]) + _table(story["threads"]["rows"], (NAME_WIDTH,), width)
        if story["threads"]["more"]:
            out += para("  " + story["threads"]["more"], 2)
    out.append("")
    for key in ("waiting", "digest"):
        if story[key]:
            out += para(story[key])
    out += para("%s %s" % (story["cost"], story["gate"]))
    return out


def _read(path):
    """(lines that verified, a torn tail after them, the check's result) for one receipt file, by verify's
    own steps."""
    with open(path, "rb") as fh:
        data = fh.read()
    whole, torn = receipts._split(data)          # noqa: SLF001 - verify's own steps, on this one read
    result, good = receipts._check(whole)        # noqa: SLF001
    return good, torn, result


def _earlier_runs(path):
    """The day's runs before this one that never finished (stopped, then resumed), oldest first, walking
    back from this run to the last one that finished with its summary: [(path, lines, torn)], and the path
    of an earlier run that did not verify (the walk stops there), or None. Only inside a runs folder."""
    folder = os.path.dirname(os.path.dirname(os.path.abspath(path)))
    if os.path.basename(folder) != "runs":
        return [], None
    mine = os.path.getmtime(path)
    others = []
    for name in os.listdir(folder):
        other = os.path.join(folder, name, "receipts.jsonl")
        if os.path.abspath(other) != os.path.abspath(path) and os.path.isfile(other):
            when = os.path.getmtime(other)
            if when < mine:
                others.append((when, name, other))
    found = []
    for _when, _name, other in sorted(others, reverse=True):
        good, torn, result = _read(other)
        if not result.ok:
            return found[::-1], other
        if any(l.get("kind") == "summary" for l in good):
            break
        found.append((other, good, torn))
    return found[::-1], None


def with_earlier_runs(receipts_path, run_lines):
    """This run's verified lines after those of the day's earlier stopped runs, so the watch page tells the
    same day as lines(). On any trouble reading the earlier runs, this run's lines alone."""
    try:
        earlier, _unverified = _earlier_runs(receipts_path)
    except Exception:                            # noqa: BLE001 - the page shows this run, as it did before
        return list(run_lines)
    return [l for _p, done, _t in earlier for l in done] + list(run_lines)


def lines(receipts_path, width=None):
    """The lines the demo prints when its run ends: the story, the chain's check, and where to look.
    The story is the day's: this run's lines after those of any earlier run that was stopped and resumed
    (a stop does not end a day; a finished run does). No receipt file (a start refused before her first
    line), no lines. A chain that does not verify gets one line naming where it breaks, and no story:
    nothing past a break can be trusted."""
    width = width or _width()
    try:
        good, torn, result = _read(receipts_path)
    except FileNotFoundError:
        return []
    except OSError as exc:
        return ["", "No summary: the receipt file can't be read (%s)." % exc.__class__.__name__]
    if not result.ok:
        return [""] + _wrap("No summary: the receipt chain is BROKEN at line %d (%s). Check it with %s %s" % (
            result.first_bad_seq, result.reason.split(": ", 1)[-1], paths.command("verify"),
            _shown(paths.shown(receipts_path))), width)
    try:
        earlier, unverified = _earlier_runs(receipts_path)
    except OSError:
        earlier, unverified = [], None
    day = [l for _p, run_lines, _t in earlier for l in run_lines] + good
    try:
        s = build(day)
        out = render(s, width)
    except Exception as exc:                     # noqa: BLE001 - one line, never a traceback at the demo's end
        return [""] + _wrap("No summary: it could not be built from these receipts (%s). Check them with %s and "
                            "%s." % (exc.__class__.__name__, paths.command("verify"), paths.command("replay")), width)
    if unverified:
        out += _wrap("An earlier run of this day does not verify (%s), so this story starts after it."
                     % _shown(paths.shown(unverified)), width)
    cut = torn or any(t for _p, _l, t in earlier)
    if not earlier:
        said = ("Receipts: %d lines verified; the line after them was cut short." % len(good) if cut
                else "Receipts: %d lines, chain verified." % len(good))
    else:
        said = "Receipts: %d lines in %d runs, each chain verified%s." % (
            len(day), len(earlier) + 1, "; a run's last line was cut short" if cut else "")
    out += _wrap(said, width)
    if s["mode"] == "sandbox":
        out += _wrap("See for yourself: her sent mail in sandbox/outbox/, her bookings in sandbox/calendar/, "
                     "every step with %s." % paths.command("replay"), width)
    return out
