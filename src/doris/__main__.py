"""python -m doris <command>: the one way in.

    demo     the sandbox day under the kill switch: no install, no key
    run      the long run under the kill switch: a pass over the inbox every ten minutes until
             `python -m doris stop`, and the day's digest to the owner at her first pass from
             5:00 PM Pacific; the sandbox by default (no key), or with --live her own
             mailbox and the owner's calendar from the env file (--env PATH, default
             /root/doris-secrets/.env; --fresh starts the live ledger, once)
    traps    the Trap Pack, one file at a time on fresh throwaway state
    verify   recompute a run's receipt chain (the latest sandbox run by default)
    stop     halt Doris now, in the sandbox and live; she stays halted until resume
    resume   clear a halt (only here, on the command line)
    smoke    every live adapter call once against the real servers, before a live run

redteam, replay and watch are not built yet: each says so and exits 2.
"""

import argparse
import datetime as _dt
import json
import os
import sys

from . import caps, loop, paths, receipts, supervisor

SANDBOX_OWNER = loop.SANDBOX_OWNER
SANDBOX_DORIS = loop.SANDBOX_DORIS
STOP_MODES = ("sandbox", "live")
LATER = ("redteam", "replay", "watch")
WORKER = "_worker"
RUN_CYCLE_S = loop.CYCLE.total_seconds()   # `run`: the wall-clock wait from one pass to her next cycle


def answers_path():
    """Recorded model answers for the judge path: sandbox/answers.jsonl, from a real run."""
    return os.path.join(paths.SANDBOX, "answers.jsonl")


def demo_source(env, answers_exist):
    """A key means the model is called live; recorded answers without a key are replayed; neither,
    the built-in stand-in. A run never mixes them."""
    if (env.get("ANTHROPIC_API_KEY") or "").strip():
        return "live"
    return "replayed" if answers_exist else "stub"


