"""slots.py: where slots can sit (G9), what counts as busy (ruling 3), slot choice (ruling 6).

Every expected instant below is a hand-written UTC literal; the Pacific time is in the comment.
Pacific is UTC-8 in December (PST) and UTC-7 before Nov 1 2026 and from Mar 14 2027 (PDT).
"""
import datetime as dt
import unittest

from doris import slots
from doris.shapes import Event, Slot

UTC = dt.timezone.utc


def u(*args):
    return dt.datetime(*args, tzinfo=UTC)


def S(start, minutes=30):
    return Slot(start, start + dt.timedelta(minutes=minutes))


def timed(start, end, status="CONFIRMED", transp="OPAQUE", uid="ev"):
    return Event(uid=uid, status=status, transp=transp, kind="timed", start_utc=start, end_utc=end)


def all_day(d1, d2, status="CONFIRMED", uid="allday"):
    return Event(uid=uid, status=status, transp="TRANSPARENT", kind="all_day",
                 start_utc=None, end_utc=None, start_date=d1, end_date=d2)


def unknown(start, end, d1=None, d2=None, uid="unk"):
    return Event(uid=uid, status="CONFIRMED", transp="OPAQUE", kind="unknown_zone",
                 start_utc=start, end_utc=end, start_date=d1, end_date=d2)


# Tue Dec 1 2026, 9:00 AM Pacific: the default fixture clock (A.3).
TUE_9AM = u(2026, 12, 1, 17, 0)


