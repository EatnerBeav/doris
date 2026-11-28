"""The live adapters: IMAP for her own mailbox, her sends over JMAP, CalDAV for the owner's calendar.

The law: live mail is sent via Fastmail JMAP over HTTPS (DigitalOcean blocks SMTP). LiveMail reads
and files over IMAP and hands each send to adapters/jmap.py's JmapSender; there is no SMTP here.

Doris holds two keys, both for her own Fastmail account (doris.htm@fastmail.com) only: an app
password for IMAP and CalDAV (against the URL of the owner's calendar as it is shared to her
account), and an API token for JMAP (DORIS_JMAP_TOKEN: scopes Email and Email submission). She
never holds a key to the owner's account.

Every rule of adapters/base.py holds here:
- A failure raises MailError, SendUnknown, CalendarError, CalendarTimeout or AlreadyExists.
  It is never read as an empty inbox, a free calendar or a sent message.
- Mail: fetch reads INBOX read-only with BODY.PEEK, statelessly; filing is a keyword on her own
  copy. There is no delete, expunge, move, copy, append or forward here. A send is one JMAP
  submission over HTTPS (jmap.JmapSender.send), never retried here. A message carrying her
  keyword SMOKE_TEST_KEYWORD is the smoke's own test request: fetch never hands it over, and
  the smoke's own run (smoke_message_id) is handed that one message and nothing else.
- Calendar: reads times, status and transparency only, never a title; writes create-only;
  deletes only her own event by booking ID.
- No secret reaches an exception message: errors carry the step, a status code or the
  underlying exception's class name, never a header, a body or server text.
- Every socket has the timeout given. Every TLS connection verifies the server certificate.

Hosts (Fastmail's own help pages, read Nov 27 2026: "Server names and ports",
fastmail.help/hc/en-us/articles/1500000278342): IMAP imap.fastmail.com:993 (SSL/TLS), JMAP
https://api.fastmail.com/jmap/session (jmap.HOST; its session names the account's own API host
below it, such as phl.api.fastmail.com), CalDAV https://caldav.fastmail.com/. hosts(env) names the
hosts these adapters connect to first, for the run's network guard.
"""

import base64
import contextlib
import datetime as _dt
import email.parser
import http.client
import imaplib
import io
import re
import socket
import ssl
import time
import urllib.parse
import xml.etree.ElementTree as ET
import xml.parsers.expat

from .. import caps, ics, slots
from ..shapes import (AlreadyExists, CalendarError, CalendarTimeout, Event, InboundItem, MailError, NotHers,
                      SendUnknown)
from . import jmap
from .base import CalendarAdapter, MailAdapter

UTC = _dt.timezone.utc
_DAY = _dt.timedelta(days=1)

# --- the env names this module reads (values live only in the server's .env) -------------------
ENV_ADDRESS = "DORIS_MAIL_ADDRESS"     # her address: the login for IMAP and CalDAV, her JMAP account, the sender
ENV_PASSWORD = "DORIS_APP_PASSWORD"    # the app password for her own account (IMAP and CalDAV)
ENV_IMAP_HOST = "DORIS_IMAP_HOST"
ENV_IMAP_PORT = "DORIS_IMAP_PORT"
ENV_JMAP_TOKEN = jmap.ENV_TOKEN        # her JMAP API token: every send
ENV_CALDAV_URL = "DORIS_CALDAV_URL"    # the owner's shared calendar, as her account sees it
ENV_NAMES = (ENV_ADDRESS, ENV_PASSWORD, ENV_IMAP_HOST, ENV_IMAP_PORT, ENV_JMAP_TOKEN, ENV_CALDAV_URL)

DEFAULT_IMAP_HOST = "imap.fastmail.com"
DEFAULT_IMAP_PORT = 993
# Her keyword on the smoke's own test request in INBOX (set by the smoke under her login, before its
# run answers it on the smoke's throwaway ledger): no live run reads it again, whatever its ledger.
SMOKE_TEST_KEYWORD = "doris-smoke-test"
SMOKE_MID_MARK = SMOKE_TEST_KEYWORD + "."   # in every Message-ID the smoke mints: no live run reads such a message

# Every pattern that validates a whole value ends in \Z: in Python, $ also matches before a final
# newline, which would let "name\n" through to a protocol line.
_HOST = re.compile(r"^(?=.{1,253}\Z)[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*\Z")
_ADDRESS = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
                      r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+\Z")


FETCH_BATCH = 50                       # messages per UID FETCH
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

_INTERNALDATE = re.compile(rb'INTERNALDATE "\s?(\d{1,2})-([A-Za-z]{3})-(\d{4}) (\d{2}):(\d{2}):(\d{2}) ([+-])(\d{2})(\d{2})"')
_UID = re.compile(rb"\bUID (\d+)")
_FLAGS = re.compile(rb"\bFLAGS \(([^)]*)\)")
_SIZE = re.compile(rb"\bRFC822\.SIZE (\d+)")
_BODY_LITERAL = re.compile(rb"BODY\[\](?:<0>)? \{\d+\}\Z")   # the whole message, or its first octets (<0>)
_HEADER_LITERAL = re.compile(rb'BODY\[HEADER\.FIELDS \("?MESSAGE-ID"?\)\] \{\d+\}\Z', re.I)
_LIST_LINE = re.compile(rb'^\((?P<attrs>[^)]*)\)\s+(?P<delim>"(?:[^"\\]|\\.)*"|NIL)\s+(?P<rest>.*)\Z', re.S)
_KEYWORD = re.compile(r"^[A-Za-z0-9$_-]{1,64}\Z")
_SERVER_ID = re.compile(r"^INBOX:([1-9][0-9]{0,9}):([1-9][0-9]{0,9})\Z")
_MESSAGE_ID = re.compile(r"^[!#-;=?A-\[\]-~]{1,200}@[!#-;=?A-\[\]-~]{1,200}\Z")   # printable, no space " < > @ \
_URL_PATH = re.compile(r"^[A-Za-z0-9._~%!$&'()*+,;=:@/-]+\Z")                      # RFC 3986 path characters
_IMAP_FAILURES = (imaplib.IMAP4.error, OSError, EOFError, ValueError)

# The env lacks or misstates a setting: one class for this module and the JMAP sender LiveMail
# builds, so a caller catches either one. It names the setting, never its value.
ConfigError = jmap.ConfigError


class _Secret(object):
    """Holds a credential so that repr, str and vars never show it."""
    __slots__ = ("_value",)

    def __init__(self, value):
        self._value = value

    def reveal(self):
        return self._value

    def __repr__(self):
        return "<secret>"

    __str__ = __repr__


# ------------------------------------------------------------------------------------------------
# Settings
# ------------------------------------------------------------------------------------------------
def _value(env, name):
    value = env.get(name)
    return value.strip() if isinstance(value, str) else ""


