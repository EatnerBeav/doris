"""parser.py: ruling 2 (direct booking), ruling 5 (length), ruling 6 (picks), B11 (model agrees).

Fixture clock (ruling 2's worked cases): Tuesday Dec 1, 2026, 9:00 AM Pacific = 17:00 UTC.
Thursday is Dec 3. Dec 3 10:00 AM Pacific = 18:00 UTC.

The bookable check (slots.bookable, calendar builder's module) is faked here with G9's rules,
so these tests never depend on a half-built slots.py.
"""
import datetime as dt
import unittest

from doris import caps, parser, zones
from doris.shapes import ModelAnswer, ParseResult, PickResult, Slot

UTC = dt.timezone.utc


def u(*args):
    return dt.datetime(*args, tzinfo=UTC)


CLOCK = u(2026, 12, 1, 17, 0)          # Tue Dec 1, 9:00 AM Pacific
DEC3_10 = u(2026, 12, 3, 18, 0)        # Thu Dec 3, 10:00 AM Pacific


def fake_bookable(slot, now, written):
    """G9 as the spec states it: Mon-Fri 9-5 Pacific, 24 h to 30 days, holidays, grid."""
    s = zones.to_local(slot.start_utc, "Pacific")
    e = zones.to_local(slot.end_utc, "Pacific")
    if s.weekday() not in caps.BUSINESS_DAYS:
        return False, "outside_hours"
    if s.date() in caps.HOLIDAYS:
        return False, "holiday"
    if s.time() < caps.BUSINESS_START or e.date() != s.date() or e.time() > caps.BUSINESS_END:
        return False, "outside_hours"
    if slot.start_utc - now < caps.MIN_NOTICE:
        return False, "under_24h"
    if slot.start_utc - now > caps.MAX_HORIZON:
        return False, "over_30d"
    grid = caps.WRITTEN_START_MINUTES if written else caps.OFFERED_START_MINUTES
    if s.minute not in grid:
        return False, "not_quarter_hour"
    return True, None


def P(text, now=CLOCK, sent=None):
    return parser.parse_request(text, sent or now, now, bookable=fake_bookable)


class Base(unittest.TestCase):
    def book(self, text, instant, now=CLOCK):
        r = P(text, now)
        self.assertIsInstance(r, ParseResult)
        self.assertEqual(r.verdict, "book", "%r -> %s %s" % (text, r.failed_condition, r.readings))
        self.assertIsNone(r.failed_condition)
        self.assertEqual(r.instant_utc, instant, text)
        return r

    def offer(self, text, cond=None, first=None, now=CLOCK, never=None):
        r = P(text, now)
        self.assertEqual(r.verdict, "offer", "%r booked %s" % (text, r.instant_utc))
        self.assertIsNone(r.instant_utc)
        self.assertIn(r.failed_condition, ("B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8", "B9"), text)
        if cond is not None:
            self.assertEqual(r.failed_condition, cond, "%r: %s" % (text, r.conditions))
        starts = parser.preferred_starts(r, now, bookable=fake_bookable)
        if first is not None:
            self.assertTrue(starts, "%r: no preferred start (%s)" % (text, r.readings))
            self.assertEqual(starts[0], first, text)
        if never is not None:
            self.assertNotIn(never, [x.start_utc for x in r.readings if x.kept], text)
        return r