class BookableTest(unittest.TestCase):

    def test_weekday_business_hour_slot_is_bookable(self):
        # Wed Dec 2, 10:00 AM Pacific.
        self.assertEqual(slots.bookable(S(u(2026, 12, 2, 18, 0)), TUE_9AM, False), (True, None))

    def test_weekend_is_outside_hours(self):
        # Sat Dec 5, 10:00 AM Pacific.
        self.assertEqual(slots.bookable(S(u(2026, 12, 5, 18, 0)), TUE_9AM, False), (False, "outside_hours"))

    def test_before_nine_is_outside_hours(self):
        # Wed Dec 2, 8:30 AM Pacific.
        self.assertEqual(slots.bookable(S(u(2026, 12, 2, 16, 30)), TUE_9AM, False), (False, "outside_hours"))

    def test_slot_ending_at_five_is_bookable(self):
        # Wed Dec 2, 4:30 to 5:00 PM Pacific.
        self.assertEqual(slots.bookable(S(u(2026, 12, 3, 0, 30)), TUE_9AM, False), (True, None))

    def test_slot_running_past_five_is_outside_hours(self):
        # Wed Dec 2, 4:45 to 5:15 PM Pacific, a written time.
        self.assertEqual(slots.bookable(S(u(2026, 12, 3, 0, 45)), TUE_9AM, True), (False, "outside_hours"))

    def test_slot_starting_at_five_is_outside_hours(self):
        self.assertEqual(slots.bookable(S(u(2026, 12, 3, 1, 0)), TUE_9AM, False), (False, "outside_hours"))

    def test_under_24_hours_from_the_check(self):
        now = u(2026, 12, 1, 18, 5)   # Tue 10:05 AM Pacific
        # Wed 10:00 AM Pacific is 23 h 55 min away; Wed 10:30 AM is 24 h 25 min away.
        self.assertEqual(slots.bookable(S(u(2026, 12, 2, 18, 0)), now, False), (False, "under_24h"))
        self.assertEqual(slots.bookable(S(u(2026, 12, 2, 18, 30)), now, False), (True, None))

    def test_exactly_24_hours_is_bookable(self):
        now = u(2026, 12, 1, 18, 0)   # Tue 10:00 AM Pacific
        self.assertEqual(slots.bookable(S(u(2026, 12, 2, 18, 0)), now, False), (True, None))

    def test_thirty_days_is_the_last_start(self):
        now = u(2026, 12, 7, 17, 0)   # Mon Dec 7, 9:00 AM Pacific
        # Wed Jan 6 2027, 9:00 AM Pacific is exactly 30 days later; 9:30 is past it.
        self.assertEqual(slots.bookable(S(u(2027, 1, 6, 17, 0)), now, False), (True, None))
        self.assertEqual(slots.bookable(S(u(2027, 1, 6, 17, 30)), now, False), (False, "over_30d"))

    def test_holiday_list_dates_are_not_bookable(self):
        # Thu Dec 24 (ours), Fri Nov 27 (ours), Mon Jan 18 2027 (MLK), all 10:00 AM Pacific.
        self.assertEqual(slots.bookable(S(u(2026, 12, 24, 18, 0)), TUE_9AM, False), (False, "holiday"))
        self.assertEqual(slots.bookable(S(u(2026, 11, 27, 18, 0)), u(2026, 11, 25, 17, 0), False),
                         (False, "holiday"))
        self.assertEqual(slots.bookable(S(u(2027, 1, 18, 18, 0)), u(2027, 1, 4, 17, 0), False),
                         (False, "holiday"))

    def test_offered_slot_must_start_on_hour_or_half_hour(self):
        self.assertEqual(slots.bookable(S(u(2026, 12, 2, 18, 15)), TUE_9AM, False), (False, "not_quarter_hour"))
        self.assertEqual(slots.bookable(S(u(2026, 12, 2, 18, 30)), TUE_9AM, False), (True, None))

    def test_written_time_may_start_on_any_quarter_hour(self):
        self.assertEqual(slots.bookable(S(u(2026, 12, 2, 18, 15)), TUE_9AM, True), (True, None))
        self.assertEqual(slots.bookable(S(u(2026, 12, 2, 18, 45)), TUE_9AM, True), (True, None))
        self.assertEqual(slots.bookable(S(u(2026, 12, 2, 18, 10)), TUE_9AM, True), (False, "not_quarter_hour"))

    def test_seconds_off_the_grid_are_not_bookable(self):
        start = u(2026, 12, 2, 18, 0, 30)
        self.assertEqual(slots.bookable(S(start), TUE_9AM, True), (False, "not_quarter_hour"))

    def test_length_must_be_5_to_60_minutes(self):
        start = u(2026, 12, 2, 18, 0)
        self.assertEqual(slots.bookable(S(start, 5), TUE_9AM, True), (True, None))
        self.assertEqual(slots.bookable(S(start, 60), TUE_9AM, True), (True, None))
        self.assertEqual(slots.bookable(S(start, 4), TUE_9AM, True), (False, "length"))
        self.assertEqual(slots.bookable(S(start, 61), TUE_9AM, True), (False, "length"))
        self.assertEqual(slots.bookable(Slot(start, start), TUE_9AM, True), (False, "length"))
        self.assertEqual(slots.bookable(Slot(start, start - dt.timedelta(minutes=30)), TUE_9AM, True),
                         (False, "length"))

    def test_every_rule_word_is_on_the_why_list(self):
        from doris import why
        for word in ("outside_hours", "under_24h", "over_30d", "holiday", "not_quarter_hour", "length"):
            self.assertIn(word, why.RULE_WORDS)

    def test_naive_datetimes_are_refused(self):
        naive = dt.datetime(2026, 12, 2, 18, 0)
        with self.assertRaises(ValueError):
            slots.bookable(Slot(naive, naive + dt.timedelta(minutes=30)), TUE_9AM, False)
        with self.assertRaises(ValueError):
            slots.bookable(S(u(2026, 12, 2, 18, 0)), dt.datetime(2026, 12, 1, 17, 0), False)


class BookableAcrossDaylightChangeTest(unittest.TestCase):

    def test_monday_after_nov_1_2026_uses_standard_time(self):
        now = u(2026, 10, 29, 16, 0)   # Thu Oct 29, 9:00 AM PDT
        # Fri Oct 30, 9:00 AM PDT is 16:00 UTC.
        self.assertEqual(slots.bookable(S(u(2026, 10, 30, 16, 0)), now, False), (True, None))
        # Mon Nov 2: 9:00 AM PST is 17:00 UTC; 16:00 UTC is 8:00 AM PST.
        self.assertEqual(slots.bookable(S(u(2026, 11, 2, 17, 0)), now, False), (True, None))
        self.assertEqual(slots.bookable(S(u(2026, 11, 2, 16, 0)), now, False), (False, "outside_hours"))

    def test_sunday_nov_1_2026_is_a_weekend(self):
        now = u(2026, 10, 29, 16, 0)
        self.assertEqual(slots.bookable(S(u(2026, 11, 1, 18, 0)), now, False), (False, "outside_hours"))

    def test_monday_after_mar_14_2027_uses_daylight_time(self):
        now = u(2027, 3, 11, 17, 0)    # Thu Mar 11, 9:00 AM PST
        # Fri Mar 12, 4:30 PM PST is 00:30 UTC Mar 13.
        self.assertEqual(slots.bookable(S(u(2027, 3, 13, 0, 30)), now, False), (True, None))
        # Mon Mar 15: 9:00 AM PDT is 16:00 UTC; 4:30 PM PDT is 23:30 UTC; 00:30 UTC Mar 16 is 5:30 PM PDT.
        self.assertEqual(slots.bookable(S(u(2027, 3, 15, 16, 0)), now, False), (True, None))
        self.assertEqual(slots.bookable(S(u(2027, 3, 15, 23, 30)), now, False), (True, None))
        self.assertEqual(slots.bookable(S(u(2027, 3, 16, 0, 0)), now, False), (False, "outside_hours"))

    def test_sunday_mar_14_2027_is_a_weekend(self):
        now = u(2027, 3, 11, 17, 0)
        self.assertEqual(slots.bookable(S(u(2027, 3, 14, 18, 0)), now, False), (False, "outside_hours"))


