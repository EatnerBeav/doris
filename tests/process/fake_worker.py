"""A stand-in for `python -m doris _worker`, plus contract-faithful fakes of receipts.py and
ledger.py, for the kill switch's process tests.

Run as a script it is the worker (or a second supervisor, or a bare lock holder). Like the real
worker, it takes the ledger lock for its own life and exits 3 when another holds it, and it does
not outlive the supervisor that started it (its stdin is its lifeline):

    fake_worker.py work --scenario midcall|quick|budget|torn|g1|lostrace --receipts P --ledger P --run NAME
                        --mode MODE --state DIR --repo DIR --life P --marker P [--backend fake|real]
    fake_worker.py supervise <same options>     # a whole second Doris: run_supervised in this process
    fake_worker.py hold-lock --lock P --marker P # takes the ledger lock, says so, sleeps
    fake_worker.py doris-run --sandbox DIR --state DIR --cycle-s S --marker P
                                                 # a real Doris: `python -m doris run` on a sandbox copy,
                                                 # its real worker's pid written to --marker

Imported, it gives the tests `FakeReceipts` (ReceiptWriter, read_lines, verify) and `JsonLedger`,
written to CONTRACTS.md so the tests never depend on a half-built receipts.py or ledger.py.
"""
import argparse
import hashlib
import json
import os
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(os.path.dirname(HERE)), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

THIS_SCRIPT = os.path.abspath(__file__)
SLOW_CALL_S = 60.0            # the "slowed calendar": far longer than any test waits


# ------------------------------------------------------------------------------------------
# receipts.py, as CONTRACTS.md writes it
# ------------------------------------------------------------------------------------------
def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _ts():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _split_whole(data):
    """(whole lines as bytes, torn tail bytes). A last line with no newline or bad JSON is torn."""
    if not data:
        return b"", b""
    cut = data.rfind(b"\n") + 1
    whole, tail = data[:cut], data[cut:]
    if not tail and whole:
        last_start = whole.rfind(b"\n", 0, len(whole) - 1) + 1
        try:
            json.loads(whole[last_start:-1].decode("ascii"))
        except ValueError:
            whole, tail = whole[:last_start], whole[last_start:]
    return whole, tail


class ChainWriter:
    def __init__(self, path, run_id, mode, clock=None):
        self.path = path
        self.run_id = run_id
        self.mode = mode
        self.seq = 0
        self.last_hash = "genesis"
        folder = os.path.dirname(path)
        os.makedirs(folder, exist_ok=True)
        data = b""
        if os.path.exists(path):
            with open(path, "rb") as fh:
                data = fh.read()
        whole, tail = _split_whole(data)
        if tail:                                   # move the torn tail aside, never edit in place
            n = 1
            while os.path.exists("%s.torn-%d" % (path, n)):
                n += 1
            with open("%s.torn-%d" % (path, n), "wb") as fh:
                fh.write(tail)
            fd, tmp = tempfile.mkstemp(dir=folder)
            with os.fdopen(fd, "wb") as fh:
                fh.write(whole)
            os.replace(tmp, path)
        lines = [json.loads(raw.decode("ascii")) for raw in whole.split(b"\n") if raw]
        if lines:
            self.seq, self.last_hash = lines[-1]["seq"], lines[-1]["hash"]

    def write(self, kind, payload, subject="", why=None):
        if kind in ("decided", "checked", "held", "halted") and not (isinstance(why, dict) and why.get("code")):
            raise ValueError("a %s line needs a why" % kind)
        line = {"ts": _ts(), "run_id": self.run_id, "mode": self.mode, "seq": self.seq + 1, "kind": kind,
                "subject": subject, "payload": payload, "prev": self.last_hash}
        if why is not None:
            line["why"] = why
        line["hash"] = hashlib.sha256(self.last_hash.encode("ascii") + canonical(line)).hexdigest()
        with open(self.path, "ab") as fh:
            fh.write(canonical(line) + b"\n")
            fh.flush()
            os.fsync(fh.fileno())
        self.seq, self.last_hash = line["seq"], line["hash"]
        return line


