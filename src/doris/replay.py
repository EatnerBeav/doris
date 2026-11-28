"""python -m doris replay: a finished run, told step by step from its receipts alone (decision 50).

    python -m doris replay [RUN] [--message ID]

RUN is a receipts file, a run folder, a trace folder holding one run folder per item (a Trap Pack
trace) or one folder per case holding one run folder per answer (a redteam trace), or a run ID found
under state/sandbox/runs, state/live/runs or traces/. With none, the latest
sandbox run, as `verify` takes it.

First the chain check `verify` runs (receipts.verify, finished=True), on every file named: if any
fails, one line names the file and the first broken seq, and nothing is stepped through. Then, for
each message in the order she first touched it:

    Saw       the sender, the Message-ID as recorded, the server ID, what the screens settled
    Asked     the model (or the stand-in), and what came back      Classed   the class it gave
    Decided   the action, and its policy row or its why
    Checked   the gate before an effect: each check and its verdict
    Called    a tool, and what it got back           Verified  a read-back (calendar or Sent)
    Closed    the closing line (Held for a hold), its why

Sensitive mail shows as skipped by design. The run's own lines close it: the start, the inbox reads,
the digest, a halt (its cause, the call in flight, the dropped queue) and the summary. Each step is
worded only from what its own lines hold; what a line does not record is said to be not recorded.

It reads the receipt files and nothing else: each file's bytes are read once, checked, and stepped
through from those same bytes. The one exception is a manifest.json beside a receipts file (a live
trace's): its receipt_chain and counts are checked against the verified lines and one line says
whether they agree; nothing else in it is read, and it never changes what is replayed. No model, mail adapter, calendar, ledger or loop code is loaded
(templates is used only to print times in Pacific); a network guard allowing no host is installed for
the whole command; nothing is written anywhere. Every value shown goes through shown(): control,
format and separator characters, runs of spaces and backslashes are escaped, so a sender's text can
never move the cursor or build a line that looks like one of the replay's own.
"""

import argparse
import datetime as _dt
import json
import math
import os
import re
import sys

from . import netguard, paths, receipts, templates

EXIT_OK, EXIT_BROKEN, EXIT_USAGE = 0, 1, 2
SHOWN = 200                                   # characters of one value shown, at most
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
        prog="python3 -m doris replay",
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
    if not _emit(text) and code == EXIT_OK:
        return EXIT_BROKEN                    # nothing reached the terminal: not a success
    return code


def replay(run, message=None):
    """(text, exit code) for `python -m doris replay [run] [--message ID]`."""
    try:
        files, shown_as, skipped = find(run)
        checked = [(label, path, _checked_lines(label, path)) for label, path in files]   # every chain first
        parts = []
        if len(files) > 1 or skipped:
            head = ("Replay of trace %s: %d receipt file%s, one per item, each chain verified before any is "
                    "stepped through." % (shown(_display(shown_as)), len(files), "" if len(files) == 1 else "s"))
            for why, words in (("none", "no receipts.jsonl in"), ("unreadable", "could not be read"),
                               ("nested", "inside an item with its own receipts")):
                names = [shown(n) for n, w in skipped if w == why]
                if names:
                    head += " NOT replayed, %s: %s." % (words, ", ".join(names))
            parts.append(head)
        found = False
        for label, path, lines in checked:
            text = render(lines, path, label, message)
            if text is not None:
                found = True
                parts.append(text)
        if message is not None and not found:
            raise _Refused("No message %s in %s." % (shown(message), shown(_display(shown_as))), EXIT_USAGE)
        return "\n\n".join(parts), EXIT_OK
    except _Refused as refused:
        return refused.line, refused.code


def _checked_lines(label, path):
    """The file's lines, read once and checked as `verify` checks them; _Refused (one line) otherwise."""
    where = _where(label, path)
    data = _read(path, where)
    try:
        r = receipts.verify(path, finished=True)
    except Exception as exc:                  # verify itself failed on what the file holds
        line = _not_json(data)
        what = ("line %d holds a number JSON cannot carry (NaN or infinity)" % line if line is not None
                else "the check failed (%s)" % (type(exc).__name__,))
        raise _Refused("Refused: the receipt chain of %s cannot be checked: %s. Nothing is replayed."
                       % (where, what), EXIT_BROKEN)
    if not r.ok:
        if r.first_bad_seq is None:
            raise _Refused("Refused: %s: %s. Nothing is replayed." % (where, shown(r.reason)), EXIT_BROKEN)
        reason = re.sub(r"^broken at seq \d+: ", "", r.reason)
        raise _Refused("Refused: the receipt chain of %s is broken at seq %d: %s. Nothing is replayed from a "
                       "broken chain (%d lines verified before it)." % (where, r.first_bad_seq, shown(reason), r.lines),
                       EXIT_BROKEN)
    if _read(path, where) != data:
        raise _Refused("Refused: %s changed while it was being checked. Nothing is replayed." % (where,), EXIT_BROKEN)
    try:
        return [json.loads(raw.decode("ascii")) for raw in data.split(b"\n") if raw]
    except ValueError:
        raise _Refused("Refused: %s could not be read back after its check. Nothing is replayed." % (where,),
                       EXIT_BROKEN)


