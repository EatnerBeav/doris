"""python -m doris watch: the live page (live monitoring) and its Stop button (the kill switch).

    python -m doris watch [--port N]

One page, served on 127.0.0.1 only (never 0.0.0.0), so only this computer can open it. On a server, reach
it through an SSH tunnel. It is a live tail of the newest run's receipts, the sandbox's or live (the smoke's
own run of her loop included), whichever started last: the brief's four panes at the top (current step, tool calls, decisions, cost so far), then
every receipt line as it lands, newest first, each decision with its why in the receipt's own words. It
reads the receipt file and nothing else, so it cannot show anything the trace lacks, and it never writes
there. Every string reaches the page as text, never as HTML: a display name or a Message-ID is email text.
Model words never reach it (a classified line shows its class, from the closed list), nor any mail body
(no receipt holds one), nor an error's text (only its class name).

One Stop button. It does what `python -m doris stop` does, through the same function: supervisor.request_stop
for each of that command's modes, the sandbox and live, with the cause watch_button. The supervisor then kills
a running worker mid-call and writes the halted line, as for any stop. The smoke runs her loop without the
supervisor: her own halt check stops it between calls, and the page says so for that run. Only a POST from the page itself stops
her: it carries a token made fresh each time `watch` starts, and its Host and Origin must be this page's. A
request from any other site, or through a name rebound to this computer, is refused and stops nothing. The
page can stop her; it has no way to start, resume or release anything. No external assets: one inline script
and one inline style, each allowed by a fresh nonce per response.
"""

import argparse
import datetime as _dt
import hashlib
import hmac
import html
import json
import math
import os
import re
import secrets
import string
import sys
import threading
import time
import unicodedata
from collections import namedtuple
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import paths, receipts, supervisor, zones
from .loop import count_outcomes
from .shapes import CLASSES

HOST = "127.0.0.1"                  # fixed: no option serves the page to another computer
DEFAULT_PORT = 8765
POLL_MS = 750                       # how often the page asks for new lines
CAUSE = "watch_button"
WATCHED_MODES = ("sandbox", "live")
LABELS = ("Current step", "Tool calls", "Decisions", "Cost so far")    # the brief's four, in its words
MAX_BODY = 1024                     # the Stop form is one token
CLIP = 120                          # email text is cut to this many characters on the page
MAX_ROWS = 1000                     # the page holds the newest lines; the receipt file holds every one

Run = namedtuple("Run", "name folder mode started has_receipts smoke known")


# ------------------------------------------------------------------------------------------
# Which run: the newest one started, in the sandbox or live
# ------------------------------------------------------------------------------------------
_SMOKE_STAMP = re.compile(r"-smoke-(\d{8}T\d{6}Z)$")


def _started(folder, name="", smoke=False):
    """(when a run started, in ns, or None; whether that is known). A run's config.json is written once,
    before its worker starts and before the halt check. The smoke writes none: its run is named for the
    instant the smoke began (smoke.py, livetrace.STAMP). Failing both, the receipt file's last change,
    which moves with every line, so it is not known."""
    m = _SMOKE_STAMP.search(name) if smoke else None
    if m:
        try:
            began = _dt.datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=_dt.timezone.utc)
            return int(began.timestamp()) * 10 ** 9, True
        except ValueError:
            pass
    for name_, known in (("config.json", True), ("receipts.jsonl", False)):
        try:
            return os.stat(os.path.join(folder, name_)).st_mtime_ns, known
        except OSError:
            continue
    return None, False


def _listed(runs):
    try:
        return [(name, os.path.join(runs, name)) for name in os.listdir(runs)]
    except OSError:
        return []


def _run_folders():
    """(name, folder, mode, smoke) for every run folder the page follows: state/<mode>/runs/* for the sandbox
    and live, and the smoke's own runs of her loop, state/live/smoke/<stamp>/runs/* (smoke.py)."""
    for mode in WATCHED_MODES:
        for name, folder in _listed(paths.runs_dir(mode)):
            yield name, folder, mode, False
    smoke = os.path.join(paths.state_dir("live"), "smoke")
    for _stamp, stamp_folder in _listed(smoke):
        for name, folder in _listed(os.path.join(stamp_folder, "runs")):
            yield name, folder, "live", True


def newest_run():
    """The run started last, in the sandbox or live (the smoke's own runs of her loop included), or None. A
    run with receipts comes first: a run refused at the start (a halt in force, or another run holding the
    ledger) leaves a folder with its config.json and no receipts, and must not hide the run that is going.
    A folder with no receipts is watched only when no run has any (one starting, before its first line)."""
    best = {True: None, False: None}
    for name, folder, mode, smoke in _run_folders():
        started, known = _started(folder, name, smoke)
        if started is None:
            continue
        has = os.path.exists(os.path.join(folder, "receipts.jsonl"))
        if best[has] is None or (started, name) > best[has][0]:
            best[has] = ((started, name), Run(name, folder, mode, started, has, smoke, known))
    pick = best[True] or best[False]
    return pick[1] if pick is not None else None


# ------------------------------------------------------------------------------------------
# Words: code tokens as they are, email text as plain text
# ------------------------------------------------------------------------------------------
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
_TS = re.compile(r"^(\d{4}-\d\d-\d\d)T(\d\d:\d\d:\d\dZ)$")
_RUN_NAME = re.compile(r"^model-(live|replayed|stub|yes-man)-")
_HIDDEN = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})   # controls, direction overrides, lone surrogates, breaks


def _token(value, unknown="?"):
    """A code word (a call, a template ID, a booking ID, an outcome, a check) as it is; anything else is `unknown`."""
    if isinstance(value, bool):
        return unknown
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and _TOKEN.match(value):
        return value
    return unknown


def _text(value, limit=CLIP):
    """Email text for the page: invisible and direction-changing characters shown as U+FFFD, cut to `limit`."""
    if not isinstance(value, str):
        return ""
    shown = "".join("\ufffd" if unicodedata.category(ch) in _HIDDEN else ch for ch in value)
    return shown if len(shown) <= limit else shown[:limit - 1] + "\u2026"


def _int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _str(value):
    return value if isinstance(value, str) else None


