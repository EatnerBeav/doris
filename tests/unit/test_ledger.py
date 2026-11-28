"""ledger.py: her SQLite state. Every write commits before it returns; a hard kill loses nothing written."""
import datetime as dt
import hashlib
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from doris import ledger as ledger_mod
from doris.ledger import Ledger, LedgerError
from doris.shapes import Slot

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 11, 30, 17, 0, 0, tzinfo=UTC)       # Mon Nov 30, 9:00 AM Pacific
H = dt.timedelta(hours=1)
SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src")
PRIYA = "priya@northwind.example"
OWNER = "whizz@fastmail.com"
K1 = "m1@northwind.example"
K2 = "m2@northwind.example"
TH = "m1@northwind.example"          # a thread is keyed by its first message
RUN = "model-stub-sandbox-20261130T1700Z"


def slot(day, hour, minute=0, length=30):
    start = dt.datetime(2026, 12, day, hour, minute, tzinfo=UTC)
    return Slot(start, start + dt.timedelta(minutes=length))


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "state", "sandbox", "ledger.sqlite")
        self.db = Ledger(self.path)
        self.extra = []

    def tearDown(self):
        for other in self.extra:
            other.close()
        self.db.close()
        self._tmp.cleanup()

    def other(self):
        """A second connection: it sees only what was committed."""
        o = Ledger(self.path)
        self.extra.append(o)
        return o

    def raw(self, sql, *args):
        con = sqlite3.connect(self.path)
        try:
            out = con.execute(sql, args).fetchall()
            con.commit()
            return out
        finally:
            con.close()

    def message(self, key=K1, thread=TH, received=T0, status="fetched", **kw):
        self.db.record_message(key, thread, PRIYA, received, status, **kw)

    def reply(self, mid, thread=TH, to=PRIYA, now=T0, run=RUN, kind="send_reply", body="b1"):
        return self.db.record_send(mid, kind, "T2", thread, to, sha(body), now, run)


class OpenAndSchema(Base):
    def test_schema_and_version_table_created_on_open(self):
        self.assertEqual(self.raw("SELECT version FROM schema_version"), [(ledger_mod.SCHEMA_VERSION,)])
        self.db.close()
        self.db = Ledger(self.path)
        self.assertEqual(self.raw("SELECT version FROM schema_version"), [(ledger_mod.SCHEMA_VERSION,)])

    def test_other_schema_version_refused(self):
        self.raw("UPDATE schema_version SET version = 999")
        with self.assertRaises(LedgerError):
            Ledger(self.path)

    def test_a_version_2_ledger_gains_the_direct_column_and_keeps_its_bookings(self):
        self.db.start_run(RUN, "sandbox", T0)
        self.db.record_booking("doris-1", K1, TH, PRIYA, slot(2, 18), T0, direct=True)
        self.make_version_3()                   # a 2 has nothing a 3 or a 4 added
        cols = [r[1] for r in self.raw("PRAGMA table_info(bookings)") if r[1] != "direct"]
        self.raw("CREATE TABLE b2 AS SELECT %s FROM bookings" % ", ".join(cols))
        self.raw("DROP TABLE bookings")
        self.raw("ALTER TABLE b2 RENAME TO bookings")
        self.raw("UPDATE schema_version SET version = 2")
        self.db = Ledger(self.path)
        self.assertEqual(self.raw("SELECT version FROM schema_version"), [(ledger_mod.SCHEMA_VERSION,)])
        b = self.db.booking("doris-1")
        self.assertEqual((b["state"], b["slot"]), ("intent", slot(2, 18)))
        self.assertIsNone(b["direct"])          # recorded before the ledger kept it: unknown, never guessed here
        self.db.record_booking("doris-2", K1, TH, PRIYA, slot(3, 18), T0, run_id=RUN, direct=False)
        self.assertIs(self.db.booking("doris-2")["direct"], False)

    # The two tables version 4 changed, exactly as version 3 made them (main at fd9128f).
    V3_OFFERS = """CREATE TABLE offers (
        id INTEGER PRIMARY KEY AUTOINCREMENT, thread_key TEXT NOT NULL, thread_norm TEXT NOT NULL,
        sender TEXT NOT NULL, length_min INTEGER NOT NULL, created_utc TEXT NOT NULL, expires_utc TEXT NOT NULL,
        used INTEGER NOT NULL DEFAULT 0, closed_reason TEXT, mid_norm TEXT)"""
    V3_OFFER_SLOTS = """CREATE TABLE offer_slots (
        offer_id INTEGER NOT NULL, idx INTEGER NOT NULL, start_utc TEXT NOT NULL, end_utc TEXT NOT NULL,
        PRIMARY KEY (offer_id, idx))"""

    def make_version_3(self):
        """This ledger's file as version 3 wrote it: no followups table, offers with no nudged_utc and
        offer_slots with no dropped, every row kept."""
        self.db.close()
        con = sqlite3.connect(self.path)
        try:
            con.executescript("""
                DROP INDEX followups_by_offer; DROP INDEX followups_by_booking; DROP TABLE followups;
                ALTER TABLE offers RENAME TO offers4; DROP INDEX offers_by_send; %s;
                INSERT INTO offers SELECT id, thread_key, thread_norm, sender, length_min, created_utc, expires_utc,
                    used, closed_reason, mid_norm FROM offers4;
                DROP TABLE offers4;
                CREATE UNIQUE INDEX offers_by_send ON offers (mid_norm) WHERE mid_norm IS NOT NULL;
                ALTER TABLE offer_slots RENAME TO slots4; %s;
                INSERT INTO offer_slots SELECT offer_id, idx, start_utc, end_utc FROM slots4;
                DROP TABLE slots4;
                UPDATE schema_version SET version = 3;
            """ % (self.V3_OFFERS, self.V3_OFFER_SLOTS))
            con.commit()
        finally:
            con.close()

    def test_a_version_3_ledger_gains_follow_ups_and_keeps_every_row(self):
        # the live ledger on our server is a 3: it is brought up in place the first time this Doris opens it
        self.db.start_run(RUN, "sandbox", T0)
        self.message()
        used = self.db.create_offer(TH, PRIYA, [slot(2, 18), slot(3, 19)], 30, T0, T0 + dt.timedelta(days=7),
                                    message_id="<o1@fastmail.com>")
        self.db.use_offer(used)
        still = self.db.create_offer("t2@x.example", PRIYA, [slot(7, 18), slot(8, 18), slot(9, 18)], 45, T0,
                                     T0 + dt.timedelta(days=7), message_id="<o2@fastmail.com>")
        self.db.record_booking("doris-1", K1, TH, PRIYA, slot(2, 18), T0, direct=False)
        self.db.set_booking_state("doris-1", "confirmed")
        self.reply("<o1@fastmail.com>")
        self.db.record_hold("t3@x.example", K2, "hold.money", None, T0)
        self.make_version_3()
        self.assertEqual(self.raw("SELECT name FROM sqlite_master WHERE name LIKE 'followups%'"), [])
        self.assertNotIn("nudged_utc", [r[1] for r in self.raw("PRAGMA table_info(offers)")])
        self.assertNotIn("dropped", [r[1] for r in self.raw("PRAGMA table_info(offer_slots)")])

        self.db = Ledger(self.path, create=False)
        self.assertEqual(self.raw("SELECT version FROM schema_version"), [(4,)])
        self.assertEqual(self.raw("PRAGMA integrity_check"), [("ok",)])
        [o] = self.db.open_offers("t2@x.example", T0)
        self.assertEqual((o["id"], o["slots"], o["length_min"], o["created"], o["nudged"]),
                         (still, [slot(7, 18), slot(8, 18), slot(9, 18)], 45, T0, None))
        self.assertEqual(self.db.offer_for_send("<o1@fastmail.com>")["id"], used)
        self.assertEqual(self.db.offer_holding(TH, slot(3, 19)), used)
        self.assertEqual([b["booking_id"] for b in self.db.future_bookings(T0)], ["doris-1"])
        self.assertIs(self.db.booking("doris-1")["direct"], False)
        self.assertTrue(self.db.thread_held("t3@x.example"))
        self.assertEqual(self.db.send("<o1@fastmail.com>")["state"], "pending")
        self.assertEqual(self.db.open_followups(), [])           # an offer made before follow-ups has none
        # and everything version 4 adds works on it
        fid, new = self.db.file_followup("nudge", "t2@x.example", PRIYA, K2, T0 + 48 * H, T0, offer_id=still)
        self.assertTrue(new)
        self.db.mark_nudged(still, [slot(7, 18), slot(9, 18)], T0 + 48 * H)
        self.assertEqual(self.db.open_offers("t2@x.example", T0 + 48 * H)[0]["slots"], [slot(7, 18), slot(9, 18)])
        self.assertEqual(self.db.mark_booking_gone("doris-1", "<n1@fastmail.com>", T0),
                         self.db.followup_for_booking("doris-1")["id"])
        # opened again it is a 4 and nothing is brought up twice
        self.db.close()
        self.db = Ledger(self.path, create=False)
        self.assertEqual(self.db.followup(fid)["state"], "open")
        self.assertEqual(self.db.booking("doris-1")["state"], "gone")

    def test_a_version_3_upgrade_that_fails_part_way_leaves_the_file_a_3(self):
        # all of it is one transaction: the version is written last, so a file is a whole 3 or a whole 4
        oid = self.db.create_offer(TH, PRIYA, [slot(2, 18)], 30, T0, T0 + dt.timedelta(days=7))
        self.make_version_3()
        self.raw("ALTER TABLE offer_slots ADD COLUMN dropped INTEGER NOT NULL DEFAULT 0")   # the 2nd step will fail
        with self.assertRaises(LedgerError):
            Ledger(self.path, create=False)
        self.assertEqual(self.raw("SELECT version FROM schema_version"), [(3,)])
        self.assertNotIn("nudged_utc", [r[1] for r in self.raw("PRAGMA table_info(offers)")])
        self.assertEqual(self.raw("SELECT name FROM sqlite_master WHERE name = 'followups'"), [])
        self.assertEqual(self.raw("SELECT id FROM offers"), [(oid,)])
        self.db = Ledger(os.path.join(self._tmp.name, "other.sqlite"))      # for tearDown

    def test_a_version_2_ledger_comes_all_the_way_up(self):
        self.db.start_run(RUN, "sandbox", T0)
        oid = self.db.create_offer(TH, PRIYA, [slot(2, 18), slot(3, 19)], 30, T0, T0 + dt.timedelta(days=7))
        self.db.record_booking("doris-1", K1, TH, PRIYA, slot(2, 18), T0, direct=True)
        self.make_version_3()
        cols = [r[1] for r in self.raw("PRAGMA table_info(bookings)") if r[1] != "direct"]
        self.raw("CREATE TABLE b2 AS SELECT %s FROM bookings" % ", ".join(cols))
        self.raw("DROP TABLE bookings")
        self.raw("ALTER TABLE b2 RENAME TO bookings")
        self.raw("UPDATE schema_version SET version = 2")
        self.db = Ledger(self.path)
        self.assertEqual(self.raw("SELECT version FROM schema_version"), [(4,)])
        self.assertIsNone(self.db.booking("doris-1")["direct"])
        self.assertEqual(self.db.open_offers(TH, T0)[0]["id"], oid)
        self.assertEqual(self.db.file_followup("nudge", TH, PRIYA, K1, T0 + H, T0, offer_id=oid), (1, True))

    def test_a_version_1_ledger_is_still_refused(self):
        self.raw("UPDATE schema_version SET version = 1")
        with self.assertRaises(LedgerError):
            Ledger(self.path)

    def test_create_false_refuses_a_missing_ledger(self):
        missing = os.path.join(self._tmp.name, "live", "ledger.sqlite")
        with self.assertRaises(FileNotFoundError):
            Ledger(missing, create=False)
        self.assertFalse(os.path.exists(missing))

    def test_a_file_that_is_not_a_ledger_is_refused(self):
        bad = os.path.join(self._tmp.name, "bad.sqlite")
        with open(bad, "wb") as fh:
            fh.write(b"this is not a database, it is a judge's prank" * 50)
        with self.assertRaises(LedgerError):
            Ledger(bad)