def read_lines(path):
    with open(path, "rb") as fh:
        whole, _tail = _split_whole(fh.read())
    return [json.loads(raw.decode("ascii")) for raw in whole.split(b"\n") if raw]


class VerifyResult(tuple):
    __slots__ = ()

    def __new__(cls, ok, lines, first_bad_seq, reason):
        return tuple.__new__(cls, (ok, lines, first_bad_seq, reason))

    ok = property(lambda self: self[0])
    lines = property(lambda self: self[1])
    first_bad_seq = property(lambda self: self[2])
    reason = property(lambda self: self[3])


def verify(path):
    with open(path, "rb") as fh:
        data = fh.read()
    prev, count = "genesis", 0
    for raw in data.split(b"\n"):
        if not raw:
            continue
        line = json.loads(raw.decode("ascii"))
        count += 1
        got = line.pop("hash", None)
        if line.get("prev") != prev or line.get("seq") != count:
            return VerifyResult(False, count, count, "broken link")
        if hashlib.sha256(prev.encode("ascii") + canonical(line)).hexdigest() != got:
            return VerifyResult(False, count, count, "hash mismatch")
        prev = got
    return VerifyResult(True, count, None, "")


class FakeReceipts:
    """Module-shaped: what run_supervised(receipts_module=...) needs."""
    ReceiptWriter = ChainWriter
    read_lines = staticmethod(read_lines)
    verify = staticmethod(verify)


# ------------------------------------------------------------------------------------------
# ledger.py, the part the kill switch touches, as a JSON file
# ------------------------------------------------------------------------------------------
class JsonLedger:
    def __init__(self, path):
        self.path = path
        if os.path.exists(path) and os.path.getsize(path):
            with open(path, "rb") as fh:
                self.db = json.loads(fh.read().decode("utf-8"))
        else:
            self.db = {"actions": [], "messages": {}, "bookings": {}, "sends": {}}

    def _save(self):
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path) or ".")
        with os.fdopen(fd, "wb") as fh:
            fh.write(json.dumps(self.db, sort_keys=True).encode("utf-8"))
        os.replace(tmp, self.path)

    def close(self):
        pass

    def record_fetch(self, server_id, message_key, run_id, now):
        self.db["messages"].setdefault(message_key, {})["run_id"] = run_id
        self._save()

    def record_message(self, key, thread_key, sender, received_utc, status):
        self.db["messages"].setdefault(key, {}).update({"key": key, "thread_key": thread_key, "status": status})
        self._save()

    def set_message_status(self, key, status, outcome=None):
        self.db["messages"][key]["status"] = status
        self._save()

    def thread_of_message(self, key):
        return self.db["messages"].get(key, {}).get("thread_key")

    def unprocessed_messages(self, run_id=None):
        return [dict(m) for m in self.db["messages"].values()
                if m.get("status") in ("fetched", "processing") and (run_id is None or m.get("run_id") == run_id)]

    def enqueue_action(self, run_id, message_key, kind, detail_dict):
        action_id = len(self.db["actions"]) + 1
        self.db["actions"].append({"id": action_id, "run_id": run_id, "message_key": message_key, "kind": kind,
                                   "detail": json.dumps(detail_dict, sort_keys=True), "state": "queued"})
        self._save()
        return action_id

    def start_action(self, action_id):
        self.db["actions"][action_id - 1]["state"] = "started"
        self._save()

    def finish_action(self, action_id, outcome):
        self.db["actions"][action_id - 1]["state"] = "finished"
        self._save()

    def queued_actions(self, run_id=None):
        return [dict(a) for a in self.db["actions"] if a["state"] in ("queued", "started")
                and (run_id is None or a["run_id"] == run_id)]

    def drop_queued(self, cause):
        out = []
        for action in self.db["actions"]:
            if action["state"] in ("queued", "started"):
                action["state"], action["drop_cause"] = "dropped", cause
                out.append(dict(action))
        self._save()
        return out

    def record_booking(self, booking_id, message_key, thread_key, sender, slot, now, run_id=None, *, direct):
        self.db["bookings"][booking_id] = {"state": "intent", "thread_key": thread_key, "direct": direct}
        self._save()

    def set_booking_state(self, booking_id, state):
        if booking_id not in self.db["bookings"]:
            raise KeyError(booking_id)
        self.db["bookings"][booking_id]["state"] = state
        self._save()

    def booking(self, booking_id):
        return self.db["bookings"].get(booking_id)

    def record_send(self, message_id, kind, template, thread_key, to_addr, body_sha256, now, run_id):
        self.db["sends"][message_id] = {"state": "pending", "template": template}
        self._save()

    def set_send_state(self, message_id, state):
        if message_id not in self.db["sends"]:
            raise KeyError(message_id)
        self.db["sends"][message_id]["state"] = state
        self._save()