def _read(path, where):
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError as exc:
        raise _Refused("Refused: cannot read %s (%s). Nothing is replayed." % (where, type(exc).__name__), EXIT_BROKEN)


def _where(label, path):
    return shown(_display(path)) if label is None else "item %s (%s)" % (shown(label), shown(_display(path)))


def _not_json(data):
    """The number of the first line holding NaN, Infinity or a number too big for a float, or None."""
    def refuse(_value):
        raise ValueError("not a JSON number")

    def finite(text):
        value = float(text)
        if not math.isfinite(value):
            raise ValueError("not a finite number")
        return value

    for n, raw in enumerate(data.split(b"\n"), 1):
        try:
            json.loads(raw.decode("ascii", "replace") or "null", parse_constant=refuse, parse_float=finite)
        except (ValueError, RecursionError) as exc:
            if "number" in str(exc):
                return n
    return None


# --- finding the run ---------------------------------------------------------------------------
def find(run):
    """([(item label or None, receipts path)], what to call it, [item folders with no receipts]). _Refused."""
    if run is None:
        path = latest()
        if path is None:
            raise _Refused("No run to replay yet: run `python -m doris demo` first, or name a receipts file, "
                           "a run folder or a run ID.", EXIT_USAGE)
        return [(None, path)], path, []
    if os.path.isfile(run):
        return [(None, os.path.abspath(run))], os.path.abspath(run), []
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
    """A run folder (its receipts.jsonl), or a trace folder of item folders, each holding its own
    receipts.jsonl (a Trap Pack trace) or one folder per answer that does (a redteam trace, items
    labelled case/answer). Returns (items, folder, skipped): skipped names each folder passed over and
    why ("none": no receipts in it; "unreadable": it could not be read; "nested": receipts inside an
    item that has its own), so none is passed over in silence. A folder reached twice through a link
    is replayed once."""
    own = os.path.join(folder, "receipts.jsonl")
    if os.path.isfile(own):
        return [(None, own)], own, []
    items, skipped, seen = [], [], {os.path.realpath(folder)}
    for name in _folders(folder):
        sub = os.path.join(folder, name)
        if not _first_visit(sub, seen):
            continue
        if os.path.isfile(os.path.join(sub, "receipts.jsonl")):
            items.append((name, os.path.join(sub, "receipts.jsonl")))
            skipped.extend(("%s/%s" % (name, n), "nested") for n in (_folders(sub, quiet=True) or ())
                           if os.path.isfile(os.path.join(sub, n, "receipts.jsonl")))
            continue
        inner = _folders(sub, quiet=True)
        if inner is None:
            skipped.append((name, "unreadable"))
            continue
        answers, empty, unreadable = [], [], []
        for answer in inner:
            at = os.path.join(sub, answer)
            if not _first_visit(at, seen):
                continue
            label = "%s/%s" % (name, answer)
            if os.path.isfile(os.path.join(at, "receipts.jsonl")):
                answers.append((label, os.path.join(at, "receipts.jsonl")))
            elif _folders(at, quiet=True) is None:
                unreadable.append((label, "unreadable"))
            else:
                empty.append((label, "none"))
        items.extend(answers)
        if answers:
            skipped.extend(empty)
        elif not unreadable:
            skipped.append((name, "none"))       # an item folder with no receipts (its outbox is no answer)
        skipped.extend(unreadable)
    if not items:
        raise _Refused("No receipts.jsonl in %s or in a folder inside it." % (shown(_display(folder)),),
                       EXIT_USAGE)
    return items, folder, skipped


def _first_visit(path, seen):
    real = os.path.realpath(path)
    if real in seen:
        return False
    seen.add(real)
    return True


def _folders(folder, quiet=False):
    """The folder's subfolders, sorted; when it cannot be read, _Refused (or None if quiet)."""
    try:
        return sorted(n for n in os.listdir(folder) if os.path.isdir(os.path.join(folder, n)))
    except OSError as exc:
        if quiet:
            return None
        raise _Refused("Cannot read the folder %s: %s." % (shown(_display(folder)), type(exc).__name__),
                       EXIT_USAGE)


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


