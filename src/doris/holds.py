"""The owner's hold notice (S1): its slots, the quote of what the sender wrote, and defanging.

The quote is attacker-controlled text arriving from a sender the owner trusts, so it goes as
plain text only, with every link and address defanged (hxxp://, example[.]com,
name[@]example[.]com) so nothing in it is clickable (templates.md S1, decision 10). It is never
acted on. It is the sender's own visible words only, never hidden text, and every code, key,
passphrase or token in it is blanked (decision 77): a notice is a send, and Trap Pack run 1 put
two leak canaries in notices by quoting hidden text. The sender's name and address are made safe
the same way wherever she shows them, and so is the subject she echoes in a reply. The draft is
the approved S1 draft with a gap for Shane: no model-written prose.
"""

import re
import unicodedata

from . import caps, templates

# Characters a reader never sees, removed before the 400 characters are counted (decision 61):
# Unicode's default-ignorable code points (zero-width, direction and format characters, the soft
# hyphen, fillers, variation selectors, tags), and every other format character (Cf) with them.
_INVISIBLE = re.compile(
    "[\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f\u200b-\u200f\u202a-\u202e"
    "\u2060-\u206f\u3164\ufe00-\ufe0f\ufeff\uffa0\ufff0-\ufff8\U0001bca0-\U0001bca3"
    "\U0001d173-\U0001d17a\U000e0000-\U000e0fff]")
_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_SCHEME = re.compile(r"(?i)\b(https?|[a-z][a-z0-9+.\-]*)://")
_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
# Labels are bounded at 63 characters, the most DNS allows, so no label is read more than once over.
_HOST = re.compile(r"(?i)\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]\b")


def _scheme(m):
    name = m.group(1)
    low = name.lower()
    if low in ("http", "https"):
        return ("hxxps" if low == "https" else "hxxp") + "://"
    if low in ("hxxp", "hxxps"):
        return m.group(0)
    return name + "[://]"


def _host(m):
    token = m.group(0)
    labels = token.split(".")
    if not any(c.isalpha() for c in labels[-1]) or len(labels[-1]) < 2:
        return token
    return token.replace(".", "[.]")


# A fullwidth or ideographic dot between letters reads as a dot in a link, and a fullwidth "@" as
# an at sign (Fable review of PR #4, finding 10): they are defanged like their ASCII forms.
_WIDE_DOT = re.compile("(?<=[A-Za-z0-9])[\u3002\uff0e\uff61](?=[A-Za-z0-9])")


def defang(text):
    """Every link and address made unclickable. Idempotent."""
    text = _WIDE_DOT.sub(".", text).replace("\uff20", "@").replace("\ufe6b", "@")
    text = _SCHEME.sub(_scheme, text)
    text = text.replace("[@]", "\x00").replace("@", "[@]").replace("\x00", "[@]")
    text = _IPV4.sub(lambda m: m.group(0).replace(".", "[.]"), text)
    return _HOST.sub(_host, text)