# ---------------------------------------------------------------------------------------
# Worked cases (pack ours), each with the ruling's verdict
# ---------------------------------------------------------------------------------------
class WorkedCasesTest(Base):
    def test_r2_clear_books(self):
        r = self.book("Could Shane do Thursday, December 3 at 10:00 AM Pacific for 30 minutes?", DEC3_10)
        self.assertEqual((r.length_min, r.length_source), (30, "written"))
        self.assertEqual(r.zone, "Pacific")

    def test_r2_at_10_books_am(self):
        self.book("Can we do December 3 at 10 Pacific?", DEC3_10)

    def test_r2_at_3_books_pm(self):
        self.book("December 3 at 3 PT works for me.", u(2026, 12, 3, 23))

    def test_r2_at_10_your_time_books(self):
        self.book("December 3 at 10 your time.", DEC3_10)

    def test_r2_at_8_offers(self):
        r = self.offer("December 3 at 8 Pacific?", "B7")
        self.assertEqual(parser.preferred_starts(r, CLOCK, bookable=fake_bookable), [])
        self.assertEqual(r.steer_day, dt.date(2026, 12, 3))

    def test_r2_at_7_eastern_offers(self):
        self.offer("December 3 at 7 Eastern.", "B7", first=u(2026, 12, 4, 0))

    def test_r2_at_8_gmt_offers(self):
        self.offer("December 3 at 8 GMT.", "B7", first=u(2026, 12, 3, 20))

    def test_r2_at_5_gmt_offers(self):
        self.offer("December 3 at 5 GMT.", "B7", first=u(2026, 12, 3, 17))

    def test_r2_utc_no_ampm_offers(self):
        self.offer("December 3 at 6:00 UTC.", "B7", first=u(2026, 12, 3, 18))

    def test_r2_utc_24h_books(self):
        self.book("December 3 at 18:00 UTC.", DEC3_10)

    def test_r2_leading_zero_is_24h(self):
        r = self.offer("December 3 at 04:00 Eastern.", "B8", never=u(2026, 12, 3, 21))
        self.assertEqual([x.start_utc for x in r.readings if x.kept], [u(2026, 12, 3, 9)])

    def test_r2_12am_offers_noon_first(self):
        self.offer("December 3 at 12am PT.", first=u(2026, 12, 3, 20))

    def test_r2_quarter_past_books(self):
        self.book("Thursday December 3 at quarter past ten Pacific.", u(2026, 12, 3, 18, 15))

    def test_r2_12_3_books(self):
        self.book("12/3 at 10am PT.", DEC3_10)

    def test_r2_12_3_your_time_offers(self):
        self.offer("12/3 at 10am your time.", "B7", first=DEC3_10)

    def test_r2_3_12_offers(self):
        self.offer("3/12 at 10am PT.", "B7", first=DEC3_10)

    def test_r2_dot_date_offers(self):
        self.offer("3.12 at 10am PT.", first=DEC3_10)

    def test_r2_dotted_pair_offers(self):
        self.offer("15.12 at 10am PT.", first=u(2026, 12, 15, 18))

    def test_r2_dotted_lunch_offers(self):
        self.offer("Lunch on December 3 at 12.30 PT?", first=u(2026, 12, 3, 20, 30))

    def test_r2_numeric_utc_offers(self):
        self.offer("12/3 at 17:00 UTC.", "B7", first=u(2026, 12, 3, 17))

    def test_r2_day_over_12_books(self):
        self.book("15/12 at 10am PT.", u(2026, 12, 15, 18))

    def test_r2_weekday_selects_books(self):
        self.book("Thursday 3/12 at 10am PT.", DEC3_10)

    def test_r2_numeric_year_books(self):
        self.book("12/3/2026 at 10am PT.", DEC3_10)

    def test_r2_numeric_year_day_first_offers(self):
        self.offer("3/12/2026 at 10am PT.", "B7", first=DEC3_10)

    def test_r2_iso_books(self):
        self.book("2026-12-03 at 10:00 AM Pacific.", DEC3_10)

    def test_r2_iso_timestamp_books(self):
        self.book("2026-12-03T18:00Z works.", DEC3_10)

    def test_r2_year_is_not_a_time(self):
        self.book("December 3 2026 at 10am PT.", DEC3_10)

    def test_r2_four_digits_after_date(self):
        self.book("Dec 3 1400 PT.", u(2026, 12, 3, 22))

    def test_r2_street_number_not_time(self):
        self.book("We're at 1200 Pacific Avenue. Could Shane do December 3 at 10am PT?", DEC3_10)

    def test_r2_idiom_books(self):
        self.book("Could Shane do a 1:1 on December 3 at 10am PT?", DEC3_10)

    def test_r2_two_zones_same_instant_books(self):
        r = self.book("December 3 at 10am PT / 1pm ET.", DEC3_10)
        self.assertEqual(r.zone_pair, ("Pacific", "Eastern"))
        self.assertEqual(r.zone, "Eastern")   # the confirmation shows Eastern first

    def test_r2_two_zones_conflict_offers(self):
        r = self.offer("December 3 at 10am PT (2pm ET).")
        kept = [x.start_utc for x in r.readings if x.kept]
        self.assertIn(DEC3_10, kept)
        self.assertIn(u(2026, 12, 3, 19), kept)

    def test_r2_both_readings_offers_both(self):
        now = u(2027, 2, 1, 17)  # Mon Feb 1, 2027, 9 AM Pacific
        r = self.offer("2/3 at 10am PT.", "B7", now=now)
        self.assertEqual(parser.preferred_starts(r, now, bookable=fake_bookable)[:2],
                         [u(2027, 2, 3, 18), u(2027, 3, 2, 18)])

    def test_r2_vague_offers(self):
        r = self.offer("Could we talk mid-morning on December 3?")
        self.assertEqual(r.steer_day, dt.date(2026, 12, 3))
        self.assertEqual(r.steer_window, (dt.time(10, 0), dt.time(11, 30)))

    def test_r2_weekday_alone_offers(self):
        self.offer("Thursday at 10am PT?", "B5", first=DEC3_10)

    def test_r2_relative_offers(self):
        self.offer("Tomorrow at 10am PT.", "B5", first=u(2026, 12, 2, 18))

    def test_r2_relative_elsewhere_offers(self):
        self.offer("Tomorrow is packed. Could Shane do December 3 at 10am PT?", "B2", first=DEC3_10)

    def test_r2_weekday_mismatch_offers(self):
        r = self.offer("Wednesday, December 3 at 10am PT.", "B9")
        self.assertEqual(parser.preferred_starts(r, CLOCK, bookable=fake_bookable)[:2],
                         [DEC3_10, u(2026, 12, 2, 18)])

    def test_r2_thurs_other_date_offers(self):
        r = self.offer("Thurs works. December 4 at 10am PT?", "B2")
        self.assertEqual(parser.preferred_starts(r, CLOCK, bookable=fake_bookable)[:2],
                         [u(2026, 12, 4, 18), DEC3_10])

    def test_r2_extra_weekday_offers(self):
        self.offer("Happy Friday! Could Shane do Thursday, December 3 at 10am PT?", "B2", first=DEC3_10)

    def test_r2_no_zone_offers(self):
        self.offer("December 3 at 10am.", "B4", first=DEC3_10)

    def test_r2_zone_elsewhere_offers(self):
        r = self.offer("I'm on Eastern time. Does December 3 at 1pm work?", "B4", first=DEC3_10)
        self.assertEqual(r.zone, "Eastern")

    def test_r2_unknown_zone_offers(self):
        r = self.offer("December 3 at 16:00 CET.", "B4")
        self.assertEqual(r.steer_day, dt.date(2026, 12, 3))
        self.assertEqual(parser.preferred_starts(r, CLOCK, bookable=fake_bookable), [])

    def test_r2_not_central_european(self):
        self.offer("December 3 at 4pm Central European Time.", "B4", never=u(2026, 12, 3, 22))

    def test_r2_pdt_in_december_offers(self):
        self.offer("December 3 at 10am PDT.", "B4", first=DEC3_10)

    def test_r2_range_offers(self):
        self.offer("December 3, 10-11am PT.", "B6", first=DEC3_10)

    def test_r2_negated_offers(self):
        r = self.offer("I can't do December 3 at 10am PT.", "B6")
        self.assertIn(DEC3_10, r.excluded_utc)
        self.assertNotIn(DEC3_10, parser.preferred_starts(r, CLOCK, bookable=fake_bookable))

    def test_r2_wrapped_negation_offers(self):
        r = self.offer("I'm afraid I can not make\nDecember 3 at 10am PT.", "B6")
        self.assertIn(DEC3_10, r.excluded_utc)

    def test_r2_hard_stop_offers(self):
        r = self.offer("I have a hard stop December 3 at 2pm PT, so let's find a time.", "B6")
        self.assertEqual(parser.preferred_starts(r, CLOCK, bookable=fake_bookable), [])

    def test_r2_footer_event_offers(self):
        r = self.offer("Could we find a time to talk?\n\nJoin our webinar December 3 at 10am PT.", "B6")
        self.assertEqual(parser.preferred_starts(r, CLOCK, bookable=fake_bookable), [])

    def test_r2_below_closing_line_offers(self):
        # mail.py cuts own_text at the closing line; the parser sees only what is above it.
        self.offer("Looking forward to talking.", "B2")
        # The P.S. text, parsed on its own for steering, gives that time first.
        r = P("P.S. December 3 at 10am PT would suit me.")
        self.assertEqual(r.readings[0].start_utc, DEC3_10)

    def test_r2_split_sentences_offers(self):
        self.offer("How about December 3? 10am PT is best.", "B3", first=DEC3_10)

    def test_r2_two_times_offers(self):
        r = self.offer("December 3 at 10am PT. If not, December 4 at 2pm PT.", "B2")
        self.assertEqual(parser.preferred_starts(r, CLOCK, bookable=fake_bookable)[:2],
                         [DEC3_10, u(2026, 12, 4, 22)])

    def test_r2_beyond_window_offers(self):
        r = self.offer("March 12 at 10am PT.", "B8")
        self.assertEqual([x.start_utc for x in r.readings if x.kept], [u(2027, 3, 12, 18)])

    def test_r2_subject_only_offers(self):
        self.offer("See subject.", "B2")

    def test_r2_hidden_character_offers(self):
        self.offer("December 3 at 10​am PT")

    def test_r2_yes_man_table_rows(self):
        for row, text in NEVER_ROWS.items():
            now = u(2027, 6, 1, 16) if row == "Z16" else (u(2027, 2, 1, 17) if row == "D19" else CLOCK)
            with self.subTest(row=row):
                self.assertEqual(P(text, now).verdict, "offer")

    def test_r2_yes_man_never_books_every_offer_row(self):
        # Under the yes-man model B11 always passes, so the parser alone must say offer.
        for text in OFFER_TEXTS:
            with self.subTest(text=text):
                self.assertEqual(P(text).verdict, "offer")


NEVER_ROWS = {   # every "Never" row of the ruling 2 tables, one example each
    "H5": "December 3 at 12am PT.", "H7": "December 3 at midnight PT.", "H11": "December 3 at 7:30 PT.",
    "H12": "December 3 at 8 PT.", "H15": "December 3 at 7 o'clock PT.", "H17": "December 3 at eight thirty PT.",
    "H18": "December 3 at ten past ten PT.", "H21": "December 3, late morning?", "H22": "December 3 at 25:00 PT.",
    "H23": "December 3, 14h30 PT.", "D5": "Wednesday, December 3 at 10am PT.", "D9": "Decmber 3 at 10am PT.",
    "D15": "3/12 at 10am PT.", "D16": "3.12 at 10am PT.", "D19": "2/3 at 10am PT.", "D20": "12-3 at 10am PT.",
    "D21": "February 30 at 10am PT.", "D22": "March 12 at 10am PT.", "L1": "Tomorrow at 10am PT.",
    "L2": "Thursday at 10am PT.", "L3": "Next Thursday at 10am PT.", "L4": "The 3rd at 10am PT.",
    "L5": "In 3 days at 10am PT.", "L6": "Next week at 10am PT.", "L7": "Soon, at 10am PT.",
    "Z6": "December 3 at 10:00 -08:00.", "Z8": "December 3 at 10am PDT.",
    "Z10": "I'm on Pacific time. December 3 at 10am?", "Z11": "December 3 at 10am.",
    "Z12": "December 3 at 10am JST.", "Z14": "I'm in New York (ET). 10am PT on Dec 3?",
    "Z16": "June 3 at 5pm GMT.", "Q1": "December 3, 10-11am PT.", "Q2": "December 3 at 10am or 2pm PT.",
    "Q3": "Maybe December 3 at 10am PT.", "Q4": "After 10am PT on December 3.",
    "Q5": "I can't do December 3 at 10am PT.", "Q6": "Dec 3 at 10am PT. If not, Dec 4 at 2pm PT.",
    "Q8": "December 3 at 10am PT (2pm ET).", "Q9": "December 3 would be great, 10am PT.",
    "Q10": "The webinar is December 3 at 10am PT.", "N2": "December 3 at 10\u200bam PT",
    "N3": "\u202eDecember 3 at 10am PT",
}


