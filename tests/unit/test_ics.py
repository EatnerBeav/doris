"""ics.py: her .ics carries only ruling 10's fixed fields (G13); events are read as times and status only."""
import datetime as dt
import unittest

from doris import ics
from doris.shapes import Event, Slot

UTC = dt.timezone.utc


def u(*args):
    return dt.datetime(*args, tzinfo=UTC)


LINK = "https://meet.jit.si/htm-example-room"
SLOT = Slot(u(2026, 12, 2, 19, 0), u(2026, 12, 2, 19, 30))     # Wed Dec 2, 11:00 AM Pacific
STAMP = u(2026, 12, 1, 16, 2, 41, 123456)


def build(**over):
    args = dict(booking_id="doris-0123abcd", slot=SLOT, meeting_link=LINK, status="CONFIRMED",
                sequence=0, dtstamp=STAMP, method="PUBLISH")
    args.update(over)
    return ics.build_event(**args)


EXPECTED = (
    b"BEGIN:VCALENDAR\r\n"
    b"VERSION:2.0\r\n"
    b"PRODID:-//Hire the Machines//Doris//EN\r\n"
    b"METHOD:PUBLISH\r\n"
    b"BEGIN:VEVENT\r\n"
    b"UID:doris-0123abcd\r\n"
    b"DTSTAMP:20261201T160241Z\r\n"
    b"DTSTART:20261202T190000Z\r\n"
    b"DTEND:20261202T193000Z\r\n"
    b"SUMMARY:Call with Shane Bornstein (Hire the Machines)\r\n"
    b"LOCATION:https://meet.jit.si/htm-example-room\r\n"
    b"STATUS:CONFIRMED\r\n"
    b"SEQUENCE:0\r\n"
    b"END:VEVENT\r\n"
    b"END:VCALENDAR\r\n"
)


def unfold(raw):
    return raw.decode("ascii").replace("\r\n ", "").split("\r\n")


class BuildEventTest(unittest.TestCase):

    def test_fixed_fields_only_in_fixed_order(self):
        self.assertEqual(build(), EXPECTED)

    def test_calendar_write_form_has_no_method_line(self):
        raw = build(method=None)
        self.assertEqual(raw, EXPECTED.replace(b"METHOD:PUBLISH\r\n", b""))

    def test_cancel_form(self):
        raw = build(status="CANCELLED", sequence=1, method="CANCEL")
        self.assertIn(b"\r\nMETHOD:CANCEL\r\n", raw)
        self.assertIn(b"\r\nSTATUS:CANCELLED\r\n", raw)
        self.assertIn(b"\r\nSEQUENCE:1\r\n", raw)

    def test_every_line_ends_crlf_and_none_is_bare(self):
        raw = build()
        self.assertTrue(raw.endswith(b"\r\n"))
        self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"))
        self.assertEqual(raw.count(b"\r"), raw.count(b"\r\n"))

    def test_long_lines_fold_at_75_octets(self):
        link = "https://meet.jit.si/" + "a" * 100
        raw = build(meeting_link=link)
        physical = raw.split(b"\r\n")[:-1]
        self.assertTrue(all(len(line) <= 75 for line in physical))
        logical = "LOCATION:" + link
        folded = [p for p in physical if p.startswith(b"LOCATION:")][0]
        self.assertEqual(folded, logical[:75].encode("ascii"))
        idx = physical.index(folded)
        self.assertEqual(physical[idx + 1], b" " + logical[75:].encode("ascii"))
        self.assertIn(logical, unfold(raw))

    def test_text_specials_in_the_link_are_escaped(self):
        raw = build(meeting_link="https://meet.example/room;x,y\\z")
        self.assertIn("LOCATION:https://meet.example/room\\;x\\,y\\\\z", unfold(raw))

    def test_refuses_anything_outside_the_fixed_form(self):
        bad = [
            dict(booking_id="owner-123"),
            dict(booking_id="doris-../../etc"),
            dict(booking_id="doris-a/b"),
            dict(status="TENTATIVE"),
            dict(method="REQUEST"),
            dict(method="CANCEL", status="CONFIRMED"),
            dict(sequence=-1),
            dict(sequence=True),
            dict(meeting_link="https://meet.example/a\r\nATTENDEE:mailto:x@y.example"),
            dict(meeting_link=""),
            dict(meeting_link="https://meet.example/café"),
            dict(slot=Slot(dt.datetime(2026, 12, 2, 19, 0), dt.datetime(2026, 12, 2, 19, 30))),
            dict(slot=Slot(u(2026, 12, 2, 19, 30), u(2026, 12, 2, 19, 0))),
            dict(slot=Slot(u(2026, 12, 2, 19, 0, 30), u(2026, 12, 2, 19, 30))),
            dict(dtstamp=dt.datetime(2026, 12, 1, 16, 2)),
        ]
        for over in bad:
            with self.assertRaises(ValueError, msg=repr(over)):
                build(**over)