def bare_address(env, name=ENV_ADDRESS):
    """The setting `name` as one bare email address. ConfigError names the setting, never its value."""
    value = _value(env, name)
    if not value:
        raise ConfigError("%s is not set" % name)
    if not _ADDRESS.match(value):
        raise ConfigError("%s is not one bare email address" % name)
    return value


def _address(env):
    return bare_address(env, ENV_ADDRESS)


def _password(env):
    value = env.get(ENV_PASSWORD)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("%s is not set" % ENV_PASSWORD)
    return _Secret(value.strip())


def _host(env, name, default):
    value = _value(env, name) or default
    if not _HOST.match(value):
        raise ConfigError("%s is not a bare host name" % name)
    return value.lower()


def _port(env, name, default):
    value = _value(env, name)
    if not value:
        return default
    if not (value.isascii() and value.isdigit()) or not 0 < int(value) < 65536:
        raise ConfigError("%s is not a port number" % name)
    return int(value)


def _caldav(env):
    """(host, port, collection path ending in /) from the calendar URL: https only, no user or
    password in it, no query or fragment."""
    value = _value(env, ENV_CALDAV_URL)
    if not value:
        raise ConfigError("%s is not set" % ENV_CALDAV_URL)
    try:
        parts = urllib.parse.urlsplit(value)
        host, port = parts.hostname, parts.port
    except ValueError:
        raise ConfigError("%s is not a URL" % ENV_CALDAV_URL) from None
    if parts.scheme.lower() != "https":
        raise ConfigError("%s must be an https URL" % ENV_CALDAV_URL)
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise ConfigError("%s must not carry a user or password" % ENV_CALDAV_URL)
    if parts.query or parts.fragment or value.endswith(("?", "#")):
        raise ConfigError("%s must not carry a query or fragment" % ENV_CALDAV_URL)
    if not host or not _HOST.match(host):
        raise ConfigError("%s has no host name" % ENV_CALDAV_URL)
    if not parts.path or parts.path == "/":
        raise ConfigError("%s must name the calendar collection, not the server" % ENV_CALDAV_URL)
    if not _URL_PATH.match(parts.path):
        raise ConfigError("%s has a character a URL path cannot carry (percent-encode it)" % ENV_CALDAV_URL)
    path = parts.path if parts.path.endswith("/") else parts.path + "/"
    return host.lower(), port or 443, path