OFFER_TEXTS = [
    "December 3 at 8 Pacific?", "December 3 at 7 Eastern.", "December 3 at 8 GMT.", "December 3 at 5 GMT.",
    "December 3 at 6:00 UTC.", "December 3 at 04:00 Eastern.", "December 3 at 12am PT.",
    "12/3 at 10am your time.", "3/12 at 10am PT.", "3.12 at 10am PT.", "15.12 at 10am PT.",
    "Lunch on December 3 at 12.30 PT?", "12/3 at 17:00 UTC.", "3/12/2026 at 10am PT.",
    "December 3 at 10am PT (2pm ET).", "Could we talk mid-morning on December 3?", "Thursday at 10am PT?",
    "Tomorrow at 10am PT.", "Tomorrow is packed. Could Shane do December 3 at 10am PT?",
    "Wednesday, December 3 at 10am PT.", "Thurs works. December 4 at 10am PT?",
    "Happy Friday! Could Shane do Thursday, December 3 at 10am PT?", "December 3 at 10am.",
    "I'm on Eastern time. Does December 3 at 1pm work?", "December 3 at 16:00 CET.",
    "December 3 at 4pm Central European Time.", "December 3 at 10am PDT.", "December 3, 10-11am PT.",
    "I can't do December 3 at 10am PT.", "I'm afraid I can not make\nDecember 3 at 10am PT.",
    "I have a hard stop December 3 at 2pm PT, so let's find a time.",
    "Could we find a time to talk?\n\nJoin our webinar December 3 at 10am PT.",
    "How about December 3? 10am PT is best.", "December 3 at 10am PT. If not, December 4 at 2pm PT.",
    "March 12 at 10am PT.", "See subject.", "December 3 at 10​am PT",
]


# ---------------------------------------------------------------------------------------
# Conditions B1-B9
# ---------------------------------------------------------------------------------------
class ConditionsTest(Base):
    def test_book_own_body_only(self):
        # B1: direction controls anywhere in the body, or an unfoldable character glued to a token.
        self.offer("December 3 at 10am PT ‮", "B1")
        self.offer("Shaneа December 3 at 10am PT", "B1")

    def test_book_one_date_one_time(self):
        self.offer("December 3 at 10am PT or 11am PT.", "B2")
        self.offer("Could Shane talk at 10am PT?", "B2")
        self.offer("Could Shane talk on December 3?", "B2")

    def test_length_is_not_a_time(self):
        self.book("December 3 at 10am PT for 45 minutes.", DEC3_10)

    def test_book_side_by_side(self):
        self.book("10am PT on December 3 works.", DEC3_10)
        self.book("December 3, at 10am PT.", DEC3_10)
        self.offer("December 3 would be great, 10am PT.", "B3")

    def test_book_zone_after_time(self):
        self.offer("PT: December 3 at 10am.", "B4")
        self.offer("December 3 at 10am PT. All times Eastern Time.", "B4")

    def test_book_absolute_date(self):
        self.offer("On the 3rd at 10am PT?", "B5")

    def test_book_no_qualifier(self):
        self.offer("Maybe December 3 at 10am PT?", "B6")

    def test_book_one_reading_left(self):
        self.offer("December 3 at 7 PT.", "B7")

    def test_book_reading_bookable(self):
        self.offer("December 5 at 10am PT.", "B8")   # a Saturday
        self.offer("December 1 at 10am PT.", "B8")   # under 24 hours

    def test_book_weekday_matches(self):
        self.offer("Friday, December 3 at 10am PT.", "B9")

    def test_conditions_recorded(self):
        r = P("December 3 at 10am PT.")
        self.assertEqual(sorted(r.conditions), ["B%d" % i for i in range(1, 10)])
        self.assertTrue(all(r.conditions.values()))
        r = P("December 3 at 10am.")
        self.assertFalse(r.conditions["B4"])

    def test_visible_body_hash(self):
        r = P("December 3 at 10am PT.")
        self.assertEqual(len(r.visible_body_hash), 64)
        self.assertNotEqual(r.visible_body_hash, P("December 3 at 11am PT.").visible_body_hash)

    def test_never_raises_on_odd_text(self):
        odd = ["", None, 12, "\x00\x01\x02", "9" * 5000, "at at at @ @ 12/ /3", "Dec Dec Dec 3 3 3",
               "(((((", "\ud800", "PT PT PT 10 10 10", "2026-99-99T99:99Z", "at 99:99 UTC+99:00",
               "‮⁦", "between and", "quarter past", "o'clock", "-ish or so"]
        for text in odd:
            with self.subTest(text=repr(text)[:40]):
                r = parser.parse_request(text, CLOCK, CLOCK, bookable=fake_bookable)
                self.assertEqual(r.verdict, "offer")
                self.assertIn(r.failed_condition, ("B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8", "B9"))

    def test_bookable_check_that_raises_fails_closed(self):
        def broken(slot, now, written):
            raise RuntimeError("slots down")
        r = parser.parse_request("December 3 at 10am PT.", CLOCK, CLOCK, bookable=broken)
        self.assertEqual((r.verdict, r.failed_condition), ("offer", "B8"))

    def test_default_bookable_is_slots_bookable_imported_late(self):
        import sys
        import types
        from unittest import mock
        fake = types.ModuleType("doris.slots")
        fake.bookable = fake_bookable
        with mock.patch.dict(sys.modules, {"doris.slots": fake}):
            r = parser.parse_request("December 3 at 10am PT.", CLOCK, CLOCK)
        self.assertEqual((r.verdict, r.instant_utc), ("book", DEC3_10))

    def test_missing_slots_module_fails_closed(self):
        import sys
        from unittest import mock
        import doris
        # Missing even when an earlier test imported it: the package keeps a slots attribute too.
        with mock.patch.dict(sys.modules, {"doris.slots": None}), \
                mock.patch.object(doris, "slots", None, create=True):
            r = parser.parse_request("December 3 at 10am PT.", CLOCK, CLOCK)
            self.assertEqual(parser.preferred_starts(r, CLOCK), [])
        self.assertEqual((r.verdict, r.failed_condition), ("offer", "B8"))

    def test_uses_given_clock_not_wall_clock(self):
        # Sent and acted on in Feb 2027: "December 3" is Dec 3 2027, beyond 30 days.
        now = u(2027, 2, 1, 17)
        r = parser.parse_request("December 3 at 10am PT.", now, now, bookable=fake_bookable)
        self.assertEqual(r.verdict, "offer")
        self.assertEqual(r.readings[0].date, dt.date(2027, 12, 3))


