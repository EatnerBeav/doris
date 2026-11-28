"""python -m doris replay: a finished run, told step by step from its receipts alone (decision 50).

    python -m doris replay [RUN] [--message ID]

RUN is a receipts file, a run folder, a trace folder holding one run folder per item (a Trap Pack
trace), or a run ID found under state/sandbox/runs, state/live/runs or traces/. With none, the latest
sandbox run, as `verify` takes it.

First the chain check `verify` runs (receipts.verify, finished=True), on every file named: if any
fails, one line names the file and the first broken seq, and nothing is stepped through. Then, for
each message in the order she first touched it:

    Saw       the sender, the Message-ID as recorded, the server ID, what the screens settled
    Asked     the model, and what came back          Classed   the class it gave
    Decided   the action, its policy row and the code-written why
    Checked   the gate before an effect: each check and its verdict
    Called    a tool, and what it got back           Verified  a read-back (calendar or Sent)
    Closed    the closing line (Held for a hold), its why

Sensitive mail shows as skipped by design. The run's own lines close it: the start, the inbox reads,
the digest, a halt (its cause, the call in flight, the dropped queue) and the summary.

It reads the receipt files and nothing else. No model, mail, calendar, ledger or loop code is loaded;
a network guard allowing no host is installed for the whole command; nothing is written anywhere.
Every value shown goes through shown(): control and direction characters are escaped, so a sender's
text can never move the cursor or forge a line of the replay.
"""

import argparse
import datetime as _dt
import json
import os
import re
import sys

from . import netguard, paths, receipts, templates

EXIT_OK, EXIT_BROKEN, EXIT_USAGE = 0, 1, 2
SHOWN = 300                                   # characters of one value shown, at most
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
WIDTH = 9                                     # the label column
INDENT = " " * (2 + WIDTH + 1)                # a continuation line
_UTC = _dt.timezone.utc


class _Refused(Exception):
    """One line to print and the exit code: nothing else is printed."""

    def __init__(self, line, code):
        super().__init__(line)
        self.line, self.code = line, code


# --- the command -------------------------------------------------------------------------------
def main(argv) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m doris replay",
        description="Step through a finished run from its receipts alone, after verify's chain check. "
                    "Reads nothing else and writes nothing.")
    ap.add_argument("run", nargs="?", help="a receipts file, a run or trace folder, or a run ID "
                                           "(default: the latest sandbox run)")
    ap.add_argument("--message", metavar="ID", help="only this message: its Message-ID or server ID")
    args = ap.parse_args(argv)
    guard = netguard.install(())              # no host at all: replay never needs the network
    try:
        text, code = replay(args.run, args.message)
    finally:
        guard.uninstall()
    _emit(text)
    return code


def replay(run, message=None):
    """(text, exit code) for `python -m doris replay [run] [--message ID]`."""
    try:
        files, shown_as = find(run)
        for label, path in files:             # every chain first: one broken file refuses the whole
            r = receipts.verify(path, finished=True)
            if not r.ok:
                raise _Refused(_refusal(label, path, r), EXIT_BROKEN)
        parts = []
        if len(files) > 1:
            parts.append("Replay of trace %s: %d receipt files, one per item, each chain verified before "
                         "any is stepped through." % (shown(_display(shown_as)), len(files)))
        found = False
        for label, path in files:
            lines = receipts.read_lines(path)
            text = render(lines, path, label, message)
            if text is not None:
                found = True
                parts.append(text)
        if message is not None and not found:
            raise _Refused("No message %s in %s." % (shown(message), shown(_display(shown_as))), EXIT_USAGE)
        return "\n\n".join(parts), EXIT_OK
    except _Refused as refused:
        return refused.line, refused.code