def _usd(value):
    """A cost in dollars as a float, or None when it is not a plain finite number of at least zero."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except OverflowError:
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _payload(line):
    payload = line.get("payload")
    return payload if isinstance(payload, dict) else {}


def _why(line):
    why = line.get("why")
    return why if isinstance(why, dict) else {}


def _s(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


# ------------------------------------------------------------------------------------------
# One row per receipt line
# ------------------------------------------------------------------------------------------
STEPS = {"observed": "read", "classified": "classified", "decided": "decided", "checked": "checked",
         "called": "tool call", "result": "result", "held": "held", "halted": "halted", "error": "error",
         "summary": "summary"}
TONES = {"observed": "read", "classified": "read", "decided": "decision", "held": "held", "halted": "halted",
         "called": "call", "error": "error", "summary": "summary"}


def _flight_words(flight):
    if not isinstance(flight, dict):
        return "nothing was in flight"
    words = "%s %s (line %s) was in flight" % (_token(flight.get("call")), _token(flight.get("target")),
                                                _token(flight.get("seq")))
    return words + (", unconfirmed until resume reconciles it" if flight.get("unconfirmed") is True else "")


def _dropped_words(dropped):
    if dropped is None:
        return "the dropped work is unknown (the ledger could not be read)"
    if not isinstance(dropped, list):
        return "the dropped work is unknown"
    if not dropped:
        return "nothing queued was dropped"
    items = []
    for item in dropped:
        item = item if isinstance(item, dict) else {}
        name = item.get("template") or item.get("booking_id")
        items.append(_token(item.get("kind")) + (" " + _token(name) if name else ""))
    return "dropped %d: %s" % (len(dropped), ", ".join(items))


def _tally(p):
    return ("%d handled, %d held, %s, %s, %d sent to prospects, %s, %s, %s"
            % (_int(p.get("handled")), _int(p.get("held")), _s(_int(p.get("human_approvals")), "human approval"),
               _s(_int(p.get("errors")), "error"), _int(p.get("sends")), _s(_int(p.get("hold_notices")), "hold notice"),
               _s(_int(p.get("bookings")), "booking"), _s(_int(p.get("model_calls")), "model call")))


def _what(kind, p):
    """What the line says happened, in code words. Email text only in a read line's sender."""
    if kind == "observed":
        if p.get("step") == "start":
            return "Run started: %s, model %s." % (_token(p.get("mode")), _token(p.get("model_source")))
        name, addr = _text(p.get("from_name")), _text(p.get("from_addr"))
        if addr:
            text = "Read a message from %s." % ("%s <%s>" % (name, addr) if name else addr)
        else:
            text = "Read a message that could not be decoded."
        staged = p.get("simulated")
        if isinstance(staged, dict) and staged.get("fault"):
            text += " Staged fault: %s." % _token(staged.get("fault"))
        return text
    if kind == "classified":
        if p.get("ok") is False:
            return "The model's answer could not be used (model %s)." % _token(p.get("source"))
        cls = p.get("cls") if p.get("cls") in CLASSES else "a class not on the list"
        return "Classified as %s (model %s)." % (cls, _token(p.get("source")))
    if kind == "decided" and "outcome" in p:
        text = "Closed: %s" % _token(p.get("outcome"))
        if p.get("template") not in (None, "-"):
            text += ", %s" % _token(p.get("template"))
        if p.get("booking_id"):
            text += ", booking %s" % _token(p.get("booking_id"))
        return text + "."
    if kind == "decided":
        if p.get("step") == "plan":
            template = p.get("template")
            return "Plan: %s%s." % (_token(p.get("action")), ", " + _token(template) if template else "")
        return "Decided."
    if kind == "held":
        return "Held for the owner: %s." % _token(p.get("outcome"))
    if kind == "checked":
        if p.get("passed") is True:
            return "Gate on %s: passed." % _token(p.get("action"))
        return "Gate on %s: blocked at %s." % (_token(p.get("action")), _token(p.get("first_fail")))
    if kind == "called":
        text = "%s %s" % (_token(p.get("call")), _token(p.get("target")))
        if p.get("call") == "send" and p.get("to"):
            text += " to the %s" % _token(p.get("to"))
        if _int(p.get("attempt")) > 1:
            text += ", attempt %d" % p["attempt"]
        return text + "."
    if kind == "result":
        ok = p.get("ok") is True
        text = "Answer to line %s: %s" % (_token(p.get("call_seq"), "none"), "ok" if ok else "failed")
        if not ok and p.get("error"):
            text += " (%s)" % _token(p.get("error"))
        cost = _usd(p.get("cost_usd"))
        if cost:
            text += ", $%.4f" % cost
        elif "cost_usd" in p and cost is None:
            text += ", a cost that can't be read"
        return text + "."
    if kind == "halted":
        text = "Cause %s; %s; %s." % (_token(p.get("cause")), _flight_words(p.get("in_flight")),
                                      _dropped_words(p.get("dropped")))
        dead = p.get("stop_to_dead_ms")
        if isinstance(dead, int) and not isinstance(dead, bool) and 0 <= dead < 10 ** 9:
            text += " Worker dead %d ms after the stop." % dead
        return text
    if kind == "error" and "moved_to" in p:     # the writer's own line: a cut-off line moved aside, on reopening
        return "A cut-off line (%d bytes after line %s) was moved aside to %s." % (
            _int(p.get("torn_bytes")), _token(p.get("after_seq")), _token(p.get("moved_to")))
    if kind == "error":
        error = p.get("error")
        name = error.split(":", 1)[0] if isinstance(error, str) else None
        return "Error in %s: %s." % (_token(p.get("step")), _token(name))
    if kind == "summary":
        return "Run closed: %s." % _tally(p)
    return ""


def _tone(kind, p):
    if kind == "checked":
        return "decision" if p.get("passed") is True else "blocked"
    if kind == "result":
        return "result" if p.get("ok") is True else "failed"
    return TONES.get(kind, "read")


def _pacific(utc):
    """An aware instant on the Pacific wall clock, as the terminal tells times: 'Tue Dec 1, 8:10:05 AM'."""
    t = zones.to_local(utc, "Pacific")
    clock = "%d:%02d:%02d" % ((t.hour + 11) % 12 + 1, t.minute, t.second)
    return "%s %s %d, %s %s" % ("Mon Tue Wed Thu Fri Sat Sun".split()[t.weekday()],
                                "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()[t.month - 1], t.day,
                                clock, "AM" if t.hour < 12 else "PM")


def _time(ts):
    """A receipt line's time (UTC on the line) on the Pacific clock, as the terminal shows it."""
    m = _TS.match(ts) if isinstance(ts, str) else None
    if not m:
        return "?"
    try:
        return _pacific(_dt.datetime.strptime("%s %s" % m.groups(), "%Y-%m-%d %H:%M:%SZ").replace(
            tzinfo=_dt.timezone.utc))
    except (ValueError, OverflowError):     # not a date, or one the Pacific clock can't reach (year 1)
        return "?"


