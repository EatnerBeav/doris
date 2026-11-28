"""python -m doris watch: the live page (live monitoring) and its Stop button (the kill switch).

    python -m doris watch [--port N]

One page, served on 127.0.0.1 only (never 0.0.0.0), so only this computer can open it. On a server, reach
it through an SSH tunnel. It is a live tail of the newest run's receipts, the sandbox's or live, whichever
started last: the brief's four panes at the top (current step, tool calls, decisions, cost so far), then
every receipt line as it lands, newest first, each decision with its why in the receipt's own words. It
reads the receipt file and nothing else, so it cannot show anything the trace lacks, and it never writes
there. Every string reaches the page as text, never as HTML: a display name or a Message-ID is email text.
Model words never reach it (a classified line shows its class, from the closed list), nor any mail body
(no receipt holds one), nor an error's text (only its class name).

One Stop button. It does what `python -m doris stop` does, through the same function: supervisor.request_stop
for each of that command's modes, the sandbox and live, with the cause watch_button. The supervisor then kills
a running worker mid-call and writes the halted line, as for any stop. Only a POST from the page itself stops
her: it carries a token made fresh each time `watch` starts, and its Host and Origin must be this page's. A
request from any other site, or through a name rebound to this computer, is refused and stops nothing. The
page can stop her; it has no way to start, resume or release anything. No external assets: one inline script
and one inline style, each allowed by a fresh nonce per response.
"""

import argparse
import datetime as _dt
import hmac
import html
import json
import os
import re
import secrets
import string
import sys
import threading
import unicodedata
from collections import namedtuple
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import paths, receipts, supervisor
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

Run = namedtuple("Run", "name folder mode")


# ------------------------------------------------------------------------------------------
# Which run: the newest one started, in the sandbox or live
# ------------------------------------------------------------------------------------------
def _started(folder):
    """When a run started: its config.json, written once before the worker starts (else its receipts)."""
    for name in ("config.json", "receipts.jsonl"):
        try:
            return os.stat(os.path.join(folder, name)).st_mtime_ns
        except OSError:
            continue
    return None


def newest_run():
    """The run started last under state/<mode>/runs, for the sandbox and live, or None."""
    best = None
    for mode in WATCHED_MODES:
        runs = paths.runs_dir(mode)
        try:
            names = os.listdir(runs)
        except OSError:
            continue
        for name in names:
            folder = os.path.join(runs, name)
            started = _started(folder)
            if started is not None and (best is None or (started, name) > best[0]):
                best = ((started, name), Run(name, folder, mode))
    return best[1] if best is not None else None