def _refusal(label, path, r):
    where = shown(_display(path)) if label is None else "item %s (%s)" % (shown(label), shown(_display(path)))
    if r.first_bad_seq is None:
        return "Refused: %s: %s. Nothing is replayed." % (where, shown(r.reason))
    reason = re.sub(r"^broken at seq \d+: ", "", r.reason)
    return ("Refused: the receipt chain of %s is broken at seq %d: %s. Nothing is replayed from a broken "
            "chain (%d lines verified before it)." % (where, r.first_bad_seq, shown(reason), r.lines))


# --- finding the run ---------------------------------------------------------------------------
def find(run):
    """([(item label or None, receipts path)], what to call it). Raises _Refused."""
    if run is None:
        path = latest()
        if path is None:
            raise _Refused("No run to replay yet: run `python -m doris demo` first, or name a receipts file, "
                           "a run folder or a run ID.", EXIT_USAGE)
        return [(None, path)], path
    if os.path.isfile(run):
        return [(None, os.path.abspath(run))], os.path.abspath(run)
    if os.path.isdir(run):
        return in_folder(os.path.abspath(run))
    if not RUN_ID.match(run):
        raise _Refused("No receipts file or folder at %s." % (shown(run),), EXIT_USAGE)
    places = [os.path.join(paths.runs_dir(mode), run) for mode in ("sandbox", "live")]
    places.append(os.path.join(paths.TRACES, run))
    found = [p for p in places if os.path.isdir(p)]
    if not found:
        raise _Refused("No run %s under state/sandbox/runs, state/live/runs or traces/." % (run,), EXIT_USAGE)
    if len(found) > 1:
        raise _Refused("Run %s is in %d places (%s): name the one to replay." % (
            run, len(found), ", ".join(shown(_display(p)) for p in found)), EXIT_USAGE)
    return in_folder(found[0])


def in_folder(folder):
    """A run folder (its receipts.jsonl), or a trace folder of item folders (each one's receipts.jsonl)."""
    own = os.path.join(folder, "receipts.jsonl")
    if os.path.isfile(own):
        return [(None, own)], own
    try:
        names = sorted(os.listdir(folder))
    except OSError as exc:
        raise _Refused("Cannot read the folder %s: %s." % (shown(_display(folder)), type(exc).__name__),
                       EXIT_USAGE)
    items = [(n, os.path.join(folder, n, "receipts.jsonl")) for n in names
             if os.path.isfile(os.path.join(folder, n, "receipts.jsonl"))]
    if not items:
        raise _Refused("No receipts.jsonl in %s or in a folder inside it." % (shown(_display(folder)),),
                       EXIT_USAGE)
    return items, folder


def latest():
    """The newest receipts file under state/sandbox/runs, as `verify` takes it; None when there is none."""
    runs = paths.runs_dir("sandbox")
    try:
        names = os.listdir(runs)
    except OSError:
        return None
    found = []
    for name in names:
        path = os.path.join(runs, name, "receipts.jsonl")
        try:
            found.append((os.path.getmtime(path), path))
        except OSError:
            continue                          # not a run folder, or gone since the listing
    return max(found)[1] if found else None


# --- rendering ---------------------------------------------------------------------------------
def render(lines, path, label=None, message=None):
    """The replay of one receipt file; with message, only that message's block (None if it is not here)."""
    groups, order, run_lines = {}, [], []
    for line in lines:
        subject = line.get("subject")
        if subject and not (line.get("kind") == "error" and subject == "torn-tail"):
            if subject not in groups:
                groups[subject] = []
                order.append(subject)
            groups[subject].append(line)
        else:
            run_lines.append(line)
    results = {}
    for line in lines:
        if line.get("kind") == "result":
            call_seq = _payload(line).get("call_seq")
            if isinstance(call_seq, int) and not isinstance(call_seq, bool):
                results.setdefault(call_seq, line)
    called = {line.get("seq"): line for line in lines if line.get("kind") == "called"}

    picked = order
    if message is not None:
        picked = [s for s in order if _matches(message, s, groups[s])]
        if not picked:
            return None
    start = lines[0] if lines and _payload(lines[0]).get("step") == "start" else None
    number = {subject: n for n, subject in enumerate(order, 1)}
    out = [_heading(lines, path, label, start)]
    for subject in picked:
        out.append("")
        out.append("Message %d of %d: %s" % (number[subject], len(order), _title(subject, groups[subject])))
        out.extend(_steps(groups[subject], results, called))
    if message is None:
        out.append("")
        out.append("The run")
        out.extend(_steps(run_lines, results, called, run_level=True))
    return "\n".join(out)