def row(line):
    """One receipt line as the page shows it: every value a plain string (or the seq)."""
    kind, p, why = line.get("kind"), _payload(line), _why(line)
    hash_ = line.get("hash")
    return {
        "seq": _int(line.get("seq")),
        "hash": _token(hash_[:8], "") if isinstance(hash_, str) else "",
        "time": _time(line.get("ts")),
        "step": "start" if kind == "observed" and p.get("step") == "start" else STEPS.get(kind, "?"),
        "message": _text(line.get("subject")),
        "what": _what(kind, p),
        "why": _text(why.get("text"), 400),       # written by code from the fixed list (why.py)
        "code": _token(why.get("code"), ""),
        "tone": _tone(kind, p),
    }


# ------------------------------------------------------------------------------------------
# The whole view of one run
# ------------------------------------------------------------------------------------------
MODE_WORDS = {"sandbox": "Sandbox run: the sample inbox, a calendar of files and a simulated clock.",
              "live": "Live run: her own mailbox and the owner's calendar."}
SOURCE_WORDS = {
    "stub": "No key and no recorded answers, so a built-in keyword stand-in (stub) reads the mail; "
            "every receipt says stub.",
    "replayed": "Model answers replayed from a recorded real run; no model is called now.",
    "live": "Model called live; the cost is measured.",
    "yes-man": "No model: the yes-man stand-in (redteam) gives the most permissive valid answer and obeys any "
               "instruction in the mail, so the gate alone bounds what she does.",
}
# smoke.py runs her loop itself, not under the supervisor: her own halt check stops it, between calls
SMOKE_WORDS = ("The smoke's own run of her loop, on its one test request, without the kill switch's supervisor: "
               "a Stop halts such a run at her next check, after any call in flight returns, not mid-call.")


def _chain(path):
    """(the lines that verified, the chain ok or only still being written, the chain in words, the line it
    breaks at or None), from one read of the file and the verify command's own check. Only lines that
    verify are shown or counted: nothing past a break can be trusted."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        return [], False, "The receipt file can't be read (%s)." % exc.__class__.__name__, None
    whole, torn = receipts._split(data)          # noqa: SLF001 - verify's own steps, on this one read
    result, good = receipts._check(whole)        # noqa: SLF001
    shown = _text(path, 400)
    if not result.ok:
        reason = result.reason.split(": ", 1)[-1]
        return good, False, ("Chain BROKEN at line %d: %s. Check it with %s %s"
                             % (result.first_bad_seq, reason, paths.command("verify"), shown)), result.first_bad_seq
    if torn and good and good[-1]["kind"] in receipts.CLOSING_KINDS and not receipts._summary_follows(good[-1]):  # noqa: SLF001
        return good, False, ("Chain BROKEN after its closing line (line %d): %d bytes follow it that are not a whole "
                             "line. Check it with %s %s"
                             % (len(good), len(torn), paths.command("verify"), shown)), len(good) + 1
    if torn:
        return good, True, ("Chain verified through line %d; the line after it is still being written, "
                            "or was cut short." % len(good)), None
    moved = ["a cut-off line moved aside at line %d" % l["seq"] for l in good
             if l["kind"] == "error" and l["subject"] == "torn-tail"]
    return good, True, "Chain verified: %d lines, each hash matching the line before it%s." % (
        len(good), " (%s)" % "; ".join(moved) if moved else ""), None


def _unanswered(lines):
    """The newest called line with no result naming it, or None."""
    answered = {cs for cs in (_payload(l).get("call_seq") for l in lines if l.get("kind") == "result")
                if isinstance(cs, int) and not isinstance(cs, bool)}
    for line in reversed(lines):
        if line.get("kind") == "called" and line.get("seq") not in answered:
            return line
    return None


def _call_words(line):
    p = _payload(line)
    return "%s %s" % (_token(p.get("call")), _token(p.get("target")))


def _cost(source, lines, model_calls):
    """The Cost so far pane: (value, note), worded as the terminal words a run's cost."""
    total, unread = 0.0, []
    for line in lines:
        p = _payload(line)
        if line.get("kind") != "result" or "cost_usd" not in p:
            continue
        cost = _usd(p["cost_usd"])
        if cost is None:
            unread.append(str(_int(line.get("seq"))))
        else:
            total += cost
    note = _s(model_calls, "model call") + "."
    if unread:
        note += (" The cost on line %s can't be read." % unread[0] if len(unread) == 1 else
                 " The costs on lines %s can't be read." % ", ".join(unread))
    if source == "stub":
        return "none (stub)", note                 # as the terminal says it
    if unread:
        return "unknown", note
    return "$%.4f%s" % (total, " (recorded)" if source == "replayed" else ""), note


_WHY_KINDS_WORDS = (("plan", "plan"), ("close", "close"), ("decision", "decision"), ("checked", "gate check"),
                    ("held", "hold"), ("halted", "halt"))


def _decision_note(decisions):
    """What the Decisions pane counts (every line with a why), and the latest why."""
    if not decisions:
        return "None yet. Each one shows its why below."
    counts = dict.fromkeys(k for k, _ in _WHY_KINDS_WORDS)
    for line in decisions:
        kind, p = line.get("kind"), _payload(line)
        key = kind if kind != "decided" else ("plan" if p.get("step") == "plan" else
                                              "close" if "outcome" in p else "decision")
        counts[key] = (counts[key] or 0) + 1
    parts = [_s(counts[k], word) for k, word in _WHY_KINDS_WORDS if counts[k]]
    return "Lines with a why: %s. Latest: %s" % (", ".join(parts), _text(_why(decisions[-1]).get("text"), 400))


def _clock(ns):
    return "%s Pacific" % _pacific(_dt.datetime.fromtimestamp(ns / 1e9, _dt.timezone.utc))