class FreshStart(Base):
    """Ruling 8: the live ledger is made by a fresh start, which records when it started; her live
    mail is read from that time on, so the first run never answers the old mail in her inbox."""

    def setUp(self):
        super().setUp()
        self.live = os.path.join(self._tmp.name, "state", "live", "ledger.sqlite")

    def test_a_fresh_start_makes_a_ledger_that_already_names_its_start(self):
        ledger_mod.start_fresh(self.live, T0)
        with Ledger(self.live, create=False) as db:
            self.assertEqual(db.fresh_start(), T0)

    def test_a_ledger_that_never_started_fresh_names_no_start(self):
        self.assertIsNone(self.db.fresh_start())

    def test_a_fresh_start_refuses_a_ledger_already_there_and_leaves_it_alone(self):
        with self.assertRaises(FileExistsError):
            ledger_mod.start_fresh(self.path, T0)
        self.assertIsNone(self.db.fresh_start())
        ledger_mod.start_fresh(self.live, T0)
        with self.assertRaises(FileExistsError):
            ledger_mod.start_fresh(self.live, T0 + H)
        with Ledger(self.live, create=False) as db:
            self.assertEqual(db.fresh_start(), T0)

    def test_a_fresh_start_that_cannot_record_its_time_leaves_no_ledger(self):
        with self.assertRaises(ValueError):
            ledger_mod.start_fresh(self.live, dt.datetime(2026, 11, 30, 17, 0))      # naive: refused
        self.assertFalse(os.path.exists(self.live))
        ledger_mod.start_fresh(self.live, T0)                                     # and it can start after
        with Ledger(self.live, create=False) as db:
            self.assertEqual(db.fresh_start(), T0)


class CommitsBeforeReturn(Base):
    def test_every_write_is_visible_to_a_second_connection_at_once(self):
        db, other = self.db, self.other()
        db.start_run(RUN, "sandbox", T0)
        self.assertEqual([r["run_id"] for r in other.unended_runs()], [RUN])
        db.record_fetch("01-priya.eml", K1, RUN, T0)
        self.assertTrue(other.server_id_seen("01-priya.eml"))
        self.message()
        self.assertTrue(other.message_seen(K1))
        db.set_message_status(K1, "processing")
        self.assertEqual(other.message(K1)["status"], "processing")
        self.reply("<a1@fastmail.com>")
        self.assertTrue(other.is_her_message_id("<a1@fastmail.com>"))
        db.set_send_state("<a1@fastmail.com>", "confirmed")
        self.assertEqual(other.send("<a1@fastmail.com>")["state"], "confirmed")
        db.silence(PRIYA, "stop", K1, T0)
        self.assertTrue(other.is_silenced(PRIYA))
        oid = db.create_offer(TH, PRIYA, [slot(2, 18)], 30, T0, T0 + 24 * H)
        self.assertEqual(other.offer_count(TH), 1)
        db.use_offer(oid)
        self.assertEqual(other.open_offers(TH, T0), [])
        db.record_booking("doris-1", K1, TH, PRIYA, slot(2, 18), T0, direct=False)
        self.assertEqual(other.booking("doris-1")["state"], "intent")
        db.set_booking_state("doris-1", "written")
        self.assertEqual(other.booking("doris-1")["state"], "written")
        db.record_hold(TH, K1, "hold.money", "<n1@fastmail.com>", T0)
        self.assertTrue(other.thread_held(TH))
        aid = db.enqueue_action(RUN, K1, "send_reply", {"template": "T1"})
        self.assertEqual([a["id"] for a in other.queued_actions()], [aid])
        db.drop_queued("cli")
        self.assertEqual(other.queued_actions(), [])
        db.add_spend(RUN, 0.25)
        self.assertEqual(other.spend(RUN), 0.25)
        db.end_run(RUN, T0 + H)
        self.assertEqual(other.unended_runs(), [])

    def test_booking_intent_commits_before_the_calendar_write(self):
        seen = {}

        class FakeCalendar:
            def create_only(cal, booking_id, ics):
                seen["state"] = (self.other().booking(booking_id) or {}).get("state")

        self.db.start_run(RUN, "sandbox", T0)
        self.db.record_booking("doris-2", K1, TH, PRIYA, slot(2, 18), T0, direct=False)
        FakeCalendar().create_only("doris-2", b"BEGIN:VCALENDAR")
        self.assertEqual(seen["state"], "intent")


class FailedCommit(Base):
    def test_a_failed_commit_rolls_back_and_the_ledger_stays_usable(self):
        real = self.db._con

        class CommitFailsOnce:
            failed = False

            def __getattr__(self, name):
                return getattr(real, name)

            def execute(self, sql, *args):
                if sql == "COMMIT" and not CommitFailsOnce.failed:
                    CommitFailsOnce.failed = True
                    raise sqlite3.OperationalError("disk I/O error")
                return real.execute(sql, *args)

        self.db._con = CommitFailsOnce()
        try:
            with self.assertRaises(sqlite3.OperationalError):
                self.db.record_message(K1, TH, PRIYA, T0, "fetched")
            self.assertFalse(self.db.message_seen(K1))
            self.db.record_message(K1, TH, PRIYA, T0, "fetched")
            self.assertTrue(self.other().message_seen(K1))
        finally:
            self.db._con = real