# Wed Dec 2, 10:00 to 10:30 AM Pacific.
WED_10 = S(u(2026, 12, 2, 18, 0))


class BusyTest(unittest.TestCase):

    def test_overlapping_confirmed_event_is_busy(self):
        ev = timed(u(2026, 12, 2, 18, 0), u(2026, 12, 2, 19, 0))
        self.assertTrue(slots.busy([ev], WED_10))
        self.assertEqual(slots.overlapping([ev], WED_10), [ev])

    def test_back_to_back_events_are_not_busy(self):
        before = timed(u(2026, 12, 2, 17, 0), u(2026, 12, 2, 18, 0))
        after = timed(u(2026, 12, 2, 18, 30), u(2026, 12, 2, 19, 0))
        self.assertFalse(slots.busy([before, after], WED_10))
        self.assertEqual(slots.overlapping([before, after], WED_10), [])

    def test_one_minute_overlap_is_busy(self):
        self.assertTrue(slots.busy([timed(u(2026, 12, 2, 17, 0), u(2026, 12, 2, 18, 1))], WED_10))
        self.assertTrue(slots.busy([timed(u(2026, 12, 2, 18, 29), u(2026, 12, 2, 19, 0))], WED_10))

    def test_cancelled_event_never_counts(self):
        ev = timed(u(2026, 12, 2, 18, 0), u(2026, 12, 2, 19, 0), status="CANCELLED")
        self.assertFalse(slots.busy([ev], WED_10))
        self.assertFalse(slots.busy([timed(u(2026, 12, 2, 18, 0), u(2026, 12, 2, 19, 0), status="cancelled")],
                                    WED_10))

    def test_free_and_tentative_events_count(self):
        free = timed(u(2026, 12, 2, 18, 0), u(2026, 12, 2, 19, 0), transp="TRANSPARENT")
        tentative = timed(u(2026, 12, 2, 18, 0), u(2026, 12, 2, 19, 0), status="TENTATIVE")
        self.assertTrue(slots.busy([free], WED_10))
        self.assertTrue(slots.busy([tentative], WED_10))

    def test_all_day_event_is_busy_midnight_to_midnight_pacific(self):
        self.assertTrue(slots.busy([all_day(dt.date(2026, 12, 2), dt.date(2026, 12, 3))], WED_10))
        self.assertFalse(slots.busy([all_day(dt.date(2026, 12, 1), dt.date(2026, 12, 2))], WED_10))
        # Wed Dec 2, 11:30 PM Pacific is 07:30 UTC Dec 3: still inside the Dec 2 all-day event.
        late = S(u(2026, 12, 3, 7, 30))
        self.assertTrue(slots.busy([all_day(dt.date(2026, 12, 2), dt.date(2026, 12, 3))], late))
        self.assertFalse(slots.busy([all_day(dt.date(2026, 12, 2), dt.date(2026, 12, 3))], S(u(2026, 12, 3, 8, 0))))

    def test_all_day_event_on_nov_1_2026_ends_at_pacific_standard_midnight(self):
        nov1 = all_day(dt.date(2026, 11, 1), dt.date(2026, 11, 2))
        # Nov 1 starts at 00:00 PDT (07:00 UTC) and ends at Nov 2 00:00 PST (08:00 UTC).
        self.assertTrue(slots.busy([nov1], S(u(2026, 11, 2, 7, 30))))
        self.assertFalse(slots.busy([nov1], S(u(2026, 11, 2, 8, 0))))
        self.assertFalse(slots.busy([nov1], S(u(2026, 11, 1, 6, 30))))
        self.assertTrue(slots.busy([nov1], S(u(2026, 11, 1, 7, 0))))

    def test_all_day_event_on_mar_14_2027_ends_at_pacific_daylight_midnight(self):
        mar14 = all_day(dt.date(2027, 3, 14), dt.date(2027, 3, 15))
        # Mar 14 starts at 00:00 PST (08:00 UTC) and ends at Mar 15 00:00 PDT (07:00 UTC).
        self.assertTrue(slots.busy([mar14], S(u(2027, 3, 15, 6, 30))))
        self.assertFalse(slots.busy([mar14], S(u(2027, 3, 15, 7, 0))))
        self.assertFalse(slots.busy([mar14], S(u(2027, 3, 14, 7, 30))))

    def test_unknown_zone_event_is_busy_for_its_whole_pacific_day(self):
        # Read as Pacific, it sits Wed Dec 2 6:00 to 6:30 AM; the whole of Dec 2 is busy.
        ev = unknown(u(2026, 12, 2, 14, 0), u(2026, 12, 2, 14, 30), dt.date(2026, 12, 2), dt.date(2026, 12, 3))
        self.assertTrue(slots.busy([ev], WED_10))
        self.assertTrue(slots.busy([ev], S(u(2026, 12, 3, 0, 30))))    # Wed 4:30 PM Pacific
        self.assertFalse(slots.busy([ev], S(u(2026, 12, 3, 17, 0))))   # Thu 9:00 AM Pacific

    def test_unknown_zone_event_without_dates_uses_the_pacific_days_of_its_times(self):
        ev = unknown(u(2026, 12, 2, 14, 0), u(2026, 12, 2, 14, 30))
        self.assertTrue(slots.busy([ev], S(u(2026, 12, 3, 0, 30))))
        self.assertFalse(slots.busy([ev], S(u(2026, 12, 3, 17, 0))))

    def test_an_event_that_cannot_be_placed_is_busy(self):
        self.assertTrue(slots.busy([timed(None, None)], WED_10))
        odd = Event(uid="x", status="", transp="", kind="weird", start_utc=None, end_utc=None)
        self.assertTrue(slots.busy([odd], WED_10))

    def test_overlapping_keeps_only_the_events_that_overlap(self):
        a = timed(u(2026, 12, 2, 18, 0), u(2026, 12, 2, 19, 0), uid="a")
        b = timed(u(2026, 12, 2, 20, 0), u(2026, 12, 2, 21, 0), uid="b")
        c = timed(u(2026, 12, 2, 17, 45), u(2026, 12, 2, 18, 15), uid="c", status="CANCELLED")
        self.assertEqual(slots.overlapping([a, b, c], WED_10), [a])