# --- one file ----------------------------------------------------------------------------------
class _Trace:
    """What one file's lines say about each other: calls and their results, the source of the
    model's answers, and the calls a halted line names as in flight."""

    def __init__(self, lines):
        self.lines = lines
        self.called = {l.get("seq"): l for l in lines if l.get("kind") == "called"}
        self.results = {}                     # call seq -> every result line naming it, in order
        for line in lines:
            call_seq = _payload(line).get("call_seq") if line.get("kind") == "result" else None
            if _is_int(call_seq) and call_seq in self.called:
                self.results.setdefault(call_seq, []).append(line)
        self.in_flight = set()
        for line in lines:
            flight = _payload(line).get("in_flight") if line.get("kind") == "halted" else None
            if isinstance(flight, dict) and _is_int(flight.get("seq")):
                self.in_flight.add(flight["seq"])
        start = lines[0] if lines and _payload(lines[0]).get("step") == "start" else None
        self.start = _payload(start) if start is not None else {}

    def result(self, seq):
        found = self.results.get(seq)
        return _payload(found[0]) if found else None

    def unanswered_before(self, seq):
        return [s for s in sorted(k for k in self.called if _is_int(k) and k < seq) if s not in self.results]


def render(lines, path, label=None, message=None):
    """The replay of one receipt file; with message, only that message's block (None if it is not here)."""
    t = _Trace(lines)
    blocks, latest_block, run_lines = [], {}, []
    for line in lines:
        subject = line.get("subject")
        if not subject or (line.get("kind") == "error" and subject == "torn-tail"):
            run_lines.append(line)
            continue
        at = latest_block.get(subject)
        if at is None or (line.get("kind") == "observed" and
                          any(l.get("kind") == "observed" for l in blocks[at][1])):
            blocks.append((subject, []))      # a Message-ID seen again (a duplicate) gets a block of its own
            at = latest_block[subject] = len(blocks) - 1
        blocks[at][1].append(line)

    picked = list(range(len(blocks)))
    if message is not None:
        picked = [n for n in picked if _matches(message, *blocks[n])]
        if not picked:
            return None
    out = [_heading(lines, path, label, t.start)]
    for n in picked:
        subject, group = blocks[n]
        out.append("")
        out.append("Message %d of %d: %s" % (n + 1, len(blocks), _title(subject, group)))
        out.extend(_steps(group, t))
    shown_run = run_lines if message is None else [l for l in run_lines if l.get("kind") == "halted"]
    if shown_run:
        out.append("")
        out.append("The run")
        out.extend(_steps(shown_run, t, run_level=True))
    return "\n".join(out)


def _heading(lines, path, label, start):
    run_id = lines[0].get("run_id") if lines else None
    first = ("Replay of run %s (%s)" % (shown(run_id), shown(_display(path))) if label is None else
             "Item %s: run %s (%s)" % (shown(label), shown(run_id), shown(_display(path))))
    chain = "Chain: ok, %d lines, every hash matches the line before it (the check `verify` runs)." % len(lines)
    mode = start.get("mode") or (lines[0].get("mode") if lines else None)
    about = "Mode %s; %s." % (shown(mode), _source_words(start.get("model_source"), start.get("model")))
    if start.get("simulated") is True:
        about += " Simulated: her clock, inbox and calendar are the sandbox's; times are her clock's."
    manifest = _manifest(path, lines)
    return "\n".join((first, chain) + ((manifest,) if manifest else ()) + (about,))


MANIFEST_MAX = 1 << 20                        # bytes of a manifest.json read, at most


def _manifest(path, lines):
    """One line on the manifest.json beside a receipts file (a live trace's), or None when there is none:
    whether its receipt_chain and counts agree with the lines just verified. Nothing else in it is read,
    and it never changes what is replayed."""
    where = os.path.join(os.path.dirname(path), "manifest.json")
    if os.path.basename(path) != "receipts.jsonl" or not os.path.isfile(where):
        return None                           # a manifest describes the receipts.jsonl beside it, no other file
    try:
        with open(where, "rb") as fh:
            data = fh.read(MANIFEST_MAX + 1)
        if len(data) > MANIFEST_MAX:
            raise ValueError("over %d bytes" % MANIFEST_MAX)
        m = json.loads(data.decode("utf-8"))
        if not isinstance(m, dict):
            raise ValueError("not a JSON object")
    except Exception as exc:
        return ("Manifest: the manifest.json beside it could not be read (%s); nothing in it was checked."
                % (type(exc).__name__,))
    chain, counts = m.get("receipt_chain"), m.get("counts")
    if not isinstance(chain, dict) and not isinstance(counts, dict):
        return "Manifest: holds no receipt chain or counts to check."
    agree, differ, unchecked = [], [], []
    if isinstance(chain, dict):
        if chain.get("lines") != len(lines) or _is_int(chain.get("lines")) is False:
            differ.append("its receipt chain says %s lines, the file holds %d" % (shown(chain.get("lines")), len(lines)))
        if chain.get("ok") is not True:
            differ.append("its receipt chain says ok %s, verify found it ok" % (shown(chain.get("ok")),))
        if not differ:
            agree.append("its receipt chain: %d lines, ok" % (len(lines),))
    if isinstance(counts, dict):
        mine, same, before = _counted(lines), [], len(differ)
        for key in sorted(counts, key=str):
            if key not in mine:
                unchecked.append(shown(key))
            elif json.dumps(counts[key]) == json.dumps(mine[key]):
                same.append("%s %d" % (shown(key), mine[key]))
            else:
                differ.append("it counts %s %s, the lines hold %d" % (shown(key), shown(counts[key]), mine[key]))
        if len(differ) == before and same:
            agree.append("its counts, recounted from the lines: %s" % (", ".join(same),))
    tail = " (not checked: %s)" % (", ".join(unchecked),) if unchecked else ""
    if not differ and not agree:
        return "Manifest: holds no receipt chain or counts to check%s." % (tail,)
    if differ:
        return "Manifest DISAGREES with the receipts: %s%s. Only the receipts are replayed." % ("; ".join(differ), tail)
    return "Manifest: agrees with the receipts (%s)%s." % ("; ".join(agree), tail)


