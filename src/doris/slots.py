"""Where a slot can sit (G9), what counts as busy (ruling 3) and which slots she offers (ruling 6).

Times are aware UTC. Pacific exists only inside these functions, through zones.py. Every rule
returned is a word from why.RULE_WORDS: length, not_quarter_hour, outside_hours, holiday,
under_24h, over_30d, and overlap for a busy span.
"""

import datetime as _dt
from typing import Iterable, List, Optional, Sequence, Tuple, Union

from . import caps, zones
from .shapes import UTC, Event, Slot

_DAY = _dt.timedelta(days=1)
_NO_TIME = _dt.timedelta(0)
PACIFIC = "Pacific"


def _aware(value, what):
    if not isinstance(value, _dt.datetime) or value.tzinfo is None:
        raise ValueError("%s must be a timezone-aware datetime, got %r" % (what, value))
    return value.astimezone(UTC)


def _pacific_midnight(day):
    return zones.to_utc(_dt.datetime.combine(day, _dt.time(0)), PACIFIC)


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
def busy_window(ev: Event) -> Optional[Tuple[_dt.datetime, _dt.datetime]]:
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
            pacific_date = zones.pacific_date
            first_day = pacific_date(_aware(ev.start_utc, "event start"))
            last = ev.end_utc if ev.end_utc is not None else ev.start_utc
            end_day = pacific_date(_aware(last, "event end")) + _DAY
        if end_day is None or end_day <= first_day:
            end_day = first_day + _DAY
        return _pacific_midnight(first_day), _pacific_midnight(end_day)

    return None


_busy_window = busy_window      # the name the adapters still call


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
        window = busy_window(ev)
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


def _one_time(window, length):
    """The window holds one slot at most: it is the one time the sender asked for, not a part of day."""
    if window is None:
        return False
    opens = _dt.datetime.combine(_dt.date(2000, 1, 3), window[0])
    closes = _dt.datetime.combine(_dt.date(2000, 1, 3), window[1])
    if closes < opens:               # it runs past midnight
        closes += _DAY
    return closes - opens <= length