def snapshot(run):
    """Everything the page shows for this run (None: no run yet), as plain strings: the title, the run's
    words, the four panes, and one row per receipt line that verified, oldest first."""
    if run is None:
        return {"run": "", "title": "Doris watch: no run yet", "run_line": "No run yet.", "source": "",
                "status": "", "chain": "", "chain_ok": True, "state": "none",
                "panes": [{"label": label, "value": "\u2014", "note": ""} for label in LABELS],
                "rows": [], "after": 0, "more": "",
                "empty": "No run yet. Start one with %s, or %s; this page picks it up as soon as it "
                         "starts." % (paths.command("demo"), paths.command("run"))}
    path = os.path.join(run.folder, "receipts.jsonl")
    landed = None
    if os.path.exists(path):
        lines, chain_ok, chain, broken_at = _chain(path)
        try:
            landed = os.stat(path).st_mtime_ns
        except OSError:
            landed = None
    else:
        lines, chain_ok, chain, broken_at = [], True, "No receipt line yet.", None
    start = next((l for l in lines if l.get("kind") == "observed" and _payload(l).get("step") == "start"), None)
    named = _RUN_NAME.match(run.name)
    source = _str(_payload(start).get("model_source")) if start else (named.group(1) if named else None)
    mode = (_str(_payload(start).get("mode")) if start else None) or run.mode
    rows = [row(l) for l in lines]

    tally = count_outcomes([dict(l, payload=_payload(l)) for l in lines])
    counts = "%d handled, %d held, %s" % (tally["handled"], tally["held"],
                                          _s(tally["human_approvals"], "human approval"))
    last = lines[-1] if lines else None
    last_kind = last.get("kind") if last else None
    halted = next((l for l in reversed(lines) if l.get("kind") == "halted"), None) \
        if last_kind in receipts.CLOSING_KINDS else None
    flight = _unanswered(lines)
    calls = [l for l in lines if l.get("kind") == "called"]
    failed = sum(1 for l in lines if l.get("kind") == "result" and _payload(l).get("ok") is not True)
    decisions = [l for l in lines if l.get("kind") in receipts.WHY_KINDS]
    model_calls = sum(1 for l in calls if _payload(l).get("call") == "model")
    since = " Last line landed at %s." % _clock(landed) if landed is not None else ""

    if last is None:
        current = ("Starting.", "")
    elif flight is not None and last_kind not in receipts.CLOSING_KINDS:
        current = ("Tool call: %s, no answer yet." % _call_words(flight),
                   "Line %d. No result line answers it yet." % _int(flight.get("seq")))
    else:
        r = rows[-1]
        current = (r["what"], "Line %d, %s.%s" % (r["seq"], r["step"], " Why: " + r["why"] if r["why"] else ""))

    if not chain_ok and broken_at is None:
        state, status = "unreadable", chain
    elif not chain_ok:
        state = "broken"
        if not lines:
            status = "Chain broken at line %d: no line can be trusted, so none is shown." % broken_at
            current = ("Nothing to show: no line can be trusted.", "")
        else:
            status = "Chain broken at line %d: only the %s before it %s shown and counted." % (
                broken_at, "line" if len(lines) == 1 else "%d lines" % len(lines), "is" if len(lines) == 1 else "are")
    elif last is None:
        state, status = "starting", "Starting: no receipt line yet."
    elif halted is not None:            # a halted line closes the run (her own is followed by her summary)
        state, status = "halted", "Halted: %s before the stop." % counts
        current = (_text(_why(halted).get("text"), 400) or "Halted.", _halt_note(halted))
    elif last_kind == "summary" and _payload(last).get("read_failed") is True:
        state = "ended"
        status = "Ended: the inbox could not be read, so the run ended there (%s before it)." % counts
        current = ("Ended: the inbox could not be read.", "Line %d." % rows[-1]["seq"])
    elif last_kind == "summary":
        state, status = "finished", "Finished: %s." % counts
        current = (status, "Line %d." % rows[-1]["seq"])
    else:
        state = "waiting" if flight is not None else "running"
        status = "No closing line yet: %s so far.%s" % (counts, since)

    closed = last_kind in receipts.CLOSING_KINDS
    if flight is not None and not closed:
        call_note = "1 with no answer yet: %s (line %d)." % (_call_words(flight), _int(flight.get("seq")))
    elif flight is not None:
        call_note = "Cut off by the stop: %s (line %d)." % (_call_words(flight), _int(flight.get("seq")))
    elif calls:
        call_note = "All answered%s." % ("; %d came back failed" % failed if failed else "")
    else:
        call_note = "None yet."
    cost = _cost(source, lines, model_calls)
    panes = [
        {"label": LABELS[0], "value": current[0], "note": current[1]},
        {"label": LABELS[1], "value": str(len(calls)), "note": call_note},
        {"label": LABELS[2], "value": str(len(decisions)), "note": _decision_note(decisions)},
        {"label": LABELS[3], "value": cost[0], "note": cost[1]},
    ]
    if not chain_ok and not lines:          # nothing to count from: no number at all, never a zero
        for pane in panes[1:]:
            pane.update(value="?", note="No line can be trusted.")
    elif not chain_ok:
        only = " From the %s before the break only." % ("line" if len(lines) == 1 else "%d lines" % len(lines))
        for pane in panes[1:]:
            pane["note"] += only
    more = ("Showing the newest %d of %d lines. The receipt file holds every one." % (MAX_ROWS, len(rows))
            if len(rows) > MAX_ROWS else "")
    name = _text(run.name, 200)          # a folder name is bytes on disk, not always text
    return {"run": name, "title": "Doris watch: " + name, "run_line": "Run %s" % name,
            "source": " ".join(w for w in (MODE_WORDS.get(mode, ""), SOURCE_WORDS.get(source, ""),
                                           SMOKE_WORDS if run.smoke else "") if w),
            "status": status, "chain": chain, "chain_ok": chain_ok, "state": state, "panes": panes,
            "rows": rows, "after": max([r["seq"] for r in rows] or [0]), "more": more, "empty": ""}


def unreadable(run, exc):
    """The page when a run's receipts can't be shown at all: still the page, so still its Stop button."""
    path = _text(os.path.join(run.folder, "receipts.jsonl"), 400) if run is not None else ""
    name = _text(run.name, 200) if run is not None else ""
    return {"run": name, "title": "Doris watch: receipts not shown",
            "run_line": "Run %s" % name if run is not None else "", "source": "",
            "status": "The receipts could not be shown (%s). The Stop button still works. Check them with "
                      "%s %s" % (exc.__class__.__name__, paths.command("verify"), path),
            "chain": "", "chain_ok": False, "state": "unreadable",
            "panes": [{"label": label, "value": "?", "note": ""} for label in LABELS],
            "rows": [], "after": 0, "more": "", "empty": ""}


def _halt_note(line):
    p = _payload(line)
    return "When stopped, %s; %s." % (_flight_words(p.get("in_flight")), _dropped_words(p.get("dropped")))