def _counted(lines):
    """The counts a live trace's manifest holds, counted from the lines as its writer counts them
    (livetrace.counts and loop.count_outcomes), without loading either."""
    calls = [_payload(l).get("call") for l in lines if l.get("kind") == "called"]
    out = {"messages": sum(1 for l in lines if l.get("kind") == "observed" and _payload(l).get("step") != "start"),
           "model_calls": calls.count("model"), "sends": calls.count("send"),
           "errors": sum(1 for l in lines if l.get("kind") == "error"), "handled": 0, "held": 0, "human_approvals": 0}
    for line in lines:
        p = _payload(line)
        if line.get("kind") == "approved" or p.get("human_approval") is True:
            out["human_approvals"] += 1
        if line.get("kind") in ("decided", "held") and "outcome" in p:
            out["held" if str(p["outcome"]).startswith("hold:") else "handled"] += 1
    return out


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


def _reader(source, target=None):
    """Who answered a classify call, by the source its own line records: only a live call is "the model"."""
    if source == "live":
        return "the model (%s, called live)" % (shown(target),)
    if source == "replayed":
        return "the model's recorded answer (%s, replayed)" % (shown(target),)
    if source == "stub":
        return "the stand-in (stub)"
    if source == "yes-man":
        return "the yes-man stand-in"
    return "the reader (source %s)" % (shown(source),)


def _flagger(source):
    return _pick({"live": "the model", "replayed": "the model's recorded answer", "stub": "the stand-in",
                  "yes-man": "the yes-man stand-in"}, source, "the reader")


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


def _steps(group, t, run_level=False):
    """One row per step of this message (or of the run itself), in receipt order."""
    out, state, reads_done = [], {"reread_due": False}, False
    reads = [l for l in group if l.get("kind") == "called" and _payload(l).get("call") == "mail_fetch"]
    for line in group:
        kind, p = line.get("kind"), _payload(line)
        if kind == "result" and _is_int(p.get("call_seq")) and p.get("call_seq") in t.results:
            first = t.results[p["call_seq"]][0]
            if first is line:
                continue                                  # shown beside its call
        if run_level and kind == "called" and p.get("call") == "mail_fetch":
            if not reads_done:
                out.append(_row("Read", _inbox_reads(reads, t), _seqs([l.get("seq") for l in reads])))
                reads_done = True
            continue
        try:
            out.extend(_step(line, t, state))
        except Exception:                                 # an odd shape is shown as recorded, never a crash
            out.append(_row(_label_for(kind), "%s line, shown as recorded: %s" % (shown(kind), shown(p)),
                            _seqs([line.get("seq")])))
            first = t.results.get(line.get("seq"), [None])[0] if kind == "called" and _is_int(line.get("seq")) \
                else None
            if first is not None:                         # its result was skipped above as shown beside it
                out.append(_row("Got back", "its result, shown as recorded: %s" % (shown(_payload(first)),),
                                _seqs([first.get("seq")])))
    return out


def _label_for(kind):
    return {"observed": "Saw", "classified": "Classed", "decided": "Decided", "checked": "Checked",
            "called": "Called", "result": "Got back", "held": "Held", "halted": "Halted", "error": "Error",
            "summary": "Ended"}.get(kind, "Line")