class Fetches(Base):
    def test_server_id_seen_after_record_fetch(self):
        self.assertFalse(self.db.server_id_seen("01-priya.eml"))
        self.db.record_fetch("01-priya.eml", K1, RUN, T0)
        self.db.record_fetch("01-priya.eml", K1, RUN, T0)
        self.assertTrue(self.db.server_id_seen("01-priya.eml"))
        self.assertFalse(self.db.server_id_seen("02-sam.eml"))


class Messages(Base):
    def test_seen_once_and_never_recorded_twice(self):
        self.assertFalse(self.db.message_seen(K1))
        self.message()
        self.assertTrue(self.db.message_seen(K1))
        with self.assertRaises(ValueError):
            self.message(status="processing")

    def test_angle_brackets_and_outer_spaces_do_not_make_a_new_message(self):
        self.message(key="<m3@northwind.example>")
        self.assertTrue(self.db.message_seen("m3@northwind.example"))
        self.assertTrue(self.db.message_seen(" <m3@northwind.example> "))
        self.assertFalse(self.db.message_seen("M3@northwind.example"))
        with self.assertRaises(ValueError):
            self.message(key="m3@northwind.example")

    def test_status_values_are_checked(self):
        with self.assertRaises(ValueError):
            self.message(status="weird")
        self.message()
        with self.assertRaises(ValueError):
            self.db.set_message_status(K1, "weird")
        with self.assertRaises(KeyError):
            self.db.set_message_status(K2, "done")

    def test_status_outcome_and_thread(self):
        self.message()
        self.db.set_message_status(K1, "done", outcome="book")
        row = self.db.message(K1)
        self.assertEqual((row["status"], row["outcome"], row["sender"], row["received_utc"]), ("done", "book", PRIYA, T0))
        self.assertEqual(self.db.thread_of_message("<" + K1 + ">"), TH)
        self.assertIsNone(self.db.thread_of_message(K2))
        self.assertIsNone(self.db.thread_of_message("<>"))
        self.assertIsNone(self.db.thread_of_message(None))
        self.assertFalse(self.db.is_her_message_id("<>"))
        self.assertIsNone(self.db.thread_of_sent(None))

    def test_unprocessed_oldest_first_and_by_run(self):
        self.db.record_message("m5@x.example", TH, PRIYA, T0 + 2 * H, "processing", run_id="run-b")
        self.db.record_message("m4@x.example", TH, PRIYA, T0 + H, "fetched", run_id="run-a")
        self.db.record_message("m6@x.example", TH, PRIYA, T0, "done", run_id="run-a")
        self.db.record_fetch("07.eml", "m7@x.example", "run-b", T0)
        self.db.record_message("m7@x.example", TH, PRIYA, T0 + 3 * H, "fetched")
        self.assertEqual([m["key"] for m in self.db.unprocessed_messages()],
                         ["m4@x.example", "m5@x.example", "m7@x.example"])
        self.assertEqual([m["key"] for m in self.db.unprocessed_messages("run-b")], ["m5@x.example", "m7@x.example"])
        self.assertEqual(self.db.unprocessed_messages("run-a")[0]["thread_key"], TH)
        self.db.record_message("m8@x.example", "t8@x.example", "Sam@Contoso.example", T0, "fetched", run_id="run-b")
        self.assertEqual([m["key"] for m in self.db.unprocessed_messages(sender="sam@contoso.example")], ["m8@x.example"])
        self.assertEqual([m["key"] for m in self.db.unprocessed_messages("run-a", sender=PRIYA)], ["m4@x.example"])

    def test_naive_times_are_refused(self):
        with self.assertRaises(ValueError):
            self.db.record_message(K1, TH, PRIYA, dt.datetime(2026, 11, 30, 9, 0), "fetched")


class Sends(Base):
    def test_pending_then_confirmed(self):
        self.assertTrue(self.reply("<a1@fastmail.com>"))
        self.assertEqual(self.db.send("<a1@fastmail.com>")["state"], "pending")
        self.db.set_send_state("<a1@fastmail.com>", "confirmed")
        self.assertEqual(self.db.send("a1@fastmail.com")["state"], "confirmed")
        with self.assertRaises(ValueError):
            self.db.set_send_state("<a1@fastmail.com>", "sent")
        with self.assertRaises(KeyError):
            self.db.set_send_state("<zz@fastmail.com>", "unknown")

    def test_a_send_that_never_went_can_take_a_new_body_one_that_may_have_gone_cannot(self):
        # a send the transport never had (pending) may go out with fresh slots under the same
        # Message-ID (ruling 8); from its sending mark on it may have gone, so it never changes
        self.reply("<a1@fastmail.com>")
        self.db.reset_send("<a1@fastmail.com>", "f" * 64)
        self.assertEqual(self.db.send("<a1@fastmail.com>")["body_sha256"], "f" * 64)
        for state in ("sending", "unknown", "handed_over", "confirmed"):
            self.db.set_send_state("<a1@fastmail.com>", state)
            with self.assertRaises(ValueError):
                self.db.reset_send("<a1@fastmail.com>", "e" * 64)
        self.assertEqual(self.db.send("<a1@fastmail.com>")["body_sha256"], "f" * 64)
        with self.assertRaises(KeyError):
            self.db.reset_send("<zz@fastmail.com>", "e" * 64)

    def test_sending_is_marked_before_the_transport_and_only_its_refusal_takes_it_back(self):
        # review 2, finding 1: from its sending mark a send may have gone, so it only moves up
        # (sending, unknown, handed_over, confirmed); the transport refusing it (MailError: it surely
        # did not go) is the one way back to pending, and only from sending
        mid = "<a1@fastmail.com>"
        self.reply(mid)
        self.db.set_send_state(mid, "sending")
        self.db.set_send_state(mid, "pending")
        self.assertEqual(self.db.send(mid)["state"], "sending")
        self.db.send_refused(mid)
        self.assertEqual(self.db.send(mid)["state"], "pending")
        self.db.set_send_state(mid, "sending")
        self.db.set_send_state(mid, "unknown")
        for later in ("pending", "sending"):
            self.db.set_send_state(mid, later)
        self.db.send_refused(mid)
        self.assertEqual(self.db.send(mid)["state"], "unknown")
        self.db.set_send_state(mid, "handed_over")
        self.db.send_refused(mid)
        self.db.set_send_state(mid, "unknown")
        self.assertEqual(self.db.send(mid)["state"], "handed_over")
        with self.assertRaises(KeyError):
            self.db.send_refused("<zz@fastmail.com>")
        self.assertEqual(ledger_mod.NEVER_AGAIN_STATES, ("sending", "unknown", "handed_over", "confirmed"))

    def test_a_send_the_server_took_is_never_taken_back(self):
        # decision 76: the server taking it is written down as handed_over before the Sent look; from there the
        # send can only become confirmed, whatever a later look, a restart or the kill switch writes
        self.reply("<a1@fastmail.com>")
        self.db.set_send_state("<a1@fastmail.com>", "handed_over")
        for later in ("unknown", "pending", "handed_over"):
            self.db.set_send_state("<a1@fastmail.com>", later)
            self.assertEqual(self.db.send("<a1@fastmail.com>")["state"], "handed_over")
        with self.assertRaises(ValueError):
            self.db.reset_send("<a1@fastmail.com>", "f" * 64)
        self.db.set_send_state("<a1@fastmail.com>", "confirmed")
        for later in ("unknown", "pending", "handed_over"):
            self.db.set_send_state("<a1@fastmail.com>", later)
            self.assertEqual(self.db.send("<a1@fastmail.com>")["state"], "confirmed")

    def test_send_kind_is_checked(self):
        with self.assertRaises(ValueError):
            self.reply("<a1@fastmail.com>", kind="reply")

    def test_her_message_ids_with_or_without_brackets(self):
        self.reply("<a1@fastmail.com>", thread=TH)
        self.assertTrue(self.db.is_her_message_id("<a1@fastmail.com>"))
        self.assertTrue(self.db.is_her_message_id("a1@fastmail.com"))
        self.assertFalse(self.db.is_her_message_id("<a2@fastmail.com>"))
        self.assertEqual(self.db.thread_of_sent("a1@fastmail.com"), TH)
        self.assertIsNone(self.db.thread_of_sent("<a2@fastmail.com>"))

    def test_addresses_written_are_prospect_replies_only_lowercased(self):
        self.reply("<a1@fastmail.com>", to="Priya@Northwind.example")
        self.reply("<a2@fastmail.com>", to="sam@contoso.example")
        self.reply("<n1@fastmail.com>", to=OWNER, kind="send_hold_notice")
        self.reply("<a3@fastmail.com>", thread="other@x.example", to="lee@x.example")
        self.assertEqual(self.db.addresses_written_on_thread(TH), {PRIYA, "sam@contoso.example"})

    def test_count_prospect_sends_filters(self):
        self.reply("<a1@fastmail.com>", now=T0, run="run-a")
        self.reply("<a2@fastmail.com>", now=T0 + H, run="run-b")
        self.reply("<a3@fastmail.com>", now=T0 + 2 * H, run="run-b", thread="t2@x.example")
        self.reply("<n1@fastmail.com>", now=T0 + 2 * H, run="run-b", to=OWNER, kind="send_hold_notice")
        self.reply("<d1@fastmail.com>", now=T0 + 2 * H, run="run-b", to=OWNER, kind="send_digest")
        self.db.set_send_state("<a2@fastmail.com>", "unknown")
        self.assertEqual(self.db.count_prospect_sends(), 3)
        self.assertEqual(self.db.count_prospect_sends(since=T0 + H), 2)
        self.assertEqual(self.db.count_prospect_sends(run_id="run-b"), 2)
        self.assertEqual(self.db.count_prospect_sends(thread_key=TH), 2)
        self.assertEqual(self.db.count_prospect_sends(since=T0 + H, run_id="run-b", thread_key=TH), 1)
        self.assertEqual(self.db.count_hold_notices(T0 + 2 * H), 1)
        self.assertEqual(self.db.count_hold_notices(T0 + 3 * H), 0)

    def test_same_send_recorded_again_is_a_no_op_and_a_different_one_refused(self):
        self.reply("<a1@fastmail.com>")
        self.db.set_send_state("<a1@fastmail.com>", "confirmed")
        self.assertFalse(self.reply("<a1@fastmail.com>", now=T0 + H))
        self.assertEqual(self.db.send("<a1@fastmail.com>")["state"], "confirmed")
        self.assertEqual(self.db.count_prospect_sends(), 1)
        with self.assertRaises(ValueError):
            self.reply("<a1@fastmail.com>", body="another body")

    def test_unfinished_sends(self):
        self.reply("<a1@fastmail.com>")
        self.reply("<a2@fastmail.com>")
        self.reply("<a3@fastmail.com>")
        self.db.set_send_state("<a2@fastmail.com>", "confirmed")
        self.db.set_send_state("<a3@fastmail.com>", "unknown")
        self.assertEqual([s["message_id"] for s in self.db.unfinished_sends()], ["<a1@fastmail.com>", "<a3@fastmail.com>"])
        self.db.set_send_state("<a1@fastmail.com>", "handed_over")      # the server took it: nothing to redo
        self.assertEqual([s["message_id"] for s in self.db.unfinished_sends()], ["<a3@fastmail.com>"])
        self.reply("<a4@fastmail.com>")
        self.db.set_send_state("<a4@fastmail.com>", "sending")          # cut off mid-send: looked for again
        self.assertEqual([s["message_id"] for s in self.db.unfinished_sends()],
                         ["<a3@fastmail.com>", "<a4@fastmail.com>"])


