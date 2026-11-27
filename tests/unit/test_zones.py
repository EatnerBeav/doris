"""zones.py: coded US and UK daylight rules, no zone database."""
import datetime as dt
import unittest

from doris import zones

UTC = dt.timezone.utc


def u(*args):
    return dt.datetime(*args, tzinfo=UTC)


class UsDaylightTest(unittest.TestCase):
    # US 2026: daylight from Sun Mar 8 to Sun Nov 1. 2027: Sun Mar 14 to Sun Nov 7.

    def test_pacific_winter_and_summer_offsets(self):
        self.assertEqual(zones.utc_offset("Pacific", u(2026, 12, 3, 18)), dt.timedelta(hours=-8))
        self.assertEqual(zones.utc_offset("Pacific", u(2026, 7, 1, 18)), dt.timedelta(hours=-7))

    def test_each_us_zone_standard_offset(self):
        for zid, hours in (("Pacific", -8), ("Mountain", -7), ("Central", -6), ("Eastern", -5)):
            self.assertEqual(zones.utc_offset(zid, u(2026, 12, 3, 18)), dt.timedelta(hours=hours), zid)

    def test_each_us_zone_daylight_offset(self):
        for zid, hours in (("Pacific", -7), ("Mountain", -6), ("Central", -5), ("Eastern", -4)):
            self.assertEqual(zones.utc_offset(zid, u(2026, 7, 3, 18)), dt.timedelta(hours=hours), zid)

    def test_spring_2026_switch_at_2am_pacific(self):
        # 2:00 AM PST on Mar 8 2026 is 10:00 UTC.
        self.assertEqual(zones.utc_offset("Pacific", u(2026, 3, 8, 9, 59)), dt.timedelta(hours=-8))
        self.assertEqual(zones.utc_offset("Pacific", u(2026, 3, 8, 10, 0)), dt.timedelta(hours=-7))

    def test_fall_2026_switch_at_2am_pacific(self):
        # 2:00 AM PDT on Nov 1 2026 is 09:00 UTC.
        self.assertEqual(zones.utc_offset("Pacific", u(2026, 11, 1, 8, 59)), dt.timedelta(hours=-7))
        self.assertEqual(zones.utc_offset("Pacific", u(2026, 11, 1, 9, 0)), dt.timedelta(hours=-8))

    def test_spring_2027_switch_eastern(self):
        # 2:00 AM EST on Mar 14 2027 is 07:00 UTC.
        self.assertEqual(zones.utc_offset("Eastern", u(2027, 3, 14, 6, 59)), dt.timedelta(hours=-5))
        self.assertEqual(zones.utc_offset("Eastern", u(2027, 3, 14, 7, 0)), dt.timedelta(hours=-4))

    def test_fall_2027_switch_eastern(self):
        # 2:00 AM EDT on Nov 7 2027 is 06:00 UTC.
        self.assertEqual(zones.utc_offset("Eastern", u(2027, 11, 7, 5, 59)), dt.timedelta(hours=-4))
        self.assertEqual(zones.utc_offset("Eastern", u(2027, 11, 7, 6, 0)), dt.timedelta(hours=-5))

    def test_day_before_switch_is_still_old_offset(self):
        self.assertEqual(zones.utc_offset("Pacific", u(2026, 3, 7, 20)), dt.timedelta(hours=-8))
        self.assertEqual(zones.utc_offset("Pacific", u(2027, 11, 6, 20)), dt.timedelta(hours=-7))

    def test_to_utc_local_noon_on_switch_days(self):
        self.assertEqual(zones.to_utc(dt.datetime(2026, 3, 8, 12), "Pacific"), u(2026, 3, 8, 19))
        self.assertEqual(zones.to_utc(dt.datetime(2026, 11, 1, 12), "Pacific"), u(2026, 11, 1, 20))
        self.assertEqual(zones.to_utc(dt.datetime(2027, 3, 14, 12), "Central"), u(2027, 3, 14, 17))
        self.assertEqual(zones.to_utc(dt.datetime(2027, 11, 7, 12), "Mountain"), u(2027, 11, 7, 19))

    def test_to_utc_local_just_before_and_after_spring_switch(self):
        self.assertEqual(zones.to_utc(dt.datetime(2026, 3, 8, 1, 30), "Pacific"), u(2026, 3, 8, 9, 30))
        self.assertEqual(zones.to_utc(dt.datetime(2026, 3, 8, 3, 0), "Pacific"), u(2026, 3, 8, 10, 0))


