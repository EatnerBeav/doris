"""adapters/sandbox.py: the sandbox mail and calendar behave like the live ones, failures included."""
import datetime as dt
import json
import os
import shutil
import tempfile
import time
import unittest

from doris import caps, ics, slots
from doris.adapters import sandbox
from doris.adapters.base import CalendarAdapter, MailAdapter
from doris.shapes import AlreadyExists, CalendarError, CalendarTimeout, MailError, SendUnknown, Slot

UTC = dt.timezone.utc
LINK = "https://meet.jit.si/htm-example-room"


def u(*args):
    return dt.datetime(*args, tzinfo=UTC)


def vcal(*lines):
    return ("\r\n".join(("BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Fixture//EN") + lines + ("END:VCALENDAR",))
            + "\r\n").encode("utf-8")


def vevent(uid, start, end=None, *extra):
    lines = ["BEGIN:VEVENT", "UID:" + uid, "SUMMARY:Private title " + uid, "DTSTART" + start]
    if end:
        lines.append("DTEND" + end)
    return tuple(lines) + extra + ("END:VEVENT",)


def hers(booking_id, slot, status="CONFIRMED"):
    return ics.build_event(booking_id, slot, LINK, status, 0, u(2026, 12, 1, 16, 0), None)


class Fixture(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="doris-sandbox-test-")
        self.addCleanup(shutil.rmtree, self.root, True)
        os.makedirs(os.path.join(self.root, "calendar"))
        os.makedirs(os.path.join(self.root, "inbox"))
        self.cal = sandbox.SandboxCalendar(self.root, timeout_s=0.01)

    def put(self, name, *lines):
        with open(os.path.join(self.root, "calendar", name), "wb") as fh:
            fh.write(vcal(*lines))

    def cal_files(self):
        return sorted(os.listdir(os.path.join(self.root, "calendar")))


# Query span used by most tests: Tue Dec 1 00:00 UTC to Fri Jan 1 2027 00:00 UTC.
SPAN = (u(2026, 12, 1), u(2027, 1, 1))