# ------------------------------------------------------------------------------------------
# Stop: what `python -m doris stop` does
# ------------------------------------------------------------------------------------------
def stop_modes():
    """The modes `python -m doris stop` halts, from that command itself."""
    from . import __main__ as cli          # here, not at the top: the command line imports this module
    return tuple(cli.STOP_MODES)


def stop_now(modes):
    """What `python -m doris stop` does, from the page: supervisor.request_stop for each of that command's
    modes, in its order, with the cause watch_button. Every mode is tried even when one fails.
    Returns ([(mode, cause in force)], [(mode, error class name)])."""
    done, failed = [], []
    for mode in modes:
        try:
            done.append((mode, supervisor.request_stop(mode, CAUSE)))
        except Exception as exc:           # noqa: BLE001 - the other mode is still stopped, and this is said
            failed.append((mode, exc.__class__.__name__))
    return done, failed


_WHERE = {"sandbox": "the sandbox", "live": "live"}


def pressed_words(done, failed, now):
    """The page's one line about a press of its Stop button."""
    words = ["Stop pressed %s Pacific." % _pacific(now)]
    if done:
        if all(cause == CAUSE for _, cause in done):
            words.append("Halted in %s." % " and ".join(_WHERE.get(m, m) for m, _ in done))
        else:
            words.append("Halted in %s." % " and ".join(
                "%s (%s)" % (_WHERE.get(m, m), supervisor.CAUSE_WORDS.get(c, c)) for m, c in done))
    if failed:
        words.append("NOT halted: %s. Run %s now."
                     % (", ".join("%s (%s)" % (m, e) for m, e in failed), paths.command("stop")))
    else:
        words.append("A run under the kill switch is killed mid-call and its receipt names what was dropped. Nothing more "
                     "is sent or booked until %s." % paths.command("resume"))
    return " ".join(words)


# ------------------------------------------------------------------------------------------
# Who may ask
# ------------------------------------------------------------------------------------------
_LOCAL_HOST = re.compile(r"^(?:127\.0\.0\.1|localhost)(?::\d{1,5})?$")


def host_ok(host):
    """The Host is this computer by its own name (any port: an SSH tunnel may use another). A name rebound
    to 127.0.0.1 (DNS rebinding) is not, so another site can never read the page or its token."""
    return bool(host) and _LOCAL_HOST.match(host.lower()) is not None


def stop_refusal(host, origin, fetch_site, content_type, body, token):
    """Why a POST to /stop is not the page's own Stop, or None when it is."""
    if not host_ok(host):
        return 403, "it names another host"
    if origin is not None and origin.lower() not in ("http://" + host.lower(), "https://" + host.lower()):
        return 403, "it came from another site"     # https: the page behind a TLS proxy on this computer
    if fetch_site is not None and fetch_site.lower() != "same-origin":
        return 403, "the browser says it came from another site"
    if (content_type or "").split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
        return 415, "it is not the page's form"
    try:
        sent = parse_qs(body.decode("ascii"), keep_blank_values=True).get("token", [])
    except (UnicodeDecodeError, ValueError):
        sent = []
    if len(sent) != 1 or not hmac.compare_digest(sent[0].encode("ascii", "replace"), token.encode("ascii")):
        return 403, ("it does not carry this page's token (a page left open from an earlier start of watch "
                     "carries an old one: reload the page, then press Stop)")
    return None


# ------------------------------------------------------------------------------------------
# The page
# ------------------------------------------------------------------------------------------
_e = html.escape

_ROW = ('<tr class="t-{tone}"><td class="seq">{seq}<span class="hash">{hash}</span></td><td class="time">{time}</td>'
        '<td class="step">{step}</td><td class="msg" title="{message}">{message}</td><td class="what">{what}</td>'
        '<td class="why">{why}{code}</td></tr>')


def _row_html(r):
    return _ROW.format(tone=_e(r["tone"]), seq=int(r["seq"]), hash=_e(r["hash"]), time=_e(r["time"]),
                       step=_e(r["step"]), message=_e(r["message"]), what=_e(r["what"]), why=_e(r["why"]),
                       code='<span class="code">%s</span>' % _e(r["code"]) if r["code"] else "")


