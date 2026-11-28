"""traces/live-<kind>-<UTC stamp>/: the trace every live run keeps, pass or fail.

The standing rule: every live run, the smoke included, keeps its full receipt trace in traces/,
marked live, with the date, the commit and the measured cost, committed from wide, pass or fail.
`python -m doris smoke` writes traces/live-smoke-<stamp>/ and `python -m doris run --live` writes
traces/live-run-<stamp>/ when it ends, whatever the outcome. Each folder holds:

  receipts.jsonl  the receipts of her loop's run, byte for byte as written (none when no loop ran,
                  as in a smoke that stopped before its end-to-end test)
  manifest.json   mode live, the kind, UTC start and end, the code's git commit, the model ID, the
                  measured cost in USD (the model calls' own cost lines in those receipts), the
                  counts, the receipt chain's check, and the outcome, pass or fail, with the first
                  failing step
  timeline.txt    one short line per step and per receipt line that matters, UTC time first
  terminal.log    every line the command printed, as printed

A start refused before anything was contacted (a halt, the lock, a setting) is not a run and writes
no folder. Nothing here holds a setting's value: the receipts carry none (the law), the manifest and
the timeline are made of counts, fixed words, step names and receipt kinds (never a subject, an
address or a body), and terminal.log holds the lines the command printed, which name no value either.
"""

import datetime as _dt
import json
import os
import re
import shutil

from . import paths, receipts

UTC = _dt.timezone.utc
KINDS = ("smoke", "run")
STAMP = "%Y%m%dT%H%M%SZ"
STOP_CAUSES = ("cli", "stop_file", "watch_button", "signal")   # a person stopped her: the way a long run ends
TERMINAL = "terminal.log"
_SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?\Z")


def now():
    """UTC now, to the second: when a live command starts and ends."""
    return _dt.datetime.now(UTC).replace(microsecond=0)


def _iso(moment):
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _display(path):
    """Inside the repository, the path from its root, with "/" between its parts on every OS (as
    traces/live-smoke-<stamp> reads in the repo, and on Windows too); anywhere else, the whole path."""
    try:
        rel = os.path.relpath(path, paths.REPO)
    except ValueError:
        return path
    return path if rel.startswith("..") else rel.replace(os.sep, "/")


# ------------------------------------------------------------------------------------------------
# What the folder says
# ------------------------------------------------------------------------------------------------
def git_commit(repo=None):
    """The commit the code was checked out at, read from the repository's own .git (no git program
    needed): HEAD itself, or the branch it names (loose or packed). "unknown" when it cannot be read."""
    repo = repo or paths.REPO
    git = os.path.join(repo, ".git")
    try:
        if os.path.isfile(git):                               # a worktree: .git names its folder
            with open(git, encoding="utf-8") as fh:
                line = fh.read().strip()
            if not line.startswith("gitdir:"):
                return "unknown"
            git = os.path.normpath(os.path.join(repo, line[len("gitdir:"):].strip()))
        with open(os.path.join(git, "HEAD"), encoding="utf-8") as fh:
            head = fh.read().strip()
        if _SHA.match(head):
            return head
        if not head.startswith("ref: "):
            return "unknown"
        ref = head[len("ref: "):].strip()
        common = git
        if os.path.isfile(os.path.join(git, "commondir")):
            with open(os.path.join(git, "commondir"), encoding="utf-8") as fh:
                common = os.path.normpath(os.path.join(git, fh.read().strip()))
        for base in (git, common):
            loose = os.path.join(base, *ref.split("/"))
            if os.path.isfile(loose):
                with open(loose, encoding="utf-8") as fh:
                    sha = fh.read().strip()
                if _SHA.match(sha):
                    return sha
        for base in (git, common):
            packed = os.path.join(base, "packed-refs")
            if os.path.isfile(packed):
                with open(packed, encoding="utf-8") as fh:
                    for line in fh:
                        parts = line.strip().split(" ")
                        if len(parts) == 2 and parts[1] == ref and _SHA.match(parts[0]):
                            return parts[0]
    except (OSError, UnicodeDecodeError, ValueError):
        pass
    return "unknown"


def _calls(lines):
    return {l["seq"]: l["payload"] for l in lines if l["kind"] == "called"}