def add_before(raw, marker, line):
    assert marker in raw
    return raw.replace(marker, line + marker, 1)


class FieldsOkTest(unittest.TestCase):

    def test_her_own_event_passes_in_every_form(self):
        self.assertTrue(ics.fields_ok(build()))
        self.assertTrue(ics.fields_ok(build(method=None)))
        self.assertTrue(ics.fields_ok(build(status="CANCELLED", sequence=2, method="CANCEL")))
        self.assertTrue(ics.fields_ok(build(status="CANCELLED", method="PUBLISH")))
        self.assertTrue(ics.fields_ok(build(meeting_link="https://meet.jit.si/" + "a" * 200)))
        self.assertTrue(ics.fields_ok(build(meeting_link="https://meet.example/room;x,y\\z")))

    def test_refuses_any_extra_property(self):
        for line in (b"ORGANIZER:mailto:priya@northwind.example\r\n",
                     b"ATTENDEE;RSVP=TRUE:mailto:priya@northwind.example\r\n",
                     b"DESCRIPTION:Click here to confirm\r\n",
                     b"ATTACH:https://evil.example/x.pdf\r\n",
                     b"URL:https://evil.example/\r\n",
                     b"X-ANYTHING:1\r\n",
                     b"RRULE:FREQ=DAILY\r\n",
                     b"TRANSP:OPAQUE\r\n",
                     b"CATEGORIES:x\r\n"):
            self.assertFalse(ics.fields_ok(add_before(build(), b"END:VEVENT\r\n", line)), line)

    def test_refuses_an_alarm_or_any_other_component(self):
        alarm = b"BEGIN:VALARM\r\nACTION:DISPLAY\r\nTRIGGER:-PT15M\r\nEND:VALARM\r\n"
        self.assertFalse(ics.fields_ok(add_before(build(), b"END:VEVENT\r\n", alarm)))
        tz = b"BEGIN:VTIMEZONE\r\nTZID:X\r\nEND:VTIMEZONE\r\n"
        self.assertFalse(ics.fields_ok(add_before(build(), b"BEGIN:VEVENT\r\n", tz)))
        second = b"BEGIN:VEVENT\r\nUID:doris-2\r\nEND:VEVENT\r\n"
        self.assertFalse(ics.fields_ok(add_before(build(), b"END:VCALENDAR\r\n", second)))

    def test_refuses_an_extra_calendar_property(self):
        self.assertFalse(ics.fields_ok(add_before(build(), b"BEGIN:VEVENT\r\n", b"CALSCALE:GREGORIAN\r\n")))
        self.assertFalse(ics.fields_ok(add_before(build(), b"BEGIN:VEVENT\r\n", b"X-WR-CALNAME:x\r\n")))

    def test_refuses_duplicate_or_missing_fields(self):
        raw = build()
        self.assertFalse(ics.fields_ok(add_before(raw, b"END:VEVENT\r\n",
                                                  b"SUMMARY:Call with Shane Bornstein (Hire the Machines)\r\n")))
        self.assertFalse(ics.fields_ok(raw.replace(b"DTEND:20261202T193000Z\r\n", b"")))
        self.assertFalse(ics.fields_ok(raw.replace(b"SEQUENCE:0\r\n", b"")))
        self.assertFalse(ics.fields_ok(raw.replace(b"VERSION:2.0\r\n", b"")))

    def test_refuses_parameters_and_zones(self):
        raw = build().replace(b"DTSTART:20261202T190000Z", b"DTSTART;TZID=America/Los_Angeles:20261202T110000")
        self.assertFalse(ics.fields_ok(raw))
        raw = build().replace(b"LOCATION:", b"LOCATION;ALTREP=\"https://evil.example\":")
        self.assertFalse(ics.fields_ok(raw))

    def test_refuses_other_values(self):
        raw = build()
        for old, new in ((b"SUMMARY:Call with Shane Bornstein (Hire the Machines)", b"SUMMARY:Free money"),
                         (b"STATUS:CONFIRMED", b"STATUS:TENTATIVE"),
                         (b"METHOD:PUBLISH", b"METHOD:REQUEST"),
                         (b"UID:doris-0123abcd", b"UID:owner-standup"),
                         (b"DTEND:20261202T193000Z", b"DTEND:20261202T183000Z"),
                         (b"DTSTART:20261202T190000Z", b"DTSTART:20261302T190000Z"),
                         (b"DTSTART:20261202T190000Z", b"DTSTART:20261202"),
                         (b"SEQUENCE:0", b"SEQUENCE:-1"),
                         (b"VERSION:2.0", b"VERSION:1.0"),
                         (b"PRODID:-//Hire the Machines//Doris//EN", b"PRODID:-//Other//EN"),
                         (b"BEGIN:VEVENT", b"begin:vevent")):
            self.assertFalse(ics.fields_ok(raw.replace(old, new)), new)
        cancel_confirmed = build(status="CANCELLED", method="CANCEL").replace(b"STATUS:CANCELLED",
                                                                                b"STATUS:CONFIRMED")
        self.assertFalse(ics.fields_ok(cancel_confirmed))

    def test_refuses_malformed_bytes(self):
        raw = build()
        self.assertFalse(ics.fields_ok(raw.replace(b"\r\n", b"\n")))
        self.assertFalse(ics.fields_ok(raw[:-2]))
        self.assertFalse(ics.fields_ok(raw + b"X"))
        self.assertFalse(ics.fields_ok(raw + b"BEGIN:VCALENDAR\r\n"))
        self.assertFalse(ics.fields_ok(b""))
        self.assertFalse(ics.fields_ok(b"\x00\xff garbage"))
        self.assertFalse(ics.fields_ok(raw.decode("ascii")))
        self.assertFalse(ics.fields_ok(None))
        self.assertFalse(ics.fields_ok(raw.replace(b"https://meet", b"https://m\xc3\xa9et")))
        unfolded_long = raw.replace(LINK.encode(), ("https://meet.jit.si/" + "a" * 100).encode())
        self.assertFalse(ics.fields_ok(unfolded_long))
        self.assertFalse(ics.fields_ok(raw.replace(b"SUMMARY:", b"SUMMARY")))

    def test_optional_exact_checks_against_what_she_meant_to_write(self):
        raw = build()
        self.assertTrue(ics.fields_ok(raw, meeting_link=LINK, booking_id="doris-0123abcd", slot=SLOT))
        self.assertFalse(ics.fields_ok(raw, meeting_link="https://meet.jit.si/other"))
        self.assertFalse(ics.fields_ok(raw, booking_id="doris-ffff"))
        self.assertFalse(ics.fields_ok(raw, slot=Slot(SLOT.start_utc, SLOT.end_utc + dt.timedelta(minutes=30))))


