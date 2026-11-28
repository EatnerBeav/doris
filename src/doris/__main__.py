"""python3 -m doris <command>: the one way in.

    demo     the sandbox day under the kill switch: no install, no key
    run      the long run under the kill switch: a pass over the inbox every ten minutes until
             `python3 -m doris stop`, and the day's digest to the owner at her first pass from
             5:00 PM Pacific; the sandbox by default (no key), or with --live her own
             mailbox and the owner's calendar from the env file (--env PATH, default
             /root/doris-secrets/.env, on our server; never asked of a judge; --fresh
             starts the live ledger, once)
    traps    the Trap Pack, one file at a time on fresh throwaway state
    verify   recompute a run's receipt chain (the latest sandbox run by default)
    stop     halt Doris now, in the sandbox and live; she stays halted until resume
    resume   clear a halt (only here, on the command line)
    smoke    every live adapter call once against the real servers, before a live run
    replay   a finished run told step by step from its receipts alone, chain checked first
             (the latest sandbox run by default; or a run ID, run folder or trace folder)
    watch    the live page on 127.0.0.1: current step, tool calls, decisions with their why,
             cost so far, and one Stop button (--port N)
    redteam  the yes-man proof: the real loop and gate with a model that obeys every
             injection, on throwaway state; passes only with 0 bad actions

With ANTHROPIC_API_KEY in .env at the repo root (or in src/.env, or in the environment, which wins),
demo, the sandbox run and traps --model live call the model and print its measured cost.

Every live run, the smoke included, keeps its trace in traces/live-<run|smoke>-<UTC stamp>/ when
it ends, pass or fail (livetrace.py); a start refused before anything was contacted keeps none.
"""

import argparse
import contextlib
import datetime as _dt
import json
import os
import sys
import time

from . import caps, envfile, livetrace, loop, paths, receipts, story, supervisor

SANDBOX_OWNER = loop.SANDBOX_OWNER
SANDBOX_DORIS = loop.SANDBOX_DORIS
STOP_MODES = ("sandbox", "live")
STOP_WHERE = {"sandbox": "in the sandbox", "live": "live"}
WORKER = "_worker"
SHOWN_FROM = paths.SHOWN_FROM      # set for her worker only: the folder the command was run in (paths.shown)
RUN_CYCLE_S = loop.CYCLE.total_seconds()   # `run`: the wall-clock wait from one pass to her next cycle


def answers_path():
    """Recorded model answers for the judge path: sandbox/answers.jsonl, from a real run."""
    return os.path.join(paths.SANDBOX, "answers.jsonl")


# --- the model key from .env --------------------------------------------------------------------
DOTENV_NAMES = ("ANTHROPIC_API_KEY", "DORIS_MODEL")    # all a sandbox run or traps takes from .env


def dotenv_files():
    """The .env files a judge may fill, first first: the repo root's, beside .env.example, then src/'s,
    the folder every command is run from."""
    return [os.path.join(paths.REPO, ".env"), os.path.join(paths.SRC, ".env")]


def load_dotenv(environ=None, files=None):
    """ANTHROPIC_API_KEY and DORIS_MODEL from .env into this command's environment, which the worker it
    starts inherits, so a key in .env calls the model just as an exported one does. A name already in
    the environment, even empty, is left as it is: the environment wins. The first file that gives a
    name a value gives it. No other name is taken, the live run reads none of this (only its own env
    file), and no value is ever printed. Returns {name: the file it came from}. A file outside the
    KEY=VALUE format raises envfile.EnvFileError naming the file and the line, never a value."""
    environ = os.environ if environ is None else environ
    came_from = {}
    for path in dotenv_files() if files is None else files:
        if not os.path.isfile(path):
            continue
        try:
            values = envfile.load(path, names=DOTENV_NAMES)
        except envfile.EnvFileError as exc:
            raise envfile.EnvFileError("%s: %s" % (_display(path), exc)) from None
        for name in DOTENV_NAMES:
            value = (values.get(name) or "").strip()
            if value and name not in environ and name not in came_from:
                environ[name] = value
                came_from[name] = path
    return came_from


def model_key():
    """load_dotenv, then where the key is from, for the first line: the .env path as typed from here,
    "the environment", or None when there is no key."""
    came_from = load_dotenv()
    if "ANTHROPIC_API_KEY" in came_from:
        return _display(came_from["ANTHROPIC_API_KEY"])
    return "the environment" if (os.environ.get("ANTHROPIC_API_KEY") or "").strip() else None


