"""The owner's hold notice (S1): its slots, the quote of what the sender wrote, and defanging.

The quote is attacker-controlled text arriving from a sender the owner trusts, so it goes as
plain text only, with every link and address defanged (hxxp://, example[.]com,
name[@]example[.]com) so nothing in it is clickable (templates.md S1, decision 10). It is never
acted on. The draft is the approved S1 draft with a gap for Shane: no model-written prose.
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


def quote(msg, reason_code):
    """The text that caused the hold (the hidden text for hold.hidden_text, else the sender's
    own visible text), first 400 characters after invisibles are removed and the run's meeting link
    is "[meeting link]" (so it is never quoted, whole or cut in half: the quote goes in a receipt),
    then defanged."""
    source = msg.hidden_text if reason_code == "hold.hidden_text" and msg.hidden_text else msg.own_text
    return defang(templates.redact_link(clean(source)).strip()[:caps.HOLD_QUOTE_CHARS])


def _name_line(text, limit=80):
    one = " ".join(templates.redact_link(clean(text)).split())
    return defang(one[:limit]).strip()


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
        "quote": quote(msg, reason_code),
        "first_name": first,
        "draft_slots": list(draft_slots),
        "zone": zone,
    }
