"""The loop: read -> classify -> decide -> check -> act -> record, one message at a time.

`run(cfg)` makes cfg.cycles passes over the inbox and returns a RunSummary: one by default (the
demo, traps); `python -m doris run` goes on until stopped, waiting for her next cycle on the wall
clock between passes. Each cycle, in the order of calendar-rulings-spec ("The order inside one
cycle"):

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
import math
import os
import time
import types
import urllib.parse
from dataclasses import dataclass, replace
from typing import Callable, List, Optional

from . import caps, compose, digest, gate, holds, ics, mail, netguard, parser, paths, policy, receipts
from . import screens, sentrecord, slots, supervisor, templates, zones
from . import ledger as ledger_mod
from . import model as model_mod
from . import why as why_mod
from .shapes import UTC, Action, AlreadyExists, CalendarError, CalendarTimeout, MailError, SendUnknown, Slot

CYCLE = _dt.timedelta(minutes=10)
DIGEST_AT = _dt.time(17, 0)            # Pacific: the digest goes at 5:00 PM (the demo's, or after its last cycle)
SANDBOX_OWNER = "whizz@fastmail.com"   # the sandbox's owner and Doris addresses (sandbox/README.md)
SANDBOX_DORIS = "doris.htm@fastmail.com"
WINDOW = caps.MAX_HORIZON + _dt.timedelta(days=1)    # the span an offer reads
ENV_OWNER = "DORIS_OWNER_ADDRESS"      # live: the owner's own address, from the env file
SMOKE_FIRST = "Run `%s` first." % paths.command("smoke --send-to wolf.whizz@fastmail.com")
SMOKE_REFUSAL = "Not started: %s. %s" % (sentrecord.MISSING, SMOKE_FIRST)   # no smoke record at all
FRESH_OVER_LEDGER = ("Not started: the live ledger is already there, and --fresh only starts a new one. "
                     "Run without --fresh to go on from it.")
NO_LIVE_LEDGER = ("Not started: there is no live ledger yet. `%s` starts one, "
                  "and she then reads only mail that arrives after it." % paths.command("run --live --fresh"))
ENV_KEY = "ANTHROPIC_API_KEY"
ENV_NAMES = (ENV_KEY, "DORIS_MODEL", templates.MEETING_LINK_ENV, ENV_OWNER)   # names only ever reach a receipt

CALENDAR_FAULTS = frozenset({"timeout-once", "timeout-twice", "read-fails", "slow-calendar", "lands-between",
                             "write-unknown-present", "write-unknown-absent", "lookup-fails",
                             "remove-fails-once", "remove-fails-always"})
MAIL_FAULTS = frozenset({"sent-unconfirmed"})
TIMEOUT_FAULTS = frozenset({"timeout-once", "timeout-twice"})   # the calendar does not answer: a wait
NEW_OFFER_TEMPLATES = frozenset({"T2", "T3", "T4", "T13", "T18"})   # each one is an offer record
REPLY_TEMPLATES = ("T1", "T2", "T3", "T4", "T10", "T12", "T13", "T18")   # her one reply to a message
# Her own booking for a message that a kill or a stop left part way: picked up again, never made again.
RESUMABLE_BOOKING_STATES = ("intent", "unconfirmed", "written", "confirmed", "collision_unresolved", "removed")
NOTICE_WITHOUT_TIMES = frozenset({"hold.owner_mail", "hold.calendar_down", "hold.unknown_result", "hold.no_slot",
                                  "hold.remove_failed", "hold.internal_error"})

NUDGE_REFERENCES = 20                  # the thread's last Message-IDs a nudge's References header carries
HALT_POLL_S = 1.0                      # how often a long run's wait looks for a halt itself
COPY_NAME_HEX = 16                     # her copy of a mail is named by this many hex digits of its sha256,
                                       # short enough for Windows' default path limit; the receipt keeps all 64
_sleep = time.sleep                    # her waits go through here, so a test can stand in for the wall clock
_time = time.time


def _wall_now():
    """The wall clock, aware UTC to the second: a live run's clock."""
    return _dt.datetime.now(UTC).replace(microsecond=0)


# ------------------------------------------------------------------------------------------
# Config and summary
# ------------------------------------------------------------------------------------------
@dataclass
class RunConfig:
    mode: str                               # sandbox | live | throwaway
    run_name: str                           # starts with the model source, then the mode (A6)
    state_dir: str                          # holds ledger.sqlite, ledger.lock, HALT and runs/
    sandbox_root: Optional[str] = None      # sandbox and throwaway: inbox/, calendar/, outbox/ and clock.txt
    owner_addr: Optional[str] = None        # sandbox and throwaway; a live run reads both from its env file
    doris_addr: Optional[str] = None
    model_source: str = "stub"              # live | replayed | stub | yes-man
    model_id: Optional[str] = None
    answers_path: Optional[str] = None      # recorded answers, for replayed
    record_path: Optional[str] = None       # live: where answers are recorded
    clock_mode: str = "fixed"               # fixed: stays at clock.txt; advance: steps cycle by cycle
    budget_usd: float = caps.BUDGET_USD_PER_RUN
    digest: bool = False                    # S2: when a finite run ends (the demo), or each day from 5:00 PM
                                            # Pacific in a run until stopped (`run`)
    supervised: bool = False                # the worker under the kill switch: a halt exits, the parent records it
    retry_wait_s: Optional[float] = None    # None: caps.CALENDAR_RETRY_WAIT_S
    calendar_timeout_s: Optional[float] = None   # sandbox calendar's wait: None the adapter's own; demo caps'
    say: Optional[Callable[[str], None]] = None   # one plain line per message, for the terminal
    env_path: Optional[str] = None          # live only: the env file the run loads itself (live_settings)
    fresh: bool = False                     # live only: start a new live ledger, reading mail from now on
    cycles: Optional[int] = 1               # passes over the inbox; None: until stopped (`run`)
    cycle_s: Optional[float] = None         # the wall-clock grid her waits keep; None: CYCLE, ten minutes
    lifeline: Optional[object] = None       # the worker's tie to its supervisor (supervisor.Lifeline): once it
                                            # is gone she halts herself and writes her own halted line
    # The smoke's own run (live only; smoke.py): her loop on the smoke's one test request, on a throwaway
    # ledger of its own (state_dir), obeying the live folder all the same.
    live_dir: Optional[str] = None          # the live state folder whose HALT, STOP and smoke record she obeys
                                            # (None: state_dir, as every other run)
    smoke_message_id: Optional[str] = None  # the one message she reads: the smoke's request, marked with
                                            # live.SMOKE_TEST_KEYWORD (needs live_dir)
    calendar_read_only: bool = False        # a create or delete is refused, so no test booking reaches the
                                            # owner's calendar (the message is held instead)


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
    """A halt was found mid-run (G1, or the budget cap). The worker exits; the parent writes the line
    (unless the worker's supervisor is gone: then she writes it herself)."""

    def __init__(self, cause):
        Exception.__init__(self, cause)
        self.cause = cause


class _CalendarDown(Exception):
    """A calendar call failed after its one retry. Carries the hold code it becomes."""

    def __init__(self, code="hold.calendar_down"):
        Exception.__init__(self, code)
        self.code = code


class _ReadOnlyCalendar(object):
    """The smoke's run: the owner's calendar for reads only. A create or a delete is refused as a
    CalendarError, which the loop holds on (calendar_down): a test booking never reaches his calendar."""
    REFUSED = "the smoke's run writes nothing to the owner's calendar"

    def __init__(self, calendar):
        self._calendar = calendar

    def create_only(self, booking_id, ics_bytes):
        raise CalendarError(self.REFUSED)

    def delete(self, booking_id):
        raise CalendarError(self.REFUSED)

    def __getattr__(self, name):                # query, get and the rest: the calendar's own
        return getattr(self._calendar, name)


def compose_ok_message_id(value):
    """One Message-ID, with or without its brackets: what the smoke mints and her fetch can search for."""
    from .adapters import live
    return isinstance(value, str) and bool(live._MESSAGE_ID.match(live._norm_mid(value)))


class LiveSettings(object):
    """What a live run takes from its env file. Its repr shows no value: the env holds her app
    password and the model key, and the link and the addresses are the env file's too."""
    __slots__ = ("env", "owner_addr", "doris_addr", "meeting_link", "hosts", "model_id")

    def __init__(self, env, owner_addr, doris_addr, meeting_link, hosts, model_id):
        self.env, self.owner_addr, self.doris_addr = env, owner_addr, doris_addr
        self.meeting_link, self.hosts, self.model_id = meeting_link, hosts, model_id

    def __repr__(self):
        return "<live settings from the env file>"

    __str__ = __repr__


def _live_link(env, error):
    """The meeting link live bookings carry: one https link of printable characters, no space."""
    name = templates.MEETING_LINK_ENV
    value = (env.get(name) or "").strip()
    if not value:
        raise error("%s is not set" % name)
    try:
        parts = urllib.parse.urlsplit(value)
        ok = parts.scheme == "https" and bool(parts.hostname)
    except ValueError:
        ok = False
    if not ok or len(value) > ics.MAX_LINK_CHARS or not all(0x21 <= ord(c) <= 0x7E for c in value):
        raise error("%s must be one https link of printable characters, at most %d" % (name, ics.MAX_LINK_CHARS))
    return value


def model_hosts():
    """The hosts a live model call reaches: the API's, and the HTTPS proxy's when the environment
    names one (the network guard lets the call through the proxy)."""
    hosts = [urllib.parse.urlsplit(model_mod.API_URL).hostname]
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if proxy:
        hosts.append(urllib.parse.urlsplit(proxy).hostname)
    return tuple(h for h in hosts if h)


