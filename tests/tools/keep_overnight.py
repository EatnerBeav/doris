"""Keep the overnight run's traces, pass or fail (Steve's rule: marked live, date, commit, measured
cost, pass or fail). tests/tools/overnight.sh runs it once the night is over.

    python3 tests/tools/keep_overnight.py --night DIR --started ISO --killed-at ISO
        --run1 RUN_DIR --run2 RUN_DIR [--run2-exit CODE] [--traces-root DIR] [--status-dir DIR] [--rehearsal]

--night DIR       the night's own folder (overnight.sh's): drip-log.jsonl and night.log are read there
--run1 RUN_DIR    the run killed by the planned forced restart (state/live/runs/<name>); "" when the
                  night had no restart (run 1 ended some other way: then it is the only run)
--run2 RUN_DIR    the run started after the restart, ended by `stop` in the morning ("" when none)

What it writes:
- traces/live-run-<stamp>/ for run 1 (livetrace.write: the killed run wrote none itself). Its
  outcome is pass when every whole receipt line verifies to the kill (the closing line, and at
  most a torn last line, are missing by design: the kill came first) and no line is an error;
  the manifest says how it ended (ended_by, killed_at_utc) and what the chain shows to the kill.
- traces/live-run-<stamp>/ for run 2 only when its own command kept none (it keeps its own).
- traces/live-overnight-<stamp>/: drip-log.jsonl and night.log as written, and overnight.json:
  both runs' traces, outcomes, costs and counts, the restart checks, the drip's tally, the total
  measured cost, and the night's outcome, pass or fail, with the first failing check.
- status/<the night's Pacific date>.md: the one-screen status of the night, made from overnight.json.

It prints one KEEP line per path to commit, then one OVERNIGHT line. No setting, address, subject
or body is read or written: the drip log holds Message-IDs and kinds only.
"""

import argparse
import datetime as _dt
import json
import os
import shutil
import sys