# ------------------------------------------------------------------------------------------
# The worker
# ------------------------------------------------------------------------------------------
def _touch(path):
    with open(path, "wb") as fh:
        fh.write(b"%d\n" % os.getpid())


def _modules(backend):
    if backend == "real":
        from doris import receipts
        from doris.ledger import Ledger
        return receipts.ReceiptWriter, Ledger
    return ChainWriter, JsonLedger


def _patch_paths(args):
    from doris import paths
    paths.STATE = args.state
    paths.REPO = args.repo


def _die_with_the_supervisor():
    """The real worker's lifeline, short: when the supervisor that started it is gone (killed by a
    test, its stdin pipe closed), this fake worker exits at once instead of sleeping on alone."""
    from doris import supervisor
    if os.environ.get(supervisor.LIFELINE_ENV) != supervisor.LIFELINE_STDIN:
        return

    def watch():
        try:
            while os.read(0, 1):
                pass
        except OSError:
            pass
        os._exit(1)
    threading.Thread(target=watch, name="fake-lifeline", daemon=True).start()


def work(args):
    from doris import supervisor
    _die_with_the_supervisor()
    _patch_paths(args)
    life = supervisor.acquire_lock(args.life)       # the OS lets go of it the instant this process dies
    if life is None:
        raise SystemExit("life lock already held")
    if args.scenario == "lostrace":                 # as if another worker took the ledger first
        print(supervisor.LOCK_REFUSAL, flush=True)
        return supervisor.WORKER_EXIT_LOCKED
    ledger_lock = supervisor.acquire_lock(supervisor.lock_path_for(args.ledger))   # held for its life
    if ledger_lock is None:
        print(supervisor.LOCK_REFUSAL, flush=True)
        return supervisor.WORKER_EXIT_LOCKED
    writer_cls, ledger_cls = _modules(args.backend)
    w = writer_cls(args.receipts, args.run, args.mode)
    w.write("observed", {"cycle": 1, "fetched": 3}, subject="cycle")
    model = w.write("called", {"call": "model", "target": "model-stub"})
    w.write("result", {"call_seq": model["seq"], "ok": True})

    if args.scenario == "quick":
        _touch(args.marker)
        return 0

    if args.scenario == "budget":
        supervisor.request_stop(args.mode, "budget_cap")    # the worker writes HALT, then exits
        _touch(args.marker)
        return supervisor.WORKER_EXIT_HALTED

    if args.scenario == "g1":                               # a STOP file lands; the gate (G1) sees it first
        with open(os.path.join(args.repo, "STOP"), "w") as fh:
            fh.write("stop\n")
        _touch(args.marker)
        return supervisor.WORKER_EXIT_HALTED

    import datetime as dt
    from doris.shapes import Slot
    led = ledger_cls(args.ledger)
    now = dt.datetime(2026, 11, 27, 19, 0, tzinfo=dt.timezone.utc)
    slot = Slot(dt.datetime(2026, 12, 2, 19, 0, tzinfo=dt.timezone.utc),
                dt.datetime(2026, 12, 2, 19, 30, tzinfo=dt.timezone.utc))
    for key, thread in (("m1@a.example", "t1@a.example"), ("m2@b.example", "t2@b.example"),
                        ("m3@c.example", "t3@c.example")):
        led.record_fetch(key + "-srv", key, args.run, now)
        led.record_message(key, thread, "sender@" + key.split("@")[1], now, "fetched")
    led.set_message_status("m1@a.example", "processing")
    led.set_message_status("m2@b.example", "processing")
    led.enqueue_action(args.run, "m1@a.example", "send_reply", {"template": "T4", "thread_key": "t1@a.example"})
    booking = led.enqueue_action(args.run, "m2@b.example", "calendar_create",
                                 {"booking_id": "doris-b2", "thread_key": "t2@b.example"})
    led.record_booking("doris-b2", "m2@b.example", "t2@b.example", "sender@b.example", slot, now, run_id=args.run,
                       direct=True)
    led.record_send("<s1@fastmail.com>", "send_reply", "T2", "t1@a.example", "sender@a.example", "0" * 64, now,
                    args.run)
    led.start_action(booking)
    led.close()

    if args.scenario == "torn":
        w.write("called", {"call": "send", "target": "T2", "message_id": "<s1@fastmail.com>"})
        with open(args.receipts, "ab") as fh:          # killed half way through the next line
            fh.write(b'{"kind":"result","payload":{"call_seq":')
            fh.flush()
    else:   # midcall: the slowed calendar write that the stop lands on
        w.write("called", {"call": "calendar_create", "target": "doris-b2"})
    _touch(args.marker)
    time.sleep(SLOW_CALL_S)
    w.write("result", {"call_seq": w.seq, "ok": True})    # never reached when the kill lands
    return 0