class StopList(Base):
    def test_silenced_by_address_whatever_its_case(self):
        self.db.silence("Priya@Northwind.example", "stop emailing me", K1, T0)
        self.db.silence(PRIYA, "stop", K2, T0 + H)
        self.assertTrue(self.db.is_silenced(PRIYA))
        self.assertTrue(self.db.is_silenced(" PRIYA@northwind.example "))
        self.assertFalse(self.db.is_silenced("sam@contoso.example"))
        self.assertEqual(self.db.silenced(), [PRIYA])


class Offers(Base):
    def offer(self, slots=None, now=T0, expires=None, thread=TH, length=30):
        slots = slots if slots is not None else [slot(2, 18), slot(3, 19), slot(4, 20)]
        return self.db.create_offer(thread, PRIYA, slots, length, now, expires or now + dt.timedelta(days=7))

    def test_open_offer_comes_back_with_slots(self):
        oid = self.offer()
        self.assertIsInstance(oid, int)
        [o] = self.db.open_offers(TH, T0)
        self.assertEqual((o["id"], o["sender"], o["length_min"], o["created"], o["expires"]),
                         (oid, PRIYA, 30, T0, T0 + dt.timedelta(days=7)))
        self.assertEqual(o["slots"], [slot(2, 18), slot(3, 19), slot(4, 20)])
        self.assertIsInstance(o["slots"][0], Slot)

    def test_the_offer_that_held_a_slot_used_closed_or_expired(self):
        # a booking read again after a restart finds the offer it came from (ruling 8)
        oid = self.offer()
        self.db.use_offer(oid)
        self.db.close_offers(TH, "booked")
        self.assertEqual(self.db.offer_holding(TH, slot(3, 19)), oid)
        self.assertEqual(self.db.offer_holding("<" + TH + ">", slot(3, 19)), oid)
        self.assertIsNone(self.db.offer_holding(TH, slot(3, 19, length=45)))
        self.assertIsNone(self.db.offer_holding(TH, slot(3, 20)))
        self.assertIsNone(self.db.offer_holding("t2@x.example", slot(3, 19)))

    def test_expired_at_now_and_not_before(self):
        self.offer()
        expiry = T0 + dt.timedelta(days=7)
        self.assertEqual(len(self.db.open_offers(TH, expiry - dt.timedelta(seconds=1))), 1)
        self.assertEqual(self.db.open_offers(TH, expiry), [])

    def test_used_or_closed_offers_are_not_open_but_still_counted(self):
        first = self.offer()
        self.db.use_offer(first)
        self.assertEqual(self.db.open_offers(TH, T0), [])
        second = self.offer(slots=[slot(7, 18)])
        self.assertEqual(self.db.close_offers(TH, "no_thanks"), 1)
        self.assertEqual(self.db.open_offers(TH, T0), [])
        self.assertEqual(self.db.offer_count(TH), 2)
        with self.assertRaises(ValueError):
            self.db.use_offer(first)
        with self.assertRaises(ValueError):
            self.db.use_offer(second)
        with self.assertRaises(KeyError):
            self.db.use_offer(999)

    def test_open_offers_in_creation_order_and_only_this_thread(self):
        a = self.offer(slots=[slot(2, 18)])
        self.offer(slots=[slot(2, 19)], thread="t2@x.example")
        b = self.offer(slots=[slot(3, 18)])
        self.assertEqual([o["id"] for o in self.db.open_offers(TH, T0)], [a, b])
        self.assertEqual(self.db.close_offers("t2@x.example", "stop"), 1)
        self.assertEqual([o["id"] for o in self.db.open_offers(TH, T0)], [a, b])

    def test_a_newer_offer_to_the_sender_closes_their_older_ones_only(self):
        # a re-offer supersedes the sender's earlier offer: never itself, and the thread still counts both
        # (Fable review of PR #3, finding 4)
        first = self.offer(slots=[slot(2, 18)])
        second = self.offer(slots=[slot(3, 18)])
        self.assertEqual(self.db.close_offers(TH, "superseded", sender=" Priya@Northwind.example ", before=second), 1)
        self.assertEqual([o["id"] for o in self.db.open_offers(TH, T0)], [second])
        self.assertEqual(self.db.close_offers(TH, "superseded", sender=PRIYA, before=second), 0)
        self.assertEqual(self.db.offer_count(TH), 2)
        self.assertEqual(self.raw("SELECT id, closed_reason FROM offers ORDER BY id"),
                         [(first, "superseded"), (second, None)])
        for bad in (True, str(second), 1.0):
            with self.assertRaises(ValueError):
                self.db.close_offers(TH, "superseded", sender=PRIYA, before=bad)

    def test_closing_one_senders_offers_leaves_another_senders_open(self):
        # an offer is one sender's (same sender, same thread)
        mine = self.offer(slots=[slot(2, 18)])
        theirs = self.db.create_offer(TH, "sam@contoso.example", [slot(3, 18)], 30, T0, T0 + dt.timedelta(days=7))
        self.assertEqual(self.db.close_offers(TH, "superseded", sender="lee@x.example"), 0)
        self.assertEqual(self.db.close_offers(TH, "superseded", sender=PRIYA), 1)
        self.assertEqual([o["id"] for o in self.db.open_offers(TH, T0)], [theirs])
        self.assertEqual(self.raw("SELECT id, closed_reason FROM offers ORDER BY id"),
                         [(mine, "superseded"), (theirs, None)])
        with self.assertRaises(ValueError):
            self.db.close_offers(TH, "superseded", sender=" ")

    def test_offered_starts_cover_every_offer_ever(self):
        first = self.offer(slots=[slot(2, 18), slot(3, 19)])
        self.db.use_offer(first)
        self.offer(slots=[slot(4, 20)])
        self.assertEqual(self.db.offered_starts(TH),
                         {slot(2, 18).start_utc, slot(3, 19).start_utc, slot(4, 20).start_utc})

    def test_an_offer_is_found_by_the_send_that_carries_it(self):
        oid = self.db.create_offer(TH, PRIYA, [slot(2, 18)], 30, T0, T0 + dt.timedelta(days=7),
                                   message_id="<o1@fastmail.com>")
        self.assertEqual(self.db.offer_for_send("o1@fastmail.com")["id"], oid)
        self.assertIsNone(self.db.offer_for_send("<o2@fastmail.com>"))
        with self.assertRaises(ValueError):          # one offer per send
            self.db.create_offer(TH, PRIYA, [slot(3, 18)], 30, T0, T0 + dt.timedelta(days=7),
                                 message_id="<o1@fastmail.com>")

    def test_an_offer_takes_new_slots_without_counting_twice(self):
        oid = self.offer()
        self.db.set_offer_slots(oid, [slot(7, 18), slot(8, 18)], T0, T0 + dt.timedelta(days=7))
        [o] = self.db.open_offers(TH, T0)
        self.assertEqual(o["slots"], [slot(7, 18), slot(8, 18)])
        self.assertEqual(self.db.offer_count(TH), 1)
        self.assertEqual(self.db.offered_starts(TH), {slot(7, 18).start_utc, slot(8, 18).start_utc})

    def test_two_offers_per_thread_ever(self):
        self.offer()
        self.offer()
        with self.assertRaises(ValueError):
            self.offer()
        self.assertEqual(self.db.offer_count(TH), 2)

    def test_expiry_is_never_beyond_seven_days(self):
        with self.assertRaises(ValueError):
            self.offer(expires=T0 + dt.timedelta(days=7, seconds=1))
        with self.assertRaises(ValueError):
            self.offer(expires=T0)
        self.assertEqual(self.db.offer_count(TH), 0)

    def test_one_to_three_slots_and_an_allowed_length(self):
        for kw in ({"slots": []}, {"slots": [slot(2, 18), slot(3, 18), slot(4, 18), slot(7, 18)]}, {"length": 61},
                   {"length": 4}):
            with self.subTest(**{k: len(v) if k == "slots" else v for k, v in kw.items()}), self.assertRaises(ValueError):
                self.offer(**kw)
        self.assertEqual(self.db.offer_count(TH), 0)