class UkSummerTest(unittest.TestCase):
    # UK 2026: summer from Sun Mar 29 to Sun Oct 25. 2027: Sun Mar 28 to Sun Oct 31. Both at 01:00 UTC.

    def test_uk_2026_switches(self):
        self.assertEqual(zones.utc_offset("UK", u(2026, 3, 29, 0, 59)), dt.timedelta(0))
        self.assertEqual(zones.utc_offset("UK", u(2026, 3, 29, 1, 0)), dt.timedelta(hours=1))
        self.assertEqual(zones.utc_offset("UK", u(2026, 10, 25, 0, 59)), dt.timedelta(hours=1))
        self.assertEqual(zones.utc_offset("UK", u(2026, 10, 25, 1, 0)), dt.timedelta(0))

    def test_uk_2027_switches(self):
        self.assertEqual(zones.utc_offset("UK", u(2027, 3, 28, 0, 59)), dt.timedelta(0))
        self.assertEqual(zones.utc_offset("UK", u(2027, 3, 28, 1, 0)), dt.timedelta(hours=1))
        self.assertEqual(zones.utc_offset("UK", u(2027, 10, 31, 0, 59)), dt.timedelta(hours=1))
        self.assertEqual(zones.utc_offset("UK", u(2027, 10, 31, 1, 0)), dt.timedelta(0))

    def test_uk_in_december_is_utc(self):
        self.assertEqual(zones.to_utc(dt.datetime(2026, 12, 3, 20), "UK"), u(2026, 12, 3, 20))

    def test_uk_in_july_is_one_hour_ahead(self):
        self.assertEqual(zones.to_utc(dt.datetime(2026, 7, 3, 20), "UK"), u(2026, 7, 3, 19))