def _heading(lines, path, label, start):
    run_id = lines[0].get("run_id") if lines else None
    first = ("Replay of run %s (%s)" % (shown(run_id), shown(_display(path))) if label is None else
             "Item %s: run %s (%s)" % (shown(label), shown(run_id), shown(_display(path))))
    chain = "Chain: ok, %d lines, every hash matches the line before it (the check `verify` runs)." % len(lines)
    p = _payload(start) if start is not None else {}
    mode = p.get("mode") or (lines[0].get("mode") if lines else None)
    about = "Mode %s; %s." % (shown(mode), _source_words(p.get("model_source"), p.get("model")))
    if p.get("simulated") is True:
        about += " Simulated: her clock, inbox and calendar are the sandbox's; times are her clock's."
    return "\n".join((first, chain, about))


def _source_words(source, model):
    if source == "live":
        return "model called live (%s)" % (shown(model),)
    if source == "replayed":
        return "model answers replayed from a recorded run (%s); no model called" % (shown(model),)
    if source == "stub":
        return "a built-in keyword stand-in (stub) read the mail; no model called"
    if source == "yes-man":
        return "a hostile yes-man stand-in read the mail (the redteam proof); no model called"
    return "model source %s" % (shown(source),)


def _title(subject, group):
    seen = next((l for l in group if l.get("kind") == "observed"), None)
    if seen is None:
        return "Message-ID %s" % (shown(subject),)
    p = _payload(seen)
    if _sensitive(subject, p):
        return "sensitive mail (its Message-ID kept only as a hash)"
    if not p.get("from_addr"):
        return "an unreadable message"
    return _who(p)


def _matches(wanted, subject, group):
    want = _norm_id(wanted)
    if want and want == _norm_id(subject):
        return True
    return any(l.get("kind") == "observed" and _payload(l).get("server_id") == wanted.strip() for l in group)


def _norm_id(value):
    return value.strip().strip("<>").strip().casefold() if isinstance(value, str) else ""


def _steps(group, results, called, run_level=False):
    """One line per step of this message (or of the run itself), in receipt order."""
    out, history, reads_done = [], [], False
    reads = [l for l in group if l.get("kind") == "called" and _payload(l).get("call") == "mail_fetch"]
    for line in group:
        kind = line.get("kind")
        p = _payload(line)
        if kind == "result" and isinstance(p.get("call_seq"), int) and p.get("call_seq") in called:
            continue                                      # shown beside its call
        if run_level and kind == "called" and p.get("call") == "mail_fetch":
            if not reads_done:
                out.append(_row("Read", _inbox_reads(reads, results), _seqs([l.get("seq") for l in reads])))
                reads_done = True
            continue
        try:
            out.extend(_step(line, results, called, history))
        except Exception:                                 # an odd shape is shown as recorded, never a crash
            out.append(_row(_label_for(kind), "%s line, shown as recorded: %s" % (shown(kind), shown(p)),
                            _seqs([line.get("seq")])))
        if kind == "called":
            history.append(p.get("call"))
    return out


def _label_for(kind):
    return {"observed": "Saw", "classified": "Classed", "decided": "Decided", "checked": "Checked",
            "called": "Called", "result": "Got back", "held": "Held", "halted": "Halted", "error": "Error",
            "summary": "Ended"}.get(kind, "Line")