class CalendarQueryTest(Fixture):

    def test_is_a_calendar_adapter(self):
        self.assertIsInstance(self.cal, CalendarAdapter)

    def test_returns_overlapping_timed_events_with_times_and_status_only(self):
        self.put("a.ics", *vevent("a", ":20261202T170000Z", ":20261202T190000Z", "STATUS:CONFIRMED"))
        self.put("b.ics", *vevent("b", ":20261202T190000Z", ":20261202T200000Z", "TRANSP:TRANSPARENT"))
        found = self.cal.query(u(2026, 12, 2, 18, 0), u(2026, 12, 2, 19, 0))
        self.assertEqual([(e.uid, e.kind, e.start_utc, e.end_utc, e.status) for e in found],
                         [("a", "timed", u(2026, 12, 2, 17, 0), u(2026, 12, 2, 19, 0), "CONFIRMED")])
        self.assertNotIn("Private", repr(found))

    def test_cancelled_events_are_returned_with_their_status_and_never_count_as_busy(self):
        self.put("c.ics", *vevent("c", ":20261202T180000Z", ":20261202T183000Z", "STATUS:CANCELLED"))
        found = self.cal.query(*SPAN)
        self.assertEqual([e.status for e in found], ["CANCELLED"])
        self.assertFalse(slots.busy(found, Slot(u(2026, 12, 2, 18, 0), u(2026, 12, 2, 18, 30))))

    def test_all_day_event(self):
        self.put("d.ics", *vevent("d", ";VALUE=DATE:20261203", ";VALUE=DATE:20261204"))
        found = self.cal.query(*SPAN)
        self.assertEqual([(e.kind, e.start_date, e.end_date) for e in found],
                         [("all_day", dt.date(2026, 12, 3), dt.date(2026, 12, 4))])

    def test_tzid_and_floating_events_are_unknown_zone_and_busy_all_pacific_day(self):
        self.put("t.ics", *vevent("t", ";TZID=Europe/Paris:20261202T060000", ";TZID=Europe/Paris:20261202T063000"))
        self.put("f.ics", *vevent("f", ":20261203T060000", ":20261203T063000"))
        # Wed Dec 2, 3:00 PM Pacific: far from 6:00 AM read as Pacific, but on the same Pacific day.
        afternoon = Slot(u(2026, 12, 2, 23, 0), u(2026, 12, 2, 23, 30))
        found = self.cal.query(afternoon.start_utc, afternoon.end_utc)
        self.assertEqual([(e.uid, e.kind) for e in found], [("t", "unknown_zone")])
        self.assertTrue(slots.busy(found, afternoon))
        self.assertEqual({e.uid for e in self.cal.query(*SPAN) if e.kind == "unknown_zone"}, {"t", "f"})

    def test_weekly_repeat_with_byday_and_count(self):
        # Mon Nov 30 10:00 AM Pacific, weekly on Mon and Wed, 3 times: Nov 30, Dec 2, Dec 7.
        self.put("w.ics", *vevent("w", ":20261130T180000Z", ":20261130T183000Z",
                                  "RRULE:FREQ=WEEKLY;BYDAY=MO,WE;COUNT=3"))
        self.assertEqual([e.start_utc for e in self.cal.query(*SPAN)],
                         [u(2026, 12, 2, 18, 0), u(2026, 12, 7, 18, 0)])

    def test_daily_repeat_with_interval_and_until(self):
        self.put("d.ics", *vevent("d", ":20261201T170000Z", ":20261201T173000Z",
                                  "RRULE:FREQ=DAILY;INTERVAL=2;UNTIL=20261207T170000Z"))
        self.assertEqual([e.start_utc for e in self.cal.query(*SPAN)],
                         [u(2026, 12, 1, 17, 0), u(2026, 12, 3, 17, 0), u(2026, 12, 5, 17, 0), u(2026, 12, 7, 17, 0)])

    def test_every_other_week_without_an_end(self):
        self.put("e.ics", *vevent("e", ":20261202T180000Z", ":20261202T190000Z", "RRULE:FREQ=WEEKLY;INTERVAL=2"))
        self.assertEqual([e.start_utc for e in self.cal.query(*SPAN)],
                         [u(2026, 12, 2, 18, 0), u(2026, 12, 16, 18, 0), u(2026, 12, 30, 18, 0)])

    def test_series_that_started_years_ago_still_repeats_inside_the_span(self):
        self.put("o.ics", *vevent("o", ":20200106T170000Z", ":20200106T173000Z", "RRULE:FREQ=WEEKLY;BYDAY=MO"))
        self.assertEqual([e.start_utc for e in self.cal.query(u(2026, 12, 1), u(2026, 12, 15))],
                         [u(2026, 12, 7, 17, 0), u(2026, 12, 14, 17, 0)])

    def test_daily_weekdays_only(self):
        self.put("k.ics", *vevent("k", ":20261204T170000Z", ":20261204T173000Z",
                                  "RRULE:FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR;COUNT=3"))
        self.assertEqual([e.start_utc for e in self.cal.query(*SPAN)],
                         [u(2026, 12, 4, 17, 0), u(2026, 12, 7, 17, 0), u(2026, 12, 8, 17, 0)])

    def test_weekly_all_day_and_unknown_zone_repeats(self):
        self.put("a.ics", *vevent("a", ";VALUE=DATE:20261204", ";VALUE=DATE:20261205", "RRULE:FREQ=WEEKLY;COUNT=2"))
        self.put("z.ics", *vevent("z", ";TZID=X:20261207T090000", ";TZID=X:20261207T100000",
                                  "RRULE:FREQ=WEEKLY;COUNT=2"))
        found = self.cal.query(*SPAN)
        self.assertEqual(sorted((e.uid, e.start_date) for e in found),
                         [("a", dt.date(2026, 12, 4)), ("a", dt.date(2026, 12, 11)),
                          ("z", dt.date(2026, 12, 7)), ("z", dt.date(2026, 12, 14))])

    def test_repeat_rule_it_cannot_expand_is_busy_over_the_whole_span(self):
        for i, rule in enumerate(("RRULE:FREQ=MONTHLY;BYMONTHDAY=3", "RRULE:FREQ=WEEKLY;BYDAY=1MO",
                                  "RRULE:FREQ=DAILY;COUNT=3;UNTIL=20261210T000000Z", "RRULE:FREQ=DAILY;BYHOUR=9",
                                  "RRULE:FREQ=HOURLY", "RRULE:FREQ=DAILY;INTERVAL=0")):
            self.put("r%d.ics" % i, *vevent("r%d" % i, ":20261130T180000Z", ":20261130T183000Z", rule))
        self.put("rd.ics", *vevent("rd", ":20261130T180000Z", ":20261130T183000Z", "RDATE:20261215T180000Z"))
        start, end = u(2026, 12, 10, 17, 0), u(2026, 12, 10, 18, 0)
        found = self.cal.query(start, end)
        self.assertEqual(len(found), 7)
        self.assertTrue(all((e.kind, e.start_utc, e.end_utc) == ("timed", start, end) for e in found))

    def test_unreadable_file_is_busy_over_the_whole_span(self):
        with open(os.path.join(self.root, "calendar", "broken.ics"), "wb") as fh:
            fh.write(b"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nDTSTART:garbage\r\n")
        found = self.cal.query(*SPAN)
        self.assertEqual([(e.uid, e.start_utc, e.end_utc) for e in found], [("broken", SPAN[0], SPAN[1])])

    def test_other_files_in_the_folder_are_ignored(self):
        with open(os.path.join(self.root, "calendar", "README.txt"), "wb") as fh:
            fh.write(b"not an event")
        self.assertEqual(self.cal.query(*SPAN), [])

    def test_missing_calendar_folder_raises_never_empty(self):
        shutil.rmtree(os.path.join(self.root, "calendar"))
        with self.assertRaises(CalendarError):
            self.cal.query(*SPAN)

    def test_naive_or_backward_span_is_refused(self):
        with self.assertRaises(ValueError):
            self.cal.query(dt.datetime(2026, 12, 1), dt.datetime(2026, 12, 2))
        with self.assertRaises(ValueError):
            self.cal.query(SPAN[1], SPAN[0])