class AvailableTest(unittest.TestCase):

    def test_busy_slot_reports_overlap(self):
        ev = timed(u(2026, 12, 2, 18, 0), u(2026, 12, 2, 19, 0))
        self.assertEqual(slots.available([ev], WED_10, TUE_9AM, written=True), (False, "overlap"))

    def test_unbookable_slot_reports_its_rule(self):
        self.assertEqual(slots.available([], S(u(2026, 12, 5, 18, 0)), TUE_9AM, written=True),
                         (False, "outside_hours"))

    def test_free_bookable_slot_is_available(self):
        self.assertEqual(slots.available([], WED_10, TUE_9AM, written=False), (True, None))


def demo_calendar():
    """Shane's calendar on demo day (demo-day.md), Pacific = UTC-8."""
    return [
        timed(u(2026, 12, 2, 17, 0), u(2026, 12, 2, 19, 0), uid="wed-am"),    # Wed 9-11 AM
        timed(u(2026, 12, 2, 22, 0), u(2026, 12, 2, 23, 0), uid="wed-pm"),    # Wed 2-3 PM
        timed(u(2026, 12, 3, 18, 0), u(2026, 12, 3, 20, 0), uid="thu-am"),    # Thu 10 AM-12 PM
        timed(u(2026, 12, 3, 21, 0), u(2026, 12, 3, 22, 0), uid="thu-pm"),    # Thu 1-2 PM
        timed(u(2026, 12, 4, 17, 0), u(2026, 12, 4, 18, 0), uid="fri-am"),    # Fri 9-10 AM
        timed(u(2026, 12, 4, 21, 0), u(2026, 12, 5, 1, 0), uid="fri-pm"),     # Fri 1-5 PM
    ]