_PAGE = string.Template("""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>$title</title>
<noscript><meta http-equiv="refresh" content="3"></noscript>
<style nonce="$nonce">
:root {
  --paper: #edf0f3; --ink: #17202a; --muted: #5b6673; --rule: #c9d1da; --pane: #f7f8fa;
  --signal: #c8102e; --signal-ink: #ffffff; --amber: #8a5300; --amber-bar: #d89a2b; --ok: #1d6b50;
  --alarm-bg: #fbe2e6;
  color-scheme: light dark;
}
@media (prefers-color-scheme: dark) {
  :root {
    --paper: #10151b; --ink: #e3e8ee; --muted: #9aa5b2; --rule: #2a3440; --pane: #171e26;
    --signal: #e0283f; --amber: #f2b35b; --amber-bar: #c98a1e; --ok: #74d1a3; --alarm-bg: #3b151b;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--paper); color: var(--ink);
  font: 16px/1.45 Seravek, "Gill Sans Nova", Ubuntu, Calibri, "DejaVu Sans", source-sans-pro, sans-serif;
  font-variant-numeric: tabular-nums;
}
code { font-size: .92em; }
code, .run, .hash, .code, .time { font-family: ui-monospace, "Cascadia Mono", "Source Code Pro", Menlo, Consolas, monospace; }
header {
  position: sticky; top: 0; z-index: 2; background: var(--paper); border-bottom: 1px solid var(--rule);
  display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 12px 40px; padding: 18px 28px 16px;
}
h1 { margin: 0; font-size: 1.6rem; font-weight: 600; letter-spacing: -0.01em; }
header p { margin: 2px 0 0; max-width: 78ch; }
.run { font-size: .85rem; color: var(--muted); overflow-wrap: anywhere; }
#source { color: var(--muted); }
#status { font-weight: 600; }
#chain { font-size: .9rem; color: var(--ok); }
body[data-chain="broken"] #chain { color: var(--ink); background: var(--alarm-bg); font-weight: 700; padding: 4px 8px; }
.stop { display: grid; grid-template-columns: 104px minmax(0, 22rem); gap: 8px 16px; align-items: center; align-content: start; }
.stop .sign { border-radius: 4px; }
.stop .sign:focus-within { outline: 3px solid var(--ink); outline-offset: 4px; }
.stop button {
  display: block; width: 104px; height: 104px; border: 0; cursor: pointer;
  background: var(--signal); color: var(--signal-ink); font: inherit; font-size: 1.35rem; font-weight: 700;
  clip-path: polygon(29.3% 0, 70.7% 0, 100% 29.3%, 100% 70.7%, 70.7% 100%, 29.3% 100%, 0 70.7%, 0 29.3%);
}
.stop button:focus { outline: none; }
.stop button:active { filter: brightness(.85); }
.stop p { margin: 0; font-size: .9rem; }
#pressed { grid-column: 1 / -1; font-weight: 600; max-width: calc(104px + 16px + 22rem); }
#pressed:empty, #lost:empty, #empty:empty, #more:empty { display: none; }
#checked { font-size: .85rem; color: var(--muted); }
main { padding: 0 28px 40px; }
#lost { margin: 16px 0 0; padding: 8px 12px; background: var(--alarm-bg); font-weight: 700; }
.panes {
  display: grid; grid-template-columns: minmax(0, 2.2fr) repeat(3, minmax(0, 1fr));
  margin: 20px 0 8px; border-top: 1px solid var(--rule); border-bottom: 1px solid var(--rule);
}
.pane { padding: 14px 18px 16px; border-left: 1px solid var(--rule); background: var(--pane); }
.pane:first-child { border-left: 4px solid var(--rule); }
body[data-state="waiting"] .pane:first-child { border-left-color: var(--amber-bar); }
body[data-state="halted"] .pane:first-child, body[data-state="ended"] .pane:first-child,
body[data-state="broken"] .pane:first-child, body[data-state="unreadable"] .pane:first-child { border-left-color: var(--signal); }
body[data-state="ended"] #status, body[data-state="broken"] #status, body[data-state="unreadable"] #status {
  background: var(--alarm-bg); padding: 4px 8px; }
body[data-state="finished"] .pane:first-child { border-left-color: var(--ok); }
.pane h2 { margin: 0 0 6px; font-size: .95rem; font-weight: 600; color: var(--muted); }
.pane .value { margin: 0; font-size: 1.9rem; line-height: 1.15; font-weight: 600; overflow-wrap: anywhere; }
.pane:first-child .value { font-size: 1.3rem; line-height: 1.3; }
.pane .note { margin: 6px 0 0; font-size: .9rem; color: var(--muted); overflow-wrap: anywhere; }
.tail h2 { margin: 28px 0 2px; font-size: 1.15rem; }
.tail .lede { margin: 0 0 12px; color: var(--muted); max-width: 78ch; }
.scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: .9rem; }
th { text-align: left; font-weight: 600; color: var(--muted); padding: 6px 10px; border-bottom: 1px solid var(--rule); }
td { padding: 6px 10px; border-bottom: 1px solid var(--rule); vertical-align: top; }
td.seq { white-space: nowrap; border-left: 4px solid transparent; }
.hash { display: block; font-size: .75rem; color: var(--muted); }
td.time { white-space: nowrap; color: var(--muted); font-size: .82rem; }
td.step { white-space: nowrap; }
td.msg { max-width: 16rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--muted); }
td.what { min-width: 18rem; }
td.why { min-width: 16rem; }
.code { display: block; font-size: .75rem; color: var(--muted); }
tr.t-call td, tr.t-result td, tr.t-read td { color: var(--muted); }
tr.t-call .hash, tr.t-result .hash { color: var(--muted); }
tr.t-decision td.seq { border-left-color: var(--ink); }
tr.t-held td.seq { border-left-color: var(--amber-bar); }
tr.t-held td.why { color: var(--amber); font-weight: 600; }
tr.t-halted td.seq, tr.t-blocked td.seq, tr.t-failed td.seq, tr.t-error td.seq { border-left-color: var(--signal); }
tr.t-halted td { font-weight: 700; }
tr.t-summary td.seq { border-left-color: var(--ok); }
@media (max-width: 860px) {
  header { position: static; grid-template-columns: 1fr; padding: 14px 16px; }
  main { padding: 0 16px 32px; }
  .panes { grid-template-columns: 1fr 1fr; }
  .pane:first-child { grid-column: 1 / -1; }
  .pane:nth-child(2) { border-left: 0; }
}
</style>
</head>
<body data-chain="$chain_state" data-state="$state" data-boot="$boot">
<header>
  <div>
    <h1>Doris watch</h1>
    <p class="run" id="run">$run_line</p>
    <p id="source">$source</p>
    <p id="status">$status</p>
    <p id="chain">$chain</p>
    <p id="checked">$checked</p>
  </div>
  <form class="stop" method="post" action="/stop">
    <input type="hidden" name="token" value="$token">
    <div class="sign"><button type="submit">Stop</button></div>
    <p>Halts Doris now in the sandbox and live, as <code id="stop-cmd">$stop_cmd</code> does. A run under the kill switch is killed mid-call.
      Only <code>$resume_cmd</code>, on the command line, lets her run again.</p>
    <p id="pressed" role="status">$pressed</p>
  </form>
</header>
<main>
<p id="lost" role="alert"></p>
<section class="panes" aria-label="Where she is now">
  <div class="pane"><h2>$label0</h2><p class="value" id="pane-0-value">$value0</p><p class="note" id="pane-0-note">$note0</p></div>
  <div class="pane"><h2>$label1</h2><p class="value" id="pane-1-value">$value1</p><p class="note" id="pane-1-note">$note1</p></div>
  <div class="pane"><h2>$label2</h2><p class="value" id="pane-2-value">$value2</p><p class="note" id="pane-2-note">$note2</p></div>
  <div class="pane"><h2>$label3</h2><p class="value" id="pane-3-value">$value3</p><p class="note" id="pane-3-note">$note3</p></div>
</section>
<section class="tail">
  <h2>Every step, newest first</h2>
  <p class="lede">One row per receipt line, as it lands. A decision's why is written by code from a fixed list,
    never by the model or an email.</p>
  <p id="empty">$empty</p>
  <p id="more">$more</p>
  <div class="scroll"><table>
    <thead><tr><th scope="col">Line</th><th scope="col">Time (Pacific)</th><th scope="col">Step</th>
      <th scope="col">Message</th><th scope="col">What happened</th><th scope="col">Why</th></tr></thead>
    <tbody id="steps" data-run="$run" data-after="$after" data-tip="$tip">$rows</tbody>
  </table></div>
</section>
</main>
<script nonce="$nonce">
(function () {
  "use strict";
  var POLL_MS = $poll;
  var MAX_ROWS = $max_rows;
  var boot = document.body.getAttribute("data-boot");
  var steps = document.getElementById("steps");
  var run = steps.getAttribute("data-run") || "";
  var after = parseInt(steps.getAttribute("data-after"), 10) || 0;
  var tip = steps.getAttribute("data-tip") || "";

  function put(id, value) {
    var el = document.getElementById(id);
    if (el !== null && el.textContent !== value) { el.textContent = value; }
  }
  function cell(tr, cls, value) {
    var td = document.createElement("td");
    td.className = cls;
    td.textContent = value;
    tr.appendChild(td);
    return td;
  }
  function tag(parent, cls, value) {
    var span = document.createElement("span");
    span.className = cls;
    span.textContent = value;
    parent.appendChild(span);
  }
  function row(r) {
    var tr = document.createElement("tr");
    tr.className = "t-" + r.tone;
    tag(cell(tr, "seq", String(r.seq)), "hash", r.hash);
    cell(tr, "time", r.time);
    cell(tr, "step", r.step);
    cell(tr, "msg", r.message).title = r.message;
    cell(tr, "what", r.what);
    var why = cell(tr, "why", r.why);
    if (r.code) { tag(why, "code", r.code); }
    return tr;
  }
  function show(s) {
    var i;
    // watch was started again: this page's Stop token is void, so load the page afresh (and its new token)
    if (s.boot !== boot) { window.location.reload(); return; }
    if (s.reset) {
      while (steps.firstChild) { steps.removeChild(steps.firstChild); }
    }
    for (i = 0; i < s.rows.length; i += 1) {
      steps.insertBefore(row(s.rows[i]), steps.firstChild);
    }
    while (steps.childNodes.length > MAX_ROWS) { steps.removeChild(steps.lastChild); }
    if (s.rows.length) { tip = s.rows[s.rows.length - 1].hash; } else if (s.reset) { tip = ""; }
    run = s.run;
    after = s.after;
    if (document.title !== s.title) { document.title = s.title; }
    put("run", s.run_line);
    put("source", s.source);
    put("status", s.status);
    put("chain", s.chain);
    put("pressed", s.pressed);
    put("checked", s.checked);
    put("empty", s.empty);
    put("more", s.more);
    for (i = 0; i < s.panes.length; i += 1) {
      put("pane-" + i + "-value", s.panes[i].value);
      put("pane-" + i + "-note", s.panes[i].note);
    }
    document.body.setAttribute("data-chain", s.chain_ok ? "ok" : "broken");
    document.body.setAttribute("data-state", s.state);
    put("lost", "");
  }
  function lost() {
    put("lost", "This page has lost touch with the watch command, so it is not live and its Stop button " +
        "cannot reach her. Stop her on the command line: " + document.getElementById("stop-cmd").textContent);
  }
  function poll() {
    fetch("/state?run=" + encodeURIComponent(run) + "&after=" + after + "&tip=" + encodeURIComponent(tip),
          {cache: "no-store", credentials: "same-origin"})
      .then(function (response) {
        if (!response.ok) { throw new Error("watch answered " + response.status); }
        return response.json();
      })
      .then(show)
      .catch(lost)
      .then(function () { window.setTimeout(poll, POLL_MS); });
  }
  window.setTimeout(poll, POLL_MS);
}());
</script>
</body>
</html>
""")


