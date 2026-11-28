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

import base64
import binascii
import re
import unicodedata

from . import caps, templates

# Characters a reader never sees, removed before the 400 characters are counted (decision 61):
# templates.INVISIBLES, the set has_link drops too, and every other format character (Cf) with them.
_INVISIBLES = templates.INVISIBLES
_CONTROLS = "\x00-\x08\x0b\x0c\x0e-\x1f\x7f"
_INVISIBLE = re.compile("[" + _INVISIBLES + "]")
_CONTROL = re.compile("[" + _CONTROLS + "]")
# A character a reader would see: not white space, not invisible, not a control character.
_SEEN = re.compile("[^\\s" + _INVISIBLES + _CONTROLS + "]")

# Defanging is linear (second review of PR #4, round 2: defang("a." * 16000) took 21 s): the scheme
# and host patterns start a match only where a run of their characters starts, so each run is read
# once, and the IPv4 pattern reads at most 15 characters from any place.
# A scheme is the run of scheme characters before "://", from its first letter.
_SCHEME = re.compile(r"(?<![A-Za-z0-9+.\-])([A-Za-z0-9+.\-]+)://")
_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
# A host is a run of letters, digits, "_" and "-" in any script (münchen.de, пример.рф: second Fable
# review of PR #4, L3) with dots between; all its dots are defanged when a label after the first
# starts with a letter and has two characters or more (example.com, but not 3.5 or e.g).
_HOST = re.compile(r"(?<![\w-])[\w-]+(?:\.[\w-]+)+")


def _scheme(m):
    run = m.group(1)
    at = next((i for i, c in enumerate(run) if c.isalpha()), None)
    if at is None:
        return m.group(0)
    name = run[at:]
    low = name.lower()
    if low in ("http", "https"):
        return run[:at] + ("hxxps" if low == "https" else "hxxp") + "://"
    if low in ("hxxp", "hxxps"):
        return m.group(0)
    return run[:at] + name + "[://]"


def _host(m):
    token = m.group(0)
    if any(len(label) >= 2 and label[0].isalpha() for label in token.split(".")[1:]):
        return token.replace(".", "[.]")
    return token


# A fullwidth or ideographic dot between letters or digits (in any script: 例。com) reads as a dot in
# a link, and a fullwidth "@" as an at sign (Fable review of PR #4, finding 10; second review, L3):
# they are defanged like their ASCII forms.
_WIDE_DOT = re.compile("(?<=[^\\W_])[\u3002\uff0e\uff61](?=[^\\W_])")


def defang(text):
    """Every link and address made unclickable. Idempotent."""
    text = _WIDE_DOT.sub(".", text).replace("\uff20", "@").replace("\ufe6b", "@")
    text = _SCHEME.sub(_scheme, text)
    text = text.replace("[@]", "\x00").replace("@", "[@]").replace("\x00", "[@]")
    text = _IPV4.sub(lambda m: m.group(0).replace(".", "[.]"), text)
    return _HOST.sub(_host, text)