TOOLS = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(TOOLS))
SRC = os.path.join(REPO, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from doris import livetrace, paths, receipts       # noqa: E402

UTC = _dt.timezone.utc
PACIFIC_WINTER = _dt.timedelta(hours=-8)          # Nov 28 - Nov 29, 2026 is standard time throughout
ENDED_BY = "the planned forced restart: kill -9 of the run and its worker, no stop asked"


def _t(text):
    return _dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _read_jsonl(path):
    out = []
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        out.append({"event": "unreadable line"})
    return out


def _lines(path):
    try:
        return receipts.read_lines(path)
    except (OSError, ValueError):
        return []


def start_line(lines):
    for l in lines:
        if l["kind"] == "observed" and l["payload"].get("step") == "start":
            return l
    return None


def to_the_kill(receipts_path):
    """(ok, chain dict, first failing) for a killed run's receipts: whole lines verify, no error line."""
    v = receipts.verify(receipts_path, finished=False)
    chain = {"ok": v.ok, "lines": v.lines, "reason": v.reason}
    if not v.ok and not str(v.reason).startswith("torn tail"):
        return False, chain, "receipt chain to the kill: %s" % (v.reason,)
    for line in _lines(receipts_path):
        if line["kind"] == "error":
            return False, chain, "error line at seq %d (%s)" % (line["seq"], line["payload"].get("step") or "-")
    return True, chain, None


def _noop(line):
    pass


def keep_killed(run_dir, started, killed_at, traces_root, night_name, rehearsal):
    path = os.path.join(run_dir, "receipts.jsonl")
    ok, chain, failing = to_the_kill(path)
    printed = livetrace.Printed(out=_noop, path=os.path.join(run_dir, livetrace.TERMINAL))
    extra = {"run_name": os.path.basename(run_dir.rstrip("/\\")), "exit_code": None, "ended_by": ENDED_BY,
             "killed_at_utc": killed_at.strftime("%Y-%m-%dT%H:%M:%SZ"), "receipt_chain_to_the_kill": chain,
             "overnight": night_name}
    if rehearsal:
        extra["rehearsal"] = True
    folder = livetrace.write("run", started, killed_at, "pass" if ok else "fail", failing, printed=printed,
                             receipts_path=path, extra=extra, traces_root=traces_root)
    return folder


def find_trace(run_name, traces_root):
    """The live-run trace a run's own command kept, by its run_name, or None."""
    if not os.path.isdir(traces_root):
        return None
    for name in sorted(os.listdir(traces_root)):
        manifest = os.path.join(traces_root, name, "manifest.json")
        if name.startswith("live-run-") and os.path.isfile(manifest):
            try:
                with open(manifest, encoding="utf-8") as fh:
                    if json.load(fh).get("run_name") == run_name:
                        return os.path.join(traces_root, name)
            except (OSError, ValueError):
                continue
    return None


def keep_ended(run_dir, started, exit_code, ended, traces_root, night_name, rehearsal):
    """Run 2's trace: the one its own command kept, else one written now from its receipts."""
    name = os.path.basename(run_dir.rstrip("/\\"))
    found = find_trace(name, traces_root)
    if found:
        return found, False
    path = os.path.join(run_dir, "receipts.jsonl")
    outcome, failing = livetrace.judge_run(path, exit_code)
    extra = {"run_name": name, "exit_code": exit_code, "overnight": night_name,
             "kept_by": "keep_overnight.py: the run's own command kept no trace"}
    if rehearsal:
        extra["rehearsal"] = True
    printed = livetrace.Printed(out=_noop, path=os.path.join(run_dir, livetrace.TERMINAL))
    return livetrace.write("run", started, ended, outcome, failing, printed=printed, receipts_path=path,
                           extra=extra, traces_root=traces_root), True


def _manifest(folder):
    with open(os.path.join(folder, "manifest.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _rel(path):
    return livetrace._display(path)


def summarize(night, started, ended, killed_at, run1, run2, drip, restart_named):
    runs = [r for r in (run1, run2) if r is not None]
    cost = round(sum(r["cost_usd"] for r in runs), 6)
    keys = ("messages", "model_calls", "sends", "handled", "held", "human_approvals", "errors")
    counts = dict((k, sum(int(r["counts"].get(k) or 0) for r in runs)) for k in keys)
    tally = next((d for d in reversed(drip) if d.get("event") == "tally"), None)
    sent = [d for d in drip if d.get("event") == "sent"]
    checks = []

    def check(name, ok, why=""):
        checks.append({"check": name, "ok": bool(ok), "why": why})

    if run1 is not None and killed_at is not None:
        check("run 1 verifies to the kill, no error line", run1["outcome"] == "pass", run1.get("first_failing_step") or "")
        check("run 2 started after the kill and named run 1 as interrupted", restart_named)
    else:
        check("the forced restart happened", False, "no run was killed at the restart time")
    check("run 2 passed (chain verified, no error, ended by a stop)", run2 is not None and run2["outcome"] == "pass",
          (run2 or {}).get("first_failing_step") or ("no run 2" if run2 is None else ""))
    check("the drip finished its plan", tally is not None and not tally.get("stopped"),
          "no tally" if tally is None else (tally.get("stopped") or ""))
    check("under 60 emails from the test sender", len(sent) < 60, "%d sent" % len(sent))
    check("no message of the drip answered twice with the same kind", tally is not None
          and tally.get("answered_twice") == 0, "" if tally is None else "%s" % tally.get("answered_twice"))
    check("human approvals: 0", counts["human_approvals"] == 0, "%d" % counts["human_approvals"])
    failing = next((c for c in checks if not c["ok"]), None)
    return {
        "mode": "live", "kind": "overnight", "name": night, "started_utc": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ended_utc": ended.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "killed_at_utc": killed_at.strftime("%Y-%m-%dT%H:%M:%SZ") if killed_at else None,
        "commit": livetrace.git_commit(), "cost_usd": cost, "counts": counts,
        "runs": [{"trace": r["trace"], "run_name": r.get("run_name"), "outcome": r["outcome"],
                  "first_failing_step": r.get("first_failing_step"), "cost_usd": r["cost_usd"],
                  "counts": r["counts"], "ended_by": r.get("ended_by", "stop")} for r in runs],
        "drip": {"emails": len(sent), "expected_met": (tally or {}).get("met"), "expected_missed": (tally or {}).get("missed"),
                 "answered_twice": (tally or {}).get("answered_twice"), "stopped": (tally or {}).get("stopped"),
                 "not_as_expected": [r for r in (tally or {}).get("rows", []) if not r.get("ok") or r.get("twice")]},
        "checks": checks, "outcome": "fail" if failing else "pass",
        "first_failing_check": failing["check"] if failing else None}


def pacific(moment):
    return (moment + PACIFIC_WINTER).replace(tzinfo=None)


def _clock(moment):
    p = pacific(moment)
    return "%s %d:%02d %s" % (p.strftime("%a"), p.hour % 12 or 12, p.minute, "AM" if p.hour < 12 else "PM")


def status_text(o, night_folder):
    started = _t(o["started_utc"])
    lines = ["# Overnight status: the night of %s (Pacific)" % pacific(started).strftime("%a %b %d, %Y").replace(" 0", " "),
             "",
             "Outcome: **%s**%s. Commit %s. Measured model cost $%.4f. Human approvals: %d."
             % (o["outcome"], "" if o["outcome"] == "pass" else " (first failing check: %s)" % o["first_failing_check"],
                (o["commit"] or "unknown")[:7], o["cost_usd"], o["counts"]["human_approvals"]),
             "",
             "## What ran",
             "",
             "| Run | Pacific | Outcome | Messages | Handled | Held | Sent | Cost | Trace |",
             "|---|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(o["runs"], 1):
        m = _manifest(os.path.join(paths.REPO, r["trace"])) if os.path.isdir(os.path.join(paths.REPO, r["trace"])) else {}
        when = "%s to %s" % (_clock(_t(m["started_utc"])), _clock(_t(m["ended_utc"]))) if m else ""
        c = r["counts"]
        lines.append("| %d (%s) | %s | %s | %s | %s | %s | %s | $%.4f | [%s](../%s) |" % (
            i, "killed by plan" if r["ended_by"] != "stop" else "stopped", when, r["outcome"], c.get("messages"),
            c.get("handled"), c.get("held"), c.get("sends"), r["cost_usd"], r["trace"], r["trace"]))
    d = o["drip"]
    if d["expected_met"] is None:
        lines += ["", "Test sender (wolf.whizz, tests/tools/overnight_drip.py): %d emails; it wrote no tally." % d["emails"], ""]
    else:
        lines += ["",
                  "Test sender (wolf.whizz, tests/tools/overnight_drip.py): %d emails; %s of %s steps got the answer the "
                  "plan expects; %s answered twice." % (d["emails"], d["expected_met"],
                                                        (d["expected_met"] or 0) + (d["expected_missed"] or 0),
                                                        d["answered_twice"]),
                  ""]
    for r in d["not_as_expected"]:
        lines.append("- Not as expected: %s step %s expected %s, got %s%s." % (
            r["thread"], r["step"], r["expect"], ", ".join(r["got"]) or "nothing",
            " (%s)" % r["why"] if r.get("why") else ""))
    if d["not_as_expected"]:
        lines.append("")
    lines += ["## Checks", ""]
    for c in o["checks"]:
        lines.append("- %s %s%s" % ("pass" if c["ok"] else "FAIL", c["check"], " (%s)" % c["why"] if c["why"] else ""))
    lines += ["", "The night's folder: [%s](../%s) (overnight.json, the drip log, the night's log)." % (night_folder, night_folder), ""]
    return "\n".join(lines)


def main(argv):
    ap = argparse.ArgumentParser(prog="python3 tests/tools/keep_overnight.py")
    ap.add_argument("--night", required=True)
    ap.add_argument("--started", required=True)
    ap.add_argument("--killed-at", default="")
    ap.add_argument("--run1", default="")
    ap.add_argument("--run2", default="")
    ap.add_argument("--run2-started", default="", help="when the night started run 2 (default: --killed-at)")
    ap.add_argument("--run2-exit", type=int, default=None)
    ap.add_argument("--traces-root", default=paths.TRACES)
    ap.add_argument("--status-dir", default=os.path.join(paths.REPO, "status"))
    ap.add_argument("--rehearsal", action="store_true")
    args = ap.parse_args(argv)
    started, ended = _t(args.started), livetrace.now()
    killed_at = _t(args.killed_at) if args.killed_at else None
    night = "live-overnight-%s" % started.strftime(livetrace.STAMP)
    keep = []
    run1 = run2 = None
    restart_named = False
    if args.run1 and killed_at is not None:
        folder = keep_killed(args.run1, started, killed_at, args.traces_root, night, args.rehearsal)
        run1 = dict(_manifest(folder), trace=_rel(folder), ended_by=ENDED_BY)
        keep.append(folder)
    if args.run2:
        run2_started = _t(args.run2_started) if args.run2_started else killed_at or started
        folder, _ = keep_ended(args.run2, run2_started, args.run2_exit, ended, args.traces_root, night, args.rehearsal)
        run2 = dict(_manifest(folder), trace=_rel(folder))
        keep.append(folder)
        first = start_line(_lines(os.path.join(args.run2, "receipts.jsonl")))
        name1 = os.path.basename(args.run1.rstrip("/\\")) if args.run1 else None
        restart_named = bool(first and name1 and name1 in (first["payload"].get("interrupted_runs") or []))
    elif args.run1 and killed_at is None:          # no restart happened: run 1 is the night's only run
        folder, _ = keep_ended(args.run1, started, args.run2_exit, ended, args.traces_root, night, args.rehearsal)
        run2 = dict(_manifest(folder), trace=_rel(folder))
        keep.append(folder)
    out_dir = os.path.join(args.traces_root, night)
    os.makedirs(out_dir)
    for name in ("drip-log.jsonl", "night.log"):
        src = os.path.join(args.night, name)
        if os.path.isfile(src):
            shutil.copyfile(src, os.path.join(out_dir, name))
    o = summarize(night, started, ended, killed_at, run1, run2, _read_jsonl(os.path.join(args.night, "drip-log.jsonl")),
                  restart_named)
    if args.rehearsal:
        o["rehearsal"] = True
    with open(os.path.join(out_dir, "overnight.json"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(o, indent=1, sort_keys=True) + "\n")
    keep.append(out_dir)
    os.makedirs(args.status_dir, exist_ok=True)
    status = os.path.join(args.status_dir, "%s.md" % pacific(started).strftime("%Y-%m-%d"))
    with open(status, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(status_text(o, _rel(out_dir)))
    keep.append(status)
    for path in keep:
        print("KEEP %s" % _rel(path))
    print("OVERNIGHT %s: %s%s; cost $%.4f; %d emails from the test sender"
          % (night, o["outcome"], "" if o["outcome"] == "pass" else " (first failing check: %s)" % o["first_failing_check"],
             o["cost_usd"], o["drip"]["emails"]))
    return 0 if o["outcome"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