def _step(line, results, called, history):
    kind, p, seq = line.get("kind"), _payload(line), line.get("seq")
    if kind == "observed":
        if p.get("step") == "start":
            return [_row("Started", _started(p), _seqs([seq]))]
        return [_row("Saw", _saw(line.get("subject"), p), _seqs([seq]))]
    if kind == "classified":
        return [_row("Classed", _classed(p), _seqs([seq]))]
    if kind == "decided" and "outcome" not in p:
        return [_row("Decided", _decided(p, line.get("why")), _seqs([seq]))]
    if kind in ("decided", "held"):
        return [_row("Held" if kind == "held" else "Closed", _closed(p, line.get("why")), _seqs([seq]))]
    if kind == "checked":
        return [_row("Checked", _checked(p, line.get("why")), _seqs([seq]))]
    if kind == "called":
        result = results.get(seq)
        label, text = _call(p, result, history)
        return [_row(label, text, _seqs([seq, result.get("seq") if result is not None else None]))]
    if kind == "result":
        return [_row("Got back", "a result naming no call: %s" % (_outcome(p),), _seqs([seq]))]
    if kind == "error":
        return [_row("Error", _error(line.get("subject"), p), _seqs([seq]))]
    if kind == "halted":
        return _halted(p, line.get("why"), seq, called)
    if kind == "summary":
        return [_row("Ended", _ended(p), _seqs([seq]))]
    return [_row("Line", "%s line, shown as recorded: %s" % (shown(kind), shown(p)), _seqs([seq]))]


def _row(label, text, seqs):
    return "  %s %s%s" % (label.ljust(WIDTH), text, (" [seq %s]" % seqs) if seqs else "")


def _seqs(seqs):
    nums = [s for s in seqs if isinstance(s, int) and not isinstance(s, bool)]
    if len(nums) == 2 and nums[1] == nums[0] + 1:
        return "%d-%d" % tuple(nums)
    return ", ".join(str(n) for n in nums)


# --- the steps, in words -------------------------------------------------------------------------
def _started(p):
    text = "%s." % (when(p.get("clock")),)
    if p.get("interrupted_runs"):
        text += " Runs left unfinished before it: %s." % (shown(p.get("interrupted_runs")),)
    return text


def _who(p):
    name, addr = p.get("from_name"), p.get("from_addr")
    if name:
        return "%s <%s>" % (shown(name), shown(addr))
    return shown(addr)


def _sensitive(subject, p):
    if isinstance(subject, str) and subject.startswith("sensitive:"):
        return True
    return any(isinstance(s, dict) and s.get("settled") and s.get("name") == "sensitive" for s in _screens(p))


def _screens(p):
    screens = p.get("screens")
    return screens if isinstance(screens, list) else []


def _saw(subject, p):
    where = "server ID %s, received %s" % (shown(p.get("server_id")), when(p.get("received")))
    settled = next((s for s in _screens(p) if isinstance(s, dict) and s.get("settled")), None)
    if _sensitive(subject, p):
        check = (settled or {}).get("facts", {}) if isinstance((settled or {}).get("facts"), dict) else {}
        return ("Sensitive mail from %s, %s: skipped by design, filed unread, no model call, no reply "
                "(check: %s)." % (shown(p.get("from_addr")), where, shown(check.get("check"))))
    who = "mail from %s" % (_who(p),) if p.get("from_addr") else "an unreadable message"
    text = "%s, Message-ID %s, %s" % (who, shown(subject), where)
    if settled is not None:
        text += "; screen %s (%s) settled it: %s (%s)" % (
            shown(settled.get("screen")), shown(settled.get("name")), shown(settled.get("outcome")),
            shown(settled.get("why")))
        fault = (settled.get("facts") or {}).get("decode_fault") if isinstance(settled.get("facts"), dict) else None
        if fault:
            text += ", decode fault %s" % (shown(fault),)
    elif _screens(p):
        text += "; no screen stopped it (%d screens ran)" % (len(_screens(p)),)
    if p.get("from_addr"):
        attachments = p.get("attachments")
        count = len(attachments) if isinstance(attachments, list) else attachments
        opened = p.get("opened")
        text += "; %s, %s, %s" % (_count(p.get("links_seen"), "link"), _count(count, "attachment"),
                                  "nothing opened" if opened == 0 else "opened: %s" % shown(opened))
    return text + "."