def _plain(text):
    """Plain text as a reader sees it: CRLF folded to LF, invisible, format and control characters
    removed."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = _INVISIBLE.sub("", text)
    if not text.isascii():
        text = "".join(c for c in text if unicodedata.category(c) != "Cf")
    return _CONTROL.sub("", text)


# The dotted capital I and the dotless small i, which NFKC keeps and a case-blind match reads as "i"
# (second review of PR #4, round 2, L4).
_I = {0x130: "I", 0x131: "i"}


def _fold(text):
    """Plain text as the rules read it: compatibility forms folded (NFKC: fullwidth letters to ASCII,
    the long s to s, the Kelvin sign to K, so the rules read what a person reads; second Fable review
    of PR #4, M2), and the dotted I and dotless i read as I and i. She echoes the text as the sender
    wrote it, unfolded, unless a code in it is blanked (second review of PR #4, round 2, L6)."""
    if text.isascii():
        return text
    return _INVISIBLE.sub("", unicodedata.normalize("NFKC", text).translate(_I))


def clean(text):
    """_plain(text), folded."""
    return _fold(_plain(text))


# --- codes (decision 77; Fable review of PR #4, findings 3 to 5) ----------------------------------
# A code (a key, token, passphrase, PIN or tracking code) is never repeated in anything she sends.
# Text is read as runs of letters, digits and "_", "+", "/", "=" joined by hyphens; a hyphen may be
# followed by a space or a line break and the run goes on, as the Trap Pack's canary scan allows.
# A run is split into its parts (letters and digits only). A run is blanked whole when:
#   - one part is code-shaped: 4 or more characters, letters and digits, starting with a digit or
#     switching between letters and digits at least twice (7QXZ, Q7XX, a1b2c3, 9f86d081);
#   - it has 3 or more parts and one mixes letters and digits (tag-abc-t002), or any three parts in
#     a row have no lowercase letter and one of them has 2 letters or more (ABC-DEF-WXYZ, and
#     ref=ABC-DEF-1234 or x/ABC-DEF-WXYZ: the words around a code don't save it; second Fable
#     review of PR #4, H1);
# three or more mixed groups in a row are blanked together (AB12 CD34 EF56, A1B2:C3D4:E5F6); a line of
# base64 text is blanked (below); and the
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


def _capitals(parts):
    """Three parts in a row with no lowercase letter, one of them with 2 letters or more."""
    upper = [not any(c.islower() for c in p) for p in parts]
    wordy = [sum(c.isalpha() for c in p) >= 2 for p in parts]
    return any(all(upper[i:i + 3]) and any(wordy[i:i + 3]) for i in range(len(parts) - 2))


def _run(m):
    run = m.group(0)
    parts = [p for p in _PARTS.split(run) if p]
    if any(_code_shaped(p) for p in parts):
        return REDACTED
    if len(parts) >= 3 and (any(_mixed(p) for p in parts) or _capitals(parts)):
        return REDACTED
    return run


def _groups(m):
    return REDACTED if all(_mixed(g) for g in re.split(r"[ :]", m.group(0))) else m.group(0)


# Base64 text (second Fable review of PR #4, M2, and its second round): a run of 8 or more base64
# characters, capitals and small letters both, that decodes to printable text at some alignment (0 to
# 3 leading characters dropped): the whole of it, if that is 6 bytes or more, or any 9 bytes in a row
# of it, so bytes that are not text beside a code don't save it. Each line is judged alone, so a line
# wrap doesn't save the first line. A run of letters only must give 9 bytes (12 characters), or
# ordinary words would go (Eventually, Installs and Visibility each decode to 6 printable bytes). A
# run is read with any "=" in it or after it and blanked whole, so padding and what follows it
# (aGVsbG8...==1, aGVsbG8...=x) don't save it either. It is also read piece by piece, split at "=",
# "+" and "/" (key=..., billing+ and a base64 code).
_B64 = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/=]{8,}(?![A-Za-z0-9+/=])")
_TEXT_BYTES = bytes(range(32, 127)) + b"\t\n\r"
_TEXT_RUN = re.compile(rb"[\x20-\x7e\t\n\r]{9}")


def _base64_text(run):
    pieces = [p for p in re.split(r"=+", run) if p]
    pieces += [p for piece in pieces if "+" in piece or "/" in piece for p in re.split(r"[+/]", piece)]
    for piece in pieces:
        if len(piece) < 8 or not any(c.isupper() for c in piece) or not any(c.islower() for c in piece):
            continue
        need = 9 if piece.isalpha() else 6
        for off in range(4):
            chunk = piece[off:]
            chunk = chunk[:len(chunk) - len(chunk) % 4]
            if len(chunk) // 4 * 3 < need:
                break
            try:
                data = base64.b64decode(chunk)
            except (binascii.Error, ValueError):
                continue
            if not data.translate(None, _TEXT_BYTES) or _TEXT_RUN.search(data):
                return True
    return False


def _b64(m):
    return REDACTED if _base64_text(m.group(0)) else m.group(0)


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
    text = _B64.sub(_b64, text)
    return _RUN.sub(_run, text)


# --- how far a value is read (second Fable review of PR #4, H2) -----------------------------------
SCAN_FACTOR = 5          # a value is read at most 5 times as far as it may show (the quote: 2,000)
SUBJECT_CHARS = 998      # the subject her reply echoes is read this far (RFC 5322's line length)
ADDRESS_CHARS = 1000     # an address longer than this is never shown (a real one is 254 at most)
_GROUP_WORD = re.compile(_GROUP + r"(?::" + _GROUP + r")*$")


def _window(text, n, one_line=False):
    """_plain(text), folded to one line if asked, at most n characters of it (the raw text is read at
    most 4n far). A cut may split a code, so where the text is cut its last word, and every word a
    code joins to it (one ending in a hyphen, a group of a grouped code, read folded), become one
    [redacted]. Invisibles and white space at the end are no cut (second review of PR #4, round 2,
    L5): the text is cut only where a reader would see more after it."""
    text = text or ""
    cut = len(text) > 4 * n and _SEEN.search(text, 4 * n) is not None
    text = _plain(text[:4 * n])
    if one_line:
        text = " ".join(text.split())
    if not cut and len(text.rstrip()) <= n:
        return text if len(text) <= n else text.rstrip()
    head = text[:n]
    end, last = len(head), True
    while True:
        j = end
        while j and head[j - 1].isspace():
            j -= 1
        i = j
        while i and not head[i - 1].isspace():
            i -= 1
        word = _fold(head[i:j])
        if not word or not (last or word.endswith("-") or _GROUP_WORD.match(word)):
            return head[:end] + REDACTED
        end, last = i, False


def _fit(text, limit):
    """The first characters of text, defanged, at most limit of them. Defanging adds brackets, so
    source characters are given up, in proportion, until the defanged text fits. Returns (shown,
    the source characters it shows)."""
    n = limit
    out = defang(text[:n]).rstrip()
    while len(out) > limit:
        n = max(0, min(n - 1, n * limit // len(out)))
        out = defang(text[:n]).rstrip()
    return out, text[:n]


def safe_excerpt(text, limit, one_line=False):
    """Inbound text made safe to send: invisibles and control characters removed (folded to one
    line if asked), read only so far (_window), the run's meeting link made "[meeting link]", codes
    blanked, the first `limit` characters kept and defanged. It is echoed as written if no code in it
    is blanked, folding shows no meeting link, and it defangs as its folded form does; otherwise
    folded (a fullwidth link: hxxps://...). The link goes before any cut, so it is never shown whole
    or cut in half: what she echoes reaches receipts (a quote) and recorded answers (a question)."""
    text = templates.redact_link(_window(text, limit * SCAN_FACTOR, one_line)).strip()
    folded = _fold(text)
    blanked = blank_codes(templates.redact_link(folded))     # the link in wide letters folds to the link
    if blanked == folded and text != folded:
        out, source = _fit(text, limit)
        if _fold(out) == defang(_fold(source)).rstrip():
            return out
    return _fit(blanked, limit)[0]


def shown_subject(subject):
    """The sender's subject as her reply echoes it: plain text, read at most SUBJECT_CHARS far, every
    code in it blanked (Fable review of PR #4, finding 1), as written if none is."""
    text = _window(subject, SUBJECT_CHARS)
    folded = _fold(text)
    blanked = blank_codes(folded)
    return text if blanked == folded else blanked


def shown_address(addr):
    """The sender's address as she shows it to the owner: as written, unless a code is in it, which
    is blanked as in the quote (Fable review of PR #4, finding 2). An address the notice can't carry
    as one address on one line (too long once blanked, no dot in its domain, a quoted or bracketed
    part) shows as [redacted], so the notice and the digest always go (second review, M1)."""
    addr = addr or ""
    if not addr:
        return ""
    if len(addr) > 4 * ADDRESS_CHARS:
        return REDACTED
    addr = clean(addr)
    if len(addr) > ADDRESS_CHARS:
        return REDACTED
    local, at, domain = addr.rpartition("@")
    shown = blank_codes(local) + "@" + blank_codes(domain) if at else blank_codes(addr)
    return shown if templates.shown_address_ok(shown) else REDACTED


def sender_name(from_name, from_addr):
    """How a notice names the sender: the display name made safe, else the shown address, else the
    address made safe as a line (200 characters), else [redacted]."""
    shown = _name_line(from_name)
    if not shown:
        shown = shown_address(from_addr)
        if not shown or shown == REDACTED or len(shown) > 200:
            shown = _name_line(from_addr, 200)
    return shown if templates.line_ok(shown) else REDACTED


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
    addr = msg.from_addr or ""
    first = shown.split()[0] if shown else _name_line(addr.rpartition("@")[0] or addr)
    address = shown_address(addr)
    # a value the template can't carry shows as [redacted]; the notice itself always goes (M1)
    return {
        "name": sender_name(msg.from_name, addr),
        "address": address if templates.shown_address_ok(address) else REDACTED,
        "reason": reason_code,
        "wrote_at": wrote_at,
        "quote": quote_of(msg),
        "first_name": first if templates.line_ok(first, 80) else REDACTED,
        "draft_slots": list(draft_slots),
        "zone": zone,
    }