def supervise(args):
    from doris import supervisor
    _patch_paths(args)
    argv = [sys.executable, THIS_SCRIPT, "work"] + _common(args)
    receipts_module = None if args.backend == "real" else FakeReceipts
    open_ledger = None if args.backend == "real" else JsonLedger
    return supervisor.run_supervised(args.mode, args.run, argv, args.receipts, args.ledger,
                                     receipts_module=receipts_module, open_ledger=open_ledger)


def hold_lock(args):
    from doris import supervisor
    held = supervisor.acquire_lock(args.lock)
    if held is None:
        return 9
    _touch(args.marker)
    time.sleep(SLOW_CALL_S)
    return 0


def doris_run(args):
    """`python -m doris run` on a sandbox copy and a state folder of the test's, its cycle --cycle-s
    seconds: the real supervisor over the real worker. The worker's pid goes to --marker as it starts,
    so a test can find the worker its supervisor leaves behind."""
    from doris import __main__ as cli
    from doris import paths, supervisor
    paths.SANDBOX, paths.STATE = args.sandbox, args.state
    cli.RUN_CYCLE_S = float(args.cycle_s)
    spawn = supervisor._spawn

    def spawn_and_note(argv, cwd):
        child = spawn(argv, cwd)
        with open(args.marker, "w") as fh:
            fh.write("%d\n" % child.pid)
        return child
    supervisor._spawn = spawn_and_note
    return cli.main(["run"])


def _common(args):
    return ["--scenario", args.scenario, "--receipts", args.receipts, "--ledger", args.ledger,
            "--run", args.run, "--mode", args.mode, "--state", args.state, "--repo", args.repo,
            "--life", args.life, "--marker", args.marker, "--backend", args.backend]


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("work", "supervise", "hold-lock", "doris-run"))
    for name in ("receipts", "ledger", "run", "state", "repo", "life", "marker", "lock", "sandbox", "cycle-s"):
        parser.add_argument("--" + name)
    parser.add_argument("--mode", default="sandbox")
    parser.add_argument("--scenario", default="midcall", choices=("midcall", "quick", "budget", "torn", "g1", "lostrace"))
    parser.add_argument("--backend", default="fake", choices=("fake", "real"))
    args = parser.parse_args(argv)
    return {"work": work, "supervise": supervise, "hold-lock": hold_lock, "doris-run": doris_run}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