def smoke_refusal(state_dir, now):
    """None when the live state folder holds the smoke's sent record (sentrecord) exactly as
    `smoke --send-to` writes it once it finds its copy in Sent, and fresh at now (no more than
    caps.SMOKE_RECORD_MAX_AGE old): the proof that the mail she sends reaches her Sent folder. Else the
    one line a live run is refused with, saying what is wrong."""
    why = sentrecord.problem(state_dir, now)
    return None if why is None else "Not started: %s. %s" % (why, SMOKE_FIRST)


def live_settings(env_path, model_source):
    """Everything a live run takes from its env file, and nothing from os.environ: her address and
    the owner's, the meeting link, the hosts the network guard allows and, with the model live, the
    model ID (the key stays in the env). The env it keeps holds only the names she reads (ENV_NAMES
    and live.ENV_NAMES): the smoke's wolf.whizz settings in the same file never reach her loop. The
    adapters are built once to check every setting they read; building one connects nowhere.
    Refused (exit 2) with one plain line naming the env file or the setting, never a value."""
    from . import envfile
    from .adapters import live
    try:
        env = envfile.load(env_path, names=_her_names())
    except envfile.EnvFileError as exc:
        raise Refused("Not started: the env file at %s cannot be used: %s." % (env_path, exc), 2) from None
    try:
        return settings_from(env, model_source)
    except live.ConfigError as exc:
        raise Refused("Not started: %s (env file %s)." % (exc, env_path), 2) from None


def _her_names():
    from .adapters import live
    return set(ENV_NAMES) | set(live.ENV_NAMES)


def her_env(env):
    """Of an env already loaded (the smoke's, which holds wolf's names too), the names she reads alone."""
    names = _her_names()
    return {name: value for name, value in env.items() if name in names}


def settings_from(env, model_source):
    """live_settings' checks on an env that holds only her names (envfile.load with them, or her_env:
    the smoke checks her loop's settings so before its end-to-end test). live.ConfigError naming the
    setting, never its value."""
    from .adapters import live
    doris_addr = live.bare_address(env, live.ENV_ADDRESS)
    owner_addr = live.bare_address(env, ENV_OWNER)
    if owner_addr.lower() == doris_addr.lower():
        raise live.ConfigError("%s must be the owner's own address, not hers" % ENV_OWNER)
    link = _live_link(env, live.ConfigError)
    hosts = tuple(live.hosts(env))
    live.LiveMail(env, doris_addr)
    live.LiveCalendar(env)
    model_id = None
    if model_source == "live":
        if not (env.get(ENV_KEY) or "").strip():
            raise live.ConfigError("%s is not set" % ENV_KEY)
        model_id = model_mod.default_model_id(env)
        if not caps.MODEL_PRICES_USD_PER_MTOK.get(model_id):
            raise live.ConfigError("DORIS_MODEL names a model with no price in caps.MODEL_PRICES_USD_PER_MTOK")
    return LiveSettings(env, owner_addr, doris_addr, link, hosts, model_id)


def live_preflight(state_dir, env_path, fresh):
    """`run --live`'s checks in the worker's own order, for the command line to make before it makes
    anything: the env file's settings (the model live), the smoke's proof, then the ledger against
    --fresh. Refused (exit 2) in one plain line; the worker makes every check again under its lock."""
    settings = live_settings(env_path, "live")
    refusal = smoke_refusal(state_dir, _wall_now())
    if refusal is not None:
        raise Refused(refusal, 2)
    there = os.path.exists(os.path.join(state_dir, "ledger.sqlite"))
    if fresh and there:
        raise Refused(FRESH_OVER_LEDGER, 2)
    if not fresh and not there:
        raise Refused(NO_LIVE_LEDGER, 2)
    return settings