def _timeout(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
        raise ConfigError("the timeout must be a positive number of seconds")
    return float(value)


def hosts(env) -> list:
    """Every host the two adapters connect to first, each once: her IMAP server, her JMAP session's
    host (jmap.HOST) and the calendar URL's host. The run's network guard allows these, then the
    host her JMAP session names for apiUrl and uploadUrl once her sender checked it (jmap,
    netguard.admit), and nothing else."""
    found = []
    for host in (_host(env, ENV_IMAP_HOST, DEFAULT_IMAP_HOST), jmap.HOST, _caldav(env)[0]):
        if host not in found:
            found.append(host)
    return found


# ------------------------------------------------------------------------------------------------
# Small shared pieces
# ------------------------------------------------------------------------------------------------
def _tls():
    """A client context that verifies the server's certificate and name (never the stdlib's
    unverified default, which imaplib falls back to when given none), TLS 1.2 or newer: Python
    3.9's default context sets no floor of its own."""
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def _imap_tls(host, port, timeout):
    return imaplib.IMAP4_SSL(host, port, ssl_context=_tls(), timeout=timeout)


def _https(host, port, timeout):
    return http.client.HTTPSConnection(host, port, timeout=timeout, context=_tls())


def _failure(cls, step, exc=None, status=None, detail=None):
    """An adapter error whose text is the step and a status code, the underlying exception's
    class name or a fixed phrase of ours: never server text, a header or a body."""
    if detail is None:
        if status is not None:
            detail = str(status)
        elif exc is not None:
            detail = type(exc).__qualname__
        else:
            detail = "failed"
    err = cls("%s: %s" % (step, detail))
    err.status = status
    err.cause = type(exc).__qualname__ if exc is not None else None
    return err


def _quietly(call, *args):
    try:
        call(*args)
    except Exception:
        pass


def _aware_utc(value, what):
    if not isinstance(value, _dt.datetime) or value.tzinfo is None:
        raise ValueError("%s must be a timezone-aware datetime" % what)
    return value.astimezone(UTC)


def _batches(items, size=FETCH_BATCH):
    return [items[i:i + size] for i in range(0, len(items), size)]


def _uid_list(uids):
    return ",".join(str(uid) for uid in uids)


def _imap_day(moment):
    """An IMAP search date (dd-Mon-yyyy, English months whatever the locale)."""
    return "%d-%s-%04d" % (moment.day, MONTHS[moment.month - 1], moment.year)


def _quote_box(name):
    """A mailbox name as an IMAP quoted string. Every imaplib version sends it as given."""
    return '"%s"' % name.replace("\\", "\\\\").replace('"', '\\"')


def _internaldate(match, step):
    day, month, year, hh, mm, ss, sign, oh, om = match.groups()
    try:
        local = _dt.datetime(int(year), MONTHS.index(month.decode("ascii").title()) + 1, int(day),
                             int(hh), int(mm), int(ss))
        offset = _dt.timedelta(hours=int(oh), minutes=int(om))
        return (local + offset if sign == b"-" else local - offset).replace(tzinfo=UTC)
    except (ValueError, OverflowError):            # OverflowError: year 1 or 9999 moved past the edge
        raise _failure(MailError, step, detail="an INTERNALDATE cannot be read") from None


def server_id(uidvalidity, uid):
    """Her copy's ID across runs: the mailbox, its UIDVALIDITY and the UID."""
    return "INBOX:%d:%d" % (uidvalidity, uid)


def _norm_mid(value):
    return str(value or "").strip().strip("<>").strip()


def _flags_of(data, uid):
    """The FLAGS the server reported for this uid, or None when it reported none."""
    for item in data:
        text = item[0] if isinstance(item, tuple) else item
        if not isinstance(text, bytes):
            continue
        found_uid, flags = _UID.search(text), _FLAGS.search(text)
        if found_uid and flags and int(found_uid.group(1)) == uid:
            return flags.group(1).decode("ascii", "replace").split()
    return None


def _list_name(rest):
    """A mailbox name from the rest of a LIST line: a quoted string or an atom."""
    rest = rest.strip()
    if rest.startswith(b'"'):
        out, i = bytearray(), 1
        while i < len(rest):
            c = rest[i:i + 1]
            if c == b"\\" and i + 1 < len(rest):
                out += rest[i + 1:i + 2]
                i += 2
                continue
            if c == b'"':
                return bytes(out)
            out += c
            i += 1
        return None
    words = rest.split()
    return words[0] if words else None


def _flagged(data, flag):
    """Names of the mailboxes whose LIST attributes include flag (lowercase, e.g. \\sent).
    A name may come as a literal; the line after a literal is its trailer, not a mailbox."""
    names, after_literal = [], False
    for item in data or ():
        if item is None:
            continue
        if isinstance(item, tuple):
            line = _LIST_LINE.match(item[0]) if isinstance(item[0], bytes) else None
            name, after_literal = (bytes(item[1]) if line else None), True
        else:
            if after_literal:
                after_literal = False
                continue
            line = _LIST_LINE.match(item) if isinstance(item, bytes) else None
            name = _list_name(line.group("rest")) if line else None
        if line is None or name is None:
            continue
        if flag in line.group("attrs").decode("ascii", "replace").lower().split():
            try:
                names.append(name.decode("utf-8"))
            except UnicodeDecodeError:
                continue
    return names


def _message_ids(header_bytes):
    headers = email.parser.BytesHeaderParser().parsebytes(bytes(header_bytes))
    return {_norm_mid(str(value)) for value in headers.get_all("Message-ID") or ()}


# ------------------------------------------------------------------------------------------------
# Mail
# ------------------------------------------------------------------------------------------------
class LiveMail(MailAdapter):
    """Her mailbox over IMAP (read, file) under her own login, over TLS; her sends over JMAP
    (jmap.JmapSender, HTTPS) under her own API token.

    fetch_new is stateless: every INBOX message received at or after since_utc (all of them
    when it is None), oldest first, but never one carrying SMOKE_TEST_KEYWORD; the ledger decides
    what is new. With smoke_message_id (the smoke's own run) it hands over only the message that
    carries SMOKE_TEST_KEYWORD and exactly that Message-ID. Each IMAP call opens its own
    connection, logs in, and logs out when done. jmap_factory(host, port, timeout) makes the
    sender's connections (a test's fake server); without it they are verified HTTPS."""

    def __init__(self, env, doris_addr, timeout_s=caps.MAIL_TIMEOUT_S, since_utc=None,
                 imap_factory=None, jmap_factory=None, smoke_message_id=None):
        login = _address(env)
        if not isinstance(doris_addr, str) or doris_addr.strip().lower() != login.lower():
            raise ConfigError("her address must be %s, the login of her own account" % ENV_ADDRESS)
        self.doris_addr = doris_addr.strip()
        self._user = login
        self._password = _password(env)
        self.imap_host = _host(env, ENV_IMAP_HOST, DEFAULT_IMAP_HOST)
        self.imap_port = _port(env, ENV_IMAP_PORT, DEFAULT_IMAP_PORT)
        self.timeout_s = _timeout(timeout_s)
        self.since_utc = since_utc
        self.smoke_left_out = 0              # the smoke's test requests the last fetch left out
        self.smoke_message_id = None
        if smoke_message_id is not None:
            target = _norm_mid(smoke_message_id) if isinstance(smoke_message_id, str) else ""
            if not _MESSAGE_ID.match(target):
                raise ConfigError("the smoke's Message-ID is not one it can search for")
            self.smoke_message_id = target
        self._imap_factory = imap_factory or _imap_tls
        self._sender = jmap.JmapSender(env, self.doris_addr, timeout_s=self.timeout_s, http_factory=jmap_factory)

    @property
    def since_utc(self):
        """Fetch hands over mail received at or after this instant (None: all of INBOX)."""
        return self._since

    @since_utc.setter
    def since_utc(self, value):
        self._since = None if value is None else _aware_utc(value, "since_utc")

    # -- IMAP sessions ---------------------------------------------------------------------------
    @contextlib.contextmanager
    def _imap(self, step):
        """One logged-in IMAP connection. Any failure inside becomes a MailError naming the step."""
        try:
            conn = self._imap_factory(self.imap_host, self.imap_port, self.timeout_s)
        except _IMAP_FAILURES as exc:
            raise _failure(MailError, step + " connect", exc) from None
        try:
            try:
                conn.login(self._user, self._password.reveal())
            except _IMAP_FAILURES as exc:
                raise _failure(MailError, step + " login", exc) from None
            try:
                yield conn
            except MailError:
                raise
            except _IMAP_FAILURES as exc:
                raise _failure(MailError, step, exc) from None
        finally:
            try:
                conn.logout()
            except Exception:
                _quietly(conn.shutdown)

    def _open(self, conn, mailbox, readonly, step):
        """SELECT (or EXAMINE when readonly) a mailbox; its UIDVALIDITY. No UIDVALIDITY is a
        failure: without it a UID names nothing for sure."""
        typ, data = conn.select(_quote_box(mailbox), readonly=readonly)
        if typ != "OK":
            raise _failure(MailError, step + (" examine" if readonly else " select"), status=typ)
        _, values = conn.response("UIDVALIDITY")
        value = values[-1] if values else None
        if not isinstance(value, bytes) or not value.strip().isdigit() or int(value) <= 0:
            raise _failure(MailError, step, detail="the mailbox has no UIDVALIDITY")
        return int(value), data

    def _search(self, conn, criteria, step):
        typ, data = conn.uid("SEARCH", *criteria)
        if typ != "OK":
            raise _failure(MailError, step + " search", status=typ)
        uids = set()
        for line in data:
            if line is None:
                continue
            if not isinstance(line, bytes):
                raise _failure(MailError, step, detail="a search answer cannot be read")
            for token in line.split():
                if not token.isdigit():
                    raise _failure(MailError, step, detail="a search answer cannot be read")
                uids.add(int(token))
        return sorted(uids)

    def _fetch(self, conn, uids, items, step):
        typ, data = conn.uid("FETCH", _uid_list(uids), items)
        if typ != "OK":
            raise _failure(MailError, step + " fetch", status=typ)
        return [item for item in data if item is not None]

    def _dates(self, conn, uids, step):
        """uid -> (INTERNALDATE as aware UTC, RFC822.SIZE), for every uid the server gave both for."""
        out = {}
        for item in self._fetch(conn, uids, "(UID INTERNALDATE RFC822.SIZE)", step):
            text = item[0] if isinstance(item, tuple) else item
            if not isinstance(text, bytes):
                continue
            uid, when, size = _UID.search(text), _INTERNALDATE.search(text), _SIZE.search(text)
            if uid and when and size:
                out[int(uid.group(1))] = (_internaldate(when, step), int(size.group(1)))
        return out

    def _bodies(self, conn, uids, step, octets=None):
        """uid -> the whole message as it arrived, read with BODY.PEEK[] (\\Seen untouched); with
        octets, only its first octets (BODY.PEEK[]<0.octets>)."""
        items = "(UID BODY.PEEK[])" if octets is None else "(UID BODY.PEEK[]<0.%d>)" % octets
        return self._literals(conn, uids, items, _BODY_LITERAL, step)

    def _literals(self, conn, uids, items, literal, step):
        """uid -> the literal the server sent for the one item asked (its answer's head matching
        literal), for every uid it answered that way. A uid left out, or answered with anything
        but a literal (NIL, a quoted string), is simply not in the result."""
        data = self._fetch(conn, uids, items, step)
        out = {}
        for i, item in enumerate(data):
            if not isinstance(item, tuple) or len(item) != 2 or not isinstance(item[0], bytes):
                continue
            head, body = item
            if not literal.search(head):
                continue
            trailer = data[i + 1] if i + 1 < len(data) and isinstance(data[i + 1], bytes) else b""
            uid = _UID.search(head + b" " + trailer)
            if uid:
                out[int(uid.group(1))] = bytes(body)
        return out

    # -- the interface ---------------------------------------------------------------------------
    def fetch_new(self):
        """Every INBOX message received at or after since_utc, oldest first (INTERNALDATE, then
        UID), leaving out the smoke's own test requests, answered on the smoke's ledger: any
        carrying SMOKE_TEST_KEYWORD, and any whose Message-ID holds SMOKE_MID_MARK (one the smoke
        cut short before its mark), in the search and again once read, counted in smoke_left_out
        (a second search, without those two criteria, finds the ones the first left out); with
        smoke_message_id, only that request: the keyword and exactly that Message-ID, and nothing
        counted. Read-only (EXAMINE) and with BODY.PEEK[], so nothing about her mail changes. Each
        message's RFC822.SIZE is read with its date: one over caps.MAX_RAW_BYTES is handed over
        as only its first MAX_RAW_BYTES + 1 octets, never downloaded whole, and the reader holds
        it as oversize. Raises MailError on any failure, including a message the server named
        and then did not hand over: a failure is never an empty inbox."""
        step = "imap fetch"
        since = self.since_utc
        cap = caps.MAX_RAW_BYTES
        self.smoke_left_out = 0
        left_out = 0
        with self._imap(step) as conn:
            uidvalidity, _ = self._open(conn, "INBOX", True, step)
            window = ("ALL",) if since is None else ("SINCE", _imap_day(since - _DAY))
            if self.smoke_message_id is None:
                criteria = window + ("NOT", "KEYWORD", SMOKE_TEST_KEYWORD,
                                     "NOT", "HEADER", "Message-ID", '"%s"' % SMOKE_MID_MARK)
            else:                          # a substring search: each match is read back below
                criteria = window + ("KEYWORD", SMOKE_TEST_KEYWORD, "HEADER", "Message-ID", '"%s"' % self.smoke_message_id)
            uids = self._search(conn, criteria, step)
            if self.smoke_message_id is None:
                left = sorted(set(self._search(conn, window, step)) - set(uids))
                for batch in _batches(left):
                    facts = self._dates(conn, batch, step)
                    left_out += sum(1 for uid in batch
                                    if uid in facts and (since is None or facts[uid][0] >= since))
            dated = []
            for batch in _batches(uids):
                facts = self._dates(conn, batch, step)
                for uid in batch:
                    if uid not in facts:
                        raise _failure(MailError, step, detail="the server left a message's date or size out")
                    received, size = facts[uid]
                    if since is None or received >= since:
                        dated.append((received, uid, size))
            dated.sort()
            items = []
            for batch in _batches(dated):
                whole = [uid for _, uid, size in batch if size <= cap]
                over = [uid for _, uid, size in batch if size > cap]
                bodies = self._bodies(conn, whole, step) if whole else {}
                if over:
                    bodies.update(self._bodies(conn, over, step, octets=cap + 1))
                for received, uid, _ in batch:
                    if uid not in bodies:
                        raise _failure(MailError, step, detail="the server left a message out")
                    mids = _message_ids(bodies[uid])
                    if self.smoke_message_id is not None and self.smoke_message_id not in mids:
                        continue           # it only contains the smoke's Message-ID: never the smoke's to read
                    if self.smoke_message_id is None and any(SMOKE_MID_MARK in mid.lower() for mid in mids):
                        left_out += 1      # the smoke's request, unmarked: a server that searched loosely
                        continue
                    items.append(InboundItem(server_id=server_id(uidvalidity, uid), raw=bodies[uid],
                                             received_utc=received))
            self.smoke_left_out = left_out
            return items

    def file(self, server_id, keyword):
        """Add a keyword (doris-file, doris-held, ...) to her own copy in INBOX, then read its
        flags back. Never deletes, moves or copies. A keyword outside letters, digits, $, - and
        _ is a ValueError; an ID this adapter did not hand out, a changed UIDVALIDITY, a missing
        message or a keyword the server did not keep is a MailError."""
        if not isinstance(keyword, str) or not _KEYWORD.match(keyword):
            raise ValueError("a keyword is one word of letters, digits, $, - or _")
        step = "imap file"
        found = _SERVER_ID.match(server_id) if isinstance(server_id, str) else None
        if found is None:
            raise _failure(MailError, step, detail="not a message this adapter handed out")
        uidvalidity, uid = int(found.group(1)), int(found.group(2))
        with self._imap(step) as conn:
            current, _ = self._open(conn, "INBOX", False, step)
            if current != uidvalidity:
                raise _failure(MailError, step, detail="INBOX has a new UIDVALIDITY; nothing filed")
            typ, data = conn.uid("STORE", str(uid), "+FLAGS", "(%s)" % keyword)
            if typ != "OK":
                raise _failure(MailError, step + " store", status=typ)
            flags = _flags_of(data, uid)
            if flags is None:
                flags = _flags_of(self._fetch(conn, [uid], "(UID FLAGS)", step), uid)
            if flags is None:
                raise _failure(MailError, step, detail="no such message")
            if keyword.lower() not in {flag.lower() for flag in flags}:
                raise _failure(MailError, step, detail="the server did not keep the keyword")

    def find_in_sent(self, message_id):
        """True when a folder flagged \\Sent (RFC 6154 special-use, never a folder name) holds a
        message whose Message-ID is exactly this one. Read-only; writes nothing anywhere.
        False only when every message the search named was read and none is it. Raises
        MailError when it cannot tell: no folder flagged \\Sent, or a message the search named
        that the fetch then left out (expunged meanwhile) or answered without a header literal."""
        step = "imap sent lookup"
        target = _norm_mid(message_id)
        if not _MESSAGE_ID.match(target):
            raise _failure(MailError, step, detail="not a Message-ID it can search for")
        unread = False
        with self._imap(step) as conn:
            for folder in self._sent_folders(conn, step):
                self._open(conn, folder, True, step)
                found, missed = self._with_message_id(conn, target, step)
                if found:
                    return True
                unread = unread or missed
        if unread:
            raise _failure(MailError, step, detail="a message the search named could not be read; cannot tell")
        return False

    def find_in_inbox(self, message_id):
        """The server IDs of every INBOX message whose Message-ID is exactly this one, oldest UID
        first ([] when none): the smoke's look for its own test request. Read-only (EXAMINE,
        BODY.PEEK). Raises MailError when it cannot tell: a message the search named that the
        fetch then left out or answered without a header literal."""
        step = "imap inbox lookup"
        target = _norm_mid(message_id)
        if not _MESSAGE_ID.match(target):
            raise _failure(MailError, step, detail="not a Message-ID it can search for")
        with self._imap(step) as conn:
            uidvalidity, _ = self._open(conn, "INBOX", True, step)
            found, missed = self._with_message_id(conn, target, step)
        if missed:
            raise _failure(MailError, step, detail="a message the search named could not be read; cannot tell")
        return [server_id(uidvalidity, uid) for uid in found]

    def _with_message_id(self, conn, target, step):
        """(the UIDs in the open mailbox whose Message-ID is exactly target, oldest first; whether a
        message the search named was left unread). The HEADER search is a substring match, so
        each message it names is read back by its Message-ID header."""
        found, missed = [], False
        for batch in _batches(self._search(conn, ("HEADER", "Message-ID", '"%s"' % target), step)):
            headers = self._literals(conn, batch, "(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])",
                                     _HEADER_LITERAL, step)
            found.extend(uid for uid in batch if uid in headers and target in _message_ids(headers[uid]))
            missed = missed or any(uid not in headers for uid in batch)
        return found, missed

    def _sent_folders(self, conn, step):
        """Every mailbox flagged \\Sent: asked for with LIST ... RETURN (SPECIAL-USE), then a
        plain LIST if the server refuses that form or flags nothing in it."""
        try:
            typ, _ = conn.xatom("LIST", '""', '"*"', "RETURN", "(SPECIAL-USE)")
        except imaplib.IMAP4.abort:
            raise
        except imaplib.IMAP4.error:
            typ = "BAD"
        if typ == "OK":
            found = _flagged(conn.response("LIST")[1], "\\sent")
            if found:
                return found
        typ, data = conn.list()
        if typ != "OK":
            raise _failure(MailError, step + " list", status=typ)
        found = _flagged(data, "\\sent")
        if not found:
            raise _failure(MailError, step, detail="no folder carries the \\Sent special-use flag")
        return found

    # -- sends, over JMAP ----------------------------------------------------------------------
    def send(self, raw, envelope_to, message_id, template=None):
        """Hand one message to her JMAP account (jmap.JmapSender.send): her address as the envelope
        sender, exactly envelope_to as the recipients, raw as it is (CRLF line ends). One
        submission, never a retry. template is accepted for the interface and not used.

        MailError: it did not go (a refusal before any connection, any failure before the
        submission request went in full, a submission the server turned away or did not create).
        SendUnknown: the submission request went and no readable answer came back. The caller
        looks in Sent; nothing here sends it again."""
        self._sender.send(raw, envelope_to, message_id, template)

    @property
    def last_copy_in_sent(self):
        """After a send that went: True when her copy reached Sent, False when it may sit in Drafts."""
        return self._sender.last_copy_in_sent

    # -- what the smoke checks -------------------------------------------------------------------
    def check_send_login(self):
        """Read her JMAP session, mailboxes and identity: proves her token, its scopes and her
        account. Sends and uploads nothing. Raises MailError."""
        self._sender.check_login()

    def inbox_count(self):
        """Log in and count the messages in INBOX (read-only). Raises MailError."""
        step = "imap inbox count"
        with self._imap(step) as conn:
            _, data = self._open(conn, "INBOX", True, step)
            value = data[-1] if data else None
            if not isinstance(value, bytes) or not value.strip().isdigit():
                raise _failure(MailError, step, detail="no message count")
            return int(value)

    def sent_folders(self):
        """The names of the folders flagged \\Sent. Raises MailError when there is none."""
        step = "imap sent folders"
        with self._imap(step) as conn:
            return self._sent_folders(conn, step)


# ------------------------------------------------------------------------------------------------
# Calendar
# ------------------------------------------------------------------------------------------------
class LiveCalendar(CalendarAdapter):
    """The owner's calendar, shared to her account, over CalDAV: HTTPS with Basic auth as her.

    query is a calendar-query REPORT with a time-range; the server expands repeats and is asked
    for times, status and transparency only (partial retrieval), and the parser reads nothing
    else in any case. An event it cannot read, or a repeat the server did not expand, is busy
    over the whole span asked about. create_only is a PUT with If-None-Match: *. delete reads
    the resource first and removes it only when it holds her event alone, unchanged since read.
    Every request is one connection; every call (all of its requests) has one deadline, the
    timeout given. Before a request goes, a failure is a CalendarError; after, a lost
    connection or no answer in time is a CalendarTimeout."""

    def __init__(self, env, timeout_s=caps.CALENDAR_TIMEOUT_S, http_factory=None):
        user = _address(env)
        password = _password(env)
        self.host, self.port, self.path = _caldav(env)
        self._auth = _Secret("Basic " + base64.b64encode(("%s:%s" % (user, password.reveal())).encode("utf-8"))
                             .decode("ascii"))
        self.timeout_s = _timeout(timeout_s)
        self._http_factory = http_factory or _https
        self.last_query_whole_span = 0     # events the last query could only read as busy everywhere

    def _resource(self, booking_id):
        return self.path + urllib.parse.quote(booking_id + ".ics", safe="@")

    def _deadline(self):
        """The one deadline of a call: every request it makes, and every wait in each, ends by then."""
        return time.monotonic() + self.timeout_s

    def _request(self, step, method, path, body=None, headers=None, read=False, deadline=None):
        """(status, ETag, body) for one request on a connection of its own, inside the call's one
        deadline (a fresh one when none is given): connecting, sending and every read get only
        the time left. A socket timeout alone restarts with each byte, so a server answering a
        byte at a time would hold the call for as long as it liked."""
        deadline = self._deadline() if deadline is None else deadline
        sent = dict(headers or {})
        sent["Authorization"] = self._auth.reveal()
        try:
            conn = self._http_factory(self.host, self.port, self.timeout_s)
        except _HTTP_FAILURES as exc:
            raise _failure(CalendarError, step + " connect", exc) from None
        try:
            try:
                conn.timeout = _time_left(deadline)          # the TCP connect and the TLS handshake
                conn.connect()
                conn.sock = _DeadlineSocket(conn.sock, deadline)
                _time_left(deadline)                         # none left: the request never went
            except _HTTP_FAILURES as exc:
                raise _failure(CalendarError, step + " connect", exc) from None
            resp = None
            try:
                conn.request(method, path, body=body, headers=sent)
                resp = conn.getresponse()
                data = resp.read() if read else b""
            except _HTTP_FAILURES as exc:
                raise _failure(CalendarTimeout, step, exc) from None
            finally:
                if resp is not None:
                    _quietly(resp.close)
            return resp.status, resp.getheader("ETag"), data
        finally:
            _quietly(conn.close)

    # -- reads -----------------------------------------------------------------------------------
    def query(self, start_utc, end_utc):
        """Every event touching [start, end), repeats expanded by the server, cancelled ones
        included with their status. The server is asked about the span padded by two days on
        each side (an all-day event is a day in some zone) and the answer is cut to the span here."""
        start, end = _aware_utc(start_utc, "start_utc"), _aware_utc(end_utc, "end_utc")
        if end <= start:
            raise ValueError("the query span runs forward")
        step = "caldav query"
        window = {"start": _ics_stamp(start - QUERY_PAD), "end": _ics_stamp(end + QUERY_PAD), "props": _QUERY_PROPS}
        status, _, body = self._request(step, "REPORT", self.path, (_QUERY % window).encode("utf-8"),
                                        {"Depth": "1", "Content-Type": "application/xml; charset=utf-8"}, read=True)
        if status != 207:
            raise _failure(CalendarError, step, status=status)
        found, whole = [], 0
        for name, data in self._multistatus(body, step):
            events, unread = _events_in(name, data, start, end)
            found.extend(events)
            whole += unread
        found.sort(key=lambda ev: (ev.start_utc or start, ev.uid))
        self.last_query_whole_span = whole
        return found

    def _multistatus(self, body, step):
        """[(resource name, calendar data or None)] from a 207 answer, the collection's own entry
        left out. A DOCTYPE, an answer that is not a multistatus, or the collection marked with
        an error status (507: the server cut the answer short) is a CalendarError."""
        root = _xml(body, step)
        if root.tag != _DAV + "multistatus":
            raise _failure(CalendarError, step, detail="the answer is not a multistatus")
        collection = urllib.parse.unquote(self.path).rstrip("/")
        out = []
        for resp in root.findall(_DAV + "response"):
            href = _href_path(resp, step)
            code = _status_code(resp.findtext(_DAV + "status"))
            if href.rstrip("/") == collection:
                if code is not None and not 200 <= code < 300:
                    raise _failure(CalendarError, step, status=code)
                continue
            data = None
            if code is None:
                for propstat in resp.findall(_DAV + "propstat"):
                    if _status_code(propstat.findtext(_DAV + "status")) == 200:
                        found = propstat.find(_DAV + "prop/" + _CALDAV + "calendar-data")
                        if found is not None and found.text and found.text.strip():
                            data = found.text
            name = href.rstrip("/").rsplit("/", 1)[-1]
            out.append((name[:-4] if name.lower().endswith(".ics") else name, data))
        return out

    def get(self, booking_id):
        """Her event by booking ID, with the times and status the server holds (the caller
        compares them with what she wrote). A resource holding anything but exactly her one,
        unrepeated VEVENT is a CalendarError. None only when the server answers 404 and the
        collection itself, asked by UID, holds no event of hers; any other failure raises (a
        timeout or lost connection as CalendarTimeout), never None."""
        if not ics.is_booking_id(booking_id):
            raise NotHers("caldav get: not one of her booking IDs")
        step = "caldav get"
        deadline = self._deadline()
        status, _, body = self._request(step, "GET", self._resource(booking_id), read=True, deadline=deadline)
        if status == 404:
            self._confirm_absent(booking_id, step + " absent check", deadline)
            return None
        if status != 200:
            raise _failure(CalendarError, step, status=status)
        return _her_one_event(body, booking_id, step)

    # -- writes ----------------------------------------------------------------------------------
    def create_only(self, booking_id, ics_bytes):
        """PUT her event as <booking_id>.ics with If-None-Match: *, so it never overwrites.
        Only 201 is created. 412 is AlreadyExists. A gateway error or a timeout answer (408, 502,
        504) is CalendarTimeout: the write may have landed, so the caller looks it up by booking
        ID. Any other status, 200 and 204 included (under If-None-Match an overwrite, or a server
        that ignores the condition), is a CalendarError. Refuses before any call an ID not hers,
        an .ics outside her fixed form (G13) and a METHOD line (a calendar resource carries none,
        RFC 4791 4.1)."""
        if not ics.is_booking_id(booking_id):
            raise NotHers("caldav create: not one of her booking IDs")
        step = "caldav create"
        if not ics.fields_ok(ics_bytes, booking_id=booking_id):
            raise _failure(CalendarError, step, detail="refused: the .ics is not her fixed form for this ID")
        if b"\r\nMETHOD:" in bytes(ics_bytes):
            raise _failure(CalendarError, step, detail="refused: a calendar write carries no METHOD line")
        status, _, _ = self._request(step, "PUT", self._resource(booking_id), bytes(ics_bytes),
                                     {"If-None-Match": "*", "Content-Type": "text/calendar; charset=utf-8"})
        if status == 201:
            return
        if status == 412:
            raise _failure(AlreadyExists, step, status=status)
        if status in _WRITE_UNKNOWN:
            raise _failure(CalendarTimeout, step, status=status)
        if status in (200, 204):
            raise _failure(CalendarError, step, status=status,
                           detail="%d, not 201: an overwrite, or a server that ignores If-None-Match" % status)
        raise _failure(CalendarError, step, status=status)

    def delete(self, booking_id):
        """Delete her own event by booking ID: read the resource, refuse unless it holds her
        event alone (NotHers) and carries a strong ETag (without one the DELETE could not be
        conditional), then DELETE exactly that resource If-Match that ETag. Absent
        (404, before or after the read, and the collection asked by UID holding no event of
        hers) is a no-op; the caller's read-back confirms it."""
        if not ics.is_booking_id(booking_id):
            raise NotHers("caldav delete: not one of her booking IDs")
        step = "caldav delete"
        path = self._resource(booking_id)
        deadline = self._deadline()
        status, etag, body = self._request(step + " read", "GET", path, read=True, deadline=deadline)
        if status == 404:
            self._confirm_absent(booking_id, step + " absent check", deadline)
            return
        if status != 200:
            raise _failure(CalendarError, step + " read", status=status)
        try:
            uids = {v.uid for v in ics.read_events(body)}
        except _UNREADABLE:
            raise _failure(CalendarError, step, detail="the event cannot be read; not deleted") from None
        if uids != {booking_id}:
            raise _failure(NotHers, step, detail="the resource under her booking ID is not her event alone; not deleted")
        if not isinstance(etag, str) or not _STRONG_ETAG.match(etag):
            raise _failure(CalendarError, step, detail="no strong ETag to make the delete conditional; not deleted")
        status, _, _ = self._request(step, "DELETE", path, headers={"If-Match": etag}, deadline=deadline)
        if status in (200, 204):
            return
        if status == 404:
            self._confirm_absent(booking_id, step + " absent check", deadline)
            return
        raise _failure(CalendarError, step, status=status)

    def _confirm_absent(self, booking_id, step, deadline):
        """Return only when the collection itself answers a query for events with her UID and
        names none. A 404 on her resource alone is no proof her event is gone: a revoked share or
        a wrong collection answers 404 to everything, and a client may have filed her event under
        another name. Anything else is a CalendarError."""
        uid = booking_id.replace("&", "&amp;").replace("<", "&lt;")     # her IDs carry neither; kept safe anyway
        status, _, body = self._request(step, "REPORT", self.path, (_UID_QUERY % {"uid": uid}).encode("utf-8"),
                                        {"Depth": "1", "Content-Type": "application/xml; charset=utf-8"}, read=True,
                                        deadline=deadline)
        if status != 207:
            raise _failure(CalendarError, step, status=status)
        if self._multistatus(body, step):
            raise _failure(CalendarError, step, detail="an event with her booking ID's UID is on the calendar; not gone")

    # -- what the smoke checks -------------------------------------------------------------------
    def booking_ids(self, prefix):
        """Her booking IDs on the calendar that start with prefix, sorted: the smoke's search for the
        test events earlier smokes left. One calendar-query REPORT for the events whose UID holds
        prefix (RFC 4791 9.7.5: a substring match), each answer's UIDs read from its calendar data;
        a UID counts only when it starts with prefix and is one of her IDs. A prefix none of her IDs
        can start with is refused before any call; an answer that cannot be read (no calendar data,
        data that is no calendar, an error status) is a CalendarError, never an empty list."""
        if not isinstance(prefix, str) or not prefix or not ics.is_booking_id(prefix + "0"):
            raise NotHers("caldav find: not the start of her booking IDs")
        step = "caldav find"
        text = prefix.replace("&", "&amp;").replace("<", "&lt;")     # her IDs carry neither; kept safe anyway
        status, _, body = self._request(step, "REPORT", self.path, (_FIND_QUERY % {"uid": text}).encode("utf-8"),
                                        {"Depth": "1", "Content-Type": "application/xml; charset=utf-8"}, read=True)
        if status != 207:
            raise _failure(CalendarError, step, status=status)
        found = set()
        for _, data in self._multistatus(body, step):
            if data is None:
                raise _failure(CalendarError, step, detail="an event came back without its calendar data")
            try:
                uids = [v.uid for v in ics.read_events(data.encode("utf-8"))]
            except _UNREADABLE:
                raise _failure(CalendarError, step, detail="an event it found cannot be read") from None
            found.update(uid for uid in uids if uid.startswith(prefix) and ics.is_booking_id(uid))
        return sorted(found)

    def display_name(self):
        """The calendar's display name (DAV:displayname) exactly as the server reports it, read
        with one PROPFIND, Depth 0, on the collection: so a person can see which calendar the URL
        names. A CalendarError when the answer cannot be read or gives the collection no name."""
        step = "caldav display name"
        status, _, body = self._request(step, "PROPFIND", self.path, _NAME_QUERY.encode("utf-8"),
                                        {"Depth": "0", "Content-Type": "application/xml; charset=utf-8"}, read=True)
        if status != 207:
            raise _failure(CalendarError, step, status=status)
        root = _xml(body, step)
        if root.tag != _DAV + "multistatus":
            raise _failure(CalendarError, step, detail="the answer is not a multistatus")
        collection = urllib.parse.unquote(self.path).rstrip("/")
        for resp in root.findall(_DAV + "response"):
            if _href_path(resp, step).rstrip("/") != collection:
                continue
            for propstat in resp.findall(_DAV + "propstat"):
                found = propstat.find(_DAV + "prop/" + _DAV + "displayname")
                if found is not None and _status_code(propstat.findtext(_DAV + "status")) == 200:
                    name = "".join(found.itertext())
                    if name:
                        return name
        raise _failure(CalendarError, step, detail="the calendar reports no display name")


# ------------------------------------------------------------------------------------------------
# Calendar pieces
# ------------------------------------------------------------------------------------------------
_HTTP_FAILURES = (http.client.HTTPException, OSError, ValueError)
_UNREADABLE = (ValueError, OverflowError)     # what ics raises on an event it cannot read (OverflowError: year 9999)
# What expat and ElementTree raise on XML they cannot read: LookupError for an encoding Python does
# not know, ValueError for one expat cannot take (a multi-byte one such as UTF-7).
_XML_UNREADABLE = (xml.parsers.expat.ExpatError, ET.ParseError, LookupError, ValueError)
_DAV, _CALDAV = "{DAV:}", "{urn:ietf:params:xml:ns:caldav}"
_STATUS_LINE = re.compile(r"^\s*HTTP/\d(?:\.\d)?\s+(\d{3})\b")
_WRITE_UNKNOWN = (408, 502, 504)   # a timeout or a gateway answered for the server: the write may have landed
_STRONG_ETAG = re.compile(r'^"[\x21\x23-\x7e\x80-\xff]*"\Z')   # RFC 7232 entity-tag, not W/: what If-Match compares
QUERY_PAD = _dt.timedelta(days=2)
READ_PROPS = ("UID", "DTSTART", "DTEND", "DURATION", "STATUS", "TRANSP", "RECURRENCE-ID", "RRULE", "RDATE")
_QUERY_PROPS = "\n".join('          <C:prop name="%s"/>' % name for name in READ_PROPS)
# RFC 4791 7.8: partial retrieval (no title, no description, no people), expanded by the server.
# RRULE and RDATE are asked for so that a server that does not expand is seen not to.
_QUERY = """<?xml version="1.0" encoding="utf-8"?>
<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:prop>
    <C:calendar-data>
      <C:comp name="VCALENDAR">
        <C:prop name="VERSION"/>
        <C:comp name="VEVENT">
%(props)s
        </C:comp>
      </C:comp>
      <C:expand start="%(start)s" end="%(end)s"/>
    </C:calendar-data>
  </D:prop>
  <C:filter>
    <C:comp-filter name="VCALENDAR">
      <C:comp-filter name="VEVENT">
        <C:time-range start="%(start)s" end="%(end)s"/>
      </C:comp-filter>
    </C:comp-filter>
  </C:filter>
</C:calendar-query>
"""


def _time_left(deadline):
    """Seconds left before the deadline; socket.timeout when there are none."""
    left = deadline - time.monotonic()
    if left <= 0:
        raise socket.timeout("the call's deadline has passed")
    return left


class _DeadlineSocket(object):
    """A connected socket (plain or TLS) whose every send and read waits only for the time left
    before one deadline. http.client sends with sendall and reads through makefile("rb"); every
    other attribute is the socket's own. As with socket.makefile, close() leaves the socket open
    while a reader made from it is open (http.client closes the connection as soon as it sees
    the answer will end it, and goes on reading the body through the reader)."""

    def __init__(self, sock, deadline):
        self._sock = sock
        self._deadline = deadline
        self._readers = 0
        self._closed = False

    def sendall(self, data, *flags):
        self._sock.settimeout(_time_left(self._deadline))
        return self._sock.sendall(data, *flags)

    def recv_into(self, buffer):
        self._sock.settimeout(_time_left(self._deadline))
        return self._sock.recv_into(buffer)

    def makefile(self, mode="r", *args, **kwargs):
        if mode != "rb":
            raise ValueError("only a binary reader")
        self._readers += 1
        return io.BufferedReader(_DeadlineReader(self))

    def reader_closed(self):
        self._readers -= 1
        if self._closed and self._readers <= 0:
            self._sock.close()

    def close(self):
        self._closed = True
        if self._readers <= 0:
            self._sock.close()

    def __getattr__(self, name):
        return getattr(self._sock, name)


class _DeadlineReader(io.RawIOBase):
    """The raw reader under http.client's response: each read is one wait, bounded by the deadline."""

    def __init__(self, sock):
        io.RawIOBase.__init__(self)
        self._sock = sock

    def readable(self):
        return True

    def readinto(self, buffer):
        return self._sock.recv_into(buffer)

    def close(self):
        if not self.closed:
            io.RawIOBase.close(self)
            self._sock.reader_closed()


class _DTDRefused(Exception):
    """A DOCTYPE or an entity declaration in a server's XML answer."""


def _refuse_dtd(*_):
    raise _DTDRefused()


def _xml(body, step):
    """The root element of a server's XML answer. A DOCTYPE or an entity declaration is refused
    by expat itself, in whatever encoding the answer is in (a byte scan misses "<!DOCTYPE" in
    UTF-16 and trips on it inside CDATA), before ElementTree reads anything: no entity is ever
    expanded. The C ElementTree parser does not expose its expat parser, hence the pre-pass."""
    check = xml.parsers.expat.ParserCreate()
    check.StartDoctypeDeclHandler = _refuse_dtd
    check.EntityDeclHandler = _refuse_dtd
    try:
        check.Parse(body, True)
    except _DTDRefused:
        raise _failure(CalendarError, step, detail="the answer carries a DOCTYPE; not read") from None
    except _XML_UNREADABLE:
        raise _failure(CalendarError, step, detail="the answer cannot be read") from None
    try:
        return ET.fromstring(body)
    except _XML_UNREADABLE:
        raise _failure(CalendarError, step, detail="the answer cannot be read") from None


def _her_one_event(body, booking_id, step):
    """Her event as the server holds it (its own times and status, whatever she wrote), or a
    CalendarError when the resource holds anything but exactly one VEVENT, carrying her UID, that
    does not repeat: never the first of several, never one occurrence of a series."""
    try:
        events = ics.read_events(body)
    except _UNREADABLE:
        raise _failure(CalendarError, step, detail="her event cannot be read") from None
    if len(events) != 1:
        raise _failure(CalendarError, step, detail="the resource under her booking ID holds more than one event")
    v = events[0]
    if v.uid != booking_id:
        raise _failure(CalendarError, step, detail="the event filed under her booking ID carries another UID")
    if v.rrule is not None or v.rdate or v.recurrence_id:
        raise _failure(CalendarError, step, detail="the event under her booking ID repeats; hers never do")
    try:
        return ics.to_event(v)
    except _UNREADABLE:
        raise _failure(CalendarError, step, detail="her event cannot be read") from None


# RFC 4791 9.7.5: every event whose UID contains her booking ID (a substring match; any answer at all
# means "not gone"). Only the ETag is asked for: nothing about the event is read.
_UID_QUERY = """<?xml version="1.0" encoding="utf-8"?>
<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:prop>
    <D:getetag/>
  </D:prop>
  <C:filter>
    <C:comp-filter name="VCALENDAR">
      <C:comp-filter name="VEVENT">
        <C:prop-filter name="UID">
          <C:text-match collation="i;octet">%(uid)s</C:text-match>
        </C:prop-filter>
      </C:comp-filter>
    </C:comp-filter>
  </C:filter>
</C:calendar-query>
"""


# RFC 4918 9.1: one property of the collection itself; nothing about any event.
# The smoke's search for its own test events: the whole calendar data of every event whose UID holds
# the prefix (a substring match), so their UIDs can be read and kept only when they start with it.
_FIND_QUERY = """<?xml version="1.0" encoding="utf-8"?>
<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:prop>
    <C:calendar-data/>
  </D:prop>
  <C:filter>
    <C:comp-filter name="VCALENDAR">
      <C:comp-filter name="VEVENT">
        <C:prop-filter name="UID">
          <C:text-match collation="i;octet">%(uid)s</C:text-match>
        </C:prop-filter>
      </C:comp-filter>
    </C:comp-filter>
  </C:filter>
</C:calendar-query>
"""

_NAME_QUERY = """<?xml version="1.0" encoding="utf-8"?>
<D:propfind xmlns:D="DAV:">
  <D:prop>
    <D:displayname/>
  </D:prop>
</D:propfind>
"""


def _href_path(resp, step):
    """The path a multistatus response names, percent-decoded. An href that is not a URL (e.g.
    "//[") is a CalendarError."""
    try:
        return urllib.parse.unquote(urllib.parse.urlsplit((resp.findtext(_DAV + "href") or "").strip()).path)
    except ValueError:
        raise _failure(CalendarError, step, detail="an href cannot be read") from None


def _ics_stamp(moment):
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _status_code(text):
    found = _STATUS_LINE.match(text or "")
    return int(found.group(1)) if found else None


def _whole_span(uid, status, transp, start, end):
    return Event(uid=uid, status=status, transp=transp, kind="timed", start_utc=start, end_utc=end)


def _touches(ev, start, end):
    """The event is busy at some moment of [start, end). A moment-long event counts at its
    start; an event that cannot be placed touches every span."""
    window = slots._busy_window(ev)
    if window is None:
        return True
    lo, hi = window
    if lo == hi:
        return start <= lo < end
    return lo < end and hi > start


def _events_in(name, data, start, end):
    """(events touching the span, how many could only be read as busy over all of it) for one
    resource of a query's answer."""
    if data is None:
        return [_whole_span(name, "", "", start, end)], 1
    try:
        vevents = ics.read_events(data.encode("utf-8"))
        out, whole = [], 0
        for v in vevents:
            if v.rrule is not None or v.rdate:
                out.append(_whole_span(v.uid, v.status, v.transp, start, end))
                whole += 1
                continue
            ev = ics.to_event(v)
            if _touches(ev, start, end):
                out.append(ev)
        return out, whole
    except _UNREADABLE:
        return [_whole_span(name, "", "", start, end)], 1