def measured_cost(lines):
    """USD: the sum of the cost each model call's own result line names."""
    calls = _calls(lines)
    total = 0.0
    for line in lines:
        p = line["payload"]
        if line["kind"] == "result" and calls.get(p.get("call_seq"), {}).get("call") == "model":
            cost = p.get("cost_usd")
            if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                total += cost
    return round(total, 6)


def counts(lines):
    """What the run did, counted from its receipts."""
    from .loop import count_outcomes
    calls = list(_calls(lines).values())
    out = {"messages": sum(1 for l in lines if l["kind"] == "observed" and l["payload"].get("step") != "start"),
           "model_calls": sum(1 for c in calls if c.get("call") == "model"),
           "sends": sum(1 for c in calls if c.get("call") == "send"),
           "errors": sum(1 for l in lines if l["kind"] == "error")}
    out.update(count_outcomes(lines))
    return out


def judge_run(receipts_path, exit_code):
    """(outcome, first failing step) for a live run that has ended: pass only when its chain verifies,
    no line is an error, it did not end on failed reads, and it ended by a person's stop (or by itself
    with exit 0). Holds are her answers, never failures."""
    verified = receipts.verify(receipts_path, finished=True)
    if not verified.ok:
        return "fail", "receipt chain: %s" % (verified.reason,)
    lines = receipts.read_lines(receipts_path)
    for line in lines:
        if line["kind"] == "error":
            return "fail", "error line at seq %d (%s)" % (line["seq"], line["payload"].get("step") or "-")
    summaries = [l["payload"] for l in lines if l["kind"] == "summary"]
    if summaries and summaries[-1].get("read_failed"):
        return "fail", "the inbox could not be read, so the run ended"
    halted = [l["payload"] for l in lines if l["kind"] == "halted"]
    if halted:
        cause = halted[-1].get("cause")
        if halted[-1].get("halt_file") == "unreadable":
            return "fail", "halted: a halt file that could not be read"
        if cause not in STOP_CAUSES:
            return "fail", "halted: %s" % (cause,)
        return "pass", None
    if exit_code != 0:
        return "fail", "the run ended with exit %s" % (exit_code,)
    return "pass", None


# What a receipt line says in the timeline: its kind and a few fixed fields, never a subject or a body.
def _event(line):
    p, kind = line["payload"], line["kind"]
    if kind == "observed":
        if p.get("step") == "start":
            return "run started: mode %s, model %s" % (p.get("mode"), p.get("model"))
        return "message read"
    if kind == "classified":
        return "classified %s" % (p.get("cls"),)
    if kind == "decided":
        return "decided %s" % (p.get("outcome") or p.get("step"),)
    if kind == "held":
        return "held %s" % (p.get("outcome"),)
    if kind == "checked":
        return "gate %s%s" % ("passed" if p.get("passed") else "blocked",
                              "" if p.get("passed") else " at %s" % (p.get("first_fail"),))
    if kind == "error":
        return "error in %s" % (p.get("step"),)
    if kind == "halted":
        return "halted: %s" % (p.get("cause"),)
    if kind == "summary":
        return "summary: %s handled, %s held, %s sent" % (p.get("handled"), p.get("held"), p.get("sends"))
    return None


def _result(line, calls):
    call = calls.get(line["payload"].get("call_seq"), {})
    p = line["payload"]
    if call.get("call") == "send":
        to = " to the %s" % (call["to"],) if call.get("to") in ("sender", "owner") else ""
        return "send %s%s: %s" % (call.get("target"), to, p.get("state"))
    if call.get("call") == "sent_lookup":
        return "her copy in Sent: %s" % ("found" if p.get("found") else "not found" if p.get("ok") else "look failed")
    if call.get("call") == "calendar_create":
        return "calendar create: %s" % (p.get("state"),)
    if call.get("call") == "model" and not p.get("ok"):
        return "model call failed"
    if call.get("call") == "mail_fetch" and not p.get("ok"):
        return "inbox read failed"
    return None


def timeline(lines, printed=()):
    """The short human-readable timeline: the command's steps as printed (time, line), then her loop's
    receipt lines that matter, each with its UTC time and seq."""
    out = ["%s  %s" % (_iso(when), text) for when, text in printed]
    calls = _calls(lines)
    if lines:
        out.append("her loop's receipts (run %s):" % (lines[0].get("run_id"),))
    for line in lines:
        said = _result(line, calls) if line["kind"] == "result" else _event(line)
        if said:
            out.append("%s  seq %d  %s" % (line["ts"], line["seq"], said))
    return out