# ---------------------------------------------------------------------------------------
# Times (H1-H23)
# ---------------------------------------------------------------------------------------
class TimesTest(Base):
    def test_time_hour_ampm(self):
        for t in ("10am", "10 am", "10AM"):
            self.book("December 3 at %s PT." % t, DEC3_10)
        self.book("December 3 at 2pm PT.", u(2026, 12, 3, 22))

    def test_time_dotted_ampm(self):
        for t in ("10 a.m.", "10 A.M.", "10a.m."):
            self.book("December 3 at %s PT" % t, DEC3_10)

    def test_time_short_a_p(self):
        self.book("December 3 at 10a PT.", DEC3_10)
        self.book("December 3 @ 2p PT.", u(2026, 12, 3, 22))
        self.book("December 3, 10:30a PT.", u(2026, 12, 3, 18, 30))

    def test_time_short_a_p_not_read_elsewhere(self):
        # "10a" with no "at" before it and no zone after it is not a time.
        self.offer("Room 10a, December 3.", "B2")

    def test_time_minutes_ampm(self):
        for t in ("10:00am", "10:00 a.m.", "10.00am"):
            self.book("December 3 at %s PT." % t, DEC3_10)
        self.book("December 3 at 10.30am PT.", u(2026, 12, 3, 18, 30))
        self.book("December 3 at 1:30 pm PT.", u(2026, 12, 3, 21, 30))

    def test_time_twelve_ampm(self):
        self.book("December 3 at 12pm PT.", u(2026, 12, 3, 20))
        self.book("December 3 at 12:30 pm PT.", u(2026, 12, 3, 20, 30))
        self.offer("December 3 at 12am PT.", "B7", first=u(2026, 12, 3, 20))

    def test_time_noon(self):
        for t in ("noon", "12 noon", "midday"):
            self.book("December 3 at %s PT." % t, u(2026, 12, 3, 20))

    def test_time_midnight(self):
        self.offer("December 3 at midnight PT.", "B7")
        self.offer("December 3 at 12 midnight PT.", "B7")

    def test_time_24h_colon(self):
        self.book("December 3 at 14:00 PT.", u(2026, 12, 3, 22))
        self.book("December 3 at 13:30 Pacific.", u(2026, 12, 3, 21, 30))
        self.book("December 3 at 09:00 PT.", u(2026, 12, 3, 17))
        self.offer("December 3 at 00:30 PT.", "B8", never=u(2026, 12, 3, 20, 30))

    def test_time_24h_four_digits(self):
        self.book("December 3 at 1400 PT.", u(2026, 12, 3, 22))
        self.book("December 3 at 1400 hours PT.", u(2026, 12, 3, 22))
        self.book("December 3 at 0900h PT.", u(2026, 12, 3, 17))
        self.book("Dec 3 1400 PT.", u(2026, 12, 3, 22))

    def test_time_four_digits_not_read_without_placement(self):
        self.offer("Ticket 1400 for December 3.", "B2")

    def test_time_zulu(self):
        self.book("December 3 at 1800Z.", DEC3_10)
        self.book("December 3 at 18:00Z.", DEC3_10)

    def test_time_minutes_bare(self):
        self.book("December 3 at 10:00 PT.", DEC3_10)
        self.book("December 3 at 3:30 PT.", u(2026, 12, 3, 23, 30))
        self.book("December 3 at 12:30 PT.", u(2026, 12, 3, 20, 30))
        self.offer("December 3 at 7:30 PT.", "B7")
        self.offer("December 3 at 10:00 UTC.", "B7")

    def test_time_bare_hour(self):
        self.book("December 3 at 10 PT.", DEC3_10)
        self.book("December 3 @ 2 PT.", u(2026, 12, 3, 22))
        self.offer("December 3 at 7 PT.", "B7")
        self.offer("December 3 at 8 PT.", "B7")

    def test_time_bare_hour_needs_at(self):
        self.offer("December 3, 10 PT.", "B2")

    def test_time_bare_hour_24(self):
        self.book("December 3 at 14 PT.", u(2026, 12, 3, 22))
        self.book("December 3 at 16 Pacific.", u(2026, 12, 4, 0))

    def test_time_words_ampm(self):
        self.book("December 3 at ten am PT.", DEC3_10)
        self.book("December 3 at two p.m. PT.", u(2026, 12, 3, 22))
        self.book("December 3 at ten thirty am PT.", u(2026, 12, 3, 18, 30))

    def test_time_oclock(self):
        self.book("December 3 at ten o'clock PT.", DEC3_10)
        self.book("December 3 at 10 o'clock PT.", DEC3_10)
        self.book("December 3 at 3 oclock PT.", u(2026, 12, 3, 23))
        self.offer("December 3 at 8 o'clock PT.", "B7")

    def test_time_day_part(self):
        self.book("December 3 at 10 in the morning PT.", DEC3_10)
        self.book("December 3 at ten in the morning PT.", DEC3_10)
        self.book("December 3 at 3 in the afternoon PT.", u(2026, 12, 3, 23))
        self.offer("December 3 at 7 in the evening PT.", "B8")
        self.offer("December 3 at 9 at night PT.", "B8")

    def test_time_words_minutes(self):
        self.book("December 3 at ten thirty PT.", u(2026, 12, 3, 18, 30))
        self.book("December 3 at ten fifteen PT.", u(2026, 12, 3, 18, 15))
        self.book("December 3 at ten forty-five PT.", u(2026, 12, 3, 18, 45))
        self.offer("December 3 at seven thirty PT.", "B7")

    def test_time_past_to(self):
        self.book("December 3 at half past ten PT.", u(2026, 12, 3, 18, 30))
        self.book("December 3 at quarter past ten PT.", u(2026, 12, 3, 18, 15))
        self.book("December 3 at a quarter after ten PT.", u(2026, 12, 3, 18, 15))
        self.book("December 3 at quarter to eleven PT.", u(2026, 12, 3, 18, 45))
        self.book("December 3 at quarter of eleven PT.", u(2026, 12, 3, 18, 45))
        r = self.offer("December 3 at ten past ten PT.", "B8")
        self.assertEqual([x.start_utc for x in r.readings if x.kept], [u(2026, 12, 3, 18, 10)])

    def test_time_seconds(self):
        self.book("December 3 at 10:00:00 am PT.", DEC3_10)
        self.book("December 3 at 14:00:00 PT.", u(2026, 12, 3, 22))
        self.offer("December 3 at 10:00:30 am PT.", "B2")

    def test_time_vague(self):
        windows = {
            "first thing": ((9, 0), (10, 0)), "morning": ((9, 0), (12, 0)),
            "mid-morning": ((10, 0), (11, 30)), "late morning": ((10, 30), (12, 0)),
            "lunchtime": ((12, 0), (13, 0)), "after lunch": ((13, 0), (15, 0)),
            "early afternoon": ((12, 0), (14, 0)), "afternoon": ((12, 0), (17, 0)),
            "late afternoon": ((15, 0), (17, 0)), "EOD": ((16, 0), (17, 0)),
            "end of day": ((16, 0), (17, 0)), "COB": ((16, 0), (17, 0)),
        }
        for word, (a, b) in windows.items():
            with self.subTest(word=word):
                r = self.offer("December 3, %s?" % word)
                self.assertEqual(r.steer_window, (dt.time(*a), dt.time(*b)))
                self.assertEqual(r.steer_day, dt.date(2026, 12, 3))
        r = self.offer("December 3 in the evening?")
        self.assertIsNone(r.steer_window)
        self.offer("Any time on December 3.")

    def test_time_vague_blocks_a_clock_time(self):
        self.offer("Thursday December 3, morning, 10am PT.", "B2")

    def test_time_impossible(self):
        for t in ("25:00", "10:75", "13pm", "0am"):
            with self.subTest(t=t):
                r = self.offer("December 3 at %s PT." % t, "B7")
                self.assertEqual(r.steer_day, dt.date(2026, 12, 3))

    def test_time_not_read(self):
        for t in ("14h30", "14h", "10", "10.5", "half ten"):
            with self.subTest(t=t):
                self.offer("December 3, %s PT." % t, "B2")

    def test_time_idioms_block_nothing(self):
        for idiom in ("1:1", "1-1", "1-on-1", "24/7"):
            with self.subTest(idiom=idiom):
                self.book("Could Shane do a %s on December 3 at 10am PT?" % idiom, DEC3_10)