WED_11 = Slot(u(2026, 12, 2, 19, 0), u(2026, 12, 2, 19, 30))   # Wed Dec 2, 11:00 AM Pacific
MON_10 = Slot(u(2026, 12, 7, 18, 0), u(2026, 12, 7, 18, 30))   # Mon Dec 7, 10:00 AM Pacific


class CalendarWriteTest(Fixture):

    def test_create_only_writes_her_event_under_its_booking_id(self):
        raw = hers("doris-aaa1", WED_11)
        self.cal.create_only("doris-aaa1", raw)
        with open(os.path.join(self.root, "calendar", "doris-aaa1.ics"), "rb") as fh:
            self.assertEqual(fh.read(), raw)
        ev = self.cal.get("doris-aaa1")
        self.assertEqual((ev.uid, ev.start_utc, ev.end_utc, ev.status),
                         ("doris-aaa1", WED_11.start_utc, WED_11.end_utc, "CONFIRMED"))
        self.assertEqual([e.uid for e in self.cal.query(WED_11.start_utc, WED_11.end_utc)], ["doris-aaa1"])

    def test_create_only_refuses_an_existing_booking_id(self):
        self.cal.create_only("doris-aaa1", hers("doris-aaa1", WED_11))
        with self.assertRaises(AlreadyExists):
            self.cal.create_only("doris-aaa1", hers("doris-aaa1", MON_10))
        self.assertEqual(self.cal.get("doris-aaa1").start_utc, WED_11.start_utc)

    def test_create_only_refuses_ids_that_are_not_hers_and_foreign_fields(self):
        raw = hers("doris-aaa1", WED_11)
        for bad_id in ("owner-1", "doris-../x", "doris-a\\b", "../doris-x"):
            with self.assertRaises(CalendarError):
                self.cal.create_only(bad_id, raw)
        with self.assertRaises(CalendarError):     # UID inside differs from the booking ID
            self.cal.create_only("doris-bbb2", raw)
        organizer = raw.replace(b"END:VEVENT", b"ORGANIZER:mailto:x@y.example\r\nEND:VEVENT")
        with self.assertRaises(CalendarError):
            self.cal.create_only("doris-aaa1", organizer)
        self.assertEqual(self.cal_files(), [])

    def test_get_missing_is_none_and_never_reads_others(self):
        self.assertIsNone(self.cal.get("doris-nope"))
        self.put("owner-1.ics", *vevent("owner-1", ":20261202T170000Z", ":20261202T180000Z"))
        with self.assertRaises(CalendarError):
            self.cal.get("owner-1")

    def test_get_of_an_unreadable_or_mismatched_file_raises(self):
        with open(os.path.join(self.root, "calendar", "doris-torn.ics"), "wb") as fh:
            fh.write(b"BEGIN:VCALENDAR\r\nBEGIN:VEV")
        with self.assertRaises(CalendarError):
            self.cal.get("doris-torn")
        self.put("doris-x.ics", *vevent("owner-planted", ":20261202T170000Z", ":20261202T180000Z"))
        with self.assertRaises(CalendarError):
            self.cal.get("doris-x")

    def test_delete_removes_only_her_own_event(self):
        self.cal.create_only("doris-aaa1", hers("doris-aaa1", WED_11))
        self.put("owner-1.ics", *vevent("owner-1", ":20261202T170000Z", ":20261202T180000Z"))
        for bad_id in ("owner-1", "doris-../owner-1"):
            with self.assertRaises(CalendarError):
                self.cal.delete(bad_id)
        self.cal.delete("doris-aaa1")
        self.assertIsNone(self.cal.get("doris-aaa1"))
        self.assertEqual(self.cal_files(), ["owner-1.ics"])

    def test_delete_refuses_a_planted_file_with_her_prefix_but_another_uid(self):
        self.put("doris-x.ics", *vevent("owner-planted", ":20261202T170000Z", ":20261202T180000Z"))
        with self.assertRaises(CalendarError):
            self.cal.delete("doris-x")
        self.assertEqual(self.cal_files(), ["doris-x.ics"])

    def test_delete_of_an_absent_booking_is_a_no_op(self):
        self.cal.delete("doris-gone")
        self.assertEqual(self.cal_files(), [])

    def test_missing_calendar_folder_is_a_failure_never_absent(self):
        shutil.rmtree(os.path.join(self.root, "calendar"))
        with self.assertRaises(CalendarError):
            self.cal.get("doris-aaa1")
        with self.assertRaises(CalendarError):
            self.cal.delete("doris-aaa1")
        with self.assertRaises(CalendarError):
            self.cal.create_only("doris-aaa1", hers("doris-aaa1", WED_11))

    def test_the_calendar_write_carries_no_method_line(self):
        attachment = ics.build_event("doris-aaa1", WED_11, LINK, "CONFIRMED", 0, u(2026, 12, 1, 16, 0), "PUBLISH")
        with self.assertRaises(CalendarError):
            self.cal.create_only("doris-aaa1", attachment)
        self.assertEqual(self.cal_files(), [])


