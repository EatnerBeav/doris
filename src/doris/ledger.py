"""The ledger: her state in one SQLite file per mode (owner: state).

Rules this file keeps:
- Every write is committed before the call returns (autocommit, or one BEGIN IMMEDIATE ...
  COMMIT for a write of several rows), with synchronous=FULL and a rollback journal, so a
  hard kill loses nothing a call already returned. "Queued means written down" (A3).
- Keys are inbound Message-IDs (Message.key). Message-IDs, thread keys and her sent
  Message-IDs are compared with outer spaces and one pair of angle brackets removed (norm_id),
  so "<a@b>" and "a@b" are one message; rows return the value as it was given.
- Every time is stored as an ISO UTC string "YYYY-MM-DDTHH:MM:SSZ" and comes back as an aware
  UTC datetime. Every function takes `now` from the caller; nothing here reads the wall clock.
- A skipped sensitive message is keyed by sha256 of its Message-ID; only the sender, the hashes
  and the check name are kept, and any row recorded for it before the screen is scrubbed
  (secure_delete is on, so the Message-ID's bytes do not linger in the file).
- One process at a time (the lock is the supervisor's); one thread per Ledger object.
"""

import contextlib
import datetime as _dt
import hashlib
import json
import os
import sqlite3
from typing import List, Optional

from . import caps
from .paths import MODES
from .receipts import canonical, iso, jsonable
from .shapes import Slot

SCHEMA_VERSION = 2   # 2: an offer names the send that carries it (offers.message_id)
MESSAGE_STATUSES = ("fetched", "processing", "done", "superseded", "dropped", "held", "quarantined")
UNPROCESSED = ("fetched", "processing")
SEND_KINDS = ("send_reply", "send_hold_notice", "send_digest")
SEND_STATES = ("pending", "confirmed", "unknown", "handed_over")
# Decision 76: a send SMTP accepted (250) is handed_over before the Sent look and is never sent
# again; from there, and from confirmed, a send only ever moves up to confirmed.
SENT_STATES = ("handed_over", "confirmed")
BOOKING_STATES = ("intent", "written", "confirmed", "cancelled", "gone", "collision_unresolved", "removed",
                  "unconfirmed", "blocked")   # blocked: the gate stopped it, never written
LIVE_BOOKING_STATES = ("intent", "written", "confirmed", "unconfirmed", "collision_unresolved")
UNFINISHED_BOOKING_STATES = ("intent", "written", "unconfirmed")
ON_CALENDAR_STATES = ("written", "confirmed")

_UTC = _dt.timezone.utc
_ISO = "%Y-%m-%dT%H:%M:%SZ"

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)",
    """CREATE TABLE IF NOT EXISTS runs (
        run_id TEXT PRIMARY KEY, mode TEXT NOT NULL, started_utc TEXT NOT NULL, ended_utc TEXT)""",
    """CREATE TABLE IF NOT EXISTS fetches (
        server_id TEXT PRIMARY KEY, message_key TEXT, run_id TEXT, fetched_utc TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS messages (
        key_norm TEXT PRIMARY KEY, key TEXT NOT NULL, thread_key TEXT, thread_norm TEXT,
        sender TEXT NOT NULL, received_utc TEXT NOT NULL, status TEXT NOT NULL, outcome TEXT, run_id TEXT)""",
    """CREATE TABLE IF NOT EXISTS sensitive_skips (
        key_sha256 TEXT PRIMARY KEY, sender TEXT NOT NULL, message_sha256 TEXT NOT NULL,
        check_name TEXT NOT NULL, skipped_utc TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS sends (
        mid_norm TEXT PRIMARY KEY, message_id TEXT NOT NULL, kind TEXT NOT NULL, template TEXT,
        thread_key TEXT, thread_norm TEXT, to_addr TEXT NOT NULL, body_sha256 TEXT, state TEXT NOT NULL,
        created_utc TEXT NOT NULL, run_id TEXT)""",
    """CREATE TABLE IF NOT EXISTS silences (
        addr TEXT PRIMARY KEY, phrase TEXT, message_key TEXT, silenced_utc TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS offers (
        id INTEGER PRIMARY KEY AUTOINCREMENT, thread_key TEXT NOT NULL, thread_norm TEXT NOT NULL,
        sender TEXT NOT NULL, length_min INTEGER NOT NULL, created_utc TEXT NOT NULL, expires_utc TEXT NOT NULL,
        used INTEGER NOT NULL DEFAULT 0, closed_reason TEXT, mid_norm TEXT)""",
    "CREATE UNIQUE INDEX IF NOT EXISTS offers_by_send ON offers (mid_norm) WHERE mid_norm IS NOT NULL",
    """CREATE TABLE IF NOT EXISTS offer_slots (
        offer_id INTEGER NOT NULL, idx INTEGER NOT NULL, start_utc TEXT NOT NULL, end_utc TEXT NOT NULL,
        PRIMARY KEY (offer_id, idx))""",
    """CREATE TABLE IF NOT EXISTS bookings (
        booking_id TEXT PRIMARY KEY, message_key TEXT NOT NULL, thread_key TEXT NOT NULL,
        thread_norm TEXT NOT NULL, sender TEXT NOT NULL, start_utc TEXT NOT NULL, end_utc TEXT NOT NULL,
        state TEXT NOT NULL, created_utc TEXT NOT NULL, run_id TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS holds (
        id INTEGER PRIMARY KEY AUTOINCREMENT, thread_key TEXT NOT NULL, thread_norm TEXT NOT NULL,
        message_key TEXT, reason_code TEXT NOT NULL, notice_message_id TEXT, created_utc TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS actions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, message_key TEXT, kind TEXT NOT NULL,
        detail TEXT NOT NULL, state TEXT NOT NULL, outcome TEXT, drop_cause TEXT)""",
    """CREATE TABLE IF NOT EXISTS spend (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, usd REAL NOT NULL)""",
    # live only: when the ledger was started fresh. Her live mail is read from then on (ruling 8).
    "CREATE TABLE IF NOT EXISTS fresh_start (started_utc TEXT NOT NULL)",
)