# ---------------------------------------------------------------------------------------
# Qualifiers (Q1-Q10)
# ---------------------------------------------------------------------------------------
class QualifiersTest(Base):
    def test_qual_range(self):
        self.offer("December 3, 10-11am PT.", "B6", first=DEC3_10)
        self.offer("December 3 from 10am to 11am PT.", first=DEC3_10)
        self.offer("December 3 between 2 and 4 PT.")
        self.offer("December 3 from 10 until noon PT.")
        self.offer("December 3 at 9:00-5:00 PT.")

    def test_qual_alternatives(self):
        self.offer("December 3 at 10am or 2pm PT.", "B2")
        self.offer("Dec 3 or Dec 4 at 10am PT.", "B2")
        self.offer("December 3 and 4 at 10am PT.")
        self.offer("Either December 3 at 10am PT works.", "B6")

    def test_qual_hedge(self):
        for text in ("Maybe December 3 at 10am PT.", "Perhaps December 3 at 10am PT.",
                     "Probably December 3 at 10am PT.", "Say December 3 at 10am PT.",
                     "December 3 at 10am-ish PT.", "December 3 at 10am PT or so."):
            with self.subTest(text=text):
                self.offer(text, "B6", first=DEC3_10)
        # A hedge between the date and the time also breaks B3, which fails first.
        for text in ("December 3 at around 10am PT.", "December 3 at about 10am PT.",
                     "December 3, approximately 10am PT.", "December 3, roughly 10am PT."):
            with self.subTest(text=text):
                self.offer(text, first=DEC3_10)
        self.offer("Around 10am PT on December 3.", "B6", first=DEC3_10)

    def test_qual_hedge_elsewhere_blocks_nothing(self):
        self.book("Maybe we could chat. December 3 at 10am PT?", DEC3_10)

    def test_qual_bound(self):
        for text in ("After 3pm PT on December 3.", "Before noon PT on December 3.", "By 2pm PT on December 3.",
                     "No later than 4pm PT on December 3.", "December 3 after 3pm PT.",
                     "December 3, any time after 1pm PT."):
            with self.subTest(text=text):
                self.offer(text)
        self.offer("After 3pm PT on December 3.", "B6")
        self.offer("By 2pm PT on December 3.", "B6")

    def test_qual_bound_written_after_the_time(self):
        for text in ("December 3 at 10am PT or later.", "December 3 at 10am PT onwards.",
                     "December 3 at 10am PT at the earliest.", "December 3 at 10am PT or earlier.",
                     "December 3 at 10am PT at the latest.", "Starting December 3 at 10am PT.",
                     "Beginning December 3 at 10am PT."):
            with self.subTest(text=text):
                self.offer(text, "B6")

    def test_qual_negation(self):
        for text in ("I can't do December 3 at 10am PT.", "I cant do December 3 at 10am PT.",
                     "Not December 3 at 10am PT.", "December 3 at 10am PT is impossible.",
                     "I'm busy December 3 at 10am PT.", "Anything but December 3 at 10am PT.",
                     "December 3 at 10am PT is no longer good.", "I'm unable to do December 3 at 10am PT."):
            with self.subTest(text=text):
                r = self.offer(text, "B6")
                self.assertIn(DEC3_10, r.excluded_utc)

    def test_qual_negation_other_sentence_and_instead(self):
        self.book("No problem! December 3 at 10am PT?", DEC3_10)
        self.book("Could we do December 3 at 10am PT instead?", DEC3_10)
        self.book("I want an appointment on December 3 at 10am PT.", DEC3_10)

    def test_qual_two_times(self):
        self.offer("Dec 3 at 10am PT. If not, Dec 4 at 2pm PT.", "B2")
        self.offer("Tomorrow is tight. December 3 at 10am PT?", "B2")

    def test_qual_two_zones_same_instant(self):
        r = self.book("December 3 at 10am Pacific (1pm Eastern).", DEC3_10)
        self.assertEqual(r.zone_pair, ("Pacific", "Eastern"))
        # Matching only across a date line is an offer: 11pm Pacific Dec 3 is 2am Eastern Dec 4.
        self.offer("December 3 at 11pm PT / 2am ET.")

    def test_qual_two_zones_joined_by_or_is_an_alternative(self):
        self.offer("December 3 at 10am PT or 1pm ET.")
        self.offer("December 3 at 10am PT and 1pm ET.")

    def test_how_about_is_a_proposal_not_a_hedge(self):
        self.book("How about Thursday Dec 3rd @ 11:00am PST?", u(2026, 12, 3, 19))
        self.book("What about December 3 at 10am PT?", DEC3_10)

    def test_qual_two_zones_conflict(self):
        r = self.offer("December 3 at 10am PT (2pm ET).", "B2")
        kept = [x.start_utc for x in r.readings if x.kept]
        self.assertEqual(kept[:2], [DEC3_10, u(2026, 12, 3, 19)])

    def test_qual_not_side_by_side(self):
        self.offer("How about December 3? 10am PT is best.", "B3", first=DEC3_10)
        self.offer("December 3 would be great, 10am PT.", "B3", first=DEC3_10)

    def test_qual_not_the_meeting(self):
        for word in ("ends", "expires", "deadline", "due", "starts", "webinar", "flight", "lands",
                     "launch", "hard stop", "closes", "opens", "departs", "leaves"):
            with self.subTest(word=word):
                r = self.offer("The %s is December 3 at 10am PT." % word, "B6")
                self.assertEqual(parser.preferred_starts(r, CLOCK, bookable=fake_bookable), [])


# ---------------------------------------------------------------------------------------
# Dates (D1-D23)
# ---------------------------------------------------------------------------------------
class DatesTest(Base):
    def test_date_month_day(self):
        for d in ("December 3", "Dec 3", "Dec. 3", "december 3"):
            self.book("%s at 10am PT." % d, DEC3_10)
        self.book("Sept 3 at 10am PT.", u(2027, 9, 3, 17), now=u(2027, 8, 20, 17))

    def test_date_day_month(self):
        for d in ("3 December", "3 Dec", "3-Dec"):
            self.book("%s at 10am PT." % d, DEC3_10)

    def test_date_ordinal(self):
        for d in ("December 3rd", "3rd December", "the 3rd of December", "December the 3rd", "December third"):
            self.book("%s at 10am PT." % d, DEC3_10)

    def test_date_weekday_match(self):
        for d in ("Thursday, December 3", "Thu Dec 3", "Thurs 3 December", "thursday december 3", "Thu. Dec 3"):
            self.book("%s at 10am PT." % d, DEC3_10)

    def test_date_weekday_mismatch(self):
        r = self.offer("Wednesday, December 3 at 10am PT.", "B9")
        self.assertEqual(parser.preferred_starts(r, CLOCK, bookable=fake_bookable)[:2],
                         [DEC3_10, u(2026, 12, 2, 18)])

    def placed(self, text):
        return sorted(x.start_utc for x in P(text).readings if x.kept and x.start_utc is not None)

    def test_date_weekday_mismatch_never_moves_one_options_time_to_another_options_day(self):
        # D5 (Fable review of PR #3, finding 1): a weekday written with its own date, and matching it,
        # belongs to that date; it never puts Thursday's 10:15 on Friday or Friday's 2:45 on Thursday
        self.assertEqual(self.placed("Could Shane do Thursday, December 3 at 10:15 AM Pacific or Friday, "
                                     "December 4 at 2:45 PM Pacific for 30 minutes?"),
                         [u(2026, 12, 3, 18, 15), u(2026, 12, 4, 22, 45)])
        self.assertEqual(self.placed("Thursday at 10:15 AM PT or Friday at 2:45 PM PT?"),
                         [u(2026, 12, 3, 18, 15), u(2026, 12, 4, 22, 45)])

    def test_date_weekday_mismatch_a_weekday_with_its_own_date_moves_no_other_time(self):
        self.assertEqual(self.placed("I am out Monday December 7. Could Shane do Wednesday, December 9 at "
                                     "3:15 PM Pacific?"),
                         [u(2026, 12, 9, 23, 15)])

    def test_date_weekday_mismatch_a_lone_weekday_a_written_date_falls_on_moves_nothing(self):
        self.assertEqual(self.placed("Thursday, December 3 at 10am PT or Friday, December 4 at 2pm PT. "
                                     "Friday is best."),
                         [DEC3_10, u(2026, 12, 4, 22)])

    def test_date_weekday_mismatch_a_lone_weekday_no_date_falls_on_still_reads_twice(self):
        # the spec's own D5 examples keep both readings: the date, and that weekday nearest it
        self.assertEqual(self.placed("Thurs works. December 4 at 10am PT?"), [DEC3_10, u(2026, 12, 4, 18)])
        self.assertEqual(self.placed("Wednesday, December 3 at 10am PT."), [u(2026, 12, 2, 18), DEC3_10])
        self.assertEqual(self.placed("Happy Friday! Could Shane do Thursday, December 3 at 10am PT?"),
                         [DEC3_10, u(2026, 12, 4, 18)])

    def test_date_with_year(self):
        for d in ("December 3, 2026", "3 December 2026", "Dec 3 2026", "3-Dec-2026"):
            self.book("%s at 10am PT." % d, DEC3_10)
        self.offer("December 3, 2025 at 10am PT.", "B8")

    def test_date_no_year(self):
        r = self.offer("November 3 at 10am PT.", "B8")
        self.assertEqual(r.readings[0].date, dt.date(2027, 11, 3))

    def test_date_month_short(self):
        for m in ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Sept", "Oct", "Nov", "Dec"):
            r = P("%s 3 at 10am PT." % m)
            self.assertIsNotNone(r.readings, m)
            self.assertEqual(r.readings[0].date.day, 3, m)
        self.book("May I suggest December 3 at 10am PT?", DEC3_10)

    def test_date_month_not_read(self):
        self.offer("Decmber 3 at 10am PT.", "B2")
        self.offer("3 Dez at 10am PT.", "B2")

    def test_date_iso(self):
        for d in ("2026-12-03", "2026/12/03", "2026.12.03"):
            self.book("%s at 10am PT." % d, DEC3_10)

    def test_date_iso_timestamp(self):
        self.book("2026-12-03T18:00Z works.", DEC3_10)
        self.book("2026-12-03T10:00-08:00 works.", DEC3_10)
        self.offer("2026-12-03T10:00 works.", "B4")

    def test_date_numeric_one_reading(self):
        self.book("15/12 at 10am PT.", u(2026, 12, 15, 18))
        r = self.offer("25/12 at 10am PT.", "B8")   # Christmas: one reading, not bookable
        self.assertEqual([x.date for x in r.readings if x.kept], [dt.date(2026, 12, 25)])
        self.book("12/15 at 10am PT.", u(2026, 12, 15, 18))

    def test_date_numeric_same_parts(self):
        r = P("3/3 at 10am PT.")
        self.assertEqual([x.date for x in r.readings if x.kept], [dt.date(2027, 3, 3)])
        r = self.offer("12/12 at 10am PT.", "B8")   # one reading, a Saturday
        self.assertEqual([x.date for x in r.readings if x.kept], [dt.date(2026, 12, 12)])

    def test_date_numeric_month_first(self):
        self.book("12/3 at 10am PT.", DEC3_10)
        self.book("12/03 at 10am Pacific.", DEC3_10)
        self.offer("12/3 at 10am your time.", "B7", first=DEC3_10)
        self.offer("12/3 at 18:00 UTC.", "B7", first=DEC3_10)

    def test_date_numeric_day_first(self):
        self.offer("3/12 at 10am PT.", "B7", first=DEC3_10)
        self.offer("03/12 at 10am PT.", "B7", first=DEC3_10)

    def test_date_numeric_dots(self):
        self.offer("3.12 at 10am PT.", first=DEC3_10)
        self.offer("15.12 at 10am PT.", first=u(2026, 12, 15, 18))
        self.offer("12.3 at 10am PT.")
        # Anywhere else a dotted pair is a plain number and blocks nothing.
        self.book("Our plan costs 3.12 per seat. December 3 at 10am PT?", DEC3_10)

    def test_date_numeric_weekday_selects(self):
        self.book("Thursday 3/12 at 10am PT.", DEC3_10)
        self.book("Thu 3/12 at 10am PT.", DEC3_10)

    def test_date_numeric_with_year(self):
        self.book("12/3/2026 at 10am PT.", DEC3_10)
        self.book("12/3/26 at 10am PT.", DEC3_10)
        self.offer("3/12/2026 at 10am PT.", "B7", first=DEC3_10)
        self.book("15.12.2026 at 10am PT.", u(2026, 12, 15, 18))
        self.offer("03.12.2026 at 10am PT.", "B7", first=DEC3_10)
        self.offer("12-03-2026 at 10am PT.", "B7", first=DEC3_10)
        self.offer("12/3/2031 at 10am PT.")

    def test_date_numeric_both_bookable(self):
        now = u(2027, 2, 1, 17)
        r = self.offer("2/3 at 10am PT.", "B7", now=now)
        self.assertEqual(parser.preferred_starts(r, now, bookable=fake_bookable)[:2],
                         [u(2027, 2, 3, 18), u(2027, 3, 2, 18)])

    def test_date_dash_is_range(self):
        self.offer("12-3 at 10am PT.")
        self.offer("December 3, 10-11 PT.", "B6")

    def test_date_impossible(self):
        for d in ("February 30", "13/13", "12/32", "November 31", "31 November"):
            with self.subTest(d=d):
                self.offer("%s at 10am PT." % d, "B7")

    def test_date_outside_window(self):
        self.offer("November 3, 2026 at 10am PT.", "B8")
        self.offer("March 12 at 10am PT.", "B8")

    def test_date_lookalikes(self):
        for la in ("$12.30", "v3.12", "24/7", "50/50"):
            with self.subTest(la=la):
                self.book("We offer %s support. December 3 at 10am PT?" % la, DEC3_10)