def clean(text):
    """Plain text: CRLF folded to LF, invisible, format and control characters removed."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = _INVISIBLE.sub("", text)
    if not text.isascii():
        text = "".join(c for c in text if unicodedata.category(c) != "Cf")
    return _CONTROL.sub("", text)


# --- codes (decision 77; Fable review of PR #4, findings 3 to 5) ----------------------------------
# A code (a key, token, passphrase, PIN or tracking code) is never repeated in anything she sends.
# Text is read as runs of letters, digits and "_", "+", "/", "=" joined by hyphens; a hyphen may be
# followed by a space or a line break and the run goes on, as the Trap Pack's canary scan allows.
# A run is split into its parts (letters and digits only). A run is blanked whole when:
#   - one part is code-shaped: 4 or more characters, letters and digits, starting with a digit or
#     switching between letters and digits at least twice (7QXZ, Q7XX, a1b2c3, 9f86d081);
#   - it has 3 or more parts and one mixes letters and digits (tag-abc-t002), or none has a
#     lowercase letter and one has 2 letters or more (ABC-DEF-WXYZ);
# three or more mixed groups in a row are blanked together (AB12 CD34 EF56, A1B2:C3D4:E5F6); and the
# word after password, passphrase, passcode, PIN, OTP, a named code or key (verification code, API
# key), or after "code", "key", "token" or "secret" with ":", "=" or "is", is blanked when it holds a
# digit or three or more hyphenated words. Times, dates, ordinals, amounts with units (10am, 30th,
# 6weeks, 60fps), ISO times (01T15), plain words, names with a number (jsmith2026, FY27) and
# numbers alone stay as written.
# Every pattern runs in linear time: the whitespace beside a keyword is bounded (32 characters) and
# no two unbounded runs of the same characters meet (second Fable review of PR #4, H2: "The key is",
# 60,000 spaces and "!" took 84 seconds). Each caller also reads a value only as far as it can show
# it (_window).
REDACTED = templates.REDACTED
_RUN = re.compile(r"[A-Za-z0-9_+/=]+(?:-+\s*[A-Za-z0-9_+/=]+)*")
_PARTS = re.compile(r"[-_+/=\s]+")
_UNIT = re.compile(r"(?i)^\d+(?:st|nd|rd|th|am|pm|a|p|h|hrs?|hours?|m|mins?|minutes?|s|secs?|ms|d|days?|w|wks?|"
                   r"weeks?|mos?|months?|y|yrs?|years?|k|kg|km|mi|ft|in|x|g|gb|mb|kb|tb|px|pt|mm|cm|ml|l|lbs?|"
                   r"oz|kwh?|v|hz|ghz|mhz|fps|mph|kph|pcs?|bn|mn|c|f|z|i)$")
_ISO = re.compile(r"(?i)^(?:\d{1,2}|\d{8})t\d{2}(?:\d{2}){0,2}z?$")
_GROUP = r"(?=[A-Za-z0-9]*[A-Za-z])(?=[A-Za-z0-9]*[0-9])[A-Za-z0-9]{3,12}"
_GROUPS = re.compile(r"(?<![A-Za-z0-9_+/=-])" + _GROUP + r"(?:[ :]" + _GROUP + r"){2,}(?![A-Za-z0-9_+/=-])")
_WEAK = r"codes?|keys?|tokens?|secrets?"
_SP = r"\s{1,32}"
_SECRET = re.compile(
    r"(?i)\b(?P<k>pass(?:word|phrase|code)s?|passwd|pins?|otp|"
    r"(?:pin|access|security|verification|login|one-time|confirmation|door|gate|alarm|entry)" + _SP + r"codes?|"
    r"(?:api|access|secret|private|license|product)" + _SP + r"keys?|" + _WEAK + r")\b"
    r"(?P<c>\s{0,32}[:=#]\s{0,32}|" + _SP + r"(?:is|was|are|were)(?:\s{0,32}:)?" + _SP + r"|" + _SP + r")"
    r"(?P<v>\d+(?:[ .-]\d+)+(?![A-Za-z0-9])|[A-Za-z0-9_+/=]+(?:-+\s{0,32}[A-Za-z0-9_+/=]+)*)")
_WEAK_WORD = re.compile(r"(?i)^(?:" + _WEAK + r")$")


def _mixed(part):
    return (len(part) >= 3 and any(c.isdigit() for c in part) and any(c.isalpha() for c in part)
            and not _UNIT.match(part) and not _ISO.match(part))


def _code_shaped(part):
    if not _mixed(part) or len(part) < 4:
        return False
    shifts = sum(1 for a, b in zip(part, part[1:]) if a.isdigit() != b.isdigit())
    return shifts >= 2 or part[0].isdigit()


def _run(m):
    run = m.group(0)
    parts = [p for p in _PARTS.split(run) if p]
    if any(_code_shaped(p) for p in parts):
        return REDACTED
    if len(parts) >= 3 and (any(_mixed(p) for p in parts) or (
            not any(c.islower() for c in run) and any(sum(c.isalpha() for c in p) >= 2 for p in parts))):
        return REDACTED
    return run


def _groups(m):
    return REDACTED if all(_mixed(g) for g in re.split(r"[ :]", m.group(0))) else m.group(0)


def _secret(m):
    named = not _WEAK_WORD.match(m.group("k")) or m.group("c").strip()
    v = m.group("v")
    if named and (any(c.isdigit() for c in v) or len(re.findall(r"[-_]", v)) >= 2):
        return m.group(0)[:m.start("v") - m.start()] + REDACTED
    return m.group(0)


def blank_codes(text):
    """Every code in the text replaced by [redacted] (the rules above)."""
    text = _SECRET.sub(_secret, text)
    text = _GROUPS.sub(_groups, text)
    return _RUN.sub(_run, text)


# --- how far a value is read (second Fable review of PR #4, H2) -----------------------------------
SCAN_FACTOR = 5          # a value is read at most 5 times as far as it may show (the quote: 2,000)
SUBJECT_CHARS = 998      # the subject her reply echoes is read this far (RFC 5322's line length)
ADDRESS_CHARS = 1000     # an address longer than this is never shown (a real one is 254 at most)
_GROUP_WORD = re.compile(_GROUP + r"(?::" + _GROUP + r")*$")


def _window(text, n, one_line=False):
    """clean(text), folded to one line if asked, at most n characters of it (the raw text is read at
    most 4n far). A cut may split a code, so where the text is cut its last word, and every word a
    code joins to it (one ending in a hyphen, a group of a grouped code), become one [redacted]."""
    text = text or ""
    cut = len(text) > 4 * n
    text = clean(text[:4 * n])
    if one_line:
        text = " ".join(text.split())
    if not cut and len(text) <= n:
        return text
    head = text[:n]
    end, last = len(head), True
    while True:
        j = end
        while j and head[j - 1].isspace():
            j -= 1
        i = j
        while i and not head[i - 1].isspace():
            i -= 1
        word = head[i:j]
        if not word or not (last or word.endswith("-") or _GROUP_WORD.match(word)):
            return head[:end] + REDACTED
        end, last = i, False


def safe_excerpt(text, limit, one_line=False):
    """Inbound text made safe to send: invisibles and control characters removed (folded to one
    line if asked), read only so far (_window), codes blanked, the first `limit` characters kept and
    defanged. Defanging adds brackets, so source characters are given up, in proportion, until the
    defanged text fits."""
    text = blank_codes(_window(text, limit * SCAN_FACTOR, one_line).strip())
    n = limit
    out = defang(text[:n]).rstrip()
    while len(out) > limit:
        n = max(0, min(n - 1, n * limit // len(out)))
        out = defang(text[:n]).rstrip()
    return out


def shown_subject(subject):
    """The sender's subject as her reply echoes it: plain text, read at most SUBJECT_CHARS far, every
    code in it blanked (Fable review of PR #4, finding 1)."""
    return blank_codes(_window(subject, SUBJECT_CHARS))


def shown_address(addr):
    """The sender's address as she shows it to the owner: as written, unless a code is in it, which
    is blanked as in the quote (Fable review of PR #4, finding 2). An address too long to be real
    is never read: it shows as [redacted]."""
    addr = addr or ""
    if len(addr) > 4 * ADDRESS_CHARS:
        return REDACTED
    addr = clean(addr)
    if len(addr) > ADDRESS_CHARS:
        return REDACTED
    local, at, domain = addr.rpartition("@")
    if not at:
        return blank_codes(addr)
    return blank_codes(local) + "@" + blank_codes(domain)


def sender_name(from_name, from_addr):
    """How a notice names the sender: the display name made safe, else the shown address (fitted to
    the name's 200 characters)."""
    shown = _name_line(from_name)
    if shown:
        return shown
    address = shown_address(from_addr)
    return address if len(address) <= 200 else _name_line(address, 200)


def quote_of(msg):
    """The S1 quote: the sender's own visible words, never hidden text, made safe to send and at
    most 400 characters (decisions 61 and 77)."""
    return safe_excerpt(msg.own_text or "", caps.HOLD_QUOTE_CHARS)


def _name_line(text, limit=80):
    return safe_excerpt(text or "", limit, one_line=True)


def notice_slots(msg, reason_code, wrote_at, draft_slots, zone=None):
    """Slots for templates.render("S1", ...). draft_slots: 0 to 3 aware UTC datetimes computed by
    code (slots as T2); zone: the sender's zone id for their brackets, or None (UTC).
    Raises TemplateError for a code that sends no notice (hold.thread_held)."""
    if reason_code in templates.NO_NOTICE:
        raise templates.TemplateError("%s sends no hold notice" % (reason_code,))
    shown = _name_line(msg.from_name)
    address = shown_address(msg.from_addr)
    first = shown.split()[0] if shown else _name_line(address.rpartition("@")[0] or address)
    return {
        "name": sender_name(msg.from_name, msg.from_addr),
        "address": address,
        "reason": reason_code,
        "wrote_at": wrote_at,
        "quote": quote_of(msg),
        "first_name": first,
        "draft_slots": list(draft_slots),
        "zone": zone,
    }
