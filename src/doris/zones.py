"""Zones without a zone database (ruling 2, "Daylight time without a zone database").

Zone ids: "Pacific", "Mountain", "Central", "Eastern", "UTC", fixed offsets "UTC+HH:MM" /
"UTC-HH:MM", and "UK" (only for the GMT-in-summer second reading, row Z16).

US daylight time is coded directly: from the second Sunday of March at 2:00 AM local to the
first Sunday of November at 2:00 AM local. UK summer time: from the last Sunday of March to
the last Sunday of October, at 01:00 UTC both ways. The gap and repeat hours are not handled
(B10 is cut): a local time inside them takes the offset in effect just before the change.

Everything here is pure: no wall clock, no environment.
"""

import datetime as _dt
import re
from typing import Optional

UTC = _dt.timezone.utc

US_STANDARD_HOURS = {"Pacific": -8, "Mountain": -7, "Central": -6, "Eastern": -5}
US_ZONES = tuple(US_STANDARD_HOURS)

_OFFSET_ID = re.compile(r"^UTC([+-])(\d{2}):(\d{2})$")

# Abbreviation -> (zone id, "standard" | "daylight" | None for a form good all year)
_ABBR = {
    "PT": ("Pacific", None), "PST": ("Pacific", "standard"), "PDT": ("Pacific", "daylight"),
    "MT": ("Mountain", None), "MST": ("Mountain", "standard"), "MDT": ("Mountain", "daylight"),
    "CT": ("Central", None), "CST": ("Central", "standard"), "CDT": ("Central", "daylight"),
    "ET": ("Eastern", None), "EST": ("Eastern", "standard"), "EDT": ("Eastern", "daylight"),
    "UTC": ("UTC", None), "GMT": ("UTC", None), "Z": ("UTC", None),
}


def _nth_sunday(year, month, n):
    first = _dt.date(year, month, 1)
    return first + _dt.timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))