class Bookings(Base):
    def setUp(self):
        super().setUp()
        self.db.start_run(RUN, "sandbox", T0)

    def book(self, bid="doris-1", s=None, thread=TH, **kw):
        kw.setdefault("direct", False)
        return self.db.record_booking(bid, K1, thread, PRIYA, s or slot(2, 18), T0, **kw)

    def test_recorded_as_intent_with_its_slot(self):
        self.assertTrue(self.book())
        b = self.db.booking("doris-1")
        self.assertEqual((b["booking_id"], b["message_key"], b["thread_key"], b["sender"], b["state"], b["run_id"]),
                         ("doris-1", K1, TH, PRIYA, "intent", RUN))
        self.assertEqual(b["slot"], slot(2, 18))
        self.assertIsNone(self.db.booking("doris-9"))

    def test_a_booking_keeps_whether_it_was_direct(self):
        # a restart reads it back instead of guessing from the offers (Fable review of PR #3, finding 2)
        self.book("doris-1", direct=True)
        self.book("doris-2", s=slot(3, 18), direct=False)
        self.assertIs(self.db.booking("doris-1")["direct"], True)
        self.assertIs(self.db.booking("doris-2")["direct"], False)
        self.assertEqual([b["direct"] for b in self.db.bookings_for_thread(TH)], [True, False])

    def test_direct_is_true_or_false(self):
        for bad in (None, 1, "yes"):
            with self.assertRaises(ValueError):
                self.book("doris-1", direct=bad)
        self.assertIsNone(self.db.booking("doris-1"))

    def test_a_booking_needs_a_run(self):
        fresh = Ledger(os.path.join(self._tmp.name, "other.sqlite"))
        self.extra.append(fresh)
        with self.assertRaises(ValueError):
            fresh.record_booking("doris-1", K1, TH, PRIYA, slot(2, 18), T0, direct=False)
        fresh.record_booking("doris-1", K1, TH, PRIYA, slot(2, 18), T0, run_id="run-x", direct=False)
        self.assertEqual(fresh.count_calendar_writes("run-x"), 1)

    def test_states_are_checked(self):
        self.book()
        with self.assertRaises(ValueError):
            self.db.set_booking_state("doris-1", "booked")
        with self.assertRaises(KeyError):
            self.db.set_booking_state("doris-9", "written")

    def test_same_booking_again_is_a_no_op_and_a_different_one_refused(self):
        self.book()
        self.db.set_booking_state("doris-1", "confirmed")
        self.assertFalse(self.book())
        self.assertEqual(self.db.booking("doris-1")["state"], "confirmed")
        with self.assertRaises(ValueError):
            self.book(s=slot(3, 18))

    def test_live_booking_is_the_oldest_live_one(self):
        self.book("doris-1")
        self.book("doris-2", s=slot(3, 18))
        self.assertEqual(self.db.live_booking_for_thread(TH)["booking_id"], "doris-1")
        self.db.set_booking_state("doris-1", "cancelled")
        self.assertEqual(self.db.live_booking_for_thread(TH)["booking_id"], "doris-2")
        self.db.set_booking_state("doris-2", "gone")
        self.assertIsNone(self.db.live_booking_for_thread(TH))
        self.assertEqual([b["booking_id"] for b in self.db.bookings_for_thread(TH)], ["doris-1", "doris-2"])

    def test_a_booking_the_gate_blocked_is_not_live_and_not_unfinished(self):
        # never written: not live on the thread, not looked up after a crash, still counted against the cap
        self.book("doris-1")
        self.db.set_booking_state("doris-1", "blocked")
        self.assertIsNone(self.db.live_booking_for_thread(TH))
        self.assertEqual(self.db.unfinished_bookings(), [])
        self.assertEqual(self.db.count_calendar_writes(RUN), 1)

    def test_future_bookings_are_written_or_confirmed_and_start_after_now(self):
        for bid, day, state in (("doris-1", 3, "confirmed"), ("doris-2", 2, "written"), ("doris-3", 4, "intent"),
                                ("doris-4", 4, "cancelled"), ("doris-5", 1, "confirmed")):
            self.book(bid, s=slot(day, 18), thread=bid)
            self.db.set_booking_state(bid, state)
        self.assertEqual([b["booking_id"] for b in self.db.future_bookings(dt.datetime(2026, 12, 1, 18, 0, tzinfo=UTC))],
                         ["doris-2", "doris-1"])

    def test_calendar_writes_counted_per_run(self):
        self.book("doris-1")
        self.book("doris-2", s=slot(3, 18), run_id="run-other")
        self.assertEqual(self.db.count_calendar_writes(RUN), 1)
        self.assertEqual(self.db.count_calendar_writes("run-other"), 1)
        self.assertEqual(self.db.count_calendar_writes("run-none"), 0)

    def test_unfinished_bookings_need_reconciling(self):
        for bid, state in (("doris-1", "intent"), ("doris-2", "written"), ("doris-3", "unconfirmed"),
                           ("doris-4", "confirmed"), ("doris-5", "removed")):
            self.book(bid, thread=bid)
            self.db.set_booking_state(bid, state)
        self.assertEqual([b["booking_id"] for b in self.db.unfinished_bookings()], ["doris-1", "doris-2", "doris-3"])


