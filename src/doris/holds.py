"""The owner's hold notice (S1): its slots, the quote of what the sender wrote, and defanging.

The quote is attacker-controlled text arriving from a sender the owner trusts, so it goes as
plain text only, with every link and address defanged (hxxp://, example[.]com,
name[@]example[.]com) so nothing in it is clickable (templates.md S1, decision 10). It is never
acted on. It is the sender's own visible words only, never hidden text, and every code, key,
passphrase or token in it is blanked (decision 77): a notice is a send, and Trap Pack run 1 put
two leak canaries in notices by quoting hidden text. The draft is the approved S1 draft with a
gap for Shane: no model-written prose.
"""

import re

from . import caps, templates

# Zero-width and direction characters, removed before the 400 characters are counted (decision 61).
_INVISIBLE = re.compile("[​-‏‪-‮⁠-⁤⁦-⁩﻿]")
_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_SCHEME = re.compile(r"(?i)\b(https?|[a-z][a-z0-9+.\-]*)://")
_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_HOST = re.compile(r"(?i)\b(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z][a-z0-9-]*[a-z0-9]\b")


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


def defang(text):
    """Every link and address made unclickable. Idempotent."""
    text = _SCHEME.sub(_scheme, text)
    text = text.replace("[@]", "\x00").replace("@", "[@]").replace("\x00", "[@]")
    text = _IPV4.sub(lambda m: m.group(0).replace(".", "[.]"), text)
    return _HOST.sub(_host, text)


def clean(text):
    """Plain text: CRLF folded to LF, invisible and control characters removed."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    return _CONTROL.sub("", _INVISIBLE.sub("", text))


# A run of 8 or more letters, digits, "-", "_", "+", "/" or "=" holding both a letter and a digit
# is a code (a key, token, passphrase or tracking code) and is never repeated (decision 77).
# Plain words, dates, times, phone numbers and amounts have no such run and stay as written.
_CODE_RUN = re.compile(r"[A-Za-z0-9_+/=-]{8,}")
REDACTED = "[redacted]"


def blank_codes(text):
    """Every code-shaped run replaced by [redacted]."""
    def one(m):
        run = m.group(0)
        return REDACTED if any(c.isdigit() for c in run) and any(c.isalpha() for c in run) else run
    return _CODE_RUN.sub(one, text)


def safe_excerpt(text, limit, one_line=False):
    """Inbound text made safe to send: invisibles and control characters removed (folded to one
    line if asked), codes blanked, the first `limit` characters kept and defanged. Defanging adds
    brackets, so source characters are given up until the defanged text fits `limit` too."""
    text = clean(text)
    if one_line:
        text = " ".join(text.split())
    text = blank_codes(text.strip())
    n = limit
    out = defang(text[:n]).rstrip()
    while len(out) > limit:
        n -= len(out) - limit
        out = defang(text[:n]).rstrip()
    return out


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
    name = shown or msg.from_addr
    first = shown.split()[0] if shown else defang(msg.from_addr.split("@")[0])
    return {
        "name": name,
        "address": msg.from_addr,
        "reason": reason_code,
        "wrote_at": wrote_at,
        "quote": quote_of(msg),
        "first_name": first,
        "draft_slots": list(draft_slots),
        "zone": zone,
    }