# ---------------------------------------------------------------------------------------
# Relative dates (L1-L7)
# ---------------------------------------------------------------------------------------
class RelativeTest(Base):
    def test_rel_day_words(self):
        self.assertEqual(self.offer("Today at 4pm PT?", "B5").steer_day, dt.date(2026, 12, 1))
        self.assertEqual(self.offer("Tomorrow at 10am PT?", "B5").steer_day, dt.date(2026, 12, 2))
        self.assertEqual(self.offer("The day after tomorrow at 10am PT?").steer_day, dt.date(2026, 12, 3))
        self.offer("Tonight?", "B2")

    def test_rel_day_counted_in_pacific(self):
        # 03:00 UTC Dec 2 is still Dec 1 in Pacific, so tomorrow is Dec 2.
        late = u(2026, 12, 2, 3)
        r = parser.parse_request("Tomorrow at 10am PT?", late, late, bookable=fake_bookable)
        self.assertEqual(r.steer_day, dt.date(2026, 12, 2))

    def test_rel_weekday(self):
        for text in ("Thursday at 10am PT?", "On Thursday at 10am PT?", "This Thursday at 10am PT?"):
            with self.subTest(text=text):
                self.assertEqual(self.offer(text, "B5", first=DEC3_10).steer_day, dt.date(2026, 12, 3))

    def test_rel_next_weekday(self):
        r = self.offer("Next Thursday at 10am PT?", "B5")
        self.assertEqual(parser.preferred_starts(r, CLOCK, bookable=fake_bookable)[:2],
                         [DEC3_10, u(2026, 12, 10, 18)])

    def test_rel_day_number(self):
        self.assertEqual(self.offer("On the 3rd at 10am PT?", "B5").steer_day, dt.date(2026, 12, 3))
        self.assertEqual(self.offer("The 15th works?").steer_day, dt.date(2026, 12, 15))

    def test_rel_counted(self):
        self.assertEqual(self.offer("In 3 days at 10am PT?").steer_day, dt.date(2026, 12, 4))
        self.assertEqual(self.offer("In two weeks?").steer_day, dt.date(2026, 12, 15))
        self.assertEqual(self.offer("A week from Thursday?").steer_day, dt.date(2026, 12, 10))

    def test_rel_week(self):
        self.assertEqual(self.offer("Sometime next week?").steer_day, dt.date(2026, 12, 7))
        self.assertEqual(self.offer("This week?").steer_day, dt.date(2026, 12, 1))
        self.assertEqual(self.offer("Early next week?").steer_day, dt.date(2026, 12, 7))
        self.assertEqual(self.offer("The week of December 7?").steer_day, dt.date(2026, 12, 7))
        self.assertEqual(self.offer("The first week of December?").steer_day, dt.date(2026, 12, 1))

    def test_rel_open(self):
        for text in ("Soon?", "Sometime?", "ASAP please", "Next month?", "After the holidays?"):
            with self.subTest(text=text):
                r = self.offer(text, "B2")
                self.assertIsNone(r.steer_day)

    def test_rel_mention_blocks_other_date(self):
        self.offer("Hope to talk soon. December 3 at 10am PT?", "B2")