# ------------------------------------------------------------------------------------------------
# Writing the folder
# ------------------------------------------------------------------------------------------------
def folder_for(kind, started, traces_root=None):
    """traces/live-<kind>-<UTC stamp of the start>, made new (a second one in the same second: -2, -3...)."""
    if kind not in KINDS:
        raise ValueError("a live trace is for a smoke or a run")
    root = traces_root or paths.TRACES
    base = "live-%s-%s" % (kind, started.astimezone(UTC).strftime(STAMP))
    for n in range(1, 100):
        path = os.path.join(root, base if n == 1 else "%s-%d" % (base, n))
        try:
            os.makedirs(path)
            return path
        except FileExistsError:
            continue
    raise FileExistsError("no free trace folder for %s" % (base,))


def write(kind, started, ended, outcome, first_failing, printed=None, model_id=None, receipts_path=None,
          extra=None, traces_root=None, repo=None):
    """Write traces/live-<kind>-<stamp>/ and return its path. printed: the command's terminal (a
    Printed): its lines with their times open the timeline, the trace's own line is said through it
    once the rest is written, and then its text (with its file, every line any of the command's
    processes printed) is terminal.log. receipts_path: her loop's receipts, copied as written."""
    if outcome not in ("pass", "fail"):
        raise ValueError("outcome is pass or fail")
    folder = folder_for(kind, started, traces_root)
    lines, chain = [], {"ok": None, "lines": 0, "reason": "no loop ran"}
    if receipts_path and os.path.isfile(receipts_path):
        copy = os.path.join(folder, "receipts.jsonl")
        shutil.copyfile(receipts_path, copy)
        verified = receipts.verify(copy, finished=True)
        chain = {"ok": verified.ok, "lines": verified.lines, "reason": verified.reason}
        try:
            lines = receipts.read_lines(copy)
        except ValueError:
            lines = []                      # a line that is no JSON object: the chain above says where
    start_lines = [l["payload"] for l in lines if l["kind"] == "observed" and l["payload"].get("step") == "start"]
    files = ["manifest.json", TERMINAL, "timeline.txt"] + (["receipts.jsonl"] if chain["ok"] is not None else [])
    manifest = {
        "mode": "live", "kind": kind, "started_utc": _iso(started), "ended_utc": _iso(ended),
        "commit": git_commit(repo), "model": (start_lines[0].get("model") if start_lines else None) or model_id,
        "cost_usd": measured_cost(lines), "counts": counts(lines), "receipt_chain": chain,
        "outcome": outcome, "first_failing_step": first_failing, "files": sorted(files)}
    manifest.update(extra or {})
    said = list(printed.lines) if printed is not None else []
    _write_text(os.path.join(folder, "manifest.json"), json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    _write_text(os.path.join(folder, "timeline.txt"), "".join(l + "\n" for l in timeline(lines, said)))
    if printed is not None:
        printed("Live trace kept in %s: outcome %s%s." % (
            _display(folder), outcome, "" if first_failing is None else " (first failing step: %s)" % (first_failing,)))
    _write_text(os.path.join(folder, TERMINAL), printed.text() if printed is not None else "")
    return folder


def _write_text(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


class Printed(object):
    """A command's terminal: each line goes to `out` (print, by default) and is kept with its UTC time,
    and, with path, appended to that file at once (so a line printed just before a crash is kept)."""

    def __init__(self, out=None, path=None, clock=None):
        self._out = out or _print
        self.path = path
        self._clock = clock or now
        self.lines = []

    def __call__(self, line):
        self.lines.append((self._clock(), line))
        if self.path is not None:
            try:
                with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass                       # the terminal still gets it; the trace notes what it has
        self._out(line)

    def text(self):
        """Every line printed, as terminal.log holds it: its file's text when it has one (lines another
        process of the command added there included), else its own lines."""
        if self.path is not None:
            try:
                with open(self.path, encoding="utf-8", newline="") as fh:
                    return fh.read()
            except (OSError, UnicodeDecodeError):
                pass
        return "".join(line + "\n" for _, line in self.lines)


def _print(line):
    try:
        print(line, flush=True)
    except (OSError, ValueError):
        pass