def _step(line, t, state):
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
        r = t.result(seq)
        label, what = _call_words(p, state)
        if p.get("call") == "calendar_get" and isinstance(r, dict) and "fields_match" in r:
            state["reread_due"] = True                    # the next calendar read is the re-read after the write
        if p.get("call") == "calendar_query" and label == "Verified" and (r is None or r.get("ok") is not True):
            state["reread_due"] = True                    # its retry is the re-read still
        first = t.results.get(seq, [None])[0]
        return [_row(label, "%s -> %s" % (what, _result_words(p, r, seq in t.in_flight)),
                     _seqs([seq, first.get("seq") if first is not None else None]))]
    if kind == "result":
        call_seq = p.get("call_seq")
        if _is_int(call_seq) and call_seq in t.results:   # a second result for one call
            call = _payload(t.called[call_seq])
            return [_row("Got back", "another result for the call at seq %d (%s %s) -> %s" % (
                call_seq, shown(call.get("call")), shown(call.get("target")), _result_words(call, p, False)),
                _seqs([seq]))]
        return [_row("Got back", "a result naming no call: %s" % (_outcome(p, False),), _seqs([seq]))]
    if kind == "error":
        return [_row("Error", _error(line.get("subject"), p), _seqs([seq]))]
    if kind == "halted":
        return _halted(p, line.get("why"), seq, t)
    if kind == "summary":
        return [_row("Ended", _ended(p, t.start.get("model_source")), _seqs([seq]))]
    return [_row("Line", "%s line, shown as recorded: %s" % (shown(kind), shown(p)), _seqs([seq]))]


def _row(label, text, seqs):
    return "  %s %s%s" % (label.ljust(WIDTH), text, (" [seq %s]" % seqs) if seqs else "")


def _seqs(seqs):
    nums = [s for s in seqs if _is_int(s)]
    if len(nums) == 2 and nums[1] == nums[0] + 1:
        return "%d-%d" % tuple(nums)
    return ", ".join(str(n) for n in nums)


# --- the steps, in words -------------------------------------------------------------------------
def _started(p):
    text = "%s." % (when(p.get("clock")),)
    if "fresh_start" in p:                    # a live run: a new ledger, or the one already there
        fresh = p.get("fresh_start")
        text += (" This run started a new live ledger (--fresh)." if fresh is True else
                 " It went on from the live ledger already there." if fresh is False else
                 " Fresh start: %s." % (shown(fresh),))
    if "mail_since" in p:
        since = p.get("mail_since")
        text += (" Mail read from: not recorded." if since is None else
                 " Mail received before %s is never read (the ledger's fresh start)." % (when(since),))
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
    facts = settled.get("facts") if isinstance(settled, dict) and isinstance(settled.get("facts"), dict) else {}
    if _sensitive(subject, p):
        return "Sensitive mail from %s, %s: skipped by design (check: %s)." % (
            shown(p.get("from_addr")), where, shown(facts.get("check")))
    who = "mail from %s" % (_who(p),) if p.get("from_addr") else "an unreadable message"
    text = "%s, Message-ID %s, %s" % (who, shown(subject), where)
    if settled is not None:
        text += "; screen %s (%s) settled it: %s (%s)" % (
            shown(settled.get("screen")), shown(settled.get("name")), shown(settled.get("outcome")),
            shown(settled.get("why")))
        if facts.get("decode_fault"):
            text += ", decode fault %s" % (shown(facts.get("decode_fault")),)
    elif _screens(p):
        text += "; no screen stopped it (%d screens ran)" % (len(_screens(p)),)
    if p.get("from_addr"):
        attachments = p.get("attachments")
        count = len(attachments) if isinstance(attachments, list) else attachments
        opened = p.get("opened")
        text += "; %s, %s, %s" % (_count(p.get("links_seen"), "link"), _count(count, "attachment"),
                                  "nothing opened" if opened == 0 and not isinstance(opened, bool)
                                  else "opened: %s" % shown(opened))
    return text + "."


def _count(n, word):
    return _tally(n, word, word + "s")


def _tally(n, one, many):
    """'1 booking', '2 bookings'; 'bookings: not recorded' when the line holds no count."""
    if _is_int(n):
        return "%d %s" % (n, one if n == 1 else many)
    return "%s: %s" % (many, shown(n))


def _classed(p):
    if p.get("ok") is False:
        return "no usable answer from %s." % (_flagger(p.get("source")),)
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
    seen = p.get("injection_seen")
    text += "; %s." % ("%s flagged an instruction in it" % _flagger(p.get("source")) if seen is True else
                       "no instruction seen in it" if seen is False else
                       "not recorded whether an instruction was seen")
    return text


def _because(why):
    """'Why (code): text', the code-written why of a decided, checked or closing line."""
    if isinstance(why, dict):
        return "Why (%s): %s" % (shown(why.get("code")), shown(why.get("text")))
    return "Why: %s" % (shown(why),)


