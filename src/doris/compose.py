"""The outgoing .eml: her replies to a sender (T templates) and her notices to the owner (S1, S2).

Every message is built by hand, header by header, so identical inputs give identical bytes on
every system (the Date and Message-ID are injected). CRLF line endings throughout. The body is
the rendered template plus the locked signature, appended here byte for byte (locked call 10).

Recipients: a reply goes To the sender only (the bare From address of the message answered).
The owner's blind copy rides on the envelope (reply_envelope), never in a Bcc header, which a
raw SMTP hand-off would show the prospect. Owner notices go To the owner only, no copy.
"""

import base64
import datetime as _dt
import email.header
import email.utils
import hashlib
import quopri
import re

from . import holds, templates
from .shapes import UTC

# Locked call 10 in the law. templates.md shows the same two lines without the period after
# "Machines"; the law wins (named in the voice builder's report for Shane to confirm).
SIGNATURE = ("Doris, AI assistant to Shane Bornstein, Hire the Machines.\n"
             "I'm an AI, not a person. Reply \"stop\" to opt out.")
_SIG_TAIL = "\n\n" + SIGNATURE


class ComposeError(ValueError):
    """The message cannot be composed as the rules require (nothing is sent)."""


def new_message_id(doris_addr, inbound_key, template_id, n=1):
    """Her deterministic Message-ID (CONTRACTS.md, 10:35 PT; decision 66)."""
    digest = hashlib.sha256(("%s|%s|%s" % (inbound_key, template_id, n)).encode("utf-8")).hexdigest()[:32]
    return "<" + digest + "@" + doris_addr.split("@")[-1] + ">"


def digest_key(now):
    """The inbound key for the day's digest: 'digest:YYYY-MM-DD' (the Pacific date)."""
    local = templates._local(now, "Pacific")
    return "digest:%04d-%02d-%02d" % (local.year, local.month, local.day)


def signature_ok(body_text):
    """G8: the body ends with the two locked lines, byte for byte, and carries them once."""
    if not isinstance(body_text, str):
        return False
    return body_text.endswith(_SIG_TAIL) and body_text.count(SIGNATURE) == 1


def with_signature(text):
    return text + _SIG_TAIL


def reply_envelope(msg, owner_addr):
    """The envelope of a reply: the sender, then the owner's blind copy (locked call 9)."""
    if not msg.from_addr or "@" not in msg.from_addr:
        raise ComposeError("no sender address to reply to")
    if not owner_addr or "@" not in owner_addr:
        raise ComposeError("no owner address in config")
    return [msg.from_addr, owner_addr]


# --- header helpers --------------------------------------------------------------------------
_ONE_LINE = re.compile(r"[\r\n\t]+")


def _clean(value):
    return _ONE_LINE.sub(" ", value or "").strip()


def _header_value(value):
    value = _clean(value)
    try:
        value.encode("ascii")
        return value
    except UnicodeEncodeError:
        return email.header.Header(value, "utf-8").encode(linesep="\r\n")


def _msgid(value):
    """A Message-ID as one header token, or None: printable ASCII only (RFC 5322), so one stray byte
    in a sender's header is left out of hers instead of stopping the reply."""
    v = (value or "").strip().strip("<>").strip()
    if not v or any(c in "<>" or not "!" <= c <= "~" for c in v):
        return None
    return "<" + v + ">"


def _date(date):
    if not isinstance(date, _dt.datetime) or date.tzinfo is None:
        raise ComposeError("the Date must be an aware datetime")
    return email.utils.format_datetime(date.astimezone(UTC))


def _addr(value, what):
    if (not value or "@" not in value or any(c in value for c in "<>,;:\r\n \t\"()[]\\")
            or any(not "!" <= c <= "~" for c in value)):
        raise ComposeError("%s must be one bare ASCII address" % (what,))
    return value


def _qp(text):
    data = quopri.encodestring(text.replace("\r\n", "\n").encode("utf-8"), quotetabs=False)
    return data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


def _ics_method(ics):
    for line in ics.replace(b"\r\n", b"\n").split(b"\n"):
        if line.upper().startswith(b"METHOD:"):
            return line[7:].strip().decode("ascii", "replace").upper()
    return None