class FollowUps(Base):
    """The follow-up: one dated entry per offer (the nudge) and per booking (the calendar check)."""
    DUE = T0 + 48 * H

    def setUp(self):
        super().setUp()
        self.db.start_run(RUN, "sandbox", T0)
        self.oid = self.db.create_offer(TH, PRIYA, [slot(2, 18), slot(3, 19), slot(4, 20)], 30, T0,
                                        T0 + dt.timedelta(days=7), message_id="<o1@fastmail.com>")

    def nudge(self, **kw):
        kw.setdefault("offer_id", self.oid)
        return self.db.file_followup("nudge", TH, PRIYA, K1, kw.pop("due", self.DUE), kw.pop("now", T0), **kw)

    def booked(self, bid="doris-1", state="confirmed", thread=TH):
        self.db.record_booking(bid, K1, thread, PRIYA, slot(2, 18), T0, direct=False)
        self.db.set_booking_state(bid, state)

    def test_filed_once_dated_and_open(self):
        fid, new = self.nudge(detail={"subject": "Intro call", "zone": "Eastern", "references": ["<r1@x.example>"]})
        self.assertTrue(new)
        f = self.db.followup(fid)
        self.assertEqual((f["kind"], f["thread_key"], f["sender"], f["message_key"], f["offer_id"], f["booking_id"]),
                         ("nudge", TH, PRIYA, K1, self.oid, None))
        self.assertEqual((f["due"], f["filed"], f["state"], f["outcome"], f["closed"]), (self.DUE, T0, "open", None, None))
        self.assertEqual(f["detail"], {"subject": "Intro call", "zone": "Eastern", "references": ["<r1@x.example>"]})
        self.assertEqual(self.db.followup_for_offer(self.oid)["id"], fid)
        self.assertEqual(self.other().followup(fid)["due"], self.DUE)          # committed before it returned
        self.assertIsNone(self.db.followup(fid + 1))
        self.assertIsNone(self.db.followup_for_offer(self.oid + 1))

    def test_filed_again_it_is_the_same_entry_and_follows_the_offer_while_open(self):
        fid, _ = self.nudge(detail={"subject": "a"})
        again, new = self.nudge(due=self.DUE + H, detail={"subject": "b"})     # a restart sent the offer later
        self.assertEqual((again, new), (fid, False))
        self.assertEqual((self.db.followup(fid)["due"], self.db.followup(fid)["detail"]), (self.DUE + H, {"subject": "b"}))
        self.assertTrue(self.db.close_followup(fid, "nudged", self.DUE))
        self.assertEqual(self.nudge(due=self.DUE + 5 * H), (fid, False))       # settled: never opened or moved again
        f = self.db.followup(fid)
        self.assertEqual((f["state"], f["outcome"], f["due"], f["closed"]), ("done", "nudged", self.DUE + H, self.DUE))
        self.assertEqual(self.raw("SELECT COUNT(*) FROM followups"), [(1,)])

    def test_a_booking_check_is_one_per_booking(self):
        self.booked()
        fid, new = self.db.file_followup("booking_check", TH, PRIYA, K1, slot(2, 18).start_utc, T0,
                                         booking_id="doris-1", detail={"name": "Priya Raman"})
        self.assertTrue(new)
        self.assertEqual(self.db.followup_for_booking("doris-1")["id"], fid)
        self.assertEqual(self.db.file_followup("booking_check", TH, PRIYA, K1, slot(2, 18).start_utc, T0,
                                               booking_id="doris-1"), (fid, False))
        self.assertIsNone(self.db.followup_for_booking("doris-2"))

    def test_each_kind_names_its_own_thing_and_nothing_else(self):
        for kind, kw in (("nudge", {}), ("nudge", {"booking_id": "doris-1"}), ("booking_check", {}),
                         ("booking_check", {"offer_id": self.oid}), ("call", {"offer_id": self.oid}),
                         ("nudge", {"offer_id": self.oid, "booking_id": "doris-1"}), ("nudge", {"offer_id": "1"}),
                         ("nudge", {"offer_id": True}), ("nudge", {"offer_id": self.oid, "detail": ["x"]})):
            with self.subTest(kind=kind, **kw), self.assertRaises(ValueError):
                self.db.file_followup(kind, TH, PRIYA, K1, self.DUE, T0, **kw)
        with self.assertRaises(ValueError):
            self.db.file_followup("nudge", TH, PRIYA, K1, self.DUE.replace(tzinfo=None), T0, offer_id=self.oid)
        self.assertEqual(self.raw("SELECT COUNT(*) FROM followups"), [(0,)])

    def test_open_ones_come_soonest_due_first_and_by_kind_and_date(self):
        second = self.db.create_offer("t2@x.example", PRIYA, [slot(7, 18)], 30, T0, T0 + dt.timedelta(days=7))
        late, _ = self.nudge(due=self.DUE + H)
        soon, _ = self.db.file_followup("nudge", "t2@x.example", PRIYA, K2, self.DUE, T0, offer_id=second)
        self.booked()
        check, _ = self.db.file_followup("booking_check", TH, PRIYA, K1, self.DUE - H, T0, booking_id="doris-1")
        self.assertEqual([f["id"] for f in self.db.open_followups()], [check, soon, late])
        self.assertEqual([f["id"] for f in self.db.open_followups(kind="nudge")], [soon, late])
        self.assertEqual([f["id"] for f in self.db.open_followups(kind="nudge", due_by=self.DUE)], [soon])
        self.assertEqual(self.db.open_followups(kind="nudge", due_by=self.DUE - dt.timedelta(seconds=1)), [])
        self.db.close_followup(soon, "no_reply_needed", self.DUE)
        self.assertEqual([f["id"] for f in self.db.open_followups(kind="nudge")], [late])
        with self.assertRaises(ValueError):
            self.db.open_followups(kind="call")

    def test_settled_once(self):
        fid, _ = self.nudge()
        self.assertTrue(self.db.close_followup(fid, "nudged", self.DUE))
        self.assertFalse(self.db.close_followup(fid, "expired", self.DUE + H))
        self.assertEqual(self.db.followup(fid)["outcome"], "nudged")
        with self.assertRaises(KeyError):
            self.db.close_followup(fid + 9, "nudged", self.DUE)

    # --- the nudge's own mark on the offer -----------------------------------------------------
    def test_a_nudge_drops_slots_keeps_the_rest_and_moves_neither_date(self):
        self.db.mark_nudged(self.oid, [slot(4, 20), slot(2, 18)], self.DUE)
        [o] = self.db.open_offers(TH, self.DUE)
        self.assertEqual(o["slots"], [slot(2, 18), slot(4, 20)])          # in offered order, the dropped one closed
        self.assertEqual((o["created"], o["expires"], o["nudged"]), (T0, T0 + dt.timedelta(days=7), self.DUE))
        self.assertEqual(self.db.offer_count(TH), 1)                      # not a new offer
        # a dropped time was still offered once: it never comes back, and a booking made from it is still found
        self.assertEqual(self.db.offered_starts(TH), {slot(2, 18).start_utc, slot(3, 19).start_utc, slot(4, 20).start_utc})
        self.assertEqual(self.db.offer_holding(TH, slot(3, 19)), self.oid)

    def test_a_nudge_never_adds_a_time_or_brings_a_dropped_one_back(self):
        for keep in ([slot(7, 18)], [slot(2, 18), slot(7, 18)], [slot(2, 18, length=45)], []):
            with self.subTest(keep=len(keep)), self.assertRaises(ValueError):
                self.db.mark_nudged(self.oid, keep, self.DUE)
        self.assertEqual(self.db.open_offers(TH, T0)[0]["slots"], [slot(2, 18), slot(3, 19), slot(4, 20)])
        self.assertIsNone(self.db.open_offers(TH, T0)[0]["nudged"])
        self.db.mark_nudged(self.oid, [slot(2, 18)], self.DUE)
        with self.assertRaises(ValueError):
            self.db.mark_nudged(self.oid, [slot(2, 18), slot(3, 19)], self.DUE + H)
        self.db.mark_nudged(self.oid, [slot(2, 18)], self.DUE + H)        # the same nudge again after a restart
        self.assertEqual(self.db.open_offers(TH, T0)[0]["nudged"], self.DUE + H)
        with self.assertRaises(KeyError):
            self.db.mark_nudged(self.oid + 9, [slot(2, 18)], self.DUE)

    def test_one_offer_in_any_state(self):
        o = self.db.offer(self.oid)
        self.assertEqual((o["id"], o["used"], o["closed_reason"], o["nudged"]), (self.oid, False, None, None))
        self.db.close_offers(TH, "no_thanks")
        self.assertEqual(self.db.offer(self.oid)["closed_reason"], "no_thanks")
        self.assertIsNone(self.db.offer(self.oid + 9))

    def test_nudges_are_counted_per_thread_in_any_state(self):
        self.assertEqual(self.db.count_nudges(TH), 0)
        self.db.record_send("<n1@fastmail.com>", "send_reply", "T9", TH, PRIYA, sha("b"), T0, RUN)
        self.db.record_send("<o9@fastmail.com>", "send_reply", "T2", TH, PRIYA, sha("b"), T0, RUN)
        self.db.record_send("<n2@fastmail.com>", "send_reply", "T9", "t2@x.example", PRIYA, sha("b"), T0, RUN)
        self.assertEqual(self.db.count_nudges("<" + TH + ">"), 1)         # pending counts: fail closed
        self.assertEqual(self.db.count_nudges("t3@x.example"), 0)

    def test_has_the_sender_written_since(self):
        self.db.record_message(K1, TH, PRIYA, T0, "done")
        self.assertFalse(self.db.wrote_since(PRIYA, T0))                  # at the offer's own second: not since
        self.assertTrue(self.db.wrote_since(" Priya@Northwind.example ", T0 - H))
        self.db.record_message(K2, "t9@x.example", "sam@contoso.example", T0 + H, "held")
        self.assertFalse(self.db.wrote_since(PRIYA, T0))                  # someone else's mail is not theirs
        self.db.record_sensitive_skip("s1@bank.example", PRIYA, sha("raw"), "one-time-code", T0 + 2 * H)
        self.assertTrue(self.db.wrote_since(PRIYA, T0))                   # sensitive mail skipped unread still counts
        self.assertFalse(self.db.wrote_since(PRIYA, T0 + 2 * H))

    def test_the_message_her_offer_answered_is_never_mail_since_it(self):
        # a sandbox clock can stand still while its mail is dated later: the request is still not a reply
        self.db.record_message(K1, TH, PRIYA, T0 + H, "done")
        self.assertTrue(self.db.wrote_since(PRIYA, T0))
        self.assertFalse(self.db.wrote_since(PRIYA, T0, but=K1))
        self.assertFalse(self.db.wrote_since(PRIYA, T0, but="<" + K1 + ">"))
        self.db.record_message(K2, TH, PRIYA, T0 + 2 * H, "done")         # their next mail is one
        self.assertTrue(self.db.wrote_since(PRIYA, T0, but=K1))

    # --- ruling 17's mark ------------------------------------------------------------------------
    def test_a_booking_found_gone_is_marked_held_and_kept_open_in_one_step(self):
        self.booked()
        check, _ = self.db.file_followup("booking_check", TH, PRIYA, K1, slot(2, 18).start_utc, T0, booking_id="doris-1")
        self.assertEqual(self.db.gone_unfinished(), [])
        self.assertEqual(self.db.mark_booking_gone("doris-1", "<n1@fastmail.com>", T0 + H), check)
        seen = self.other()                                               # all of it was committed together
        self.assertEqual(seen.booking("doris-1")["state"], "gone")
        self.assertTrue(seen.thread_held(TH))
        h = seen.hold_for_message("booking:doris-1")
        self.assertEqual((h["reason_code"], h["notice_message_id"], h["thread_key"], h["created"]),
                         ("hold.owner_removed", "<n1@fastmail.com>", TH, T0 + H))
        [left] = seen.gone_unfinished()
        self.assertEqual((left["booking"]["booking_id"], left["followup"]["id"], left["notice_message_id"]),
                         ("doris-1", check, "<n1@fastmail.com>"))
        self.assertEqual(seen.future_bookings(T0), [])                    # never read again
        self.db.close_followup(check, "gone", T0 + H)
        self.assertEqual(self.db.gone_unfinished(), [])

    def test_a_booking_from_before_follow_ups_gets_one_when_it_is_found_gone(self):
        self.booked()
        fid = self.db.mark_booking_gone("doris-1", None, T0 + H)          # None: past the hourly notice cap
        f = self.db.followup(fid)
        self.assertEqual((f["kind"], f["booking_id"], f["thread_key"], f["sender"], f["message_key"], f["due"],
                          f["filed"], f["state"], f["detail"]),
                         ("booking_check", "doris-1", TH, PRIYA, K1, slot(2, 18).start_utc, T0 + H, "open", {}))
        self.assertEqual([g["notice_message_id"] for g in self.db.gone_unfinished()], [None])

    def test_only_a_booking_on_the_calendar_can_be_found_gone(self):
        for state in ("intent", "cancelled", "gone", "removed", "blocked", "unconfirmed", "collision_unresolved"):
            bid = "doris-" + state
            self.booked(bid, state, thread=bid)
            with self.subTest(state=state), self.assertRaises(ValueError):
                self.db.mark_booking_gone(bid, None, T0)
            self.assertEqual(self.db.booking(bid)["state"], state)
            self.assertFalse(self.db.thread_held(bid))
        with self.assertRaises(KeyError):
            self.db.mark_booking_gone("doris-none", None, T0)
        self.assertEqual(self.raw("SELECT COUNT(*) FROM holds"), [(0,)])

    def test_a_gone_booking_held_through_its_own_message_is_not_unfinished(self):
        # read again after a restart, her booking is found gone and held on that message (loop.resume_booking):
        # that hold is the message's own, so ruling 17 has nothing left to finish
        self.booked()
        check, _ = self.db.file_followup("booking_check", TH, PRIYA, K1, slot(2, 18).start_utc, T0, booking_id="doris-1")
        self.db.set_booking_state("doris-1", "gone")
        self.db.record_hold(TH, K1, "hold.owner_removed", "<n1@fastmail.com>", T0)
        self.assertEqual(self.db.gone_unfinished(), [])