def _decided(p, why):
    """The plan line. Only a policy.* code is a row of the policy table; a hold code is the hold's why."""
    code = why.get("code") if isinstance(why, dict) else None
    if isinstance(code, str) and code.startswith("policy."):
        text = "%s. Policy row %s: %s" % (shown(p.get("action")), shown(code), shown(why.get("text")))
    else:
        text = "%s. %s" % (shown(p.get("action")), _because(why))
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
        notes.append("An instruction was flagged in it.")
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
    if p.get("passed") is True:
        verdict = "passed"
    elif p.get("passed") is False:
        verdict = ("BLOCKED at %s" % (shown(p.get("first_fail")),) if p.get("first_fail") else
                   "BLOCKED (failing check not recorded)")
    else:
        verdict = "verdict not recorded"
    text = "%s: %s: %s" % (target, verdict, ", ".join(applied) or "no check recorded")
    if not_applied:
        text += "; not applicable: %s" % (", ".join(not_applied),)
    return text + ". " + _because(why)


def _call_words(p, state):
    """(label, what was called) for one called line. A calendar read right after a read-back that
    compared the booking's fields (the one after a write) is the re-read after the write."""
    call, target = p.get("call"), p.get("target")
    attempt = " (attempt %s)" % (shown(p.get("attempt")),) if p.get("attempt") not in (None, 1) else ""
    if call == "model":
        return "Asked", "%s to class it" % (_reader(p.get("source"), target),)
    if call == "calendar_query":
        span = _span(p.get("start"), p.get("end"))
        if state.get("reread_due"):
            state["reread_due"] = False
            after = "the write" if state.get("wrote") else "the read-back"
            return "Verified", "calendar re-read %s after %s%s" % (span, after, attempt)
        return "Called", "calendar read %s%s" % (span, attempt)
    if call == "calendar_create":
        state["reread_due"], state["wrote"] = False, True
        return "Called", "calendar write, booking %s%s" % (shown(target), attempt)
    if call == "calendar_delete":
        state["reread_due"] = False
        return "Called", "calendar delete, booking %s%s" % (shown(target), attempt)
    if call == "calendar_get":
        return "Verified", "read back booking %s%s" % (shown(target), attempt)
    if call == "send":
        to = _pick({"sender": "the sender", "owner": "the owner"}, p.get("to"), shown(p.get("to")))
        bcc = ", BCC the owner" if p.get("bcc") == "owner" else (
            ", BCC %s" % (shown(p.get("bcc")),) if p.get("bcc") else "")
        return "Called", "send %s to %s%s (Message-ID %s)" % (shown(target), to, bcc, shown(p.get("message_id")))
    if call == "sent_lookup":
        again = " again" if p.get("after_wait_s") else ""
        return "Verified", "looked in Sent%s for %s %s" % (again, shown(target), shown(p.get("message_id")))
    if call == "mail_file":
        return "Called", "file it under %s" % (shown(target),)
    if call == "mail_fetch":
        return "Read", "the inbox"
    return "Called", "%s %s" % (shown(call), shown(target))


def _result_words(p, r, in_flight, state=None):
    """What came back for this call, from its result line alone."""
    call = p.get("call")
    if r is None or r.get("ok") is not True:
        if call == "send" and r is not None and r.get("state") in ("unknown", "refused"):
            return _sent(r)
        text = _outcome(r, in_flight)
        if call in ("calendar_create", "calendar_delete") and r is not None and r.get("state"):
            text += " (%s)" % (shown(r.get("state")),)
        return text
    if call == "model":
        return "answered, %s tokens in, %s out, cost %s" % (shown(r.get("tokens_in")), shown(r.get("tokens_out")),
                                                             _money(r.get("cost_usd")))
    if call == "calendar_query":
        return _events(r)
    if call == "calendar_create":
        return shown(r.get("state")) if r.get("state") else "ok"
    if call == "calendar_delete":
        return shown(r.get("state")) if r.get("state") else "removed"
    if call == "calendar_get":
        return _read_back(r)
    if call == "send":
        return _sent(r)
    if call == "sent_lookup":
        return _found(r)
    if call == "mail_fetch":
        return "%s message(s) handed over" % (shown(r.get("count")),)
    return "ok"


def _outcome(r, in_flight):
    if r is None:
        return "no result recorded" + (" (in flight when the run halted)" if in_flight else "")
    if r.get("ok") is True:
        return "ok"
    if r.get("ok") is False:
        return "failed%s" % (": %s" % (shown(r.get("error")),) if r.get("error") else "")
    return "outcome not recorded" + (" (ok: %s)" % (shown(r.get("ok")),) if "ok" in r else "")


def _found(r):
    found = r.get("found")
    return "found" if found is True else "not found" if found is False else "not recorded whether found"


def _money(cost):
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        try:
            return "$%.4f" % cost
        except OverflowError:
            pass
    return shown(cost)


def _events(r):
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


def _read_back(r):
    if r.get("found") is not True:
        return _found(r)
    if "fields_match" not in r:
        match = "not compared"
    elif r.get("fields_match") is True:
        match = "fields match"
    else:
        match = "fields DIFFER (%s)" % (shown(r.get("fields_match")),)
    return "found, %s, %s, %s" % (_span(r.get("start"), r.get("end")), shown(r.get("status")), match)