class CalendarFaultTest(Fixture):

    def _assert_staged(self, exc, fault):
        self.assertTrue(getattr(exc, "staged", False))
        self.assertEqual(exc.fault, fault)

    def test_timeout_once_fails_the_first_call_then_recovers(self):
        cal = sandbox.SandboxCalendar(self.root, timeout_s=0.05)
        cal.arm("timeout-once")
        began = time.monotonic()
        with self.assertRaises(CalendarTimeout) as ctx:
            cal.query(*SPAN)
        self.assertGreaterEqual(time.monotonic() - began, 0.05)
        self._assert_staged(ctx.exception, "timeout-once")
        self.assertEqual(cal.last_call_fault, "timeout-once")
        self.assertEqual(cal.query(*SPAN), [])
        self.assertIsNone(cal.last_call_fault)

    def test_timeout_once_on_a_write_writes_nothing(self):
        self.cal.arm("timeout-once")
        with self.assertRaises(CalendarTimeout):
            self.cal.create_only("doris-aaa1", hers("doris-aaa1", WED_11))
        self.assertIsNone(self.cal.get("doris-aaa1"))

    def test_timeout_twice_fails_two_calls(self):
        self.cal.arm("timeout-twice")
        for _ in range(2):
            with self.assertRaises(CalendarTimeout):
                self.cal.query(*SPAN)
        self.assertEqual(self.cal.query(*SPAN), [])

    def test_read_fails_every_read_never_free(self):
        self.cal.arm("read-fails")
        for _ in range(3):
            with self.assertRaises(CalendarError) as ctx:
                self.cal.query(*SPAN)
            self.assertNotIsInstance(ctx.exception, CalendarTimeout)
            self._assert_staged(ctx.exception, "read-fails")
        self.cal.disarm()
        self.assertEqual(self.cal.query(*SPAN), [])

    def test_slow_calendar_waits_then_answers(self):
        self.cal.arm("slow-calendar", seconds=0.2)
        began = time.monotonic()
        self.assertEqual(self.cal.query(*SPAN), [])
        self.assertGreaterEqual(time.monotonic() - began, 0.2)
        self.assertEqual(self.cal.last_call_fault, "slow-calendar")

    def test_slow_calendar_past_the_call_timeout_times_out(self):
        waits = []
        cal = sandbox.SandboxCalendar(self.root, sleep=waits.append)
        cal.arm("slow-calendar", seconds=30)
        with self.assertRaises(CalendarTimeout):
            cal.create_only("doris-aaa1", hers("doris-aaa1", WED_11))
        self.assertEqual(waits, [caps.CALENDAR_TIMEOUT_S])
        cal.disarm()
        self.assertIsNone(cal.get("doris-aaa1"))

    def test_lands_between_puts_an_event_on_the_calendar_before_her_write(self):
        self.cal.arm("lands-between")
        # Both reads before the write (the loop's re-check, then the gate's G11) are clean.
        self.assertEqual(self.cal.query(MON_10.start_utc, MON_10.end_utc), [])
        self.assertEqual(self.cal.query(MON_10.start_utc, MON_10.end_utc), [])
        self.cal.create_only("doris-sam1", hers("doris-sam1", MON_10))
        self.assertEqual(self.cal.last_call_fault, "lands-between")
        reread = self.cal.query(MON_10.start_utc, MON_10.end_utc)
        others = [e for e in reread if e.uid != "doris-sam1"]
        self.assertEqual(len(reread), 2)
        self.assertEqual([(e.status, e.transp, e.uid.startswith("doris-")) for e in others],
                         [("CONFIRMED", "OPAQUE", False)])
        # Mon Dec 7, 10:00 to 11:00 AM Pacific, as on demo day.
        self.assertEqual([(e.start_utc, e.end_utc) for e in others], [(u(2026, 12, 7, 18, 0), u(2026, 12, 7, 19, 0))])
        self.assertTrue(slots.busy(others, MON_10))
        with self.assertRaises(CalendarError):
            self.cal.delete(others[0].uid)
        self.cal.delete("doris-sam1")
        self.assertEqual([e.uid for e in self.cal.query(MON_10.start_utc, MON_10.end_utc)], [others[0].uid])

    def test_lands_between_takes_its_span_from_facts_as_written(self):
        # facts.json for 08a: "fault_start" and "fault_end", ISO UTC strings, prefix stripped by the loop.
        self.cal.arm("lands-between", start="2026-12-07T18:15:00Z", end="2026-12-07T19:45:00Z")
        self.cal.create_only("doris-sam1", hers("doris-sam1", MON_10))
        others = [e for e in self.cal.query(*SPAN) if e.uid != "doris-sam1"]
        self.assertEqual([(e.start_utc, e.end_utc) for e in others], [(u(2026, 12, 7, 18, 15), u(2026, 12, 7, 19, 45))])

    def test_fault_params_written_as_strings(self):
        waits = []
        cal = sandbox.SandboxCalendar(self.root, sleep=waits.append)
        cal.arm("slow-calendar", seconds="2.5")
        cal.query(*SPAN)
        cal.arm("timeout-once", seconds="0.5")
        with self.assertRaises(CalendarTimeout):
            cal.query(*SPAN)
        self.assertEqual(waits, [2.5, 0.5])

    def test_lands_between_fires_once(self):
        self.cal.arm("lands-between")
        self.cal.create_only("doris-sam1", hers("doris-sam1", MON_10))
        self.cal.create_only("doris-wed1", hers("doris-wed1", WED_11))
        self.assertEqual(len(self.cal.query(WED_11.start_utc, WED_11.end_utc)), 1)

    def test_write_unknown_present(self):
        self.cal.arm("write-unknown-present")
        with self.assertRaises(CalendarTimeout) as ctx:
            self.cal.create_only("doris-aaa1", hers("doris-aaa1", WED_11))
        self._assert_staged(ctx.exception, "write-unknown-present")
        self.assertEqual(self.cal.get("doris-aaa1").start_utc, WED_11.start_utc)

    def test_write_unknown_absent_then_one_retry_lands(self):
        self.cal.arm("write-unknown-absent")
        raw = hers("doris-aaa1", WED_11)
        with self.assertRaises(CalendarTimeout):
            self.cal.create_only("doris-aaa1", raw)
        self.assertIsNone(self.cal.get("doris-aaa1"))
        self.cal.create_only("doris-aaa1", raw)
        self.assertEqual(self.cal.get("doris-aaa1").uid, "doris-aaa1")

    def test_lookup_fails(self):
        self.cal.arm("lookup-fails")
        with self.assertRaises(CalendarTimeout):
            self.cal.create_only("doris-aaa1", hers("doris-aaa1", WED_11))
        for _ in range(2):
            with self.assertRaises(CalendarError) as ctx:
                self.cal.get("doris-aaa1")
            self._assert_staged(ctx.exception, "lookup-fails")

    def test_remove_fails_once(self):
        self.cal.create_only("doris-aaa1", hers("doris-aaa1", WED_11))
        self.cal.arm("remove-fails-once")
        with self.assertRaises(CalendarError) as ctx:
            self.cal.delete("doris-aaa1")
        self._assert_staged(ctx.exception, "remove-fails-once")
        self.assertIsNotNone(self.cal.get("doris-aaa1"))
        self.cal.delete("doris-aaa1")
        self.assertIsNone(self.cal.get("doris-aaa1"))

    def test_remove_fails_always(self):
        self.cal.create_only("doris-aaa1", hers("doris-aaa1", WED_11))
        self.cal.arm("remove-fails-always")
        for _ in range(3):
            with self.assertRaises(CalendarError):
                self.cal.delete("doris-aaa1")
        self.assertIsNotNone(self.cal.get("doris-aaa1"))
        self.cal.disarm()
        self.cal.delete("doris-aaa1")
        self.assertIsNone(self.cal.get("doris-aaa1"))

    def test_login_refused_fails_every_call_loudly(self):
        self.cal.arm("login-refused")
        with self.assertRaises(CalendarError) as ctx:
            self.cal.query(*SPAN)
        self._assert_staged(ctx.exception, "login-refused")
        with self.assertRaises(CalendarError):
            self.cal.create_only("doris-aaa1", hers("doris-aaa1", WED_11))
        self.assertEqual(self.cal_files(), [])

    def test_arm_checks_its_params_up_front(self):
        for name, params in (("lands-between", {"start": "Monday at ten"}),
                             ("lands-between", {"strat": "2026-12-07T18:00:00Z"}),
                             ("lands-between", {"start": "2026-12-07T19:00:00Z", "end": "2026-12-07T18:00:00Z"}),
                             ("slow-calendar", {"seconds": "-1"}),
                             ("slow-calendar", {"seconds": "soon"}),
                             ("read-fails", {"seconds": "1"})):
            with self.assertRaises(ValueError, msg=(name, params)):
                self.cal.arm(name, **params)

    def test_arming_is_per_message(self):
        with self.assertRaises(ValueError):
            self.cal.arm("sent-unconfirmed")
        with self.assertRaises(ValueError):
            self.cal.arm("no-such-fault")
        self.cal.arm("timeout-once")
        with self.assertRaises(CalendarTimeout):
            self.cal.query(*SPAN)
        self.cal.arm("timeout-once")       # the next message: counts start again
        with self.assertRaises(CalendarTimeout):
            self.cal.query(*SPAN)
        self.cal.disarm()
        self.assertEqual(self.cal.query(*SPAN), [])

    def test_route_faults_to_the_right_adapter(self):
        mail = sandbox.SandboxMail(self.root, "doris.htm@fastmail.com")
        self.assertEqual(sandbox.arm_fault("lands-between", mail, self.cal), [self.cal])
        self.assertEqual(sandbox.arm_fault("sent-unconfirmed", mail, self.cal), [mail])
        self.assertEqual(sandbox.arm_fault("login-refused", mail, self.cal), [mail, self.cal])
        self.assertEqual(sandbox.arm_fault("model-down", mail, self.cal), [])
        with self.assertRaises(ValueError):
            sandbox.arm_fault("no-such-fault", mail, self.cal)
        sandbox.disarm_all(mail, self.cal)
        self.assertEqual(self.cal.query(*SPAN), [])