class Holds(Base):
    def test_thread_held_after_a_hold(self):
        self.assertFalse(self.db.thread_held(TH))
        self.db.record_hold(TH, K1, "hold.money", None, T0)
        self.assertTrue(self.db.thread_held(TH))
        self.assertTrue(self.db.thread_held("<" + TH + ">"))
        self.assertFalse(self.db.thread_held("t2@x.example"))

    def test_the_hold_a_message_already_made(self):
        self.assertIsNone(self.db.hold_for_message(K1))
        self.db.record_hold(TH, K2, "hold.unsure", None, T0)
        self.db.record_hold(TH, K1, "hold.money", "<n1@fastmail.com>", T0)
        h = self.db.hold_for_message("<" + K1 + ">")
        self.assertEqual((h["reason_code"], h["notice_message_id"], h["thread_key"]),
                         ("hold.money", "<n1@fastmail.com>", TH))
        self.assertEqual(self.db.hold_for_message(K2)["reason_code"], "hold.unsure")


class Queue(Base):
    def test_queued_until_finished(self):
        self.message()
        a = self.db.enqueue_action(RUN, K1, "calendar_create", {"booking_id": "doris-1", "at": T0})
        b = self.db.enqueue_action("run-2", K1, "send_reply", {"template": "T1"})
        self.assertLess(a, b)
        self.db.start_action(a)
        listed = self.db.queued_actions()
        self.assertEqual([(x["id"], x["state"]) for x in listed], [(a, "started"), (b, "queued")])
        self.assertEqual((listed[0]["kind"], listed[0]["message_key"], listed[0]["thread_key"], listed[0]["run_id"]),
                         ("calendar_create", K1, TH, RUN))
        self.assertEqual(listed[0]["detail"], {"booking_id": "doris-1", "at": "2026-11-30T17:00:00Z"})
        self.assertEqual([x["id"] for x in self.db.queued_actions("run-2")], [b])
        self.db.finish_action(a, "written")
        self.assertEqual([x["id"] for x in self.db.queued_actions()], [b])
        self.assertEqual(self.db.action(a)["outcome"], "written")
        with self.assertRaises(ValueError):
            self.db.finish_action(a, "again")

    def test_drop_queued_returns_what_it_dropped_and_they_never_run(self):
        a = self.db.enqueue_action(RUN, K1, "calendar_create", {"booking_id": "doris-1"})
        b = self.db.enqueue_action(RUN, K2, "send_reply", {"template": "T2"})
        c = self.db.enqueue_action(RUN, K2, "send_reply", {"template": "T1"})
        self.db.start_action(a)
        self.db.finish_action(c, "confirmed")
        dropped = self.db.drop_queued("watch_button")
        self.assertEqual([(d["id"], d["was"], d["state"], d["drop_cause"]) for d in dropped],
                         [(a, "started", "dropped", "watch_button"), (b, "queued", "dropped", "watch_button")])
        self.assertEqual(self.db.queued_actions(), [])
        self.assertEqual(self.db.drop_queued("cli"), [])
        for act in (self.db.start_action, lambda i: self.db.finish_action(i, "late")):
            with self.assertRaises(ValueError):
                act(b)
        with self.assertRaises(KeyError):
            self.db.start_action(999)

    def test_detail_must_be_json_safe(self):
        with self.assertRaises(ValueError):
            self.db.enqueue_action(RUN, K1, "send_reply", {"x": {1, 2}})
        self.assertEqual(self.db.queued_actions(), [])


