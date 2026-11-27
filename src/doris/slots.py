"""Where a slot can sit (G9), what counts as busy (ruling 3) and which slots she offers (ruling 6).

Times are aware UTC. Pacific exists only inside these functions, through zones.py, which is
imported where it is used (another builder owns it). Every rule returned is a word from
why.RULE_WORDS: length, not_quarter_hour, outside_hours, holiday, under_24h, over_30d, and
overlap for a busy span.
"""

import datetime as _dt
from typing import Iterable, List, Optional, Sequence, Tuple

from . import caps
from .shapes import Event, Slot

UTC = _dt.timezone.utc
_DAY = _dt.timedelta(days=1)
PACIFIC = "Pacific"


def _zones():
    from . import zones
    return zones


def _aware(value, what):
    if not isinstance(value, _dt.datetime) or value.tzinfo is None:
        raise ValueError("%s must be a timezone-aware datetime, got %r" % (what, value))
    return value.astimezone(UTC)


def _pacific_midnight(day):
    return _zones().to_utc(_dt.datetime.combine(day, _dt.time(0)), PACIFIC)


# ---------------------------------------------------------------------------------------
# G9: where a slot can sit
# ---------------------------------------------------------------------------------------
def bookable(slot: Slot, now: _dt.datetime, written: bool) -> Tuple[bool, Optional[str]]:
    """(True, None) when the slot may be booked or offered, else (False, rule).

    written=True: a time the sender wrote, which may start on any quarter hour.
    written=False: a slot she offers, which starts on the hour or half hour.
    """
    start = _aware(slot.start_utc, "slot start")
    end = _aware(slot.end_utc, "slot end")
    now = _aware(now, "now")

    length = end - start
    minutes, rest = divmod(length.total_seconds(), 60)
    if rest or not (caps.MIN_LENGTH_MIN <= minutes <= caps.MAX_LENGTH_MIN):
        return False, "length"

    zones = _zones()
    local_start = zones.to_local(start, PACIFIC)
    local_end = zones.to_local(end, PACIFIC)
    grid = caps.WRITTEN_START_MINUTES if written else caps.OFFERED_START_MINUTES
    if local_start.minute not in grid or local_start.second or local_start.microsecond:
        return False, "not_quarter_hour"

    day = local_start.date()
    opens = _dt.datetime.combine(day, caps.BUSINESS_START)
    closes = _dt.datetime.combine(day, caps.BUSINESS_END)
    if day.weekday() not in caps.BUSINESS_DAYS or local_start < opens or local_end > closes:
        return False, "outside_hours"
    if day in caps.HOLIDAYS:
        return False, "holiday"
    if start < now + caps.MIN_NOTICE:
        return False, "under_24h"
    if start > now + caps.MAX_HORIZON:
        return False, "over_30d"
    return True, None


# ---------------------------------------------------------------------------------------
# Ruling 3: what counts as busy
# ---------------------------------------------------------------------------------------
def _busy_window(ev: Event) -> Optional[Tuple[_dt.datetime, _dt.datetime]]:
    """The UTC span an event makes busy, or None when it cannot be placed (then it is busy
    everywhere: fail closed)."""
    if ev.kind == "timed":
        if ev.start_utc is None or ev.end_utc is None:
            return None
        start, end = _aware(ev.start_utc, "event start"), _aware(ev.end_utc, "event end")
        return None if end < start else (start, end)

    if ev.kind in ("all_day", "unknown_zone"):
        first_day, end_day = ev.start_date, ev.end_date
        if first_day is None:
            if ev.start_utc is None:
                return None
            if ev.kind == "all_day" and ev.end_utc is not None:
                return _aware(ev.start_utc, "event start"), _aware(ev.end_utc, "event end")
            # unknown_zone: the whole Pacific day (or days) its times touch.
            pacific_date = _zones().pacific_date
            first_day = pacific_date(_aware(ev.start_utc, "event start"))
            last = ev.end_utc if ev.end_utc is not None else ev.start_utc
            end_day = pacific_date(_aware(last, "event end")) + _DAY
        if end_day is None or end_day <= first_day:
            end_day = first_day + _DAY
        return _pacific_midnight(first_day), _pacific_midnight(end_day)

    return None


def overlapping(events: Iterable[Event], slot: Slot) -> List[Event]:
    """Every event that is not cancelled and overlaps any part of the slot. Marked free,
    tentative, all-day and repeating events count; back-to-back does not overlap. An event
    with no readable zone is busy for its whole Pacific day; one that cannot be placed at all
    is busy everywhere."""
    start = _aware(slot.start_utc, "slot start")
    end = _aware(slot.end_utc, "slot end")
    hits = []
    for ev in events:
        if (ev.status or "").upper() == "CANCELLED":
            continue
        window = _busy_window(ev)
        if window is None or (window[0] < end and window[1] > start):
            hits.append(ev)
    return hits


def busy(events: Iterable[Event], slot: Slot) -> bool:
    return bool(overlapping(events, slot))


def available(events: Iterable[Event], slot: Slot, now: _dt.datetime, written: bool = True
              ) -> Tuple[bool, Optional[str]]:
    """bookable() and then busy(): (True, None), or (False, rule) with rule "overlap" when busy."""
    ok, rule = bookable(slot, now, written)
    if not ok:
        return False, rule
    if busy(events, slot):
        return False, "overlap"
    return True, None