PRIYA = timed(u(2026, 12, 2, 19, 0), u(2026, 12, 2, 19, 30), uid="doris-priya")    # Wed 11:00 AM
MARCUS = timed(u(2026, 12, 3, 17, 0), u(2026, 12, 3, 17, 30), uid="doris-marcus")  # Thu 9:00 AM
SAM_LANDED = timed(u(2026, 12, 7, 18, 0), u(2026, 12, 7, 19, 0), uid="staged")     # Mon Dec 7 10-11 AM


def starts(found):
    return [s.start_utc for s in found]


class FindSlotsDemoDayTest(unittest.TestCase):

    def test_email_2_offer_for_marcus(self):
        now = u(2026, 12, 1, 16, 10)   # Tue 8:10 AM Pacific; Priya booked Wed 11:00 at 8:02
        found = slots.find_slots(demo_calendar() + [PRIYA], now, 30)
        # Wed 11:30 AM, Thu 9:00 AM, Fri 10:00 AM Pacific.
        self.assertEqual(starts(found), [u(2026, 12, 2, 19, 30), u(2026, 12, 3, 17, 0), u(2026, 12, 4, 18, 0)])
        self.assertTrue(all(s.end_utc - s.start_utc == dt.timedelta(minutes=30) for s in found))

    def test_email_3_busy_written_time_is_not_offered_first(self):
        now = u(2026, 12, 1, 16, 25)   # Tue 8:25 AM Pacific; Dana asks Fri 2:00 PM Pacific (busy)
        found = slots.find_slots(demo_calendar() + [PRIYA], now, 30, first=[u(2026, 12, 4, 22, 0)])
        self.assertEqual(starts(found), [u(2026, 12, 2, 19, 30), u(2026, 12, 3, 17, 0), u(2026, 12, 4, 18, 0)])

    def test_email_8_move_puts_the_named_time_first(self):
        now = u(2026, 12, 1, 18, 5)    # Tue 10:05 AM Pacific; Priya's booking still on the calendar
        found = slots.find_slots(demo_calendar() + [PRIYA, MARCUS], now, 30,
                                 first=[u(2026, 12, 3, 22, 0)])   # Thu 2:00 PM Pacific
        # Thu 2:00 PM first, then Wed 11:30 AM and Fri 10:00 AM (one per day: Thu is taken).
        self.assertEqual(starts(found), [u(2026, 12, 3, 22, 0), u(2026, 12, 2, 19, 30), u(2026, 12, 4, 18, 0)])

    def test_email_8a_offer_after_the_collision(self):
        now = u(2026, 12, 1, 18, 30)   # Tue 10:30 AM Pacific; Priya cancelled, Marcus booked
        found = slots.find_slots(demo_calendar() + [MARCUS, SAM_LANDED], now, 30)
        # Wed 11:00 AM, Thu 9:30 AM, Fri 10:00 AM Pacific.
        self.assertEqual(starts(found), [u(2026, 12, 2, 19, 0), u(2026, 12, 3, 17, 30), u(2026, 12, 4, 18, 0)])

    def test_second_offer_never_repeats_a_start_offered_on_the_thread(self):
        now = u(2026, 12, 1, 16, 10)
        offered = {u(2026, 12, 2, 19, 30), u(2026, 12, 3, 17, 0), u(2026, 12, 4, 18, 0)}
        found = slots.find_slots(demo_calendar() + [PRIYA], now, 30, exclude_starts=offered)
        # Wed 12:00 PM, Thu 9:30 AM, Fri 10:30 AM Pacific.
        self.assertEqual(starts(found), [u(2026, 12, 2, 20, 0), u(2026, 12, 3, 17, 30), u(2026, 12, 4, 18, 30)])

    def test_offered_starts_may_come_back_from_the_ledger_as_iso_text(self):
        now = u(2026, 12, 1, 16, 10)
        offered = {"2026-12-02T19:30:00Z", "2026-12-03T17:00:00Z", "2026-12-04T18:00:00Z"}
        found = slots.find_slots(demo_calendar() + [PRIYA], now, 30, exclude_starts=offered)
        self.assertEqual(starts(found), [u(2026, 12, 2, 20, 0), u(2026, 12, 3, 17, 30), u(2026, 12, 4, 18, 30)])

    def test_sender_candidate_already_offered_is_not_offered_again(self):
        now = u(2026, 12, 1, 16, 10)
        found = slots.find_slots(demo_calendar() + [PRIYA], now, 30, exclude_starts={u(2026, 12, 3, 17, 0)},
                                 first=[u(2026, 12, 3, 17, 0)])
        self.assertNotIn(u(2026, 12, 3, 17, 0), starts(found))

    def test_sixty_minutes_needs_a_free_hour(self):
        now = u(2026, 12, 1, 18, 30)
        found = slots.find_slots(demo_calendar() + [MARCUS], now, 60)
        # Wed 11:00 AM, Thu 12:00 PM (9:30 would run into 10:00), Fri 10:00 AM Pacific.
        self.assertEqual(starts(found), [u(2026, 12, 2, 19, 0), u(2026, 12, 3, 20, 0), u(2026, 12, 4, 18, 0)])
        self.assertTrue(all(s.end_utc - s.start_utc == dt.timedelta(minutes=60) for s in found))