class ActionsForAMessage(Base):
    def test_every_action_for_a_message_oldest_first(self):
        self.message()
        a = self.db.enqueue_action(RUN, K1, "cancel", {"booking_id": "doris-1"})
        self.db.start_action(a)
        b = self.db.enqueue_action("run-2", K1, "move", {"booking_id": "doris-2"})
        self.assertEqual([(x["id"], x["kind"], x["state"]) for x in self.db.actions_for_message("<%s>" % K1)],
                         [(a, "cancel", "started"), (b, "move", "queued")])
        self.assertEqual(self.db.actions_for_message("other@x.example"), [])


class Spend(Base):
    def test_spend_adds_up_per_run(self):
        self.db.add_spend(RUN, 0.5)
        self.db.add_spend(RUN, 0.25)
        self.db.add_spend("run-2", 1.0)
        self.assertEqual(self.db.spend(RUN), 0.75)
        self.assertEqual(self.db.spend("run-none"), 0.0)

    def test_negative_or_non_finite_spend_refused(self):
        for bad in (-0.01, float("nan"), float("inf"), True, "0.1"):
            with self.subTest(usd=bad), self.assertRaises(ValueError):
                self.db.add_spend(RUN, bad)
        self.assertEqual(self.db.spend(RUN), 0.0)


class Runs(Base):
    def test_start_and_end(self):
        self.db.start_run(RUN, "sandbox", T0)
        self.db.start_run(RUN, "sandbox", T0 + H)
        self.db.start_run("run-2", "sandbox", T0 + H)
        self.assertEqual([(r["run_id"], r["started_utc"]) for r in self.db.unended_runs()], [(RUN, T0), ("run-2", T0 + H)])
        self.db.end_run(RUN, T0 + 2 * H)
        self.assertEqual([r["run_id"] for r in self.db.unended_runs()], ["run-2"])
        with self.assertRaises(ValueError):
            self.db.start_run("run-2", "live", T0)
        with self.assertRaises(ValueError):
            self.db.start_run("run-3", "demo", T0)
        with self.assertRaises(KeyError):
            self.db.end_run("run-9", T0)

    def test_every_run_in_the_order_it_started(self):
        # a sandbox restart's clock begins at clock.txt again, so the order is the ledger's, not the clock's
        self.db.start_run("run-b", "sandbox", T0 + H)
        self.db.end_run("run-b", T0 + 2 * H)
        self.db.start_run("run-a", "sandbox", T0)
        self.assertEqual([(r["run_id"], r["started_utc"], r["ended_utc"]) for r in self.db.runs()],
                         [("run-b", T0 + H, T0 + 2 * H), ("run-a", T0, None)])


class SensitiveSkip(Base):
    MID = "otp-7731-secret@bank.example"

    def test_only_sender_hashes_and_check_name_are_kept(self):
        self.db.record_fetch("05-bank.eml", self.MID, RUN, T0)
        self.db.record_message(self.MID, self.MID, "alerts@bank.example", T0, "fetched", run_id=RUN)
        self.db.record_sensitive_skip(self.MID, "Alerts@Bank.example", "ab" * 32, "one-time-code", T0)
        self.db.record_sensitive_skip(self.MID, "Alerts@Bank.example", "ab" * 32, "one-time-code", T0)
        self.assertEqual(self.db.sensitive_skips(), [{"key_sha256": sha(self.MID), "sender": "alerts@bank.example",
                                                      "message_sha256": "ab" * 32, "check": "one-time-code",
                                                      "skipped_utc": T0}])
        self.assertTrue(self.db.message_seen(self.MID))
        self.assertTrue(self.db.message_seen("<" + self.MID + ">"))
        self.assertTrue(self.db.server_id_seen("05-bank.eml"))
        self.assertIsNone(self.db.message(self.MID))
        self.assertEqual(self.db.unprocessed_messages(), [])
        self.db.record_fetch("06-bank-forwarded.eml", "<" + self.MID + ">", RUN, T0)
        with self.assertRaises(ValueError):
            self.db.record_message(self.MID, self.MID, "alerts@bank.example", T0, "fetched")
        self.db.close()
        with open(self.path, "rb") as fh:
            data = fh.read()
        self.assertFalse(b"otp-7731-secret" in data, "the Message-ID's bytes are still in the ledger file")
        self.assertTrue(sha(self.MID).encode("ascii") in data, "the Message-ID's hash is not in the ledger file")
        self.db = Ledger(self.path)


TIME_LIKE = re.compile(r"^\d{4}-\d\d-\d\d[T ]\d\d:\d\d")
ISO_Z = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


class TimesAsIsoUtc(Base):
    def test_every_stored_time_is_iso_utc_z(self):
        pacific = dt.timezone(dt.timedelta(hours=-8))
        local = T0.astimezone(pacific)
        db = self.db
        db.start_run(RUN, "sandbox", local)
        db.record_fetch("01.eml", K1, RUN, local)
        db.record_message(K1, TH, PRIYA, local, "fetched")
        db.record_send("<a1@fastmail.com>", "send_reply", "T2", TH, PRIYA, sha("b"), local, RUN)
        db.silence(PRIYA, "stop", K1, local)
        db.create_offer(TH, PRIYA, [Slot(slot(2, 18).start_utc.astimezone(pacific), slot(2, 18).end_utc)], 30,
                        local, local + H)
        db.record_booking("doris-1", K1, TH, PRIYA, slot(2, 18), local, direct=False)
        db.record_hold(TH, K1, "hold.money", None, local)
        db.record_sensitive_skip(K2, PRIYA, "cd" * 32, "ssn", local)
        db.end_run(RUN, local)
        stamps = []
        for (table,) in self.raw("SELECT name FROM sqlite_master WHERE type = 'table'"):
            for row in self.raw('SELECT * FROM "%s"' % table):
                stamps += [v for v in row if isinstance(v, str) and TIME_LIKE.match(v)]
        self.assertGreaterEqual(len(stamps), 12)
        for value in stamps:
            self.assertRegex(value, ISO_Z)
        self.assertIn("2026-11-30T17:00:00Z", stamps)
        self.assertIn("2026-12-02T18:00:00Z", stamps)
        self.assertEqual(db.open_offers(TH, T0)[0]["slots"][0].start_utc, slot(2, 18).start_utc)


CHILD = r"""
import datetime, sys
sys.path.insert(0, sys.argv[2])
from doris.ledger import Ledger
db = Ledger(sys.argv[1])
now = datetime.datetime(2026, 11, 30, 17, 0, tzinfo=datetime.timezone.utc)
db.start_run("run-kill", "sandbox", now)
n = 0
while True:
    n += 1
    key = "k%d@northwind.example" % n
    db.record_message(key, key, "priya@northwind.example", now, "fetched", run_id="run-kill")
    aid = db.enqueue_action("run-kill", key, "send_reply", {"template": "T2", "pad": "x" * 2000})
    db.add_spend("run-kill", 0.5)
    sys.stdout.write("%d %d\n" % (n, aid))
    sys.stdout.flush()
"""


class HardKill(Base):
    def test_a_killed_writer_loses_nothing_it_reported(self):
        self.db.close()
        proc = subprocess.Popen([sys.executable, "-c", CHILD, self.path, SRC],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        reported = []
        try:
            while len(reported) < 25:
                out = proc.stdout.readline()
                if not out:
                    break
                reported.append(tuple(int(x) for x in out.split()))
        finally:
            proc.kill()
            proc.wait()
            proc.stdout.close()
            err = proc.stderr.read()
            proc.stderr.close()
        self.assertGreaterEqual(len(reported), 25, err)
        self.assertEqual(self.raw("PRAGMA integrity_check"), [("ok",)])
        self.db = Ledger(self.path)
        queued = {a["id"] for a in self.db.queued_actions("run-kill")}
        for n, aid in reported:
            self.assertTrue(self.db.message_seen("k%d@northwind.example" % n))
            self.assertIn(aid, queued)
        self.assertGreaterEqual(self.db.spend("run-kill"), 0.5 * len(reported))
        self.assertEqual([r["run_id"] for r in self.db.unended_runs()], ["run-kill"])
        dropped = self.db.drop_queued("cli")
        self.assertGreaterEqual(len(dropped), len(reported))


if __name__ == "__main__":
    unittest.main()