def cal(*lines, eol="\r\n"):
    return (eol.join(("BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Fixture//EN") + lines + ("END:VCALENDAR",))
            + eol).encode("utf-8")


class ReadEventTest(unittest.TestCase):

    def test_round_trips_her_own_event(self):
        ev = ics.read_event(build())
        self.assertEqual(ev, Event(uid="doris-0123abcd", status="CONFIRMED", transp="", kind="timed",
                                   start_utc=SLOT.start_utc, end_utc=SLOT.end_utc))
        ev = ics.read_event(build(status="CANCELLED", method="CANCEL", meeting_link="https://x.example/" + "b" * 90))
        self.assertEqual((ev.status, ev.start_utc, ev.end_utc), ("CANCELLED", SLOT.start_utc, SLOT.end_utc))

    def test_reads_times_and_status_never_a_title(self):
        raw = cal("BEGIN:VEVENT", "UID:owner-1", "SUMMARY:Board meeting about the secret merger",
                  "DESCRIPTION:Private notes", "LOCATION:Room 4",
                  "DTSTART:20261202T170000Z", "DTEND:20261202T190000Z",
                  "STATUS:tentative", "TRANSP:TRANSPARENT", "END:VEVENT")
        ev = ics.read_event(raw)
        self.assertEqual(ev, Event(uid="owner-1", status="TENTATIVE", transp="TRANSPARENT", kind="timed",
                                   start_utc=u(2026, 12, 2, 17, 0), end_utc=u(2026, 12, 2, 19, 0)))
        for word in ("Board", "secret", "Private", "Room"):
            self.assertNotIn(word, repr(ev))

    def test_all_day_event(self):
        raw = cal("BEGIN:VEVENT", "UID:a", "DTSTART;VALUE=DATE:20261202", "DTEND;VALUE=DATE:20261203", "END:VEVENT")
        ev = ics.read_event(raw)
        self.assertEqual((ev.kind, ev.start_date, ev.end_date), ("all_day", dt.date(2026, 12, 2), dt.date(2026, 12, 3)))
        # Midnight to midnight Pacific (PST): 08:00 UTC to 08:00 UTC.
        self.assertEqual((ev.start_utc, ev.end_utc), (u(2026, 12, 2, 8, 0), u(2026, 12, 3, 8, 0)))

    def test_all_day_event_without_an_end_lasts_one_day(self):
        ev = ics.read_event(cal("BEGIN:VEVENT", "UID:a", "DTSTART;VALUE=DATE:20261101", "END:VEVENT"))
        self.assertEqual((ev.start_date, ev.end_date), (dt.date(2026, 11, 1), dt.date(2026, 11, 2)))
        # Nov 1 2026 runs 00:00 PDT (07:00 UTC) to Nov 2 00:00 PST (08:00 UTC).
        self.assertEqual((ev.start_utc, ev.end_utc), (u(2026, 11, 1, 7, 0), u(2026, 11, 2, 8, 0)))

    def test_tzid_is_an_unknown_zone_read_as_pacific(self):
        raw = cal("BEGIN:VEVENT", "UID:z", "DTSTART;TZID=America/New_York:20261202T100000",
                  "DTEND;TZID=America/New_York:20261202T103000", "END:VEVENT")
        ev = ics.read_event(raw)
        self.assertEqual(ev.kind, "unknown_zone")
        self.assertEqual((ev.start_utc, ev.end_utc), (u(2026, 12, 2, 18, 0), u(2026, 12, 2, 18, 30)))
        self.assertEqual((ev.start_date, ev.end_date), (dt.date(2026, 12, 2), dt.date(2026, 12, 3)))

    def test_floating_time_is_an_unknown_zone(self):
        raw = cal("BEGIN:VEVENT", "UID:f", "DTSTART:20261202T233000", "DTEND:20261203T003000", "END:VEVENT")
        ev = ics.read_event(raw)
        self.assertEqual(ev.kind, "unknown_zone")
        self.assertEqual((ev.start_date, ev.end_date), (dt.date(2026, 12, 2), dt.date(2026, 12, 4)))

    def test_duration_instead_of_end(self):
        raw = cal("BEGIN:VEVENT", "UID:d", "DTSTART:20261202T180000Z", "DURATION:PT1H15M", "END:VEVENT")
        self.assertEqual(ics.read_event(raw).end_utc, u(2026, 12, 2, 19, 15))
        raw = cal("BEGIN:VEVENT", "UID:d", "DTSTART;VALUE=DATE:20261202", "DURATION:P2D", "END:VEVENT")
        self.assertEqual(ics.read_event(raw).end_date, dt.date(2026, 12, 4))

    def test_timed_event_without_end_or_duration_ends_at_its_start(self):
        ev = ics.read_event(cal("BEGIN:VEVENT", "UID:p", "DTSTART:20261202T180000Z", "END:VEVENT"))
        self.assertEqual((ev.start_utc, ev.end_utc), (u(2026, 12, 2, 18, 0), u(2026, 12, 2, 18, 0)))

    def test_lenient_on_lf_endings_folding_and_case(self):
        raw = cal("BEGIN:VEVENT", "uid:l", "dtstart:20261202T18", " 0000Z", "DTEND:20261202T190000Z",
                  "END:VEVENT", eol="\n")
        ev = ics.read_event(raw)
        self.assertEqual((ev.uid, ev.start_utc), ("l", u(2026, 12, 2, 18, 0)))

    def test_a_leading_byte_order_mark_is_read(self):
        raw = b"\xef\xbb\xbf" + cal("BEGIN:VEVENT", "UID:b", "DTSTART:20261202T180000Z", "DTEND:20261202T183000Z",
                                    "END:VEVENT")
        self.assertEqual(ics.read_event(raw).start_utc, u(2026, 12, 2, 18, 0))

    def test_timezone_and_alarm_components_do_not_leak_into_the_event(self):
        raw = cal("BEGIN:VTIMEZONE", "TZID:X", "BEGIN:STANDARD", "DTSTART:19701101T020000", "END:STANDARD",
                  "END:VTIMEZONE",
                  "BEGIN:VEVENT", "UID:v", "DTSTART:20261202T180000Z", "DTEND:20261202T183000Z",
                  "BEGIN:VALARM", "TRIGGER:-PT15M", "DTSTART:20991231T000000Z", "STATUS:CANCELLED", "END:VALARM",
                  "STATUS:CONFIRMED", "END:VEVENT")
        ev = ics.read_event(raw)
        self.assertEqual((ev.kind, ev.start_utc, ev.status), ("timed", u(2026, 12, 2, 18, 0), "CONFIRMED"))

    def test_unreadable_events_raise(self):
        bad = [
            b"",
            b"not a calendar",
            cal("BEGIN:VTODO", "UID:t", "END:VTODO"),
            cal("BEGIN:VEVENT", "UID:x", "DTEND:20261202T183000Z", "END:VEVENT"),
            cal("BEGIN:VEVENT", "UID:x", "DTSTART:20261302T180000Z", "END:VEVENT"),
            cal("BEGIN:VEVENT", "UID:x", "DTSTART:20261202T180000Z", "DTEND:20261202T170000Z", "END:VEVENT"),
            cal("BEGIN:VEVENT", "UID:x", "DTSTART;VALUE=DATE:20261202", "DTEND:20261202T170000Z", "END:VEVENT"),
            cal("BEGIN:VEVENT", "UID:x", "DTSTART:20261202T180000Z"),
            cal("BEGIN:VEVENT", "UID:x", "DTSTART:20261202T180000Z", "DURATION:soon", "END:VEVENT"),
        ]
        for raw in bad:
            with self.assertRaises(ValueError, msg=raw):
                ics.read_event(raw)