# ------------------------------------------------------------------------------------------
# Pure pieces
# ------------------------------------------------------------------------------------------
def next_cycle(t):
    """The first point on the ten-minute grid at or after t: mail waits for her next cycle."""
    base = t.replace(minute=0, second=0, microsecond=0)
    steps = -(-(t - base) // CYCLE)
    return base + steps * CYCLE


def wait_words(seconds):
    """A wait as the terminal says it after "every": "10 minutes", "minute", "90 seconds"."""
    if seconds >= 60 and seconds % 60 == 0:
        minutes = int(seconds // 60)
        return "minute" if minutes == 1 else "%d minutes" % minutes
    return "%g seconds" % seconds


def booking_id(message_key, slot):
    """Ruling 8: from the inbound Message-ID and the slot, so a restart writes the same ID again."""
    seed = "%s|%s|%s" % (message_key, receipts.iso(slot.start_utc), receipts.iso(slot.end_utc))
    return ics.BOOKING_PREFIX + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


def nudge_due(sent):
    """When an offer's one nudge is due: caps.NUDGE_AFTER_BUSINESS_DAYS business days after the offer
    went out (Monday to Friday, her holiday list off), at the same Pacific clock time."""
    local = zones.to_local(sent, "Pacific")
    day, left = local.date(), caps.NUDGE_AFTER_BUSINESS_DAYS
    while left > 0:
        day += _dt.timedelta(days=1)
        if day.weekday() in caps.BUSINESS_DAYS and day not in caps.HOLIDAYS:
            left -= 1
    return zones.to_utc(_dt.datetime.combine(day, local.time()), "Pacific")


def _instant(text):
    """An instant a follow-up's detail kept (ISO UTC, as the ledger stores it), or None."""
    try:
        return _dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def _plural(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


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


def _file_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


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


def _as_written(ev, slot):
    """Her event read back exactly as she wrote it: the slot's start and end, and CONFIRMED."""
    return (ev is not None and ev.start_utc == slot.start_utc and ev.end_utc == slot.end_utc
            and ev.status == "CONFIRMED")


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
        self.offers_superseded = 0   # the sender's earlier open offers its own offer closed
        self.follow_up = None        # the follow-up its reply filed (brief 6.2): on its closing line too

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
        if cfg.mode != "live" and (cfg.live_dir is not None or cfg.smoke_message_id is not None
                                   or cfg.calendar_read_only):
            raise ValueError("live_dir, smoke_message_id and calendar_read_only are for the smoke's live run only")
        if cfg.smoke_message_id is not None and (not cfg.live_dir or not compose_ok_message_id(cfg.smoke_message_id)):
            raise ValueError("smoke_message_id needs live_dir, and it must be one Message-ID")
        if cfg.mode == "live":
            if cfg.sandbox_root is not None or cfg.owner_addr is not None or cfg.doris_addr is not None:
                raise ValueError("a live run reads both addresses from its env file and has no sandbox")
            if not cfg.env_path:
                raise ValueError("a live run needs env_path, its env file")
            if cfg.clock_mode != "fixed":
                raise ValueError("a live run reads the wall clock: clock_mode is fixed")
        else:
            if not (cfg.sandbox_root and cfg.owner_addr and cfg.doris_addr):
                raise ValueError("a %s run needs sandbox_root, owner_addr and doris_addr" % (cfg.mode,))
            if cfg.env_path is not None or cfg.fresh:
                raise ValueError("env_path and fresh are for a live run only")
        if cfg.cycles is not None and (isinstance(cfg.cycles, bool) or not isinstance(cfg.cycles, int)
                                       or cfg.cycles < 1):
            raise ValueError("cycles is a whole number from 1, or None for until stopped")
        if cfg.cycle_s is not None and (isinstance(cfg.cycle_s, bool) or not isinstance(cfg.cycle_s, (int, float))
                                        or not cfg.cycle_s > 0):
            raise ValueError("cycle_s is a positive number of seconds")
        self.cfg = cfg
        self.halt_dir = cfg.live_dir or cfg.state_dir   # where her HALT, STOP and smoke record are read
        self.cycle_s = CYCLE.total_seconds() if cfg.cycle_s is None else float(cfg.cycle_s)
        self.mode = cfg.mode
        self.run_id = cfg.run_name
        self.run_dir = os.path.join(cfg.state_dir, "runs", cfg.run_name)
        self.receipts_path = os.path.join(self.run_dir, "receipts.jsonl")
        self.summary = RunSummary(self.run_id, self.run_dir, self.receipts_path)
        self.say = cfg.say or (lambda text: None)
        self.wait_s = caps.CALENDAR_RETRY_WAIT_S if cfg.retry_wait_s is None else cfg.retry_wait_s
        self.lock = self.ledger = self.receipts = self.guard = None
        self.mail = self.calendar = self.model = self.env = None
        self.link_before = self.pinned_link = None
        self.live = None                    # LiveSettings, for a live run
        self.mail_since = None              # live: the ledger's fresh start; mail before it is never read
        self.model_target = cfg.model_source
        self.t = None
        self.model_down = False
        self.clean_readbacks = set()
        self.redo = set()
        self.batch_keys = set()
        self.read_failures = 0              # a run until stopped: her failed inbox reads in a row (pass_every_s)
        self.bookings_checked = False       # ruling 17 ran in this cycle (once a cycle)
        self.nudge_said = {}                # follow-up id -> why its nudge waits, said once a run
        self.swept = []                     # the rule words of the follow-ups this pass closed, for its terminal line

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
            raise Refused("Halted (%s). Only `%s` clears it." % (cause, paths.command("resume")),
                          supervisor.WORKER_EXIT_HALTED)
        if cfg.mode == "live":              # every setting from the env file, before anything is made
            self.live = live_settings(cfg.env_path, cfg.model_source)
            refusal = smoke_refusal(self.halt_dir, _wall_now())
            if refusal is not None:
                raise Refused(refusal, 2)
            self.cfg = cfg = replace(cfg, owner_addr=self.live.owner_addr, doris_addr=self.live.doris_addr)
        os.makedirs(os.path.join(self.run_dir, "messages"), exist_ok=True)
        # live: the env file's link; the sandbox: its own fixed link, never one from the shell
        self.pin_link(self.live.meeting_link if self.live else templates.PLACEHOLDER_MEETING_LINK)
        self.t = None if self.live else read_clock(cfg.sandbox_root)
        self.guard = netguard.install(self.allowed_hosts())
        self.ledger = self.open_ledger()
        self.receipts = receipts.ReceiptWriter(self.receipts_path, self.run_id, cfg.mode, clock=self.now)
        self.mail, self.calendar = self.adapters()
        self.model = model_mod.Model(cfg.model_source, model_id=cfg.model_id, answers_path=cfg.answers_path,
                                     record_path=cfg.record_path, env=self.live.env if self.live else None)
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

    def open_ledger(self):
        """The mode's ledger. Live (ruling 8): no ledger, no run, unless told to start fresh; a fresh
        start records its time, and she reads only mail received from then on."""
        path = os.path.join(self.cfg.state_dir, "ledger.sqlite")
        if self.live is None:
            return ledger_mod.Ledger(path)
        if self.cfg.fresh:
            try:
                ledger_mod.start_fresh(path, self.now())
            except FileExistsError:
                raise Refused(FRESH_OVER_LEDGER, 2) from None
        elif not os.path.exists(path):
            raise Refused(NO_LIVE_LEDGER, 2)
        led = ledger_mod.Ledger(path, create=False)
        self.mail_since = led.fresh_start()
        if self.mail_since is None:
            led.close()
            raise Refused("Not started: the live ledger names no fresh start, so old mail cannot be told from new.", 2)
        return led

    def pin_link(self, link):
        """The one link this run uses, pinned for the reply, the .ics and G13 alike; let go at close."""
        self.link_before = templates.use_meeting_link(link)
        self.pinned_link = link

    def close_handles(self):
        if self.pinned_link is not None:
            templates.use_meeting_link(self.link_before)
            self.pinned_link = self.link_before = None
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
        hosts = list(self.live.hosts) if self.live else []      # live: the hosts the env file names
        if self.cfg.model_source == "live":
            hosts += model_hosts()
        return tuple(h for h in hosts if h)

    def adapters(self):
        cal = {} if self.cfg.calendar_timeout_s is None else {"timeout_s": self.cfg.calendar_timeout_s}
        if self.live:
            from .adapters import live
            calendar = live.LiveCalendar(self.live.env, **cal)
            return (live.LiveMail(self.live.env, self.cfg.doris_addr, since_utc=self.mail_since,
                                  smoke_message_id=self.cfg.smoke_message_id),
                    _ReadOnlyCalendar(calendar) if self.cfg.calendar_read_only else calendar)
        from .adapters import sandbox
        clock = self.now if self.cfg.clock_mode == "advance" else None
        return (sandbox.SandboxMail(self.cfg.sandbox_root, self.cfg.doris_addr, clock=clock),
                sandbox.SandboxCalendar(self.cfg.sandbox_root, **cal))

    # --- small helpers ---------------------------------------------------------------------
    def now(self):
        if self.t is None:
            return _wall_now()
        return self.t

    def halt_cause(self):
        """The halt in force, read from disk now (G1 asks this at every effect). A worker whose
        supervisor is gone is halted even before HALT is down, or when it cannot be written."""
        cause = supervisor.halt_cause(self.mode, state_dir=self.halt_dir)
        if cause is None and self.supervisor_gone():
            return "supervisor_gone"
        return cause

    def supervisor_gone(self):
        """True once the worker's lifeline to its supervisor has closed (supervisor.Lifeline)."""
        line = self.cfg.lifeline
        return bool(line is not None and line.gone)

    def check_halt(self):
        try:
            cause = self.halt_cause()
        except supervisor.HaltFileError:
            cause = "unreadable"
        if cause is not None:
            raise Halted(cause)

    def link(self):
        return templates.meeting_link()     # the one the gate checks (G13): the link this run pinned

    def pause(self):
        if self.wait_s > 0:
            time.sleep(self.wait_s)

    def write(self, kind, payload, subject="", why=None):
        """One receipt line. The law: the meeting link never goes in one, whoever wrote it (a sender's
        text, the model's answer quoting it): every form of it is "[meeting link]" in every line."""
        return self.receipts.write(kind, templates.redact_values(payload), subject=templates.redact_link(subject),
                                   why=templates.redact_values(why) if why is not None else None)

    def called(self, call, target, subject="", **detail):
        return self.write("called", dict(detail, call=call, target=target), subject=subject)["seq"]

    def result(self, seq, subject="", **detail):
        self.write("result", dict(detail, call_seq=seq), subject=subject)

    def error(self, e, step, exc, subject=None, **detail):
        text = templates.redact_link(str(exc))[:200]       # the link out before the cut, so none is cut in half
        if subject is None:
            subject = e.subject if e is not None else ""
        self.write("error", dict(detail, step=step, error="%s: %s" % (type(exc).__name__, text)), subject=subject)

    # --- start, reconcile, drain, finish -------------------------------------------------------
    def start(self):
        cfg = self.cfg
        interrupted = [r["run_id"] for r in self.ledger.unended_runs() if r["run_id"] != self.run_id]
        self.ledger.start_run(self.run_id, self.mode, self.now())
        payload = {
            "step": "start", "mode": cfg.mode, "model_source": cfg.model_source, "model": self.model_target,
            "run_name": cfg.run_name, "clock": self.now(), "clock_mode": cfg.clock_mode,
            "env_set": self.env_set(), "interrupted_runs": interrupted, "simulated": cfg.mode != "live"}
        if self.live:
            payload.update(fresh_start=cfg.fresh, mail_since=self.mail_since)
        self.write("observed", payload)
        self.reconcile()

    def env_set(self):
        """The names set, never a value: live from its env file, otherwise from the environment."""
        if self.live:
            from .adapters import live
            return sorted(n for n in set(ENV_NAMES) | set(live.ENV_NAMES) if (self.live.env.get(n) or "").strip())
        return sorted(n for n in ENV_NAMES if os.environ.get(n))

    def reconcile(self):
        """Ruling 8 and A3: before anything new, every unfinished send is looked up in Sent (never
        sent again blind), every unfinished booking is looked up on the calendar, and every message
        fetched but never finished is read again through the whole loop. A send cut off mid-send
        (sending: a kill or a crash came between its mark and the transport's answer) may have gone,
        so it gets a send's two looks, the wait between them live (one wait for all of them); absent
        both times it is unknown and never sent again (review 2, finding 1), and the terminal says so."""
        cut_off = []
        for s in self.ledger.unfinished_sends():
            found = self.look_in_sent(None, s["message_id"], s["template"] or "-")
            if found:
                self.ledger.set_send_state(s["message_id"], "confirmed")
            elif found is False and s["state"] == "sending":
                cut_off.append(s)
        if cut_off:
            wait = caps.SENT_LOOK_WAIT_S if self.live else 0.0
            if wait > 0:
                _sleep(wait)
            for s in cut_off:
                template = s["template"] or "-"
                found = self.look_in_sent(None, s["message_id"], template, after_wait_s=wait or None)
                if found:
                    self.ledger.set_send_state(s["message_id"], "confirmed")
                elif found is False:
                    self.ledger.set_send_state(s["message_id"], "unknown")
                    self.say("%s  %s was cut off mid-send (a stop or a crash): no copy in Sent after two looks. "
                             "It may have gone, so it is never sent again." % (templates.fmt_pacific(self.now()),
                                                                              template))
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
                step = max(self.t, next_cycle(nxt))
                if step != self.t:              # her sandbox clock steps to her next cycle: ruling 17 runs in it
                    self.t, self.bookings_checked = step, False
            items = self.fetch()
            fresh = self.batch(items)
            if self.cfg.clock_mode != "advance" and not fresh:
                return                      # nothing new: a fetch can hand over old mail again
            if self.cfg.clock_mode == "advance" and not items and last == self.t:
                return                      # the adapter named an arrival it then did not hand over
            last = self.t

    def passes(self):
        """cfg.cycles passes over the inbox (None: until stopped), each after a wait for her next
        cycle. On the sandbox's advancing clock a later pass first steps her clock to her next cycle
        and reads what is in by then (a judge's new email), before any later arrival. In a run until
        stopped with the digest on (`run`), each pass ends with the day's digest once it is due."""
        self.one_pass(first=True)
        done = 1
        while self.cfg.cycles is None or done < self.cfg.cycles:
            self.wait_for_cycle()
            self.one_pass()
            done += 1

    def one_pass(self, first=False):
        """One pass over the inbox, then the day's digest when due. A read that fails is never an
        empty inbox (Contract 7). A finite run (the demo, traps) ends on it. A run until stopped never
        does (decision 89): an error line for each failed read, nothing sent until a read goes (not
        the digest either: fail closed), and she reads again at her next pass (pass_every_s). Only a
        stop or a halt ends it. One good read starts the count again."""
        self.bookings_checked = False
        try:
            if not first and self.cfg.clock_mode == "advance":
                self.t = next_cycle(self.t + _dt.timedelta(seconds=1))
                self.batch(self.fetch())
            self.drain()
        except MailError as exc:
            if self.cfg.cycles is not None:
                raise
            self.read_failures += 1
            every = self.pass_every_s()
            self.error(None, "fetch", exc, failed_in_a_row=self.read_failures, tries_every_s=every)
            when = ("at her next cycle" if self.read_failures < caps.MAIL_READ_FAILURES_IN_A_ROW else
                    "every %s until it does or she is stopped" % wait_words(every))
            self.say("%s  Inbox not read: %s (error line written). She sends nothing until it reads again, and "
                     "tries again %s (%d failed in a row)." % (templates.fmt_pacific(self.now()),
                                                               type(exc).__name__, when, self.read_failures))
            return
        self.read_failures = 0
        self.check_bookings()           # a pass with no new mail reads her bookings all the same (ruling 17)
        self.follow_ups()               # after the inbox: a reply that came in is seen before any nudge goes
        self.daily_digest()

    def pass_every_s(self):
        """Seconds between her passes: her cycle, and from caps.MAIL_READ_FAILURES_IN_A_ROW failed
        reads in a row caps.MAIL_READ_RETRY_S, never more often than her cycle (decision 89)."""
        if self.read_failures < caps.MAIL_READ_FAILURES_IN_A_ROW:
            return self.cycle_s
        return max(self.cycle_s, caps.MAIL_READ_RETRY_S)

    def wait_for_cycle(self):
        """Until her next pass: the next point on a wall-clock grid of pass_every_s() seconds (her
        cycle, unless her reads keep failing). She looks for a halt at least every HALT_POLL_S; under
        the kill switch the parent kills her sooner."""
        every = self.pass_every_s()
        end = (math.floor(_time() / every) + 1) * every
        while True:
            self.check_halt()
            left = end - _time()
            if left <= 0:
                return
            _sleep(min(left, HALT_POLL_S))

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
        """Unsupervised runs (traps, tests) write their own halted line, and so does a worker whose
        supervisor is gone, HALT first so the halt survives a restart (when even that cannot be
        written, the line says so); under the kill switch the supervisor writes it."""
        kept = {}
        if self.supervisor_gone():
            try:
                supervisor.request_stop(self.mode, "supervisor_gone", state_dir=self.halt_dir)
            except Exception:
                kept = {"halt_file": "unwritten"}      # she stops all the same; a restart is not refused
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
        self.write("halted", dict({"cause": cause, "in_flight": flight or "none", "dropped": dropped,
                                   "summary": "follows"}, **kept),       # cut the summary and verify fails
                   why=why_mod.why(why_code))

    # --- read ---------------------------------------------------------------------------------
    def fetch(self):
        seq = self.called("mail_fetch", target="inbox")
        try:
            items = list(self.mail.fetch_new())
        except MailError as exc:
            self.result(seq, ok=False, error=type(exc).__name__)
            raise                          # a refused login is loud: never read as an empty inbox
        left_out = getattr(self.mail, "smoke_left_out", 0)     # live: the smoke's own requests it left out
        self.result(seq, ok=True, count=len(items), **({"smoke_left_out": left_out} if left_out else {}))
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
        # (2) what screens 1, 2 and 4 settled goes first: a stop lands before anything else, so a sender's
        # other mail in the batch is filed after their opt-out, as "opted out earlier" says (oldest first otherwise)
        early = [e for e in entries if e.settled_by() is not None and e.settled_by().screen in (1, 2, 4)]
        early.sort(key=lambda e: e.settled_by().outcome != "suppress")
        for e in early:
            self.close_screened(e)
        # (3) ruling 17: her future bookings are read on the calendar, once a cycle, after the stop scan and
        # before any new message is handled
        self.check_bookings()
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
            payload.update(self.keep_copy(e))
        self.write("observed", payload, subject=e.subject)

    @staticmethod
    def screen_line(r):
        out = {"screen": r.screen, "name": r.name, "settled": r.settled, "facts": r.facts}
        if r.settled:
            out["outcome"] = r.outcome
            out["why"] = (r.why or {}).get("code")
        return out

    def keep_copy(self, e):
        """Her copy of the mail as it came, named by the first COPY_NAME_HEX hex digits of its sha256
        (raw_sha256 on the observed line holds all 64), for whoever reads the receipts; returns the
        observed line's copy fields. The law keeps the meeting link out of it: every form of the
        link in its bytes becomes "[meeting link]" (copy_redacted), and a copy that would still show
        the link once a part or header is decoded (base64, an encoded word) is not kept at all
        (copy_withheld), nor one the check could not run on. A mail without the link is kept byte
        for byte. Should another mail's copy already hold the short name, this one takes the short
        name and the first free number (<hex>-2.eml, -3, ...), which fits the path budget as the whole
        hash would not (test_path_lengths)."""
        data, replaced = templates.redact_link_bytes(e.item.raw)
        try:
            left = any(templates.has_link(view) for view in mail.decoded_views(data))
        except Exception:                   # Contract 7: a check that cannot run keeps nothing
            return {"copy_withheld": "link_check_failed"}
        if left:
            return {"copy_withheld": "meeting_link"}
        short, n = e.raw_sha256[:COPY_NAME_HEX], 1
        while True:
            name = os.path.join("messages", short + ("-%d" % n if n > 1 else "") + ".eml")
            path = os.path.join(self.run_dir, name)
            if not os.path.exists(path) or _file_bytes(path) == data:
                break
            n += 1
        if not os.path.exists(path):
            with open(path, "wb") as fh:
                fh.write(data)
        fields = {"message_copy": name.replace(os.sep, "/")}
        if replaced:
            fields["copy_redacted"] = "meeting_link"
        return fields

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
            detail["error"] = templates.redact_link(str(getattr(self.model, "last_error", "") or ""))[:200]
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
                                  "side_question": a.side_question, "ok": a.ok, "source": a.source,
                                  "raw": templates.redact_json_text(a.raw)},     # keeps its shape: see there
                   subject=e.subject)
        if self.ledger.spend(self.run_id) >= self.cfg.budget_usd:
            supervisor.request_stop(self.mode, "budget_cap", state_dir=self.halt_dir)
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
                            arrived=e.item.received_utc, **own)

    def own_effects(self, e):
        """What a message read again after a kill or a stop already did (ruling 8, A3), for policy to
        keep: its hold; its reply, if Sent has it (one never confirmed is looked up now: the lookup
        failing, or a reply that may have gone out, cut off sending, unknown or handed over, is never
        sent again blind, and the message is held); its booking written down."""
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
            if found:
                self.ledger.set_send_state(mid, "confirmed")
                e.recovered = True
                return {"own_reply": template}
            if found is None or sent["state"] in ledger_mod.NEVER_AGAIN_STATES:
                # the lookup failed, or it may have gone (cut off sending, unknown, or the server took
                # it) and Sent shows no copy: it is never sent again (decision 76; review 2, finding 1)
                if found is False:
                    self.ledger.set_send_state(mid, "unknown")  # sending looked for: unknown; the rest stay
                e.recovered = True
                return {"own_reply_unknown": template}
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
        if a == "replied":                      # its reply went out before a kill: nothing more to send
            self.supersede(e, plan.template)    # the kill may have come before its offer closed the earlier one
            self.file_follow_up(e, plan.template, f.parse.zone, self.booked_by(e))   # or before its follow-up was filed
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

    def open_offers_to(self, thread_key, sender, e=None):
        """The thread's open offers that went to this address, oldest first. An offer is one sender's
        (ruling 6: same sender, same thread), so another sender's is nothing to pick from or resend.
        With e, the offer e's own reply carries (written down before a kill) is left out: it is not one
        already open to them (Fable review 2 of PR #3, finding E)."""
        addr = sender.strip().lower()
        mine = set(o["id"] for o in self.own_offers(e)) if e is not None else set()
        return [o for o in self.ledger.open_offers(thread_key, self.now()) if o["sender"] == addr
                and o["id"] not in mine]

    def open_offer_slots(self, thread_key, sender, e=None):
        """Every slot of the open offers to this sender on the thread, as G9 reads them."""
        return [s for o in self.open_offers_to(thread_key, sender, e) for s in o["slots"]]

    def book_fresh(self, e, f, plan, bid):
        now, slot, msg = self.now(), plan.slot, e.msg
        # a pick books only a slot an open offer to its sender holds, the gate's own test (G9): one whose
        # offer expired or closed while she was down gets fresh times before any booking is made (Fable
        # review of PR #3, finding 6). Past that, the slot is a time the sender wrote or one she offered
        # them, so a quarter hour stays bookable.
        if plan.direct is not True and slot not in self.open_offer_slots(e.thread, msg.from_addr, e):
            self.drop_booking(bid)
            return self.act(e, f, policy.on_unavailable(plan, "not_offered", f.thread))
        ok, rule = slots.bookable(slot, now, written=True)
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
        """Her event is written: read it back (held unless exactly as she wrote it), re-read its span, T1 once."""
        now = self.now()
        try:
            mine, others = self.readback(e, bid, slot)
        except _CalendarDown as down:
            return self.hold_code(e, down.code, f)
        if mine is not None and not _as_written(mine, slot):
            # The server kept other times or another status than she wrote: nothing goes to the prospect.
            self.error(e, "readback", CalendarError("her event read back other than she wrote it"),
                       booking_id=bid)
            return self.hold_code(e, "hold.internal_error", f)
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
                                steer_zone=p.zone or slots.PACIFIC, derived=plan.derived)

    def resend(self, e, f, plan):
        """T12: the same open offer again, in both zones; no new offer record. T12 states no length,
        so G9 checks each time at the open offer's own (Fable review of PR #3, finding 5)."""
        offered = [s for o in f.thread.open_offers for s in o["slots"]][:caps.SLOTS_PER_OFFER]
        tslots = {"slots": [s.start_utc for s in offered], "zone": f.parse.zone}
        if self.reply(e, f, "T12", tslots, offered=offered, length=f.thread.open_offers[0]["length_min"]):
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
    def read(self, e, start, end, subject=None):
        if subject is None:
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

    def get_event(self, e, bid, expect=None, subject=None, **note):
        if subject is None:
            subject = e.subject if e is not None else ""
        for attempt in (1, 2):
            seq = self.called("calendar_get", target=bid, subject=subject, attempt=attempt, **note)
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
                detail["fields_match"] = _as_written(ev, expect)
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
    def check(self, e, action, f=None, subject=None):
        """The gate, G1 to G17, on one effect, and its `checked` line. With no message in hand (her
        digest, a nudge, the notice for a booking found gone) e is None; subject then names the
        receipt lines (default: run level)."""
        led = self.ledger
        named = {} if subject is None else {"subject": subject}    # the gate's own calendar read is named the same
        if subject is None:
            subject = e.subject if e is not None else ""
        ctx = types.SimpleNamespace(
            now=self.now(), run_id=self.run_id, owner_addr=self.cfg.owner_addr, doris_addr=self.cfg.doris_addr,
            message=e.msg if e is not None else None, screen_facts=self.screen_facts(e, f),
            model_answer=f.answer if f is not None else None, parse=f.parse if f is not None else None,
            halt_cause=self.halt_cause, holds_lock=lambda: bool(self.lock is not None and self.lock.held),
            spend_usd=lambda: led.spend(self.run_id), silenced=led.is_silenced,
            counts=lambda thread_key: self.counts(thread_key, action),
            fresh_read=lambda slot: self.read(e, slot.start_utc, slot.end_utc, **named), booking=led.booking,
            open_offer_slots=lambda thread_key, sender: self.open_offer_slots(thread_key, sender, e),
            open_offers=lambda thread_key: led.open_offers(thread_key, self.now()), thread_held=led.thread_held,
            live_booking_for_thread=led.live_booking_for_thread,
            readback_clean=lambda bid: bid in self.clean_readbacks, model_ok=lambda: not self.model_down,
            meeting_link=self.link())
        g = gate.check(action, ctx)
        payload = dict(gate.receipt_payload(g), action=action.kind, template=action.template or "-")
        if action.booking_id:
            payload["booking_id"] = action.booking_id
        self.write("checked", payload, subject=subject, why=g.why)
        if g.halt_cause:
            raise Halted(self.halt_from(g))
        return g

    def halt_from(self, g):
        """G1 to G3 stop her: a known halt cause, or fail closed."""
        cause = g.halt_cause
        if g.first_fail == "G3" and cause == "budget_cap":
            supervisor.request_stop(self.mode, "budget_cap", state_dir=self.halt_dir)
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
                "nudges_thread": (less(led.count_nudges(thread_key), here and mine["template"] == "T9")
                                  if thread_key else 0),
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
            # same Message-ID gives the same offer record its fresh times and the time it goes out
            # (a pick that came before then cannot be answering it); the thread's count is unchanged
            if template not in NEW_OFFER_TEMPLATES or not offered:
                return
            prior, sent = self.ledger.offer_for_send(mid), self.now()
            if prior is None:
                self.ledger.create_offer(e.thread, msg.from_addr, list(offered), length or caps.DEFAULT_LENGTH_MIN,
                                         sent, sent + caps.OFFER_EXPIRY, message_id=mid)
            else:
                self.ledger.set_offer_slots(prior["id"], list(offered), sent, sent + caps.OFFER_EXPIRY)

        if not self.deliver(e, mid, "send_reply", template, msg.from_addr, raw, body,
                            compose.reply_envelope(msg, self.cfg.owner_addr), tslots, before_send=write_offer_down):
            self.hold(e, "hold.send_unconfirmed", why_mod.why("hold.send_unconfirmed"), f)
            return False
        self.supersede(e, template)
        self.summary.sends += 1
        self.file_follow_up(e, template, tslots.get("zone"), booking_id)
        return True

    def supersede(self, e, template):
        """An offer this message's reply carries, found in Sent, closes the sender's earlier open offers
        on the thread ("superseded"), so "the first one" means the newest offer's first (Fable review of
        PR #3, finding 4). Not at write-down: a kill before the send is confirmed leaves the offer the
        message answered open for the restart, which plans the same reply again. The thread's count
        keeps every offer, so a third set of times is still a hold."""
        if template not in NEW_OFFER_TEMPLATES:
            return
        newest = self.ledger.offer_for_send(compose.new_message_id(self.cfg.doris_addr, e.key, template, 1))
        if newest is not None:
            e.offers_superseded += self.ledger.close_offers(e.thread, "superseded", sender=newest["sender"],
                                                            before=newest["id"])

    def deliver(self, e, mid, kind, template, to_addr, raw, body, envelope, tslots, before_send=None, thread=None,
                subject=None):
        """Recorded before it is sent; sent once; found in Sent after (one more look); never resent blind.
        Right before the transport is called she reads the halt again (G1 passed before the record, and
        a lookup in Sent can take a while): a halt in force then raises Halted with the send still
        pending and no send line written, so the run after `resume` sends it once.
        It is marked sending right before the transport is called: from there it may have gone, and a
        kill or a crash before the transport's answer is written down leaves it so (review 2, finding
        1). The answer is read the same for any transport: accepted (a JMAP submission created) is handed_over,
        written down before the Sent look (decision 76); SendUnknown is unknown, also before any look;
        MailError (it surely did not go) puts it back to pending. From sending on, it never goes
        again, whatever Sent shows. A send an earlier run recorded and never confirmed is looked up in
        Sent first: found, it is done; it may have gone, or the lookup failed, nothing goes out; still
        pending and confirmed absent, it goes out now under the same Message-ID (ruling 8). True only
        when her copy is found in Sent. With no message in hand (e is None: her digest, a nudge, the
        notice for a booking found gone), thread is the thread the send is recorded on and subject
        names its receipt lines."""
        named = {} if subject is None else {"subject": subject}    # her lookups in Sent are named the same
        if subject is None:
            subject = e.subject if e is not None else ""
        if e is not None:
            thread = e.thread
        old = self.ledger.send(mid)
        if old is not None and old["state"] == "confirmed":
            return True                     # sent in an earlier run: the restart sends nothing twice
        body_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
        if old is None:
            self.ledger.record_send(mid, kind, template, thread, to_addr, body_sha, self.now(), self.run_id)
        else:
            if e is not None:
                e.recovered = True
            found = self.look_in_sent(e, mid, template, **named)
            if found:
                self.ledger.set_send_state(mid, "confirmed")
                return True
            if old["state"] in ledger_mod.NEVER_AGAIN_STATES or found is None:
                return False                # it went or may have, or the lookup failed: nothing goes out
            if old["body_sha256"] != body_sha:
                self.ledger.reset_send(mid, body_sha)
        if before_send is not None:
            before_send()
        self.check_halt()                   # G1 passed before this; a halt since then stops it here, still pending
        seq = self.called("send", target=template, subject=subject, message_id=mid,
                          to="sender" if kind == "send_reply" else "owner",
                          bcc="owner" if kind == "send_reply" else None, body_sha256=body_sha,
                          slots=templates.receipt_slots(tslots))
        self.ledger.set_send_state(mid, "sending")             # from here it may have gone: never sent again
        try:
            self.mail.send(raw, envelope, mid, template=template)
        except SendUnknown:
            self.ledger.set_send_state(mid, "unknown")         # it may have gone, written down before any look
            self.result(seq, subject=subject, ok=False, state="unknown")
        except MailError:
            self.ledger.send_refused(mid)                       # it surely did not go: pending again
            self.result(seq, subject=subject, ok=False, state="refused")
            return False
        else:
            self.ledger.set_send_state(mid, "handed_over")     # the 250, written down before any look
            self.result(seq, subject=subject, ok=True, state="handed_over")
        found = self.look_in_sent(e, mid, template, **named)
        if not found:                       # one more look; live gives the server time to file her copy
            wait = caps.SENT_LOOK_WAIT_S if self.live else 0.0
            if wait > 0:
                _sleep(wait)
            found = self.look_in_sent(e, mid, template, after_wait_s=wait or None, **named)
        if found:
            self.ledger.set_send_state(mid, "confirmed")
        return bool(found)

    def look_in_sent(self, e, mid, template, after_wait_s=None, subject=None):
        """True found, False confirmed absent, None the lookup failed (never read as "not sent")."""
        if subject is None:
            subject = e.subject if e is not None else ""
        extra = {} if after_wait_s is None else {"after_wait_s": after_wait_s}
        seq = self.called("sent_lookup", target=template, subject=subject, message_id=mid, **extra)
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

    # --- the follow-up (the law: "a dated ledger entry, one nudge on an unanswered offer") ----------
    def file_follow_up(self, e, template, zone=None, booking_id=None):
        """Brief 6.2: the follow-up, filed once her reply is found in Sent. For an offer (T2, T3, T4,
        T13, T18): its one nudge, dated caps.NUDGE_AFTER_BUSINESS_DAYS business days after the offer
        went out. For a confirmation (T1): the check, every cycle until the meeting starts, that her
        booking is still on the calendar (ruling 17). Her ledger first, then one `decided` line naming
        the entry and its date (and an offer's expiry); the message's closing line and its terminal
        line name it too. Filed again (a restart read the message again) it is the same entry, and the
        line says so (new: false). The reply has gone by now, so filing that fails is an error line and
        the message closes as it was decided: it never turns a sent reply into a hold."""
        msg, now = e.msg, self.now()
        try:
            if template in NEW_OFFER_TEMPLATES:
                offer = self.ledger.offer_for_send(compose.new_message_id(self.cfg.doris_addr, e.key, template, 1))
                if offer is None:
                    return
                due = nudge_due(offer["created"])
                refs = [r for r in map(compose._msgid, msg.references) if r][-NUDGE_REFERENCES:]
                fid, new = self.ledger.file_followup(
                    "nudge", e.thread, msg.from_addr, e.key, due, now, offer_id=offer["id"],
                    detail={"name": holds._name_line(msg.from_name or ""),
                            "subject": holds.shown_subject(msg.subject or ""),
                            "message_id": compose._msgid(msg.message_id), "references": refs, "zone": zone})
                entry = {"id": fid, "kind": "nudge", "due": due, "offer_id": offer["id"], "expires": offer["expires"]}
                why_ = why_mod.why("followup.nudge_filed", when=due, expires=offer["expires"])
            elif template == "T1" and booking_id is not None and self.ledger.booking(booking_id) is not None:
                due = self.ledger.booking(booking_id)["slot"].start_utc
                fid, new = self.ledger.file_followup(
                    "booking_check", e.thread, msg.from_addr, e.key, due, now, booking_id=booking_id,
                    detail={"name": holds._name_line(msg.from_name or ""), "quote": holds.quote_of(msg),
                            "wrote_at": msg.date_utc or e.item.received_utc, "zone": zone})
                entry = {"id": fid, "kind": "booking_check", "due": due, "booking_id": booking_id}
                why_ = why_mod.why("followup.booking_filed", when=due)
            else:
                return
            self.write("decided", {"step": "follow_up", "action": "file_follow_up", "follow_up": entry, "new": new,
                                   "thread": e.thread}, subject=e.subject, why=why_)
            e.follow_up = entry
        except (Halted, Refused):
            raise
        except Exception as exc:            # Contract 7: loud, and the reply that went stands
            self.error(e, "follow_up", exc, template=template)

    def booked_by(self, e):
        """The booking this message's own confirmation stands for (on the calendar), or None."""
        own = ledger_mod.norm_id(e.key)
        mine = [b for b in self.ledger.bookings_for_thread(e.thread)
                if ledger_mod.norm_id(b["message_key"]) == own and b["state"] in ledger_mod.ON_CALENDAR_STATES]
        return mine[-1]["booking_id"] if mine else None

    @staticmethod
    def entry_of(fu):
        """A follow-up as a receipt line names it: its id, kind and date, and its offer or booking."""
        entry = {"id": fu["id"], "kind": fu["kind"], "due": fu["due"]}
        entry.update({"offer_id": fu["offer_id"]} if fu["kind"] == "nudge" else {"booking_id": fu["booking_id"]})
        return entry

    def close_follow_up(self, fu, code, rule=None, **extra):
        """One follow-up settled: the `decided` line that says why first, then her ledger (a kill
        between the two writes the line once more at her next pass, never loses the close)."""
        payload = dict(extra, step="follow_up", action="close_follow_up", follow_up=self.entry_of(fu),
                       thread=fu["thread_key"], message=fu["message_key"], sender=fu["sender"])
        if rule is not None:
            payload["rule"] = rule
        self.write("decided", payload, why=why_mod.why(code, **({"rule": rule} if rule is not None else {})))
        self.ledger.close_followup(fu["id"], rule or "nudged", self.now())
        self.nudge_said.pop(fu["id"], None)
        self.swept.append(rule or "nudged")

    # --- ruling 17: her future bookings, read on the calendar once a cycle ---------------------------
    def check_bookings(self):
        """Ruling 17, once a cycle, after the stop scan and before any new message is handled: every
        future booking in her ledger is read on the calendar by its booking ID. A cycle is a pass of a
        long `run`, and on the sandbox's advancing clock each step of her clock to her next ten-minute
        cycle (so `demo`, one pass over a whole day, reads them cycle by cycle as a live day would).
        Gone means missing on two reads a short wait apart, both answered. A read that fails is never
        "missing", and a calendar that does not answer ends this cycle's check (her next cycle reads
        again). One still there leaves its read line and nothing more: edits are not checked. A
        booking whose own message a kill left part way is that message's to finish (resume_booking).
        One found gone before a kill or a stop is finished first."""
        if self.bookings_checked:
            return
        self.bookings_checked = True
        for g in self.ledger.gone_unfinished():
            self.check_halt()
            self.guarded_gone(g["booking"], g["notice_message_id"], True)
        for b in self.ledger.future_bookings(self.now()):
            self.check_halt()
            own = self.ledger.message(b["message_key"])
            if own is not None and own["status"] in ledger_mod.UNPROCESSED:
                continue
            there = self.still_there(b["booking_id"])
            if there is None:
                break
            if there is False:
                self.guarded_gone(b, None, False)

    def still_there(self, bid):
        """True: her event is on the calendar. False: gone, missing on two reads a short wait apart
        that the calendar answered. None: a read failed after its one retry, which is never "missing"."""
        try:
            if self.get_event(None, bid, booking_check=1) is not None:
                return True
            wait = caps.RULING17_REREAD_WAIT_S if self.cfg.retry_wait_s is None else self.cfg.retry_wait_s
            if wait > 0:
                _sleep(wait)
            return self.get_event(None, bid, booking_check=2) is not None
        except _CalendarDown:
            return None

    def guarded_gone(self, b, notice, recovered):
        """booking_gone for one booking: a bug in it is an error line and the run goes on (Contract 7).
        What her ledger already holds (the booking marked gone, the thread held) stands, and her next
        pass finishes the rest."""
        try:
            self.booking_gone(b, notice, recovered)
        except (Halted, Refused):
            raise
        except Exception as exc:
            self.error(None, "booking_check", exc, subject=ledger_mod.GONE_KEY + b["booking_id"],
                       booking_id=b["booking_id"])

    def booking_gone(self, b, notice=None, recovered=False):
        """One of her future bookings is gone from the calendar (ruling 17): the owner gets a hold
        notice (S1: "you removed this booking from your calendar; I haven't told them") and the
        prospect gets nothing, no message, no cancel .ics and no fresh times. In order: what she saw
        (`observed`: owner_deleted, the booking ID and its two reads, never a title); her ledger in one
        step (the booking marked gone; the thread held, so it stays the owner's and a later cancel or
        move for it is held as "a cancel or move for a booking I don't have"; the notice's Message-ID
        kept); the notice through the gate; the one `held` line that closes it; its follow-up closed.
        A kill after the ledger step is finished at her next pass (recovered), the notice never sent
        twice. Past the hourly cap on hold notices no notice goes: the hold stands and the digest
        lists it."""
        bid, thread, now = b["booking_id"], b["thread_key"], self.now()
        subject = ledger_mod.GONE_KEY + bid
        fu = self.ledger.followup_for_booking(bid)
        kept = fu["detail"] if fu is not None else {}
        who = {"from_addr": b["sender"], "from_name": kept.get("name") or ""}
        action_id = self.ledger.enqueue_action(self.run_id, b["message_key"], "booking_check",
                                               {"template": "S1", "booking_id": bid, "thread": thread})
        self.ledger.start_action(action_id)
        self.write("observed", dict(who, step="booking_check", owner_deleted=True, booking_id=bid,
                                    reads=0 if recovered else 2, slot=_span(b["slot"]), thread=thread,
                                    message=b["message_key"], clock=now, recovered=recovered), subject=subject)
        if not recovered:
            if self.ledger.count_hold_notices(now - _dt.timedelta(hours=1)) < caps.HOLD_NOTICES_PER_HOUR:
                notice = compose.new_message_id(self.cfg.doris_addr, subject, "S1", 1)
            self.ledger.mark_booking_gone(bid, notice, now)
        self.ledger.close_offers(thread, "held")
        sent = self.gone_notice(b, subject, notice, kept) if notice is not None else False
        code = ledger_mod.GONE_HOLD
        why_ = why_mod.why(code)
        self.write("held", dict(who, outcome="hold:" + code[len("hold."):], template="-", step="booking_check",
                                booking_id=bid, notice_message_id=notice, notice_sent=sent, thread=thread,
                                message=b["message_key"], recovered=recovered), subject=subject, why=why_)
        self.ledger.close_followup(self.ledger.followup_for_booking(bid)["id"], "gone", now)
        self.ledger.finish_action(action_id, "hold:" + code[len("hold."):])
        self.say("%s  %s: hold:%s. %s Her booking for %s is gone from the calendar (missing on two reads). Hold "
                 "notice to the owner %s. Nothing sent to them."
                 % (templates.fmt_pacific(now), holds.sender_name(who["from_name"], who["from_addr"]),
                    code[len("hold."):], why_["text"], templates.fmt_pacific(b["slot"].start_utc),
                    "sent" if sent else "not sent (the digest lists the hold)"))

    def gone_notice(self, b, subject, mid, kept):
        """The S1 for a booking found gone: to the owner only, through the gate, sent once (Sent is
        looked in first after a restart). It names the prospect, when they wrote and what, from what
        her ledger kept when she confirmed the booking (the same safe words any S1 carries), and its
        draft carries fresh times worked out by code. True when her copy is found in Sent."""
        thread, code = b["thread_key"], ledger_mod.GONE_HOLD
        own = self.ledger.message(b["message_key"])
        wrote_at = _instant(kept.get("wrote_at")) or (own["received_utc"] if own is not None else None) or b["created"]
        stand_in = types.SimpleNamespace(from_name=kept.get("name") or "", from_addr=b["sender"],
                                         own_text=kept.get("quote") or "")
        try:
            ns = holds.notice_slots(stand_in, code, wrote_at, self.gone_draft_times(subject), kept.get("zone"))
            raw, body = compose.owner_notice("S1", ns, self.cfg.doris_addr, self.cfg.owner_addr, mid, self.now())
        except (templates.TemplateError, compose.ComposeError) as exc:
            self.error(None, "hold_notice", exc, subject=subject, template="S1", reason=code)
            return False
        action = Action(kind="send_hold_notice", message_key=subject, thread_key=thread, template="S1", slots=ns,
                        to=[self.cfg.owner_addr], recipient_source="owner", body_text=body)
        if not self.check(None, action, subject=subject).passed:
            return False                    # the hold stands and the digest lists it
        if self.deliver(None, mid, "send_hold_notice", "S1", self.cfg.owner_addr, raw, body, [self.cfg.owner_addr],
                        ns, thread=thread, subject=subject):
            self.summary.hold_notices += 1
            return True
        return False

    def gone_draft_times(self, subject):
        """The draft's times for that notice, worked out by code like any S1's; none when the calendar
        does not answer."""
        now = self.now()
        try:
            events = self.read(None, now, now + WINDOW, subject=subject)
        except _CalendarDown:
            return []
        return [s.start_utc for s in slots.find_slots(events, now, caps.DEFAULT_LENGTH_MIN,
                                                      count=caps.SLOTS_PER_OFFER)]

    # --- the follow-up sweep: nudges due, and entries with nothing left to do ---------------------------
    def follow_ups(self):
        """Once a pass, after the inbox is read and every new message is handled: each open follow-up
        in her ledger is looked at. A booking's entry closes once the meeting started, or the booking
        was cancelled or is gone. An offer's entry closes with no nudge as soon as there is nobody to
        nudge (nudge_rule), and its one nudge goes once it is due (nudge). A bug on one entry is an
        error line, that nudge never goes, and the run goes on (Contract 7)."""
        self.swept = []
        gone = set(g["followup"]["id"] for g in self.ledger.gone_unfinished())
        for fu in self.ledger.open_followups():
            self.check_halt()
            try:
                if fu["kind"] == "nudge":
                    self.sweep_nudge(fu)
                elif fu["id"] not in gone:  # one found gone and not finished is ruling 17's, at her next pass
                    self.sweep_booking(fu)
            except (Halted, Refused):
                raise
            except Exception as exc:
                self.error(None, "follow_up", exc, follow_up=fu["id"], follow_up_kind=fu["kind"])
                if fu["kind"] == "nudge":
                    self.close_follow_up(fu, "followup.no_nudge", "internal_error")
        self.say_follow_ups()

    def say_follow_ups(self):
        """One terminal line for a sweep that closed something: how many and why, and what is still open."""
        closed = [r for r in self.swept if r != "nudged"]       # a nudge that went has its own line
        if not closed:
            return
        words = why_mod.FOLLOW_UP_WORDS
        why_ = ", ".join("%s: %d" % (words.get(r, r), closed.count(r)) for r in sorted(set(closed)))
        still = self.ledger.open_followups()
        nudges = [f for f in still if f["kind"] == "nudge"]
        parts = []
        if nudges:
            parts.append("%s, the next due %s" % (_plural(len(nudges), "nudge"),
                                                  templates.fmt_pacific(min(f["due"] for f in nudges))))
        if len(still) > len(nudges):
            parts.append(_plural(len(still) - len(nudges), "booking check"))
        self.say("%s  Follow-ups: %d closed (%s). Still open: %s." % (
            templates.fmt_pacific(self.now()), len(closed), why_, "; ".join(parts) if parts else "none"))

    def sweep_booking(self, fu):
        b = self.ledger.booking(fu["booking_id"])
        if b is None or b["state"] == "gone":
            rule = "gone"
        elif b["state"] not in ledger_mod.ON_CALENDAR_STATES:
            rule = "cancelled"
        elif b["slot"].start_utc <= self.now():
            rule = "started"
        else:
            return
        self.close_follow_up(fu, "followup.booking_closed", rule)

    def nudge_rule(self, fu, offer, own):
        """Why this offer gets no nudge, as a why's rule word, or None while it may still get one
        (templates.md T9). The offer is used, closed or expired; the thread is held; they opted out;
        the thread already had its one nudge; or they wrote since the offer went out, anything at all
        on any thread (an auto-reply too: stricter than "away past that day", and nothing she would
        have to read)."""
        now, thread = self.now(), fu["thread_key"]
        if offer is None or offer["id"] not in [o["id"] for o in self.ledger.open_offers(thread, now)]:
            expired = offer is not None and not offer["used"] and offer["closed_reason"] is None
            return "expired" if expired else "offer_closed"
        if self.ledger.thread_held(thread):
            return "thread_held"
        if self.ledger.is_silenced(offer["sender"]):
            return "opted_out"
        if self.ledger.count_nudges(thread) - (1 if own is not None else 0) >= caps.NUDGES_PER_THREAD_EVER:
            return "already_nudged"
        if self.ledger.wrote_since(offer["sender"], offer["created"], but=fu["message_key"]):
            return "they_wrote"
        return None

    def sweep_nudge(self, fu):
        """One offer's follow-up. A nudge of hers that went or may have (a kill came before it was
        found in Sent) is looked up there and never sent again: found, the entry closes; absent, an
        error line and it closes unsent; a lookup that fails waits for her next pass. Else the entry
        closes with no nudge once nudge_rule says so, waits while it is not due, and then goes."""
        mid = compose.new_message_id(self.cfg.doris_addr, fu["message_key"], "T9", 1)
        offer, own = self.ledger.offer(fu["offer_id"]), self.ledger.send(mid)
        if own is not None and own["state"] != "pending":
            found = True if own["state"] == "confirmed" else self.look_in_sent(None, mid, "T9")
            if found is None:
                return self.nudge_waits(fu, "not_sent")
            if found:
                self.ledger.set_send_state(mid, "confirmed")
                return self.close_follow_up(fu, "followup.nudge_done")
            return self.nudge_unconfirmed(fu, mid)
        rule = self.nudge_rule(fu, offer, own)
        if rule is not None:
            return self.close_follow_up(fu, "followup.no_nudge", rule)
        if self.now() < fu["due"]:
            return None
        if self.model_down:                 # G4: while the model is down nudges wait
            return self.nudge_waits(fu, "model_down")
        action_id = self.ledger.enqueue_action(self.run_id, fu["message_key"], "nudge", {
            "template": "T9", "booking_id": None, "thread": fu["thread_key"], "offer_id": offer["id"]})
        self.ledger.start_action(action_id)
        outcome = "error"
        try:
            outcome = self.nudge(fu, offer, mid)
        except (Halted, Refused):
            outcome = None                  # left started: the halted line names it as dropped
            raise
        finally:
            if outcome is not None:
                self.ledger.finish_action(action_id, outcome)

    def nudge(self, fu, offer, mid):
        """The one nudge (T9) on an offer nobody answered, two business days on. Never a new time: the
        offer's own slots are read on the calendar now, any that is no longer available is dropped and
        closed, and the rest go again (the one-slot form when one is left; no nudge when none is). To
        the address the offer went to, on their thread, the owner on blind copy, through the gate like
        every send; her ledger marks the offer nudged before the send (A3). Not a new offer record,
        and the offer's expiry does not move. Returns the outcome its queue row keeps."""
        now, thread, sender, kept_ = self.now(), fu["thread_key"], offer["sender"], fu["detail"]
        who = holds.sender_name(kept_.get("name") or "", sender)
        try:
            events = self.read(None, min(s.start_utc for s in offer["slots"]), max(s.end_utc for s in offer["slots"]))
        except _CalendarDown:
            self.nudge_waits(fu, "calendar_down")
            return "waits:calendar_down"
        kept = [s for s in offer["slots"] if slots.available(events, s, now, written=True)[0]]
        if not kept:
            self.close_follow_up(fu, "followup.no_nudge", "no_time_left")
            return "no_nudge:no_time_left"
        length = offer["length_min"] or caps.DEFAULT_LENGTH_MIN
        why_ = why_mod.why("followup.nudge", n=len(kept))
        self.write("decided", {"step": "nudge", "action": "nudge", "template": "T9", "follow_up": self.entry_of(fu),
                               "thread": thread, "message": fu["message_key"], "sender": sender,
                               "kept": [_span(s) for s in kept],
                               "dropped": [_span(s) for s in offer["slots"] if s not in kept],
                               "checks": "this send passes the gate, G1 to G17, before it happens"}, why=why_)
        stand_in = types.SimpleNamespace(subject=kept_.get("subject") or "", from_addr=sender,
                                         message_id=kept_.get("message_id"), key=fu["message_key"],
                                         references=list(kept_.get("references") or []))
        tslots = {"slots": [s.start_utc for s in kept], "duration": length, "zone": kept_.get("zone")}
        try:
            raw, body = compose.reply(stand_in, "T9", tslots, self.cfg.doris_addr, mid, now)
            envelope = compose.reply_envelope(stand_in, self.cfg.owner_addr)
        except (templates.TemplateError, compose.ComposeError) as exc:
            self.error(None, "compose", exc, template="T9", follow_up=fu["id"])
            self.close_follow_up(fu, "followup.no_nudge", "gate_blocked", check="G8")
            return "no_nudge:gate_blocked"
        action = Action(kind="send_reply", message_key=fu["message_key"], thread_key=thread, template="T9",
                        slots=dict(tslots), to=[sender], bcc=[self.cfg.owner_addr], recipient_source="sender",
                        body_text=body, evidence={"length_min": length})
        g = self.check(None, action)
        if not g.passed:
            if g.first_fail == "G7" and self.ledger.count_nudges(thread) - (
                    1 if self.ledger.send(mid) is not None else 0) < caps.NUDGES_PER_THREAD_EVER:
                self.nudge_waits(fu, "gate_blocked")        # a send cap: it frees up, and she tries again
                return "waits:gate_blocked"
            self.close_follow_up(fu, "followup.no_nudge", "gate_blocked", check=g.first_fail or "G16")
            return "no_nudge:gate_blocked"
        if self.deliver(None, mid, "send_reply", "T9", sender, raw, body, envelope, tslots,
                        before_send=lambda: self.ledger.mark_nudged(offer["id"], kept, self.now()), thread=thread):
            self.summary.sends += 1
            self.close_follow_up(fu, "followup.nudge_done")
            self.say("%s  %s: nudge. T9 sent (%s). %s" % (templates.fmt_pacific(now), who,
                                                         templates.plain_name("T9"), why_["text"]))
            return "nudged"
        if (self.ledger.send(mid) or {}).get("state") == "pending":
            self.nudge_waits(fu, "not_sent")                # it surely did not go: tried again at her next pass
            return "waits:not_sent"
        self.nudge_unconfirmed(fu, mid)
        return "no_nudge:send_unconfirmed"

    def nudge_unconfirmed(self, fu, mid):
        """Her nudge went or may have gone and Sent shows no copy: an error line, and it is never sent
        again (ruling 8). The offer stays open, with the times the nudge listed."""
        self.error(None, "nudge", SendUnknown("her nudge is not in Sent after it went or may have gone; "
                                               "it is never sent again"), template="T9", follow_up=fu["id"],
                   message_id=mid)
        self.close_follow_up(fu, "followup.no_nudge", "send_unconfirmed")
        self.say("%s  Nudge not confirmed in Sent (error line written): it may have gone, so it is never sent "
                 "again." % (templates.fmt_pacific(self.now()),))

    def nudge_waits(self, fu, rule):
        """A nudge that is due and cannot go yet: said once a run for each reason, tried again at her
        next pass, and closed by nudge_rule once the offer expires."""
        if self.nudge_said.get(fu["id"]) == rule:
            return None
        self.nudge_said[fu["id"]] = rule
        why_ = why_mod.why("followup.nudge_waits", rule=rule)
        self.write("decided", {"step": "nudge", "action": "nudge_waits", "follow_up": self.entry_of(fu), "rule": rule,
                               "thread": fu["thread_key"], "message": fu["message_key"], "sender": fu["sender"]},
                   why=why_)
        self.say("%s  A nudge is due and waits (%s). She tries again at her next cycle."
                 % (templates.fmt_pacific(self.now()), why_mod.FOLLOW_UP_WORDS.get(rule, rule)))
        return None

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
        if e.offers_superseded:
            payload["offers_superseded"] = e.offers_superseded
        if e.follow_up is not None:
            payload["follow_up"] = e.follow_up
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
        sent = ""
        if template != "-":
            name = templates.plain_name(template)
            sent = " %s sent%s." % (template, " (%s)" % name if name else "")
        cost = ""
        if e.cost is not None and self.cfg.model_source != "stub":
            cost = " Model cost $%.4f%s." % (e.cost, " (recorded)" if self.cfg.model_source == "replayed" else "")
        staged = ""
        fault = (e.facts or {}).get("fault")
        if fault:
            done = "retried once" if e.retried else "recovered" if e.recovered else "no effect"
            staged = " Staged fault %s: %s." % (fault, done)
        text = why_["text"]
        if self.cfg.model_source == "stub":     # no model ran: the terminal says stand-in, as replay does
            text = stand_in_words(text)         # (the receipt keeps the listed why, word for word)
        follow = ""
        if e.follow_up is not None and e.follow_up["kind"] == "nudge":
            follow = " Follow-up filed: one nudge %s if no reply by then." % templates.fmt_pacific(e.follow_up["due"])
        elif e.follow_up is not None:
            follow = " Follow-up filed: this booking is checked on the calendar every cycle until it starts."
        return "%s  %s: %s.%s %s%s%s%s" % (templates.fmt_pacific(self.now()), who, outcome, sent, text, follow, staged,
                                           cost)

    # --- the digest ----------------------------------------------------------------------------
    def end_of_run_digest(self):
        """The demo's S2 at the end of the run, at 5:00 PM Pacific or later, counted over the whole day
        as the daily digest counts it (digest_window): every line this mode's runs wrote since the last
        digest the server took. A demo stopped part way and run again after `resume` so reports the
        day (13 handled, 2 held), not only what the run after the stop did, and its first line says
        what the end-of-day summary says. Receipts that cannot be read send nothing: an error line,
        said in the terminal (Contract 7)."""
        if self.t is not None:
            day = zones.to_local(self.t, "Pacific").date()
            self.t = max(self.t, zones.to_utc(_dt.datetime.combine(day, DIGEST_AT), "Pacific"))
        now = self.now()
        try:
            lines, verified = self.digest_window()
        except (Halted, Refused):
            raise
        except Exception as exc:
            self.error(None, "digest", exc, template="S2")
            self.say("%s  Digest not sent: %s (error line written)." % (templates.fmt_pacific(now),
                                                                      type(exc).__name__))
            return
        self.send_digest(lines, verified, now)

    def daily_digest(self):
        """A run until stopped (`run`): at her first pass at or after 5:00 PM Pacific, that Pacific
        date's S2, once. The ledger answers first: once the date's digest went or may have gone
        (sending, unknown, handed_over or confirmed: decision 76; review 2, finding 1), a pass reads,
        writes and sends nothing more for it, whatever restarts or kills came between. Receipts or a
        ledger that cannot answer send nothing: an error line, and her next pass tries again (Contract 7)."""
        if not (self.cfg.digest and self.cfg.cycles is None):
            return
        now = self.now()                # one instant: the date she checks is the date she sends
        if zones.to_local(now, "Pacific").time() < DIGEST_AT:
            return
        try:
            sent = self.ledger.send(compose.new_message_id(self.cfg.doris_addr, compose.digest_key(now), "S2", 1))
            if sent is not None and sent["state"] in ledger_mod.NEVER_AGAIN_STATES:
                return
            lines, verified = self.digest_window()
            self.send_digest(lines, verified, now)
        except (Halted, Refused):
            raise
        except Exception as exc:
            self.error(None, "digest", exc, template="S2")
            self.say("%s  Digest not sent: %s (error line written). She tries again at her next pass."
                     % (templates.fmt_pacific(now), type(exc).__name__))

    def digest_window(self):
        """(receipt lines, verified) for the daily digest: every line this mode's runs wrote after the
        last digest the server took (before the first, all of them), oldest first, so mail after one
        digest is in the next and nothing falls between two. The run files are read back from this one
        until that digest's send line turns up. verified only when every file read verifies; this run's,
        still open, without its closing line. A file that cannot be read raises, never reads as empty."""
        chunks, verified = [], True
        for run in reversed(self.ledger.runs()):
            path = os.path.join(self.cfg.state_dir, "runs", run["run_id"], "receipts.jsonl")
            lines = receipts.read_lines(path)
            verified = receipts.verify(path, finished=run["run_id"] != self.run_id).ok and verified
            cut = next((i for i in range(len(lines) - 1, -1, -1) if self.digest_taken(lines[i])), None)
            chunks.append(lines if cut is None else lines[cut + 1:])
            if cut is not None:
                break
        return [line for chunk in reversed(chunks) for line in chunk], verified

    def digest_taken(self, line):
        """A digest's send line, for a digest the server took (one it refused is no boundary)."""
        p = line.get("payload") or {}
        if line.get("kind") != "called" or p.get("call") != "send" or p.get("target") != "S2":
            return False
        sent = self.ledger.send(p["message_id"])
        return sent is not None and sent["state"] in ledger_mod.SENT_STATES

    def send_digest(self, lines, verified, now):
        """S2 to the owner, counted from these receipt lines (verified: the files they came from
        verify), dated `now`: through the gate, to the owner only, sent once. The one way a digest goes."""
        ds = digest.build(lines, now, verified=verified)
        mid = compose.new_message_id(self.cfg.doris_addr, compose.digest_key(now), "S2", 1)
        try:
            raw, body = compose.owner_notice("S2", ds, self.cfg.doris_addr, self.cfg.owner_addr, mid, now)
        except (templates.TemplateError, compose.ComposeError) as exc:
            self.error(None, "digest", exc, template="S2")
            return
        action = Action(kind="send_digest", message_key="", thread_key="", template="S2", slots=ds,
                        to=[self.cfg.owner_addr], recipient_source="owner", body_text=body)
        if self.check(None, action).passed:
            before = self.ledger.send(mid)
            # recorded by an earlier run and past pending: deliver sends nothing, it only finds her copy
            earlier = before is not None and before["state"] != "pending"
            if self.deliver(None, mid, "send_digest", "S2", self.cfg.owner_addr, raw, body, [self.cfg.owner_addr],
                            ds):
                if earlier:             # this date's digest went in an earlier run: no send is claimed
                    self.say("%s  Digest already sent to the owner earlier today; not sent twice."
                             % (templates.fmt_pacific(now),))
                else:
                    self.say("%s  Digest sent to the owner: Handled %d, held %d, human approvals %d."
                             % (templates.fmt_pacific(now), ds["handled"], ds["held"], ds["approvals"]))

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
        if fault in TIMEOUT_FAULTS:         # said before the wait starts, so the silence never reads as a hang
            try:
                waits = float(params.get("seconds", getattr(target, "timeout_s", 0.0)))
            except (TypeError, ValueError):
                return
            self.say("%s  Staged fault %s (simulated): the calendar will not answer her next call for %g s. "
                     "She waits, then retries once after %g s." % (templates.fmt_pacific(self.now()), fault, waits,
                                                                   self.wait_s))

    def disarm(self):
        for adapter in (self.calendar, self.mail):
            if hasattr(adapter, "disarm"):
                adapter.disarm()