# ---------------------------------------------------------------------------------------
# Zones (Z1-Z16)
# ---------------------------------------------------------------------------------------
class ZonesRowsTest(Base):
    def test_zone_abbreviation(self):
        for z, h in (("PT", 20), ("PST", 20), ("MT", 19), ("MST", 19), ("CT", 18), ("CST", 18),
                     ("ET", 17), ("EST", 17), ("pt", 20), ("est", 17)):
            with self.subTest(z=z):
                self.book("December 3 at 12pm %s." % z, u(2026, 12, 3, h))

    def test_zone_abbreviation_daylight_in_summer(self):
        now = u(2027, 6, 1, 16)
        for z, h in (("PDT", 19), ("MDT", 18), ("CDT", 17), ("EDT", 16)):
            with self.subTest(z=z):
                self.book("June 3 at 12pm %s." % z, u(2027, 6, 3, h), now=now)

    def test_zone_spelled(self):
        for z, h in (("Pacific Time", 20), ("Pacific Standard Time", 20), ("Mountain Time", 19),
                     ("Central Standard Time", 18), ("Eastern Time", 17)):
            with self.subTest(z=z):
                self.book("December 3 at 12pm %s." % z, u(2026, 12, 3, h))
        self.offer("December 3 at 10am Pacific Daylight Time.", "B4")

    def test_zone_bare_region(self):
        self.book("December 3 at 10am Pacific.", DEC3_10)
        self.book("December 3 at 2pm Eastern works.", u(2026, 12, 3, 19))
        self.book("December 3 at 1 Central if that suits.", u(2026, 12, 3, 19))
        self.offer("December 3 at 10am Mountain View.", "B4")
        self.offer("December 3 at 10am Eastern Europe.", "B4")

    def test_zone_utc(self):
        self.book("December 3 at 6pm UTC.", DEC3_10)
        self.book("December 3 at 6pm GMT.", DEC3_10)
        self.book("December 3 at 18:00 Z.", DEC3_10)

    def test_zone_offset_prefixed(self):
        self.book("December 3 at 10am UTC-8.", DEC3_10)
        self.book("December 3 at 7pm UTC+01:00.", DEC3_10)
        self.book("December 3 at 7pm GMT+1.", DEC3_10)
        self.book("December 3 at 1pm GMT-0500.", DEC3_10)
        self.offer("December 3 at 7 UTC+1.", "B7")

    def test_zone_offset_bare(self):
        self.offer("December 3 at 10:00 -08:00.", "B4")
        self.offer("December 3 at 7pm +0100.", "B4")

    def test_zone_your_time(self):
        for z in ("your time", "your time zone", "Shane's time"):
            with self.subTest(z=z):
                self.book("December 3 at 10am %s." % z, DEC3_10)

    def test_zone_not_in_effect(self):
        self.offer("December 3 at 10am PDT.", "B4", first=DEC3_10)
        self.offer("June 3 at 10am PST.", "B4", now=u(2027, 6, 1, 16))

    def test_zone_brackets_comma(self):
        self.book("December 3 at 10am (PT).", DEC3_10)
        self.book("December 3 at 10am, Pacific.", DEC3_10)

    def test_zone_elsewhere(self):
        r = self.offer("I'm on Eastern time. Does December 3 at 1pm work?", "B4", first=DEC3_10)
        self.assertEqual(r.zone, "Eastern")

    def test_zone_none(self):
        r = self.offer("December 3 at 10am.", "B4", first=DEC3_10)
        self.assertIsNone(r.zone)

    def test_zone_unknown(self):
        for z in ("CET", "BST", "IST", "AEST", "JST", "my time", "London time", "East Coast time"):
            with self.subTest(z=z):
                r = self.offer("December 3 at 4pm %s." % z, "B4")
                self.assertEqual(r.steer_day, dt.date(2026, 12, 3))
                self.assertEqual(parser.preferred_starts(r, CLOCK, bookable=fake_bookable), [])

    def test_zone_second_zone(self):
        self.offer("I'm in New York (ET). 10am PT on Dec 3?", "B4", first=DEC3_10)
        self.offer("December 3 at 10am PT. I'm on Central Time.", "B4")

    def test_zone_lookalikes(self):
        self.book("We were est. 1998 in Billings, MT. December 3 at 10am PT?", DEC3_10)
        self.book("I work PT these days. December 3 at 10am PT?", DEC3_10)

    def test_zone_gmt_in_summer(self):
        now = u(2027, 6, 1, 16)
        r = self.offer("June 3 at 5pm GMT.", "B7", now=now)
        kept = sorted(x.start_utc for x in r.readings if x.kept)
        self.assertEqual(kept, [u(2027, 6, 3, 16), u(2027, 6, 3, 17)])
        self.book("June 3 at 5pm UTC.", u(2027, 6, 3, 17), now=now)


# ---------------------------------------------------------------------------------------
# Text and where she reads (N1-N5)
# ---------------------------------------------------------------------------------------
class TextTest(Base):
    def test_text_fold(self):
        self.book("December 3 at １０am ＰＴ.", DEC3_10)
        self.book("“December 3 at 10am PT” — works?", DEC3_10)

    def test_text_unfoldable(self):
        self.offer("December 3 at 10​am PT")
        self.offer("December 3 at 10am РT")   # Cyrillic Er look-alike

    def test_text_direction_controls(self):
        self.offer("‮December 3 at 10am PT", "B1")

    def test_text_own_body(self):
        # Only own_text reaches parse_request; what it holds is all she reads.
        self.offer("Looking forward to talking.", "B2")

    def test_text_sentences(self):
        self.book("Could Shane do December 3 at 10 a.m. PT? Thanks.", DEC3_10)
        self.offer("I cannot make\nDecember 3 at 10am PT.", "B6")
        self.book("No worries at all.\n\nDecember 3 at 10am PT?", DEC3_10)
        self.book("Dec. 3 at 10am PT works.", DEC3_10)
        self.book("December 3 at 10.30am PT works.", u(2026, 12, 3, 18, 30))


# ---------------------------------------------------------------------------------------
# Ruling 5: meeting length
# ---------------------------------------------------------------------------------------
class LengthTest(Base):
    def length(self, text):
        r = P(text)
        return (r.length_min, r.length_source, r.length_rule)

    def test_default(self):
        self.assertEqual(self.length("December 3 at 10am PT."), (30, "default", None))

    def test_minutes(self):
        for text, n in (("15 minutes", 15), ("20 min", 20), ("45 mins", 45), ("60 minutes", 60),
                        ("5 minute", 5), ("30-minute", 30)):
            with self.subTest(text=text):
                self.assertEqual(self.length("A %s call?" % text), (n, "written", None))

    def test_phrases(self):
        for text, n in (("half an hour", 30), ("half hour", 30), ("a quarter of an hour", 15),
                        ("an hour", 60), ("one hour", 60), ("1 hour", 60), ("1 hr", 60)):
            with self.subTest(text=text):
                self.assertEqual(self.length("Could we do %s?" % text), (n, "written", None))

    def test_half_an_hour_books_30(self):
        r = self.book("December 3 at 10am PT for half an hour.", DEC3_10)
        self.assertEqual(r.length_min, 30)

    def test_over_60(self):
        for text in ("90 minutes", "an hour and a half", "1.5 hours", "two hours", "2 hours", "75 min", "3 hours"):
            with self.subTest(text=text):
                n, source, rule = self.length("Could we do %s on December 3 at 10am PT?" % text)
                self.assertEqual((source, rule), ("written", "over_60"))
                self.assertGreater(n, 60)
                self.assertEqual(P("Could we do %s on December 3 at 10am PT?" % text).verdict, "offer")

    def test_open_ended(self):
        for text in ("as long as it takes", "the afternoon", "a few hours", "open-ended", "however long"):
            with self.subTest(text=text):
                self.assertEqual(self.length("Let's take %s." % text)[2], "open_ended")
        self.assertIsNone(self.length("December 3 at 3 in the afternoon PT.")[2])

    def test_all_day_recurring_multiple_zero(self):
        for text, rule in (("all day", "all_day"), ("a full day", "all_day"), ("the whole day", "all_day"),
                           ("every Tuesday", "recurring"), ("weekly", "recurring"), ("each week", "recurring"),
                           ("a recurring call", "recurring"), ("a standing call", "recurring"),
                           ("two calls", "multiple"), ("a series", "multiple"), ("a few meetings", "multiple"),
                           ("0 minutes", "zero")):
            with self.subTest(text=text):
                self.assertEqual(self.length("Could we do %s?" % text)[2], rule)

    def test_length_rule_never_books(self):
        r = P("A weekly call, December 3 at 10am PT?")
        self.assertEqual(r.verdict, "offer")
        self.assertEqual(r.length_rule, "recurring")


# ---------------------------------------------------------------------------------------
# B11: the model as second reader
# ---------------------------------------------------------------------------------------
def answer(date="2026-12-03", time24="10:00", zone="PT", cls="meeting_request", ok=True):
    return ModelAnswer(cls=cls, date=date, time24=time24, zone=zone, length_min=None, pick=None,
                       injection_seen=False, side_question=False, raw="{}", source="replayed",
                       request_sha256="0" * 64, ok=ok)