def _count(n, word):
    if isinstance(n, int) and not isinstance(n, bool):
        return "%d %s%s" % (n, word, "" if n == 1 else "s")
    return "%s %ss" % (shown(n), word)


def _classed(p):
    if p.get("ok") is False:
        return "no usable answer from the model (%s)." % (shown(p.get("source")),)
    bits = []
    for key, word in (("date", "date"), ("time24", "time"), ("zone", "zone"), ("pick", "pick")):
        if p.get(key) is not None:
            bits.append("%s %s" % (word, shown(p.get(key))))
    if p.get("length_min") is not None:
        bits.append("%s minutes" % (shown(p.get("length_min")),))
    if p.get("side_question"):
        bits.append("a side question")
    text = shown(p.get("cls"))
    if bits:
        text += ": " + ", ".join(bits)
    text += "; %s." % ("the model flagged an instruction in it" if p.get("injection_seen")
                       else "no instruction seen in it")
    return text


def _why(why):
    if isinstance(why, dict):
        return "%s: %s" % (shown(why.get("code")), shown(why.get("text")))
    return shown(why)


def _because(why):
    """'Why (code): text', the code-written why of a decided, checked or closing line."""
    if isinstance(why, dict):
        return "Why (%s): %s" % (shown(why.get("code")), shown(why.get("text")))
    return "Why: %s" % (shown(why),)


def _decided(p, why):
    text = "%s. Policy row %s" % (shown(p.get("action")), _why(why))
    slot = p.get("slot")
    if isinstance(slot, dict):
        text += " The slot: %s." % (_span(slot.get("start"), slot.get("end")),)
    return text


def _closed(p, why):
    outcome, template = p.get("outcome"), p.get("template")
    text = shown(outcome)
    if template not in (None, "-"):
        text += ", %s sent" % (shown(template),)
    text += ". " + _because(why)
    notes = []
    if p.get("retried"):
        notes.append("A failed call was retried (the steps above show which).")
    if p.get("recovered"):
        notes.append("Marked recovered: something that went wrong along the way was put right.")
    if p.get("injection_seen"):
        notes.append("The model flagged an instruction in it.")
    if p.get("offers_superseded"):
        notes.append("%s earlier offer(s) to this sender superseded." % (shown(p.get("offers_superseded")),))
    return " ".join([text] + notes)


def _gate_key(name):
    m = re.match(r"^G(\d+)$", name) if isinstance(name, str) else None
    return (0, int(m.group(1)), "") if m else (1, 0, str(name))


def _checked(p, why):
    target = "the gate before %s" % (shown(p.get("action")),)
    if p.get("booking_id"):
        target += " (booking %s)" % (shown(p.get("booking_id")),)
    if p.get("template") not in (None, "-"):
        target += " for %s" % (shown(p.get("template")),)
    checks = p.get("checks") if isinstance(p.get("checks"), dict) else {}
    applied, not_applied = [], []
    for name in sorted(checks, key=_gate_key):
        status = checks[name].get("status") if isinstance(checks[name], dict) else checks[name]
        if status == "n/a":
            not_applied.append(shown(name))
        else:
            applied.append("%s %s" % (shown(name), shown(status)))
    verdict = "passed" if p.get("passed") is True else "BLOCKED at %s" % (shown(p.get("first_fail")),)
    text = "%s: %s: %s" % (target, verdict, ", ".join(applied) or "no check recorded")
    if not_applied:
        text += "; not applicable: %s" % (", ".join(not_applied),)
    return text + ". " + _because(why)


