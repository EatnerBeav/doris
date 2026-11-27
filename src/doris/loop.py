"""The loop: read -> classify -> decide -> check -> act -> record, one message at a time.

`run(cfg)` drains the inbox once and returns a RunSummary. Each cycle, in the order of
calendar-rulings-spec ("The order inside one cycle"):

  1. fetch new mail;
  2. screens 1, 2 and 4 over the whole batch, so a stop lands before anything else happens;
  3. the rest, oldest first: screens 5 to 11; when several wait on one thread only the newest
     goes on (the others are superseded); then the model (classify), the parser, the policy
     table (decide), the gate (check), her adapters (act) and a read-back (verify).

Every message writes `observed` first and ends on exactly one closing line (`decided`, or
`held` for a hold) whose payload names the outcome. Every call writes a `called` line before
it starts and a `result` naming it after. Every decided, checked, held and halted line carries
a why from why.py. Queued means written down: a batch is in the ledger before any of it is
acted on, so a kill can name what it dropped.

Times are aware UTC. The sandbox clock starts at clock.txt. In "advance" mode (the demo) it
steps to her next ten-minute cycle after each arrival ("her 3:10 PM cycle"); in "fixed" mode
(traps) it stays put. Live runs read the wall clock.
"""

import datetime as _dt
import hashlib
import os
import time
import types
import urllib.parse
from dataclasses import dataclass
from typing import Callable, List, Optional

from . import caps, compose, digest, gate, holds, ics, mail, netguard, parser, paths, policy, receipts
from . import screens, slots, supervisor, templates, zones
from . import ledger as ledger_mod
from . import model as model_mod
from . import why as why_mod
from .shapes import UTC, Action, AlreadyExists, CalendarError, CalendarTimeout, MailError, SendUnknown, Slot

CYCLE = _dt.timedelta(minutes=10)
DIGEST_AT = _dt.time(17, 0)            # Pacific: the demo's digest goes at 5:00 PM (or after the last cycle)
SANDBOX_OWNER = "whizz@fastmail.com"   # the sandbox's owner and Doris addresses (sandbox/README.md)
SANDBOX_DORIS = "doris.htm@fastmail.com"
WINDOW = caps.MAX_HORIZON + _dt.timedelta(days=1)    # the span an offer reads
ENV_NAMES = ("ANTHROPIC_API_KEY", "DORIS_MODEL", "DORIS_MEETING_LINK")   # names only ever reach a receipt

CALENDAR_FAULTS = frozenset({"timeout-once", "timeout-twice", "read-fails", "slow-calendar", "lands-between",
                             "write-unknown-present", "write-unknown-absent", "lookup-fails",
                             "remove-fails-once", "remove-fails-always"})
MAIL_FAULTS = frozenset({"sent-unconfirmed"})
NEW_OFFER_TEMPLATES = frozenset({"T2", "T3", "T4", "T13", "T18"})   # each one is an offer record
REPLY_TEMPLATES = ("T1", "T2", "T3", "T4", "T10", "T12", "T13", "T18")   # her one reply to a message
# Her own booking for a message that a kill or a stop left part way: picked up again, never made again.
RESUMABLE_BOOKING_STATES = ("intent", "unconfirmed", "written", "confirmed", "collision_unresolved", "removed")
NOTICE_WITHOUT_TIMES = frozenset({"hold.owner_mail", "hold.calendar_down", "hold.unknown_result", "hold.no_slot",
                                  "hold.remove_failed", "hold.internal_error"})


# ------------------------------------------------------------------------------------------
# Config and summary
# ------------------------------------------------------------------------------------------
@dataclass
class RunConfig:
    mode: str                               # sandbox | live | throwaway
    run_name: str                           # starts with the model source, then the mode (A6)
    state_dir: str                          # holds ledger.sqlite, ledger.lock, HALT and runs/
    sandbox_root: str                       # holds inbox/, calendar/, outbox/ and clock.txt
    owner_addr: str
    doris_addr: str
    model_source: str = "stub"              # live | replayed | stub | yes-man
    model_id: Optional[str] = None
    answers_path: Optional[str] = None      # recorded answers, for replayed
    record_path: Optional[str] = None       # live: where answers are recorded
    clock_mode: str = "fixed"               # fixed: stays at clock.txt; advance: steps cycle by cycle
    budget_usd: float = caps.BUDGET_USD_PER_RUN
    digest: bool = False                    # send S2 when the run ends (the demo does)
    supervised: bool = False                # the worker under the kill switch: a halt exits, the parent records it
    retry_wait_s: Optional[float] = None    # None: caps.CALENDAR_RETRY_WAIT_S
    calendar_timeout_s: Optional[float] = None   # sandbox calendar's wait: None the adapter's own; demo caps'
    say: Optional[Callable[[str], None]] = None   # one plain line per message, for the terminal


@dataclass
class RunSummary:
    run_id: str
    run_dir: str
    receipts_path: str
    handled: int = 0
    held: int = 0
    errors: int = 0
    quarantined: int = 0
    human_approvals: int = 0
    sends: int = 0
    hold_notices: int = 0
    bookings: int = 0
    model_calls: int = 0
    cost_usd: float = 0.0
    verify_ok: bool = False
    halted: Optional[str] = None
    read_failed: bool = False               # the inbox could not be read: the run ended there, never "clean"


class Refused(Exception):
    """The run does not start: another Doris holds the ledger, or a halt is in force."""

    def __init__(self, text, exit_code):
        Exception.__init__(self, text)
        self.exit_code = exit_code


class Halted(Exception):
    """A halt was found mid-run (G1, or the budget cap). The worker exits; the parent writes the line."""

    def __init__(self, cause):
        Exception.__init__(self, cause)
        self.cause = cause


class _CalendarDown(Exception):
    """A calendar call failed after its one retry. Carries the hold code it becomes."""

    def __init__(self, code="hold.calendar_down"):
        Exception.__init__(self, code)
        self.code = code