class DemoDayTest(Fixture):
    """demo-day.md's calendar as .ics files, through query, the busy rule and slot choice."""

    def setUp(self):
        super().setUp()
        busy = [("wed-am", "20261202T170000Z", "20261202T190000Z"),    # Wed 9-11 AM Pacific
                ("wed-pm", "20261202T220000Z", "20261202T230000Z"),    # Wed 2-3 PM
                ("thu-am", "20261203T180000Z", "20261203T200000Z"),    # Thu 10 AM-12 PM
                ("thu-pm", "20261203T210000Z", "20261203T220000Z"),    # Thu 1-2 PM
                ("fri-am", "20261204T170000Z", "20261204T180000Z"),    # Fri 9-10 AM
                ("fri-pm", "20261204T210000Z", "20261205T010000Z")]    # Fri 1-5 PM
        for uid, start, end in busy:
            self.put(uid + ".ics", *vevent(uid, ":" + start, ":" + end))

    def offer(self, now):
        return [s.start_utc for s in slots.find_slots(self.cal.query(now, now + dt.timedelta(days=31)), now, 30)]

    def test_email_2_offer_reads_the_calendar_files(self):
        self.cal.create_only("doris-priya", hers("doris-priya", WED_11))      # booked at 8:02 AM
        # Tue 8:10 AM Pacific: Wed 11:30 AM, Thu 9:00 AM, Fri 10:00 AM Pacific.
        self.assertEqual(self.offer(u(2026, 12, 1, 16, 10)),
                         [u(2026, 12, 2, 19, 30), u(2026, 12, 3, 17, 0), u(2026, 12, 4, 18, 0)])

    def test_email_8a_collision_is_caught_on_the_read_back_and_removed(self):
        self.cal.create_only("doris-marcus", hers("doris-marcus", Slot(u(2026, 12, 3, 17, 0), u(2026, 12, 3, 17, 30))))
        self.cal.arm("lands-between", start="2026-12-07T18:00:00Z", end="2026-12-07T19:00:00Z")
        for _ in range(2):                                            # re-check, then G11's fresh read
            self.assertFalse(slots.busy(self.cal.query(MON_10.start_utc, MON_10.end_utc), MON_10))
        self.cal.create_only("doris-sam", hers("doris-sam", MON_10))
        reread = self.cal.query(MON_10.start_utc, MON_10.end_utc)
        self.assertEqual(sorted(e.uid.startswith("doris-") for e in reread), [False, True])
        self.assertTrue(slots.busy([e for e in reread if e.uid != "doris-sam"], MON_10))
        self.cal.delete("doris-sam")
        self.assertIsNone(self.cal.get("doris-sam"))
        self.cal.disarm()
        # Tue 10:30 AM Pacific, Priya's Wednesday booking cancelled: Wed 11:00, Thu 9:30, Fri 10:00 AM.
        self.assertEqual(self.offer(u(2026, 12, 1, 18, 30)),
                         [u(2026, 12, 2, 19, 0), u(2026, 12, 3, 17, 30), u(2026, 12, 4, 18, 0)])