class FixedZonesTest(unittest.TestCase):
    def test_utc_is_zero(self):
        self.assertEqual(zones.utc_offset("UTC", u(2026, 7, 1)), dt.timedelta(0))
        self.assertEqual(zones.to_utc(dt.datetime(2026, 12, 3, 18), "UTC"), u(2026, 12, 3, 18))

    def test_fixed_offsets(self):
        self.assertEqual(zones.to_utc(dt.datetime(2026, 12, 3, 10), "UTC-08:00"), u(2026, 12, 3, 18))
        self.assertEqual(zones.to_utc(dt.datetime(2026, 12, 3, 19), "UTC+01:00"), u(2026, 12, 3, 18))
        self.assertEqual(zones.utc_offset("UTC+05:30", u(2026, 7, 1)), dt.timedelta(hours=5, minutes=30))

    def test_offset_zone_id_builder(self):
        self.assertEqual(zones.offset_zone(-8 * 60), "UTC-08:00")
        self.assertEqual(zones.offset_zone(60), "UTC+01:00")
        self.assertEqual(zones.offset_zone(0), "UTC")

    def test_unknown_zone_raises(self):
        for bad in ("CET", "", None, "UTC+25:00", "UTC+1", "Pacific/Los_Angeles"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                zones.utc_offset(bad, u(2026, 12, 3))

    def test_is_zone(self):
        self.assertTrue(zones.is_zone("Pacific"))
        self.assertTrue(zones.is_zone("UTC-05:00"))
        self.assertFalse(zones.is_zone("CET"))


class ConversionTest(unittest.TestCase):
    def test_to_utc_requires_naive(self):
        with self.assertRaises(ValueError):
            zones.to_utc(u(2026, 12, 3, 10), "Pacific")

    def test_to_local_requires_aware(self):
        with self.assertRaises(ValueError):
            zones.to_local(dt.datetime(2026, 12, 3, 10), "Pacific")

    def test_to_local_returns_naive(self):
        local = zones.to_local(u(2026, 12, 3, 18), "Pacific")
        self.assertEqual(local, dt.datetime(2026, 12, 3, 10))
        self.assertIsNone(local.tzinfo)

    def test_to_local_eastern_and_round_trip(self):
        self.assertEqual(zones.to_local(u(2026, 12, 3, 18), "Eastern"), dt.datetime(2026, 12, 3, 13))
        for zid in ("Pacific", "Mountain", "Central", "Eastern", "UTC", "UK", "UTC+09:00"):
            for when in (u(2026, 12, 3, 18), u(2027, 6, 15, 16, 45)):
                self.assertEqual(zones.to_utc(zones.to_local(when, zid), zid), when, zid)

    def test_to_utc_returns_utc_tz(self):
        self.assertEqual(zones.to_utc(dt.datetime(2026, 12, 3, 10), "Pacific").utcoffset(), dt.timedelta(0))

    def test_pacific_date(self):
        # 2026-12-04 05:00 UTC is still Dec 3 in Pacific.
        self.assertEqual(zones.pacific_date(u(2026, 12, 4, 5)), dt.date(2026, 12, 3))
        self.assertEqual(zones.pacific_date(u(2026, 12, 4, 8)), dt.date(2026, 12, 4))


class AbbreviationTest(unittest.TestCase):
    def test_standard_abbreviations_in_winter(self):
        for abbr in ("PST", "MST", "CST", "EST", "pst"):
            self.assertTrue(zones.abbr_in_effect(abbr, dt.date(2026, 12, 3)), abbr)
        for abbr in ("PDT", "MDT", "CDT", "EDT", "pdt"):
            self.assertFalse(zones.abbr_in_effect(abbr, dt.date(2026, 12, 3)), abbr)

    def test_daylight_abbreviations_in_summer(self):
        for abbr in ("PDT", "MDT", "CDT", "EDT"):
            self.assertTrue(zones.abbr_in_effect(abbr, dt.date(2026, 7, 3)), abbr)
        for abbr in ("PST", "MST", "CST", "EST"):
            self.assertFalse(zones.abbr_in_effect(abbr, dt.date(2026, 7, 3)), abbr)

    def test_abbreviations_on_switch_days_follow_business_hours(self):
        # On the switch day the new rule holds from 2 AM, so all of 9-5 is the new one.
        self.assertTrue(zones.abbr_in_effect("PDT", dt.date(2026, 3, 8)))
        self.assertFalse(zones.abbr_in_effect("PST", dt.date(2026, 3, 8)))
        self.assertTrue(zones.abbr_in_effect("PST", dt.date(2026, 11, 1)))
        self.assertFalse(zones.abbr_in_effect("PDT", dt.date(2026, 11, 1)))
        self.assertTrue(zones.abbr_in_effect("EDT", dt.date(2027, 3, 14)))
        self.assertTrue(zones.abbr_in_effect("EST", dt.date(2027, 11, 7)))

    def test_plain_forms_always_in_effect(self):
        for abbr in ("PT", "MT", "CT", "ET", "UTC", "GMT", "Z"):
            self.assertTrue(zones.abbr_in_effect(abbr, dt.date(2026, 7, 3)), abbr)
            self.assertTrue(zones.abbr_in_effect(abbr, dt.date(2026, 12, 3)), abbr)

    def test_unknown_abbreviation_not_in_effect(self):
        for abbr in ("CET", "BST", "IST", "JST", ""):
            self.assertFalse(zones.abbr_in_effect(abbr, dt.date(2026, 12, 3)), abbr)

    def test_abbr_zone(self):
        self.assertEqual(zones.abbr_zone("PST"), "Pacific")
        self.assertEqual(zones.abbr_zone("edt"), "Eastern")
        self.assertEqual(zones.abbr_zone("MT"), "Mountain")
        self.assertEqual(zones.abbr_zone("CT"), "Central")
        self.assertEqual(zones.abbr_zone("GMT"), "UTC")
        self.assertIsNone(zones.abbr_zone("CET"))


class DisplayTest(unittest.TestCase):
    def test_display_names(self):
        self.assertEqual(zones.display("Pacific"), "Pacific")
        self.assertEqual(zones.display("Central"), "Central")
        self.assertEqual(zones.display("UTC"), "UTC")
        self.assertEqual(zones.display("UTC+01:00"), "UTC+01:00")
        self.assertEqual(zones.display("UK"), "UK")

    def test_display_unknown_raises(self):
        with self.assertRaises(ValueError):
            zones.display("CET")


if __name__ == "__main__":
    unittest.main()