class FindSlotsRulesTest(unittest.TestCase):

    def test_empty_calendar_gives_the_earliest_slot_of_each_day(self):
        found = slots.find_slots([], TUE_9AM, 30)
        self.assertEqual(starts(found), [u(2026, 12, 2, 17, 0), u(2026, 12, 3, 17, 0), u(2026, 12, 4, 17, 0)])

    def test_count_limits_the_number_of_slots(self):
        self.assertEqual(len(slots.find_slots([], TUE_9AM, 30, count=2)), 2)
        self.assertEqual(slots.find_slots([], TUE_9AM, 30, count=0), [])

    def test_same_day_slots_only_when_no_other_day_is_free(self):
        # Free only Wed Dec 2, 9:00 to 10:30 AM Pacific; everything after is busy past the window.
        block = timed(u(2026, 12, 2, 18, 30), u(2027, 1, 5, 0, 0))
        found = slots.find_slots([block], TUE_9AM, 30)
        self.assertEqual(starts(found), [u(2026, 12, 2, 17, 0), u(2026, 12, 2, 17, 30), u(2026, 12, 2, 18, 0)])

    def test_one_free_slot_is_still_offered(self):
        block = timed(u(2026, 12, 2, 17, 30), u(2027, 1, 5, 0, 0))
        self.assertEqual(starts(slots.find_slots([block], TUE_9AM, 30)), [u(2026, 12, 2, 17, 0)])

    def test_a_derived_time_follows_the_senders_own_on_her_grid_and_never_leads(self):
        # A D5 cross-pair (Fable review 2 of PR #3, finding A) is no time the sender wrote: it is offered right
        # after their own times, only on the hour or half hour, and only once one of their own is offered.
        wed_3 = u(2026, 12, 9, 23, 0)              # Wed Dec 9, 3:00 PM Pacific: the sender wrote it
        mon_3 = u(2026, 12, 7, 23, 0)              # Mon Dec 7, 3:00 PM Pacific: D5 moved it to "Monday"
        mon_315 = u(2026, 12, 7, 23, 15)           # the same move of a 3:15 PM: off her grid
        found = slots.find_slots([], TUE_9AM, 30, first=[wed_3], derived=[mon_315, mon_3])
        self.assertEqual(starts(found), [wed_3, mon_3, u(2026, 12, 2, 17, 0)])
        busy = timed(wed_3, wed_3 + dt.timedelta(hours=1))
        found = slots.find_slots([busy], TUE_9AM, 30, first=[wed_3], derived=[mon_3])
        self.assertEqual(starts(found), [u(2026, 12, 2, 17, 0), u(2026, 12, 3, 17, 0), u(2026, 12, 4, 17, 0)])
        self.assertEqual(starts(slots.find_slots([], TUE_9AM, 30, derived=[mon_3])),
                         [u(2026, 12, 2, 17, 0), u(2026, 12, 3, 17, 0), u(2026, 12, 4, 17, 0)])

    def test_no_free_slot_in_30_days_gives_none(self):
        block = timed(u(2026, 12, 1, 0, 0), u(2027, 1, 5, 0, 0))
        self.assertEqual(slots.find_slots([block], TUE_9AM, 30), [])

    def test_last_start_of_the_day_fits_before_five(self):
        # Only Wed 4:30 to 5:00 PM Pacific is free.
        events = [timed(u(2026, 12, 1, 0, 0), u(2026, 12, 3, 0, 30)),
                  timed(u(2026, 12, 3, 1, 0), u(2027, 1, 5, 0, 0))]
        found = slots.find_slots(events, TUE_9AM, 30)
        self.assertEqual(starts(found), [u(2026, 12, 3, 0, 30)])

    def test_cancelled_events_leave_the_slot_free(self):
        cancelled = timed(u(2026, 12, 2, 17, 0), u(2026, 12, 2, 18, 0), status="CANCELLED")
        self.assertEqual(starts(slots.find_slots([cancelled], TUE_9AM, 30))[0], u(2026, 12, 2, 17, 0))

    def test_tentative_free_and_all_day_events_block(self):
        events = [timed(u(2026, 12, 2, 17, 0), u(2026, 12, 2, 18, 0), status="TENTATIVE"),
                  timed(u(2026, 12, 2, 18, 0), u(2026, 12, 2, 18, 30), transp="TRANSPARENT"),
                  all_day(dt.date(2026, 12, 3), dt.date(2026, 12, 4))]
        self.assertEqual(starts(slots.find_slots(events, TUE_9AM, 30)),
                         [u(2026, 12, 2, 18, 30), u(2026, 12, 4, 17, 0), u(2026, 12, 7, 17, 0)])

    def test_unknown_zone_event_blocks_its_whole_day(self):
        ev = unknown(u(2026, 12, 2, 14, 0), u(2026, 12, 2, 14, 30), dt.date(2026, 12, 2), dt.date(2026, 12, 3))
        self.assertEqual(starts(slots.find_slots([ev], TUE_9AM, 30)),
                         [u(2026, 12, 3, 17, 0), u(2026, 12, 4, 17, 0), u(2026, 12, 7, 17, 0)])

    def test_holidays_are_skipped(self):
        now = u(2026, 12, 21, 17, 0)   # Mon Dec 21, 9:00 AM Pacific
        # Tue Dec 22, Wed Dec 23, then Mon Dec 28 (Dec 24 and 25 are holidays).
        self.assertEqual(starts(slots.find_slots([], now, 30)),
                         [u(2026, 12, 22, 17, 0), u(2026, 12, 23, 17, 0), u(2026, 12, 28, 17, 0)])

    def test_friday_late_clock_offers_from_monday(self):
        now = u(2026, 12, 5, 0, 30)    # Fri Dec 4, 4:30 PM Pacific
        self.assertEqual(starts(slots.find_slots([], now, 30)),
                         [u(2026, 12, 7, 17, 0), u(2026, 12, 8, 17, 0), u(2026, 12, 9, 17, 0)])

    def test_first_candidate_on_a_quarter_hour_is_offered_first(self):
        found = slots.find_slots([], TUE_9AM, 30, first=[u(2026, 12, 3, 18, 15)])   # Thu 10:15 AM
        self.assertEqual(starts(found), [u(2026, 12, 3, 18, 15), u(2026, 12, 2, 17, 0), u(2026, 12, 4, 17, 0)])

    def test_first_candidate_off_the_quarter_hour_or_unbookable_is_dropped(self):
        found = slots.find_slots([], TUE_9AM, 30, first=[u(2026, 12, 3, 18, 10), u(2026, 12, 5, 18, 0)])
        self.assertEqual(starts(found), [u(2026, 12, 2, 17, 0), u(2026, 12, 3, 17, 0), u(2026, 12, 4, 17, 0)])

    def test_two_sender_candidates_on_one_day_are_both_offered(self):
        first = [u(2026, 12, 3, 18, 0), u(2026, 12, 3, 22, 0)]      # Thu 10 AM and 2 PM
        found = slots.find_slots([], TUE_9AM, 30, first=first)
        self.assertEqual(starts(found), [u(2026, 12, 3, 18, 0), u(2026, 12, 3, 22, 0), u(2026, 12, 2, 17, 0)])

    def test_first_accepts_slots_as_well_as_instants(self):
        found = slots.find_slots([], TUE_9AM, 30, first=[S(u(2026, 12, 3, 18, 0))])
        self.assertEqual(found[0], S(u(2026, 12, 3, 18, 0)))

    def test_invalid_length_is_refused(self):
        for bad in (4, 61, 0):
            with self.assertRaises(ValueError):
                slots.find_slots([], TUE_9AM, bad)