def _sent(r):
    state = r.get("state")
    if state == "handed_over" and r.get("ok") is True:
        return "handed over"
    if state == "unknown":
        return "result unknown (it may have gone)"
    if state == "refused":
        return "refused: it did not go"
    return _outcome(r, False)


def _inbox_reads(reads, t):
    ok, failed, pending, unsaid = 0, [], [], []
    for line in reads:
        r = t.result(line.get("seq"))
        if r is None:
            pending.append(line.get("seq"))
        elif r.get("ok") is True:
            ok += 1
        elif r.get("ok") is False:
            failed.append("seq %s: %s" % (shown(line.get("seq")), _outcome(r, False)))
        else:
            unsaid.append(line.get("seq"))
    text = "the inbox %d time%s: " % (len(reads), "" if len(reads) == 1 else "s")
    if ok == len(reads):
        return text + "every read ok."
    bits = ["%d ok" % ok] if ok else []
    if failed:
        bits.append("%d failed (%s)" % (len(failed), "; ".join(failed)))
    for seq in unsaid:
        bits.append("outcome not recorded for seq %s" % (shown(seq),))
    for seq in pending:
        bits.append("no result recorded for seq %s%s" % (
            shown(seq), " (in flight when the run halted)" if seq in t.in_flight else ""))
    return text + ", ".join(bits) + "."


def _error(subject, p):
    if subject == "torn-tail":
        return "a torn tail of %s bytes was moved aside to %s, after seq %s." % (
            shown(p.get("torn_bytes")), shown(p.get("moved_to")), shown(p.get("after_seq")))
    return "in step %s: %s." % (shown(p.get("step")), shown(p.get("error")))


def _halted(p, why, seq, t):
    rows = [_row("Halted", shown(why.get("text")) if isinstance(why, dict) else shown(why), _seqs([seq]))]
    if "in_flight" not in p:
        rows.append(INDENT + "In flight: not recorded.")
    else:
        flight = p.get("in_flight")
        if isinstance(flight, dict):
            rows.append(INDENT + _flight(flight, p.get("ledger"), t) + ".")
        elif flight in (None, "none"):
            open_calls = t.unanswered_before(seq) if _is_int(seq) else []
            rows.append(INDENT + ("In flight: none recorded, though %s no result line." % (
                "seq %s has" % open_calls[0] if len(open_calls) == 1 else
                "seqs %s have" % ", ".join(str(s) for s in open_calls)) if open_calls else
                "In flight: no call (every call before the halt has its result)."))
        else:
            rows.append(INDENT + "In flight: as recorded, %s." % (shown(flight),))
    rows.append(INDENT + "Dropped: " + ("not recorded." if "dropped" not in p else
                                        _dropped(p.get("dropped"), p.get("ledger"), "ledger" in p)))
    if _is_int(p.get("stop_to_dead_ms")):
        rows.append(INDENT + "Stopped %d ms after the stop was asked for." % (p["stop_to_dead_ms"],))
    if p.get("requested_utc"):
        rows.append(INDENT + "Stop asked for at %s." % (when(p.get("requested_utc")),))
    if p.get("signal"):
        rows.append(INDENT + "Signal: %s." % (shown(p.get("signal")),))
    if p.get("worker_exit") is not None:
        rows.append(INDENT + "Worker exit code %s." % (shown(p.get("worker_exit")),))
    if p.get("torn_tail"):
        rows.append(INDENT + "A torn tail was moved aside first: %s." % (shown(p.get("torn_tail")),))
    if p.get("halt_file"):
        rows.append(INDENT + "Halt file: %s." % (shown(p.get("halt_file")),))
    return rows


def _flight(flight, ledger, t):
    """The call a halted line names as in flight, and whether the ledger took its mark (record_halt marks
    it only when the ledger opened, by Message-ID for a send and by booking otherwise)."""
    call, seq = flight.get("call"), flight.get("seq")
    text = "In flight: %s" % (shown(call) if call is not None else "call not recorded",)
    if flight.get("target") is not None:
        text += " %s" % (shown(flight.get("target")),)
    text += " [seq %d]" % seq if _is_int(seq) else " (seq not recorded)"
    subject = (_pick(t.called, seq, None) or {}).get("subject")
    if subject:
        text += ", for Message-ID %s" % (shown(subject),)
    if flight.get("unconfirmed") is True:
        key = flight.get("message_id") if call == "send" else flight.get("target")
        if ledger == "ok" and key:
            text += ", marked unconfirmed in the ledger"
        elif ledger == "ok":
            text += ", not marked in the ledger (no %s recorded to mark it by)" % (
                "Message-ID" if call == "send" else "booking")
        else:
            text += ", not marked in the ledger (%s)" % (_pick(
                {"unreadable": "it could not be read", "absent": "there was none"}, ledger,
                "ledger %s" % (shown(ledger),)),)
    return text