def boot_id(token):
    """Names one start of watch, so a page left open across a restart knows to reload. Not the token, and
    the token can't be worked out from it."""
    return hashlib.sha256(b"doris-watch-boot:" + token.encode("ascii")).hexdigest()[:16]


def checked_words(now):
    return "Page updated %s Pacific." % _pacific(now)


def render_page(snap, token, nonce, pressed="", now=None):
    """The whole page for one snapshot: every value escaped, the newest rows first."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    fields = {"title": _e(snap["title"]), "nonce": _e(nonce), "token": _e(token), "run": _e(snap["run"]),
              "run_line": _e(snap["run_line"]), "source": _e(snap["source"]), "status": _e(snap["status"]),
              "chain": _e(snap["chain"]), "chain_state": "ok" if snap["chain_ok"] else "broken",
              "state": _e(snap["state"]), "pressed": _e(pressed), "empty": _e(snap["empty"]),
              "after": int(snap["after"]), "poll": int(POLL_MS), "max_rows": int(MAX_ROWS),
              "boot": _e(boot_id(token)), "checked": _e(checked_words(now)), "more": _e(snap["more"]),
              "stop_cmd": _e(paths.command("stop")), "resume_cmd": _e(paths.command("resume")),
              "tip": _e(snap["rows"][-1]["hash"] if snap["rows"] else ""),
              "rows": "".join(_row_html(r) for r in reversed(snap["rows"][-MAX_ROWS:]))}
    for i, pane in enumerate(snap["panes"]):
        fields.update({"label%d" % i: _e(pane["label"]), "value%d" % i: _e(pane["value"]),
                       "note%d" % i: _e(pane["note"])})
    return _PAGE.substitute(fields)


# ------------------------------------------------------------------------------------------
# The server
# ------------------------------------------------------------------------------------------
def _say(text):
    try:
        print(text, flush=True)
    except (OSError, ValueError):
        pass


class WatchServer(ThreadingHTTPServer):
    """The page's server: 127.0.0.1 only, a fresh token each start, and a cached read of the run's receipts."""

    daemon_threads = True
    allow_reuse_address = os.name != "nt"      # Windows would let a second server share the port

    def __init__(self, port, say=None):
        self.stop_modes = stop_modes()        # at start: a failure to find them shows now, not on a press
        super().__init__((HOST, port), _Handler)
        self.token = secrets.token_urlsafe(32)
        self.boot = boot_id(self.token)
        self.say = say or _say
        self.pressed, self.pressed_ns, self.pressed_failed = "", 0, False
        self._lock = threading.Lock()
        self._cache = None
        self._unread = ""

    @property
    def url(self):
        return "http://%s:%d/" % (HOST, self.server_address[1])

    def view(self):
        """(the newest run's snapshot, the line about a press of Stop). The snapshot is read again only when
        the receipt file has changed. The press line stays until a run with receipts is known to have started
        after the press (its config.json, or the smoke's stamp): that run got past the halt, so `resume` had
        cleared it. A press that failed anywhere is said until watch closes."""
        run = newest_run()
        key = None
        if run is not None:
            try:
                st = os.stat(os.path.join(run.folder, "receipts.jsonl"))
                key = (run.folder, st.st_size, st.st_mtime_ns)
            except OSError:
                key = None
        with self._lock:
            cached = self._cache[1] if key is not None and self._cache is not None and self._cache[0] == key else None
            pressed, pressed_ns, failed = self.pressed, self.pressed_ns, self.pressed_failed
        snap = cached
        if snap is None:
            try:
                snap = snapshot(run)
            except Exception as exc:        # noqa: BLE001 - an odd line must never take the Stop button away
                snap = unreadable(run, exc)
                with self._lock:
                    news, self._unread = snap["status"] != self._unread, snap["status"]
                if news:
                    self.say(snap["status"])
                key = None
            with self._lock:
                self._cache = (key, snap) if key is not None else None
        # a run's known start (config.json, or the smoke's stamp) comes before its halt check; a press that
        # failed anywhere is said until watch closes
        if pressed and not failed and run is not None and run.has_receipts and run.known and run.started > pressed_ns:
            pressed = ""
        return snap, pressed

    def press(self):
        done, failed = stop_now(self.stop_modes)
        now = time.time_ns()
        words = pressed_words(done, failed, _dt.datetime.fromtimestamp(now / 1e9, _dt.timezone.utc))
        with self._lock:
            self.pressed, self.pressed_ns, self.pressed_failed = words, now, bool(failed)
        self.say(words)
        return not failed, words


class _Handler(BaseHTTPRequestHandler):
    def version_string(self):
        return "doris-watch"
    timeout = 10                     # a client that stalls mid-request is dropped

    def log_message(self, format, *args):     # noqa: A002 - the base class names it so
        pass                         # the terminal says only what matters: each press of Stop, and refusals

    # -- replies -----------------------------------------------------------------------------------
    def _reply(self, status, content_type, body, nonce=None, extra=()):
        csp = "default-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
        if nonce is not None:
            csp += "; script-src 'nonce-%s'; style-src 'nonce-%s'; connect-src 'self'" % (nonce, nonce)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", csp)
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        # same-origin, never no-referrer: under no-referrer a browser sends the Stop form's Origin as "null",
        # and the Origin check would refuse the page's own button
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Cache-Control", "no-store")
        for key, value in extra:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _text(self, status, text, extra=()):
        self._reply(status, "text/plain; charset=utf-8", (text + "\n").encode("utf-8"), extra=extra)

    # -- GET: the page and its new lines -------------------------------------------------------------
    def do_GET(self):
        if not host_ok(self.headers.get("Host", "")):
            return self._text(403, "Refused: this page answers only to 127.0.0.1 or localhost.")
        url = urlsplit(self.path)
        if url.path == "/":
            nonce = secrets.token_urlsafe(16)
            snap, pressed = self.server.view()
            page = render_page(snap, self.server.token, nonce, pressed)
            return self._reply(200, "text/html; charset=utf-8", page.encode("utf-8", "replace"), nonce=nonce)
        if url.path == "/state":
            return self._state(parse_qs(url.query))
        if url.path == "/stop":
            return self._text(405, "Stop is a button on the page (a POST), never a link. Nothing was stopped.",
                              extra=(("Allow", "POST"),))
        return self._text(404, "Not here. This page shows her receipts and has one action: Stop.")

    def _state(self, query):
        snap, pressed = self.server.view()
        run = query.get("run", [""])[0]
        after = query.get("after", ["0"])[0]
        after = int(after) if re.fullmatch(r"[0-9]{1,9}", after) else 0
        tip = query.get("tip", [""])[0]
        rows = snap["rows"]
        reset = run != snap["run"] or after > snap["after"] or after == 0 or (     # rewritten under the page
            tip != "" and rows[after - 1]["seq"] == after and rows[after - 1]["hash"] != tip)
        out = dict(snap, reset=reset, pressed=pressed, boot=self.server.boot,
                   checked=checked_words(_dt.datetime.now(_dt.timezone.utc)),
                   rows=snap["rows"][-MAX_ROWS:] if reset else [r for r in snap["rows"] if r["seq"] > after])
        body = json.dumps(out, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        return self._reply(200, "application/json", body)

    # -- POST: Stop, and nothing else ----------------------------------------------------------------
    def do_POST(self):
        if host_ok(self.headers.get("Host", "")) and urlsplit(self.path).path != "/stop":
            return self._text(404, "Not here. This page has one action: Stop. Nothing was started or resumed.")
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY:
            return self._refuse(400, "it is not the page's form")
        body = self.rfile.read(length)
        refusal = stop_refusal(self.headers.get("Host", ""), self.headers.get("Origin"),
                               self.headers.get("Sec-Fetch-Site"), self.headers.get("Content-Type"), body,
                               self.server.token)
        if refusal is not None:
            return self._refuse(*refusal)
        ok, words = self.server.press()
        if ok:
            return self._reply(303, "text/plain; charset=utf-8", (words + "\n").encode("utf-8"),
                               extra=(("Location", "/"),))
        return self._text(500, words)

    def _refuse(self, status, why):
        text = "Refused a Stop request: %s. Nothing was stopped." % why
        self.server.say(text)
        return self._text(status, text + " Use the Stop button on the page, or `%s`." % paths.command("stop"))


# ------------------------------------------------------------------------------------------
# The command
# ------------------------------------------------------------------------------------------
def main(argv) -> int:
    ap = argparse.ArgumentParser(
        prog=paths.command("watch"),
        description="The live page: every step of the newest run, why, and its cost so far, with one Stop "
                    "button. Served on 127.0.0.1 only.")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="the port on 127.0.0.1 (default %d)" % DEFAULT_PORT)
    args = ap.parse_args(argv)
    if not 0 <= args.port <= 65535:
        print("Not started: --port takes a number from 0 to 65535.")
        return 2
    try:
        server = WatchServer(args.port)
    except OSError as exc:
        print("Not started: port %d on %s can't be used (%s). Try another, or `%s` "
              "for any free one." % (args.port, HOST, exc.__class__.__name__, paths.command("watch --port 0")))
        return 2
    _say("Watch page: %s (this computer only). It follows the newest run, sandbox or live, as its receipts "
         "land. Its Stop button halts her as `%s` does. Ctrl+C closes the page; it does not "
         "stop her." % (server.url, paths.command("stop")))
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    _say("Watch page closed. Closing it did not stop her.")
    return 0


if __name__ == "__main__":       # pragma: no cover - the command line runs it as `python -m doris watch`
    sys.exit(main(sys.argv[1:]))