@contextlib.contextmanager
def refusals_said(say):
    """For one command: the model adapter says a refused call through say, once (model.NOTICE), and
    the hook is put back as it was afterwards."""
    from . import model as model_mod
    before = model_mod.NOTICE
    model_mod.NOTICE = model_mod.Notice(say)
    try:
        yield
    finally:
        model_mod.NOTICE = before


def _not_started_env(exc):
    print("Not started: %s. Fix that line or remove the file." % (exc,))
    return 2


def demo_source(env, answers_exist):
    """A key means the model is called live; recorded answers without a key are replayed; neither,
    the built-in stand-in. A run never mixes them."""
    if (env.get("ANTHROPIC_API_KEY") or "").strip():
        return "live"
    return "replayed" if answers_exist else "stub"


def run_name(source, mode, when=None):
    """model-<source>-<mode>-<wall clock, UTC>: the source and mode first, so a trace says what it is (A6).
    when: the run's start (an aware datetime), else now."""
    when = when or _dt.datetime.now(_dt.timezone.utc)
    return "model-%s-%s-%s" % (source, mode, when.astimezone(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ"))


def _recorded_first(path, field):
    try:
        with open(path, encoding="utf-8") as fh:
            first = json.loads(fh.readline() or "{}")
        value = first.get(field)
        return value if isinstance(value, str) and value.strip() else None
    except (OSError, ValueError, AttributeError):
        return None


def _recorded_run(path):
    return _recorded_first(path, "run_id") or "unknown"


def first_line(source, model_id, key_from=None):
    """The banner: what reads the mail, and for a live model where the key came from (model_key)."""
    if source == "live":
        return ("Sandbox. Model called live (%s, key from %s); its answers are recorded with this run."
                % (model_id, key_from or "the environment"))
    if source == "replayed":
        return ("Sandbox. Model answers replayed from recorded run %s. No key in .env or the environment."
                % (_recorded_run(answers_path()),))
    return ("Sandbox. No key and no recorded answers, so a built-in keyword stand-in (stub) reads the mail; "
            "every receipt says stub. Put ANTHROPIC_API_KEY in .env or the environment to call the model.")


# --- demo and run --------------------------------------------------------------------------------
def demo(argv):
    argparse.ArgumentParser(prog=paths.command("demo"),
                            description="The sandbox day under the kill switch.").parse_args(argv)
    refusal = supervisor._halt_refusal("sandbox")
    if refusal:
        print(refusal)
        return supervisor.EXIT_REFUSED_HALT
    try:
        kept = fresh_day()
    except NotMoved as refused:
        print(refused)
        return 1
    left = orphaned_outputs()
    if left:
        print("Not started: her sends or bookings from an earlier demo day are still in the sandbox, but that day's "
              "state is not in %s, so a demo now would send and book them all again beside them. Move these into "
              "that day's folder in %s (or out of the sandbox), then run the demo again:\n%s"
              % (os.path.join(_display(paths.state_dir("sandbox")), ""),
                 os.path.join(_display(os.path.join(paths.STATE, "old")), ""),
                 "\n".join("  " + _display(f) for f, _ in left)))
        return 1
    try:
        key_from = model_key()
    except envfile.EnvFileError as exc:
        return _not_started_env(exc)
    source, model_id = sandbox_source()
    state = paths.state_dir("sandbox")
    name, run_dir = new_run_dir(state, run_name(source, "sandbox"))
    _say(first_line(source, model_id, key_from))
    if kept:
        _say("A fresh sandbox day: the last demo's state and her sends and bookings are kept in %s."
             % (os.path.join(_display(kept), ""),))
    code = supervise("sandbox", state, name, run_dir,
                     sandbox_config(name, state, run_dir, source, model_id, digest=True))
    for line in story.lines(os.path.join(run_dir, "receipts.jsonl")):     # what she did, from her receipts alone
        _say(line)
    return code


# --- a fresh day for each demo -------------------------------------------------------------------
RUN_CALENDAR = ("doris-", "staged-")     # what a run writes into sandbox/calendar/: her bookings, staged events


class NotMoved(Exception):
    """The last demo day could not be moved aside: the demo is not started, and the message says
    where everything is."""


MOVE_RETRY_WAITS_S = (0.1, 0.3, 0.6)


def _move(src, dst):
    """One folder or file, never copied. Windows refuses one held open; a virus scanner or the indexer
    holds a just-written file for a moment, so that refusal is tried again a few times before it is said."""
    for wait in MOVE_RETRY_WAITS_S:
        try:
            os.rename(src, dst)
            return
        except PermissionError:
            time.sleep(wait)
    os.rename(src, dst)


def last_day_finished():
    """True when the latest sandbox run wrote its closing summary line and no halted line: the day
    is done. Anything else (no run yet; a halt, whose dropped mail the next run reads again with
    nothing sent twice; a crash; a run still going; a file that cannot be read) is not."""
    path = latest_receipts("sandbox")
    if path is None:
        return False
    try:
        lines = receipts.read_lines(path)
    except Exception:
        return False
    return bool(lines) and lines[-1].get("kind") == "summary" and not any(l.get("kind") == "halted" for l in lines)


def her_outputs():
    """[(file, folder name under the kept day)] for what a run wrote into the sandbox: her sends
    (outbox/, but .gitkeep) and her bookings and staged events (calendar/doris-*.ics, staged-*.ics)."""
    found = []
    for folder, mine in (("outbox", lambda n: n != ".gitkeep"),
                         ("calendar", lambda n: n.startswith(RUN_CALENDAR) and n.endswith(".ics"))):
        where = os.path.join(paths.SANDBOX, folder)
        for name in sorted(os.listdir(where)) if os.path.isdir(where) else ():
            if mine(name) and os.path.isfile(os.path.join(where, name)):
                found.append((os.path.join(where, name), folder))
    return found


def orphaned_outputs():
    """Her sends or bookings in the sandbox with no sandbox ledger beside them: a day half moved aside (a
    move that could not be put back, or one cut off). A demo then would start a new ledger and send and
    book the whole day again beside them, so it is refused. [] when there is a ledger, or nothing of hers."""
    if os.path.exists(os.path.join(paths.state_dir("sandbox"), "ledger.sqlite")):
        return []
    return her_outputs()


def fresh_day():
    """Before a demo: when the last sandbox day finished, move it aside, never delete it, into
    state/old/sandbox-<UTC stamp>/ (the whole state/sandbox folder as one rename, then her sends in
    outbox/ and her bookings in calendar/ inside it), so this demo is the same day again. Returns
    that folder, or None when nothing was moved (see last_day_finished). A move that fails puts back
    what had moved and raises NotMoved; whatever cannot be put back is named, with where it is."""
    if not last_day_finished():
        return None
    state, old = paths.state_dir("sandbox"), os.path.join(paths.STATE, "old")
    base = "sandbox-" + _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    kept = os.path.join(old, base)
    for n in range(2, 100):
        if not os.path.lexists(kept):
            break
        kept = os.path.join(old, "%s-%d" % (base, n))
    moved = []                              # (from, to), in the order they moved
    src = old
    try:
        os.makedirs(old, exist_ok=True)
        src = state
        _move(state, kept)
        moved.append((state, kept))
        for src, folder in her_outputs():
            os.makedirs(os.path.join(kept, folder), exist_ok=True)
            dst = os.path.join(kept, folder, os.path.basename(src))
            _move(src, dst)
            moved.append((src, dst))
    except OSError as exc:
        stuck = _put_back(moved, kept)
        said = ("Not started: the last demo day could not be moved aside. %s refused to move (%s: %s)"
                % (_display(src), type(exc).__name__, exc.strerror or exc))
        if stuck:
            said += (". These had already moved and could not be put back; nothing is lost. Move them back by "
                     "hand before the next demo:\n"
                     + "\n".join("  %s is now at %s" % (_display(a), _display(b)) for a, b in stuck))
        else:
            said += ("; close whatever holds it open and run the demo again. Nothing was moved: her state, sends "
                     "and bookings are where they were.")
        raise NotMoved(said) from exc
    return kept


def _put_back(moved, kept):
    """Undo fresh_day's moves, last first. Returns the (from, to) pairs that could not be put back."""
    stuck = []
    for src, dst in reversed(moved):
        try:
            if dst == kept:                 # the state folder: without the folders made inside it
                if stuck:                   # a send or booking is still in there: it stays where that is said
                    raise OSError("her sends or bookings are still inside it")
                for folder in ("outbox", "calendar"):
                    if os.path.isdir(os.path.join(kept, folder)) and not os.listdir(os.path.join(kept, folder)):
                        os.rmdir(os.path.join(kept, folder))
            _move(dst, src)
        except OSError:
            stuck.append((src, dst))
    return list(reversed(stuck))


def run_cmd(argv):
    ap = argparse.ArgumentParser(
        prog=paths.command("run"),
        description="The long run under the kill switch: a pass over the inbox every ten minutes until "
                    "`%s`, and the day's digest to the owner from 5:00 PM Pacific. "
                    "The sandbox by default; --live for her own mailbox." % paths.command("stop"))
    ap.add_argument("--live", action="store_true",
                    help="her own mailbox and the owner's shared calendar, every setting from the env file")
    ap.add_argument("--fresh", action="store_true",
                    help="live: start the live ledger (once); she reads only mail that arrives after it")
    ap.add_argument("--env", metavar="PATH",
                    help="live: the env file (default %s, on our server; never asked of a judge)" % paths.LIVE_ENV)
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
    every = "every %s until `%s`" % (loop.wait_words(RUN_CYCLE_S), paths.command("stop"))
    if not args.live:
        try:
            key_from = model_key()
        except envfile.EnvFileError as exc:
            return _not_started_env(exc)
        source, model_id = sandbox_source()
        name, run_dir = new_run_dir(state, run_name(source, "sandbox"))
        _say(first_line(source, model_id, key_from))
        _say("She reads the sandbox inbox %s; a new .eml dropped in waits for her next cycle." % every)
        return supervise("sandbox", state, name, run_dir, sandbox_config(
            name, state, run_dir, source, model_id, digest=True, cycles=None, cycle_s=RUN_CYCLE_S))
    env_path = os.path.abspath(args.env or paths.LIVE_ENV)
    try:                                     # the worker makes every check again, under its lock
        settings = loop.live_preflight(state, env_path, args.fresh)
    except loop.Refused as refused:
        print(refused)
        return refused.exit_code
    started = livetrace.now()
    name, run_dir = new_run_dir(state, run_name("live", "live", started))
    # Every line from here goes to the terminal and to the run's terminal.log, the worker's too.
    term = livetrace.Printed(out=_say, path=os.path.join(run_dir, livetrace.TERMINAL))
    term("Live. Every setting from the env file %s; model called live (%s). She reads her own inbox %s.%s"
         % (env_path, settings.model_id, every,
            " A fresh live ledger: she reads only mail that arrives from now on." if args.fresh else ""))
    # No setting goes in the config: the worker loads the env file itself.
    code = supervise("live", state, name, run_dir, {
        "mode": "live", "run_name": name, "state_dir": state, "env_path": env_path, "fresh": args.fresh,
        "model_source": "live", "record_path": os.path.join(run_dir, "recorded-answers.jsonl"),
        "clock_mode": "fixed", "digest": True, "cycles": None, "cycle_s": RUN_CYCLE_S}, say=term)
    return live_trace(run_dir, name, started, code, term)


def live_trace(run_dir, name, started, code, term):
    """traces/live-run-<stamp>/ for a live run that has ended, pass or fail (livetrace). A start
    refused before her loop wrote a receipt (a halt, the lock, a setting the worker checks again under
    its lock) contacted nothing and is no run: it keeps none. Returns the run's exit code, as it was."""
    receipts_path = os.path.join(run_dir, "receipts.jsonl")
    if not os.path.isfile(receipts_path):
        return code
    outcome, first = livetrace.judge_run(receipts_path, code)
    try:
        livetrace.write("run", started, livetrace.now(), outcome, first, printed=term, receipts_path=receipts_path,
                        extra={"run_name": name, "exit_code": code})
    except Exception as exc:                 # said loudly; the run's own outcome stands
        term("Live trace NOT kept: %s. Her receipts are in %s." % (type(exc).__name__, _display(receipts_path)))
    return code


def sandbox_source():
    """(model source, model ID) for a sandbox run: see demo_source. Live: DORIS_MODEL or the default.
    Replayed: the model the answers were recorded with, never DORIS_MODEL, since the model ID is part
    of every request a recorded answer is matched by. Stub: none."""
    from . import model as model_mod
    source = demo_source(os.environ, os.path.exists(answers_path()))
    if source == "live":
        return source, model_mod.default_model_id(os.environ)
    if source == "replayed":
        return source, _recorded_first(answers_path(), "model_id") or model_mod.DEFAULT_MODEL
    return source, None


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


def supervise(mode, state, name, run_dir, config, say=None):
    """The run's config.json, then its worker under the kill switch, whose lines go through say."""
    config_path = os.path.join(run_dir, "config.json")
    with open(config_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(config, fh, indent=1, sort_keys=True)
        fh.write("\n")
    before = os.environ.get(SHOWN_FROM)
    os.environ[SHOWN_FROM] = os.getcwd()     # her worker runs in src/; its printed paths are from here
    try:
        return supervisor.run_supervised(
            mode, name, [sys.executable, "-m", "doris", WORKER, config_path],
            os.path.join(run_dir, "receipts.jsonl"), os.path.join(state, "ledger.sqlite"), say=say, cwd=paths.SRC,
            run_id=name)
    finally:
        if before is None:
            os.environ.pop(SHOWN_FROM, None)
        else:
            os.environ[SHOWN_FROM] = before


def worker(argv):
    """The child the kill switch watches: one run from its config file. Exit 0 done, 1 the run
    failed (a finite run's inbox could not be read, or the chain failed its own check; a run until
    stopped never ends on failed reads: decision 89), 3 another Doris
    holds the ledger, 4 halted (the parent writes that line; when her supervisor is gone, she has
    written it herself). Under run_supervised her stdin is her lifeline to it (supervisor.Lifeline).
    A live worker's lines also go to terminal.log beside its config, for the run's trace."""
    with open(argv[0], encoding="utf-8") as fh:
        fields = json.load(fh)
    say = _say
    if fields.get("mode") == "live":
        say = livetrace.Printed(out=_say, path=os.path.join(os.path.dirname(os.path.abspath(argv[0])),
                                                            livetrace.TERMINAL))
    lifeline = supervisor.lifeline(fields.get("mode"), fields.get("state_dir"))
    cfg = loop.RunConfig(supervised=True, say=say, lifeline=lifeline, **fields)
    try:
        with refusals_said(say):            # a refused key or tier: said once, before the holds
            s = loop.run(cfg)
    except loop.Refused as refused:
        say(str(refused))
        return refused.exit_code
    except loop.Halted:
        return supervisor.WORKER_EXIT_HALTED
    if s.halted:                    # her supervisor is gone: she wrote the halted line and the summary
        why = ("Halted: her supervisor is gone, so she stopped herself" if s.halted == "supervisor_gone" else
               "Halted (%s), and her supervisor is gone, so she stopped" % (supervisor.CAUSE_WORDS.get(s.halted, s.halted),))
        say("%s and wrote the halted line. %d handled, %d held before it. Receipts %s: %s. Run `%s` to go on."
            % (why, s.handled, s.held, _display(s.receipts_path), "chain verified" if s.verify_ok else "CHAIN BROKEN",
               supervisor._resume_command()))
        return supervisor.WORKER_EXIT_HALTED
    if s.read_failed:
        say("Stopped: the inbox could not be read, so the run ended there (%d handled, %d held before it). "
            "Nothing was read as an empty inbox. Receipts %s: %s."
            % (s.handled, s.held, _display(s.receipts_path), "chain verified" if s.verify_ok else "CHAIN BROKEN"))
        return 1
    cost = "none (stub)" if cfg.model_source == "stub" else "$%.4f%s" % (
        s.cost_usd, " (recorded)" if cfg.model_source == "replayed" else "")
    say("Done: %d handled, %d held, %s. %d sent to prospects, %s, %s. "
        "%s calls %d, cost %s. Receipts %s: %s."
        % (s.handled, s.held, _n(s.human_approvals, "human approval"), s.sends, _n(s.hold_notices, "hold notice"),
           _n(s.bookings, "booking"),
           "Stand-in" if cfg.model_source == "stub" else "Model", s.model_calls, cost,
           _display(s.receipts_path), "chain verified" if s.verify_ok else "CHAIN BROKEN"))
    return 0 if s.verify_ok else 1


def _n(k, word):
    return "%d %s%s" % (k, word, "" if k == 1 else "s")


# --- stop and resume ----------------------------------------------------------------------------
def stop(argv):
    argparse.ArgumentParser(prog=paths.command("stop"),
                            description="Halt Doris now, in the sandbox and live.").parse_args(argv)
    halted, failed = [], []
    for mode in STOP_MODES:                 # every mode is tried, whatever the one before it did
        try:
            supervisor.request_stop(mode, "cli")
            if supervisor.halt_cause(mode) is None:       # read back from disk: said only when it is there
                raise OSError("no halt is in force after the write")
            halted.append(mode)
        except Exception as exc:            # noqa: BLE001 - the other mode is still stopped, and this is said
            failed.append((mode, "%s: %s" % (type(exc).__name__, exc)))
    if not failed:
        print("Stopped: Doris is halted in the sandbox and live. A run in progress is killed mid-call and its "
              "receipt names what was dropped. Nothing more is sent or booked until `%s`." % paths.command("resume"))
        return 0
    for mode, why in failed:
        print("NOT halted %s: %s." % (STOP_WHERE[mode], why.rstrip(".")))
    for mode in halted:
        print("Halted %s. Nothing more is sent or booked there until `%s`."
              % (STOP_WHERE[mode], paths.command("resume")))
    print("The stop did NOT land everywhere: run `%s` again now, and check that no run goes on."
          % paths.command("stop"))
    return 1


def resume(argv):
    argparse.ArgumentParser(prog=paths.command("resume"),
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
    ap = argparse.ArgumentParser(prog="python3 -m doris verify", description="Recompute a run's receipt chain.")
    ap.add_argument("path", nargs="?", help="a receipts file (default: the latest sandbox run)")
    args = ap.parse_args(argv)
    path = args.path or latest_receipts()
    if path is None:
        print("No run to verify yet: run `python -m doris demo` first, or name a receipts file.")
        return 2
    r = receipts.verify(path, finished=True)
    if r.ok:
        notes = "; ".join(r.reason.split("; ")[1:])        # the reason is "ok", then anything the chain records
        print("ok: %d lines, every hash matches the line before it (%s).%s"
              % (r.lines, _display(path), " %s." % (notes[:1].upper() + notes[1:]) if notes else ""))
        return 0
    where = "seq %d" % r.first_bad_seq if r.first_bad_seq else "the file"
    said = "broken at %s: " % where                        # the reason names the seq too: say it once
    reason = r.reason[len(said):] if r.reason.startswith(said) else r.reason
    before = ". %d line%s verified before it" % (r.lines, "" if r.lines == 1 else "s") if r.lines else ""
    print("BROKEN at %s: %s%s (%s)." % (where, reason, before, _display(path)))
    return 1


# --- the rest ---------------------------------------------------------------------------------------
def traps_cmd(argv):
    from . import traps
    try:
        load_dotenv()                     # the key and model for `traps --model live`, as for demo
    except envfile.EnvFileError as exc:
        return _not_started_env(exc)
    with refusals_said(print):
        return traps.main(argv)


def replay_cmd(argv):
    from . import replay
    return replay.main(argv)


def watch_cmd(argv):
    from . import watch
    return watch.main(argv)


def redteam_cmd(argv):
    from . import redteam
    return redteam.main(argv)


def _say(text):
    try:
        print(text)
        sys.stdout.flush()
    except (OSError, ValueError):
        pass        # no terminal left to say it to (a hang-up, a closed pipe): the receipts keep the record


_display = paths.shown      # every printed path: from the folder the command was run in


def smoke_cmd(argv):
    from . import smoke                   # live adapters load only when asked for
    return smoke.main(argv)


COMMANDS = {"demo": demo, "run": run_cmd, "traps": traps_cmd, "verify": verify, "stop": stop, "resume": resume,
            "smoke": smoke_cmd, "replay": replay_cmd, "watch": watch_cmd, "redteam": redteam_cmd, WORKER: worker}


def _help():
    """The command list, with the interpreter spelled as it runs here (paths.command)."""
    return __doc__.strip().replace("python3 -m doris", paths.command())


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(_help())
        return 0 if argv else 2
    command = COMMANDS.get(argv[0])
    if command is None:
        print("Unknown command %r.\n\n%s" % (argv[0], _help()))
        return 2
    return command(argv[1:])


if __name__ == "__main__":
    sys.exit(main())