def stand_in_words(text):
    """A why's text for the terminal of a run the built-in stand-in (stub) read: where the listed
    text says the model read or agreed, it says the stand-in. Spend and cap wording is left as it is."""
    for said, shown in (("parser and model agree", "parser and stand-in agree"), ("The model", "The stand-in"),
                        ("the model", "the stand-in")):
        text = text.replace(said, shown)
    return text


def run(cfg):
    """One run in this process: cfg.cycles passes over the inbox (None: until stopped). Raises
    Refused when it cannot start, and Halted under the kill switch (the parent writes the halted line);
    a worker whose supervisor is gone writes that line and its summary itself and returns."""
    r = _Run(cfg)
    try:
        r.open()
        try:
            r.start()
            r.passes()
            if cfg.digest:              # a finite run (the demo); a run until stopped sends each day's in passes()
                r.end_of_run_digest()
        except Halted as h:
            r.summary.halted = h.cause
            if cfg.supervised and not r.supervisor_gone():
                raise                       # the supervisor writes the halted line once she is dead
            r.write_halted(h.cause)         # unsupervised, or her supervisor is gone: nobody else will
        except MailError as exc:
            r.error(None, "fetch", exc)     # a finite run's one refused read ends it, and the summary says so
            r.summary.read_failed = True
        r.finish()
    finally:
        r.close_handles()
    return r.summary