def _build(headers, body_text, ics, method, boundary_seed):
    lines = ["%s: %s" % (k, v) for k, v in headers]
    lines.append("MIME-Version: 1.0")
    text_headers = ['Content-Type: text/plain; charset="utf-8"', "Content-Transfer-Encoding: quoted-printable"]
    text_part = _qp(body_text + "\n")
    if ics is None:
        head = "\r\n".join(lines + text_headers).encode("ascii")
        return head + b"\r\n\r\n" + text_part
    boundary = "=_doris_" + hashlib.sha256(boundary_seed.encode("utf-8")).hexdigest()[:24]
    lines.append('Content-Type: multipart/mixed; boundary="%s"' % boundary)
    ics_b64 = base64.encodebytes(ics).replace(b"\n", b"\r\n")
    out = "\r\n".join(lines).encode("ascii") + b"\r\n\r\n"
    out += ("--%s\r\n" % boundary).encode("ascii")
    out += "\r\n".join(text_headers).encode("ascii") + b"\r\n\r\n" + text_part
    out += ("\r\n--%s\r\n" % boundary).encode("ascii")
    out += ('Content-Type: text/calendar; charset="utf-8"; method=%s\r\n'
            'Content-Disposition: attachment; filename="call.ics"\r\n'
            "Content-Transfer-Encoding: base64\r\n\r\n" % method).encode("ascii")
    out += ics_b64
    out += ("\r\n--%s--\r\n" % boundary).encode("ascii")
    return out


def reply(msg, template_id, slots, doris_addr, message_id, date, ics=None):
    """Her reply to the sender, on their thread. Returns (eml bytes, body_text with signature).

    The .ics part goes only on a template that carries one (T1 PUBLISH, T10 and T18 CANCEL),
    and its METHOD line must match. Raises TemplateError or ComposeError; nothing half-built."""
    if template_id not in templates.PROSPECT_TEMPLATES:
        if template_id in templates.APPROVED:
            raise ComposeError("%s goes to the owner: use owner_notice" % (template_id,))
        raise templates.TemplateError("not an approved template: %r" % (template_id,))
    want = templates.ICS_METHOD.get(template_id)
    if want is None and ics is not None:
        raise ComposeError("%s carries no calendar file" % (template_id,))
    if want is not None:
        if not isinstance(ics, (bytes, bytearray)) or not ics:
            raise ComposeError("%s needs its calendar file" % (template_id,))
        if _ics_method(bytes(ics)) != want:
            raise ComposeError("%s needs METHOD:%s in its calendar file" % (template_id, want))
    body_text = with_signature(templates.render(template_id, slots))
    mid = _msgid(message_id)
    if mid is None:
        raise ComposeError("bad Message-ID")
    # the sender's own subject, echoed with any code in it blanked (Fable review of PR #4, finding 1);
    # In-Reply-To and References keep their Message-IDs as written, which threading needs
    subject = _clean(holds.blank_codes(holds.clean(msg.subject)))
    if not re.match(r"(?i)^re\s*:", subject):
        subject = ("Re: " + subject).rstrip()
    headers = [("From", _addr(doris_addr, "her address")), ("To", _addr(msg.from_addr, "the sender")),
               ("Subject", _header_value(subject)), ("Date", _date(date)), ("Message-ID", mid)]
    parent = _msgid(msg.message_id) or (_msgid(msg.key) if not msg.key.startswith("sha256:") else None)
    if parent:
        refs = []
        for r in list(msg.references) + [parent]:
            r = _msgid(r)
            if r and r not in refs:
                refs.append(r)
        headers.append(("In-Reply-To", parent))
        headers.append(("References", " ".join(refs)))
    headers.append(("Auto-Submitted", "auto-replied"))
    return _build(headers, body_text, bytes(ics) if want else None, want, mid), body_text


def owner_notice(template_id, slots, doris_addr, owner_addr, message_id, date):
    """S1 (hold notice) or S2 (digest) to the owner's address only. Returns (eml bytes, body_text)."""
    if template_id not in ("S1", "S2"):
        raise ComposeError("only S1 and S2 go to the owner")
    body_text = with_signature(templates.render(template_id, slots))
    if template_id == "S1":
        subject = "Held for you: %s" % _clean(slots["name"])
    else:
        subject = body_text.split("\n", 1)[0]
    mid = _msgid(message_id)
    if mid is None:
        raise ComposeError("bad Message-ID")
    headers = [("From", _addr(doris_addr, "her address")), ("To", _addr(owner_addr, "the owner")),
               ("Subject", _header_value(subject)), ("Date", _date(date)), ("Message-ID", mid),
               ("Auto-Submitted", "auto-replied")]
    return _build(headers, body_text, None, None, mid), body_text