def _last_sunday(year, month):
    nxt = _dt.date(year + (month // 12), month % 12 + 1, 1)
    last = nxt - _dt.timedelta(days=1)
    return last - _dt.timedelta(days=(last.weekday() + 1) % 7)


def us_daylight_start(year):
    """The local date daylight time starts (second Sunday of March)."""
    return _nth_sunday(year, 3, 2)


def us_daylight_end(year):
    """The local date daylight time ends (first Sunday of November)."""
    return _nth_sunday(year, 11, 1)


def _parse_offset_id(zone_id):
    m = _OFFSET_ID.match(zone_id)
    if not m:
        return None
    hours, minutes = int(m.group(2)), int(m.group(3))
    if minutes >= 60:
        return None
    total = hours * 60 + minutes
    if total > 14 * 60:
        return None
    return _dt.timedelta(minutes=total if m.group(1) == "+" else -total)


def is_zone(zone_id) -> bool:
    if not isinstance(zone_id, str):
        return False
    return zone_id in US_STANDARD_HOURS or zone_id in ("UTC", "UK") or _parse_offset_id(zone_id) is not None


def _check(zone_id):
    if not is_zone(zone_id):
        raise ValueError("unknown zone id %r" % (zone_id,))


def offset_zone(minutes: int) -> str:
    """The zone id for a fixed offset east of UTC, in minutes ("UTC" for zero)."""
    if minutes == 0:
        return "UTC"
    sign = "+" if minutes > 0 else "-"
    m = abs(int(minutes))
    zid = "UTC%s%02d:%02d" % (sign, m // 60, m % 60)
    _check(zid)
    return zid


def _us_daylight_at_utc(zone_id, utc):
    std = _dt.timedelta(hours=US_STANDARD_HOURS[zone_id])
    year = utc.year
    # Start: 2:00 AM local standard time. End: 2:00 AM local daylight time.
    start = _dt.datetime.combine(us_daylight_start(year), _dt.time(2)) - std
    end = _dt.datetime.combine(us_daylight_end(year), _dt.time(2)) - (std + _dt.timedelta(hours=1))
    naive = utc.astimezone(UTC).replace(tzinfo=None)
    return start <= naive < end


def _us_daylight_at_local(local_naive):
    year = local_naive.year
    start = _dt.datetime.combine(us_daylight_start(year), _dt.time(2))
    end = _dt.datetime.combine(us_daylight_end(year), _dt.time(2))
    return start <= local_naive < end


def _uk_summer_at_utc(utc):
    year = utc.year
    start = _dt.datetime.combine(_last_sunday(year, 3), _dt.time(1))
    end = _dt.datetime.combine(_last_sunday(year, 10), _dt.time(1))
    naive = utc.astimezone(UTC).replace(tzinfo=None)
    return start <= naive < end


def _uk_summer_at_local(local_naive):
    # Local 01:00 GMT is 01:00 UTC at the start; local 02:00 BST is 01:00 UTC at the end.
    year = local_naive.year
    start = _dt.datetime.combine(_last_sunday(year, 3), _dt.time(1))
    end = _dt.datetime.combine(_last_sunday(year, 10), _dt.time(2))
    return start <= local_naive < end


def utc_offset(zone_id, utc) -> _dt.timedelta:
    """The offset of zone_id at the aware instant utc."""
    _check(zone_id)
    if utc.tzinfo is None:
        raise ValueError("utc must be timezone-aware")
    if zone_id in US_STANDARD_HOURS:
        hours = US_STANDARD_HOURS[zone_id] + (1 if _us_daylight_at_utc(zone_id, utc) else 0)
        return _dt.timedelta(hours=hours)
    if zone_id == "UTC":
        return _dt.timedelta(0)
    if zone_id == "UK":
        return _dt.timedelta(hours=1) if _uk_summer_at_utc(utc) else _dt.timedelta(0)
    return _parse_offset_id(zone_id)


def _offset_for_local(zone_id, local_naive):
    if zone_id in US_STANDARD_HOURS:
        return _dt.timedelta(hours=US_STANDARD_HOURS[zone_id] + (1 if _us_daylight_at_local(local_naive) else 0))
    if zone_id == "UTC":
        return _dt.timedelta(0)
    if zone_id == "UK":
        return _dt.timedelta(hours=1) if _uk_summer_at_local(local_naive) else _dt.timedelta(0)
    return _parse_offset_id(zone_id)


def to_utc(naive_local, zone_id) -> _dt.datetime:
    """A naive local wall time in zone_id -> aware UTC."""
    _check(zone_id)
    if not isinstance(naive_local, _dt.datetime) or naive_local.tzinfo is not None:
        raise ValueError("to_utc takes a naive local datetime")
    return (naive_local - _offset_for_local(zone_id, naive_local)).replace(tzinfo=UTC)


def to_local(utc, zone_id) -> _dt.datetime:
    """An aware instant -> naive local wall time in zone_id."""
    _check(zone_id)
    if not isinstance(utc, _dt.datetime) or utc.tzinfo is None:
        raise ValueError("to_local takes an aware datetime")
    return (utc.astimezone(UTC) + utc_offset(zone_id, utc)).replace(tzinfo=None)


def pacific_date(utc) -> _dt.date:
    return to_local(utc, "Pacific").date()


def abbr_zone(abbr) -> Optional[str]:
    """The zone id an abbreviation names (PT, PST, ..., UTC, GMT, Z), else None."""
    if not isinstance(abbr, str):
        return None
    hit = _ABBR.get(abbr.strip().upper())
    return hit[0] if hit else None


def abbr_in_effect(abbr, local_date) -> bool:
    """True when the abbreviation names a zone and is in effect on that local date (row Z8).

    The switch day takes the new rule: it changes at 2:00 AM, before any bookable hour.
    """
    if not isinstance(abbr, str):
        return False
    hit = _ABBR.get(abbr.strip().upper())
    if not hit:
        return False
    zone_id, kind = hit
    if kind is None:
        return True
    daylight = _us_daylight_at_local(_dt.datetime.combine(local_date, _dt.time(12)))
    return daylight if kind == "daylight" else not daylight


_DISPLAY = {"Pacific": "Pacific", "Mountain": "Mountain", "Central": "Central", "Eastern": "Eastern",
            "UTC": "UTC", "UK": "UK"}


def display(zone_id) -> str:
    """The zone's name as she writes it after a time: "Pacific", "Eastern", "UTC", "UTC+01:00"."""
    _check(zone_id)
    return _DISPLAY.get(zone_id, zone_id)