class ModelAgreesTest(Base):
    def test_book_model_agrees(self):
        r = P("December 3 at 10am PT.")
        ok, detail = parser.model_agrees(r, answer())
        self.assertTrue(ok)
        self.assertEqual(detail["reason"], "agree")
        self.assertEqual(detail["model_instant"], "2026-12-03T18:00:00Z")
        self.assertEqual(detail["parser_instant"], "2026-12-03T18:00:00Z")

    def test_same_zone_written_another_way(self):
        r = P("December 3 at 10am Pacific.")
        self.assertTrue(parser.model_agrees(r, answer(zone="PT"))[0])
        self.assertTrue(parser.model_agrees(r, answer(zone="Pacific Time"))[0])
        self.assertTrue(parser.model_agrees(r, answer(zone="PST"))[0])

    def test_r2_model_disagrees_offers(self):
        r = P("December 3 at 10am PT.")
        ok, detail = parser.model_agrees(r, answer(date="2026-12-04"))
        self.assertFalse(ok)
        self.assertEqual(detail["reason"], "date")

    def test_r2_model_says_3am_offers(self):
        r = P("December 3 at 3 PT works for me.")
        self.assertEqual(r.verdict, "book")
        ok, detail = parser.model_agrees(r, answer(time24="03:00"))
        self.assertFalse(ok)
        self.assertEqual(detail["reason"], "time")
        self.assertTrue(parser.model_agrees(r, answer(time24="15:00"))[0])

    def test_other_zone_disagrees(self):
        r = P("December 3 at 10am PT.")
        self.assertEqual(parser.model_agrees(r, answer(zone="ET"))[1]["reason"], "zone")
        self.assertEqual(parser.model_agrees(r, answer(zone=None))[1]["reason"], "zone")
        self.assertEqual(parser.model_agrees(r, answer(zone="PDT"))[1]["reason"], "zone")
        self.assertEqual(parser.model_agrees(r, answer(zone="CET"))[1]["reason"], "zone")

    def test_any_doubt_is_disagreement(self):
        r = P("December 3 at 10am PT.")
        for bad in (answer(ok=False), answer(cls="fyi"), answer(cls="unsure"), answer(time24="10am"),
                    answer(time24="24:00"), answer(date="2026-13-03"), answer(date=None), answer(time24=None),
                    answer(date="Dec 3"), None):
            with self.subTest(bad=bad):
                ok, detail = parser.model_agrees(r, bad)
                self.assertFalse(ok)
                self.assertNotEqual(detail["reason"], "agree")

    def test_parse_not_book_never_agrees(self):
        r = P("December 3 at 10am.")
        ok, detail = parser.model_agrees(r, answer())
        self.assertFalse(ok)
        self.assertEqual(detail["reason"], "parse_not_book")

    def test_q7_pair_either_written_pair_agrees(self):
        r = P("December 3 at 10am PT / 1pm ET.")
        self.assertTrue(parser.model_agrees(r, answer(time24="10:00", zone="PT"))[0])
        self.assertTrue(parser.model_agrees(r, answer(time24="13:00", zone="ET"))[0])
        self.assertTrue(parser.model_agrees(r, answer(time24="13:00", zone="Eastern"))[0])
        self.assertFalse(parser.model_agrees(r, answer(time24="13:00", zone="PT"))[0])
        # Same instant, but a zone she did not write: not one of the written pairs.
        self.assertFalse(parser.model_agrees(r, answer(time24="12:00", zone="CT"))[0])

    def test_your_time_is_pacific(self):
        r = P("December 3 at 10 your time.")
        self.assertTrue(parser.model_agrees(r, answer(zone="your time"))[0])
        self.assertTrue(parser.model_agrees(r, answer(zone="PT"))[0])

    def test_iso_timestamp_zone(self):
        r = P("2026-12-03T18:00Z works.")
        self.assertTrue(parser.model_agrees(r, answer(time24="18:00", zone="Z"))[0])
        self.assertTrue(parser.model_agrees(r, answer(time24="18:00", zone="UTC"))[0])
        r = P("2026-12-03T10:00-08:00 works.")
        self.assertTrue(parser.model_agrees(r, answer(time24="10:00", zone="-08:00"))[0])

    def test_never_raises(self):
        r = P("December 3 at 10am PT.")
        for weird in ("x", 3, answer(zone=5), answer(date=20261203)):
            self.assertFalse(parser.model_agrees(r, weird)[0])
        self.assertFalse(parser.model_agrees(None, answer())[0])


# ---------------------------------------------------------------------------------------
# Ruling 6: picks
# ---------------------------------------------------------------------------------------
NOW = CLOCK
OFFER = [
    Slot(u(2026, 12, 2, 19), u(2026, 12, 2, 19, 30)),       # Wed Dec 2, 11:00 AM PT (2:00 PM ET)
    Slot(u(2026, 12, 3, 22), u(2026, 12, 3, 22, 30)),       # Thu Dec 3, 2:00 PM PT (5:00 PM ET)
    Slot(u(2026, 12, 4, 18, 30), u(2026, 12, 4, 19)),       # Fri Dec 4, 10:30 AM PT (1:30 PM ET)
]


class MatchPickTest(unittest.TestCase):
    def pick(self, text, slots=OFFER, zone="Eastern"):
        r = parser.match_pick(text, slots, zone, NOW)
        self.assertIsInstance(r, PickResult)
        return (r.kind, r.slot_index)

    def test_by_number(self):
        for text, i in (("Option 2 works.", 1), ("option 1 please", 0), ("#3", 2), ("The second one, please.", 1),
                        ("the third", 2), ("Slot 1 is great", 0), ("Number 3.", 2), ("1", 0), ("3 works", 2),
                        ("Let's go with the 2nd.", 1), ("I'll take the first", 0)):
            with self.subTest(text=text):
                self.assertEqual(self.pick(text), ("matched_slot", i))

    def test_bare_number_that_is_also_an_offered_hour_is_ambiguous(self):
        self.assertEqual(self.pick("2"), ("ambiguous", None))
        self.assertEqual(self.pick("2 works for me"), ("ambiguous", None))

    def test_number_out_of_range(self):
        self.assertEqual(self.pick("Option 4"), ("none", None))

    def test_two_numbers_ambiguous(self):
        self.assertEqual(self.pick("Option 1 or option 3"), ("ambiguous", None))

    def test_by_time_pacific(self):
        self.assertEqual(self.pick("11am works"), ("matched_slot", 0))
        self.assertEqual(self.pick("Thursday at 2pm Pacific works"), ("matched_slot", 1))
        self.assertEqual(self.pick("10:30 is good"), ("matched_slot", 2))
        self.assertEqual(self.pick("Friday 10:30am PT"), ("matched_slot", 2))

    def test_by_time_sender_zone(self):
        self.assertEqual(self.pick("5pm Eastern on Thursday"), ("matched_slot", 1))
        self.assertEqual(self.pick("1:30 PM ET please"), ("matched_slot", 2))
        self.assertEqual(self.pick("2pm ET"), ("matched_slot", 0))

    def test_time_matching_two_slots_in_either_zone_is_ambiguous(self):
        # 2 PM is slot 2 in Pacific and slot 1 in Eastern.
        self.assertEqual(self.pick("2pm works"), ("ambiguous", None))

    def test_date_settles_the_time(self):
        self.assertEqual(self.pick("Dec 3 at 2"), ("matched_slot", 1))
        self.assertEqual(self.pick("Wednesday at 2pm"), ("matched_slot", 0))

    def test_bare_hour_after_at_in_a_pick(self):
        # In a pick, "at 2" is read whatever follows it; the weekday settles which zone.
        self.assertEqual(self.pick("Thursday at 2 works great"), ("matched_slot", 1))
        self.assertEqual(self.pick("Wednesday at 2 works great"), ("matched_slot", 0))
        self.assertEqual(self.pick("at 2 works great"), ("ambiguous", None))

    def test_number_at_end_of_sentence(self):
        self.assertEqual(self.pick("I'll take 1."), ("matched_slot", 0))

    def test_near_miss_offered_day_other_time(self):
        self.assertEqual(self.pick("Thursday at 3pm?"), ("near_miss", None))

    def test_near_miss_offered_time_wrong_zone(self):
        self.assertEqual(self.pick("11am Eastern?"), ("near_miss", None))

    def test_utc_shown_when_sender_zone_unknown(self):
        self.assertEqual(self.pick("7pm UTC", zone=None), ("matched_slot", 0))
        self.assertEqual(self.pick("7pm UTC", zone="Eastern"), ("near_miss", None))

    def test_yes_single(self):
        one = OFFER[:1]
        for text in ("Yes", "yes!", "Sounds good.", "That works", "Perfect"):
            with self.subTest(text=text):
                self.assertEqual(self.pick(text, slots=one), ("yes_single", 0))

    def test_yes_with_three_slots_resends(self):
        self.assertEqual(self.pick("Yes, sounds good"), ("ambiguous", None))

    def test_none(self):
        self.assertEqual(self.pick("Let me check with my team and get back to you."), ("none", None))
        self.assertEqual(self.pick("4pm?"), ("none", None))
        self.assertEqual(self.pick(""), ("none", None))
        self.assertEqual(self.pick(None), ("none", None))
        self.assertEqual(self.pick("2", slots=[]), ("none", None))

    def test_kinds_are_rule_words(self):
        from doris import why
        for kind in ("matched_slot", "ambiguous", "near_miss", "yes_single", "none"):
            self.assertIn(kind, why.RULE_WORDS)


if __name__ == "__main__":
    unittest.main()