# ------------------------------------------------------------------------------------------
# Words: code tokens as they are, email text as plain text
# ------------------------------------------------------------------------------------------
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
_TS = re.compile(r"^(\d{4}-\d\d-\d\d)T(\d\d:\d\d:\d\dZ)$")
_RUN_NAME = re.compile(r"^model-(live|replayed|stub)-")
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
    return ("%d handled, %d held, %s, %d sent to prospects, %s, %s, %s"
            % (_int(p.get("handled")), _int(p.get("held")), _s(_int(p.get("human_approvals")), "human approval"),
               _int(p.get("sends")), _s(_int(p.get("hold_notices")), "hold notice"),
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
        cost = p.get("cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost > 0:
            text += ", $%.4f" % cost
        return text + "."
    if kind == "halted":
        text = "Cause %s; %s; %s." % (_token(p.get("cause")), _flight_words(p.get("in_flight")),
                                      _dropped_words(p.get("dropped")))
        if isinstance(p.get("stop_to_dead_ms"), int):
            text += " Worker dead %d ms after the stop." % p["stop_to_dead_ms"]
        return text
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


def _time(ts):
    m = _TS.match(ts) if isinstance(ts, str) else None
    return "%s %s" % m.groups() if m else "?"


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
}


def _chain(path):
    """(lines, chain verified or only still being written, the chain in words), read now."""
    try:
        lines = receipts.read_lines(path)
    except (OSError, ValueError):
        lines = []
    result = receipts.verify(path)
    if result.ok:
        notes = result.reason[len("ok; "):] if result.reason.startswith("ok; ") else ""
        return lines, True, "Chain verified: %d lines, each hash matching the line before it%s." % (
            result.lines, " (%s)" % notes if notes else "")
    if result.reason.startswith("torn tail") and result.first_bad_seq == result.lines + 1:
        return lines, True, ("Chain verified through line %d; the line after it is still being written, "
                             "or was cut short." % result.lines)
    if result.first_bad_seq is None:
        return lines, False, "Chain not checked: %s." % result.reason
    return lines, False, ("Chain BROKEN at line %d: %s. Check it with python -m doris verify %s"
                          % (result.first_bad_seq, result.reason, path))


def _unanswered(lines):
    """The newest called line with no result naming it, or None."""
    answered = {_payload(l).get("call_seq") for l in lines if l.get("kind") == "result"}
    for line in reversed(lines):
        if line.get("kind") == "called" and line.get("seq") not in answered:
            return line
    return None


def _call_words(line):
    p = _payload(line)
    return "%s %s" % (_token(p.get("call")), _token(p.get("target")))


def _cost(source, lines):
    total = sum(_payload(l)["cost_usd"] for l in lines if l.get("kind") == "result"
                and isinstance(_payload(l).get("cost_usd"), (int, float))
                and not isinstance(_payload(l).get("cost_usd"), bool))
    if source == "stub":
        return "none (stub)"                       # as the terminal says it
    return "$%.4f%s" % (total, " (recorded)" if source == "replayed" else "")


def snapshot(run):
    """Everything the page shows for this run (None: no run yet), as plain strings: the title, the run's
    words, the four panes, and one row per receipt line, oldest first."""
    if run is None:
        return {"run": "", "title": "Doris watch: no run yet", "run_line": "No run yet.", "source": "",
                "status": "", "chain": "", "chain_ok": True, "state": "none",
                "panes": [{"label": label, "value": "\u2014", "note": ""} for label in LABELS],
                "rows": [], "after": 0,
                "empty": "No run yet. Start one with python -m doris demo, or python -m doris run; this page "
                         "picks it up as soon as it starts."}
    path = os.path.join(run.folder, "receipts.jsonl")
    if os.path.exists(path):
        raw, chain_ok, chain = _chain(path)
    else:
        raw, chain_ok, chain = [], True, "No receipt line yet."
    lines = [l for l in raw if isinstance(l, dict)]
    start = next((l for l in lines if l.get("kind") == "observed" and _payload(l).get("step") == "start"), None)
    named = _RUN_NAME.match(run.name)
    source = _payload(start).get("model_source") if start else (named.group(1) if named else None)
    mode = _payload(start).get("mode") if start else run.mode
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

    if last is None:
        state, status, current = "starting", "Starting: no receipt line yet.", ("Starting.", "")
    elif halted is not None:            # a halted line closes the run (her own is followed by her summary)
        state, status = "halted", "Halted: %s before the stop." % counts
        current = (_text(_why(halted).get("text"), 400) or "Halted.", _halt_note(halted))
    elif last_kind == "summary":
        state, status = "finished", "Finished: %s." % counts
        current = (status, "Line %d." % rows[-1]["seq"])
    elif flight is not None:
        state, status = "waiting", "In progress, no closing line yet: %s so far." % counts
        current = ("Tool call: %s, waiting." % _call_words(flight), "Line %d, since %s."
                   % (_int(flight.get("seq")), _time(flight.get("ts"))))
    else:
        state, status = "running", "In progress, no closing line yet: %s so far." % counts
        r = rows[-1]
        current = (r["what"], "Line %d, %s.%s" % (r["seq"], r["step"], " Why: " + r["why"] if r["why"] else ""))

    closed = last_kind in receipts.CLOSING_KINDS
    if flight is not None and not closed:
        call_note = "1 waiting: %s (line %d)." % (_call_words(flight), _int(flight.get("seq")))
    elif flight is not None:
        call_note = "Cut off by the stop: %s (line %d)." % (_call_words(flight), _int(flight.get("seq")))
    elif calls:
        call_note = "All answered%s." % ("; %d came back failed" % failed if failed else "")
    else:
        call_note = "None yet."
    decision_note = ("Latest why: %s" % _text(_why(decisions[-1]).get("text"), 400) if decisions
                     else "None yet. Each one shows its why below.")
    panes = [
        {"label": LABELS[0], "value": current[0], "note": current[1]},
        {"label": LABELS[1], "value": str(len(calls)), "note": call_note},
        {"label": LABELS[2], "value": str(len(decisions)), "note": decision_note},
        {"label": LABELS[3], "value": _cost(source, lines), "note": _s(model_calls, "model call") + "."},
    ]
    return {"run": run.name, "title": "Doris watch: " + run.name, "run_line": "Run %s" % run.name,
            "source": " ".join(w for w in (MODE_WORDS.get(mode, ""), SOURCE_WORDS.get(source, "")) if w),
            "status": status, "chain": chain, "chain_ok": chain_ok, "state": state, "panes": panes,
            "rows": rows, "after": max([r["seq"] for r in rows] or [0]), "empty": ""}


def _halt_note(line):
    p = _payload(line)
    return "When stopped, %s; %s." % (_flight_words(p.get("in_flight")), _dropped_words(p.get("dropped")))


# ------------------------------------------------------------------------------------------
# Stop: what `python -m doris stop` does
# ------------------------------------------------------------------------------------------
def stop_now():
    """What `python -m doris stop` does, from the page: supervisor.request_stop for each of that command's
    modes, in its order, with the cause watch_button. Every mode is tried even when one fails.
    Returns ([(mode, cause in force)], [(mode, error class name)])."""
    from . import __main__ as cli          # here, not at the top: the command line imports this module
    done, failed = [], []
    for mode in cli.STOP_MODES:
        try:
            done.append((mode, supervisor.request_stop(mode, CAUSE)))
        except Exception as exc:           # noqa: BLE001 - the other mode is still stopped, and this is said
            failed.append((mode, exc.__class__.__name__))
    return done, failed


_WHERE = {"sandbox": "the sandbox", "live": "live"}


def pressed_words(done, failed, now):
    """The page's one line about a press of its Stop button."""
    words = ["Stop pressed at %s UTC." % now.strftime("%H:%M:%S")]
    if done:
        if all(cause == CAUSE for _, cause in done):
            words.append("Halted in %s." % " and ".join(_WHERE.get(m, m) for m, _ in done))
        else:
            words.append("Halted in %s." % " and ".join(
                "%s (%s)" % (_WHERE.get(m, m), supervisor.CAUSE_WORDS.get(c, c)) for m, c in done))
    if failed:
        words.append("NOT halted: %s. Run python -m doris stop now."
                     % ", ".join("%s (%s)" % (m, e) for m, e in failed))
    else:
        words.append("A run in progress is killed mid-call and its receipt names what was dropped. Nothing more "
                     "is sent or booked until python -m doris resume.")
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
    if origin is not None and origin.lower() != "http://" + host.lower():
        return 403, "it came from another site"
    if fetch_site is not None and fetch_site.lower() != "same-origin":
        return 403, "the browser says it came from another site"
    if (content_type or "").split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
        return 415, "it is not the page's form"
    try:
        sent = parse_qs(body.decode("ascii"), keep_blank_values=True).get("token", [])
    except (UnicodeDecodeError, ValueError):
        sent = []
    if len(sent) != 1 or not hmac.compare_digest(sent[0].encode("ascii", "replace"), token.encode("ascii")):
        return 403, "it does not carry this page's token"
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
#pressed:empty, #lost:empty, #empty:empty { display: none; }
main { padding: 0 28px 40px; }
#lost { margin: 16px 0 0; padding: 8px 12px; background: var(--alarm-bg); font-weight: 700; }
.panes {
  display: grid; grid-template-columns: minmax(0, 2.2fr) repeat(3, minmax(0, 1fr));
  margin: 20px 0 8px; border-top: 1px solid var(--rule); border-bottom: 1px solid var(--rule);
}
.pane { padding: 14px 18px 16px; border-left: 1px solid var(--rule); background: var(--pane); }
.pane:first-child { border-left: 4px solid var(--rule); }
body[data-state="waiting"] .pane:first-child { border-left-color: var(--amber-bar); }
body[data-state="halted"] .pane:first-child { border-left-color: var(--signal); }
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
<body data-chain="$chain_state" data-state="$state">
<header>
  <div>
    <h1>Doris watch</h1>
    <p class="run" id="run">$run_line</p>
    <p id="source">$source</p>
    <p id="status">$status</p>
    <p id="chain">$chain</p>
  </div>
  <form class="stop" method="post" action="/stop">
    <input type="hidden" name="token" value="$token">
    <div class="sign"><button type="submit">Stop</button></div>
    <p>Halts Doris now, mid-call, in the sandbox and live, as <code>python -m doris stop</code> does.
      Only <code>python -m doris resume</code>, on the command line, lets her run again.</p>
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
  <div class="scroll"><table>
    <thead><tr><th scope="col">Line</th><th scope="col">Time (UTC)</th><th scope="col">Step</th>
      <th scope="col">Message</th><th scope="col">What happened</th><th scope="col">Why</th></tr></thead>
    <tbody id="steps" data-run="$run" data-after="$after">$rows</tbody>
  </table></div>
</section>
</main>
<script nonce="$nonce">
(function () {
  "use strict";
  var POLL_MS = $poll;
  var steps = document.getElementById("steps");
  var run = steps.getAttribute("data-run") || "";
  var after = parseInt(steps.getAttribute("data-after"), 10) || 0;

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
    if (s.reset) {
      while (steps.firstChild) { steps.removeChild(steps.firstChild); }
    }
    for (i = 0; i < s.rows.length; i += 1) {
      steps.insertBefore(row(s.rows[i]), steps.firstChild);
    }
    run = s.run;
    after = s.after;
    if (document.title !== s.title) { document.title = s.title; }
    put("run", s.run_line);
    put("source", s.source);
    put("status", s.status);
    put("chain", s.chain);
    put("pressed", s.pressed);
    put("empty", s.empty);
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
        "cannot reach her. Stop her on the command line: python -m doris stop");
  }
  function poll() {
    fetch("/state?run=" + encodeURIComponent(run) + "&after=" + after, {cache: "no-store", credentials: "same-origin"})
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


def render_page(snap, token, nonce, pressed=""):
    """The whole page for one snapshot: every value escaped, the rows newest first."""
    fields = {"title": _e(snap["title"]), "nonce": _e(nonce), "token": _e(token), "run": _e(snap["run"]),
              "run_line": _e(snap["run_line"]), "source": _e(snap["source"]), "status": _e(snap["status"]),
              "chain": _e(snap["chain"]), "chain_state": "ok" if snap["chain_ok"] else "broken",
              "state": _e(snap["state"]), "pressed": _e(pressed), "empty": _e(snap["empty"]),
              "after": int(snap["after"]), "poll": int(POLL_MS),
              "rows": "".join(_row_html(r) for r in reversed(snap["rows"]))}
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
        super().__init__((HOST, port), _Handler)
        self.token = secrets.token_urlsafe(32)
        self.say = say or _say
        self.pressed = ""
        self._lock = threading.Lock()
        self._cache = None

    @property
    def url(self):
        return "http://%s:%d/" % (HOST, self.server_address[1])

    def snapshot(self):
        """The newest run's snapshot, read again only when its receipt file has changed."""
        run = newest_run()
        key = None
        if run is not None:
            try:
                st = os.stat(os.path.join(run.folder, "receipts.jsonl"))
                key = (run.folder, st.st_size, st.st_mtime_ns)
            except OSError:
                key = None
        with self._lock:
            if key is not None and self._cache is not None and self._cache[0] == key:
                return self._cache[1]
        snap = snapshot(run)
        with self._lock:
            self._cache = (key, snap) if key is not None else None
        return snap

    def press(self):
        done, failed = stop_now()
        self.pressed = pressed_words(done, failed, _dt.datetime.now(_dt.timezone.utc))
        self.say(self.pressed)
        return not failed


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
        if self.command != "HEAD":
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
            page = render_page(self.server.snapshot(), self.server.token, nonce, self.server.pressed)
            return self._reply(200, "text/html; charset=utf-8", page.encode("utf-8"), nonce=nonce)
        if url.path == "/state":
            return self._state(parse_qs(url.query))
        if url.path == "/stop":
            return self._text(405, "Stop is a button on the page (a POST), never a link. Nothing was stopped.",
                              extra=(("Allow", "POST"),))
        return self._text(404, "Not here. This page shows her receipts and has one action: Stop.")

    def _state(self, query):
        snap = self.server.snapshot()
        run = query.get("run", [""])[0]
        after = query.get("after", ["0"])[0]
        after = int(after) if after.isdigit() and len(after) < 10 else 0
        reset = run != snap["run"] or after > snap["after"]
        out = dict(snap, reset=reset, pressed=self.server.pressed,
                   rows=snap["rows"] if reset else [r for r in snap["rows"] if r["seq"] > after])
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
        if self.server.press():
            return self._reply(303, "text/plain; charset=utf-8", (self.server.pressed + "\n").encode("utf-8"),
                               extra=(("Location", "/"),))
        return self._text(500, self.server.pressed)

    def _refuse(self, status, why):
        text = "Refused a Stop request: %s. Nothing was stopped." % why
        self.server.say(text)
        return self._text(status, text + " Use the Stop button on the page, or `python -m doris stop`.")


# ------------------------------------------------------------------------------------------
# The command
# ------------------------------------------------------------------------------------------
def main(argv) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m doris watch",
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
        print("Not started: port %d on %s can't be used (%s). Try another, or `python -m doris watch --port 0` "
              "for any free one." % (args.port, HOST, exc.__class__.__name__))
        return 2
    _say("Watch page: %s (this computer only). It follows the newest run, sandbox or live, as its receipts "
         "land. Its Stop button halts her as `python -m doris stop` does. Ctrl+C closes the page; it does not "
         "stop her." % server.url)
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