# ------------------------------------------------------------------------------------------
# Pure pieces
# ------------------------------------------------------------------------------------------
def next_cycle(t):
    """The first point on the ten-minute grid at or after t: mail waits for her next cycle."""
    base = t.replace(minute=0, second=0, microsecond=0)
    steps = -(-(t - base) // CYCLE)
    return base + steps * CYCLE


def booking_id(message_key, slot):
    """Ruling 8: from the inbound Message-ID and the slot, so a restart writes the same ID again."""
    seed = "%s|%s|%s" % (message_key, receipts.iso(slot.start_utc), receipts.iso(slot.end_utc))
    return ics.BOOKING_PREFIX + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


def count_outcomes(lines):
    """handled + held = every message that reached its closing line; approvals are counted, never assumed."""
    handled = held = approvals = 0
    for line in lines:
        p = line.get("payload") or {}
        if line.get("kind") == "approved" or p.get("human_approval") is True:
            approvals += 1
        if line.get("kind") in ("decided", "held") and "outcome" in p:
            if str(p["outcome"]).startswith("hold:"):
                held += 1
            else:
                handled += 1
    return {"handled": handled, "held": held, "human_approvals": approvals}


def read_clock(root):
    """The sandbox's start instant (clock.txt, ISO UTC)."""
    with open(os.path.join(root, "clock.txt"), "rb") as fh:
        text = fh.read().decode("ascii").strip()
    t = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    if t.tzinfo is None:
        raise ValueError("clock.txt must name a UTC instant")
    return t.astimezone(UTC)


def _status_for(outcome):
    if outcome == "quarantine":
        return "quarantined"
    if outcome.startswith("hold:"):
        return "held"
    if outcome == "superseded":
        return "superseded"
    if outcome in ("dropped", "skip"):
        return "dropped"
    return "done"


def _event_line(ev):
    """A calendar read as a receipt keeps it: times, status, transparency and her own booking ID only."""
    out = {"start": ev.start_utc, "end": ev.end_utc, "status": ev.status, "transp": ev.transp, "kind": ev.kind}
    if ev.kind == "all_day":
        out["start_date"], out["end_date"] = ev.start_date, ev.end_date
    if ev.uid.startswith(ics.BOOKING_PREFIX):
        out["booking_id"] = ev.uid
    return out


def _span(slot):
    return {"start": slot.start_utc, "end": slot.end_utc}


# ------------------------------------------------------------------------------------------
# One fetched message on its way through
# ------------------------------------------------------------------------------------------
class _Entry:
    def __init__(self, item, msg, results):
        self.item = item
        self.msg = msg
        self.results = list(results)
        self.raw_sha256 = hashlib.sha256(bytes(item.raw)).hexdigest()
        self.key = msg.key if msg is not None else mail.raw_key(item.raw)   # its Message-ID even when unreadable
        self.facts = dict(getattr(item, "facts", None) or {})
        self.thread = self.key
        self.redo = False            # fetched in an earlier run and never finished: read again
        self.in_ledger = False       # has a message row (a duplicate and sensitive mail have none)
        self.retried = False
        self.recovered = False
        self.injection_seen = False
        self.cost = None
        self.action_id = None
        self.closed = False          # its closing line is written: one per message, never two

    def settled_by(self):
        for r in self.results:
            if r.settled:
                return r
        return None

    def facts_of(self, screen):
        for r in self.results:
            if r.screen == screen:
                return r.facts or {}
        return {}

    @property
    def sensitive(self):
        r = self.settled_by()
        return r is not None and r.screen == 5

    @property
    def subject(self):
        """The receipt subject: the message key; sensitive mail is named by a hash of it only."""
        if self.sensitive:
            return "sensitive:" + ledger_mod.key_sha256(self.key)
        return self.key


# ------------------------------------------------------------------------------------------
# The run
# ------------------------------------------------------------------------------------------
class _Run:
    def __init__(self, cfg):
        if cfg.mode not in paths.MODES:
            raise ValueError("unknown mode %r" % (cfg.mode,))
        if cfg.clock_mode not in ("fixed", "advance"):
            raise ValueError("clock_mode is fixed or advance, not %r" % (cfg.clock_mode,))
        if cfg.model_source not in model_mod.SOURCES:
            raise ValueError("unknown model source %r" % (cfg.model_source,))
        self.cfg = cfg
        self.mode = cfg.mode
        self.run_id = cfg.run_name
        self.run_dir = os.path.join(cfg.state_dir, "runs", cfg.run_name)
        self.receipts_path = os.path.join(self.run_dir, "receipts.jsonl")
        self.summary = RunSummary(self.run_id, self.run_dir, self.receipts_path)
        self.say = cfg.say or (lambda text: None)
        self.wait_s = caps.CALENDAR_RETRY_WAIT_S if cfg.retry_wait_s is None else cfg.retry_wait_s
        self.lock = self.ledger = self.receipts = self.guard = None
        self.mail = self.calendar = self.model = self.env = None
        self.model_target = cfg.model_source
        self.t = None
        self.model_down = False
        self.clean_readbacks = set()
        self.redo = set()
        self.batch_keys = set()

    # --- opening and closing ---------------------------------------------------------------
    def open(self):
        cfg = self.cfg
        os.makedirs(cfg.state_dir, exist_ok=True)
        self.lock = supervisor.acquire_lock(os.path.join(cfg.state_dir, supervisor.LOCK_NAME))
        if self.lock is None:
            raise Refused(supervisor.LOCK_REFUSAL, supervisor.WORKER_EXIT_LOCKED)
        try:
            cause = self.halt_cause()
        except supervisor.HaltFileError:
            cause = "unreadable"
        if cause is not None:
            raise Refused("Halted (%s). Only `python -m doris resume` clears it." % (cause,),
                          supervisor.WORKER_EXIT_HALTED)
        os.makedirs(os.path.join(self.run_dir, "messages"), exist_ok=True)
        self.t = None if cfg.mode == "live" else read_clock(cfg.sandbox_root)
        self.guard = netguard.install(self.allowed_hosts())
        self.ledger = ledger_mod.Ledger(os.path.join(cfg.state_dir, "ledger.sqlite"))
        self.receipts = receipts.ReceiptWriter(self.receipts_path, self.run_id, cfg.mode, clock=self.now)
        self.mail, self.calendar = self.adapters()
        self.model = model_mod.Model(cfg.model_source, model_id=cfg.model_id, answers_path=cfg.answers_path,
                                     record_path=cfg.record_path)
        if cfg.model_source in ("live", "replayed"):
            self.model_target = self.model.model_id
        led = self.ledger
        self.env = screens.ScreenEnv(
            owner_addr=cfg.owner_addr, doris_addr=cfg.doris_addr,
            is_seen=lambda key: led.message_seen(key) and ledger_mod.norm_id(key) not in self.redo,
            is_her_message_id=led.is_her_message_id, is_silenced=led.is_silenced,
            addresses_written_on_thread=led.addresses_written_on_thread, thread_of=led.thread_of_sent,
            queued_count=lambda addr: len([m for m in led.unprocessed_messages(sender=addr)
                                           if ledger_mod.norm_id(m["key"]) not in self.batch_keys]))

    def close_handles(self):
        if self.guard is not None:
            self.guard.uninstall()
            self.guard = None
        if self.ledger is not None:
            self.ledger.close()
            self.ledger = None
        if self.lock is not None:
            self.lock.release()
            self.lock = None

    def allowed_hosts(self):
        hosts = []
        if self.cfg.model_source == "live":
            hosts.append("api.anthropic.com")
            proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
            if proxy:
                hosts.append(urllib.parse.urlsplit(proxy).hostname)
        return tuple(h for h in hosts if h)

    def adapters(self):
        if self.cfg.mode == "live":
            raise Refused("The live adapters (IMAP, SMTP, CalDAV) are not built yet.", 2)
        from .adapters import sandbox
        clock = self.now if self.cfg.clock_mode == "advance" else None
        cal = {} if self.cfg.calendar_timeout_s is None else {"timeout_s": self.cfg.calendar_timeout_s}
        return (sandbox.SandboxMail(self.cfg.sandbox_root, self.cfg.doris_addr, clock=clock),
                sandbox.SandboxCalendar(self.cfg.sandbox_root, **cal))

    # --- small helpers ---------------------------------------------------------------------
    def now(self):
        if self.t is None:
            return _dt.datetime.now(UTC).replace(microsecond=0)
        return self.t

    def halt_cause(self):
        return supervisor.halt_cause(self.mode, state_dir=self.cfg.state_dir)

    def check_halt(self):
        try:
            cause = self.halt_cause()
        except supervisor.HaltFileError:
            cause = "unreadable"
        if cause is not None:
            raise Halted(cause)

    def link(self):
        return templates.meeting_link()     # the one the gate checks (G13): the env, else the placeholder

    def pause(self):
        if self.wait_s > 0:
            time.sleep(self.wait_s)

    def write(self, kind, payload, subject="", why=None):
        return self.receipts.write(kind, payload, subject=subject, why=why)

    def called(self, call, target, subject="", **detail):
        return self.write("called", dict(detail, call=call, target=target), subject=subject)["seq"]

    def result(self, seq, subject="", **detail):
        self.write("result", dict(detail, call_seq=seq), subject=subject)

    def error(self, e, step, exc, **detail):
        self.write("error", dict(detail, step=step, error="%s: %s" % (type(exc).__name__, str(exc)[:200])),
                   subject=e.subject if e is not None else "")

    # --- start, reconcile, drain, finish -------------------------------------------------------
    def start(self):
        cfg = self.cfg
        interrupted = [r["run_id"] for r in self.ledger.unended_runs() if r["run_id"] != self.run_id]
        self.ledger.start_run(self.run_id, self.mode, self.now())
        self.write("observed", {
            "step": "start", "mode": cfg.mode, "model_source": cfg.model_source, "model": self.model_target,
            "run_name": cfg.run_name, "clock": self.now(), "clock_mode": cfg.clock_mode,
            "env_set": sorted(n for n in ENV_NAMES if os.environ.get(n)), "interrupted_runs": interrupted,
            "simulated": cfg.mode != "live"})
        self.reconcile()

    def reconcile(self):
        """Ruling 8 and A3: before anything new, every unfinished send is looked up in Sent (never
        sent again blind), every unfinished booking is looked up on the calendar, and every message
        fetched but never finished is read again through the whole loop."""
        for s in self.ledger.unfinished_sends():
            found = self.look_in_sent(None, s["message_id"], s["template"] or "-")
            self.ledger.set_send_state(s["message_id"], "confirmed" if found else "unknown")
        for b in self.ledger.unfinished_bookings():
            try:
                ev = self.get_event(None, b["booking_id"])
            except _CalendarDown:
                continue
            if ev is not None and b["state"] in ("intent", "unconfirmed"):
                self.ledger.set_booking_state(b["booking_id"], "written")
        self.redo = set(ledger_mod.norm_id(m["key"]) for m in self.ledger.unprocessed_messages())

    def drain(self):
        last = None
        while True:
            self.check_halt()
            if self.cfg.clock_mode == "advance":
                nxt = self.mail.next_arrival()
                if nxt is None:
                    return
                self.t = max(self.t, next_cycle(nxt))
            items = self.fetch()
            fresh = self.batch(items)
            if self.cfg.clock_mode != "advance" and not fresh:
                return                      # nothing new: a fetch can hand over old mail again
            if self.cfg.clock_mode == "advance" and not items and last == self.t:
                return                      # the adapter named an arrival it then did not hand over
            last = self.t

    def finish(self):
        s = self.summary
        lines = receipts.read_lines(self.receipts_path)
        c = count_outcomes(lines)
        s.handled, s.held, s.human_approvals = c["handled"], c["held"], c["human_approvals"]
        s.errors = sum(1 for line in lines if line.get("kind") == "error")
        s.quarantined = sum(1 for line in lines if line.get("kind") == "decided"
                            and (line.get("payload") or {}).get("outcome") == "quarantine")
        self.write("summary", {
            "handled": s.handled, "held": s.held, "human_approvals": s.human_approvals, "errors": s.errors,
            "quarantined": s.quarantined, "sends": s.sends, "hold_notices": s.hold_notices,
            "bookings": s.bookings, "model_calls": s.model_calls, "cost_usd": round(s.cost_usd, 6),
            "halted": s.halted, "read_failed": s.read_failed})
        self.ledger.end_run(self.run_id, self.now())
        s.verify_ok = receipts.verify(self.receipts_path, finished=True).ok

    def write_halted(self, cause):
        """Unsupervised runs (traps, tests) write their own halted line; under the kill switch the parent does."""
        lines = receipts.read_lines(self.receipts_path)
        flight = receipts.in_flight(lines)
        dropped = []
        for a in self.ledger.drop_queued(cause):
            dropped.append({"kind": a["kind"], "message": a["message_key"], "thread": a["thread_key"],
                            "template": a["detail"].get("template"), "booking_id": a["detail"].get("booking_id")})
        for m in self.ledger.unprocessed_messages(run_id=self.run_id):
            # left unprocessed, as the kill switch leaves them: after resume each is read again
            # through the whole loop, and no dropped action runs as it was recorded
            dropped.append({"kind": "message", "message": m["key"], "thread": m["thread_key"],
                            "template": None, "booking_id": None})
        why_code = "halt." + cause if ("halt." + cause) in why_mod.CODES else "halt.unreadable"
        self.write("halted", {"cause": cause, "in_flight": flight or "none", "dropped": dropped,
                              "summary": "follows"},       # cut the summary and verify fails
                   why=why_mod.why(why_code))

    # --- read ---------------------------------------------------------------------------------
    def fetch(self):
        seq = self.called("mail_fetch", target="inbox")
        try:
            items = list(self.mail.fetch_new())
        except MailError as exc:
            self.result(seq, ok=False, error=type(exc).__name__)
            raise                          # a refused login is loud: never read as an empty inbox
        self.result(seq, ok=True, count=len(items))
        return items

    def is_new(self, item):
        if not self.ledger.server_id_seen(item.server_id):
            return True
        if not self.redo:
            return False
        msg, _ = screens.decode(item)
        key = msg.key if msg is not None else mail.raw_key(item.raw)
        return ledger_mod.norm_id(key) in self.redo

    def thread_step(self, msg, by_key, seen):
        """One link of a message's thread: the thread of her message it answers, else of an inbound
        message it answers in the ledger, else of one earlier in this batch (by_key, never one already
        on this walk: `seen`), else its own Message-ID (it starts one). What it claims never counts.
        Returns (thread, None) when this message settles it, or (None, other) when it shares the
        thread of `other`, a message in this same batch."""
        named = list(msg.in_reply_to) + list(reversed(msg.references))
        for mid in named:
            t = self.ledger.thread_of_sent(mid)
            if t:
                return t, None
        for mid in named:
            t = self.ledger.thread_of_message(mid)
            if t:
                return t, None
            other = by_key.get(ledger_mod.norm_id(mid))
            if other is not None and ledger_mod.norm_id(other.key) not in seen:
                return None, other
        return msg.key, None

    def threads(self, entries):
        """Every message's thread, before any of the batch is written down. A reply to a message in
        this same batch joins that message's thread, so of the two only the newer goes on (Fable #4).
        Walked one link at a time, never by recursion, so no chain of forged replies can run her out
        of stack (Fable re-review D)."""
        by_key = {}
        for e in entries:
            if e.msg is not None:
                by_key.setdefault(ledger_mod.norm_id(e.key), e)
        done = {}
        for e in entries:
            if e.msg is None:
                continue
            walked, seen, cur, thread = [], set(), e, None
            while thread is None:
                k = ledger_mod.norm_id(cur.key)
                if k in done:
                    thread = done[k]
                    break
                walked.append(k)
                seen.add(k)
                thread, cur = self.thread_step(cur.msg, by_key, seen)
            for k in walked:
                done[k] = thread
            e.thread = done[ledger_mod.norm_id(e.key)]

    def batch(self, items):
        fresh = [it for it in items if self.is_new(it)]
        if not fresh:
            return 0
        entries = [_Entry(s.item, s.message, s.results) for s in screens.run_batch(fresh, self.env)]
        for e in entries:
            if e.settled_by() is None:
                e.results.extend(screens.run_message(e.msg, self.env))
            e.redo = ledger_mod.norm_id(e.key) in self.redo
        self.threads(entries)
        self.batch_keys = set(ledger_mod.norm_id(e.key) for e in entries)
        for e in entries:
            self.write_down(e)
        # (2) what screens 1, 2 and 4 settled goes first: a stop lands before anything else
        early = [e for e in entries if e.settled_by() is not None and e.settled_by().screen in (1, 2, 4)]
        for e in early:
            self.close_screened(e)
        # (3) ruling 17 (her bookings still on the calendar) is not in the skeleton
        # (4) the rest, oldest first; of several one sender has waiting on one thread only the newest
        # goes on (a stranger naming someone's message never takes its place: Fable re-review C)
        rest = [e for e in entries if e not in early]
        newest = {}
        for e in rest:
            if e.settled_by() is None:
                newest[(e.thread, e.msg.from_addr)] = e
        for e in rest:
            self.check_halt()
            if e.settled_by() is not None:
                self.close_screened(e)
            elif newest.get((e.thread, e.msg.from_addr)) is not e and not self.acted_on(e):
                self.observe(e)
                self.close(e, "decided", "superseded", why_mod.why("policy.superseded"),
                           superseded_by=newest[(e.thread, e.msg.from_addr)].key)
            else:
                self.handle(e)
        self.redo -= self.batch_keys
        self.batch_keys = set()
        return len(entries)

    def acted_on(self, e):
        """A message read again that already did something before a kill or a stop: a hold, a reply
        recorded, a booking written down, or a booking its cancel or move took off. It is finished
        first (ruling 8), never superseded by a newer one and its effects left half done."""
        if not e.redo:
            return False
        if self.ledger.hold_for_message(e.key) is not None:
            return True
        if any(self.ledger.send(compose.new_message_id(self.cfg.doris_addr, e.key, t, 1)) is not None
               for t in REPLY_TEMPLATES):
            return True
        own = ledger_mod.norm_id(e.key)
        booked = self.ledger.bookings_for_thread(e.thread)
        return (any(ledger_mod.norm_id(b["message_key"]) == own for b in booked)
                or self.unbooked_here(e, booked) is not None)

    def write_down(self, e):
        """Queued means written down (A3): the whole batch is in the ledger before any of it runs."""
        now = self.now()
        r = e.settled_by()
        duplicate = r is not None and r.screen == 2 and (r.why or {}).get("code") == "screen.duplicate"
        if e.sensitive:
            self.ledger.record_sensitive_skip(e.key, e.msg.from_addr, e.raw_sha256,
                                              r.facts.get("check") or "scan_error", now)
        elif e.redo:
            e.in_ledger = True
        elif not duplicate and not self.ledger.message_seen(e.key):
            sender = e.msg.from_addr if e.msg is not None else ""
            self.ledger.record_message(e.key, e.thread, sender, e.item.received_utc, "fetched", self.run_id)
            e.in_ledger = True
        self.ledger.record_fetch(e.item.server_id, e.key, self.run_id, now)

    def observe(self, e):
        msg = e.msg
        payload = {
            "server_id": e.item.server_id, "received": e.item.received_utc, "clock": self.now(),
            "raw_sha256": e.raw_sha256, "raw_size": len(e.item.raw),
            "thread": e.subject if e.sensitive else e.thread,
            "screens": [self.screen_line(r) for r in e.results],
        }
        if e.facts:
            payload["simulated"] = dict(e.facts)          # sandbox facts: staged, labelled simulated
        if msg is not None:
            payload["from_addr"] = msg.from_addr
            if not e.sensitive:
                payload["from_name"] = msg.from_name
                payload["attachments"] = [{"name": a.name, "type": a.declared_type, "size": a.size,
                                           "inspected": False} for a in msg.attachments]
                payload["links_seen"], payload["opened"] = msg.links_seen, 0
        if not e.sensitive:
            payload["message_copy"] = self.keep_copy(e)
        self.write("observed", payload, subject=e.subject)

    @staticmethod
    def screen_line(r):
        out = {"screen": r.screen, "name": r.name, "settled": r.settled, "facts": r.facts}
        if r.settled:
            out["outcome"] = r.outcome
            out["why"] = (r.why or {}).get("code")
        return out

    def keep_copy(self, e):
        name = os.path.join("messages", e.raw_sha256 + ".eml")
        path = os.path.join(self.run_dir, name)
        if not os.path.exists(path):
            with open(path, "wb") as fh:
                fh.write(bytes(e.item.raw))
        return name.replace(os.sep, "/")

    # --- what the screens settled -------------------------------------------------------------
    def close_screened(self, e):
        r = e.settled_by()
        self.observe(e)
        if r.outcome == "hold":
            return self.hold(e, r.hold_reason, r.why)
        outcome = {"quarantine": "quarantine", "drop": "skip"}.get(r.outcome, r.outcome)
        extra = {}
        if r.screen == 4 and r.outcome == "file" and r.facts.get("stop_in_batch"):
            outcome = "dropped"
        if r.outcome == "suppress":
            self.silence(e, r.facts.get("stop_match") or "stop")
            extra["dropped"] = r.facts.get("queued_dropped", 0)
        return self.close(e, "decided", outcome, r.why, **extra)

    def silence(self, e, phrase):
        addr = e.msg.from_addr
        self.ledger.silence(addr, phrase, e.key, self.now())
        self.ledger.close_offers(e.thread, "opt_out")
        for m in self.ledger.unprocessed_messages(sender=addr):
            if ledger_mod.norm_id(m["key"]) not in self.batch_keys:
                self.ledger.set_message_status(m["key"], "dropped", "dropped")

    # --- classify, decide ---------------------------------------------------------------------
    def handle(self, e):
        self.observe(e)
        if e.in_ledger:
            self.ledger.set_message_status(e.key, "processing")
        self.arm(e)
        try:
            answer = self.classify(e)
            f = self.facts(e, answer)
            self.act(e, f, policy.decide(f))
        except (Halted, Refused):
            raise
        except _CalendarDown as down:
            if not e.closed:
                self.hold_code(e, down.code)
        except Exception as exc:            # Contract 7: a bug holds this one message, loudly; the run goes on
            self.error(e, "handle", exc)
            if not e.closed:
                self.hold_code(e, "hold.internal_error")
        finally:
            self.disarm()

    def classify(self, e):
        seq = self.called("model", target=self.model_target, subject=e.subject, source=self.cfg.model_source)
        a = self.model.classify(e.msg)
        detail = {"ok": a.ok, "source": a.source, "request_sha256": a.request_sha256, "tokens_in": a.tokens_in,
                  "tokens_out": a.tokens_out, "cost_usd": a.cost_usd}
        if not a.ok:
            detail["error"] = str(getattr(self.model, "last_error", "") or "")[:200]
        self.result(seq, subject=e.subject, **detail)
        self.summary.model_calls += 1
        self.model_down = not a.ok
        e.cost = a.cost_usd
        if a.cost_usd:
            self.ledger.add_spend(self.run_id, a.cost_usd)
            self.summary.cost_usd += a.cost_usd
        e.injection_seen = bool(a.injection_seen)
        self.write("classified", {"cls": a.cls, "date": a.date, "time24": a.time24, "zone": a.zone,
                                  "length_min": a.length_min, "pick": a.pick, "injection_seen": a.injection_seen,
                                  "side_question": a.side_question, "ok": a.ok, "source": a.source, "raw": a.raw},
                   subject=e.subject)
        if self.ledger.spend(self.run_id) >= self.cfg.budget_usd:
            supervisor.request_stop(self.mode, "budget_cap", state_dir=self.cfg.state_dir)
            raise Halted("budget_cap")
        return a

    def facts(self, e, answer):
        msg, now = e.msg, self.now()
        s8 = e.facts_of(8)
        parse = parser.parse_request(msg.own_text, msg.date_utc or e.item.received_utc, now)
        thread = self.thread_state(e, s8)
        pick = None
        if thread.open_offers:
            offered = [s for o in thread.open_offers for s in o["slots"]]
            pick = parser.match_pick(msg.own_text, offered, parse.zone, now)
        agrees, e.agree = parser.model_agrees(parse, answer) if answer.ok else (False, {"reason": "no_model"})
        own = self.own_effects(e) if e.redo else {}
        return policy.Facts(sender=msg.from_addr, branch=str(s8.get("branch", "else")),
                            others_on_thread=int(s8.get("others_on_thread", 0) or 0), thread=thread, answer=answer,
                            parse=parse, model_agrees=agrees, pick=pick, money_code=policy.money_check(msg.own_text),
                            reply_to_mismatch=any(not screens.same_address(r, msg.from_addr) for r in msg.reply_to),
                            **own)

    def own_effects(self, e):
        """What a message read again after a kill or a stop already did (ruling 8, A3), for policy to
        keep: its hold; its reply, if Sent has it (one never confirmed is looked up now: the lookup
        failing, it may have gone out, so it is never sent again blind); its booking written down."""
        h = self.ledger.hold_for_message(e.key)
        if h is not None:
            e.recovered = True
            return {"own_hold": h["reason_code"]}
        for template in REPLY_TEMPLATES:
            mid = compose.new_message_id(self.cfg.doris_addr, e.key, template, 1)
            sent = self.ledger.send(mid)
            if sent is None:
                continue
            found = True if sent["state"] == "confirmed" else self.look_in_sent(e, mid, template)
            if found is None:
                self.ledger.set_send_state(mid, "unknown")
                e.recovered = True
                return {"own_reply_unknown": template}
            if found:
                self.ledger.set_send_state(mid, "confirmed")
                e.recovered = True
                return {"own_reply": template}
        b = self.own_booking(e)
        if b is not None:
            e.recovered = True
            return {"own_booking": b}
        return {}

    def own_booking(self, e):
        """This message's own booking, written down and not settled: its slot, whether it was direct
        (kept with the booking; one recorded before the ledger kept it is direct when no offer on the
        thread ever held its slot), the offer it came from and its length."""
        own = ledger_mod.norm_id(e.key)
        mine = [b for b in self.ledger.bookings_for_thread(e.thread)
                if ledger_mod.norm_id(b["message_key"]) == own and b["state"] in RESUMABLE_BOOKING_STATES]
        if not mine:
            return None
        slot, direct = mine[-1]["slot"], mine[-1]["direct"]
        offer_id = None if direct is True else self.ledger.offer_holding(e.thread, slot)
        if direct is None:
            direct = offer_id is None
        return {"slot": slot, "direct": direct, "offer_id": offer_id,
                "length_min": int((slot.end_utc - slot.start_utc).total_seconds() // 60)}

    def thread_state(self, e, s8):
        """The thread as policy sees it, before this message did anything (ruling 8: a kill can come
        part way, and the next run reads it again). A booking this message made itself is not "a
        live booking on the thread" (book() picks that one up), and an offer its own reply already
        carries does not count against the thread; a booking its own cancel or move already took off
        the calendar still stands for it, so the reply goes on. Only the offers that went to this
        message's sender are open to it (same sender, same thread); the thread's count is every offer."""
        tk, now = e.thread, self.now()
        mine = set(o["id"] for o in self.own_offers(e))
        on_thread = [o for o in self.ledger.open_offers(tk, now) if o["id"] not in mine]
        offers = [o for o in on_thread if o["sender"] == e.msg.from_addr.strip().lower()]
        own = ledger_mod.norm_id(e.key)
        booked = self.ledger.bookings_for_thread(tk)
        live = next((b for b in booked if b["state"] in ledger_mod.LIVE_BOOKING_STATES
                     and ledger_mod.norm_id(b["message_key"]) != own), None)
        if live is None:
            live = self.unbooked_here(e, booked)
        gone = any(b["state"] == "gone" for b in booked)
        return policy.ThreadState(
            thread_key=tk, own_thread=bool(s8.get("own_thread")), held=self.ledger.thread_held(tk),
            offer_count=self.ledger.offer_count(tk) - len(mine), open_offers=offers, live_booking=live,
            gone_booking=gone and live is None,
            written_to=frozenset(a.lower() for a in self.ledger.addresses_written_on_thread(tk)),
            offer_to=on_thread[-1]["sender"] if on_thread else None)

    def own_offers(self, e):
        """Offer records this message's own reply already carries on its thread (written down before
        the send; a kill came before the message closed)."""
        out = []
        for template in sorted(NEW_OFFER_TEMPLATES):
            o = self.ledger.offer_for_send(compose.new_message_id(self.cfg.doris_addr, e.key, template, 1))
            if o is not None and ledger_mod.norm_id(o["thread_key"]) == ledger_mod.norm_id(e.thread):
                out.append(o)
        return out

    def unbooked_here(self, e, booked):
        """The booking this message's own cancel or move already took off the calendar, or None."""
        ids = set(a["detail"].get("booking_id") for a in self.ledger.actions_for_message(e.key)
                  if a["kind"] in ("cancel", "move"))
        return next((b for b in booked if b["state"] == "cancelled" and b["booking_id"] in ids), None)

    # --- act ------------------------------------------------------------------------------------
    def act(self, e, f, plan):
        self.plan_line(e, f, plan)
        a = plan.action
        if a in ("book", "offer", "resend", "cancel", "move") and e.action_id is None:
            e.action_id = self.ledger.enqueue_action(self.run_id, e.key, a, {
                "template": plan.template or plan.offer_family, "booking_id": plan.booking_id, "thread": e.thread})
            self.ledger.start_action(e.action_id)
        if a == "book":
            return self.book(e, f, plan)
        if a == "offer":
            return self.offer(e, f, plan)
        if a == "resend":
            return self.resend(e, f, plan)
        if a == "cancel":
            return self.cancel(e, f, plan)
        if a == "move":
            return self.move(e, f, plan)
        if a == "hold":
            return self.hold(e, plan.hold_code, plan.why, f)
        if a == "replied":                      # its reply went out before a kill: nothing more to do
            return self.close(e, "decided", plan.outcome, plan.why, template=plan.template)
        if a == "thread_held":
            return self.close(e, "held", plan.outcome, plan.why)
        if a == "suppress":
            self.silence(e, "model")
            return self.close(e, "decided", "suppress", plan.why, dropped=0)
        if a == "close":
            self.ledger.close_offers(e.thread, "no_thanks")
        return self.close(e, "decided", plan.outcome, plan.why)

    def plan_line(self, e, f, plan):
        p = f.parse
        self.write("decided", {
            "step": "plan", "action": plan.action, "class": f.answer.cls if f.answer is not None else None,
            "template": plan.template or plan.offer_family, "hold": plan.hold_code,
            "parse": {"verdict": p.verdict, "failed": p.failed_condition, "instant": p.instant_utc, "zone": p.zone,
                      "length_min": p.length_min, "length_rule": p.length_rule, "conditions": p.conditions},
            "model_agrees": f.model_agrees, "agree": getattr(e, "agree", None),
            "pick": f.pick.kind if f.pick is not None else None,
            "slot": _span(plan.slot) if plan.slot is not None else None, "direct": plan.direct,
            "offer_id": plan.offer_id, "booking_id": plan.booking_id, "money_code": f.money_code,
            "thread": e.thread, "checks": "every effect passes the gate, G1 to G17, before it happens",
        }, subject=e.subject, why=plan.why)

    def book(self, e, f, plan):
        """Re-check right before the write, ledger first, create-only, read back (rulings 4 and 8).
        A booking this message already wrote down (a crash or a stop came part way) is picked up
        where it stopped: looked up by its ID first, never booked again."""
        bid = booking_id(e.key, plan.slot)
        if self.ledger.booking(bid) is not None:
            return self.resume_booking(e, f, plan, self.ledger.booking(bid))
        return self.book_fresh(e, f, plan, bid)

    def resume_booking(self, e, f, plan, b):
        """Ruling 8 after a restart. Never written: one more try, now that it is not blind. Written:
        read back, then T1 once (Sent is looked in first). Gone: held for the owner. A collision:
        that path. A lookup that fails: held."""
        bid, slot, state = b["booking_id"], b["slot"], b["state"]
        e.recovered = True
        if state in ("intent", "unconfirmed", "blocked"):
            try:
                there = self.get_event(e, bid)
            except _CalendarDown:
                return self.hold_code(e, "hold.unknown_result", f)
            if there is None:
                self.ledger.set_booking_state(bid, "intent")
                return self.book_fresh(e, f, plan, bid)
            self.ledger.set_booking_state(bid, "written")
            state = "written"
        if state in ("written", "confirmed"):
            try:
                there = self.get_event(e, bid)
            except _CalendarDown:
                return self.hold_code(e, "hold.unknown_result", f)
            if there is None:
                self.ledger.set_booking_state(bid, "gone")
                return self.hold_code(e, "hold.owner_removed", f)
            return self.confirm_booking(e, f, plan, bid, slot)
        if state == "collision_unresolved":
            return self.collision(e, f, plan, bid, slot)
        if state == "removed":
            return self.act(e, f, policy.on_collision(plan, f.thread))
        return self.hold_code(e, "hold.unsure", f)      # cancelled or gone: never booked again

    def drop_booking(self, bid):
        """A booking written down at intent and then not made: never written, so not live."""
        b = self.ledger.booking(bid)
        if b is not None and b["state"] == "intent":
            self.ledger.set_booking_state(bid, "blocked")

    def open_offers_to(self, thread_key, sender):
        """The thread's open offers that went to this address, oldest first. An offer is one sender's
        (ruling 6: same sender, same thread), so another sender's is nothing to pick from or resend."""
        addr = sender.strip().lower()
        return [o for o in self.ledger.open_offers(thread_key, self.now()) if o["sender"] == addr]

    def open_offer_slots(self, thread_key, sender):
        """Every slot of the open offers to this sender on the thread, as G9 reads them."""
        return [s for o in self.open_offers_to(thread_key, sender) for s in o["slots"]]

    def book_fresh(self, e, f, plan, bid):
        now, slot, msg = self.now(), plan.slot, e.msg
        # a pick keeps the grid its slot was offered on (G9): a time the sender wrote may sit on a quarter hour
        written = plan.direct is True or slot in self.open_offer_slots(e.thread, msg.from_addr)
        ok, rule = slots.bookable(slot, now, written=written)
        if not ok:
            self.drop_booking(bid)
            return self.act(e, f, policy.on_unavailable(plan, rule, f.thread))
        try:
            busy = slots.busy(self.read(e, slot.start_utc, slot.end_utc), slot)
        except _CalendarDown as down:
            self.drop_booking(bid)
            return self.hold_code(e, down.code, f)
        if busy:
            self.drop_booking(bid)
            return self.act(e, f, policy.on_unavailable(plan, "overlap", f.thread))
        self.ledger.record_booking(bid, e.key, e.thread, msg.from_addr, slot, now, run_id=self.run_id,
                                   direct=plan.direct is True)
        event = ics.build_event(bid, slot, self.link(), "CONFIRMED", 0, now, None)
        action = Action(kind="calendar_create", message_key=e.key, thread_key=e.thread,
                        recipient_source="sender", booking_id=bid, span=slot, direct=plan.direct, ics=event,
                        evidence={"parse_verdict": f.parse.verdict, "model_agrees": f.model_agrees})
        g = self.check(e, action, f)
        if not g.passed:
            self.ledger.set_booking_state(bid, "blocked")
            return self.blocked(e, f, g)
        try:
            self.create(e, bid, event)
        except _CalendarDown as down:
            return self.hold_code(e, down.code, f)
        return self.confirm_booking(e, f, plan, bid, slot)

    def confirm_booking(self, e, f, plan, bid, slot):
        """Her event is written: read it back and re-read its span, then T1 once."""
        now = self.now()
        try:
            mine, others = self.readback(e, bid, slot)
        except _CalendarDown as down:
            return self.hold_code(e, down.code, f)
        if others:
            return self.collision(e, f, plan, bid, slot)
        if mine is None:
            return self.hold_code(e, "hold.unknown_result", f)
        self.ledger.set_booking_state(bid, "confirmed")
        self.clean_readbacks.add(bid)
        self.summary.bookings += 1
        if plan.offer_id is not None:
            try:
                self.ledger.use_offer(plan.offer_id)
            except (KeyError, ValueError):
                pass
        self.ledger.close_offers(e.thread, "booked")
        attach = ics.build_event(bid, slot, self.link(), "CONFIRMED", 0, now, "PUBLISH")
        sent = self.reply(e, f, "T1", {"start": slot.start_utc, "duration": plan.length_min, "zone": f.parse.zone,
                                       "meeting_link": templates.MEETING_LINK}, ics=attach, booking_id=bid)
        if sent:
            extra = {"question": plan.side_question} if plan.side_question else {}
            self.close(e, "decided", "book", plan.why, template="T1", booking_id=bid, **extra)

    def collision(self, e, f, plan, bid, slot):
        """Another event landed between her re-check and her write: hers comes off, then fresh times."""
        self.ledger.set_booking_state(bid, "collision_unresolved")
        action = Action(kind="calendar_delete", message_key=e.key, thread_key=e.thread,
                        recipient_source="sender", booking_id=bid, span=slot, evidence={"reason": "collision"})
        g = self.check(e, action, f)
        if not g.passed or not self.remove(e, bid):
            return self.hold_code(e, "hold.remove_failed", f)
        self.ledger.set_booking_state(bid, "removed")
        e.recovered = True
        return self.act(e, f, policy.on_collision(plan, f.thread))

    def offer(self, e, f, plan):
        now = self.now()
        try:
            events = self.read(e, now, now + WINDOW)
        except _CalendarDown as down:
            return self.hold_code(e, down.code, f)
        exclude = set()
        if plan.exclude_offered:                # times offered before, not the ones this reply already holds
            exclude = set(self.ledger.offered_starts(e.thread)) - set(
                s.start_utc for o in self.own_offers(e) for s in o["slots"])
        if plan.asked is not None:
            exclude.add(plan.asked)
        found = self.find(events, now, plan, f, exclude)
        template, hold = policy.offer_form(plan.offer_family, len(found))
        if hold:
            return self.hold_code(e, hold, f)
        starts, zone = [s.start_utc for s in found], f.parse.zone
        if template == "T13":
            tslots = {"slot": starts[0], "duration": plan.length_min, "zone": zone}
        elif template == "T4":
            tslots = {"asked": plan.asked, "slots": starts, "duration": plan.length_min, "zone": zone}
        else:
            tslots = {"slots": starts, "duration": plan.length_min, "zone": zone}
        if self.reply(e, f, template, tslots, offered=found, length=plan.length_min):
            self.close(e, "decided", plan.outcome, plan.why, template=template)

    def find(self, events, now, plan, f, exclude):
        p = f.parse
        return slots.find_slots(events, now, plan.length_min, count=caps.SLOTS_PER_OFFER, exclude_starts=exclude,
                                first=plan.first, steer_day=p.steer_day, steer_window=p.steer_window,
                                steer_zone=p.zone or slots.PACIFIC)

    def resend(self, e, f, plan):
        """T12: the same open offer again, in both zones; no new offer record."""
        offered = [s for o in f.thread.open_offers for s in o["slots"]][:caps.SLOTS_PER_OFFER]
        tslots = {"slots": [s.start_utc for s in offered], "zone": f.parse.zone}
        if self.reply(e, f, "T12", tslots, offered=offered):
            self.close(e, "decided", plan.outcome, plan.why, template="T12")

    def cancel(self, e, f, plan):
        now, bid = self.now(), plan.booking_id
        slot = self.ledger.booking(bid)["slot"]
        attach = ics.build_event(bid, slot, self.link(), "CANCELLED", 1, now, "CANCEL")
        if not self.unbook(e, f, bid, slot):
            return None
        if self.reply(e, f, "T10", {"start": slot.start_utc, "zone": f.parse.zone}, ics=attach, booking_id=bid):
            self.close(e, "decided", "cancel", plan.why, template="T10", booking_id=bid)

    def move(self, e, f, plan):
        """A move is a cancel plus a fresh offer (ruling 7): the times are worked out first."""
        now, bid = self.now(), plan.booking_id
        slot = self.ledger.booking(bid)["slot"]
        try:
            events = self.read(e, now, now + WINDOW)
        except _CalendarDown as down:
            return self.hold_code(e, down.code, f)
        found = self.find(events, now, plan, f, set())
        template, hold = policy.offer_form("T18", len(found))
        if hold:
            return self.hold_code(e, hold, f)
        attach = ics.build_event(bid, slot, self.link(), "CANCELLED", 1, now, "CANCEL")
        if not self.unbook(e, f, bid, slot):
            return None
        tslots = {"start": slot.start_utc, "slots": [s.start_utc for s in found], "duration": plan.length_min,
                  "zone": f.parse.zone}
        if self.reply(e, f, template, tslots, ics=attach, offered=found, booking_id=bid, length=plan.length_min):
            self.close(e, "decided", "move", plan.why, template=template, booking_id=bid)

    def unbook(self, e, f, bid, slot):
        """Her own event off the calendar, through the gate, confirmed by a re-read. The cancel .ics
        goes on the reply, where G13 reads it. Already off (this message took it off, then a kill
        came before its reply): nothing is removed twice, and the reply goes on."""
        b = self.ledger.booking(bid)
        if b is not None and b["state"] == "cancelled":
            e.recovered = True
            return True
        action = Action(kind="calendar_delete", message_key=e.key, thread_key=e.thread,
                        recipient_source="sender", booking_id=bid, span=slot,
                        evidence={"reason": "sender_asked"})
        g = self.check(e, action, f)
        if not g.passed:
            self.blocked(e, f, g)
            return False
        if not self.remove(e, bid):
            self.hold_code(e, "hold.calendar_down", f)
            return False
        self.ledger.set_booking_state(bid, "cancelled")
        return True

    # --- her calendar calls (one retry after a short wait) ------------------------------------
    def read(self, e, start, end):
        subject = e.subject if e is not None else ""
        for attempt in (1, 2):
            seq = self.called("calendar_query", target="span", subject=subject, start=start, end=end,
                              attempt=attempt)
            try:
                events = list(self.calendar.query(start, end))
            except CalendarError as exc:
                self.result(seq, subject=subject, ok=False,
                            error="timeout" if isinstance(exc, CalendarTimeout) else "failed")
                if attempt == 1:
                    if e is not None:
                        e.retried = True
                    self.pause()
                    continue
                raise _CalendarDown("hold.calendar_down")
            self.result(seq, subject=subject, ok=True, events=[_event_line(ev) for ev in events])
            if attempt == 2 and e is not None:
                e.recovered = True
            return events

    def get_event(self, e, bid, expect=None):
        subject = e.subject if e is not None else ""
        for attempt in (1, 2):
            seq = self.called("calendar_get", target=bid, subject=subject, attempt=attempt)
            try:
                ev = self.calendar.get(bid)
            except CalendarError as exc:
                self.result(seq, subject=subject, ok=False,
                            error="timeout" if isinstance(exc, CalendarTimeout) else "failed")
                if attempt == 1:
                    if e is not None:
                        e.retried = True
                    self.pause()
                    continue
                raise _CalendarDown("hold.unknown_result")
            detail = {"ok": True, "found": ev is not None}
            if ev is not None:
                detail.update(_event_line(ev))
            if expect is not None:
                detail["fields_match"] = (ev is not None and ev.start_utc == expect.start_utc
                                          and ev.end_utc == expect.end_utc and ev.status in ("CONFIRMED", ""))
            self.result(seq, subject=subject, **detail)
            return ev

    def create(self, e, bid, event):
        for attempt in (1, 2):
            seq = self.called("calendar_create", target=bid, subject=e.subject, attempt=attempt)
            try:
                self.calendar.create_only(bid, event)
            except AlreadyExists:
                self.result(seq, subject=e.subject, ok=True, state="already_there")
            except CalendarTimeout:
                self.result(seq, subject=e.subject, ok=False, error="timeout", state="unknown")
                e.retried = True
                if self.get_event(e, bid) is None:      # the result is unknown: look it up by booking ID
                    if attempt == 1:
                        self.pause()
                        continue
                    raise _CalendarDown("hold.calendar_down")
            except CalendarError:
                self.result(seq, subject=e.subject, ok=False, error="failed", state="not_written")
                if attempt == 1:
                    e.retried = True
                    self.pause()
                    continue
                raise _CalendarDown("hold.calendar_down")
            else:
                self.result(seq, subject=e.subject, ok=True, state="written")
            self.ledger.set_booking_state(bid, "written")
            return

    def readback(self, e, bid, slot):
        """Her event fetched back and matched, and the span re-read: (her event, anything else overlapping)."""
        mine = self.get_event(e, bid, expect=slot)
        events = self.read(e, slot.start_utc, slot.end_utc)
        return mine, [ev for ev in slots.overlapping(events, slot) if ev.uid != bid]

    def remove(self, e, bid):
        for attempt in (1, 2):
            seq = self.called("calendar_delete", target=bid, subject=e.subject, attempt=attempt)
            try:
                self.calendar.delete(bid)
            except CalendarError as exc:
                self.result(seq, subject=e.subject, ok=False,
                            error="timeout" if isinstance(exc, CalendarTimeout) else "failed")
                e.retried = True
                if attempt == 1:
                    self.pause()
                continue
            self.result(seq, subject=e.subject, ok=True)
            try:
                if self.get_event(e, bid) is None:
                    return True
            except _CalendarDown:
                return False
        return False

    # --- check ---------------------------------------------------------------------------------
    def check(self, e, action, f=None):
        led = self.ledger
        ctx = types.SimpleNamespace(
            now=self.now(), run_id=self.run_id, owner_addr=self.cfg.owner_addr, doris_addr=self.cfg.doris_addr,
            message=e.msg if e is not None else None, screen_facts=self.screen_facts(e, f),
            model_answer=f.answer if f is not None else None, parse=f.parse if f is not None else None,
            halt_cause=self.halt_cause, holds_lock=lambda: bool(self.lock is not None and self.lock.held),
            spend_usd=lambda: led.spend(self.run_id), silenced=led.is_silenced,
            counts=lambda thread_key: self.counts(thread_key, action),
            fresh_read=lambda slot: self.read(e, slot.start_utc, slot.end_utc), booking=led.booking,
            open_offer_slots=lambda thread_key, sender: self.open_offer_slots(thread_key, sender),
            live_booking_for_thread=led.live_booking_for_thread,
            readback_clean=lambda bid: bid in self.clean_readbacks, model_ok=lambda: not self.model_down,
            meeting_link=self.link())
        g = gate.check(action, ctx)
        payload = dict(gate.receipt_payload(g), action=action.kind, template=action.template or "-")
        if action.booking_id:
            payload["booking_id"] = action.booking_id
        self.write("checked", payload, subject=e.subject if e is not None else "", why=g.why)
        if g.halt_cause:
            raise Halted(self.halt_from(g))
        return g

    def halt_from(self, g):
        """G1 to G3 stop her: a known halt cause, or fail closed."""
        cause = g.halt_cause
        if g.first_fail == "G3" and cause == "budget_cap":
            supervisor.request_stop(self.mode, "budget_cap", state_dir=self.cfg.state_dir)
            return cause                    # written to disk, so it survives a restart
        if cause in why_mod.HALT_CAUSES:
            return cause
        if cause == "G1":
            return "unreadable"             # the halt file could not be read
        raise Refused("%s could not confirm this run may go on (ledger lock or spend), so it stops" % (cause,), 3)

    def screen_facts(self, e, f):
        if e is None or e.msg is None:
            return {}
        s8 = e.facts_of(8)
        return {"branch": str(s8.get("branch", "else")), "own_thread": bool(s8.get("own_thread")),
                "others_on_thread": int(s8.get("others_on_thread", 0) or 0),
                "owner_in_to": bool(s8.get("owner_in_to")), "doris_in_to": bool(s8.get("doris_in_to")),
                "new_address": bool(s8.get("new_address")),
                "offer_to": f.thread.offer_to if f is not None else None,
                "written_to": sorted(self.ledger.addresses_written_on_thread(e.thread)) if e.thread else []}

    def counts(self, thread_key, action=None):
        """What is already done (G7). A booking's intent is written down before its check (G12), so
        the booking in hand is taken off the run's write count."""
        led, now = self.ledger, self.now()
        hour, day = now - _dt.timedelta(hours=1), now - _dt.timedelta(days=1)
        writes = led.count_calendar_writes(self.run_id)
        if action is not None and action.kind == "calendar_create" and action.booking_id:
            own = led.booking(action.booking_id)
            if own is not None and own["run_id"] == self.run_id and own["state"] == "intent":
                writes -= 1
        # a reply an earlier run recorded and never confirmed is this one again, not one more
        mine = offer = None
        if action is not None and action.kind == "send_reply" and action.template:
            mid = compose.new_message_id(self.cfg.doris_addr, action.message_key, action.template, 1)
            mine, offer = led.send(mid), led.offer_for_send(mid)

        def less(n, counted):
            return n - 1 if mine is not None and counted else n
        here = mine is not None and thread_key and ledger_mod.norm_id(mine["thread_key"] or "") == ledger_mod.norm_id(thread_key)
        return {"sends_hour": less(led.count_prospect_sends(since=hour), mine and mine["created"] >= hour),
                "sends_run": less(led.count_prospect_sends(run_id=self.run_id), mine and mine["run_id"] == self.run_id),
                "sends_thread_day": less(led.count_prospect_sends(since=day, thread_key=thread_key),
                                         here and mine["created"] >= day) if thread_key else 0,
                "sends_thread_ever": less(led.count_prospect_sends(thread_key=thread_key), here) if thread_key else 0,
                "offers_thread": (led.offer_count(thread_key) - (1 if offer is not None else 0)) if thread_key else 0,
                "calendar_writes_run": writes,
                "hold_notices_hour": led.count_hold_notices(since=hour)}

    def blocked(self, e, f, g):
        return self.hold(e, "hold.gate_blocked", why_mod.why("hold.gate_blocked", check=g.first_fail or "G16"), f)

    # --- sends ---------------------------------------------------------------------------------
    def reply(self, e, f, template, tslots, ics=None, offered=None, booking_id=None, length=None):
        """One template to the sender on their thread, the owner's blind copy on the envelope."""
        msg, now = e.msg, self.now()
        mid = compose.new_message_id(self.cfg.doris_addr, e.key, template, 1)
        try:
            raw, body = compose.reply(msg, template, tslots, self.cfg.doris_addr, mid, now, ics=ics)
        except (templates.TemplateError, compose.ComposeError) as exc:
            self.error(e, "compose", exc, template=template)
            self.hold(e, "hold.gate_blocked", why_mod.why("hold.gate_blocked", check="G8"), f)
            return False
        action = Action(kind="send_reply", message_key=e.key, thread_key=e.thread, template=template,
                        slots=dict(tslots), to=[msg.from_addr], bcc=[self.cfg.owner_addr], recipient_source="sender",
                        booking_id=booking_id, body_text=body, ics=ics,
                        evidence={"length_min": length} if length else {})
        g = self.check(e, action, f)
        if not g.passed:
            self.blocked(e, f, g)
            return False

        def write_offer_down():
            # written down before the send (A3), once per send: a restart that sends again under the
            # same Message-ID gives the same offer record its fresh times; the thread's count is unchanged
            if template not in NEW_OFFER_TEMPLATES or not offered:
                return
            prior = self.ledger.offer_for_send(mid)
            if prior is None:
                self.ledger.create_offer(e.thread, msg.from_addr, list(offered), length or caps.DEFAULT_LENGTH_MIN,
                                         now, now + caps.OFFER_EXPIRY, message_id=mid)
            elif prior["slots"] != list(offered):
                self.ledger.set_offer_slots(prior["id"], list(offered), now, now + caps.OFFER_EXPIRY)

        if not self.deliver(e, mid, "send_reply", template, msg.from_addr, raw, body,
                            compose.reply_envelope(msg, self.cfg.owner_addr), tslots, before_send=write_offer_down):
            self.hold(e, "hold.send_unconfirmed", why_mod.why("hold.send_unconfirmed"), f)
            return False
        self.summary.sends += 1
        return True

    def deliver(self, e, mid, kind, template, to_addr, raw, body, envelope, tslots, before_send=None):
        """Recorded before it is sent; sent once; found in Sent after (one more look); never resent blind.
        A send an earlier run recorded and never confirmed is looked up in Sent first: found, it is
        done; the lookup failing, nothing goes out (unknown); confirmed absent, it goes out now under
        the same Message-ID (ruling 8). True only when her copy is found in Sent."""
        subject = e.subject if e is not None else ""
        old = self.ledger.send(mid)
        if old is not None and old["state"] == "confirmed":
            return True                     # sent in an earlier run: the restart sends nothing twice
        body_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
        if old is None:
            self.ledger.record_send(mid, kind, template, e.thread if e is not None else None, to_addr, body_sha,
                                    self.now(), self.run_id)
        else:
            if e is not None:
                e.recovered = True
            found = self.look_in_sent(e, mid, template)
            if found:
                self.ledger.set_send_state(mid, "confirmed")
                return True
            if found is None:
                self.ledger.set_send_state(mid, "unknown")
                return False
            if old["body_sha256"] != body_sha:
                self.ledger.reset_send(mid, body_sha)
        if before_send is not None:
            before_send()
        seq = self.called("send", target=template, subject=subject, message_id=mid,
                          to="sender" if kind == "send_reply" else "owner",
                          bcc="owner" if kind == "send_reply" else None, body_sha256=body_sha,
                          slots=templates.receipt_slots(tslots))
        try:
            self.mail.send(raw, envelope, mid, template=template)
            self.result(seq, subject=subject, ok=True, state="handed_over")
        except SendUnknown:
            self.result(seq, subject=subject, ok=False, state="unknown")
        except MailError:
            self.result(seq, subject=subject, ok=False, state="refused")
            self.ledger.set_send_state(mid, "unknown")
            return False
        found = self.look_in_sent(e, mid, template) or self.look_in_sent(e, mid, template)
        self.ledger.set_send_state(mid, "confirmed" if found else "unknown")
        return bool(found)

    def look_in_sent(self, e, mid, template):
        """True found, False confirmed absent, None the lookup failed (never read as "not sent")."""
        subject = e.subject if e is not None else ""
        seq = self.called("sent_lookup", target=template, subject=subject, message_id=mid)
        try:
            found = self.mail.find_in_sent(mid) is True
        except MailError:
            self.result(seq, subject=subject, ok=False)
            return None
        self.result(seq, subject=subject, ok=True, found=found)
        return found

    # --- holds ---------------------------------------------------------------------------------
    def hold_code(self, e, code, f=None):
        return self.hold(e, code, why_mod.why(code), f)

    def hold(self, e, code, why_, f=None):
        """One ledger record and one notice (S1) to the owner; nothing to the prospect; the thread stays his.
        A message held before a kill keeps its one record, and its notice goes out once (Sent first)."""
        now = self.now()
        prior = self.ledger.hold_for_message(e.key) if e.redo else None
        if prior is not None:
            notice = prior["notice_message_id"]
        else:
            notice = None
            if code not in templates.NO_NOTICE and e.msg is not None:
                if self.ledger.count_hold_notices(now - _dt.timedelta(hours=1)) < caps.HOLD_NOTICES_PER_HOUR:
                    notice = compose.new_message_id(self.cfg.doris_addr, e.key, "S1", 1)
            self.ledger.record_hold(e.thread, e.key, code, notice, now)
        self.ledger.close_offers(e.thread, "held")
        if notice is not None:
            self.notice(e, code, notice, f)
        return self.close(e, "held", "hold:" + code[len("hold."):], why_)

    def notice(self, e, code, mid, f):
        msg = e.msg
        zone = f.parse.zone if f is not None and f.parse is not None else None
        try:
            ns = holds.notice_slots(msg, code, msg.date_utc or e.item.received_utc, self.draft_times(e, code), zone)
            raw, body = compose.owner_notice("S1", ns, self.cfg.doris_addr, self.cfg.owner_addr, mid, self.now())
        except (templates.TemplateError, compose.ComposeError) as exc:
            self.error(e, "hold_notice", exc, template="S1", reason=code)
            return
        action = Action(kind="send_hold_notice", message_key=e.key, thread_key=e.thread, template="S1", slots=ns,
                        to=[self.cfg.owner_addr], recipient_source="owner", body_text=body)
        if not self.check(e, action, f).passed:
            return                          # the hold stands and the digest lists it
        if self.deliver(e, mid, "send_hold_notice", "S1", self.cfg.owner_addr, raw, body, [self.cfg.owner_addr], ns):
            self.summary.hold_notices += 1

    def draft_times(self, e, code):
        """The S1 draft's times, worked out by code like T2's; none when he is already booked."""
        if code in NOTICE_WITHOUT_TIMES:
            return []
        live = self.ledger.live_booking_for_thread(e.thread)
        if live is not None and live.get("sender") == e.msg.from_addr:
            return []
        now = self.now()
        try:
            events = self.read(e, now, now + WINDOW)
        except _CalendarDown:
            return []
        return [s.start_utc for s in slots.find_slots(events, now, caps.DEFAULT_LENGTH_MIN,
                                                      count=caps.SLOTS_PER_OFFER)]

    # --- the closing line ----------------------------------------------------------------------
    def close(self, e, kind, outcome, why_, template="-", **extra):
        word = outcome.split(":")[0]
        seq = self.called("mail_file", target="doris-" + word, subject=e.subject)
        try:
            self.mail.file(e.item.server_id, "doris-" + word)
            self.result(seq, subject=e.subject, ok=True)
        except MailError:
            self.result(seq, subject=e.subject, ok=False)
        r = e.settled_by()
        payload = {"outcome": outcome, "template": template, "injection_seen": e.injection_seen,
                   "retried": e.retried, "recovered": e.recovered, "screen": r.screen if r is not None else None,
                   "wrapped": e.facts.get("wrapped"), "thread": e.subject if e.sensitive else e.thread}
        payload.update(extra)
        self.write(kind, payload, subject=e.subject, why=why_)
        e.closed = True
        if e.in_ledger:
            self.ledger.set_message_status(e.key, _status_for(outcome), outcome)
        if e.action_id is not None:
            self.ledger.finish_action(e.action_id, outcome)
        self.say(self.line_for(e, outcome, template, why_))
        return None

    def line_for(self, e, outcome, template, why_):
        if e.msg is None:
            who = "(unreadable message)"
        elif e.sensitive:
            who = "(sensitive mail, unread)"
        else:
            who = holds._name_line(e.msg.from_name or "") or e.msg.from_addr
        sent = " %s sent." % template if template != "-" else ""
        cost = ""
        if e.cost is not None and self.cfg.model_source != "stub":
            cost = " Model cost $%.4f%s." % (e.cost, " (recorded)" if self.cfg.model_source == "replayed" else "")
        staged = ""
        fault = (e.facts or {}).get("fault")
        if fault:
            done = "retried once" if e.retried else "recovered" if e.recovered else "no effect"
            staged = " Staged fault %s: %s." % (fault, done)
        return "%s  %s: %s.%s %s%s%s" % (templates.fmt_pacific(self.now()), who, outcome, sent, why_["text"], staged,
                                         cost)

    # --- the digest ----------------------------------------------------------------------------
    def send_digest(self):
        """S2 at the end of the run, counted from this run's receipts only, at 5:00 PM Pacific or later."""
        if self.t is not None:
            day = zones.to_local(self.t, "Pacific").date()
            self.t = max(self.t, zones.to_utc(_dt.datetime.combine(day, DIGEST_AT), "Pacific"))
        now = self.now()
        lines = receipts.read_lines(self.receipts_path)
        ds = digest.build(lines, now, verified=receipts.verify(self.receipts_path).ok)
        mid = compose.new_message_id(self.cfg.doris_addr, compose.digest_key(now), "S2", 1)
        try:
            raw, body = compose.owner_notice("S2", ds, self.cfg.doris_addr, self.cfg.owner_addr, mid, now)
        except (templates.TemplateError, compose.ComposeError) as exc:
            self.error(None, "digest", exc, template="S2")
            return
        action = Action(kind="send_digest", message_key="", thread_key="", template="S2", slots=ds,
                        to=[self.cfg.owner_addr], recipient_source="owner", body_text=body)
        if self.check(None, action).passed:
            if self.deliver(None, mid, "send_digest", "S2", self.cfg.owner_addr, raw, body, [self.cfg.owner_addr],
                            ds):
                self.say("%s  Digest sent to the owner: %s" % (templates.fmt_pacific(now), body.split("\n", 1)[0]))

    # --- staged faults (sandbox only, labelled simulated on the observed line) ------------------
    def arm(self, e):
        fault = e.facts.get("fault")
        if not fault:
            return
        params = dict((k[len("fault_"):], v) for k, v in e.facts.items() if k.startswith("fault_"))
        target = self.calendar if fault in CALENDAR_FAULTS else self.mail if fault in MAIL_FAULTS else None
        if target is None or not hasattr(target, "arm"):
            self.error(e, "arm", ValueError("unknown fault %r" % (fault,)))
            return
        target.arm(fault, **params)

    def disarm(self):
        for adapter in (self.calendar, self.mail):
            if hasattr(adapter, "disarm"):
                adapter.disarm()


def run(cfg):
    """One run in this process, until the inbox is drained. Raises Refused when it cannot start,
    and Halted under the kill switch (the parent writes the halted line)."""
    r = _Run(cfg)
    try:
        r.open()
        try:
            r.start()
            r.drain()
            if cfg.digest:
                r.send_digest()
        except Halted as h:
            r.summary.halted = h.cause
            if cfg.supervised:
                raise
            r.write_halted(h.cause)
        except MailError as exc:
            r.error(None, "fetch", exc)     # a refused read ends the run, and the summary says so
            r.summary.read_failed = True
        r.finish()
    finally:
        r.close_handles()
    return r.summary
