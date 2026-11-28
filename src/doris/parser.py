"""The plain date parser (ruling 2), the length reader (ruling 5), the pick matcher (ruling 6)
and the model check (B11).

Three layers. Understanding: every common way a person writes a date, a time and a zone gets
its readings. Booking: a direct booking only when the sender's own words leave exactly one
reading and it is bookable (B1-B9 here, B11 in model_agrees; the gate checks the booking
again). Anything this module cannot read cleanly is an offer naming the first failed
condition. A wrong parse is a wrong booking, so when in doubt the verdict is "offer". The
offer's steer: which days, which part of day and which zone an offer looks at first (see
_frame_days and _steer). It orders an offer and never reaches a booking condition.

Pure: `now` and `sent_utc` come from the caller; nothing reads the wall clock. The bookable
check (G9, slots.bookable) is imported inside the function that needs it, or passed in as
`bookable`.
"""

import bisect
import datetime as _dt
import hashlib
import re
import unicodedata
from typing import Callable, Dict, List, Optional, Tuple

from . import caps, zones
from .shapes import UTC, ModelAnswer, ParseResult, PickResult, Reading, Slot

CONDITIONS = ("B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8", "B9")

# ---------------------------------------------------------------------------------------
# N1-N3: folding to plain ASCII
# ---------------------------------------------------------------------------------------
SUB = "\x1a"          # stands in for a character that cannot be folded (row N2)
BREAK = "\x1e"        # a blank line: ends a sentence (row N5)
# Direction controls (row N3), written as escapes so none sits raw in this file: LRM, RLM, ALM,
# the embeddings and overrides U+202A to U+202E, the isolates U+2066 to U+2069.
_DIRECTION = set("\u200e\u200f\u061c\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
_MAP = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'", "´": "'", "`": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"', "«": '"', "»": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-", "−": "-",
    "…": "...",
}


def fold(text) -> Tuple[str, bool, bool]:
    """Fold to plain ASCII (N1). Returns (text, direction_controls_seen, unfoldable_glued_to_a_token).

    A character that cannot be folded becomes SUB, so the token it sits in no longer reads (N2).
    """
    if not isinstance(text, str):
        return "", False, False
    direction = any(c in _DIRECTION for c in text)
    out = []
    for c in text:
        if c in _DIRECTION:
            out.append(SUB)
        else:
            out.append(_MAP.get(c, c))
    s = "".join(out)
    if s.startswith("\ufeff"):       # a byte order mark
        s = s[1:]
    try:
        s = unicodedata.normalize("NFKC", s)
        s = unicodedata.normalize("NFKD", s)
    except Exception:
        pass
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    s = "".join(c if (c == "\n" or 32 <= ord(c) < 127) else SUB for c in s)
    glued = re.search(r"[A-Za-z0-9]\x1a|\x1a[A-Za-z0-9]", s) is not None
    return s, direction, glued


def _flatten(s):
    """N5: a blank line ends a sentence; a single line break is a space."""
    s = re.sub(r"\n[ \t]*\n\s*", " " + BREAK + " ", s)
    return s.replace("\n", " ")


_NO_END_BEFORE_DOT = re.compile(
    r"(?:\b(?:[ap]\.m|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec|mon|tue|tues|wed|weds|thu|thur|thurs|"
    r"fri|sat|sun|mr|mrs|ms|dr|st|vs|approx|est|e\.g|i\.e|ca)|\b[a-z])$")


def _sentence_ends(lt):
    ends = []
    for m in re.finditer(r"[.?!]+|\x1e", lt):
        tok = m.group()
        if tok == ".":
            nxt = lt[m.end():m.end() + 1]
            if nxt and not nxt.isspace() and nxt != BREAK:
                continue
            if _NO_END_BEFORE_DOT.search(lt[max(0, m.start() - 6):m.start()]):
                continue
        ends.append(m.start())
    return ends


# ---------------------------------------------------------------------------------------
# Word lists
# ---------------------------------------------------------------------------------------
NUMWORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
            "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
NUMWORD = r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
MINWORDS = {"oh five": 5, "ten": 10, "fifteen": 15, "twenty five": 25, "twenty": 20, "thirty five": 35,
            "thirty": 30, "forty five": 45, "forty": 40, "fifty five": 55, "fifty": 50}
MINWORD = r"(?:oh[\s-]five|fifteen|twenty[\s-]five|twenty|thirty[\s-]five|thirty|forty[\s-]five|forty|fifty[\s-]five|fifty)"
MONTHS = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7, "august": 8,
          "september": 9, "october": 10, "november": 11, "december": 12, "jan": 1, "feb": 2, "mar": 3,
          "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12}
MON = (r"(january|february|march|april|may|june|july|august|september|october|november|december|"
       r"jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec)(?![a-z])\.?")
WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
            "mon": 0, "tue": 1, "tues": 1, "wed": 2, "weds": 2, "thu": 3, "thur": 3, "thurs": 3, "fri": 4,
            "sat": 5, "sun": 6}
WD = (r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday|tues|weds|thurs|thur|"
      r"mon|tue|wed|thu|fri|sat|sun)(?![a-z])\.?")
_ORD = ["first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth",
        "eleventh", "twelfth", "thirteenth", "fourteenth", "fifteenth", "sixteenth", "seventeenth",
        "eighteenth", "nineteenth", "twentieth"]
ORDWORDS = {w: i + 1 for i, w in enumerate(_ORD)}
for _i, _w in enumerate(_ORD[:9]):
    ORDWORDS["twenty " + _w] = 21 + _i
ORDWORDS["thirtieth"] = 30
ORDWORDS["thirty first"] = 31
DAYWORD = "(" + "|".join(sorted((w.replace(" ", r"[\s-]") for w in ORDWORDS), key=len, reverse=True)) + ")"
DAYN = r"(\d{1,2})(?:st|nd|rd|th)?"

VAGUE = [  # (pattern, window or None) in the zone the sender wrote, else Pacific
    (r"first thing", ((9, 0), (10, 0))), (r"mid[\s-]?morning", ((10, 0), (11, 30))),
    (r"late morning", ((10, 30), (12, 0))), (r"early afternoon", ((12, 0), (14, 0))),
    (r"late afternoon", ((15, 0), (17, 0))), (r"after lunch", ((13, 0), (15, 0))),
    (r"lunch\s?time", ((12, 0), (13, 0))), (r"end of (?:the )?day", ((16, 0), (17, 0))),
    (r"close of business", ((16, 0), (17, 0))), (r"eod", ((16, 0), (17, 0))), (r"cob", ((16, 0), (17, 0))),
    (r"morning", ((9, 0), (12, 0))), (r"afternoon", ((12, 0), (17, 0))), (r"evening", None),
    (r"any\s?time", None),
]

US_ABBRS = ("pt", "pst", "pdt", "mt", "mst", "mdt", "ct", "cst", "cdt", "et", "est", "edt")
_REGION = {"pacific": "Pacific", "mountain": "Mountain", "central": "Central", "eastern": "Eastern"}
_REGION_ABBR = {"Pacific": "P", "Mountain": "M", "Central": "C", "Eastern": "E"}
UNKNOWN_ABBRS = ("cet", "cest", "bst", "ist", "aest", "aedt", "acst", "acdt", "awst", "jst", "kst", "hkt",
                 "sgt", "wet", "west", "eet", "eest", "msk", "nzst", "nzdt", "hst", "akst", "akdt", "sast",
                 "wat", "cat", "eat", "brt", "art", "pkt", "ict", "wib", "gst", "pht")
_Z3_FOLLOW = re.compile(
    r"\s*(?:time\b|standard\b|daylight\b|[.,;:!?()\[\]\x1e-]|$|(?:for|on|at|if|and|or|so|as|that|this|then|"
    r"with|works|work|would|will|could|can|is|was|please|instead|to|thanks|about|regarding|re(?!-)|concerning)\b)")

NEGATION = re.compile(
    r"\b(?:\w+n't|cant|dont|wont|didnt|doesnt|isnt|arent|wasnt|werent|couldnt|wouldnt|shouldnt|havent|hasnt|"
    r"hadnt|aint|mustnt|neednt|not|no|never|cannot|unable|except|unavailable|busy|booked|conflict|conflicts|"
    r"clash|clashes|impossible|other than|aside from|apart from|ruled out)\b")
NEG_BUT = re.compile(r"\b(?:anything|any day|any time|anytime|all)\s+but\b")
NOT_MEETING = re.compile(
    r"\b(?:ends|ending|expires|deadline|deadlines|due|until|hard stop|starts|webinar|flight|leaves|departs|"
    r"lands|closes|opens|launch)\b")
HEDGE_BEFORE = re.compile(
    r"\b(?:around|(?<!\bhow )(?<!\bwhat )about|approximately|approx\.?|roughly|maybe|perhaps|probably|say|circa)\s*,?\s*"
    r"(?:(?:at|on|the|@)\s*)*$")
BOUND_BEFORE = re.compile(
    r"\b(?:after|before|by|no later than|no earlier than|not later than|not before|not after|until|till|"
    r"up to|from|starting|beginning|as of|since)\s*(?:(?:at|on|the|@)\s*)*$")
BOUND_AFTER = re.compile(r"^\s*,?\s*(?:or later|or after|or earlier|or before|onwards?|at the earliest|at the latest)\b")
HEDGE_AFTER = re.compile(r"^(?:\s*-?\s?ish\b|\s*,?\s*or so\b)")
RANGE_AFTER = re.compile(r"^\s*(?:-|to|until|till|thru|through)\s*(?:\d|noon|midday|" + NUMWORD + r"\b)")
RANGE_BEFORE = re.compile(
    r"(?:\d{1,2}(?::\d{2})?\s*(?:[ap]\.?\s?m\.?)?|noon|midday|" + NUMWORD + r")\s*(?:-|to|until|till|thru|through)\s*$")
_GAP_OK = re.compile(r"^[\s,\-()\[\]]*(?:(?:at|@|on|the)[\s,\-()\[\]]*)*$")


class _M(object):
    """A mention found in the text (date, time, zone or range)."""

    def __init__(self, **kw):
        self.__dict__.update(kw)

    def __repr__(self):
        return "<%s>" % ", ".join("%s=%r" % kv for kv in sorted(self.__dict__.items()) if kv[0] != "lt")


# ---------------------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------------------
class _Text(object):
    def __init__(self, t, sent_date):
        self.t = t
        self.lt = t.lower()
        self.sent_date = sent_date
        self.taken = [False] * len(t)
        self.ends = _sentence_ends(self.lt)
        self.dates = []
        self.times = []
        self.vague = []
        self.zones_free = []
        self.ranges = []
        self.either = []
        self.q2 = []
        self.flags = {}             # sentence -> (negated, not the meeting), see _sentence_flags
        self.pick_mode = False      # ruling 6: "at 2" is read whatever follows it

    def free(self, a, b):
        return a < b and not any(self.taken[a:b])

    def take(self, a, b):
        for i in range(a, min(b, len(self.taken))):
            self.taken[i] = True

    def sentence(self, pos):
        return bisect.bisect_left(self.ends, pos)


def _next_date(month, day, ref):
    for y in (ref.year, ref.year + 1):
        try:
            d = _dt.date(y, month, day)
        except ValueError:
            continue
        if d >= ref:
            return d
    return None


def _weekday_ok_short(T, m_start, word):
    """Three-letter weekday forms count when capitalized, dotted, or next to a date or time (D4)."""
    if len(word.rstrip(".")) > 3 or word.rstrip(".") in ("tues", "weds", "thur", "thurs"):
        return True
    if T.t[m_start:m_start + 1].isupper() or word.endswith("."):
        return True
    return False


def _zone_at(T, pos):
    """The zone written right after a time (rows Z1-Z9, Z12), or None."""
    lt = T.lt
    m = re.match(r"(?:\s*-?\s?ish\b)?(\s*(?:,|\()?\s*)", lt[pos:])
    opened = "(" in m.group(1)
    p = pos + m.end()
    rest = lt[p:]
    z = None
    mm = re.match(r"(utc|gmt)\s?([+-])(\d{1,2})(?::?(\d{2}))?(?![\w:])", rest)
    if mm:
        h, mi = int(mm.group(3)), int(mm.group(4) or 0)
        zid = None
        if h <= 14 and mi < 60:
            total = (h * 60 + mi) * (1 if mm.group(2) == "+" else -1)
            zid = zones.offset_zone(total)
        z = _M(start=p, end=p + mm.end(), zid=zid, kind="offset", abbr=None, unknown=zid is None,
               your_time=False, us=False, utc_like=True, gmt=False)
    if z is None:
        mm = re.match(r"(central|eastern|western)\s+european(?:\s+(?:summer|standard))?\s+time\b", rest)
        if mm:
            z = _M(start=p, end=p + mm.end(), zid=None, kind="unknown", abbr=None, unknown=True,
                   your_time=False, us=False, utc_like=False, gmt=False)
    if z is None:
        mm = re.match(r"(pacific|mountain|central|eastern)\s+(?:(standard|daylight)\s+)?time\b(?:\s+zone\b)?", rest)
        if mm:
            zid = _REGION[mm.group(1)]
            abbr = None
            if mm.group(2):
                abbr = _REGION_ABBR[zid] + ("ST" if mm.group(2) == "standard" else "DT")
            z = _M(start=p, end=p + mm.end(), zid=zid, kind="spelled", abbr=abbr, unknown=False,
                   your_time=False, us=True, utc_like=False, gmt=False)
    if z is None:
        mm = re.match(r"(pacific|mountain|central|eastern)\b", rest)
        if mm and _Z3_FOLLOW.match(rest[mm.end():]) or (mm and _date_starts(T, p + mm.end())):
            z = _M(start=p, end=p + mm.end(), zid=_REGION[mm.group(1)], kind="region", abbr=None,
                   unknown=False, your_time=False, us=True, utc_like=False, gmt=False)
    if z is None:
        mm = re.match(r"(" + "|".join(US_ABBRS) + r")(?![\w])\.?", rest)
        if mm:
            ab = mm.group(1).upper()
            z = _M(start=p, end=p + mm.end(), zid=zones.abbr_zone(ab), kind="abbr",
                   abbr=ab if len(ab) == 3 else None, unknown=False, your_time=False, us=True,
                   utc_like=False, gmt=False)
    if z is None:
        mm = re.match(r"(utc|gmt|z)(?![\w+-])", rest)
        if mm:
            z = _M(start=p, end=p + mm.end(), zid="UTC", kind="utc", abbr=None, unknown=False,
                   your_time=False, us=False, utc_like=True, gmt=mm.group(1) == "gmt")
    if z is None:
        mm = re.match(r"(?:your\s+time(?:\s*zone)?|shane'?s\s+time(?:\s*zone)?)\b", rest)
        if mm:
            z = _M(start=p, end=p + mm.end(), zid="Pacific", kind="your_time", abbr=None, unknown=False,
                   your_time=True, us=False, utc_like=False, gmt=False)
    if z is None:
        mm = re.match(r"(?:" + "|".join(UNKNOWN_ABBRS) + r")(?![\w])|my\s+time\b|(?:[a-z]+\s+){1,2}time\b", rest)
        if mm:
            z = _M(start=p, end=p + mm.end(), zid=None, kind="unknown", abbr=None, unknown=True,
                   your_time=False, us=False, utc_like=False, gmt=False)
    if z is None:
        return None
    if opened:
        mc = re.match(r"\s*\)", lt[z.end:])
        if mc:
            z.end += mc.end()
    return z


def _date_starts(T, pos):
    m = re.match(r"\s*,?\s*(?:on\s+)?", T.lt[pos:])
    p = pos + m.end()
    return any(d.start == p for d in T.dates)


def _year_ok(T, y):
    return y in (T.sent_date.year, T.sent_date.year + 1)


def _extract_dates(T):
    lt = T.lt
    # L6 "the first week of December"
    for m in re.finditer(r"\b(?:the\s+)?(first|second|third|fourth|last)\s+week\s+of\s+" + MON, lt):
        if not T.free(m.start(), m.end()):
            continue
        mo = MONTHS[m.group(2)]
        first = _next_date(mo, 1, T.sent_date.replace(day=1)) or _dt.date(T.sent_date.year, mo, 1)
        n = {"first": 0, "second": 1, "third": 2, "fourth": 3, "last": 3}[m.group(1)]
        T.dates.append(_M(start=m.start(), end=m.end(), kind="relative", form="L6",
                          cands=[first + _dt.timedelta(days=7 * n)], weekday=None, sep=None, books=False))
        T.take(m.start(), m.end())
    # D10/D11 ISO
    iso = re.compile(r"(?<![\w.$/-])(\d{4})([-/.])(\d{1,2})\2(\d{1,2})"
                     r"(?:t(\d{1,2}):(\d{2})(?::(\d{2}))?(z|[+-]\d{2}:?\d{2})?)?(?![\w/]|[.-]\d)")
    for m in iso.finditer(lt):
        if not T.free(m.start(), m.end()):
            continue
        y, mo, d = int(m.group(1)), int(m.group(3)), int(m.group(4))
        try:
            cands = [_dt.date(y, mo, d)]
        except ValueError:
            cands = []
        date_end = m.start(5) - 1 if m.group(5) else m.end()
        dm = _M(start=m.start(), end=date_end, kind="abs", form="D10", cands=cands, weekday=None, sep="iso",
                books=True)
        _attach_weekday(T, dm)
        T.dates.append(dm)
        T.take(dm.start, m.end())
        if m.group(5):
            h, mi, sec = int(m.group(5)), int(m.group(6)), m.group(7)
            bad = h > 23 or mi > 59 or (sec is not None and sec != "00")
            zone = None
            tz = m.group(8)
            if tz:
                if tz == "z":
                    zid = "UTC"
                else:
                    hh, mm2 = int(tz[1:3]), int(tz[-2:])
                    zid = None
                    if hh <= 14 and mm2 < 60:
                        zid = zones.offset_zone((hh * 60 + mm2) * (1 if tz[0] == "+" else -1))
                zone = _M(start=m.start(8), end=m.end(), zid=zid, kind="iso", abbr=None, unknown=zid is None,
                          your_time=False, us=False, utc_like=True, gmt=False)
            tm = _M(start=date_end, end=m.end(8) if tz else m.end(), clocks=[] if bad else [(h, mi)],
                    two=False, form="D11", never=False, impossible=bad, zone=zone)
            if zone is None:
                tm.zone = _zone_at(T, tm.end)
                if tm.zone:
                    T.take(tm.zone.start, tm.zone.end)
            T.times.append(tm)
    # D1-D9: written months
    pats = [
        re.compile(r"(?<![\w$])(?:" + WD + r",?\s+)?\b" + MON + r"\s*(?:the\s+)?(?:" + DAYN + r"|" + DAYWORD +
                   r")\b(?:,?[\s-]+(\d{4})\b)?"),
        re.compile(r"(?<![\w$:./])(?:" + WD + r",?\s+)?(?:the\s+)?(?:" + DAYN + r"|" + DAYWORD + r")(?:\s+of)?[\s-]*"
                   + MON + r"(?:[\s,-]+(\d{4})\b)?"),
    ]
    found = []
    for k, pat in enumerate(pats):
        for m in pat.finditer(lt):
            found.append((m.start(), -(m.end() - m.start()), k, m))
    found.sort(key=lambda x: (x[0], x[1]))
    for _, _, k, m in found:
        if not T.free(m.start(), m.end()):
            continue
        g = m.groups()
        wd = g[0]
        if k == 0:
            mon, dnum, dword, year = g[1], g[2], g[3], g[4]
        else:
            dnum, dword, mon, year = g[1], g[2], g[3], g[4]
        start = m.start()
        if wd and not _weekday_ok_short(T, start, wd):
            start = m.start(2)
            wd = None
        end = m.end()
        y = None
        if year:
            yy = int(year)
            ys = m.start(5)
            if _year_ok(T, yy):
                y = yy
            elif _four_digit_time_placed(T, m.end()):
                end = len(lt[:ys].rstrip(" ,-"))
            elif 1900 <= yy <= 2099:
                y = yy
            else:
                end = len(lt[:ys].rstrip(" ,-"))
        day = int(dnum) if dnum else ORDWORDS.get(re.sub(r"[\s-]+", " ", dword or ""), 0)
        month = MONTHS[mon.rstrip(".")]
        cands = []
        if y is not None:
            try:
                cands = [_dt.date(y, month, day)]
            except ValueError:
                cands = []
        else:
            d0 = _next_date(month, day, T.sent_date) if 1 <= day <= 31 else None
            cands = [d0] if d0 else []
        dm = _M(start=start, end=end, kind="abs", form="D1" if k == 0 else "D2", cands=cands,
                weekday=WEEKDAYS[wd.rstrip(".")] if wd else None, sep=None, books=True, month=month)
        if not wd:
            _attach_weekday(T, dm)
        if re.search(r"\bweek\s+of\s+(?:the\s+)?$", lt[:dm.start]):
            mw = re.search(r"\b(?:the\s+)?week\s+of\s+(?:the\s+)?$", lt[:dm.start])
            dm.start, dm.kind, dm.form, dm.books = mw.start(), "relative", "L6", False
        T.dates.append(dm)
        T.take(dm.start, dm.end)
        # Q2: "December 3 and 4", "Dec 3 or 4"
        mq = re.match(r"\s*(?:or|and|&)\s+(\d{1,2})(?:st|nd|rd|th)?\b(?!\s*(?::|[ap]\.?\s?m\b|o'?\s?clock))",
                      lt[dm.end:])
        if mq and dm.kind == "abs":
            d2 = _next_date(month, int(mq.group(1)), T.sent_date) if 1 <= int(mq.group(1)) <= 31 else None
            s2 = dm.end + mq.start(1)
            T.dates.append(_M(start=s2, end=dm.end + mq.end(), kind="abs", form="Q2", cands=[d2] if d2 else [],
                              weekday=None, sep=None, books=True))
            T.q2.append(dm.end)
            T.take(dm.end, dm.end + mq.end())
    # D12-D23 numeric
    num = re.compile(r"(?<![\w$.,/:-])(\d{1,2})([/.-])(\d{1,2})(?:\2(\d{4}|\d{2}))?(?![\w/:]|[.,-]\d)")
    for m in num.finditer(lt):
        if not T.free(m.start(), m.end()):
            continue
        a, sep, b, year = int(m.group(1)), m.group(2), int(m.group(3)), m.group(4)
        if sep == "/" and not year and (a, b) == (24, 7):
            T.take(m.start(), m.end())
            continue
        if (a > 31 or b > 31) and a > 12 and b > 12:
            T.take(m.start(), m.end())       # D23 look-alike: no real date either way
            continue
        if sep == "-" and not year:
            if (a, b) != (1, 1):
                T.ranges.append(_M(start=m.start(), end=m.end(), a=a, b=b))
            T.take(m.start(), m.end())
            continue
        if sep == "." and not year and not _dotted_in_context(T, m.start(), m.end()):
            T.take(m.start(), m.end())       # a plain number: blocks nothing
            continue
        y = None
        if year:
            if len(year) == 2:
                y = 2000 + int(year) if sep == "/" else -1
            else:
                y = int(year)
            if y < 0 or not _year_ok(T, y):
                cands = []
                y = -1
        cands = []
        if y != -1:
            pairs = [(a, b)] if a == b else [(a, b), (b, a)]   # month first, then day first
            for mo, d in pairs:
                if not (1 <= mo <= 12 and 1 <= d <= 31):
                    continue
                if y is not None:
                    try:
                        cands.append(_dt.date(y, mo, d))
                    except ValueError:
                        pass
                else:
                    dd = _next_date(mo, d, T.sent_date)
                    if dd:
                        cands.append(dd)
        dm = _M(start=m.start(), end=m.end(), kind="dotted" if (sep == "." and not year) else "numeric",
                form="D16" if (sep == "." and not year) else ("D18" if year else "D12"), cands=cands,
                weekday=None, sep=sep, books=not (sep == "." and not year), a=a, b=b, zone=None)
        _attach_weekday(T, dm)
        if dm.kind == "dotted":
            dm.zone = _zone_at(T, dm.end)
        T.dates.append(dm)
        T.take(dm.start, dm.end)
        if dm.kind == "dotted" and dm.zone:
            T.take(dm.zone.start, dm.zone.end)


def _attach_weekday(T, dm):
    mw = re.search(r"(?<![\w])" + WD + r",?\s*$", T.lt[:dm.start])
    if mw and T.free(mw.start(), dm.start):
        dm.weekday = WEEKDAYS[mw.group(1)]
        dm.start = mw.start()
    else:
        mb = re.match(r"\s*\(\s*" + WD + r"\s*\)", T.lt[dm.end:])
        if mb:
            dm.weekday = WEEKDAYS[mb.group(1)]
            dm.end += mb.end()


def _dotted_in_context(T, a, b):
    before = T.lt[:a]
    after = T.lt[b:]
    if re.search(r"(?:\bon|\bat|@|" + WD + r")\s*$", before):
        return True
    if re.match(r"\s*(?:at\b|@|on\b|\d{1,2}(?::\d{2})?\s*[ap]\.?\s?m\b)", after):
        return True
    return False


def _four_digit_time_placed(T, pos):
    """Four digits read as a time only right before a zone or "hours" (H9)."""
    rest = T.lt[pos:]
    if re.match(r"\s?(?:hours|hrs|h)\b|z\b", rest):
        return True
    return _zone_at(T, pos) is not None and not _zone_at(T, pos).unknown


def _after_ok(T, pos):
    """H12/H9: a zone, a date, "on", "sharp", a day-part or the end of the sentence after it."""
    rest = T.lt[pos:]
    if re.match(r"\s*(?:[.?!\x1e]|$|on\b|sharp\b|in the (?:morning|afternoon|evening)\b|at night\b)", rest):
        return True
    z = _zone_at(T, pos)
    if z is not None and not (z.kind == "unknown" and not re.match(r"\s*(?:" + "|".join(UNKNOWN_ABBRS) + r")\b", rest)):
        return True
    if _date_starts(T, pos):
        return True
    return False


def _clock(h, m, ap):
    """Hour and minutes with am or pm -> (clocks, impossible, never)."""
    if m > 59 or h > 12 or h == 0:
        return [], True, False
    if ap == "a":
        if h == 12:
            return [(12, m), (0, m)], False, True      # H5: 12am never books; noon first
        return [(h, m)], False, False
    return [(12 if h == 12 else h + 12, m)], False, False


def _add_time(T, start, end, clocks, two, form, never=False, impossible=False, zone_after=True, zone=None):
    if not T.free(start, end):
        return None
    tm = _M(start=start, end=end, clocks=clocks, two=two, form=form, never=never, impossible=impossible, zone=zone)
    T.take(start, end)
    if zone_after and zone is None:
        z = _zone_at(T, end)
        if z is not None and T.free(z.start, z.end):
            tm.zone = z
            T.take(z.start, z.end)
    T.times.append(tm)
    return tm


def _two(h, m):
    """Two readings twelve hours apart for an hour 1-12 with no am or pm (H11, H12, H15, H17, H18)."""
    if h == 12:
        return [(12, m), (0, m)]
    return [(h, m), (h + 12, m)]


def _apply_marker(T, end, h, m):
    """am/pm or a day-part right after a time -> (clocks, new_end) or None."""
    rest = T.lt[end:]
    ma = re.match(r"\s?([ap])\.?\s?m\b\.?", rest)
    if ma:
        clocks, imp, nev = _clock(h, m, ma.group(1))
        return clocks, end + ma.end(), imp, nev
    md = re.match(r"\s+(in the morning|in the afternoon|in the evening|at night)\b", rest)
    if md:
        if h > 12 or h == 0:
            return [], end + md.end(), True, False
        if "morning" in md.group(1):
            return [(0 if h == 12 else h, m)], end + md.end(), False, False
        return [(12 if h == 12 else h + 12, m)], end + md.end(), False, False
    return None


def _extract_times(T):
    lt = T.lt
    # H1, H4, H5, H20, H22: digits with am or pm
    for m in re.finditer(r"(?<![\w:.$/])(\d{1,2})(?:([:.])(\d{2}))?(?::(\d{2}))?\s?([ap])\.?\s?m\b\.?", lt):
        if not T.free(m.start(), m.end()):
            continue
        if m.group(4) and m.group(4) != "00":
            T.take(m.start(), m.end())          # H20: seconds other than 00 are not read
            continue
        h, mi = int(m.group(1)), int(m.group(3) or 0)
        clocks, imp, nev = _clock(h, mi, m.group(5))
        _add_time(T, m.start(), m.end(), clocks, False, "H1", never=nev, impossible=imp)
    # H14: number words with am or pm
    for m in re.finditer(r"\b(" + NUMWORD + r")(?:[\s-]+(" + MINWORD + r"))?\s+([ap])\.?\s?m\b\.?", lt):
        h = NUMWORDS[m.group(1)]
        mi = MINWORDS.get(re.sub(r"[\s-]+", " ", m.group(2) or ""), 0)
        clocks, imp, nev = _clock(h, mi, m.group(3))
        _add_time(T, m.start(), m.end(), clocks, False, "H14", never=nev, impossible=imp)
    # H16: day-part after the time
    for m in re.finditer(r"(?<![\w:.$/])(?:(\d{1,2})(?::(\d{2}))?|(" + NUMWORD + r")(?:[\s-]+(" + MINWORD + r"))?)"
                         r"\s+(in the morning|in the afternoon|in the evening|at night)\b", lt):
        h = int(m.group(1)) if m.group(1) else NUMWORDS[m.group(3)]
        mi = int(m.group(2)) if m.group(2) else MINWORDS.get(re.sub(r"[\s-]+", " ", m.group(4) or ""), 0)
        r = _apply_marker(T, m.start(5) - 1, h, mi)
        if r is None:
            continue
        clocks, _, imp, nev = r
        _add_time(T, m.start(), m.end(), clocks, False, "H16", never=nev, impossible=imp or mi > 59)
    # H18: past and to
    for m in re.finditer(r"\b(half|(?:a\s+)?quarter|five|ten|twenty[\s-]five|twenty|(\d{1,2})\s+minutes?)\s+"
                         r"(past|after|to|of|till|til|before)\s+(\d{1,2}|" + NUMWORD + r")(?![\w:])", lt):
        word, rel, hw = m.group(1), m.group(3), m.group(4)
        h = int(hw) if hw.isdigit() else NUMWORDS[hw]
        if m.group(2):
            mins = int(m.group(2))
        else:
            w = re.sub(r"[\s-]+", " ", word)
            mins = {"half": 30, "quarter": 15, "a quarter": 15, "five": 5, "ten": 10, "twenty": 20,
                    "twenty five": 25}[w]
        if word == "half" and rel not in ("past", "after"):
            continue
        if not (1 <= h <= 12) or mins >= 60:
            continue
        if rel in ("past", "after"):
            hh, mi = h, mins
        else:
            hh, mi = (h - 1 if h > 1 else 12), 60 - mins
        r = _apply_marker(T, m.end(), hh, mi)
        if r:
            clocks, end, imp, nev = r
            _add_time(T, m.start(), end, clocks, False, "H18", never=nev, impossible=imp)
        else:
            _add_time(T, m.start(), m.end(), _two(hh, mi), True, "H18")
    # H15: o'clock
    for m in re.finditer(r"(?<![\w:.$/])(\d{1,2}|" + NUMWORD + r")\s*o'?\s?clock\b", lt):
        hw = m.group(1)
        h = int(hw) if hw.isdigit() else NUMWORDS[hw]
        r = _apply_marker(T, m.end(), h, 0)
        if r:
            clocks, end, imp, nev = r
            _add_time(T, m.start(), end, clocks, False, "H15", never=nev, impossible=imp)
        elif 1 <= h <= 12:
            _add_time(T, m.start(), m.end(), _two(h, 0), True, "H15")
        elif 13 <= h <= 23:
            _add_time(T, m.start(), m.end(), [(h, 0)], False, "H15")
        else:
            _add_time(T, m.start(), m.end(), [], False, "H15", impossible=True)
    # H17: number words with minutes
    for m in re.finditer(r"\b(" + NUMWORD + r")[\s-]+(" + MINWORD + r")\b", lt):
        h = NUMWORDS[m.group(1)]
        mi = MINWORDS[re.sub(r"[\s-]+", " ", m.group(2))]
        _add_time(T, m.start(), m.end(), _two(h, mi), True, "H17")
    # H6 noon, H7 midnight
    for m in re.finditer(r"\b(?:12\s+noon|12\s+midday|noon|midday|mid-day)\b", lt):
        _add_time(T, m.start(), m.end(), [(12, 0)], False, "H6")
    for m in re.finditer(r"\b(?:12\s+midnight|midnight)\b", lt):
        _add_time(T, m.start(), m.end(), [(0, 0), (24, 0)], False, "H7", never=True)
    # H3: short a or p, only where a time is expected
    for m in re.finditer(r"(?<![\w:.$/])(\d{1,2})(?::(\d{2}))?([ap])(?![\w])", lt):
        if not T.free(m.start(), m.end()):
            continue
        at_before = re.search(r"(?:\bat|@)\s*$", lt[:m.start()]) is not None
        z = _zone_at(T, m.end())
        if not (at_before or (z is not None and not z.unknown)):
            continue
        clocks, imp, nev = _clock(int(m.group(1)), int(m.group(2) or 0), m.group(3))
        _add_time(T, m.start(), m.end(), clocks, False, "H3", never=nev, impossible=imp)
    # H8, H10, H11, H20, H22: colon times
    for m in re.finditer(r"(?<![\w:.$/])(\d{1,2}):(\d{2})(?::(\d{2}))?(?![\d:])", lt):
        if not T.free(m.start(), m.end()):
            continue
        hs, h, mi, sec = m.group(1), int(m.group(1)), int(m.group(2)), m.group(3)
        end = m.end()
        if sec is not None and sec != "00":
            T.take(m.start(), end)
            continue
        zone = None
        if re.match(r"z(?![\w])", lt[end:]):
            zone = _M(start=end, end=end + 1, zid="UTC", kind="utc", abbr=None, unknown=False, your_time=False,
                      us=False, utc_like=True, gmt=False)
            end += 1
        if h > 23 or mi > 59:
            _add_time(T, m.start(), end, [], False, "H22", impossible=True, zone=zone)
        elif h >= 13 or h == 0 or (len(hs) == 2 and hs[0] == "0"):
            _add_time(T, m.start(), end, [(h, mi)], False, "H8", zone=zone)
        else:
            _add_time(T, m.start(), end, _two(h, mi), True, "H11", zone=zone)
    # H9, H10: four digits
    for m in re.finditer(r"(?<![\w:.$/-])(\d{2})(\d{2})(\s?(?:hours|hrs|h)\b)?(z(?![\w]))?(?![\w:/]|\.\d)", lt):
        if not T.free(m.start(), m.end()):
            continue
        h, mi = int(m.group(1)), int(m.group(2))
        if h > 23 or mi > 59:
            continue
        glued = bool(m.group(3)) or bool(m.group(4))
        at_before = re.search(r"(?:\bat|@)\s*$", lt[:m.start()]) is not None
        placed = glued or (_zone_at(T, m.end()) is not None and not _zone_at(T, m.end()).unknown) or \
            (at_before and _after_ok(T, m.end()))
        if not placed:
            continue
        zone = None
        if m.group(4):
            zone = _M(start=m.start(4), end=m.end(4), zid="UTC", kind="utc", abbr=None, unknown=False,
                      your_time=False, us=False, utc_like=True, gmt=False)
        _add_time(T, m.start(), m.end(), [(h, mi)], False, "H9", zone=zone)
    # H12, H13: a bare hour after "at" or "@"
    for m in re.finditer(r"(?:\bat|@)\s*(?:(?:around|about|approximately|roughly|maybe|perhaps|probably|say)\s+)?"
                         r"(\d{1,2})(?![\w:.])", lt):
        s, e = m.start(1), m.end(1)
        if not T.free(s, e) or not (T.pick_mode or _after_ok(T, e)):
            continue
        h = int(m.group(1))
        if 1 <= h <= 12:
            _add_time(T, s, e, _two(h, 0), True, "H12")
        elif 13 <= h <= 23:
            _add_time(T, s, e, [(h, 0)], False, "H13")
    # H21: vague words (greetings are not times)
    for pat, window in VAGUE:
        for m in re.finditer(r"\b" + pat + r"\b", lt):
            if not T.free(m.start(), m.end()):
                continue
            if re.search(r"\bgood\s+$", lt[:m.start()]):
                T.take(m.start(), m.end())
                continue
            win = (_dt.time(*window[0]), _dt.time(*window[1])) if window else None
            T.vague.append(_M(start=m.start(), end=m.end(), window=win))
            T.take(m.start(), m.end())


def _extract_ranges_into_units(T):
    """Q1 written with a dash between bare numbers right before a zone ("10-11 PT")."""
    for r in list(T.ranges):
        z = _zone_at(T, r.end)
        before_at = re.search(r"(?:\bat|@|\bfrom|\bbetween)\s*$", T.lt[:r.start]) is not None
        if (z is not None or before_at) and 1 <= r.a <= 12:
            tm = _M(start=r.start, end=r.end, clocks=_two(r.a, 0), two=True, form="Q1", never=False,
                    impossible=False, zone=z, range_start=True)
            if z is not None and T.free(z.start, z.end):
                T.take(z.start, z.end)
            T.times.append(tm)
            r.unit = tm


def _extract_relative(T):
    lt = T.lt
    sd = T.sent_date

    def add(m, form, cands, weekday=None):
        T.dates.append(_M(start=m.start(), end=m.end(), kind="relative", form=form, cands=cands,
                          weekday=weekday, sep=None, books=False))
        T.take(m.start(), m.end())

    for m in re.finditer(r"\b(?:early |late |later |sometime |some time )?(this|next) week\b", lt):
        if T.free(m.start(), m.end()):
            if m.group(1) == "this":
                add(m, "L6", [sd])
            else:
                add(m, "L6", [sd + _dt.timedelta(days=7 - sd.weekday())])
    for m in re.finditer(r"\bin\s+(\d{1,2}|" + NUMWORD + r"|a)\s+(days?|weeks?)\b", lt):
        if T.free(m.start(), m.end()):
            n = m.group(1)
            n = 1 if n == "a" else (int(n) if n.isdigit() else NUMWORDS[n])
            add(m, "L5", [sd + _dt.timedelta(days=n * (7 if m.group(2).startswith("week") else 1))])
    for m in re.finditer(r"\ba week from\s+(?:" + WD + r"|today|tomorrow)", lt):
        if T.free(m.start(), m.end()):
            if m.group(1):
                wd = WEEKDAYS[m.group(1)]
                base = sd + _dt.timedelta(days=(wd - sd.weekday()) % 7 or 7)
            else:
                base = sd + _dt.timedelta(days=0 if "today" in m.group(0) else 1)
            add(m, "L5", [base + _dt.timedelta(days=7)])
    for m in re.finditer(r"\b(the day after tomorrow|today|tonight|tomorrow|tmrw)\b", lt):
        if T.free(m.start(), m.end()):
            off = {"the day after tomorrow": 2, "today": 0, "tonight": 0, "tomorrow": 1, "tmrw": 1}[m.group(1)]
            add(m, "L1", [sd + _dt.timedelta(days=off)])
    for m in re.finditer(r"\bnext\s+" + WD, lt):
        if T.free(m.start(), m.end()):
            wd = WEEKDAYS[m.group(1)]
            first = sd + _dt.timedelta(days=(wd - sd.weekday()) % 7 or 7)
            add(m, "L3", [first, first + _dt.timedelta(days=7)], weekday=wd)
    for m in re.finditer(r"(?<![\w])(?:(?:this|on|coming)\s+)?" + WD, lt):
        if T.free(m.start(), m.end()):
            word = m.group(1)
            ws = m.start(1)
            if not _weekday_ok_short(T, ws, lt[ws:m.end()]) and not _next_to_mention(T, m.start(), m.end()):
                continue
            wd = WEEKDAYS[word]
            add(m, "L2", [sd + _dt.timedelta(days=(wd - sd.weekday()) % 7 or 7)], weekday=wd)
    for m in re.finditer(r"\b(?:on\s+)?the\s+(\d{1,2})(?:st|nd|rd|th)\b", lt):
        if T.free(m.start(), m.end()):
            day = int(m.group(1))
            d = None
            for k in range(0, 3):
                mo = (sd.month - 1 + k) % 12 + 1
                y = sd.year + (sd.month - 1 + k) // 12
                try:
                    c = _dt.date(y, mo, day)
                except ValueError:
                    continue
                if c >= sd:
                    d = c
                    break
            add(m, "L4", [d] if d else [])
    for m in re.finditer(r"\b(soon|sometime|some time|asap|a\.s\.a\.p\.?|next month|after the holidays|"
                         r"later this month|this month)\b", lt):
        if T.free(m.start(), m.end()):
            add(m, "L7", [])


def _next_to_mention(T, a, b):
    for x in T.times + T.dates:
        if abs(x.start - b) <= 2 or abs(a - x.end) <= 2:
            gap = T.lt[b:x.start] if x.start >= b else T.lt[x.end:a]
            if re.match(r"^[\s,]*$", gap):
                return True
    return False


def _zone_before_a_relative_date(T):
    """Row Z3 lets a date follow a bare region word ("1pm Eastern Thursday"). The relative dates are
    read after the times, so a time left with no zone is looked at once more. Such a date never
    books (B5): this changes what is offered, and which offered slot a reply names, never a
    direct booking."""
    for tm in T.times:
        if tm.zone is None:
            z = _zone_at(T, tm.end)
            if z is not None and z.kind == "region" and T.free(z.start, z.end):
                tm.zone = z
                T.take(z.start, z.end)


def _extract_free_zones(T):
    lt = T.lt
    pats = [
        (r"\b(?:central|eastern|western)\s+european(?:\s+(?:summer|standard))?\s+time\b", None),
        (r"\b(pacific|mountain|central|eastern)\s+(?:(standard|daylight)\s+)?time\b", "region"),
        (r"\(\s*(" + "|".join(US_ABBRS) + r"|utc|gmt)\s*\)", "abbr"),
        (r"\b(utc|gmt)\b(?:\s?([+-])(\d{1,2})(?::?(\d{2}))?)?", "utc"),
    ]
    for pat, kind in pats:
        for m in re.finditer(pat, lt):
            if not T.free(m.start(), m.end()):
                continue
            zid = None
            if kind == "region":
                zid = _REGION[m.group(1)]
            elif kind == "abbr":
                zid = zones.abbr_zone(m.group(1).upper())
            elif kind == "utc":
                zid = "UTC"
                if m.group(2):          # "GMT+1" is that offset, not UTC; past 14 hours it is no zone
                    hours, minutes = int(m.group(3)), int(m.group(4) or 0)
                    zid = None
                    if hours <= 14 and minutes < 60:
                        zid = zones.offset_zone((hours * 60 + minutes) * (1 if m.group(2) == "+" else -1))
            T.zones_free.append(_M(start=m.start(), end=m.end(), zid=zid))
            T.take(m.start(), m.end())


def _extract(t, sent_date):
    T = _Text(t, sent_date)
    _extract_dates(T)
    _extract_times(T)
    _extract_ranges_into_units(T)
    _extract_relative(T)
    _zone_before_a_relative_date(T)
    _extract_free_zones(T)
    for m in re.finditer(r"\beither\b", T.lt):
        T.either.append(m.start())
    T.dates.sort(key=lambda d: d.start)
    T.times.sort(key=lambda x: x.start)
    return T


# ---------------------------------------------------------------------------------------
# Ruling 5: length
# ---------------------------------------------------------------------------------------
def parse_length(lt) -> Tuple[int, str, Optional[str]]:
    """(length_min, written|default, rule or None) from the fixed phrase lists of ruling 5."""
    rule = None
    if re.search(r"\b(?:all day|full day|whole day|all-day|full-day)\b", lt):
        rule = "all_day"
    elif re.search(r"\b(?:every|weekly|each week|recurring|standing|bi-?weekly|fortnightly)\b", lt):
        rule = "recurring"
    elif re.search(r"\b(?:(?:two|three|four|several|multiple|a few|a couple of)\s+(?:calls|meetings|sessions)|a series)\b", lt):
        rule = "multiple"
    elif re.search(r"\b(?:as long as it takes|a few hours|open[\s-]ended|however long|several hours)\b", lt) or \
            re.search(r"(?<!\bin )(?<!\bduring )(?<!\bof )(?<!\bon )\bthe afternoon\b", lt):
        rule = "open_ended"
    elif re.search(r"\b(?:0|zero)[\s-]*(?:minutes?|mins?)\b", lt):
        rule = "zero"
    found = []
    for m in re.finditer(r"\b(\d{1,3}(?:\.\d+)?)[\s-]*(minutes?|mins?|hours?|hrs?|hr)\b", lt):
        n = float(m.group(1))
        mins = n * 60 if m.group(2).startswith("h") else n
        if m.group(2).startswith("h") or mins > 60 or 5 <= mins <= 60:
            found.append(int(round(mins)))
    for pat, mins in ((r"an hour and a half|one and a half hours|hour and a half", 90),
                      (r"(?:two|three|four|five|six) hours", None),
                      (r"half an hour|half hour|half-hour", 30), (r"a quarter of an hour|quarter hour", 15),
                      (r"(?<!half )(?<!half-)(?<!quarter of )\b(?:an|one) hour\b", 60)):
        for m in re.finditer(r"\b(?:" + pat + r")\b", lt):
            if mins is None:
                mins_v = NUMWORDS[m.group(0).split()[0]] * 60
            else:
                mins_v = mins
            found.append(mins_v)
    if re.search(r"\ban hour and a half\b", lt):
        found = [x for x in found if x != 60]
    if rule is not None:
        n = max(found) if found else caps.DEFAULT_LENGTH_MIN
        if rule == "zero":
            n = 0
        return n, "written" if found or rule == "zero" else "default", rule
    if found:
        n = max(found)
        if n > 60:
            return n, "written", "over_60"
        return n, "written", None
    return caps.DEFAULT_LENGTH_MIN, "default", None


# ---------------------------------------------------------------------------------------
# Readings
# ---------------------------------------------------------------------------------------
def _zone_ids_for(tm, d, steer_zone):
    """-> (zone ids to read the time in, booking_zone_ok, drop_rule_applies, rule)."""
    z = tm.zone
    if z is None:
        if steer_zone:
            return [steer_zone], False, steer_zone in zones.US_ZONES, "Z10"
        return ["Pacific"], False, False, "Z11"
    if z.unknown or z.zid is None:
        return [], False, False, "Z12"
    zid = z.zid
    if z.abbr and d is not None and not zones.abbr_in_effect(z.abbr, d):
        return [zid], False, z.us, "Z8"
    if z.gmt and d is not None and zones.utc_offset("UK", zones.to_utc(_dt.datetime.combine(d, _dt.time(12)), "UTC")) \
            != _dt.timedelta(0):
        return ["UTC", "UK"], True, False, "Z16"
    return [zid], True, (z.us or z.your_time), "Z"


def _instant(d, clock, zid):
    h, m = clock
    base = _dt.datetime.combine(d, _dt.time(0, 0))
    local = base + _dt.timedelta(hours=h, minutes=m)
    return zones.to_utc(local, zid)


def _time_readings(tm, d, steer_zone):
    """Readings of one time on one date: list of (instant|None, zone|None, kept_by_drop_rule, rule)."""
    zids, _, drop, zrule = _zone_ids_for(tm, d, steer_zone)
    out = []
    if not zids:
        return [(None, None, True, zrule)]
    for clock in tm.clocks:
        for zid in zids:
            try:
                inst = _instant(d, clock, zid)
            except Exception:
                continue
            kept = True
            rule = tm.form
            if tm.two and drop:
                hour = clock[0] % 24
                if hour >= 21 or hour < 7:
                    kept, rule = False, "P1"
            out.append((inst, zid, kept, rule))
    return out


class _Unit(object):
    def __init__(self, times):
        self.times = times
        self.main = times[0]
        self.start = min(t.start for t in times)
        self.end = max((t.zone.end if t.zone else t.end) for t in times)
        self.pair = False


def _date_for(T, pos, sentence, skip=()):
    """The date mention a time at pos belongs to: same sentence and nearest, else nearest. The
    mentions in `skip` are passed over."""
    best = None
    for d in T.dates:
        if d.kind == "dotted" and getattr(d, "as_time", False):
            continue
        if id(d) in skip:
            continue
        same = T.sentence(d.start) == sentence
        dist = min(abs(d.start - pos), abs(d.end - pos))
        key = (0 if same else 1, dist)
        if best is None or key < best[0]:
            best = (key, d)
    return best[1] if best else None


def _weekday_names(T):
    names = []
    for d in T.dates:
        if d.weekday is not None:
            names.append((d.weekday, d))
    return names


def _date_cands(T, dm, for_offer=False):
    """Candidate dates of a date mention, with kept flags: [(date, kept, rule)]. P2 is applied here.
    With `for_offer` (the offer's readings, never the booking conditions) a weekday written with a
    week phrase or with its day number is that one day (see _frame_days)."""
    cands = dm.cands
    if for_offer and getattr(dm, "offer_day", None) is not None:
        cands = [dm.offer_day]
    out = [(c, True, dm.form) for c in cands]
    if len(out) == 2:
        names = [w for w, _ in _weekday_names(T)]
        if names:
            matching = [i for i, (c, _, _) in enumerate(out) if all(c.weekday() == w for w in names)]
            if len(matching) == 1:
                out = [(c, i == matching[0], r if i == matching[0] else "P2") for i, (c, k, r) in enumerate(out)]
    return out


def _bookable_fn(bookable):
    if bookable is not None:
        return bookable
    from . import slots   # imported only when needed
    return slots.bookable


def _is_bookable(bookable, start, length, now, written=True):
    """G9 for one start: a time the sender wrote may sit on any quarter hour (written), one of hers
    on the hour or half hour only."""
    try:
        if start is None or start.minute % 15 or start.second:
            return False
        ok, _ = bookable(Slot(start, start + _dt.timedelta(minutes=length)), now, written)
        return bool(ok)
    except Exception:
        return False


def _q_flags(T, dm, unit):
    """B6: the qualifiers that touch the date or the time."""
    lt = T.lt
    flags = []
    spots = []
    if dm is not None:
        spots.append((dm.start, dm.end))
    if unit is not None:
        for tm in unit.times:
            spots.append((tm.start, tm.zone.end if tm.zone else tm.end))
            spots.append((tm.start, tm.end))
    for s, e in spots:
        if HEDGE_BEFORE.search(lt[:s]) or HEDGE_AFTER.match(lt[e:]):
            flags.append("Q3")
        if BOUND_BEFORE.search(lt[:s]) or BOUND_AFTER.match(lt[e:]):
            flags.append("Q4")
    if unit is not None:
        for tm in unit.times:
            ends = [tm.end] + ([tm.zone.end] if tm.zone else [])
            if any(RANGE_AFTER.match(lt[e:]) for e in ends) or RANGE_BEFORE.search(lt[:tm.start]) or \
                    getattr(tm, "range_start", False) or re.search(r"\bbetween\b[^.?!\x1e]{0,20}$", lt[:tm.start]):
                flags.append("Q1")
    sentences = set()
    if dm is not None:
        sentences.add(T.sentence(dm.start))
    if unit is not None:
        sentences.add(T.sentence(unit.start))
    for r in T.ranges:
        if T.sentence(r.start) in sentences:
            flags.append("Q1")
    for p in T.either:
        if T.sentence(p) in sentences:
            flags.append("Q2")
    if T.q2:
        flags.append("Q2")
    for s in sentences:
        text = _sentence_text(T, s)
        if NEGATION.search(text) or NEG_BUT.search(text) or _but_before(T, dm, unit):
            flags.append("Q5")
        if NOT_MEETING.search(text):
            flags.append("Q10")
    return flags


def _but_before(T, dm, unit):
    for x in ([dm] if dm else []) + ([unit] if unit else []):
        if re.search(r"\bbut\s+(?:(?:at|on|the|@)\s+)*$", T.lt[:x.start]):
            return True
    return False


def _sentence_text(T, s):
    a = T.ends[s - 1] + 1 if s > 0 else 0
    b = T.ends[s] if s < len(T.ends) else len(T.lt)
    return re.sub(r"\bif not\b", " ", T.lt[a:b])


def _sentence_flags(T, pos):
    """(negated, not the meeting) for the sentence at pos: rows Q5 and Q10. Kept per sentence."""
    s = T.sentence(pos)
    if s not in T.flags:
        text = _sentence_text(T, s)
        T.flags[s] = (bool(NEGATION.search(text) or NEG_BUT.search(text)), bool(NOT_MEETING.search(text)))
    return T.flags[s]


def _iso(dt_):
    return dt_.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if dt_ is not None else None


def _offer(reason, h, length=(caps.DEFAULT_LENGTH_MIN, "default", None)):
    return ParseResult(verdict="offer", failed_condition=reason, readings=[], instant_utc=None, zone=None,
                       length_min=length[0], length_source=length[1], length_rule=length[2],
                       visible_body_hash=h, conditions={c: False for c in CONDITIONS})


def parse_request(own_text, sent_utc, now_utc, bookable: Optional[Callable] = None) -> ParseResult:
    """Ruling 2 on the sender's own visible text. Never raises: anything unreadable is an offer."""
    try:
        h = hashlib.sha256(own_text.encode("utf-8", "surrogatepass")).hexdigest() if isinstance(own_text, str) \
            else hashlib.sha256(b"").hexdigest()
    except Exception:
        h = hashlib.sha256(b"").hexdigest()
    try:
        return _parse(own_text, sent_utc, now_utc, bookable, h)
    except Exception:
        return _offer("B1", h)


def _parse(own_text, sent_utc, now_utc, bookable, body_hash):
    text, direction, glued = fold(own_text)
    t = _flatten(text)
    sent_date = zones.pacific_date(sent_utc)
    T = _extract(t, sent_date)
    length = parse_length(T.lt)

    # Units: times grouped as one when they are a range (Q1) or the same instant in two zones (Q7).
    times = list(T.times)
    used = set()
    units = []
    for i, tm in enumerate(times):
        if id(tm) in used:
            continue
        group = [tm]
        used.add(id(tm))
        # a range written with words: "from 10am to 11am", "10am - 11am"
        for tn in times[i + 1:]:
            if id(tn) in used:
                continue
            gap = T.lt[(tm.zone.end if tm.zone else tm.end):tn.start]
            if re.match(r"^\s*(?:-|to|until|till|thru|through|and)\s*$", gap) and \
                    T.sentence(tn.start) == T.sentence(tm.start):
                group.append(tn)
                used.add(id(tn))
                tn.range_end = True
                tm.range_start = True
                if tm.zone is None and tn.zone is not None:
                    tm.zone = tn.zone
            break
        units.append(_Unit(group))
    # Q7 pairs
    merged = []
    for u in units:
        prev = merged[-1] if merged else None
        if prev is not None and _same_instant_pair(T, prev, u):
            prev.times.extend(u.times)
            prev.end = u.end
            prev.pair = True
            continue
        merged.append(u)
    units = merged
    for u in units:
        _range_start(T, u)
    steer_zone = None
    for zf in T.zones_free:
        if zf.zid:
            steer_zone = zf.zid
            break

    # Dotted pairs (D16) act as the time when there is another date and no time.
    others = [d for d in T.dates if d.kind != "dotted"]
    for d in T.dates:
        if d.kind == "dotted" and others and not units and d.a <= 23 and d.b <= 59:
            tm = _M(start=d.start, end=d.end, clocks=_two(d.a, d.b) if 1 <= d.a <= 12 else [(d.a, d.b)],
                    two=1 <= d.a <= 12, form="D16", never=False, impossible=False, zone=d.zone)
            d.as_time = True
            units.append(_Unit([tm]))

    date_mentions = list(T.dates)
    readings = []
    excluded = []
    vague_units = len(T.vague)

    # Build readings: each unit with its date, then dates with no time. For the offer a weekday
    # written with a week phrase is read inside that week, and the week then names no day itself.
    # A mail crowded with dates and times (it cannot book: B2) gets none of this: no time is put
    # first and no day steers, so the longest mail still reads in a moment.
    crowded = len(T.dates) + len(T.times) + len(T.vague) > _CROWDED
    try:
        stood_for = set() if crowded else _frame_days(T)
    except Exception:           # this only orders an offer: it never costs the reading
        stood_for = set()
    unit_dates = [] if crowded else \
        [(u, _date_for(T, u.main.start, T.sentence(u.main.start), stood_for)) for u in units]
    paired_dates = set(id(dm) for _, dm in unit_dates if dm is not None)
    with_time = {}              # a date mention -> the readings of the times written with it
    for u, dm in unit_dates:
        found = _unit_readings(T, u, dm, steer_zone, now_utc, bookable, length, excluded, paired=paired_dates)
        readings.extend(found)
        if dm is not None:
            with_time.setdefault(id(dm), []).extend(found)
    for dm in date_mentions:
        if crowded or id(dm) in paired_dates or id(dm) in stood_for or getattr(dm, "as_time", False):
            continue
        for c in ([dm.offer_day] if getattr(dm, "offer_day", None) is not None else dm.cands):
            readings.append(Reading(start_utc=None, date=c, zone=None, kept=True, rule=dm.form))

    # Conditions
    cond = {c: False for c in CONDITIONS}
    cond["B1"] = not direction and not glued
    n_dates = len(date_mentions)
    n_times = len(units) + vague_units
    cond["B2"] = n_dates == 1 and n_times == 1 and len(units) == 1
    dm = date_mentions[0] if date_mentions else None
    unit = units[0] if units else None
    instant = None
    if dm is not None and unit is not None and not getattr(dm, "as_time", False):
        cond["B3"] = _side_by_side(T, dm, unit)
        cond["B4"] = _zones_ok(T, unit, dm)
        cond["B5"] = bool(dm.books)
        cond["B6"] = not _q_flags(T, dm, unit)
        own = _unit_readings(T, unit, dm, steer_zone, now_utc, bookable, length, [], own_only=True)
        kept = sorted(set(r.start_utc for r in own if r.kept and r.start_utc is not None))
        unplaced = any(r.start_utc is None for r in own)
        cond["B7"] = len(kept) == 1 and not unplaced and not any(t.impossible for t in unit.times)
        if cond["B7"]:
            instant = kept[0]
            try:
                fn = _bookable_fn(bookable)
            except Exception:
                fn = None
            cond["B8"] = fn is not None and _is_bookable(fn, instant, length[0], now_utc)
            only = [r for r in own if r.kept and r.start_utc == instant][0]
            names = [w for w, _ in _weekday_names(T)]
            cond["B9"] = all(only.date.weekday() == w for w in names)
        else:
            names = [w for w, _ in _weekday_names(T)]
            cond["B9"] = all(any(c.weekday() == w for c in dm.cands) for w in names)
    failed = None
    for c in CONDITIONS:
        if not cond[c]:
            failed = c
            break
    verdict = "book" if failed is None and length[2] is None and instant is not None else "offer"
    zone = _result_zone(unit, steer_zone)
    zone_pair = None
    if unit is not None and unit.pair:
        zone_pair = tuple(t.zone.zid for t in unit.times[:2])
    steer_day = steer_window = None
    try:
        if not crowded:
            steer_day, steer_window, zone = _steer(T, with_time, stood_for, zone, now_utc, readings, length[0])
    except Exception:           # it only orders an offer: it never costs the reading
        steer_day = steer_window = None
    return ParseResult(
        verdict=verdict, failed_condition=failed, readings=readings,
        instant_utc=instant if verdict == "book" else None, zone=zone, zone_pair=zone_pair,
        steer_day=steer_day, steer_window=steer_window, excluded_utc=sorted(set(excluded)),
        length_min=length[0], length_source=length[1], length_rule=length[2],
        visible_body_hash=body_hash, conditions=cond)


# ---------------------------------------------------------------------------------------
# The offer's steer (ruling 2: "the sender's apparent time first"; the offer column of the
# tables): the days the sender named, the part of day, and the zone both are read in. It
# orders an offer and nothing else: no condition B1 to B9 reads anything set here.
# ---------------------------------------------------------------------------------------
_OWN_WEEK = re.compile(r"(?:this|coming)\b")
_LATE_IN_WEEK = re.compile(r"(?:late|later)\b")
_WEEK_GLUE = re.compile(r"^(?:[\s,()@-]|\b(?:of|in|during|the|at|on)\b)*$")
_LIST_JOIN = re.compile(r"^\s*(?:(?:at|@)\s+)?(?:,\s*)?(?:(?:and|or|nor)\b|&|/)\s*(?:(?:on|the)\s+)?$")
_LIST_COMMA = re.compile(r"^\s*,\s*$")
_LIST_GOES_ON = re.compile(r"^\s*(?:,|(?:and|or|nor)\b|&|/|[.?!\x1e]|$)")
_CONDITIONAL = re.compile(r"\b(?:if|unless|in case|whether|should|when)\b")
_CLAUSE_BREAK = re.compile(r"[,;:]|\bbut\b")
_NOT_FREE = (r"(?:out|off|away|travell?ing|on vacation|on holiday|on leave|on pto|out of (?:the )?office|"
             r"out of town|ooo|unavailable|busy|booked|booked up|tied up|slammed|swamped)")
_RULED_OUT_BEFORE = re.compile(
    r"(?:(?:\b(?:i|we)\s+(?:really\s+|just\s+|simply\s+|also\s+|sadly\s+|unfortunately\s+)?|(?:^|[.?!;:,\x1e-])\s*)"
    r"(?:can't|cannot|can not|cant|couldn't|couldnt|won't be able to|wont be able to|will not be able to|"
    r"am unable to|'m unable to|are unable to|'re unable to|am not able to|'m not able to)\s+"
    r"(?:(?:do|make|meet|manage|swing|attend|talk|chat|call|join)\s+)?(?:(?:it|that|this|anything|a call|a meeting)\s+)?"
    r"|(?<!\bwhy )(?<!\bif )(?<!\bor )\bnot\s+"
    r"|\b(?:except|except for|other than|aside from|apart from|excluding)\s+"
    r"|\b(?:anything|any day|any time|anytime|all|all week|every day)\s+but\s+"
    r"|\b(?:i|we)(?:'m| am|'re| are|'ll be| will be)\s+(?:not\s+(?:available|free|around|in|in the office|"
    r"at work|working)|" + _NOT_FREE + r")\s+"
    r"|\b(?:i|we)\s+(?:won't|wont|will not)\s+be\s+(?:available|free|around|in|in the office|at work|working)\s+"
    r")(?:(?:on|for|the|at|in|during|all|all day|all of)\s+)*$")
_RULED_OUT_AFTER = re.compile(
    r"^\s*(?:(?:at|@)\s+)?(?:(?:doesn't|does not|doesnt|don't|do not|dont|won't|will not|wont|wouldn't|would not|"
    r"wouldnt|can't|cannot|cant)\s+(?:really\s+)?(?:work|suit|fit|be possible|be good|happen)\b"
    r"|(?:is|are|'s|looks|look|will be|would be)\s+(?:not\s+(?:possible|good|great|ideal|going to work|an option|"
    r"available|doable|free|open)|out|bad|booked|booked up|busy|full|packed|impossible|unavailable|no good|"
    r"tough|tricky|difficult|off the table|out of the question)\b"
    r"|(?:isn't|isnt|aren't|arent|won't be|wont be)\s+(?:possible|good|great|ideal|going to work|an option|"
    r"available|doable|free|open)\b"
    r"|(?:,\s*)?(?:i|we)\s+(?:can't|cannot|cant|can not)\b"
    r"|(?:,\s*)?(?:i|we)(?:'m| am|'re| are|'ll be| will be)\s+" + _NOT_FREE + r"\b)")
_CAPS_US_ABBR = re.compile(r"(?:" + "|".join(US_ABBRS).upper() + r")\b")
_CAPS_UNKNOWN_ABBR = re.compile(r"(?:" + "|".join(UNKNOWN_ABBRS).upper() + r")\b")
_PLACE_TIME = re.compile(r"((?:[A-Z][A-Za-z.]*\s+){1,3})time\b")
_NOT_A_PLACE = ("what", "which", "any", "some", "this", "that", "next", "last", "same", "the", "a", "no",
                "every", "each", "more", "lunch", "dinner", "good", "best", "first")
_PARTS_OF_DAY = re.compile(r"\b(?:mornings|afternoons|evenings)\b")
_CROWDED = 100              # dates, times and parts of day: past this a mail gets the earliest slots
_NEAR = 160                 # how far around a mention its words are read, in characters


def _quiet(T, extra=()):
    """The lower-case text with the times (and their zones), the parts of day and the free zones
    blanked, and the spans in `extra`: the words between and around the day mentions."""
    spans = [(v.start, v.end) for v in T.vague] + [(z.start, z.end) for z in T.zones_free]
    spans += [(m.start(), m.end()) for m in _PARTS_OF_DAY.finditer(T.lt)]
    spans += [(tm.start, tm.zone.end if tm.zone is not None else tm.end) for tm in T.times]
    out = list(T.lt)
    for start, end in spans + list(extra):
        out[start:end] = " " * (min(end, len(out)) - start)
    return "".join(out)


def _frame_days(T):
    """What each day mention means to the offer, set on the mentions. Returns the ids of the week
    phrases a day written with them stands for, which then name no day of their own.

    - Row L6: a week phrase is its days, Monday to Sunday (.week).
    - A weekday written with its day number is that day (.offer_day): "Wednesday the 9th".
    - A weekday alone (row L2, not "this Thursday") written with a week phrase is the one inside
      that week (.offer_day), not the coming one. Any other day written with a week stands for
      it when it falls inside it ("next week, the 8th"). Written with: right before the week
      ("Tuesday or Wednesday next week"), right after it ("next week Tuesday"), or later in the
      same or the next sentence ("next week? Tuesday or Wednesday"). "Friday or next week" is
      two asks, a week the sender ruled out frames nothing, and neither does a date that is not
      the meeting (row Q10).
    - The days and weeks the sender ruled out (.out): see _rule_out.

    A week whose days are all ruled out keeps its own days: "next week, but not Monday".
    """
    dates = [d for d in T.dates if not getattr(d, "as_time", False)]
    quiet = _quiet(T)
    weeks = [d for d in dates if d.form == "L6" and d.cands]
    for w in weeks:
        monday = w.cands[0] - _dt.timedelta(days=w.cands[0].weekday())
        w.week = [monday + _dt.timedelta(days=i) for i in range(7)]
        w.framed = []
    for a, b in zip(dates, dates[1:]):
        for day, number in ((a, b), (b, a)):
            if day.form in ("L2", "L3") and number.form == "L4" and number.cands and \
                    number.cands[0].weekday() == day.weekday and T.sentence(a.end - 1) == T.sentence(b.start) and \
                    re.match(r"^[\s,(]*$", T.lt[a.end:b.start]):
                day.offer_day, day.number = number.cands[0], number
    is_week = set(id(w) for w in weeks)
    bare = set(id(d) for d in dates if d.form == "L2" and getattr(d, "offer_day", None) is None
               and not _OWN_WEEK.match(T.lt[d.start:d.end]))
    is_day = set(id(d) for d in dates if d.form != "L6" and not _sentence_flags(T, d.start)[1])
    framed = {}                     # a day mention -> the week it is written with

    def frame(d, week):
        if id(d) in bare:
            if week.week[d.weekday] < T.sent_date:
                return False        # "Monday this week", written on a Tuesday: the coming Monday
            d.offer_day = week.week[d.weekday]
        else:
            fixed = getattr(d, "offer_day", None)
            inside = [c for c in ([fixed] if fixed is not None else d.cands) if c in week.week]
            if not inside:
                return False        # a date outside the week is one more ask
            if len(inside) == 1:
                d.offer_day = inside[0]
        week.framed.append(d)
        framed[id(d)] = week
        return True

    # right before the week: the day next to it, and the days listed with that one
    before_week = {}
    for i in range(len(dates) - 2, -1, -1):
        d, nxt = dates[i], dates[i + 1]
        if id(d) not in is_day:
            continue
        if id(nxt) in is_week and T.sentence(nxt.start) == T.sentence(d.start) and \
                _WEEK_GLUE.match(quiet[d.end:nxt.start]):
            if frame(d, nxt):
                before_week[id(d)] = d.week_after = nxt
                nxt.paired = True
        elif id(nxt) in before_week and _listed(T, d, nxt, quiet) and frame(d, before_week[id(nxt)]):
            before_week[id(d)] = before_week[id(nxt)]
    # right after the week
    for prv, d in zip(dates, dates[1:]):
        if id(d) in is_day and id(d) not in framed and id(prv) in is_week and \
                re.match(r"^[\s,]*$", T.lt[prv.end:d.start]) and frame(d, prv):
            d.week_before = prv
            prv.paired = True
    _rule_out(T, dates, quiet)
    # later in the same or the next sentence
    week = None
    for prv, d in zip([None] + dates, dates):
        if id(d) in is_week:
            week = d
        if week is None or id(d) not in is_day or id(d) in framed:
            continue
        if getattr(week, "out", False) or T.sentence(d.start) - T.sentence(week.start) > 1:
            continue
        if prv is not week and framed.get(id(prv)) is not week:
            continue                # the day written before this one is not that week's either
        if prv is week and _LIST_JOIN.match(quiet[week.end:d.start]):
            continue                # "next week or Friday"
        frame(d, week)
    return set(id(w) for w in weeks if any(not getattr(d, "out", False) for d in w.framed))


def _span(d):
    """Where a day mention starts and ends, counting the week written with it ("Tuesday next week")."""
    return getattr(d, "week_before", d).start, getattr(d, "week_after", d).end


def _ruled_out_at(T, start, end, quiet):
    """The words right before or right after T.lt[start:end] rule it out: "I can't do ...",
    "... doesn't work", "any day except ...", "I'm out ...". Not under an "if" in the same
    clause ("if Friday doesn't work, Monday?")."""
    s, e = T.sentence(start), T.sentence(end - 1)       # end - 1: a weekday takes the full stop after it
    before = quiet[max(T.ends[s - 1] + 1 if s > 0 else 0, start - _NEAR):start]
    after = quiet[end:min(T.ends[e] if e < len(T.ends) else len(quiet), end + _NEAR)]
    if _CONDITIONAL.search(_CLAUSE_BREAK.split(before)[-1]):
        return False
    return bool(_RULED_OUT_BEFORE.search(before) or _RULED_OUT_AFTER.match(after))


def _rule_out(T, dates, quiet):
    """Marks the days and weeks the sender rules out (.out). The words sit right before or right
    after the day, or the list of days it is written in; a time or a part of day written with
    the day is read past ("I can't do 10am on Thursday"). Narrower than row Q5's whole sentence,
    so "I can't do Monday, Tuesday works" still names Tuesday. A week written with a weekday
    ("Tuesday next week") is part of that weekday."""
    lists = []
    for d in dates:
        if getattr(d, "paired", False):
            continue
        if lists and _listed(T, lists[-1][-1], d, quiet):
            lists[-1].append(d)
        else:
            lists.append([d])
    for days in lists:
        if _ruled_out_at(T, _span(days[0])[0], _span(days[-1])[1], quiet):
            for d in days:
                d.out = True
    for d in dates:                 # a weekday and its day number are one day
        number = getattr(d, "number", None)
        if number is not None and (getattr(d, "out", False) or getattr(number, "out", False)):
            d.out = number.out = True


def _listed(T, a, b, quiet):
    """Two day mentions written as one list: "Monday and Tuesday", "Monday, Tuesday or Wednesday",
    "Thursday at 10 or Friday at 2", "Wednesday the 9th"."""
    a_end, (b_start, b_end) = _span(a)[1], _span(b)
    if b_start - a_end > _NEAR:
        return False
    gap = quiet[a_end:b_start]
    if _LIST_JOIN.match(gap):
        return True
    if not T.lt[a_end:b_start].strip() and "L6" not in (a.form, b.form) and \
            T.sentence(a_end - 1) == T.sentence(b_start):
        return True
    e = T.sentence(b_end - 1)
    rest = quiet[b_end:min(T.ends[e] + 1 if e < len(T.ends) else len(quiet), b_end + _NEAR)]
    return bool(_LIST_COMMA.match(gap) and _LIST_GOES_ON.match(rest))


def _days_of(T, d):
    """The days one date mention names, in the order to offer them: a week from the day the mail
    was sent, from its end backwards when it is "late" or "later" in the week."""
    if d.form == "L6":
        days = [x for x in getattr(d, "week", ()) if x >= T.sent_date]
        return days[::-1] if _LATE_IN_WEEK.match(T.lt[d.start:d.end]) else days
    return [c for c, kept, _ in _date_cands(T, d, for_offer=True) if kept]


def _zone_after_part_of_day(T, v):
    """The zone written right after a part of day: (zone id, True) for one she knows ("afternoon
    Eastern", "morning (ET)", "afternoon your time"), (None, True) for one she has no rule for
    ("afternoon (UK time)", "morning my time", "afternoon CET"), (None, False) when none is
    written there. An abbreviation counts only in capitals or in brackets, and a place only with
    "time" after it: "afternoon et al" and "afternoon, what time works" name no zone."""
    m = re.match(r"\s*,?\s*(\()?\s*", T.lt[v.end:])
    p = v.end + m.end()
    for z in T.zones_free:      # read already: "(ET)", "Eastern time", "UTC+1", "Central European Time"
        if z.start == p or (m.group(1) and z.start == v.end + m.start(1)):
            return z.zid, True
    rest, shown = T.lt[p:], T.t[p:]
    if m.group(1):              # in brackets: the bracket holds a zone and nothing else, or it is none
        mb = re.match(r"([^()\x1e]{1,40}?)\s*\)", rest)
        if not mb or not T.free(p, p + mb.end(1)):
            return None, False
        inner = mb.group(1)
        if inner in _REGION:
            return _REGION[inner], True
        if re.match(r"(?:your\s+time(?:\s*zone)?|shane'?s\s+time(?:\s*zone)?)$", inner):
            return "Pacific", True
        if inner in UNKNOWN_ABBRS or re.search(r"\btime(?:\s*zone)?$", inner) or \
                re.match(r"[A-Z]{2,5}$", shown[:mb.end(1)]):
            return None, True
        return None, False
    mr = re.match(r"(pacific|mountain|central|eastern)\b", rest)
    if mr and _Z3_FOLLOW.match(rest[mr.end():]):
        return _REGION[mr.group(1)], True
    ma = _CAPS_US_ABBR.match(shown)
    if ma:
        return zones.abbr_zone(ma.group(0)), True
    if re.match(r"(?:your\s+time(?:\s*zone)?|shane'?s\s+time(?:\s*zone)?)\b", rest):
        return "Pacific", True
    if re.match(r"(?:my|our|local)\s+time\b", rest) or _CAPS_UNKNOWN_ABBR.match(shown):
        return None, True
    mp = _PLACE_TIME.match(shown)
    if mp and mp.group(1).split()[0].lower() not in _NOT_A_PLACE:
        return None, True
    return None, False


def _steer(T, with_time, stood_for, zone, now, readings=(), length_min=0):
    """What the offer looks at first, from what the sender wrote: (steer_day, steer_window, zone).

    steer_day: the days the sender named, in the order written (one date, several in a tuple, or
    None). A week phrase is its days (row L6), and a weekday written with a week or with its day
    number is that one day. A day written with a time is named by the readings of that time. A
    date marked as something other than the meeting (row Q10) names no day. A day the sender
    ruled out is left out; with nothing else named, every other day of the booking window stands
    in, so that day is not offered while another is free. A day ruled out together with a time
    ("I can't do Thursday at 10") is neither asked for nor avoided.

    steer_window: the first part of day the sender did not rule out (row H21), read in the zone
    written right after it, else in the sender's zone, else in Pacific. Written in a zone she
    has no rule for, next to a time written in one, or in another zone than the one the times
    are read in, it is not placed. With no part of day written, the one time of day the sender
    asked for is the window (ruling 6): when that time is taken, the offer stays near it.
    """
    named_by, ruled_out = {}, set()
    for d in T.dates:
        if getattr(d, "as_time", False):
            continue
        out = getattr(d, "out", False)
        if id(d) in with_time:
            days = [r.date for r in with_time[id(d)] if not r.derived and r.rule not in ("P2", "Q10")]
            if days and not out:
                named_by[id(d)] = _days_of(T, d) if d.form == "L6" else days
        elif out:
            ruled_out.update(_days_of(T, d))
        elif not _sentence_flags(T, d.start)[1]:
            named_by[id(d)] = _days_of(T, d)
    named = []
    for d in T.dates:
        if id(d) in stood_for and any(named_by.get(id(x)) for x in d.framed):
            continue                # a day written with the week, inside it, steers instead of the week
        named.extend(named_by.get(id(d), ()))
    named = [x for x in dict.fromkeys(named) if x not in ruled_out]
    if not named and ruled_out:
        first, last = zones.pacific_date(now), zones.pacific_date(now + caps.MAX_HORIZON)
        window = (first + _dt.timedelta(days=i) for i in range((last - first).days + 1))
        named = [x for x in window if x not in ruled_out]
    steer_day = None if not named else (named[0] if len(named) == 1 else tuple(named))

    steer_window = None
    quiet = _quiet(T, [(d.start, d.end) for d in T.dates])
    parts = sorted((v for v in T.vague if v.window and not _ruled_out_at(T, v.start, v.end, quiet)),
                   key=lambda v: v.start)
    if parts:
        steer_window = parts[0].window
        zid, written = _zone_after_part_of_day(T, parts[0])
        if written and zid is not None and zone is None:
            zone = zid
        if (written and (zid is None or zid != zone)) or (T.zones_free and not any(z.zid for z in T.zones_free)) \
                or any(_in_a_zone_she_has_no_rule_for(T, tm) for tm in T.times):
            steer_window = None
    # A part of day they wrote is never replaced, placed or not. With a day ruled out the offer keeps
    # to the day they asked for: the days nearest it would bring the day they ruled out back.
    if not parts and not ruled_out and not any(getattr(d, "out", False) for d in T.dates):
        steer_window = _the_one_time(readings, zone, length_min)
    return steer_day, steer_window, zone


def _in_a_zone_she_has_no_rule_for(T, tm):
    """A time written in a zone she has no rule for (row Z12): "2pm UK time", "14:00 CET", "2pm my
    time". Ordinary words that only look like one ("2pm works this time") do not count."""
    z = tm.zone
    if z is None or z.kind != "unknown":
        return False
    shown = T.t[z.start:z.end].strip(" ()")
    low = shown.lower()
    if low in UNKNOWN_ABBRS or re.match(r"(?:my|our|local)\s+time$", low) or " european " in " " + low + " ":
        return True
    m = _PLACE_TIME.match(shown)
    return bool(m and m.group(1).split()[0].lower() not in _NOT_A_PLACE)


def _the_one_time(readings, zone, length_min):
    """The one time of day the sender asked for, as a window in the zone it is read in: (start, end).
    None when they asked for no time she could place, for more than one time of day, for a time
    marked as something other than the meeting (row Q10), or for a length she does not book."""
    if isinstance(length_min, bool) or not isinstance(length_min, int) or \
            not caps.MIN_LENGTH_MIN <= length_min <= caps.MAX_LENGTH_MIN:
        return None
    starts = set(zones.to_local(r.start_utc, zone or "Pacific").time() for r in readings
                 if r.kept and not r.derived and r.start_utc is not None and r.rule != "Q10")
    if len(starts) != 1:
        return None
    start = _dt.datetime.combine(_dt.date(2000, 1, 3), starts.pop())
    return start.time(), (start + _dt.timedelta(minutes=length_min)).time()


_RANGE_START = re.compile(r"(?<![\w:.$/])(\d{1,2})(?::(\d{2}))?\s*(?:([ap])\.?\s?m\b\.?)?\s*"
                          r"(?:-|to|until|till|thru|through)\s*$")


def _range_start(T, u):
    """Q1 "10-11am": the range's start becomes the unit's time; the end is never offered first."""
    tm = u.main
    if getattr(tm, "range_start", False) or getattr(tm, "range_end", False):
        return
    m = _RANGE_START.search(T.lt[:tm.start])
    if not m or not T.free(m.start(), m.end()) or T.sentence(m.start()) != T.sentence(tm.start):
        return
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    two = False
    if m.group(3):
        clocks, imp, _ = _clock(h, mi, m.group(3))
    elif len(tm.clocks) == 1 and not tm.two and 1 <= h <= 12 and mi < 60:
        end_h = tm.clocks[0][0]
        pm = end_h >= 12
        if (h % 12) > (end_h % 12):
            pm = not pm
        clocks, imp = [((h % 12) + (12 if pm else 0), mi)], False
    elif 1 <= h <= 12 and mi < 60:
        clocks, imp, two = _two(h, mi), False, True
    elif h <= 23 and mi < 60:
        clocks, imp = [(h, mi)], False
    else:
        clocks, imp = [], True
    start = _M(start=m.start(), end=m.end(1) if not m.group(2) else m.end(2), clocks=clocks, two=two, form="Q1",
               never=False, impossible=imp, zone=tm.zone, range_start=True)
    tm.range_end = True
    u.times.insert(0, start)
    u.main = start
    u.start = m.start()
    T.take(m.start(), tm.start)


def _result_zone(unit, steer_zone):
    if unit is not None:
        zs = [t.zone.zid for t in unit.times if t.zone is not None and t.zone.zid]
        if unit.pair:
            non_pacific = [z for z in zs if z != "Pacific"]
            if non_pacific:
                return non_pacific[0]
        if zs:
            return zs[0]
    return steer_zone


def _d5_weekdays(T, dm, paired):
    """D5's weekdays for one unit's date, as (weekday, the mention it was written in): the weekday
    written with that date, and a weekday written on its own (no date and no time of its own) that
    no written date falls on. A weekday written with another date, or going with another time,
    belongs to that one only: it never moves this unit's time to its day. A time moved to a
    weekday written on its own is derived, never one the sender wrote: the caller marks it."""
    dated = [d for d in T.dates if not (d.kind == "relative" and d.weekday is not None)]
    out = []
    for w, d in _weekday_names(T):
        if d is dm:
            out.append((w, d))
        elif d.kind == "relative" and id(d) not in paired and \
                not any(c.weekday() == w for d2 in dated for c in d2.cands):
            out.append((w, d))
    return out


def _unit_readings(T, unit, dm, steer_zone, now, bookable, length, excluded, own_only=False, paired=()):
    out = []
    main = unit.main
    if dm is None:
        return out
    dcands = _date_cands(T, dm, for_offer=not own_only)
    # P2: slashes, a US zone by name, the month-first reading bookable and the day-first not.
    if len(dcands) == 2 and dm.sep == "/" and all(k for _, k, _ in dcands) and main.zone is not None and \
            main.zone.us and not main.zone.your_time:
        try:
            fn = _bookable_fn(bookable)
        except Exception:
            fn = None

        def any_bookable(d):
            return fn is not None and any(
                k and _is_bookable(fn, i, length[0], now) for i, _, k, _ in _time_readings(main, d, steer_zone))
        if any_bookable(dcands[0][0]) and not any_bookable(dcands[1][0]):
            dcands = [dcands[0], (dcands[1][0], False, "P2")]
    moved = {}      # a day D5 reads off a weekday written on its own -> that mention: its readings are derived
    if not own_only:
        names = _d5_weekdays(T, dm, paired)
        base = [c for c, _, _ in dcands]
        for w, src in sorted(names, key=lambda n: n[1] is not dm):     # the date's own weekday first
            for c in list(base):
                if c.weekday() != w:
                    delta = (w - c.weekday()) % 7
                    near = c + _dt.timedelta(days=delta if delta <= 3 else delta - 7)
                    if near not in [x[0] for x in dcands]:
                        dcands.append((near, True, "D5"))
                        if src is not dm:
                            moved[near] = src
    neg = qten = False
    for tm in unit.times:
        n, q = _sentence_flags(T, tm.start)
        neg, qten = neg or n, qten or q
    if dm is not None:
        n, q = _sentence_flags(T, dm.start)
        neg, qten = neg or n, qten or q
    times = unit.times
    for d, dkept, drule in dcands:
        src = moved.get(d)
        # a weekday written on its own and negated or marked as something else (Q5, Q10) moves no time
        dneg, dqten = _sentence_flags(T, src.start) if src is not None else (False, False)
        any_clock = False
        for tm in times:
            if getattr(tm, "range_end", False):
                continue
            for inst, zid, kept, rule in _time_readings(tm, d, steer_zone):
                any_clock = True
                k = dkept and kept
                r = rule
                if not dkept:
                    r = drule
                if not own_only:
                    if neg and inst is not None:
                        excluded.append(inst)
                        k, r = False, "Q5"
                    elif dneg:
                        k, r = False, "Q5"
                    elif qten or dqten:
                        k, r = False, "Q10"
                out.append(Reading(start_utc=inst, date=d, zone=zid, kept=k, rule=r, derived=src is not None))
        if not any_clock:
            out.append(Reading(start_utc=None, date=d, zone=None, kept=dkept, rule=drule, derived=src is not None))
    return out


def _same_instant_pair(T, a, b):
    """Q7: every time has its own zone right after it, one sentence, the same instant on the written date."""
    if a.pair is False and len(a.times) != 1:
        return False
    tb = b.main
    if len(b.times) != 1 or tb.zone is None:
        return False
    if any(t.zone is None or t.zone.zid is None or t.zone.unknown for t in a.times + [tb]):
        return False
    if T.sentence(a.start) != T.sentence(tb.start):
        return False
    gap = T.lt[a.end:tb.start]
    if not re.match(r"^\s*(?:/|\(|\[|,|-|=|\|)?\s*$", gap):
        return False
    dm = _date_for(T, a.main.start, T.sentence(a.main.start))
    if dm is None or not dm.cands:
        return False
    d = dm.cands[0]
    insts = set()
    for t in a.times + [tb]:
        rs = [i for i, _, k, _ in _time_readings(t, d, None) if k and i is not None]
        zids, ok, _, _ = _zone_ids_for(t, d, None)
        if len(rs) != 1 or not ok or len(zids) != 1:
            return False
        insts.add(rs[0])
    return len(insts) == 1


def _side_by_side(T, dm, unit):
    if dm.end <= unit.start:
        gap = T.lt[dm.end:unit.start]
    elif unit.end <= dm.start:
        gap = T.lt[unit.end:dm.start]
    else:
        return False
    return _GAP_OK.match(gap) is not None


def _zones_ok(T, unit, dm):
    if T.zones_free:
        return False
    for tm in unit.times:
        if getattr(tm, "range_end", False) and tm.zone is None:
            continue
        z = tm.zone
        if z is None or z.unknown or z.zid is None:
            return False
        for d in dm.cands[:1] or [T.sent_date]:
            _, ok, _, _ = _zone_ids_for(tm, d, None)
            if not ok:
                return False
    return True


def preferred_starts(parse, now_utc, bookable: Optional[Callable] = None) -> List[_dt.datetime]:
    """The sender's apparent times that are bookable, in the order the offer should put them first.

    Kept readings only, never a time the sender negated (Q5), never one marked as something
    other than the meeting (Q10). The times the sender wrote come first, on any quarter hour. A
    D5 cross-pair (a derived reading) follows them on her grid only, and never leads: with none
    of the sender's own bookable, none is put first. The calendar's busy check is the caller's.
    """
    out = []
    try:
        fn = _bookable_fn(bookable)
    except Exception:
        return out
    excluded = set(getattr(parse, "excluded_utc", []) or [])
    kept = [r for r in getattr(parse, "readings", []) or []
            if r.kept and r.start_utc is not None and r.start_utc not in excluded]
    for derived in (False, True):
        if derived and not out:
            break
        for r in kept:
            if bool(getattr(r, "derived", False)) is derived and r.start_utc not in out and \
                    _is_bookable(fn, r.start_utc, parse.length_min, now_utc, written=not derived):
                out.append(r.start_utc)
    return out


# ---------------------------------------------------------------------------------------
# B11: the model as second reader
# ---------------------------------------------------------------------------------------
def zone_from_text(text, local_date) -> Optional[str]:
    """A zone as written ("PT", "Pacific Time", "UTC-8", "-08:00", "your time") -> zone id, or None."""
    if not isinstance(text, str):
        return None
    s = fold(text)[0].strip().lower()
    s = re.sub(r"\s+", " ", s).strip(" .()")
    if not s:
        return None
    if s in ("your time", "your time zone", "shane's time", "shanes time"):
        return "Pacific"
    if s in _REGION:
        return _REGION[s]
    m = re.match(r"^(pacific|mountain|central|eastern)(?: (standard|daylight))? time(?: zone)?$", s)
    if m:
        zid = _REGION[m.group(1)]
        if m.group(2):
            abbr = _REGION_ABBR[zid] + ("ST" if m.group(2) == "standard" else "DT")
            return zid if zones.abbr_in_effect(abbr, local_date) else None
        return zid
    m = re.match(r"^(?:(utc|gmt) ?)?([+-])(\d{1,2})(?::?(\d{2}))?$", s)
    if m and (m.group(1) or len(m.group(3)) == 2):
        h, mi = int(m.group(3)), int(m.group(4) or 0)
        if h > 14 or mi > 59:
            return None
        return zones.offset_zone((h * 60 + mi) * (1 if m.group(2) == "+" else -1))
    up = s.upper()
    if up == "GMT":
        summer = zones.utc_offset("UK", zones.to_utc(_dt.datetime.combine(local_date, _dt.time(12)), "UTC"))
        return "UTC" if summer == _dt.timedelta(0) else None
    if zones.abbr_zone(up) and zones.abbr_in_effect(up, local_date):
        return zones.abbr_zone(up)
    return None


def model_agrees(parse, answer) -> Tuple[bool, Dict[str, object]]:
    """B11: the model's date, 24-hour time and zone give the parser's instant. Any doubt is no."""
    detail = {"agree": False, "reason": "no_answer", "model_instant": None,
              "parser_instant": _iso(getattr(parse, "instant_utc", None)) if isinstance(parse, ParseResult) else None}
    try:
        return _model_agrees(parse, answer, detail)
    except Exception:
        detail["agree"] = False
        if detail["reason"] == "agree":
            detail["reason"] = "error"
        return False, detail


def _model_agrees(parse, answer, detail):
    if not isinstance(answer, ModelAnswer) or not answer.ok:
        detail["reason"] = "no_answer"
        return False, detail
    if not isinstance(parse, ParseResult) or parse.verdict != "book" or parse.instant_utc is None:
        detail["reason"] = "parse_not_book"
        return False, detail
    if answer.cls != "meeting_request":
        detail["reason"] = "class"
        return False, detail
    if not isinstance(answer.date, str) or not re.match(r"^\d{4}-\d{2}-\d{2}$", answer.date):
        detail["reason"] = "date"
        return False, detail
    try:
        mdate = _dt.date(int(answer.date[:4]), int(answer.date[5:7]), int(answer.date[8:10]))
    except ValueError:
        detail["reason"] = "date"
        return False, detail
    if not isinstance(answer.time24, str) or not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", answer.time24):
        detail["reason"] = "time"
        return False, detail
    mh, mm = int(answer.time24[:2]), int(answer.time24[3:])
    mzone = zone_from_text(answer.zone, mdate)
    if mzone is not None:
        detail["model_instant"] = _iso(zones.to_utc(_dt.datetime.combine(mdate, _dt.time(mh, mm)), mzone))
    written = list(parse.zone_pair) if parse.zone_pair else ([parse.zone] if parse.zone else [])
    if mzone is None or mzone not in written:
        detail["reason"] = "zone"
        return False, detail
    local = zones.to_local(parse.instant_utc, mzone)
    if local.date() != mdate:
        detail["reason"] = "date"
        return False, detail
    if (local.hour, local.minute) != (mh, mm):
        detail["reason"] = "time"
        return False, detail
    if detail["model_instant"] != _iso(parse.instant_utc):
        detail["reason"] = "instant"
        return False, detail
    detail["agree"] = True
    detail["reason"] = "agree"
    return True, detail


# ---------------------------------------------------------------------------------------
# Ruling 6: picks
# ---------------------------------------------------------------------------------------
_ORDINAL_PICK = {"first": 1, "second": 2, "third": 3, "1st": 1, "2nd": 2, "3rd": 3}
_YES = re.compile(r"\b(?:yes|yep|yeah|yup|sounds good|that works|works for me|perfect)\b")


def match_pick(own_text, offer_slots, sender_zone, now) -> PickResult:
    """Ruling 6: which offered slot the reply names, if exactly one. Never raises."""
    try:
        return _match_pick(own_text, offer_slots, sender_zone, now)
    except Exception:
        return PickResult(kind="none")


_RANGE_JOIN = re.compile(r"^\s*(?:-|to|until|till|thru|through)\s*$")
_RANGE_AND = re.compile(r"^\s*and\s*$")
_BETWEEN = re.compile(r"\bbetween\s*$")
_RANGE_NUMBER = re.compile(r"(?<![\w:.$/])(\d{1,2})\s*(?:-|to|until|till|thru|through)\s*$"
                           r"|\bbetween\s+(\d{1,2})\s+and\s*$")
_RANGE_NUMBER_AFTER = re.compile(r"\s*(?:-|to|until|till|thru|through)\s*(\d{1,2})(?![\w:/-]|\.\w)")
_AND_NUMBER_AFTER = re.compile(r"\s*and\s*(\d{1,2})(?![\w:/-]|\.\w)")


def _pick_ranges(T):
    """The time ranges written in a reply ("1pm-2pm", "from 1 to 2pm", "1pm to 2", "between 1 and 2pm"),
    row Q1: ({id of a start: its end}, the ids of the ends, the spans of the bare numbers that start
    or end one)."""
    starts, ends, numbers = {}, set(), set()
    times = sorted(T.times, key=lambda m: m.start)
    for prev, tm in zip([None] + times, times):
        if prev is not None:
            gap = T.lt[max(prev.end, prev.zone.end if prev.zone is not None else 0):tm.start]
            if _RANGE_JOIN.match(gap) or (_RANGE_AND.match(gap)
                                          and _BETWEEN.search(T.lt, max(0, prev.start - _NEAR), prev.start)):
                starts[id(prev)] = tm
                ends.add(id(tm))
                continue
        m = _RANGE_NUMBER.search(T.lt, max(0, tm.start - _NEAR), tm.start)
        g = (1 if m.group(1) else 2) if m else None
        if m and T.free(m.start(g), m.end(g)):
            ends.add(id(tm))
            numbers.add((m.start(g), m.end(g)))
    for tm in times:                # a bare number after the time: "1pm to 2", "between 1pm and 2"
        after = max(tm.end, tm.zone.end if tm.zone is not None else 0)
        m = _RANGE_NUMBER_AFTER.match(T.lt, after)
        if m is None and _BETWEEN.search(T.lt, max(0, tm.start - _NEAR), tm.start):
            m = _AND_NUMBER_AFTER.match(T.lt, after)
        if m and T.free(m.start(1), m.end(1)):
            numbers.add((m.start(1), m.end(1)))
    return starts, ends, numbers


def _range_half(start, end):
    """The clocks of a range's start, in the half of the day its end is written in: "10-11am" starts
    at 10:00 AM, "11-1pm" at 11:00 AM."""
    if not (getattr(start, "two", False) and len(end.clocks) == 1 and not getattr(end, "two", False)):
        return set((h % 24, m) for h, m in start.clocks)
    h, mi = start.clocks[0][0] % 12, start.clocks[0][1]
    pm = end.clocks[0][0] >= 12
    if h > end.clocks[0][0] % 12:
        pm = not pm
    return {(h + (12 if pm else 0), mi)}


_DAY_BEFORE = re.compile(r"\b" + WD + r"[\s,(]*$")
_MONTH_BEFORE = re.compile(r"\b" + MON + r"\s*$")
_MONTH_AFTER = re.compile(r"\s+of\s+" + MON)
_OPTION_NOUN = re.compile(r"\s+(?:one|option|slot|time|choice)\b")


def _is_the_option(lt, m, n, local, shown, spelled):
    """ "the 3rd" is the third option, unless it reads as a day of the month that names another offered
    slot, or none: "Thursday the 3rd", "Thursday the third" and "December the 3rd" are days, and so is
    "the 3rd" when another slot is on the 3rd. Spelled out with no weekday and no month beside it
    ("the third"), it is the option."""
    if _OPTION_NOUN.match(lt, m.end()):
        return True
    near = max(0, m.start() - _NEAR)
    day = _DAY_BEFORE.search(lt, near, m.start())
    month = _MONTH_BEFORE.search(lt, near, m.start()) or _MONTH_AFTER.match(lt, m.end())
    if spelled and not day and not month:
        return True
    wd = WEEKDAYS[day.group(1)] if day else None
    on_that_day = set(i for i, loc in enumerate(local) for z in shown
                      if loc[z].day == n and (wd is None or loc[z].weekday() == wd))
    return on_that_day == {n - 1} if day or month else on_that_day <= {n - 1}


def _match_pick(own_text, offer_slots, sender_zone, now):
    slots = list(offer_slots or [])
    if not slots or not isinstance(own_text, str):
        return PickResult(kind="none")
    text, _, _ = fold(own_text)
    t = _flatten(text)
    shown = ["Pacific", sender_zone if (zones.is_zone(sender_zone) and sender_zone != "Pacific") else "UTC"]
    local = [{z: zones.to_local(s.start_utc, z) for z in shown} for s in slots]
    ref = zones.pacific_date(now) if isinstance(now, _dt.datetime) and now.tzinfo else _dt.date(2000, 1, 1)
    T = _Text(t, ref)
    T.pick_mode = True
    lt = T.lt
    hits = set()
    named = set()           # the slots the reply names when a range is read as its start (row Q1)
    on_day = []             # "the 3rd" read as a day of the month, as day filters
    ambiguous = False
    near = False
    # By number: "option 2", "#2", "the second", "2nd"
    pats = [r"\b(?:option|slot|number|no\.?|choice)\s*#?\s*(\d{1,2})\b", r"#\s*(\d{1,2})\b",
            r"\bthe\s+(first|second|third|1st|2nd|3rd)\b", r"\b(first|second|third|1st|2nd|3rd)\s+(?:one|option|slot|time|choice)\b"]
    for k, pat in enumerate(pats):
        for m in re.finditer(pat, lt):
            if not T.free(m.start(), m.end()):
                continue
            g = m.group(1)
            n = _ORDINAL_PICK.get(g) or int(g)
            T.take(m.start(), m.end())
            if 1 <= n <= len(slots):
                hits.add(n - 1)
                if k != 2 or _is_the_option(lt, m, n, local, shown, spelled=not g[0].isdigit()):
                    named.add(n - 1)
                else:       # a day of the month: the times written with it are times on that day
                    on_day.append((set(loc[z].date() for loc in local for z in shown if loc[z].day == n)
                                   | {_dt.date.min}, None))
    # Times and dates, read with the parser's own forms.
    _extract_dates(T)
    _extract_times(T)
    _extract_relative(T)
    _zone_before_a_relative_date(T)
    day_filter = []
    for d in T.dates:
        day_filter.append((set(d.cands), d.weekday))

    def fits(clocks, zs, days=day_filter):
        return set(i for i in range(len(slots)) for z in zs if z in shown
                   and (local[i][z].hour, local[i][z].minute) in clocks and _day_ok(local[i][z].date(), days))

    range_start, range_end, range_numbers = _pick_ranges(T)
    time_hits_any = False
    for tm in T.times:
        if tm.impossible or not tm.clocks:
            continue
        clocks = set((h % 24, m) for h, m in tm.clocks)
        written_zone = tm.zone.zid if (tm.zone is not None and tm.zone.zid) else None
        zs = [written_zone] if written_zone else shown
        matched = fits(clocks, zs)
        if matched:
            hits |= matched
            # A range names the slot its start names, read in the zone and the half of the day of its
            # end; its end names none. What fits only without that gets the same offer again.
            if id(tm) in range_end:
                matched = set()
            elif id(tm) in range_start:
                end = range_start[id(tm)]
                zone = written_zone or (end.zone.zid if end.zone is not None else None)
                matched = fits(_range_half(tm, end), [zone] if zone else shown, day_filter + on_day)
            elif on_day:
                matched = fits(clocks, zs, day_filter + on_day)
            named |= matched
            time_hits_any = time_hits_any or bool(matched)
            continue
        # near misses: an offered day with a time not offered, or an offered time in a zone that doesn't match
        for i, s in enumerate(slots):
            for z in shown:
                loc = local[i][z]
                if day_filter and _day_ok(loc.date(), day_filter):
                    near = True
                if written_zone and (loc.hour, loc.minute) in clocks:
                    near = True
            if written_zone and zones.is_zone(written_zone):
                loc_w = zones.to_local(s.start_utc, written_zone)
                if (loc_w.hour, loc_w.minute) in clocks and written_zone not in shown:
                    near = True
    # A bare number: a slot number, unless it is also an offered hour.
    for m in re.finditer(r"(?<![\w:.$/#-])([1-9])(?![\w:/-]|\.\w)", lt):
        if not T.free(m.start(), m.end()):
            continue
        n = int(m.group(1))
        is_index = n <= len(slots)
        is_hour = any(loc[z].minute == 0 and (loc[z].hour % 12 or 12) == n for loc in local for z in shown)
        if is_index and is_hour:
            ambiguous = True
        elif is_index:
            hits.add(n - 1)
            if (m.start(1), m.end(1)) not in range_numbers:      # "from 1 to 2pm" is not option 1
                named.add(n - 1)
    if ambiguous or len(hits) > 1:
        return PickResult(kind="ambiguous")
    if len(hits) == 1:
        if not hits <= named:
            return PickResult(kind="near_miss")
        if near and not time_hits_any:
            return PickResult(kind="ambiguous")
        # named by its time, or only by its place in the offer
        return PickResult(kind="matched_slot", slot_index=hits.pop(), by="time" if time_hits_any else "number")
    if near:
        return PickResult(kind="near_miss")
    if _YES.search(lt) and not T.times and not T.dates:
        if len(slots) == 1:
            return PickResult(kind="yes_single", slot_index=0, by="yes")
        return PickResult(kind="ambiguous")
    return PickResult(kind="none")


def _day_ok(d, day_filter):
    for cands, wd in day_filter:
        if cands and d not in cands:
            return False
        if not cands and wd is not None and d.weekday() != wd:
            return False
        if cands and wd is not None and d.weekday() != wd and d in cands:
            return False
    return True