class LedgerError(RuntimeError):
    """The ledger file cannot be used: not a ledger, damaged, or another schema version."""


def norm_id(value) -> str:
    """A Message-ID or thread key for comparing: outer spaces and one pair of <> removed, case kept."""
    if not isinstance(value, str):
        raise ValueError("a key must be a string, not %r" % (value,))
    text = value.strip()
    if len(text) >= 2 and text[0] == "<" and text[-1] == ">":
        text = text[1:-1].strip()
    if not text:
        raise ValueError("a key cannot be empty")
    return text


def _opt_norm(value):
    return None if value is None else norm_id(value)


def key_sha256(message_key) -> str:
    """How a skipped sensitive message is keyed: sha256 of its Message-ID (norm_id form), hex."""
    return hashlib.sha256(norm_id(message_key).encode("utf-8")).hexdigest()


def _ts(value, name="time"):
    if not isinstance(value, _dt.datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("%s must be an aware datetime, not %r" % (name, value))
    return iso(value)


def _when(text):
    return None if text is None else _dt.datetime.strptime(text, _ISO).replace(tzinfo=_UTC)


def _addr(value, allow_empty=False):
    if not isinstance(value, str):
        raise ValueError("an address must be a string, not %r" % (value,))
    text = value.strip().lower()
    if not text and not allow_empty:
        raise ValueError("an address cannot be empty")
    return text


def _text(value, name):
    if not isinstance(value, str) or not value:
        raise ValueError("%s must be a non-empty string" % name)
    return value


def _opt_text(value, name):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("%s must be a string or None" % name)
    return value


def start_fresh(path, now):
    """Ruling 8's fresh start: a new ledger at path that already records `now` as its start, so a
    live ledger that exists always says from when her mail is read. It is built under a temporary
    name and moved into place whole; a ledger already at path is refused (FileExistsError) and left
    alone. The caller holds the ledger lock, so no second fresh start can race this one."""
    path = os.fspath(path)
    started = _ts(now, "now")
    if os.path.exists(path):
        raise FileExistsError("a ledger is already at %s" % (path,))
    building = path + ".fresh"
    for leftover in (building, building + "-journal"):      # a fresh start cut short before
        if os.path.exists(leftover):
            os.remove(leftover)
    db = Ledger(building)
    try:
        db._con.execute("INSERT INTO fresh_start (started_utc) VALUES (?)", (started,))
    finally:
        db.close()
    os.replace(building, path)


class Ledger:
    def __init__(self, path, create=True):
        self._path = os.fspath(path)
        self._run = None
        if not os.path.exists(self._path):
            if not create:
                raise FileNotFoundError("no ledger at %s" % (self._path,))
            folder = os.path.dirname(self._path)
            if folder:
                os.makedirs(folder, exist_ok=True)
        try:
            self._con = sqlite3.connect(self._path, timeout=10.0, isolation_level=None)
        except sqlite3.Error as exc:
            raise LedgerError("cannot open the ledger %s: %s" % (self._path, exc))
        self._con.row_factory = sqlite3.Row
        try:
            self._setup()
        except BaseException:
            self._con.close()
            raise

    def _setup(self):
        con = self._con
        try:
            con.execute("PRAGMA journal_mode=DELETE").fetchall()
            con.execute("PRAGMA synchronous=FULL")
            con.execute("PRAGMA secure_delete=ON")
            check = con.execute("PRAGMA quick_check").fetchone()
            if check is None or check[0] != "ok":
                raise LedgerError("the ledger %s is damaged (quick_check: %s)" % (self._path, check and check[0]))
            with self._tx():
                for statement in _SCHEMA:
                    con.execute(statement)
                versions = [r[0] for r in con.execute("SELECT version FROM schema_version")]
                if not versions:
                    con.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
                elif versions != [SCHEMA_VERSION]:
                    raise LedgerError("the ledger %s has schema version %s; this Doris reads version %d"
                                      % (self._path, versions, SCHEMA_VERSION))
        except sqlite3.DatabaseError as exc:
            raise LedgerError("the ledger %s cannot be used: %s" % (self._path, exc))

    @contextlib.contextmanager
    def _tx(self):
        """One transaction for a write of several rows: all of it commits, or none of it."""
        self._con.execute("BEGIN IMMEDIATE")
        try:
            yield self._con
            self._con.execute("COMMIT")
        except BaseException:
            if self._con.in_transaction:      # a failed COMMIT leaves it open: never leave it half-done
                self._con.execute("ROLLBACK")
            raise

    def _one(self, sql, args=()):
        return self._con.execute(sql, args).fetchone()

    def _all(self, sql, args=()):
        return self._con.execute(sql, args).fetchall()

    @property
    def path(self):
        return self._path

    @property
    def run_id(self):
        """The run started on this Ledger object and not yet ended, or None."""
        return self._run

    def close(self):
        if self._con is not None:
            self._con.close()
            self._con = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # --- Fetch bookkeeping ---------------------------------------------------------------
    def server_id_seen(self, server_id) -> bool:
        return self._one("SELECT 1 FROM fetches WHERE server_id = ?", (_text(server_id, "server_id"),)) is not None

    def record_fetch(self, server_id, message_key, run_id, now):
        """The server's own ID, for fetching and filing only. A key already skipped as sensitive is not stored."""
        key = norm_id(message_key)
        if self._one("SELECT 1 FROM sensitive_skips WHERE key_sha256 = ?", (key_sha256(key),)):
            key = None
        self._con.execute("INSERT OR IGNORE INTO fetches (server_id, message_key, run_id, fetched_utc) "
                          "VALUES (?, ?, ?, ?)", (_text(server_id, "server_id"), key, run_id, _ts(now, "now")))

    # --- Messages --------------------------------------------------------------------------
    @staticmethod
    def _message(row):
        return {"key": row["key"], "thread_key": row["thread_key"], "sender": row["sender"],
                "received_utc": _when(row["received_utc"]), "status": row["status"], "outcome": row["outcome"],
                "run_id": row["run_id"]}

    def message_seen(self, key) -> bool:
        """True when this Message-ID already ran (or is running), including a sensitive skip."""
        k = norm_id(key)
        if self._one("SELECT 1 FROM messages WHERE key_norm = ?", (k,)):
            return True
        return self._one("SELECT 1 FROM sensitive_skips WHERE key_sha256 = ?", (key_sha256(k),)) is not None

    def record_message(self, key, thread_key, sender, received_utc, status, run_id=None):
        """A new inbound message. A Message-ID already in the ledger is refused (it runs once).
        run_id defaults to the run started on this object."""
        if status not in MESSAGE_STATUSES:
            raise ValueError("unknown message status %r" % (status,))
        k = norm_id(key)
        row = (k, key, _opt_text(thread_key, "thread_key"), _opt_norm(thread_key), _addr(sender, allow_empty=True),
               _ts(received_utc, "received_utc"), status, run_id if run_id is not None else self._run)
        with self._tx() as con:
            if (con.execute("SELECT 1 FROM messages WHERE key_norm = ?", (k,)).fetchone()
                    or con.execute("SELECT 1 FROM sensitive_skips WHERE key_sha256 = ?", (key_sha256(k),)).fetchone()):
                raise ValueError("Message-ID %s is already in the ledger: a message runs once" % (k,))
            con.execute("INSERT INTO messages (key_norm, key, thread_key, thread_norm, sender, received_utc, status, "
                        "run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", row)

    def set_message_status(self, key, status, outcome=None):
        if status not in MESSAGE_STATUSES:
            raise ValueError("unknown message status %r" % (status,))
        cur = self._con.execute("UPDATE messages SET status = ?, outcome = ? WHERE key_norm = ?",
                                (status, _opt_text(outcome, "outcome"), norm_id(key)))
        if cur.rowcount == 0:
            raise KeyError(key)

    def message(self, key) -> Optional[dict]:
        row = self._one("SELECT * FROM messages WHERE key_norm = ?", (norm_id(key),))
        return None if row is None else self._message(row)

    def thread_of_message(self, key) -> Optional[str]:
        if not isinstance(key, str) or not key.strip("<> \t"):
            return None
        row = self._one("SELECT thread_key FROM messages WHERE key_norm = ?", (norm_id(key),))
        return None if row is None else row["thread_key"]

    def unprocessed_messages(self, run_id=None, sender=None) -> List[dict]:
        """Messages fetched or processing and not finished, oldest first. With run_id: recorded in that run,
        or fetched in it. With sender: from that address (a stop drops the sender's queued requests)."""
        sql = "SELECT * FROM messages m WHERE m.status IN ('fetched', 'processing')"
        args = []
        if run_id is not None:
            sql += (" AND (m.run_id = ? OR EXISTS (SELECT 1 FROM fetches f WHERE f.message_key = m.key_norm"
                    " AND f.run_id = ?))")
            args += [run_id, run_id]
        if sender is not None:
            sql += " AND m.sender = ?"
            args.append(_addr(sender))
        return [self._message(r) for r in self._all(sql + " ORDER BY m.received_utc, m.rowid", args)]

    # --- Sensitive mail ----------------------------------------------------------------------
    def record_sensitive_skip(self, message_key, sender, message_sha256, check, now):
        """Screen 5 skipped this message: keep only the sender, the hashes and the check name, keyed by
        sha256 of its Message-ID. Any fetch or message row recorded for it earlier loses the Message-ID."""
        k = norm_id(message_key)
        row = (key_sha256(k), _addr(sender, allow_empty=True), _text(message_sha256, "message_sha256"),
               _text(check, "check"), _ts(now, "now"))
        with self._tx() as con:
            con.execute("INSERT OR IGNORE INTO sensitive_skips (key_sha256, sender, message_sha256, check_name, "
                        "skipped_utc) VALUES (?, ?, ?, ?, ?)", row)
            con.execute("UPDATE fetches SET message_key = NULL WHERE message_key = ?", (k,))
            con.execute("DELETE FROM messages WHERE key_norm = ?", (k,))

    def sensitive_skips(self) -> List[dict]:
        return [{"key_sha256": r["key_sha256"], "sender": r["sender"], "message_sha256": r["message_sha256"],
                 "check": r["check_name"], "skipped_utc": _when(r["skipped_utc"])}
                for r in self._all("SELECT * FROM sensitive_skips ORDER BY skipped_utc, rowid")]

    # --- Her sends ---------------------------------------------------------------------------
    @staticmethod
    def _send(row):
        return {"message_id": row["message_id"], "kind": row["kind"], "template": row["template"],
                "thread_key": row["thread_key"], "to_addr": row["to_addr"], "body_sha256": row["body_sha256"],
                "state": row["state"], "created": _when(row["created_utc"]), "run_id": row["run_id"]}

    def record_send(self, message_id, kind, template, thread_key, to_addr, body_sha256, now, run_id) -> bool:
        """Her outgoing message, recorded (state pending) before it is sent. The same send recorded again
        is a no-op (returns False, state kept); a different send under the same Message-ID is refused."""
        if kind not in SEND_KINDS:
            raise ValueError("send kind must be one of %s, not %r" % (", ".join(SEND_KINDS), kind))
        mid = norm_id(message_id)
        same = (kind, _opt_text(template, "template"), _opt_norm(thread_key), _addr(to_addr),
                _opt_text(body_sha256, "body_sha256"))
        created = _ts(now, "now")
        with self._tx() as con:
            old = con.execute("SELECT kind, template, thread_norm, to_addr, body_sha256 FROM sends WHERE mid_norm = ?",
                              (mid,)).fetchone()
            if old is not None:
                if tuple(old) != same:
                    raise ValueError("Message-ID %s is already recorded for a different send" % (mid,))
                return False
            con.execute("INSERT INTO sends (mid_norm, message_id, kind, template, thread_key, thread_norm, to_addr, "
                        "body_sha256, state, created_utc, run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                        (mid, message_id, kind, same[1], thread_key, same[2], same[3], same[4], created, run_id))
        return True

    def reset_send(self, message_id, body_sha256):
        """A new body for a send that never reached Sent (state pending or unknown, and the loop looked
        first): it goes out under the same Message-ID (ruling 8). A send the server took (handed_over)
        or that is confirmed in Sent never changes."""
        with self._tx() as con:
            row = con.execute("SELECT state FROM sends WHERE mid_norm = ?", (norm_id(message_id),)).fetchone()
            if row is None:
                raise KeyError(message_id)
            if row["state"] in SENT_STATES:
                raise ValueError("send %s is %s; it never changes" % (norm_id(message_id), row["state"]))
            con.execute("UPDATE sends SET body_sha256 = ? WHERE mid_norm = ?",
                        (_text(body_sha256, "body_sha256"), norm_id(message_id)))

    def set_send_state(self, message_id, state):
        """A send's state. One the server took (handed_over) only moves up to confirmed, and a confirmed
        one stays: any other state asked for later is not written (decision 76)."""
        if state not in SEND_STATES:
            raise ValueError("send state must be one of %s, not %r" % (", ".join(SEND_STATES), state))
        mid = norm_id(message_id)
        with self._tx() as con:
            row = con.execute("SELECT state FROM sends WHERE mid_norm = ?", (mid,)).fetchone()
            if row is None:
                raise KeyError(message_id)
            if row["state"] == "confirmed" or (row["state"] == "handed_over" and state != "confirmed"):
                return
            con.execute("UPDATE sends SET state = ? WHERE mid_norm = ?", (state, mid))

    def send(self, message_id) -> Optional[dict]:
        row = self._one("SELECT * FROM sends WHERE mid_norm = ?", (norm_id(message_id),))
        return None if row is None else self._send(row)

    def is_her_message_id(self, mid) -> bool:
        if not isinstance(mid, str) or not mid.strip("<> \t"):
            return False
        return self._one("SELECT 1 FROM sends WHERE mid_norm = ?", (norm_id(mid),)) is not None

    def thread_of_sent(self, mid) -> Optional[str]:
        if not isinstance(mid, str) or not mid.strip("<> \t"):
            return None
        row = self._one("SELECT thread_key FROM sends WHERE mid_norm = ?", (norm_id(mid),))
        return None if row is None else row["thread_key"]

    def addresses_written_on_thread(self, thread_key) -> set:
        """Every address she sent a reply to on this thread (hold notices and digests go to the owner)."""
        return {r["to_addr"] for r in self._all("SELECT to_addr FROM sends WHERE kind = 'send_reply' AND thread_norm = ?",
                                                (norm_id(thread_key),))}

    def count_prospect_sends(self, since=None, run_id=None, thread_key=None) -> int:
        """Replies to prospects in any state (pending and unknown count too: fail closed), recorded at or
        after `since`, in `run_id`, on `thread_key`, each filter optional."""
        sql, args = "SELECT COUNT(*) FROM sends WHERE kind = 'send_reply'", []
        if since is not None:
            sql += " AND created_utc >= ?"
            args.append(_ts(since, "since"))
        if run_id is not None:
            sql += " AND run_id = ?"
            args.append(run_id)
        if thread_key is not None:
            sql += " AND thread_norm = ?"
            args.append(norm_id(thread_key))
        return self._one(sql, args)[0]

    def count_hold_notices(self, since) -> int:
        return self._one("SELECT COUNT(*) FROM sends WHERE kind = 'send_hold_notice' AND created_utc >= ?",
                         (_ts(since, "since"),))[0]

    def unfinished_sends(self) -> List[dict]:
        """Sends pending or unknown, oldest first: looked up in Sent before anything else on restart."""
        return [self._send(r) for r in self._all("SELECT * FROM sends WHERE state IN ('pending', 'unknown') "
                                                 "ORDER BY rowid")]

    # --- Stop list -----------------------------------------------------------------------------
    def silence(self, addr, phrase, message_key, now):
        """Silence one sender (first silence kept). The owner's address is the screens' to refuse."""
        self._con.execute("INSERT OR IGNORE INTO silences (addr, phrase, message_key, silenced_utc) VALUES (?, ?, ?, ?)",
                          (_addr(addr), _opt_text(phrase, "phrase"), _opt_text(message_key, "message_key"),
                           _ts(now, "now")))

    def is_silenced(self, addr) -> bool:
        return self._one("SELECT 1 FROM silences WHERE addr = ?", (_addr(addr, allow_empty=True),)) is not None

    def silenced(self) -> List[str]:
        """Every silenced address, in the order they opted out."""
        return [r["addr"] for r in self._all("SELECT addr FROM silences ORDER BY silenced_utc, rowid")]

    # --- Offers (ruling 6) -------------------------------------------------------------------
    @staticmethod
    def _offer_rows(slots, length_min, now, expires):
        """The checked slot rows and times of an offer: 1 to 3 slots, a length allowed, expiring after
        now and at most caps.OFFER_EXPIRY later."""
        slots = list(slots)
        if not 1 <= len(slots) <= caps.SLOTS_PER_OFFER:
            raise ValueError("an offer holds 1 to %d slots, not %d" % (caps.SLOTS_PER_OFFER, len(slots)))
        rows = []
        for index, s in enumerate(slots):
            start, end = _ts(s.start_utc, "slot start"), _ts(s.end_utc, "slot end")
            if not s.start_utc < s.end_utc:
                raise ValueError("a slot must end after it starts")
            rows.append((index, start, end))
        if length_min is not None and (type(length_min) is not int
                                       or not caps.MIN_LENGTH_MIN <= length_min <= caps.MAX_LENGTH_MIN):
            raise ValueError("offer length must be %d to %d minutes, not %r"
                             % (caps.MIN_LENGTH_MIN, caps.MAX_LENGTH_MIN, length_min))
        created, until = _ts(now, "now"), _ts(expires, "expires")
        if not now < expires <= now + caps.OFFER_EXPIRY:
            raise ValueError("an offer expires after now and within %s" % (caps.OFFER_EXPIRY,))
        return rows, created, until

    def create_offer(self, thread_key, sender, slots, length_min, now, expires, message_id=None) -> int:
        """One offer record: 1 to 3 absolute slots, expiring after now and at most caps.OFFER_EXPIRY later.
        A thread gets two offers, ever (the law): a third is refused here as well as held by policy.
        message_id names the send that carries it: one offer per send."""
        rows, created, until = self._offer_rows(slots, length_min, now, expires)
        thread = norm_id(thread_key)
        mid = None if message_id is None else norm_id(message_id)
        with self._tx() as con:
            count = con.execute("SELECT COUNT(*) FROM offers WHERE thread_norm = ?", (thread,)).fetchone()[0]
            if count >= caps.OFFERS_PER_THREAD_EVER:
                raise ValueError("thread %s already has %d offers; the law allows %d, ever"
                                 % (thread, count, caps.OFFERS_PER_THREAD_EVER))
            if mid is not None and con.execute("SELECT 1 FROM offers WHERE mid_norm = ?", (mid,)).fetchone():
                raise ValueError("send %s already carries an offer" % (mid,))
            cur = con.execute("INSERT INTO offers (thread_key, thread_norm, sender, length_min, created_utc, expires_utc, "
                              "mid_norm) VALUES (?, ?, ?, ?, ?, ?, ?)",
                              (thread_key, thread, _addr(sender), length_min, created, until, mid))
            offer_id = cur.lastrowid
            con.executemany("INSERT INTO offer_slots (offer_id, idx, start_utc, end_utc) VALUES (?, ?, ?, ?)",
                            [(offer_id,) + r for r in rows])
        return offer_id

    def offer_for_send(self, message_id) -> Optional[dict]:
        """The offer the send with this Message-ID carries, or None."""
        row = self._one("SELECT * FROM offers WHERE mid_norm = ?", (norm_id(message_id),))
        return None if row is None else self._offer(row)

    def set_offer_slots(self, offer_id, slots, now, expires):
        """New times for an offer whose send never went out (ruling 8: the loop looked in Sent first).
        Still one offer: the thread's count does not change."""
        rows, created, until = self._offer_rows(slots, None, now, expires)
        with self._tx() as con:
            if con.execute("SELECT 1 FROM offers WHERE id = ?", (offer_id,)).fetchone() is None:
                raise KeyError(offer_id)
            con.execute("UPDATE offers SET created_utc = ?, expires_utc = ? WHERE id = ?", (created, until, offer_id))
            con.execute("DELETE FROM offer_slots WHERE offer_id = ?", (offer_id,))
            con.executemany("INSERT INTO offer_slots (offer_id, idx, start_utc, end_utc) VALUES (?, ?, ?, ?)",
                            [(offer_id,) + r for r in rows])

    def _offer(self, row):
        slots = [Slot(_when(s["start_utc"]), _when(s["end_utc"]))
                 for s in self._all("SELECT start_utc, end_utc FROM offer_slots WHERE offer_id = ? ORDER BY idx",
                                    (row["id"],))]
        return {"id": row["id"], "thread_key": row["thread_key"], "sender": row["sender"], "slots": slots,
                "length_min": row["length_min"], "created": _when(row["created_utc"]),
                "expires": _when(row["expires_utc"])}

    def open_offers(self, thread_key, now) -> List[dict]:
        """Offers on the thread not used, not closed and not expired at `now`, oldest first. Each: id,
        thread_key, sender, slots (Slot, in offered order), length_min, created, expires."""
        rows = self._all("SELECT * FROM offers WHERE thread_norm = ? AND used = 0 AND closed_reason IS NULL "
                         "AND expires_utc > ? ORDER BY id", (norm_id(thread_key), _ts(now, "now")))
        return [self._offer(r) for r in rows]

    def offer_count(self, thread_key) -> int:
        """Offer records on the thread, ever: used, closed and expired ones included."""
        return self._one("SELECT COUNT(*) FROM offers WHERE thread_norm = ?", (norm_id(thread_key),))[0]

    def use_offer(self, offer_id):
        """An offer is used once: a used or closed offer is refused."""
        with self._tx() as con:
            row = con.execute("SELECT used, closed_reason FROM offers WHERE id = ?", (offer_id,)).fetchone()
            if row is None:
                raise KeyError(offer_id)
            if row["used"] or row["closed_reason"] is not None:
                raise ValueError("offer %s is already %s" % (offer_id, "used" if row["used"] else "closed"))
            con.execute("UPDATE offers SET used = 1 WHERE id = ?", (offer_id,))

    def close_offers(self, thread_key, reason) -> int:
        """Close every open offer on the thread (a plain no, a stop, a move...). Returns how many."""
        return self._con.execute("UPDATE offers SET closed_reason = ? WHERE thread_norm = ? AND used = 0 "
                                 "AND closed_reason IS NULL", (_text(reason, "reason"), norm_id(thread_key))).rowcount

    def offer_holding(self, thread_key, slot) -> Optional[int]:
        """The offer on the thread that held exactly this slot, used, closed or expired (the first one),
        or None: how a booking read again after a restart knows it came from an offer (ruling 8)."""
        row = self._one("SELECT o.id FROM offers o JOIN offer_slots s ON s.offer_id = o.id WHERE o.thread_norm = ? "
                        "AND s.start_utc = ? AND s.end_utc = ? ORDER BY o.id LIMIT 1",
                        (norm_id(thread_key), _ts(slot.start_utc, "slot start"), _ts(slot.end_utc, "slot end")))
        return None if row is None else row["id"]

    def offered_starts(self, thread_key) -> set:
        """Every slot start offered on the thread, ever (times offered before never come back)."""
        rows = self._all("SELECT s.start_utc FROM offer_slots s JOIN offers o ON o.id = s.offer_id "
                         "WHERE o.thread_norm = ?", (norm_id(thread_key),))
        return {_when(r["start_utc"]) for r in rows}

    # --- Bookings (rulings 4, 7, 8; G12) --------------------------------------------------------
    @staticmethod
    def _booking(row):
        return {"booking_id": row["booking_id"], "message_key": row["message_key"], "thread_key": row["thread_key"],
                "sender": row["sender"], "slot": Slot(_when(row["start_utc"]), _when(row["end_utc"])),
                "state": row["state"], "created": _when(row["created_utc"]), "run_id": row["run_id"]}

    def record_booking(self, booking_id, message_key, thread_key, sender, slot, now, run_id=None) -> bool:
        """State intent, committed before the calendar write (G12). run_id defaults to the run started on
        this object; with neither it is refused, so the per-run write cap can never miss a write.
        The same booking recorded again is a no-op (returns False, state kept); a different one under the
        same ID is refused."""
        run = run_id if run_id is not None else self._run
        if not run:
            raise ValueError("a booking needs its run: pass run_id or call start_run first")
        start, end = _ts(slot.start_utc, "slot start"), _ts(slot.end_utc, "slot end")
        if not slot.start_utc < slot.end_utc:
            raise ValueError("a slot must end after it starts")
        thread = norm_id(thread_key)
        who = _addr(sender)
        with self._tx() as con:
            old = con.execute("SELECT message_key, thread_norm, sender, start_utc, end_utc FROM bookings "
                              "WHERE booking_id = ?", (_text(booking_id, "booking_id"),)).fetchone()
            if old is not None:
                if (norm_id(old["message_key"]), old["thread_norm"], old["sender"], old["start_utc"], old["end_utc"]) \
                        != (norm_id(message_key), thread, who, start, end):
                    raise ValueError("booking %s is already recorded for another message or slot" % (booking_id,))
                return False
            con.execute("INSERT INTO bookings (booking_id, message_key, thread_key, thread_norm, sender, start_utc, "
                        "end_utc, state, created_utc, run_id) VALUES (?, ?, ?, ?, ?, ?, ?, 'intent', ?, ?)",
                        (booking_id, _text(message_key, "message_key"), thread_key, thread, who, start, end,
                         _ts(now, "now"), run))
        return True

    def set_booking_state(self, booking_id, state):
        if state not in BOOKING_STATES:
            raise ValueError("booking state must be one of %s, not %r" % (", ".join(BOOKING_STATES), state))
        cur = self._con.execute("UPDATE bookings SET state = ? WHERE booking_id = ?", (state, booking_id))
        if cur.rowcount == 0:
            raise KeyError(booking_id)

    def booking(self, booking_id) -> Optional[dict]:
        row = self._one("SELECT * FROM bookings WHERE booking_id = ?", (booking_id,))
        return None if row is None else self._booking(row)

    def live_booking_for_thread(self, thread_key) -> Optional[dict]:
        """The oldest booking on the thread that may stand on the calendar (intent, written, confirmed,
        unconfirmed, collision_unresolved). G12 compares its ID with the one being written."""
        row = self._one("SELECT * FROM bookings WHERE thread_norm = ? AND state IN (%s) ORDER BY rowid LIMIT 1"
                        % ", ".join("'%s'" % s for s in LIVE_BOOKING_STATES), (norm_id(thread_key),))
        return None if row is None else self._booking(row)

    def bookings_for_thread(self, thread_key) -> List[dict]:
        """Every booking on the thread in any state, oldest first (a gone one included, ruling 17)."""
        return [self._booking(r) for r in self._all("SELECT * FROM bookings WHERE thread_norm = ? ORDER BY rowid",
                                                    (norm_id(thread_key),))]

    def future_bookings(self, now) -> List[dict]:
        """Her bookings written or confirmed that start after `now`, soonest first (ruling 17 reads them)."""
        return [self._booking(r) for r in self._all(
            "SELECT * FROM bookings WHERE state IN ('written', 'confirmed') AND start_utc > ? ORDER BY start_utc, rowid",
            (_ts(now, "now"),))]

    def unfinished_bookings(self) -> List[dict]:
        """Bookings in intent, written or unconfirmed, oldest first: looked up by booking ID on restart."""
        return [self._booking(r) for r in self._all(
            "SELECT * FROM bookings WHERE state IN ('intent', 'written', 'unconfirmed') ORDER BY rowid")]

    def count_calendar_writes(self, run_id) -> int:
        """Bookings recorded in the run (each one is a create-only write, or an attempt at one)."""
        return self._one("SELECT COUNT(*) FROM bookings WHERE run_id = ?", (run_id,))[0]

    # --- Holds -------------------------------------------------------------------------------
    def record_hold(self, thread_key, message_key, reason_code, notice_message_id, now):
        """One hold record. notice_message_id is None when no notice went out (past the hourly cap)."""
        self._con.execute("INSERT INTO holds (thread_key, thread_norm, message_key, reason_code, notice_message_id, "
                          "created_utc) VALUES (?, ?, ?, ?, ?, ?)",
                          (_text(thread_key, "thread_key"), norm_id(thread_key), _opt_text(message_key, "message_key"),
                           _text(reason_code, "reason_code"), _opt_text(notice_message_id, "notice_message_id"),
                           _ts(now, "now")))

    def hold_for_message(self, message_key) -> Optional[dict]:
        """The hold this message already made (the first one), or None: thread_key, message_key,
        reason_code, notice_message_id (None when no notice was due), created."""
        want = norm_id(message_key)
        for row in self._all("SELECT * FROM holds WHERE message_key IS NOT NULL ORDER BY id"):
            if norm_id(row["message_key"]) == want:
                return {"thread_key": row["thread_key"], "message_key": row["message_key"],
                        "reason_code": row["reason_code"], "notice_message_id": row["notice_message_id"],
                        "created": _when(row["created_utc"])}
        return None

    def thread_held(self, thread_key) -> bool:
        """A held thread stays the owner's: nothing more is sent on it."""
        return self._one("SELECT 1 FROM holds WHERE thread_norm = ?", (norm_id(thread_key),)) is not None

    # --- Queue (A3: queued means written down) ------------------------------------------------
    def _action(self, row):
        return {"id": row["id"], "run_id": row["run_id"], "message_key": row["message_key"],
                "thread_key": self.thread_of_message(row["message_key"]) if row["message_key"] else None,
                "kind": row["kind"], "detail": json.loads(row["detail"]), "state": row["state"],
                "outcome": row["outcome"], "drop_cause": row["drop_cause"]}

    def enqueue_action(self, run_id, message_key, kind, detail_dict) -> int:
        """Written down before it starts. detail_dict must be JSON-safe (aware datetimes become ISO)."""
        if not isinstance(detail_dict, dict):
            raise ValueError("detail must be a dict")
        detail = canonical(jsonable(detail_dict)).decode("ascii")
        cur = self._con.execute("INSERT INTO actions (run_id, message_key, kind, detail, state) VALUES (?, ?, ?, ?, 'queued')",
                                (_text(run_id, "run_id"), _opt_text(message_key, "message_key"), _text(kind, "kind"),
                                 detail))
        return cur.lastrowid

    def _move_action(self, action_id, allowed, state, outcome=None):
        with self._tx() as con:
            row = con.execute("SELECT state FROM actions WHERE id = ?", (action_id,)).fetchone()
            if row is None:
                raise KeyError(action_id)
            if row["state"] not in allowed:
                raise ValueError("action %s is %s; it cannot become %s" % (action_id, row["state"], state))
            con.execute("UPDATE actions SET state = ?, outcome = ? WHERE id = ?", (state, outcome, action_id))

    def start_action(self, action_id):
        """Only a queued action starts: a dropped one never runs."""
        self._move_action(action_id, ("queued",), "started")

    def finish_action(self, action_id, outcome):
        self._move_action(action_id, ("queued", "started"), "finished", _opt_text(outcome, "outcome"))

    def action(self, action_id) -> Optional[dict]:
        row = self._one("SELECT * FROM actions WHERE id = ?", (action_id,))
        return None if row is None else self._action(row)

    def actions_for_message(self, message_key) -> List[dict]:
        """Every action written down for this inbound message, in any state, oldest first."""
        rows = self._all("SELECT * FROM actions ORDER BY id")
        want = norm_id(message_key)
        return [self._action(r) for r in rows if r["message_key"] and norm_id(r["message_key"]) == want]

    def queued_actions(self, run_id=None) -> List[dict]:
        """Actions not finished (queued, or started and never finished), oldest first."""
        sql, args = "SELECT * FROM actions WHERE state IN ('queued', 'started')", ()
        if run_id is not None:
            sql, args = sql + " AND run_id = ?", (run_id,)
        return [self._action(r) for r in self._all(sql + " ORDER BY id", args)]

    def drop_queued(self, cause) -> List[dict]:
        """Mark every unfinished action dropped, in one transaction, and return them (state dropped,
        `was` = queued or started: a started one may have reached the server)."""
        cause = _text(cause, "cause")
        with self._tx() as con:
            rows = con.execute("SELECT * FROM actions WHERE state IN ('queued', 'started') ORDER BY id").fetchall()
            con.execute("UPDATE actions SET state = 'dropped', drop_cause = ? WHERE state IN ('queued', 'started')",
                        (cause,))
            out = []
            for row in rows:
                item = self._action(row)
                item.update(was=row["state"], state="dropped", drop_cause=cause)
                out.append(item)
        return out

    # --- Spend -------------------------------------------------------------------------------
    def add_spend(self, run_id, usd):
        if isinstance(usd, bool) or not isinstance(usd, (int, float)) or usd != usd or usd < 0 or usd == float("inf"):
            raise ValueError("spend must be a finite number of dollars, zero or more, not %r" % (usd,))
        self._con.execute("INSERT INTO spend (run_id, usd) VALUES (?, ?)", (_text(run_id, "run_id"), float(usd)))

    def spend(self, run_id) -> float:
        return float(self._one("SELECT COALESCE(SUM(usd), 0.0) FROM spend WHERE run_id = ?", (run_id,))[0])

    # --- Runs ----------------------------------------------------------------------------------
    def fresh_start(self) -> Optional[_dt.datetime]:
        """When this ledger was started fresh (start_fresh), or None: a ledger never started fresh."""
        row = self._one("SELECT started_utc FROM fresh_start ORDER BY rowid LIMIT 1")
        return None if row is None else _when(row["started_utc"])

    def start_run(self, run_id, mode, now):
        """Record the run and make it this object's current run. Starting the same open run again is a no-op."""
        if mode not in MODES:
            raise ValueError("unknown mode %r" % (mode,))
        run_id = _text(run_id, "run_id")
        started = _ts(now, "now")
        with self._tx() as con:
            old = con.execute("SELECT mode, ended_utc FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if old is not None:
                if old["mode"] != mode or old["ended_utc"] is not None:
                    raise ValueError("run %s already exists (mode %s, ended %s)" % (run_id, old["mode"], old["ended_utc"]))
            else:
                con.execute("INSERT INTO runs (run_id, mode, started_utc) VALUES (?, ?, ?)", (run_id, mode, started))
        self._run = run_id

    def end_run(self, run_id, now):
        cur = self._con.execute("UPDATE runs SET ended_utc = ? WHERE run_id = ?", (_ts(now, "now"), run_id))
        if cur.rowcount == 0:
            raise KeyError(run_id)
        if self._run == run_id:
            self._run = None

    def runs(self) -> List[dict]:
        """Every run on this ledger, in the order started (rowid: a sandbox restart's clock begins at
        clock.txt again, so started_utc can go back). The daily digest reads their receipt files."""
        return [{"run_id": r["run_id"], "mode": r["mode"], "started_utc": _when(r["started_utc"]),
                 "ended_utc": _when(r["ended_utc"])} for r in self._all("SELECT * FROM runs ORDER BY rowid")]

    def unended_runs(self) -> List[dict]:
        """Runs started and never ended (an interrupted run, for the startup line), oldest first."""
        return [{"run_id": r["run_id"], "mode": r["mode"], "started_utc": _when(r["started_utc"]), "ended_utc": None}
                for r in self._all("SELECT * FROM runs WHERE ended_utc IS NULL ORDER BY started_utc, rowid")]