OWNER = "whizz@fastmail.com"
DORIS = "doris.htm@fastmail.com"


def eml(frm, subject, date=None, message_id=None, to=OWNER):
    lines = ["From: " + frm, "To: " + to, "Subject: " + subject]
    if date:
        lines.append("Date: " + date)
    if message_id:
        lines.append("Message-ID: " + message_id)
    return ("\r\n".join(lines) + "\r\n\r\nHello.\r\n").encode("ascii")


def her_mail(message_id, to="priya@northwind.example"):
    return eml("Doris <%s>" % DORIS, "Re: Quick call", "Tue, 01 Dec 2026 08:03:00 -0800", message_id, to=to)


class MailFixture(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="doris-sandbox-test-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.inbox = os.path.join(self.root, "inbox")
        self.outbox = os.path.join(self.root, "outbox")
        os.makedirs(self.inbox)
        with open(os.path.join(self.root, "clock.txt"), "w") as fh:
            fh.write("2026-12-01T16:00:00Z\n")
        self.mail = sandbox.SandboxMail(self.root, DORIS)

    def drop(self, name, raw):
        with open(os.path.join(self.inbox, name), "wb") as fh:
            fh.write(raw)

    def facts(self, obj):
        with open(os.path.join(self.inbox, "facts.json"), "w") as fh:
            fh.write(obj if isinstance(obj, str) else json.dumps(obj))


class MailFetchTest(MailFixture):

    def test_is_a_mail_adapter(self):
        self.assertIsInstance(self.mail, MailAdapter)

    def test_every_eml_oldest_first_by_file_name_with_its_facts(self):
        second = eml("Marcus Lee <marcus@brightlane.example>", "Intro", "Tue, 01 Dec 2026 08:10:00 -0800")
        first = eml("Priya Raman <priya@northwind.example>", "Quick call", "Tue, 01 Dec 2026 08:02:00 -0800")
        self.drop("02-marcus.eml", second)
        self.drop("01-priya.eml", first)
        self.facts({"02-marcus.eml": {"fault": "timeout-once", "forwarded": "yes"}})
        items = self.mail.fetch_new()
        self.assertEqual([i.server_id for i in items], ["01-priya.eml", "02-marcus.eml"])
        self.assertEqual(items[0].raw, first)
        self.assertEqual(items[0].received_utc, u(2026, 12, 1, 16, 2))
        self.assertEqual(items[1].received_utc, u(2026, 12, 1, 16, 10))
        self.assertEqual(items[0].facts, {})
        self.assertEqual(items[1].facts, {"fault": "timeout-once", "forwarded": "yes"})

    def test_no_or_bad_date_header_takes_the_fixture_clock(self):
        self.drop("a.eml", eml("a@x.example", "No date"))
        self.drop("b.eml", eml("b@x.example", "Bad date", "the day after tomorrow"))
        self.assertEqual([i.received_utc for i in self.mail.fetch_new()], [u(2026, 12, 1, 16, 0)] * 2)

    def test_with_a_clock_only_mail_received_by_then(self):
        self.drop("01.eml", eml("a@x.example", "early", "Tue, 01 Dec 2026 08:02:00 -0800"))
        self.drop("02.eml", eml("b@x.example", "later", "Tue, 01 Dec 2026 09:40:00 -0800"))
        now = [u(2026, 12, 1, 16, 30)]
        mail = sandbox.SandboxMail(self.root, DORIS, clock=lambda: now[0])
        self.assertEqual([i.server_id for i in mail.fetch_new()], ["01.eml"])
        now[0] = u(2026, 12, 1, 17, 40)
        self.assertEqual([i.server_id for i in mail.fetch_new()], ["01.eml", "02.eml"])

    def test_next_arrival_is_the_earliest_mail_after_the_clock(self):
        self.drop("01.eml", eml("a@x.example", "early", "Tue, 01 Dec 2026 08:02:00 -0800"))
        self.drop("03.eml", eml("c@x.example", "last", "Tue, 01 Dec 2026 10:05:00 -0800"))
        self.drop("02.eml", eml("b@x.example", "later", "Tue, 01 Dec 2026 09:40:00 -0800"))
        now = [u(2026, 12, 1, 16, 10)]
        mail = sandbox.SandboxMail(self.root, DORIS, clock=lambda: now[0])
        self.assertEqual(mail.next_arrival(), u(2026, 12, 1, 17, 40))
        now[0] = u(2026, 12, 1, 17, 40)    # at or before the clock counts as arrived
        self.assertEqual(mail.next_arrival(), u(2026, 12, 1, 18, 5))
        now[0] = u(2026, 12, 1, 18, 10)
        self.assertIsNone(mail.next_arrival())
        self.assertIsNone(self.mail.next_arrival())     # no clock: nothing is pending

    def test_empty_inbox_is_empty_but_a_missing_one_raises(self):
        self.assertEqual(self.mail.fetch_new(), [])
        shutil.rmtree(self.inbox)
        with self.assertRaises(MailError):
            self.mail.fetch_new()

    def test_malformed_facts_raise(self):
        self.drop("a.eml", eml("a@x.example", "x"))
        self.facts("{not json")
        with self.assertRaises(MailError):
            self.mail.fetch_new()
        self.facts({"a.eml": {"forwarded": True}})
        with self.assertRaises(MailError):
            self.mail.fetch_new()

    def test_login_refused_is_loud_and_never_an_empty_inbox(self):
        self.drop("a.eml", eml("a@x.example", "x"))
        self.mail.arm("login-refused")
        with self.assertRaises(MailError) as ctx:
            self.mail.fetch_new()
        self.assertTrue(ctx.exception.staged)
        self.mail.disarm()
        self.assertEqual(len(self.mail.fetch_new()), 1)


class MailSendTest(MailFixture):

    def test_send_writes_numbered_files_and_finds_them_in_sent(self):
        a, b = her_mail("<aaa@fastmail.com>"), her_mail("<bbb@fastmail.com>")
        self.mail.send(a, ["priya@northwind.example", OWNER], "<aaa@fastmail.com>", template="T1")
        self.mail.send(b, ["priya@northwind.example", OWNER], "<bbb@fastmail.com>", template="S1")
        self.assertEqual(sorted(n for n in os.listdir(self.outbox) if n.endswith(".eml")), ["001-T1.eml", "002-S1.eml"])
        with open(os.path.join(self.outbox, "001-T1.eml"), "rb") as fh:
            self.assertEqual(fh.read(), a)
        self.assertTrue(self.mail.find_in_sent("<aaa@fastmail.com>"))
        self.assertTrue(self.mail.find_in_sent(" aaa@fastmail.com "))
        self.assertFalse(self.mail.find_in_sent("<zzz@fastmail.com>"))

    def test_envelope_is_recorded_beside_the_sent_copy(self):
        self.mail.send(her_mail("<aaa@fastmail.com>"), ["priya@northwind.example", OWNER], "<aaa@fastmail.com>",
                       template="T1")
        with open(os.path.join(self.outbox, "envelope.jsonl"), "rb") as fh:
            rows = [json.loads(line) for line in fh.read().splitlines()]
        self.assertEqual(rows, [{"file": "001-T1.eml", "message_id": "<aaa@fastmail.com>",
                                 "envelope_to": ["priya@northwind.example", OWNER]}])

    def test_send_refuses_a_message_id_or_sender_that_does_not_match(self):
        with self.assertRaises(MailError):
            self.mail.send(her_mail("<aaa@fastmail.com>"), ["priya@northwind.example"], "<other@fastmail.com>")
        forged = eml("Shane <%s>" % OWNER, "x", None, "<ccc@fastmail.com>", to="priya@northwind.example")
        with self.assertRaises(MailError):
            self.mail.send(forged, ["priya@northwind.example"], "<ccc@fastmail.com>")
        with self.assertRaises(MailError):
            self.mail.send(her_mail("<ddd@fastmail.com>"), [], "<ddd@fastmail.com>")
        self.assertFalse(os.path.isdir(self.outbox) and any(n.endswith(".eml") for n in os.listdir(self.outbox)))

    def test_sent_unconfirmed_sends_but_the_copy_never_appears(self):
        self.mail.arm("sent-unconfirmed")
        self.mail.send(her_mail("<aaa@fastmail.com>"), ["priya@northwind.example"], "<aaa@fastmail.com>")
        self.assertEqual(self.mail.last_call_fault, "sent-unconfirmed")
        self.assertFalse(self.mail.find_in_sent("<aaa@fastmail.com>"))
        self.assertEqual(self.mail.last_call_fault, "sent-unconfirmed")
        self.assertFalse(self.mail.find_in_sent("<aaa@fastmail.com>"))
        self.assertTrue(any(n.endswith(".eml") for n in os.listdir(self.outbox)))   # it did go out
        self.mail.send(her_mail("<bbb@fastmail.com>", to=OWNER), [OWNER], "<bbb@fastmail.com>")
        self.assertTrue(self.mail.find_in_sent("<bbb@fastmail.com>"))

    def test_login_refused_sends_nothing(self):
        self.mail.arm("login-refused")
        with self.assertRaises(MailError) as ctx:
            self.mail.send(her_mail("<aaa@fastmail.com>"), ["priya@northwind.example"], "<aaa@fastmail.com>")
        self.assertNotIsInstance(ctx.exception, SendUnknown)
        self.assertFalse(os.path.isdir(self.outbox) and any(n.endswith(".eml") for n in os.listdir(self.outbox)))


class MailFileTest(MailFixture):

    def test_file_records_a_keyword_and_never_moves_the_mail(self):
        raw = eml("a@x.example", "news")
        self.drop("05-news.eml", raw)
        self.mail.file("05-news.eml", "doris-filed")
        self.mail.file("05-news.eml", "doris-filed")
        self.mail.file("05-news.eml", "doris-held")
        with open(os.path.join(self.outbox, "keywords.json")) as fh:
            self.assertEqual(json.load(fh), {"05-news.eml": ["doris-filed", "doris-held"]})
        with open(os.path.join(self.inbox, "05-news.eml"), "rb") as fh:
            self.assertEqual(fh.read(), raw)

    def test_file_refuses_unknown_mail_and_bad_keywords(self):
        with self.assertRaises(MailError):
            self.mail.file("missing.eml", "doris-filed")
        self.drop("a.eml", eml("a@x.example", "x"))
        with self.assertRaises(ValueError):
            self.mail.file("a.eml", "two words")
        with self.assertRaises(MailError):
            self.mail.file("../clock.txt", "doris-filed")


if __name__ == "__main__":
    unittest.main()