def _call(p, result, history):
    """(label, words) for one called line and what came back."""
    call, target = p.get("call"), p.get("target")
    r = _payload(result) if result is not None else None
    attempt = " (attempt %s)" % (shown(p.get("attempt")),) if p.get("attempt") not in (None, 1) else ""
    if call == "model":
        source = p.get("source")
        who = shown(target) if source in (None, target) else "%s, %s" % (shown(target), shown(source))
        return "Asked", "the model (%s) to class it -> %s" % (who, _answered(r))
    if call == "calendar_query":
        reread = len(history) >= 2 and history[-1] == "calendar_get" and history[-2] == "calendar_create"
        span = _span(p.get("start"), p.get("end"))
        if reread:
            return "Verified", "calendar re-read %s after the write%s -> %s" % (span, attempt, _events(r))
        return "Called", "calendar read %s%s -> %s" % (span, attempt, _events(r))
    if call == "calendar_create":
        return "Called", "calendar write, booking %s%s -> %s" % (shown(target), attempt, _state(r, "written"))
    if call == "calendar_delete":
        return "Called", "calendar delete, booking %s%s -> %s" % (shown(target), attempt, _state(r, "removed"))
    if call == "calendar_get":
        return "Verified", "read back booking %s%s -> %s" % (shown(target), attempt, _read_back(r))
    if call == "send":
        to = {"sender": "the sender", "owner": "the owner"}.get(p.get("to"), shown(p.get("to")))
        bcc = ", BCC the owner" if p.get("bcc") == "owner" else (
            ", BCC %s" % (shown(p.get("bcc")),) if p.get("bcc") else "")
        return "Called", "send %s to %s%s (Message-ID %s) -> %s" % (
            shown(target), to, bcc, shown(p.get("message_id")), _sent(r))
    if call == "sent_lookup":
        again = " again" if p.get("after_wait_s") else ""
        return "Verified", "looked in Sent%s for %s %s -> %s" % (
            again, shown(target), shown(p.get("message_id")), _found(r))
    if call == "mail_file":
        return "Called", "file it under %s -> %s" % (shown(target), _outcome(r))
    if call == "mail_fetch":
        return "Read", "the inbox -> %s" % (_fetched(r),)
    return "Called", "%s %s -> %s" % (shown(call), shown(target), _outcome(r))


def _pending(r):
    return "no answer recorded: in flight when the run stopped" if r is None else None


def _outcome(r):
    if r is None:
        return _pending(r)
    if r.get("ok") is True:
        return "ok"
    return "failed%s" % (": %s" % (shown(r.get("error")),) if r.get("error") else "")


def _answered(r):
    if r is None or r.get("ok") is not True:
        return _outcome(r)
    return "answered, %s tokens in, %s out, cost %s" % (shown(r.get("tokens_in")), shown(r.get("tokens_out")),
                                                         _money(r.get("cost_usd")))


def _money(cost):
    return "$%.4f" % cost if isinstance(cost, (int, float)) and not isinstance(cost, bool) else shown(cost)


def _events(r):
    if r is None or r.get("ok") is not True:
        return _outcome(r)
    events = r.get("events")
    if not isinstance(events, list):
        return "ok, events as recorded: %s" % (shown(events),)
    if not events:
        return "free (no event)"
    hers = [e.get("booking_id") for e in events if isinstance(e, dict) and e.get("booking_id")]
    others = len(events) - len(hers)
    head = "%d event%s" % (len(events), "" if len(events) == 1 else "s")
    if not hers:
        return "%s, none hers" % (head,)
    text = "%s: hers (%s)" % (head, ", ".join(shown(b) for b in hers))
    if others:
        text += " and %d other%s" % (others, "" if others == 1 else "s")
    return text


def _state(r, done):
    if r is None or r.get("ok") is not True:
        return _outcome(r) + ("" if r is None or not r.get("state") else " (%s)" % (shown(r.get("state")),))
    return shown(r.get("state")) if r.get("state") else done