class ReadEventsTest(unittest.TestCase):

    def test_every_event_with_its_repeat_rule(self):
        raw = cal("BEGIN:VEVENT", "UID:r", "DTSTART:20261130T180000Z", "DTEND:20261130T183000Z",
                  "RRULE:FREQ=WEEKLY;BYDAY=MO,WE;COUNT=3", "END:VEVENT",
                  "BEGIN:VEVENT", "UID:r", "RECURRENCE-ID:20261202T180000Z", "DTSTART:20261202T200000Z",
                  "DTEND:20261202T203000Z", "END:VEVENT",
                  "BEGIN:VEVENT", "UID:q", "DTSTART:20261201T180000Z", "DTEND:20261201T183000Z",
                  "RDATE:20261215T180000Z", "END:VEVENT")
        found = ics.read_events(raw)
        self.assertEqual([v.uid for v in found], ["r", "r", "q"])
        self.assertEqual(found[0].rrule, "FREQ=WEEKLY;BYDAY=MO,WE;COUNT=3")
        self.assertEqual((found[0].kind, found[0].start, found[0].end),
                         ("timed", u(2026, 11, 30, 18, 0), u(2026, 11, 30, 18, 30)))
        self.assertTrue(found[1].recurrence_id)
        self.assertFalse(found[0].rdate)
        self.assertTrue(found[2].rdate)


if __name__ == "__main__":
    unittest.main()