class FindSlotsSteerTest(unittest.TestCase):

    def test_named_day_with_vague_window_offers_that_window_first(self):
        # "Could we talk mid-morning on December 3?" -> Dec 3 slots between 10:00 and 11:30 first.
        found = slots.find_slots([], TUE_9AM, 30, steer_day=dt.date(2026, 12, 3),
                                 steer_window=(dt.time(10, 0), dt.time(11, 30)))
        self.assertEqual(starts(found), [u(2026, 12, 3, 18, 0), u(2026, 12, 3, 18, 30), u(2026, 12, 3, 19, 0)])

    def test_named_day_offers_slots_on_that_day_first(self):
        busy_thu_morning = timed(u(2026, 12, 3, 17, 0), u(2026, 12, 3, 18, 0))   # Thu 9-10 AM
        found = slots.find_slots([busy_thu_morning], TUE_9AM, 30, count=2, steer_day=dt.date(2026, 12, 3))
        self.assertEqual(starts(found), [u(2026, 12, 3, 18, 0), u(2026, 12, 3, 18, 30)])

    def test_named_day_with_no_room_falls_back_to_the_earliest(self):
        busy_thu = all_day(dt.date(2026, 12, 3), dt.date(2026, 12, 4))
        found = slots.find_slots([busy_thu], TUE_9AM, 30, steer_day=dt.date(2026, 12, 3))
        self.assertEqual(starts(found), [u(2026, 12, 2, 17, 0), u(2026, 12, 4, 17, 0), u(2026, 12, 7, 17, 0)])

    def test_vague_window_alone_steers_each_day(self):
        # "afternoon" in Pacific: 12:00 to 5:00 PM.
        found = slots.find_slots([], TUE_9AM, 30, steer_window=(dt.time(12, 0), dt.time(17, 0)))
        self.assertEqual(starts(found), [u(2026, 12, 2, 20, 0), u(2026, 12, 3, 20, 0), u(2026, 12, 4, 20, 0)])

    def test_vague_window_in_the_senders_zone(self):
        # "late morning" Eastern, 10:30 to 12:00 ET, is 7:30 to 9:00 AM Pacific: only 9:00 is
        # inside Shane's hours and it ends outside the window, so the earliest slots are offered.
        found = slots.find_slots([], TUE_9AM, 30, steer_window=(dt.time(10, 30), dt.time(12, 0)),
                                 steer_zone="Eastern")
        self.assertEqual(starts(found), [u(2026, 12, 2, 17, 0), u(2026, 12, 3, 17, 0), u(2026, 12, 4, 17, 0)])
        # "after lunch" Eastern, 1:00 to 3:00 PM ET, is 10:00 AM to 12:00 PM Pacific.
        found = slots.find_slots([], TUE_9AM, 30, steer_window=(dt.time(13, 0), dt.time(15, 0)),
                                 steer_zone="Eastern")
        self.assertEqual(starts(found), [u(2026, 12, 2, 18, 0), u(2026, 12, 3, 18, 0), u(2026, 12, 4, 18, 0)])