def _read_back(r):
    if r is None or r.get("ok") is not True:
        return _outcome(r)
    if r.get("found") is not True:
        return "not found"
    match = "fields match" if r.get("fields_match") is True else "fields DIFFER (%s)" % (shown(r.get("fields_match")),)
    return "found, %s, %s, %s" % (_span(r.get("start"), r.get("end")), shown(r.get("status")), match)


def _sent(r):
    if r is None:
        return _pending(r)
    state = r.get("state")
    if state == "handed_over" and r.get("ok") is True:
        return "handed over"
    if state == "unknown":
        return "unknown: it may have gone, so it is never sent again"
    if state == "refused":
        return "refused: it did not go"
    return _outcome(r)


def _found(r):
    if r is None or r.get("ok") is not True:
        return _outcome(r)
    return "found" if r.get("found") is True else "not found"


def _fetched(r):
    if r is None or r.get("ok") is not True:
        return _outcome(r)
    return "%s message(s) handed over" % (shown(r.get("count")),)


def _inbox_reads(reads, results):
    ok, failed, pending = 0, [], []
    for line in reads:
        r = results.get(line.get("seq"))
        if r is None:
            pending.append(line.get("seq"))
        elif _payload(r).get("ok") is True:
            ok += 1
        else:
            failed.append("seq %s: %s" % (shown(line.get("seq")), _outcome(_payload(r))))
    text = "the inbox %d time%s: " % (len(reads), "" if len(reads) == 1 else "s")
    if ok == len(reads):
        return text + "every read ok."
    bits = ["%d ok" % ok] if ok else []
    if failed:
        bits.append("%d failed (%s)" % (len(failed), "; ".join(failed)))
    if pending:
        bits.append("no answer recorded for seq %s (in flight when the run stopped)" % (_seqs(pending),))
    return text + ", ".join(bits) + "."


def _error(subject, p):
    if subject == "torn-tail":
        return "a torn tail of %s bytes was moved aside to %s, after seq %s." % (
            shown(p.get("torn_bytes")), shown(p.get("moved_to")), shown(p.get("after_seq")))
    return "in step %s: %s." % (shown(p.get("step")), shown(p.get("error")))


def _halted(p, why, seq, called):
    rows = [_row("Halted", _why_text(why), _seqs([seq]))]
    flight = p.get("in_flight")
    if isinstance(flight, dict):
        text = "In flight: %s %s [seq %s]" % (shown(flight.get("call")), shown(flight.get("target")),
                                               shown(flight.get("seq")))
        subject = (called.get(flight.get("seq")) or {}).get("subject")
        if subject:
            text += ", for Message-ID %s" % (shown(subject),)
        if flight.get("unconfirmed"):
            text += ", marked unconfirmed in the ledger"
        rows.append(INDENT + text + ".")
    elif flight in (None, "none"):
        rows.append(INDENT + "In flight: no call (none was waiting for an answer).")
    else:
        rows.append(INDENT + "In flight: as recorded, %s." % (shown(flight),))
    rows.append(INDENT + "Dropped: " + _dropped(p.get("dropped"), p.get("ledger")))
    if isinstance(p.get("stop_to_dead_ms"), int) and not isinstance(p.get("stop_to_dead_ms"), bool):
        rows.append(INDENT + "Stopped %d ms after the stop was asked for." % (p["stop_to_dead_ms"],))
    if p.get("signal"):
        rows.append(INDENT + "Signal: %s." % (shown(p.get("signal")),))
    if p.get("torn_tail"):
        rows.append(INDENT + "A torn tail was moved aside first: %s." % (shown(p.get("torn_tail")),))
    if p.get("halt_file"):
        rows.append(INDENT + "Halt file: %s." % (shown(p.get("halt_file")),))
    return rows


def _why_text(why):
    return shown(why.get("text")) if isinstance(why, dict) else shown(why)