def run_name(source, mode):
    """model-<source>-<mode>-<wall clock, UTC>: the source and mode first, so a trace says what it is (A6)."""
    return "model-%s-%s-%s" % (source, mode, _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ"))


def _recorded_run(path):
    try:
        with open(path, encoding="utf-8") as fh:
            first = json.loads(fh.readline() or "{}")
        return str(first.get("run_id") or "unknown")
    except (OSError, ValueError, AttributeError):
        return "unknown"


def first_line(source, model_id):
    if source == "live":
        return "Sandbox. Model called live (%s); its answers are recorded with this run." % (model_id,)
    if source == "replayed":
        return ("Sandbox. Model answers replayed from recorded run %s. No key found."
                % (_recorded_run(answers_path()),))
    return ("Sandbox. No key and no recorded answers, so a built-in keyword stand-in (stub) reads the mail; "
            "every receipt says stub. Add ANTHROPIC_API_KEY to call the model.")


# --- demo and run --------------------------------------------------------------------------------
def demo(argv):
    argparse.ArgumentParser(prog="python -m doris demo",
                            description="The sandbox day under the kill switch.").parse_args(argv)
    refusal = supervisor._halt_refusal("sandbox")
    if refusal:
        print(refusal)
        return supervisor.EXIT_REFUSED_HALT
    source, model_id = sandbox_source()
    state = paths.state_dir("sandbox")
    name, run_dir = new_run_dir(state, run_name(source, "sandbox"))
    _say(first_line(source, model_id))
    return supervise("sandbox", state, name, run_dir,
                     sandbox_config(name, state, run_dir, source, model_id, digest=True))


def run_cmd(argv):
    ap = argparse.ArgumentParser(
        prog="python -m doris run",
        description="The long run under the kill switch: a pass over the inbox every ten minutes until "
                    "`python -m doris stop`, and the day's digest to the owner from 5:00 PM Pacific. "
                    "The sandbox by default; --live for her own mailbox.")
    ap.add_argument("--live", action="store_true",
                    help="her own mailbox and the owner's shared calendar, every setting from the env file")
    ap.add_argument("--fresh", action="store_true",
                    help="live: start the live ledger (once); she reads only mail that arrives after it")
    ap.add_argument("--env", metavar="PATH", help="live: the env file (default %s)" % paths.LIVE_ENV)
    args = ap.parse_args(argv)
    if not args.live and (args.fresh or args.env is not None):
        print("Not started: --fresh and --env are for a live run, so they need --live.")
        return 2
    mode = "live" if args.live else "sandbox"
    refusal = supervisor._halt_refusal(mode)
    if refusal:
        print(refusal)
        return supervisor.EXIT_REFUSED_HALT
    state = paths.state_dir(mode)
    every = "every %s until `python -m doris stop`" % _span(RUN_CYCLE_S)
    if not args.live:
        source, model_id = sandbox_source()
        name, run_dir = new_run_dir(state, run_name(source, "sandbox"))
        _say(first_line(source, model_id))
        _say("She reads the sandbox inbox %s; a new .eml dropped in waits for her next cycle." % every)
        return supervise("sandbox", state, name, run_dir, sandbox_config(
            name, state, run_dir, source, model_id, digest=True, cycles=None, cycle_s=RUN_CYCLE_S))
    env_path = os.path.abspath(args.env or paths.LIVE_ENV)
    try:                                     # the worker makes every check again, under its lock
        settings = loop.live_preflight(state, env_path, args.fresh)
    except loop.Refused as refused:
        print(refused)
        return refused.exit_code
    name, run_dir = new_run_dir(state, run_name("live", "live"))
    _say("Live. Every setting from the env file %s; model called live (%s). She reads her own inbox %s.%s"
         % (env_path, settings.model_id, every,
            " A fresh live ledger: she reads only mail that arrives from now on." if args.fresh else ""))
    # No setting goes in the config: the worker loads the env file itself.
    return supervise("live", state, name, run_dir, {
        "mode": "live", "run_name": name, "state_dir": state, "env_path": env_path, "fresh": args.fresh,
        "model_source": "live", "record_path": os.path.join(run_dir, "recorded-answers.jsonl"),
        "clock_mode": "fixed", "digest": True, "cycles": None, "cycle_s": RUN_CYCLE_S})


def sandbox_source():
    """(model source, model ID) for a sandbox run: see demo_source; the ID only when called live."""
    from . import model as model_mod
    source = demo_source(os.environ, os.path.exists(answers_path()))
    return source, model_mod.default_model_id(os.environ) if source == "live" else None


def sandbox_config(name, state, run_dir, source, model_id, **more):
    config = {"mode": "sandbox", "run_name": name, "state_dir": state, "sandbox_root": paths.SANDBOX,
              "owner_addr": SANDBOX_OWNER, "doris_addr": SANDBOX_DORIS, "model_source": source,
              "model_id": model_id, "answers_path": answers_path() if source == "replayed" else None,
              "record_path": os.path.join(run_dir, "recorded-answers.jsonl") if source == "live" else None,
              "clock_mode": "advance", "calendar_timeout_s": caps.CALENDAR_TIMEOUT_S,
              "retry_wait_s": caps.CALENDAR_RETRY_WAIT_S}
    config.update(more)
    return config


def new_run_dir(state, base):
    """(name, folder) for a new run under state/runs: two runs in one second each get their own."""
    name = base
    for n in range(2, 100):
        run_dir = os.path.join(state, "runs", name)
        try:
            os.makedirs(run_dir)
            break
        except FileExistsError:
            name = "%s-%d" % (base, n)
    return name, run_dir


def supervise(mode, state, name, run_dir, config):
    """The run's config.json, then its worker under the kill switch."""
    config_path = os.path.join(run_dir, "config.json")
    with open(config_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(config, fh, indent=1, sort_keys=True)
        fh.write("\n")
    return supervisor.run_supervised(
        mode, name, [sys.executable, "-m", "doris", WORKER, config_path],
        os.path.join(run_dir, "receipts.jsonl"), os.path.join(state, "ledger.sqlite"), cwd=paths.SRC, run_id=name)


def _span(seconds):
    if seconds >= 60 and seconds % 60 == 0:
        minutes = int(seconds // 60)
        return "minute" if minutes == 1 else "%d minutes" % minutes
    return "%g seconds" % seconds


def worker(argv):
    """The child the kill switch watches: one run from its config file. Exit 0 done, 1 the run
    failed (the inbox could not be read, or the chain failed its own check), 3 another Doris holds
    the ledger, 4 halted (the parent writes that line)."""
    with open(argv[0], encoding="utf-8") as fh:
        fields = json.load(fh)
    cfg = loop.RunConfig(supervised=True, say=_say, **fields)
    try:
        s = loop.run(cfg)
    except loop.Refused as refused:
        _say(str(refused))
        return refused.exit_code
    except loop.Halted:
        return supervisor.WORKER_EXIT_HALTED
    if s.read_failed:
        _say("Stopped: the inbox could not be read, so the run ended there (%d handled, %d held before it). "
             "Nothing was read as an empty inbox. Receipts %s: %s."
             % (s.handled, s.held, _display(s.receipts_path), "chain verified" if s.verify_ok else "CHAIN BROKEN"))
        return 1
    cost = "none (stub)" if cfg.model_source == "stub" else "$%.4f%s" % (
        s.cost_usd, " (recorded)" if cfg.model_source == "replayed" else "")
    _say("Done: %d handled, %d held, %d human approvals. %d sent to prospects, %d hold notices, %d bookings. "
         "Model calls %d, cost %s. Receipts %s: %s."
         % (s.handled, s.held, s.human_approvals, s.sends, s.hold_notices, s.bookings, s.model_calls, cost,
            _display(s.receipts_path), "chain verified" if s.verify_ok else "CHAIN BROKEN"))
    return 0 if s.verify_ok else 1


# --- stop and resume ----------------------------------------------------------------------------
def stop(argv):
    argparse.ArgumentParser(prog="python -m doris stop",
                            description="Halt Doris now, in the sandbox and live.").parse_args(argv)
    for mode in STOP_MODES:
        supervisor.request_stop(mode, "cli")
    print("Stopped: Doris is halted in the sandbox and live. A run in progress is killed mid-call and its "
          "receipt names what was dropped. Nothing more is sent or booked until `python -m doris resume`.")
    return 0


def resume(argv):
    argparse.ArgumentParser(prog="python -m doris resume",
                            description="Clear a halt. Only the command line can.").parse_args(argv)
    code, said = 0, []
    for mode in STOP_MODES:
        r = supervisor.resume(mode)
        if not r.ok:
            code = 1
        if r.cleared or r.stop_files or not r.ok:
            said.append("%s: %s" % (mode, r.message))
    print("\n".join(said) if said else "Resume: nothing was halted.")
    return code


# --- verify ----------------------------------------------------------------------------------------
def latest_receipts(mode="sandbox"):
    runs = paths.runs_dir(mode)
    found = []
    for name in os.listdir(runs) if os.path.isdir(runs) else ():
        path = os.path.join(runs, name, "receipts.jsonl")
        if os.path.isfile(path):
            found.append((os.path.getmtime(path), path))
    return max(found)[1] if found else None


def verify(argv):
    ap = argparse.ArgumentParser(prog="python -m doris verify", description="Recompute a run's receipt chain.")
    ap.add_argument("path", nargs="?", help="a receipts file (default: the latest sandbox run)")
    args = ap.parse_args(argv)
    path = args.path or latest_receipts()
    if path is None:
        print("No run to verify yet: run `python -m doris demo` first, or name a receipts file.")
        return 2
    r = receipts.verify(path, finished=True)
    if r.ok:
        print("ok: %d lines, every hash matches the line before it (%s). %s" % (r.lines, _display(path), r.reason))
        return 0
    where = "seq %d" % r.first_bad_seq if r.first_bad_seq else "the file"
    print("BROKEN at %s: %s. %d lines verified before it (%s)." % (where, r.reason, r.lines, _display(path)))
    return 1


# --- the rest ---------------------------------------------------------------------------------------
def traps_cmd(argv):
    from . import traps
    return traps.main(argv)


def later(name):
    def run(argv):
        print("`python -m doris %s` is not built yet." % (name,))
        return 2
    return run


def _say(text):
    print(text)
    sys.stdout.flush()


def _display(path):
    """Inside the repository, the path from its root; anywhere else, the whole path."""
    try:
        rel = os.path.relpath(path, paths.REPO)
    except ValueError:
        return path
    return path if rel.startswith("..") else rel


def smoke_cmd(argv):
    from . import smoke                   # live adapters load only when asked for
    return smoke.main(argv)


COMMANDS = {"demo": demo, "run": run_cmd, "traps": traps_cmd, "verify": verify, "stop": stop, "resume": resume,
            "smoke": smoke_cmd, WORKER: worker}
COMMANDS.update({name: later(name) for name in LATER})


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0 if argv else 2
    command = COMMANDS.get(argv[0])
    if command is None:
        print("Unknown command %r.\n\n%s" % (argv[0], __doc__.strip()))
        return 2
    return command(argv[1:])


if __name__ == "__main__":
    sys.exit(main())