class FindSlotsAcrossDaylightChangeTest(unittest.TestCase):

    def test_slots_after_nov_1_2026_start_at_nine_standard_time(self):
        now = u(2026, 10, 29, 16, 0)   # Thu Oct 29, 9:00 AM PDT
        busy_fri = all_day(dt.date(2026, 10, 30), dt.date(2026, 10, 31))
        found = slots.find_slots([busy_fri], now, 30)
        # Mon Nov 2, Tue Nov 3, Wed Nov 4 at 9:00 AM PST = 17:00 UTC.
        self.assertEqual(starts(found), [u(2026, 11, 2, 17, 0), u(2026, 11, 3, 17, 0), u(2026, 11, 4, 17, 0)])

    def test_slots_after_mar_14_2027_start_at_nine_daylight_time(self):
        now = u(2027, 3, 11, 17, 0)    # Thu Mar 11, 9:00 AM PST
        busy_fri = all_day(dt.date(2027, 3, 12), dt.date(2027, 3, 13))
        found = slots.find_slots([busy_fri], now, 30)
        # Mon Mar 15, Tue Mar 16, Wed Mar 17 at 9:00 AM PDT = 16:00 UTC.
        self.assertEqual(starts(found), [u(2027, 3, 15, 16, 0), u(2027, 3, 16, 16, 0), u(2027, 3, 17, 16, 0)])


if __name__ == "__main__":
    unittest.main()