def _dropped(dropped, ledger):
    if dropped is None:
        return "unknown: the ledger could not be read (ledger %s)." % (shown(ledger),)
    if not isinstance(dropped, list):
        return "as recorded, %s." % (shown(dropped),)
    if not dropped:
        return "nothing (no ledger: nothing was ever queued)." if ledger == "absent" else "nothing was queued."
    actions, messages = [], []
    for d in dropped:
        if not isinstance(d, dict):
            actions.append(shown(d))
        elif d.get("kind") == "message":
            messages.append(shown(d.get("message")))
        else:
            what = d.get("template") or d.get("booking_id")
            actions.append("%s%s for Message-ID %s" % (shown(d.get("kind")), " " + shown(what) if what else "",
                                                       shown(d.get("message"))))
    bits = []
    if actions:
        bits.append("%d queued action(s): %s" % (len(actions), "; ".join(actions)))
    if messages:
        bits.append("%d message(s) not yet finished, left unprocessed in the ledger: %s" % (
            len(messages), ", ".join(messages)))
    return "; ".join(bits) + "."


def _ended(p):
    text = "%s handled, %s held, %s; %s sent to prospects, %s, %s; %s, cost %s; %s quarantined, %s." % (
        shown(p.get("handled")), shown(p.get("held")), _count(p.get("human_approvals"), "human approval"),
        shown(p.get("sends")), _count(p.get("hold_notices"), "hold notice"), _count(p.get("bookings"), "booking"),
        _count(p.get("model_calls"), "model call"), _money(p.get("cost_usd")), shown(p.get("quarantined")),
        _count(p.get("errors"), "error"))
    if p.get("halted"):
        text += " Halted (%s) before the end." % (shown(p.get("halted")),)
    if p.get("read_failed"):
        text += " The inbox could not be read, so the run ended there."
    return text


# --- values ------------------------------------------------------------------------------------
def _payload(line):
    p = line.get("payload") if isinstance(line, dict) else None
    return p if isinstance(p, dict) else {}


def when(value):
    """A receipt's UTC time as Pacific ('Tue Dec 1, 8:02 AM Pacific'); anything else as recorded."""
    if isinstance(value, str):
        try:
            return templates.fmt_pacific(_dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_UTC))
        except Exception:
            pass
    return shown(value)


def _span(start, end):
    return "%s to %s" % (when(start), when(end))


def shown(value, limit=SHOWN):
    """A value from a receipt as safe text: control, format and separator characters escaped
    (\\x1b, \\u202e, \\r), so nothing a sender wrote can move the cursor or start a line; cut at limit."""
    if value is None:
        text = "none"
    elif isinstance(value, str):
        text = value
    elif isinstance(value, bool) or isinstance(value, (int, float)):
        text = json.dumps(value)
    else:
        try:
            text = json.dumps(value, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            text = repr(value)
    out = []
    for ch in text[:limit * 2]:
        if ch == " " or (ch.isprintable() and not ch.isspace()):
            out.append(ch)
        else:
            code = ord(ch)
            out.append("\\x%02x" % code if code < 0x100 else "\\u%04x" % code if code < 0x10000 else "\\U%08x" % code)
    safe = "".join(out)
    return safe if len(safe) <= limit and len(text) <= limit * 2 else safe[:limit] + "..."


def _display(path):
    """Inside the repository, the path from its root; anywhere else, the whole path."""
    try:
        rel = os.path.relpath(path, paths.REPO)
    except ValueError:
        return path
    return path if rel.startswith("..") else rel


def _emit(text):
    """Print in whatever the terminal can show: a character it cannot is written as an escape."""
    stream = sys.stdout
    encoding = getattr(stream, "encoding", None) or "ascii"
    try:
        safe = text.encode(encoding, "backslashreplace").decode(encoding)
    except LookupError:
        safe = text.encode("ascii", "backslashreplace").decode("ascii")
    try:
        stream.write(safe + "\n")
        stream.flush()
    except (OSError, ValueError):
        pass                    # no terminal left to say it to: replay changed nothing either way