def find_slots(events: Sequence[Event], now: _dt.datetime, length_min: int, count: int = 3,
               exclude_starts: Iterable = (), first: Iterable = (),
               steer_day: Union[_dt.date, Iterable[_dt.date], None] = None,
               steer_window: Optional[Tuple[_dt.time, _dt.time]] = None,
               steer_zone: Optional[str] = PACIFIC, derived: Iterable = ()) -> List[Slot]:
    """Up to `count` slots of `length_min` minutes, each bookable and not busy, in offer order:

    1. the sender's own candidates (`first`: instants or Slots), in the order given, when
       available; a written time may sit on any quarter hour; then, only once one of those
       is offered, the `derived` candidates (D5 moving a time onto a weekday written apart
       from it), on the hour or half hour only: no time the sender wrote, so never first;
    2. the days the sender named (`steer_day`: one date, or several in the order to offer
       them): a slot on each before a second on any, inside `steer_window` (the part of the
       day they wrote) when one is given; a named day with nothing free inside it still gets
       its slot nearest to it;
    3. then the earliest available slots, at most one per Pacific day where possible: other
       days inside `steer_window`; what is left of the named days, nearest the window first;
       each other day's slot nearest the window; and last any slot at all. When `steer_window`
       is the one time they asked for (it holds one slot at most), the other days come nearest
       the named days first, not earliest first: that time on each, else each day's slot
       nearest it, before a second slot on a named day.

    Her own slots (2 and 3) start on the hour or half hour. A start in `exclude_starts` (her
    earlier offers on the thread) is never offered again. `steer_day` and `steer_window` are
    read in `steer_zone` (the sender's zone, else Pacific). The list is the sender's own times,
    then the named days' slots in time order, then the rest in time order. An empty list means
    no available slot in the next 30 days: a hold.
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
    if chosen:
        for candidate in derived:
            if len(chosen) >= count:
                break
            start = _start_of(candidate, "candidate start")
            slot = Slot(start, start + length)
            if start not in taken and available(events, slot, now, written=False)[0]:
                take(slot)

    if len(chosen) >= count:
        return chosen

    pool = [s for s in _grid(now, length)
            if s.start_utc not in taken and available(events, s, now, written=False)[0]]

    def distance_outside(slot):
        """How far the slot sits outside the part of the day the sender wrote: no time at all when
        it is inside it, or when they wrote none."""
        if steer_window is None:
            return _NO_TIME
        start = zones.to_local(slot.start_utc, zone)
        end = zones.to_local(slot.end_utc, zone)
        nearest = None
        for shift in (-1, 0, 1):         # that part of the day before, of this day, of the day after
            day = start.date() + shift * _DAY
            opens = _dt.datetime.combine(day, steer_window[0])
            closes = _dt.datetime.combine(day, steer_window[1])
            if closes < opens:           # it runs past midnight
                closes += _DAY
            distance = max(opens - start, end - closes, _NO_TIME)
            nearest = distance if nearest is None else min(nearest, distance)
        return nearest

    outside = {s.start_utc: distance_outside(s) for s in pool}

    def nearest_first(slot):
        return outside[slot.start_utc], slot.start_utc

    if steer_day is None:
        days = ()
    elif isinstance(steer_day, _dt.date):
        days = (steer_day,)
    else:
        days = tuple(dict.fromkeys(steer_day))      # each day once, in the order given
    on_day = {day: [] for day in days}
    for slot in pool:
        day = zones.to_local(slot.start_utc, zone).date()
        if day in on_day:
            on_day[day].append(slot)
    for day in days:
        on_day[day].sort(key=nearest_first)

    named = []      # type: List[Slot]    # her slots on the days the sender named
    others = []     # type: List[Slot]    # her slots on other days

    def room():
        return count - len(chosen) - len(named) - len(others)

    def pick(slot, group):
        group.append(slot)
        taken.add(slot.start_utc)

    def one_more_on_each_named_day(inside_only):
        """A round over the named days, in the order given. False when no day had a slot left."""
        any_taken = False
        for day in days:
            if room() <= 0:
                break
            for slot in on_day[day]:
                if slot.start_utc in taken:
                    continue
                if inside_only and outside[slot.start_utc]:
                    break                # nearest first: nothing after this one is inside either
                pick(slot, named)
                any_taken = True
                break
        return any_taken

    # 2. The days the sender named: inside the part of the day they wrote, a slot on each day
    #    before a second on any; then a day that got nothing takes its slot nearest that part.
    while room() > 0 and one_more_on_each_named_day(inside_only=True):
        pass
    for day in days:
        if room() <= 0:
            break
        if not any(zones.to_local(s.start_utc, zone).date() == day for s in chosen + named):
            for slot in on_day[day]:
                if slot.start_utc not in taken:
                    pick(slot, named)
                    break

    # 3. The earliest available, one per Pacific day where possible.
    used_days = {zones.pacific_date(s.start_utc) for s in chosen + named}

    def fill(in_order, new_day_only):
        for slot in in_order:
            if room() <= 0:
                break
            if slot.start_utc in taken:
                continue
            day = zones.pacific_date(slot.start_utc)
            if new_day_only and day in used_days:
                continue
            pick(slot, others)
            used_days.add(day)

    if days and _one_time(steer_window, length):
        # The one time they asked for (ruling 6): that time on the days nearest the day they named,
        # then each of those days' slot nearest it, before a second slot on the day itself.
        def days_away(slot):
            day = zones.to_local(slot.start_utc, zone).date()
            return min(abs((day - named_day).days) for named_day in days)

        fill(sorted((s for s in pool if not outside[s.start_utc]), key=lambda s: (days_away(s), s.start_utc)),
             new_day_only=True)
        fill(sorted(pool, key=lambda s: (days_away(s), zones.pacific_date(s.start_utc)) + nearest_first(s)),
             new_day_only=True)
    else:
        fill([s for s in pool if not outside[s.start_utc]], new_day_only=True)  # other days, inside the window
    while room() > 0 and one_more_on_each_named_day(inside_only=False):     # what is left of the named days
        pass
    fill(sorted(pool, key=lambda s: (zones.pacific_date(s.start_utc),) + nearest_first(s)),
         new_day_only=True)                                                 # each other day's nearest slot
    fill(sorted(pool, key=nearest_first), new_day_only=False)               # any day at all
    named.sort(key=lambda s: s.start_utc)
    others.sort(key=lambda s: s.start_utc)
    return chosen + named + others