def _dropped(dropped, ledger, hashed=False):
    if dropped is None:
        return ("unknown: the ledger could not be read." if ledger == "unreadable" else
                "unknown (none recorded; ledger %s)." % (shown(ledger),))
    if not isinstance(dropped, list):
        return "as recorded, %s." % (shown(dropped),)
    if not dropped:
        return "nothing (no ledger: nothing was ever queued)." if ledger == "absent" else "nothing was queued."
    actions, messages = [], []
    for d in dropped:
        if not isinstance(d, dict):
            actions.append(shown(d))
        elif d.get("kind") == "message":
            messages.append(_key(d.get("message"), hashed, bare=True))
        else:
            what = d.get("template") or d.get("booking_id")
            kind = shown(d.get("kind")) if d.get("kind") is not None else "an action of no recorded kind"
            actions.append("%s%s for %s" % (kind, " " + shown(what) if what else "", _key(d.get("message"), hashed)))
    bits = []
    if actions:
        bits.append("%d queued action(s): %s" % (len(actions), "; ".join(actions)))
    if messages:
        bits.append("%d message(s) not yet finished, left unprocessed in the ledger: %s" % (
            len(messages), ", ".join(messages)))
    return "; ".join(bits) + "."


def _key(key, hashed, bare=False):
    """A dropped item's ledger key. Only the supervisor's halted line (hashed: it records the ledger's
    state) shows a key by its hash, "key-sha256:<hash>" (supervisor.shown_key); the loop's own line
    writes every key as it is. "sha256:<hash>" is the key of a mail with no Message-ID, or a Message-ID
    a sender wrote that way, so it is called a key, never either one."""
    if hashed and isinstance(key, str) and re.fullmatch(r"key-sha256:[0-9a-f]{64}", key):
        return "key (hashed) %s" % (key[len("key-sha256:"):],)
    if isinstance(key, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", key):
        return "key %s" % (key,)
    return shown(key) if bare else "Message-ID %s" % (shown(key),)


def _ended(p, source):
    reads = _pick({"live": ("model call", "model calls"),
                   "replayed": ("replayed model answer", "replayed model answers"),
                   "stub": ("read by the stand-in (stub)", "reads by the stand-in (stub)"),
                   "yes-man": ("read by the yes-man stand-in", "reads by the yes-man stand-in")},
                  source, ("classify call", "classify calls"))
    text = "%s, %s, %s; %s, %s, %s; %s, cost %s; %s, %s." % (
        _tally(p.get("handled"), "handled", "handled"), _tally(p.get("held"), "held", "held"),
        _count(p.get("human_approvals"), "human approval"),
        _tally(p.get("sends"), "sent to prospects", "sent to prospects"),
        _count(p.get("hold_notices"), "hold notice"), _count(p.get("bookings"), "booking"),
        _tally(p.get("model_calls"), *reads), _money(p.get("cost_usd")),
        _tally(p.get("quarantined"), "quarantined", "quarantined"), _count(p.get("errors"), "error"))
    if p.get("halted"):
        text += " Halted (%s) before the end." % (shown(p.get("halted")),)
    if p.get("read_failed"):
        text += " The inbox could not be read, so the run ended there."
    return text


# --- values ------------------------------------------------------------------------------------
def _payload(line):
    p = line.get("payload") if isinstance(line, dict) else None
    return p if isinstance(p, dict) else {}


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _pick(mapping, key, default):
    """mapping[key], or default when it is not there or the key cannot be one (a list a forged line holds)."""
    try:
        return mapping.get(key, default)
    except TypeError:
        return default


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
    """A value from a receipt as safe text, cut at limit. Control, format and separator characters are
    escaped (\\x1b, \\u202e, \\r), and so are a backslash (\\\\) and every space after the first in a run
    (\\x20): nothing a sender wrote can move the cursor, start a line or pad itself into a fake row."""
    if value is None:
        text = "not recorded"
    elif isinstance(value, str):
        text = value
    elif isinstance(value, (bool, int, float)):
        text = json.dumps(value)
    else:
        try:
            text = json.dumps(value, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            text = repr(value)
    out, prev = [], ""
    for ch in text[:limit * 2]:
        if ch == "\\":
            out.append("\\\\")
        elif ch == " ":
            out.append("\\x20" if prev == " " else " ")
        elif ch.isprintable() and not ch.isspace():
            out.append(ch)
        else:
            code = ord(ch)
            out.append("\\x%02x" % code if code < 0x100 else "\\u%04x" % code if code < 0x10000 else "\\U%08x" % code)
        prev = ch
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
    """Print in whatever the terminal can show (a character it cannot is written as an escape).
    False when the text could not be written."""
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
        return False
    return True