# ---------------------------------------------------------------------------------------
# Ruling 6: slot choice
# ---------------------------------------------------------------------------------------
def _grid(now, length):
    """Every offered-grid slot (hour and half hour) inside business hours, from the first day
    24 hours out to the last day 30 days out, in time order. bookable() still checks each."""
    zones = _zones()
    day = zones.pacific_date(now + caps.MIN_NOTICE)
    last = zones.pacific_date(now + caps.MAX_HORIZON)
    while day <= last:
        if day.weekday() in caps.BUSINESS_DAYS and day not in caps.HOLIDAYS:
            closes = _dt.datetime.combine(day, caps.BUSINESS_END)
            for hour in range(24):
                for minute in sorted(caps.OFFERED_START_MINUTES):
                    local = _dt.datetime.combine(day, _dt.time(hour, minute))
                    if local.time() < caps.BUSINESS_START or local + length > closes:
                        continue
                    start = zones.to_utc(local, PACIFIC)
                    yield Slot(start, start + length)
        day += _DAY


def _start_of(candidate, what):
    """An instant from a Slot, an aware datetime, or ISO text with an offset ("...Z", as receipts
    and the ledger write times)."""
    if isinstance(candidate, Slot):
        candidate = candidate.start_utc
    if isinstance(candidate, str):
        try:
            candidate = _dt.datetime.fromisoformat(candidate.strip().replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("%s is not an ISO time: %r" % (what, candidate))
    return _aware(candidate, what)


def find_slots(events: Sequence[Event], now: _dt.datetime, length_min: int, count: int = 3,
               exclude_starts: Iterable = (), first: Iterable = (), steer_day: Optional[_dt.date] = None,
               steer_window: Optional[Tuple[_dt.time, _dt.time]] = None,
               steer_zone: Optional[str] = PACIFIC) -> List[Slot]:
    """Up to `count` slots of `length_min` minutes, each bookable and not busy, in offer order:

    1. the sender's own candidates (`first`: instants or Slots), in the order given, when
       available; a written time may sit on any quarter hour;
    2. with `steer_day`, slots on that day (inside `steer_window` when one is given);
    3. then the earliest available slots on the hour or half hour, at most one per Pacific day
       where possible (inside `steer_window` first), in time order.

    A start in `exclude_starts` (her earlier offers on the thread) is never offered again.
    `steer_day` and `steer_window` are read in `steer_zone` (the sender's zone, else Pacific).
    An empty list means no available slot in the next 30 days: a hold.
    """
    now = _aware(now, "now")
    if isinstance(length_min, bool) or not isinstance(length_min, int) or \
            not caps.MIN_LENGTH_MIN <= length_min <= caps.MAX_LENGTH_MIN:
        raise ValueError("length_min must be a whole number of minutes from %d to %d"
                         % (caps.MIN_LENGTH_MIN, caps.MAX_LENGTH_MIN))
    if count <= 0:
        return []
    events = list(events)
    length = _dt.timedelta(minutes=length_min)
    taken = {_start_of(s, "excluded start") for s in exclude_starts}
    zones = _zones()
    zone = steer_zone or PACIFIC

    chosen = []    # type: List[Slot]

    def take(slot):
        chosen.append(slot)
        taken.add(slot.start_utc)

    # 1. The sender's own candidates.
    for candidate in first:
        if len(chosen) >= count:
            break
        start = _start_of(candidate, "candidate start")
        slot = Slot(start, start + length)
        if start not in taken and available(events, slot, now, written=True)[0]:
            take(slot)

    if len(chosen) >= count:
        return chosen

    pool = [s for s in _grid(now, length)
            if s.start_utc not in taken and available(events, s, now, written=False)[0]]

    def in_window(slot):
        if steer_window is None:
            return True
        local_start = zones.to_local(slot.start_utc, zone)
        local_end = zones.to_local(slot.end_utc, zone)
        opens = _dt.datetime.combine(local_start.date(), steer_window[0])
        closes = _dt.datetime.combine(local_start.date(), steer_window[1])
        return opens <= local_start and local_end <= closes

    # 2. The day the sender named.
    if steer_day is not None:
        for slot in pool:
            if len(chosen) >= count:
                break
            if zones.to_local(slot.start_utc, zone).date() == steer_day and in_window(slot):
                take(slot)

    # 3. The earliest available, one per Pacific day where possible.
    used_days = {zones.pacific_date(s.start_utc) for s in chosen}
    inside = [s for s in pool if in_window(s)]
    outside = [s for s in pool if not in_window(s)]
    picked = []
    for group, new_day_only in ((inside, True), (outside, True), (inside, False), (outside, False)):
        for slot in group:
            if len(chosen) + len(picked) >= count:
                break
            if slot.start_utc in taken:
                continue
            day = zones.pacific_date(slot.start_utc)
            if new_day_only and day in used_days:
                continue
            picked.append(slot)
            taken.add(slot.start_utc)
            used_days.add(day)
    picked.sort(key=lambda s: s.start_utc)
    return chosen + picked
