"""adapters/live.py: IMAP and SMTP for her mail, CalDAV for the owner's shared calendar.

No network: the real imaplib, smtplib and http.client talk to small fake servers on 127.0.0.1
(plain TCP in place of TLS, through the adapters' factory hooks). The fakes record what they
were sent, so a test can prove what Doris never does (no expunge, no Bcc, no retry, no title).
"""
import base64
import datetime as dt
import http.client
import http.server
import imaplib
import io
import re
import smtplib
import socket
import socketserver
import ssl
import threading
import time
import traceback
import unittest
import urllib.parse
import xml.etree.ElementTree as ET
from unittest import mock
from xml.sax.saxutils import escape

from doris import caps, ics, slots
from doris.adapters import live
from doris.adapters.base import CalendarAdapter, MailAdapter
from doris.adapters.sandbox import NotHers
from doris.shapes import (AlreadyExists, CalendarError, CalendarTimeout, Event, InboundItem, MailError,
                          SendUnknown, Slot)

UTC = dt.timezone.utc
DORIS = "doris.htm@fastmail.com"
OWNER = "whizz@fastmail.com"
PASSWORD = "dummy-app-pass-77"           # a stand-in: never a real key
CALDAV_URL = "https://caldav.fastmail.com/dav/calendars/user/doris.htm@fastmail.com/owner-shared-cal/"
LINK = "https://meet.example/doris-test-room"


def u(*args):
    return dt.datetime(*args, tzinfo=UTC)


def env(**overrides):
    values = {"DORIS_MAIL_ADDRESS": DORIS, "DORIS_APP_PASSWORD": PASSWORD, "DORIS_CALDAV_URL": CALDAV_URL}
    values.update(overrides)
    return {k: v for k, v in values.items() if v is not None}


def secret_free(test, exc):
    """No form of the password or of the Basic credentials appears in anything an error prints."""
    basic = base64.b64encode(("%s:%s" % (DORIS, PASSWORD)).encode()).decode()
    plain = base64.b64encode(("\0%s\0%s" % (DORIS, PASSWORD)).encode()).decode()
    printed = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    for text in (str(exc), repr(exc), printed):
        for secret in (PASSWORD, basic, plain):
            test.assertNotIn(secret, text)


# ===========================================================================================
# Configuration and hosts
# ===========================================================================================
class HostsTest(unittest.TestCase):

    def test_defaults_are_fastmails_documented_servers_plus_the_calendar_url_host(self):
        self.assertEqual(live.hosts(env()), ["imap.fastmail.com", "smtp.fastmail.com", "caldav.fastmail.com"])

    def test_overrides_are_followed_lowercased_and_listed_once(self):
        e = env(DORIS_IMAP_HOST="Mail.Example.COM", DORIS_SMTP_HOST="mail.example.com",
                DORIS_CALDAV_URL="https://DAV.example.com:8443/cal/")
        self.assertEqual(live.hosts(e), ["mail.example.com", "dav.example.com"])

    def test_empty_values_mean_the_default(self):
        e = env(DORIS_IMAP_HOST="", DORIS_IMAP_PORT="", DORIS_SMTP_HOST=" ", DORIS_SMTP_PORT="")
        self.assertEqual(live.hosts(e), ["imap.fastmail.com", "smtp.fastmail.com", "caldav.fastmail.com"])

    def test_the_calendar_url_is_required(self):
        with self.assertRaises(live.ConfigError) as cm:
            live.hosts(env(DORIS_CALDAV_URL=None))
        self.assertIn("DORIS_CALDAV_URL", str(cm.exception))

    def test_the_calendar_url_must_be_plain_https(self):
        for bad in ("http://caldav.fastmail.com/dav/cal/", "https://doris:%s@caldav.fastmail.com/cal/" % PASSWORD,
                    "https://caldav.fastmail.com/cal/?x=1", "https://caldav.fastmail.com/cal/#f",
                    "caldav.fastmail.com/cal/", "https:///cal/", "https://caldav.fastmail.com",
                    "https://caldav.fastmail.com/dav/my cal/", "https://caldav.fastmail.com/dav/caf\u00e9/",
                    "https://caldav.fastmail.com/dav/<cal>/"):
            with self.assertRaises(live.ConfigError, msg=bad) as cm:
                live.hosts(env(DORIS_CALDAV_URL=bad))
            self.assertIn("DORIS_CALDAV_URL", str(cm.exception))
            self.assertNotIn(PASSWORD, str(cm.exception))
            self.assertNotIn(bad, str(cm.exception))

    def test_a_host_that_is_not_a_bare_name_is_refused(self):
        for bad in ("imap.fastmail.com:993", "imaps://imap.fastmail.com", "imap fastmail", "-bad.example", "a/b"):
            with self.assertRaises(live.ConfigError, msg=bad) as cm:
                live.hosts(env(DORIS_IMAP_HOST=bad))
            self.assertIn("DORIS_IMAP_HOST", str(cm.exception))

    def test_a_port_out_of_range_is_refused(self):
        for bad in ("0", "65536", "imap", "-1", "99.5", "\u00b2", "\uff19\uff19\uff13"):
            with self.assertRaises(live.ConfigError, msg=bad):
                live.LiveMail(env(DORIS_SMTP_PORT=bad), DORIS)

    def test_config_error_is_a_value_error(self):
        self.assertTrue(issubclass(live.ConfigError, ValueError))


class ConstructTest(unittest.TestCase):

    def test_they_are_the_base_adapters(self):
        self.assertIsInstance(live.LiveMail(env(), DORIS), MailAdapter)
        self.assertIsInstance(live.LiveCalendar(env()), CalendarAdapter)

    def test_mail_needs_her_address_and_her_app_password(self):
        for missing in ("DORIS_MAIL_ADDRESS", "DORIS_APP_PASSWORD"):
            with self.assertRaises(live.ConfigError) as cm:
                live.LiveMail(env(**{missing: None}), DORIS)
            self.assertIn(missing, str(cm.exception))

    def test_calendar_needs_her_login_and_the_calendar_url(self):
        for missing in ("DORIS_MAIL_ADDRESS", "DORIS_APP_PASSWORD", "DORIS_CALDAV_URL"):
            with self.assertRaises(live.ConfigError) as cm:
                live.LiveCalendar(env(**{missing: None}))
            self.assertIn(missing, str(cm.exception))

    def test_mail_refuses_an_address_that_is_not_the_login(self):
        """She sends as herself with her one key: never as the owner, never as an alias."""
        with self.assertRaises(live.ConfigError):
            live.LiveMail(env(), OWNER)
        self.assertEqual(live.LiveMail(env(), DORIS.upper()).doris_addr, DORIS.upper())

    def test_mail_does_not_need_the_calendar_url(self):
        live.LiveMail(env(DORIS_CALDAV_URL=None), DORIS)

    def test_the_timeout_must_be_positive(self):
        for bad in (0, -1):
            with self.assertRaises(ValueError):
                live.LiveMail(env(), DORIS, timeout_s=bad)
            with self.assertRaises(ValueError):
                live.LiveCalendar(env(), timeout_s=bad)

    def test_default_timeouts_come_from_caps(self):
        self.assertEqual(live.LiveMail(env(), DORIS).timeout_s, caps.MAIL_TIMEOUT_S)
        self.assertEqual(live.LiveCalendar(env()).timeout_s, caps.CALENDAR_TIMEOUT_S)

    def test_the_password_never_shows_in_repr_str_or_vars(self):
        for adapter in (live.LiveMail(env(), DORIS), live.LiveCalendar(env())):
            for text in (repr(adapter), str(adapter), repr(vars(adapter))):
                self.assertNotIn(PASSWORD, text)
                self.assertNotIn(base64.b64encode(("%s:%s" % (DORIS, PASSWORD)).encode()).decode(), text)

    def test_env_names_lists_every_name_the_module_reads(self):
        self.assertEqual(set(live.ENV_NAMES), {"DORIS_MAIL_ADDRESS", "DORIS_APP_PASSWORD", "DORIS_IMAP_HOST",
                                               "DORIS_IMAP_PORT", "DORIS_SMTP_HOST", "DORIS_SMTP_PORT",
                                               "DORIS_CALDAV_URL"})


# ===========================================================================================
# A fake IMAP server: enough of IMAP4rev1 for the real imaplib to talk to
# ===========================================================================================
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
IMAP_VERBS_ALLOWED = {"CAPABILITY", "LOGIN", "LOGOUT", "NOOP", "SELECT", "EXAMINE", "LIST",
                      "UID SEARCH", "UID FETCH", "UID STORE"}


def internaldate(moment):
    """INTERNALDATE text for an aware datetime, written in that datetime's own offset."""
    minutes = int(moment.utcoffset().total_seconds() // 60)
    sign, minutes = ("+" if minutes >= 0 else "-"), abs(minutes)
    return "%02d-%s-%04d %02d:%02d:%02d %s%02d%02d" % (moment.day, MONTHS[moment.month - 1], moment.year,
                                                      moment.hour, moment.minute, moment.second, sign,
                                                      minutes // 60, minutes % 60)


def imap_tokens(text):
    """Atoms (brackets kept whole), quoted strings (unescaped) and parenthesized lists (raw)."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == " ":
            i += 1
        elif c == '"':
            j, buf = i + 1, []
            while j < n and text[j] != '"':
                if text[j] == "\\":
                    j += 1
                buf.append(text[j])
                j += 1
            out.append(("q", "".join(buf)))
            i = j + 1
        elif c == "(":
            depth, j = 0, i
            while j < n:
                depth += {"(": 1, ")": -1}.get(text[j], 0)
                if depth == 0:
                    break
                j += 1
            out.append(("l", text[i + 1:j]))
            i = j + 1
        else:
            j, depth = i, 0
            while j < n and (text[j] != " " or depth):
                depth += {"[": 1, "]": -1}.get(text[j], 0)
                j += 1
            out.append(("a", text[i:j]))
            i = j
    return out


def uid_set(text, uids):
    chosen, top = set(), max(uids) if uids else 0
    for part in text.split(","):
        if ":" in part:
            a, b = (top if x == "*" else int(x) for x in part.split(":"))
            chosen.update(x for x in uids if min(a, b) <= x <= max(a, b))
        elif (top if part == "*" else int(part)) in uids:
            chosen.add(top if part == "*" else int(part))
    return sorted(chosen)


def header_field(raw, name):
    """The raw header lines (continuations included) of one field, plus the blank line."""
    head = raw.split(b"\r\n\r\n", 1)[0].split(b"\r\n")
    keep, on = [], False
    for line in head:
        if line[:1] in (b" ", b"\t"):
            if on:
                keep.append(line)
            continue
        on = line.split(b":", 1)[0].strip().lower() == name.lower().encode()
        if on:
            keep.append(line)
    return b"\r\n".join(keep) + b"\r\n\r\n" if keep else b"\r\n"


class Box(object):
    def __init__(self, uidvalidity, special=()):
        self.uidvalidity = uidvalidity
        self.special = set(special)
        self.messages = {}            # uid -> {"date": INTERNALDATE text, "raw": bytes, "flags": set}
        self.next_uid = 1

    def add(self, raw, when, flags=()):
        uid = self.next_uid
        self.next_uid += 1
        self.messages[uid] = {"date": internaldate(when), "raw": raw, "flags": set(flags)}
        return uid

    def day_of(self, uid):
        d, mon, y = self.messages[uid]["date"].split(" ")[0].split("-")
        return dt.date(int(y), MONTHS.index(mon) + 1, int(d))


class FakeIMAP(socketserver.ThreadingTCPServer):
    daemon_threads = True
    block_on_close = False            # a handler told to hang must not hold up the test's teardown
    allow_reuse_address = True

    def __init__(self):
        socketserver.ThreadingTCPServer.__init__(self, ("127.0.0.1", 0), IMAPHandler)
        self.user, self.password = DORIS, PASSWORD
        self.boxes = {"INBOX": Box(1700000001)}
        self.commands = []            # (verb, args) for every command; LOGIN args are not kept
        self.logins = []              # the user name of every LOGIN
        self.fault = {}               # verb -> "drop" | "hang" | "bad" | "no"
        self.hang_s = 1.5
        self.uid_after_body = False   # put "UID n" after the literal, as some servers do
        self.unsolicited = False      # slip an unrelated FETCH (flags) update into answers
        self.omit = set()             # uids whose answers the server leaves out
        self.no_uidvalidity = False
        self.store_quiet = False      # STORE sends no FETCH back
        self.store_ignored = False    # STORE answers OK but keeps nothing
        self.list_return_refused = False
        self.special_in_plain_list = True
        self.literal_names = False
        self.extended_data = False
        self.thread = threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    @property
    def port(self):
        return self.server_address[1]

    def stop(self):
        self.shutdown()
        self.server_close()

    def verbs(self):
        return [verb for verb, _ in self.commands]


class IMAPHandler(socketserver.StreamRequestHandler):
    disable_nagle_algorithm = True    # many small writes: no 40 ms stalls

    def out(self, text):
        self.wfile.write(text.encode("utf-8") + b"\r\n")

    def out_raw(self, data):
        self.wfile.write(data)

    def handle(self):
        srv = self.server
        self.box, self.readonly = None, True
        self.out("* OK [CAPABILITY IMAP4rev1 LIST-EXTENDED SPECIAL-USE] fake IMAP ready")
        while True:
            line = self.rfile.readline()
            if not line:
                return
            tag, _, rest = line.rstrip(b"\r\n").decode("utf-8").partition(" ")
            verb, _, args = rest.partition(" ")
            verb = verb.upper()
            if verb == "UID":
                sub, _, args = args.partition(" ")
                verb = "UID " + sub.upper()
            srv.commands.append((verb, "" if verb == "LOGIN" else args))
            fault = srv.fault.get(verb)
            if fault == "drop":
                return
            if fault == "hang":
                time.sleep(srv.hang_s)
                return
            if fault in ("bad", "no"):
                self.out("%s %s refused by the fake" % (tag, fault.upper()))
                continue
            handler = getattr(self, "do_" + verb.replace(" ", "_"), None)
            if handler is None:
                self.out("%s BAD unknown command" % tag)
                continue
            if handler(tag, args) == "close":
                return

    def do_CAPABILITY(self, tag, args):
        self.out("* CAPABILITY IMAP4rev1 LIST-EXTENDED SPECIAL-USE")
        self.out("%s OK CAPABILITY completed" % tag)

    def do_NOOP(self, tag, args):
        self.out("%s OK NOOP completed" % tag)

    def do_LOGOUT(self, tag, args):
        self.out("* BYE fake IMAP logging out")
        self.out("%s OK LOGOUT completed" % tag)
        return "close"

    def do_LOGIN(self, tag, args):
        user, password = (value for _, value in imap_tokens(args))
        self.server.logins.append(user)
        if (user, password) == (self.server.user, self.server.password):
            self.out("%s OK LOGIN completed" % tag)
        else:
            self.out("%s NO [AUTHENTICATIONFAILED] Authentication failed" % tag)

    def _open(self, tag, args, readonly):
        name = imap_tokens(args)[0][1]
        key = "INBOX" if name.upper() == "INBOX" else name
        box = self.server.boxes.get(key)
        if box is None:
            self.out("%s NO no such mailbox" % tag)
            return
        self.box, self.readonly = box, readonly
        self.out("* FLAGS (\\Answered \\Flagged \\Deleted \\Seen \\Draft)")
        self.out("* OK [PERMANENTFLAGS (\\Answered \\Flagged \\Deleted \\Seen \\Draft \\*)] ok")
        self.out("* %d EXISTS" % len(box.messages))
        self.out("* 0 RECENT")
        if not self.server.no_uidvalidity:
            self.out("* OK [UIDVALIDITY %d] UIDs valid" % box.uidvalidity)
        self.out("* OK [UIDNEXT %d] next" % box.next_uid)
        self.out("%s OK [%s] done" % (tag, "READ-ONLY" if readonly else "READ-WRITE"))

    def do_SELECT(self, tag, args):
        self._open(tag, args, readonly=False)

    def do_EXAMINE(self, tag, args):
        self._open(tag, args, readonly=True)

    def do_LIST(self, tag, args):
        srv = self.server
        extended = "RETURN" in args.upper()
        if extended and srv.list_return_refused:
            self.out("%s BAD RETURN options are not supported here" % tag)
            return
        for name, box in srv.boxes.items():
            attrs = " ".join(["\\HasNoChildren"] + (sorted(box.special) if extended or srv.special_in_plain_list
                                                     else []))
            tail = ' ("CHILDINFO" ("SUBSCRIBED"))' if srv.extended_data else ""
            if srv.literal_names:
                self.out_raw(('* LIST (%s) "/" {%d}\r\n' % (attrs, len(name.encode()))).encode()
                             + name.encode() + tail.encode() + b"\r\n")
            else:
                quoted = '"%s"' % name.replace("\\", "\\\\").replace('"', '\\"')
                self.out('* LIST (%s) "/" %s%s' % (attrs, quoted, tail))
        self.out("%s OK LIST completed" % tag)

    def _need_box(self, tag):
        if self.box is None:
            self.out("%s BAD no mailbox selected" % tag)
        return self.box

    def do_UID_SEARCH(self, tag, args):
        box = self._need_box(tag)
        if box is None:
            return
        uids = sorted(box.messages)
        found, toks, i = set(uids), imap_tokens(args), 0
        while i < len(toks):
            key = toks[i][1].upper()
            if key == "ALL":
                i += 1
            elif key == "SINCE":
                d, mon, y = toks[i + 1][1].split("-")
                since = dt.date(int(y), MONTHS.index(mon) + 1, int(d))
                found &= {x for x in uids if box.day_of(x) >= since}
                i += 2
            elif key == "HEADER":
                field, needle = toks[i + 1][1], toks[i + 2][1].lower()
                found &= {x for x in uids
                          if needle in header_field(box.messages[x]["raw"], field).decode("utf-8", "replace").lower()}
                i += 3
            else:
                self.out("%s BAD unknown search key" % tag)
                return
        self.out("* SEARCH" + "".join(" %d" % x for x in sorted(found)))
        self.out("%s OK SEARCH completed" % tag)

    def do_UID_FETCH(self, tag, args):
        srv, box = self.server, self._need_box(tag)
        if box is None:
            return
        set_text, _, items = args.partition(" ")
        items_u = items.upper()
        uids = sorted(box.messages)
        if srv.unsolicited:
            self.out("* 1 FETCH (FLAGS (\\Seen \\Recent))")
        for uid in uid_set(set_text, uids):
            if uid in srv.omit:
                continue
            msg, seq = box.messages[uid], uids.index(uid) + 1
            head = [] if srv.uid_after_body else ["UID %d" % uid]
            if "INTERNALDATE" in items_u:
                head.append('INTERNALDATE "%s"' % msg["date"])
            if "FLAGS" in items_u:
                head.append("FLAGS (%s)" % " ".join(sorted(msg["flags"])))
            literal = None
            if "BODY.PEEK[]" in items_u or "BODY[]" in items_u:
                literal = ("BODY[]", msg["raw"])
                if "BODY.PEEK[]" not in items_u and not self.readonly:
                    msg["flags"].add("\\Seen")
            elif "HEADER.FIELDS (MESSAGE-ID)" in items_u:
                literal = ("BODY[HEADER.FIELDS (MESSAGE-ID)]", header_field(msg["raw"], "message-id"))
            tail = " UID %d" % uid if srv.uid_after_body else ""
            if literal is None:
                self.out("* %d FETCH (%s%s)" % (seq, " ".join(head), tail))
            else:
                lead = " ".join(head) + " " if head else ""
                self.out_raw(("* %d FETCH (%s%s {%d}\r\n" % (seq, lead, literal[0], len(literal[1]))).encode()
                             + literal[1] + (tail + ")\r\n").encode())
        self.out("%s OK FETCH completed" % tag)

    def do_UID_STORE(self, tag, args):
        srv, box = self.server, self._need_box(tag)
        if box is None:
            return
        set_text, op, flags = args.split(" ", 2)
        if self.readonly:
            self.out("%s NO mailbox is read-only" % tag)
            return
        uids = sorted(box.messages)
        for uid in uid_set(set_text, uids):
            msg = box.messages[uid]
            if op.upper().startswith("+FLAGS") and not srv.store_ignored:
                msg["flags"].update(flags.strip("()").split())
            if not op.upper().endswith(".SILENT") and not srv.store_quiet:
                self.out("* %d FETCH (UID %d FLAGS (%s))" % (uids.index(uid) + 1, uid, " ".join(sorted(msg["flags"]))))
        self.out("%s OK STORE completed" % tag)


def eml(n, mid=None, extra=b""):
    mid = mid or "msg-%d@example.com" % n
    return (b"From: Sender %d <sender%d@example.com>\r\nTo: " % (n, n) + DORIS.encode()
            + b"\r\nSubject: hello %d\r\nMessage-ID: <" % n + mid.encode() + b">\r\n" + extra
            + b"\r\nBody %d\r\n.leading dot line\r\n" % n)


class ImapFixture(unittest.TestCase):
    """LiveMail against the fake IMAP server. After each test: only allowed verbs were used,
    nothing was ever flagged deleted, and every body fetch was a PEEK."""

    def setUp(self):
        self.imap = FakeIMAP()
        self.addCleanup(self.imap.stop)
        self.dialed = []
        self.inbox = self.imap.boxes["INBOX"]
        self.mail = self.make_mail()

    def make_mail(self, **kw):
        e = env(DORIS_IMAP_HOST="imap.example.test", DORIS_IMAP_PORT="1993")
        return live.LiveMail(e, DORIS, timeout_s=kw.pop("timeout_s", 2.0), imap_factory=self.factory, **kw)

    def factory(self, host, port, timeout):
        self.dialed.append((host, port, timeout))
        return imaplib.IMAP4("127.0.0.1", self.imap.port, timeout=timeout)

    def tearDown(self):
        for verb, args in self.imap.commands:
            self.assertIn(verb, IMAP_VERBS_ALLOWED)
            self.assertNotIn("\\DELETED", args.upper())
            if verb == "UID FETCH":
                self.assertNotRegex(args.upper(), r"(?<!PEEK)\[[^\]]*\]")


# ===========================================================================================
# Mail: fetch
# ===========================================================================================
class FetchTest(ImapFixture):

    def test_hands_over_every_message_oldest_first_by_internaldate(self):
        plus2 = dt.timezone(dt.timedelta(hours=2))
        uid1 = self.inbox.add(eml(1), dt.datetime(2026, 11, 27, 12, 0, 5, tzinfo=plus2))
        uid2 = self.inbox.add(eml(2), u(2026, 11, 27, 9, 30, 0))
        uid3 = self.inbox.add(eml(3), u(2026, 11, 28, 1, 0, 0))
        items = self.mail.fetch_new()
        self.assertEqual([i.server_id for i in items],
                         ["INBOX:1700000001:%d" % uid for uid in (uid2, uid1, uid3)])
        self.assertEqual([i.raw for i in items], [eml(2), eml(1), eml(3)])
        self.assertEqual([i.received_utc for i in items],
                         [u(2026, 11, 27, 9, 30), u(2026, 11, 27, 10, 0, 5), u(2026, 11, 28, 1, 0)])
        self.assertTrue(all(isinstance(i, InboundItem) and i.facts == {} for i in items))
        self.assertTrue(all(i.received_utc.tzinfo == UTC for i in items))

    def test_dials_the_configured_host_and_port_with_the_timeout(self):
        self.mail.fetch_new()
        self.assertEqual(self.dialed, [("imap.example.test", 1993, 2.0)])
        self.assertEqual(self.imap.logins, [DORIS])

    def test_reads_inbox_read_only_with_peek_so_seen_never_changes(self):
        uid = self.inbox.add(eml(1), u(2026, 11, 27, 9, 0))
        self.mail.fetch_new()
        self.assertIn("EXAMINE", self.imap.verbs())
        self.assertNotIn("SELECT", self.imap.verbs())
        self.assertIn("BODY.PEEK[]", " ".join(a for v, a in self.imap.commands if v == "UID FETCH"))
        self.assertEqual(self.inbox.messages[uid]["flags"], set())

    def test_a_start_time_keeps_only_mail_received_since_then(self):
        self.inbox.add(eml(1), u(2026, 11, 20, 9, 0))
        self.inbox.add(eml(2), u(2026, 11, 27, 9, 59, 59))
        uid3 = self.inbox.add(eml(3), u(2026, 11, 27, 10, 0))
        uid4 = self.inbox.add(eml(4), u(2026, 11, 28, 8, 0))
        mail = self.make_mail(since_utc=u(2026, 11, 27, 10, 0))
        items = mail.fetch_new()
        self.assertEqual([i.server_id for i in items], ["INBOX:1700000001:%d" % uid3, "INBOX:1700000001:%d" % uid4])
        searches = [a for v, a in self.imap.commands if v == "UID SEARCH"]
        self.assertEqual(searches, ["SINCE 26-Nov-2026"])

    def test_the_start_time_can_be_set_after_construction(self):
        self.inbox.add(eml(1), u(2026, 11, 20, 9, 0))
        uid2 = self.inbox.add(eml(2), u(2026, 11, 28, 8, 0))
        self.mail.since_utc = u(2026, 11, 27)
        self.assertEqual([i.server_id for i in self.mail.fetch_new()], ["INBOX:1700000001:%d" % uid2])

    def test_a_naive_start_time_is_refused(self):
        with self.assertRaises(ValueError):
            self.make_mail(since_utc=dt.datetime(2026, 11, 27))

    def test_any_answer_order_and_unsolicited_updates_are_read_right(self):
        self.imap.uid_after_body = True
        self.imap.unsolicited = True
        self.inbox.add(eml(1), u(2026, 11, 27, 9, 0))
        self.inbox.add(eml(2), u(2026, 11, 27, 9, 5))
        items = self.mail.fetch_new()
        self.assertEqual([i.raw for i in items], [eml(1), eml(2)])
        self.assertEqual([i.server_id for i in items], ["INBOX:1700000001:1", "INBOX:1700000001:2"])

    def test_server_ids_are_stable_across_runs_and_change_with_uidvalidity(self):
        self.inbox.add(eml(1), u(2026, 11, 27, 9, 0))
        first = [i.server_id for i in self.mail.fetch_new()]
        self.assertEqual([i.server_id for i in self.make_mail().fetch_new()], first)
        self.inbox.uidvalidity = 1800000002
        self.assertEqual([i.server_id for i in self.mail.fetch_new()], ["INBOX:1800000002:1"])

    def test_many_messages_are_fetched_in_batches(self):
        for n in range(1, 131):
            self.inbox.add(eml(n), u(2026, 11, 27, 9, 0) + dt.timedelta(minutes=n))
        items = self.mail.fetch_new()
        self.assertEqual([i.raw for i in items], [eml(n) for n in range(1, 131)])
        self.assertGreater(len([v for v in self.imap.verbs() if v == "UID FETCH"]), 2)

    def test_an_empty_inbox_is_an_empty_list(self):
        self.assertEqual(self.mail.fetch_new(), [])

    def test_a_refused_login_is_a_mail_error_that_carries_no_secret(self):
        self.imap.password = "dummy-other-pass"
        with self.assertRaises(MailError) as cm:
            self.mail.fetch_new()
        self.assertNotIsInstance(cm.exception, SendUnknown)
        self.assertIn("login", str(cm.exception))
        secret_free(self, cm.exception)

    def test_a_dropped_connection_is_a_mail_error_never_an_empty_inbox(self):
        self.inbox.add(eml(1), u(2026, 11, 27, 9, 0))
        self.imap.fault["UID FETCH"] = "drop"
        with self.assertRaises(MailError) as cm:
            self.mail.fetch_new()
        secret_free(self, cm.exception)

    def test_a_server_that_hangs_times_out_as_a_mail_error(self):
        self.imap.fault["EXAMINE"] = "hang"
        started = time.monotonic()
        with self.assertRaises(MailError):
            self.make_mail(timeout_s=0.3).fetch_new()
        self.assertLess(time.monotonic() - started, 1.4)

    def test_a_refused_examine_is_a_mail_error_naming_the_status(self):
        self.imap.fault["EXAMINE"] = "no"
        with self.assertRaises(MailError) as cm:
            self.mail.fetch_new()
        self.assertIn("NO", str(cm.exception))

    def test_no_uidvalidity_is_a_failure_not_a_guess(self):
        self.inbox.add(eml(1), u(2026, 11, 27, 9, 0))
        self.imap.no_uidvalidity = True
        with self.assertRaises(MailError):
            self.mail.fetch_new()

    def test_an_internaldate_at_the_edge_of_time_is_a_mail_error_not_an_overflow(self):
        for date in ("01-Jan-0001 00:00:00 +0100", "31-Dec-9999 23:30:00 -0100"):
            self.inbox.messages.clear()
            uid = self.inbox.add(eml(1), u(2026, 11, 27, 9, 0))
            self.inbox.messages[uid]["date"] = date
            err = self.fails_with(MailError, self.mail.fetch_new)
            self.assertIn("INTERNALDATE", str(err), date)

    def fails_with(self, cls, call, *args):
        with self.assertRaises(Exception) as cm:
            call(*args)
        self.assertIsInstance(cm.exception, cls)
        secret_free(self, cm.exception)
        return cm.exception

    def test_a_body_left_out_of_the_answer_is_a_failure(self):
        self.inbox.add(eml(1), u(2026, 11, 27, 9, 0))
        self.imap.omit.add(1)
        with self.assertRaises(MailError):
            self.mail.fetch_new()

    def test_a_connect_failure_is_a_mail_error(self):
        def refuse(host, port, timeout):
            raise ConnectionRefusedError("nothing listening")
        mail = live.LiveMail(env(), DORIS, imap_factory=refuse)
        with self.assertRaises(MailError) as cm:
            mail.fetch_new()
        self.assertIn("ConnectionRefusedError", str(cm.exception))


# ===========================================================================================
# Mail: filing is a keyword on her own copy
# ===========================================================================================
class FileTest(ImapFixture):

    def setUp(self):
        ImapFixture.setUp(self)
        self.uid = self.inbox.add(eml(1), u(2026, 11, 27, 9, 0), flags={"\\Seen"})
        self.sid = "INBOX:1700000001:%d" % self.uid

    def test_adds_the_keyword_to_her_copy_and_reads_it_back(self):
        self.mail.file(self.sid, "doris-file")
        self.assertEqual(self.inbox.messages[self.uid]["flags"], {"\\Seen", "doris-file"})
        self.assertEqual([a for v, a in self.imap.commands if v == "UID STORE"],
                         ["%d +FLAGS (doris-file)" % self.uid])
        self.assertIn("SELECT", self.imap.verbs())

    def test_reads_the_flags_back_when_the_store_answer_is_silent(self):
        self.imap.store_quiet = True
        self.mail.file(self.sid, "doris-held")
        self.assertIn("doris-held", self.inbox.messages[self.uid]["flags"])
        self.assertEqual(self.imap.verbs()[-2:], ["UID FETCH", "LOGOUT"])

    def test_a_keyword_the_server_did_not_keep_is_a_failure(self):
        self.imap.store_ignored = True
        with self.assertRaises(MailError):
            self.mail.file(self.sid, "doris-file")

    def test_a_bad_keyword_is_refused_before_any_call(self):
        for bad in ("", "two words", "\\Deleted", "\\Seen", "a(b", "x" * 65, None, 7, "doris-file\n"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.mail.file(self.sid, bad)
        self.assertEqual(self.imap.commands, [])

    def test_an_id_this_adapter_did_not_hand_out_is_refused_before_any_call(self):
        for bad in ("01-priya-request.eml", "INBOX:x:1", "Sent:1700000001:1", "INBOX:1:2:3", "INBOX:0:1", None,
                    "INBOX:1700000001:1\n"):
            with self.assertRaises(MailError, msg=repr(bad)):
                self.mail.file(bad, "doris-file")
        self.assertEqual(self.imap.commands, [])

    def test_a_changed_uidvalidity_files_nothing(self):
        self.inbox.uidvalidity = 1800000002
        with self.assertRaises(MailError):
            self.mail.file(self.sid, "doris-file")
        self.assertNotIn("UID STORE", self.imap.verbs())
        self.assertEqual(self.inbox.messages[self.uid]["flags"], {"\\Seen"})

    def test_a_message_that_is_gone_is_a_failure(self):
        with self.assertRaises(MailError):
            self.mail.file("INBOX:1700000001:99", "doris-file")

    def test_a_refused_store_is_a_mail_error(self):
        self.imap.fault["UID STORE"] = "no"
        with self.assertRaises(MailError) as cm:
            self.mail.file(self.sid, "doris-file")
        self.assertIn("NO", str(cm.exception))


# ===========================================================================================
# Mail: find_in_sent looks in the folder flagged \Sent, by Message-ID, and writes nothing
# ===========================================================================================
class SentTest(ImapFixture):

    def setUp(self):
        ImapFixture.setUp(self)
        self.sent = self.imap.boxes["Sent Items"] = Box(1700000002, special=("\\Sent",))

    def test_finds_her_message_by_message_id_with_or_without_brackets(self):
        self.sent.add(eml(1, mid="0f3a9c@fastmail.com"), u(2026, 11, 27, 9, 0))
        self.assertTrue(self.mail.find_in_sent("<0f3a9c@fastmail.com>"))
        self.assertTrue(self.mail.find_in_sent("0f3a9c@fastmail.com"))
        self.assertFalse(self.mail.find_in_sent("<ffffff@fastmail.com>"))

    def test_the_folder_is_found_by_its_special_use_flag_never_by_its_name(self):
        decoy = self.imap.boxes["Sent"] = Box(1700000003)
        decoy.add(eml(2, mid="decoy@fastmail.com"), u(2026, 11, 27, 9, 0))
        self.assertFalse(self.mail.find_in_sent("<decoy@fastmail.com>"))
        self.assertEqual([a for v, a in self.imap.commands if v == "EXAMINE"], ['"Sent Items"'])

    def test_a_message_id_that_only_contains_hers_is_not_a_match(self):
        self.sent.add(eml(1, mid="x0f3a9c@fastmail.com.example"), u(2026, 11, 27, 9, 0))
        self.assertFalse(self.mail.find_in_sent("<0f3a9c@fastmail.com>"))

    def test_reads_read_only_and_never_writes(self):
        uid = self.sent.add(eml(1, mid="0f3a9c@fastmail.com"), u(2026, 11, 27, 9, 0))
        self.mail.find_in_sent("<0f3a9c@fastmail.com>")
        self.assertTrue(set(self.imap.verbs()) <= {"CAPABILITY", "LOGIN", "LIST", "EXAMINE", "UID SEARCH",
                                                   "UID FETCH", "LOGOUT"})
        self.assertEqual(self.sent.messages[uid]["flags"], set())

    def test_asks_for_special_use_and_falls_back_to_a_plain_list(self):
        self.sent.add(eml(1, mid="0f3a9c@fastmail.com"), u(2026, 11, 27, 9, 0))
        self.assertTrue(self.mail.find_in_sent("<0f3a9c@fastmail.com>"))
        self.assertEqual([a for v, a in self.imap.commands if v == "LIST"], ['"" "*" RETURN (SPECIAL-USE)'])
        self.imap.list_return_refused = True
        self.assertTrue(self.mail.find_in_sent("<0f3a9c@fastmail.com>"))
        self.assertEqual([a for v, a in self.imap.commands if v == "LIST"][1:],
                         ['"" "*" RETURN (SPECIAL-USE)', '"" *'])

    def test_names_sent_as_literals_and_extended_list_data_are_read(self):
        self.imap.literal_names = True
        self.imap.extended_data = True
        self.sent.add(eml(1, mid="0f3a9c@fastmail.com"), u(2026, 11, 27, 9, 0))
        self.assertTrue(self.mail.find_in_sent("<0f3a9c@fastmail.com>"))

    def test_every_folder_flagged_sent_is_searched(self):
        other = self.imap.boxes["Sent Archive"] = Box(1700000004, special=("\\Sent",))
        other.add(eml(1, mid="0f3a9c@fastmail.com"), u(2026, 11, 27, 9, 0))
        self.assertTrue(self.mail.find_in_sent("<0f3a9c@fastmail.com>"))

    def test_no_folder_flagged_sent_is_a_failure_not_a_guess(self):
        self.sent.special = set()
        self.imap.boxes["Sent"] = Box(1700000003)
        with self.assertRaises(MailError) as cm:
            self.mail.find_in_sent("<0f3a9c@fastmail.com>")
        self.assertIn("Sent", str(cm.exception))

    def test_a_lookup_that_fails_is_a_mail_error_never_false(self):
        self.imap.fault["UID SEARCH"] = "no"
        with self.assertRaises(MailError):
            self.mail.find_in_sent("<0f3a9c@fastmail.com>")

    def test_a_message_id_it_cannot_search_for_is_refused_before_any_call(self):
        for bad in ("", "<>", None, 'a"b@fastmail.com', "no-at-sign", "a b@fastmail.com", "a\r\nb@fastmail.com",
                    "a\\b@fastmail.com", "<<x@fastmail.com>>x"):
            with self.assertRaises(MailError, msg=repr(bad)):
                self.mail.find_in_sent(bad)
        self.assertEqual(self.imap.commands, [])


class ProbeTest(ImapFixture):
    """What the smoke checks: login with the INBOX count, and the Sent folder by special-use."""

    def test_inbox_count_examines_inbox(self):
        for n in range(3):
            self.inbox.add(eml(n), u(2026, 11, 27, 9, n))
        self.assertEqual(self.mail.inbox_count(), 3)
        self.assertIn("EXAMINE", self.imap.verbs())
        self.assertNotIn("SELECT", self.imap.verbs())

    def test_sent_folders_names_every_folder_flagged_sent(self):
        self.imap.boxes["Sent Items"] = Box(1700000002, special=("\\Sent",))
        self.imap.boxes["Drafts"] = Box(1700000005, special=("\\Drafts",))
        self.assertEqual(self.mail.sent_folders(), ["Sent Items"])

    def test_probes_fail_loudly(self):
        self.imap.password = "dummy-other-pass"
        for probe in (self.mail.inbox_count, self.mail.sent_folders):
            with self.assertRaises(MailError):
                probe()


class DefaultTransportTest(unittest.TestCase):
    """Without a factory the adapters use TLS that verifies the server, with the timeout given."""

    def test_tls_is_1_2_or_newer_even_where_the_default_context_allows_older(self):
        real = ssl.create_default_context

        def as_on_3_9(*args, **kw):          # 3.9's default context sets no floor of its own
            ctx = real(*args, **kw)
            ctx.minimum_version = ssl.TLSVersion.MINIMUM_SUPPORTED
            return ctx
        with mock.patch.object(ssl, "create_default_context", as_on_3_9):
            ctx = live._tls()
        self.assertEqual(ctx.minimum_version, ssl.TLSVersion.TLSv1_2)
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)

    def test_imap_is_tls_with_a_verified_certificate_and_the_timeout(self):
        seen = {}

        def fake(host, port, ssl_context=None, timeout=None):
            seen.update(host=host, port=port, ctx=ssl_context, timeout=timeout)
            raise ConnectionRefusedError("stop here")
        with mock.patch.object(imaplib, "IMAP4_SSL", fake):
            with self.assertRaises(MailError):
                live.LiveMail(env(), DORIS, timeout_s=7).fetch_new()
        self.assertEqual((seen["host"], seen["port"], seen["timeout"]), ("imap.fastmail.com", 993, 7.0))
        self.assertEqual(seen["ctx"].verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(seen["ctx"].check_hostname)

    def test_smtp_is_implicit_tls_with_a_verified_certificate_and_the_timeout(self):
        seen = {}

        def fake(host="", port=0, local_hostname=None, **kw):
            seen.update(host=host, port=port, name=local_hostname, **kw)
            raise ConnectionRefusedError("stop here")
        with mock.patch.object(smtplib, "SMTP_SSL", fake):
            with self.assertRaises(MailError) as cm:
                live.LiveMail(env(), DORIS, timeout_s=7).check_smtp_login()
        self.assertNotIsInstance(cm.exception, SendUnknown)
        self.assertEqual((seen["host"], seen["port"], seen["timeout"]), ("smtp.fastmail.com", 465, 7.0))
        self.assertEqual(seen["context"].verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(seen["context"].check_hostname)
        self.assertEqual(seen["name"], "[127.0.0.1]")     # EHLO names no machine of ours


# ===========================================================================================
# A fake SMTP submission server for the real smtplib
# ===========================================================================================
class FakeSMTP(socketserver.ThreadingTCPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True

    def __init__(self):
        socketserver.ThreadingTCPServer.__init__(self, ("127.0.0.1", 0), SMTPHandler)
        self.user, self.password = DORIS, PASSWORD
        self.sessions = []            # per connection: {"ehlo", "auth", "verbs"}; never the AUTH payload
        self.delivered = []           # (envelope sender, [recipients], message bytes) for each accepted message
        self.mail_code = 250
        self.refuse_rcpt = {}         # address -> refusal code
        self.data_cmd_code = 354
        self.final_code = 250
        self.on_data_cmd = None       # "drop": close on the DATA command, before 354
        self.after_body = None        # "drop" | "hang" | "garbage": what follows the final dot
        self.hang_s = 1.5
        self.on_deliver = None        # called (sender, recipients, message) for each accepted message
        self.thread = threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    @property
    def port(self):
        return self.server_address[1]

    def stop(self):
        self.shutdown()
        self.server_close()

    def verbs(self):
        return [verb for s in self.sessions for verb in s["verbs"]]


class SMTPHandler(socketserver.StreamRequestHandler):
    disable_nagle_algorithm = True

    def out(self, text):
        self.wfile.write(text.encode("utf-8") + b"\r\n")

    def handle(self):
        srv = self.server
        session = {"ehlo": None, "auth": None, "verbs": []}
        srv.sessions.append(session)
        tx = None
        self.out("220 fake.smtp ESMTP ready")
        while True:
            line = self.rfile.readline()
            if not line:
                return
            text = line.rstrip(b"\r\n").decode("utf-8", "replace")
            verb = text.split(" ", 1)[0].upper()
            session["verbs"].append(verb)
            addr = text.split(":", 1)[1].strip().strip("<>") if ":" in text else ""
            if verb == "EHLO":
                session["ehlo"] = text[5:]
                self.out("250-fake.smtp\r\n250-AUTH PLAIN\r\n250 SIZE 35000000")
            elif verb == "AUTH":
                parts = text.split()
                _, user, given = base64.b64decode(parts[2]).decode("utf-8").split("\0")
                session["auth"] = user if (user, given) == (srv.user, srv.password) else None
                self.out("235 2.7.0 ok" if session["auth"] else "535 5.7.8 Authentication failed")
            elif verb == "MAIL":
                if session["auth"] is None:
                    self.out("530 5.7.0 Authentication required")
                    continue
                tx = {"from": addr, "rcpts": []} if srv.mail_code == 250 else None
                self.out("%d sender" % srv.mail_code)
            elif verb == "RCPT":
                code = srv.refuse_rcpt.get(addr, 250) if tx is not None else 503
                if code == 250:
                    tx["rcpts"].append(addr)
                self.out("%d recipient" % code)
            elif verb == "DATA":
                if srv.on_data_cmd == "drop":
                    return
                if tx is None or not tx["rcpts"] or srv.data_cmd_code != 354:
                    self.out("%d no data now" % (srv.data_cmd_code if srv.data_cmd_code != 354 else 503))
                    continue
                self.out("354 go ahead")
                lines = []
                while True:
                    body_line = self.rfile.readline()
                    if not body_line:
                        return
                    if body_line == b".\r\n":
                        break
                    lines.append(body_line[1:] if body_line.startswith(b".") else body_line)
                if srv.after_body == "drop":
                    return
                if srv.after_body == "hang":
                    time.sleep(srv.hang_s)
                    return
                if srv.after_body == "garbage":
                    self.out("what")
                    continue
                if srv.final_code == 250:
                    srv.delivered.append((tx["from"], tx["rcpts"], b"".join(lines)))
                    if srv.on_deliver is not None:
                        srv.on_deliver(tx["from"], tx["rcpts"], b"".join(lines))
                self.out("%d done" % srv.final_code)
                tx = None
            elif verb == "RSET":
                tx = None
                self.out("250 reset")
            elif verb == "NOOP":
                self.out("250 ok")
            elif verb == "QUIT":
                self.out("221 bye")
                return
            else:
                self.out("502 unknown")


PROSPECT = "prospect@example.com"
MID = "<5d1e0c2b9a@fastmail.com>"


def reply_bytes(mid=MID, sender=DORIS, extra=b""):
    return (b"From: " + sender.encode() + b"\r\nTo: " + PROSPECT.encode() + b"\r\nSubject: Re: hello\r\n"
            b"Date: Fri, 27 Nov 2026 18:00:00 +0000\r\nMessage-ID: " + mid.encode() + b"\r\n" + extra
            + b"Auto-Submitted: auto-replied\r\nMIME-Version: 1.0\r\nContent-Type: text/plain; charset=\"utf-8\"\r\n"
            b"\r\nHello.\r\n.a line that starts with a dot\r\n..and one with two\r\n")


class SmtpFixture(unittest.TestCase):

    def setUp(self):
        self.smtp = FakeSMTP()
        self.addCleanup(self.smtp.stop)
        self.dialed = []
        self.mail = self.make_mail()

    def make_mail(self, timeout_s=2.0):
        e = env(DORIS_SMTP_HOST="smtp.example.test", DORIS_SMTP_PORT="1465")
        return live.LiveMail(e, DORIS, timeout_s=timeout_s, smtp_factory=self.factory)

    def factory(self, host, port, timeout):
        self.dialed.append((host, port, timeout))
        return smtplib.SMTP("127.0.0.1", self.smtp.port, local_hostname="[127.0.0.1]", timeout=timeout)

    def assert_one_try(self):
        """Never a retry inside the adapter: one connection, at most one transaction."""
        self.assertEqual(len(self.smtp.sessions), 1)
        self.assertLessEqual(self.smtp.verbs().count("MAIL"), 1)
        self.assertLessEqual(self.smtp.verbs().count("DATA"), 1)

    def refused(self, *args, **kw):
        with self.assertRaises(MailError) as cm:
            self.mail.send(*args, **kw)
        self.assertNotIsInstance(cm.exception, SendUnknown)
        secret_free(self, cm.exception)
        return cm.exception


# ===========================================================================================
# Mail: send
# ===========================================================================================
class SendTest(SmtpFixture):

    def test_logs_in_as_her_and_hands_over_with_her_sender_and_exactly_the_recipients(self):
        self.mail.send(reply_bytes(), [PROSPECT, OWNER], MID)
        self.assertEqual(self.smtp.delivered, [(DORIS, [PROSPECT, OWNER], reply_bytes())])
        self.assertEqual(self.smtp.sessions[0]["auth"], DORIS)
        self.assertEqual(self.dialed, [("smtp.example.test", 1465, 2.0)])
        self.assert_one_try()

    def test_the_message_arrives_byte_for_byte_with_crlf_line_ends(self):
        lf_only = reply_bytes().replace(b"\r\n", b"\n")
        self.mail.send(lf_only, [PROSPECT], MID)
        self.assertEqual(self.smtp.delivered[0][2], reply_bytes())

    def test_the_template_name_is_accepted_and_ignored(self):
        self.mail.send(reply_bytes(), [PROSPECT], MID, template="T1")
        self.mail.send(reply_bytes(), [PROSPECT], MID, "T2")
        self.assertEqual(len(self.smtp.delivered), 2)

    def test_a_bcc_header_is_refused_before_connecting(self):
        for header in (b"Bcc: hidden@example.com\r\n", b"BCC: hidden@example.com\r\n", b"bcc :hidden@example.com\r\n",
                       b"Resent-Bcc: hidden@example.com\r\n", b"Bcc:\r\n"):
            err = self.refused(reply_bytes(extra=header), [PROSPECT, OWNER], MID)
            self.assertIn("Bcc", str(err))
        self.assertEqual(self.smtp.sessions, [])

    def test_a_message_not_from_her_or_not_the_recorded_one_is_refused_before_connecting(self):
        self.refused(reply_bytes(sender=OWNER), [PROSPECT], MID)
        self.refused(reply_bytes(), [PROSPECT], "<another@fastmail.com>")
        self.refused(reply_bytes(), [PROSPECT], "")
        self.refused(reply_bytes(extra=b"From: " + DORIS.encode() + b"\r\n"), [PROSPECT], MID)
        self.refused(reply_bytes(extra=b"Message-ID: " + MID.encode() + b"\r\n"), [PROSPECT], MID)
        self.refused(b"", [PROSPECT], MID)
        self.refused("text, not bytes", [PROSPECT], MID)
        self.assertEqual(self.smtp.sessions, [])

    def test_a_bad_envelope_is_refused_before_connecting(self):
        for bad in ([], PROSPECT, ["not an address"], [PROSPECT + "\r\nRCPT TO:<x@example.com>"], [None],
                    ["<%s>" % PROSPECT], ["Name <%s>" % PROSPECT], [PROSPECT + "\n"]):
            self.refused(reply_bytes(), bad, MID)
        self.assertEqual(self.smtp.sessions, [])

    def test_a_refused_login_is_a_mail_error_that_carries_no_secret(self):
        self.smtp.password = "dummy-other-pass"
        err = self.refused(reply_bytes(), [PROSPECT], MID)
        self.assertIn("login", str(err))
        self.assertNotIn("MAIL", self.smtp.verbs())
        self.assert_one_try()

    def test_a_refused_sender_is_a_mail_error_naming_the_code(self):
        self.smtp.mail_code = 553
        err = self.refused(reply_bytes(), [PROSPECT], MID)
        self.assertIn("553", str(err))
        self.assertNotIn("DATA", self.smtp.verbs())
        self.assert_one_try()

    def test_one_refused_recipient_stops_the_whole_send_before_data(self):
        self.smtp.refuse_rcpt[OWNER] = 550
        err = self.refused(reply_bytes(), [PROSPECT, OWNER], MID)
        self.assertIn("550", str(err))
        self.assertNotIn("DATA", self.smtp.verbs())
        self.assertEqual(self.smtp.delivered, [])
        self.assert_one_try()

    def test_a_refused_data_command_is_a_mail_error(self):
        self.smtp.data_cmd_code = 554
        err = self.refused(reply_bytes(), [PROSPECT], MID)
        self.assertIn("554", str(err))
        self.assert_one_try()

    def test_a_refusal_after_the_message_is_a_mail_error_not_unknown(self):
        self.smtp.final_code = 554
        err = self.refused(reply_bytes(), [PROSPECT], MID)
        self.assertIn("554", str(err))
        self.assert_one_try()

    def test_a_connection_lost_on_the_data_command_is_a_mail_error_not_unknown(self):
        self.smtp.on_data_cmd = "drop"
        self.refused(reply_bytes(), [PROSPECT], MID)
        self.assert_one_try()

    def test_a_connection_lost_after_the_message_went_is_send_unknown(self):
        self.smtp.after_body = "drop"
        with self.assertRaises(SendUnknown) as cm:
            self.mail.send(reply_bytes(), [PROSPECT], MID)
        secret_free(self, cm.exception)
        self.assert_one_try()

    def test_a_timeout_after_the_message_went_is_send_unknown(self):
        self.smtp.after_body = "hang"
        mail = self.make_mail(timeout_s=0.3)
        started = time.monotonic()
        with self.assertRaises(SendUnknown):
            mail.send(reply_bytes(), [PROSPECT], MID)
        self.assertLess(time.monotonic() - started, 1.4)
        self.assert_one_try()

    def test_an_unreadable_answer_after_the_message_is_send_unknown(self):
        self.smtp.after_body = "garbage"
        with self.assertRaises(SendUnknown):
            self.mail.send(reply_bytes(), [PROSPECT], MID)
        self.assert_one_try()

    def test_a_connect_failure_is_a_mail_error_not_unknown(self):
        def refuse(host, port, timeout):
            raise ConnectionRefusedError("nothing listening")
        mail = live.LiveMail(env(), DORIS, smtp_factory=refuse)
        with self.assertRaises(MailError) as cm:
            mail.send(reply_bytes(), [PROSPECT], MID)
        self.assertNotIsInstance(cm.exception, SendUnknown)


class SmtpLoginTest(SmtpFixture):

    def test_check_smtp_login_logs_in_and_sends_nothing(self):
        self.mail.check_smtp_login()
        self.assertEqual(self.smtp.sessions[0]["auth"], DORIS)
        self.assertFalse({"MAIL", "RCPT", "DATA"} & set(self.smtp.verbs()))

    def test_check_smtp_login_refused_is_a_mail_error(self):
        self.smtp.password = "dummy-other-pass"
        with self.assertRaises(MailError) as cm:
            self.mail.check_smtp_login()
        secret_free(self, cm.exception)


# ===========================================================================================
# A fake CalDAV server for the real http.client
# ===========================================================================================
COLLECTION = "/dav/calendars/user/doris.htm@fastmail.com/owner-shared-cal/"
BID = "doris-0123456789abcdef01234567"
D_NS, C_NS = "{DAV:}", "{urn:ietf:params:xml:ns:caldav}"
S = u(2026, 12, 7, 17, 0)                  # Monday Dec 7, 9:00 AM Pacific
E = u(2026, 12, 7, 18, 0)
HOUR, DAY = dt.timedelta(hours=1), dt.timedelta(days=1)
SLOT = Slot(u(2027, 3, 1, 18, 0), u(2027, 3, 1, 18, 30))


def stamp(moment):
    return moment.strftime("%Y%m%dT%H%M%SZ")


def timed(uid, start, end, *more):
    return ["UID:" + uid, "DTSTAMP:20261101T000000Z", "DTSTART:" + stamp(start), "DTEND:" + stamp(end)] + list(more)


def vcal(*events):
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//test//EN"]
    for ev in events:
        lines += ["BEGIN:VEVENT"] + list(ev) + ["END:VEVENT"]
    return ("\r\n".join(lines + ["END:VCALENDAR"]) + "\r\n").encode("utf-8")


def item(name, data=None, prop_status="200 OK", status=None, href=None):
    return {"href": href or COLLECTION + name, "data": data, "prop_status": prop_status, "status": status}


def multistatus(items):
    out = ['<?xml version="1.0" encoding="utf-8"?>',
           '<d:multistatus xmlns:d="DAV:" xmlns:cal="urn:ietf:params:xml:ns:caldav">']
    for it in items:
        out.append("<d:response><d:href>%s</d:href>" % escape(it["href"]))
        if it.get("status"):
            out.append("<d:status>HTTP/1.1 %s</d:status>" % it["status"])
        else:
            data = it.get("data")
            text = escape(data.decode("utf-8") if isinstance(data, bytes) else data) if data is not None else ""
            out.append("<d:propstat><d:prop><cal:calendar-data>%s</cal:calendar-data></d:prop>"
                       "<d:status>HTTP/1.1 %s</d:status></d:propstat>" % (text, it.get("prop_status") or "200 OK"))
        out.append("</d:response>")
    out.append("</d:multistatus>")
    return "\n".join(out).encode("utf-8")


def her_event(bid=BID, slot=SLOT, method=None):
    return ics.build_event(bid, slot, LINK, "CONFIRMED", 0, u(2026, 11, 27, 12), method)


def basic(user, password):
    return "Basic " + base64.b64encode(("%s:%s" % (user, password)).encode()).decode()


class FakeCalDAV(http.server.ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self):
        http.server.ThreadingHTTPServer.__init__(self, ("127.0.0.1", 0), CalDAVHandler)
        self.auth = basic(DORIS, PASSWORD)
        self.resources = {}           # resource name -> (etag, bytes)
        self.requests = []            # (method, path, headers with lowercase names, body)
        self.report = None            # the REPORT answer's items (None: every resource, unfiltered)
        self.raw_answer = None        # (status, content type, bytes) answering REPORT instead
        self.status = {}              # method -> a status to answer with instead
        self.fault = {}               # method -> "drop" (close, no answer) | "hang" | "drip" | "drip_body"
        self.hang_s = 1.5
        self.drip_s = 0.02            # "drip": the whole answer a byte at a time; "drip_body": the body only
        self.delay = {}               # method -> seconds to wait before answering as usual
        self.on_request = None        # called (method, resource name) as each request arrives
        self.etags = 0
        self.thread = threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    @property
    def port(self):
        return self.server_address[1]

    def stop(self):
        self.shutdown()
        self.server_close()

    def put(self, name, data):
        self.etags += 1
        self.resources[name] = ('"etag-%d"' % self.etags, data)

    def methods(self):
        return [r[0] for r in self.requests]


class CalDAVHandler(http.server.BaseHTTPRequestHandler):
    disable_nagle_algorithm = True

    def log_message(self, *args):
        pass

    def answer(self, code, body=b"", headers=()):
        self.send_response(code)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def any_method(self):
        srv = self.server
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        srv.requests.append((self.command, self.path, {k.lower(): v for k, v in self.headers.items()}, body))
        if srv.on_request is not None:
            srv.on_request(self.command, self.name())
        fault = srv.fault.get(self.command)
        if fault in ("drop", "hang"):
            if fault == "hang":
                time.sleep(srv.hang_s)
            self.close_connection = True
            return
        time.sleep(srv.delay.get(self.command, 0))
        if fault in ("drip", "drip_body"):
            return self.drip(body, whole=fault == "drip")
        self.respond(body)

    def respond(self, body):
        srv = self.server
        if self.headers.get("Authorization") != srv.auth:
            return self.answer(401, b"who", [("WWW-Authenticate", 'Basic realm="fake"')])
        if self.command in srv.status:
            return self.answer(srv.status[self.command], b"no")
        getattr(self, "on_" + self.command)(body)

    def drip(self, body, whole):
        """The usual answer, sent one byte every drip_s: all of it, or the body after the headers."""
        real, self.wfile = self.wfile, io.BytesIO()
        try:
            self.respond(body)
            data = self.wfile.getvalue()
        finally:
            self.wfile = real
        start = 0 if whole else data.index(b"\r\n\r\n") + 4
        self.close_connection = True
        try:
            real.write(data[:start])
            for i in range(start, len(data)):
                real.write(data[i:i + 1])
                time.sleep(self.server.drip_s)
        except OSError:
            pass                      # the client gave up: that is the point

    do_REPORT = do_PUT = do_GET = do_DELETE = any_method

    def name(self):
        return urllib.parse.unquote(self.path[len(COLLECTION):]) if self.path.startswith(COLLECTION) else None

    def on_GET(self, body):
        found = self.server.resources.get(self.name())
        if found is None:
            return self.answer(404)
        etag = [("ETag", found[0])] if found[0] is not None else []      # None: the server sends no ETag
        self.answer(200, found[1], etag + [("Content-Type", "text/calendar; charset=utf-8")])

    def on_PUT(self, body):
        srv, name = self.server, self.name()
        if self.headers.get("If-None-Match") == "*" and name in srv.resources:
            return self.answer(412)
        srv.put(name, body)
        self.answer(201, b"", [("ETag", srv.resources[name][0])])

    def on_DELETE(self, body):
        srv, name = self.server, self.name()
        found = srv.resources.get(name)
        if found is None:
            return self.answer(404)
        if self.headers.get("If-Match") not in (None, found[0]):
            return self.answer(412)
        del srv.resources[name]
        self.answer(204)

    def on_REPORT(self, body):
        srv = self.server
        if srv.raw_answer is not None:
            code, kind, data = srv.raw_answer
            return self.answer(code, data, [("Content-Type", kind)])
        items = srv.report
        if items is None:
            uid = report_uid(body)
            items = [item(name, data) for name, (_, data) in sorted(srv.resources.items())
                     if uid is None or any(uid in v.uid for v in ics.read_events(data))]
        self.answer(207, multistatus(items), [("Content-Type", "application/xml; charset=utf-8")])


def report_uid(body):
    """The text a calendar-query matches UIDs against (RFC 4791 9.7.5: a substring), or None."""
    for prop in ET.fromstring(body).iter(C_NS + "prop-filter"):
        if prop.get("name") == "UID":
            return prop.find(C_NS + "text-match").text
    return None


def closed_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class CalFixture(unittest.TestCase):

    def setUp(self):
        self.dav = FakeCalDAV()
        self.addCleanup(self.dav.stop)
        self.dialed = []
        self.cal = self.make_calendar()

    def make_calendar(self, timeout_s=2.0, port=None):
        def factory(host, port_, timeout):
            self.dialed.append((host, port_, timeout))
            return http.client.HTTPConnection("127.0.0.1", port or self.dav.port, timeout=timeout)
        return live.LiveCalendar(env(), timeout_s=timeout_s, http_factory=factory)

    def tearDown(self):
        """Only the calendar's own resources, only these four methods, never a DELETE of the collection."""
        for method, path, headers, body in self.dav.requests:
            self.assertIn(method, ("REPORT", "PUT", "GET", "DELETE"))
            self.assertTrue(path.startswith(COLLECTION), path)
            if method != "REPORT":
                self.assertTrue(path.endswith(".ics") and "/" not in path[len(COLLECTION):], path)

    def fails(self, cls, call, *args):
        with self.assertRaises(cls) as cm:
            call(*args)
        secret_free(self, cm.exception)
        return cm.exception


# ===========================================================================================
# Calendar: query
# ===========================================================================================
class QueryTest(CalFixture):

    def test_a_calendar_query_report_asks_for_times_status_and_transparency_with_server_expansion(self):
        self.cal.query(S, E)
        (method, path, headers, body), = self.dav.requests
        self.assertEqual((method, path, headers["depth"]), ("REPORT", COLLECTION, "1"))
        self.assertEqual(headers["authorization"], self.dav.auth)
        self.assertEqual(self.dialed, [("caldav.fastmail.com", 443, 2.0)])
        root = ET.fromstring(body)
        self.assertEqual(root.tag, C_NS + "calendar-query")
        data = root.find(D_NS + "prop/" + C_NS + "calendar-data")
        self.assertEqual([c.get("name") for c in data.iter(C_NS + "comp")], ["VCALENDAR", "VEVENT"])
        calendar, = data.findall(C_NS + "comp")
        event, = calendar.findall(C_NS + "comp")
        self.assertEqual([p.get("name") for p in calendar.findall(C_NS + "prop")], ["VERSION"])
        self.assertEqual({p.get("name") for p in event.findall(C_NS + "prop")},
                         {"UID", "DTSTART", "DTEND", "DURATION", "STATUS", "TRANSP", "RECURRENCE-ID", "RRULE", "RDATE"})
        self.assertEqual(len(list(data.iter(C_NS + "prop"))), 10)       # no title, no description, no people
        window = (stamp(S - 2 * DAY), stamp(E + 2 * DAY))     # padded: any zone's all-day event is caught
        expand = data.find(C_NS + "expand")
        self.assertEqual((expand.get("start"), expand.get("end")), window)
        self.assertEqual([c.get("name") for c in root.iter(C_NS + "comp-filter")], ["VCALENDAR", "VEVENT"])
        span = root.find("/".join([C_NS + "filter", C_NS + "comp-filter", C_NS + "comp-filter", C_NS + "time-range"]))
        self.assertEqual((span.get("start"), span.get("end")), window)

    def test_events_come_back_as_times_status_and_transparency_never_a_title(self):
        self.dav.report = [
            item("a.ics", vcal(timed("ev-a", S, E, "STATUS:TENTATIVE", "TRANSP:TRANSPARENT",
                                     "SUMMARY:Board meeting", "DESCRIPTION:private notes"))),
            item("b.ics", vcal(timed("ev-b", S + HOUR / 2, E + HOUR / 2))),
        ]
        events = self.cal.query(S, E)
        self.assertEqual(events, [Event("ev-a", "TENTATIVE", "TRANSPARENT", "timed", S, E),
                                  Event("ev-b", "", "", "timed", S + HOUR / 2, E + HOUR / 2)])
        self.assertNotIn("Board meeting", repr(events))
        self.assertEqual(self.cal.last_query_whole_span, 0)

    def test_repeats_the_server_expanded_come_back_one_event_each(self):
        weekly = vcal(timed("weekly", S - DAY, E - DAY, "RECURRENCE-ID:" + stamp(S - DAY)),
                      timed("weekly", S, E, "RECURRENCE-ID:" + stamp(S)))
        self.dav.report = [item("w.ics", weekly)]
        self.assertEqual(self.cal.query(S, E), [Event("weekly", "", "", "timed", S, E)])

    def test_a_repeat_the_server_did_not_expand_is_busy_over_the_whole_span(self):
        self.dav.report = [item("w.ics", vcal(timed("weekly", S - 7 * DAY, E - 7 * DAY, "RRULE:FREQ=WEEKLY",
                                                    "STATUS:CONFIRMED"))),
                           item("r.ics", vcal(timed("odd", S - 3 * DAY, E - 3 * DAY, "RDATE:" + stamp(S))))]
        self.assertEqual(self.cal.query(S, E), [Event("odd", "", "", "timed", S, E),
                                                Event("weekly", "CONFIRMED", "", "timed", S, E)])
        self.assertEqual(self.cal.last_query_whole_span, 2)

    def test_an_event_it_cannot_read_is_busy_over_the_whole_span(self):
        self.dav.report = [item("bad.ics", b"BEGIN:VCALENDAR\r\nnot ics\r\n"),
                           item("gone.ics", None, prop_status="404 Not Found"),
                           item("x", status="403 Forbidden",
                                href="https://caldav.fastmail.com" + COLLECTION + "forbidden.ics")]
        events = self.cal.query(S, E)
        self.assertEqual([(e.uid, e.status, e.kind, e.start_utc, e.end_utc) for e in events],
                         [("bad", "", "timed", S, E), ("forbidden", "", "timed", S, E), ("gone", "", "timed", S, E)])
        self.assertEqual(self.cal.last_query_whole_span, 3)

    def test_an_event_at_the_end_of_time_is_busy_over_the_whole_span_not_a_crash(self):
        self.dav.report = [item("end.ics", vcal(["UID:end-of-time", "DTSTART;VALUE=DATE:99991231"]))]
        self.assertEqual(self.cal.query(S, E), [Event("end", "", "", "timed", S, E)])

    def test_only_events_touching_the_span_are_kept_and_cancelled_ones_keep_their_status(self):
        self.dav.report = [item("1.ics", vcal(timed("before", S - HOUR, S))),          # back to back
                           item("2.ics", vcal(timed("after", E, E + HOUR))),
                           item("3.ics", vcal(timed("cancelled", S, E, "STATUS:CANCELLED"))),
                           item("4.ics", vcal(timed("zero", S, S)))]                   # a moment at the start
        self.assertEqual([(e.uid, e.status) for e in self.cal.query(S, E)], [("cancelled", "CANCELLED"), ("zero", "")])

    def test_an_all_day_event_is_its_pacific_day(self):
        self.dav.report = [item("d.ics", vcal(["UID:allday", "DTSTART;VALUE=DATE:20261207",
                                               "DTEND;VALUE=DATE:20261208"]))]
        (ev,) = self.cal.query(S, E)
        self.assertEqual((ev.uid, ev.kind, ev.start_date, ev.end_date),
                         ("allday", "all_day", dt.date(2026, 12, 7), dt.date(2026, 12, 8)))

    def test_an_empty_answer_is_an_empty_calendar(self):
        self.dav.report = []
        self.assertEqual(self.cal.query(S, E), [])

    def test_the_collection_itself_is_skipped_but_a_truncated_answer_is_a_failure(self):
        self.dav.report = [item("", None), item("a.ics", vcal(timed("a", S, E)))]
        self.assertEqual([e.uid for e in self.cal.query(S, E)], ["a"])
        self.dav.report = [item("a.ics", vcal(timed("a", S, E))), item("", status="507 Insufficient Storage")]
        err = self.fails(CalendarError, self.cal.query, S, E)
        self.assertIn("507", str(err))

    def test_a_status_other_than_multistatus_is_a_calendar_error_naming_it(self):
        self.dav.status["REPORT"] = 403
        err = self.fails(CalendarError, self.cal.query, S, E)
        self.assertNotIsInstance(err, CalendarTimeout)
        self.assertIn("403", str(err))

    def test_a_refused_login_is_a_calendar_error_that_carries_no_secret(self):
        self.dav.auth = basic(DORIS, "dummy-other-pass")
        err = self.fails(CalendarError, self.cal.query, S, E)
        self.assertIn("401", str(err))

    def test_an_answer_that_is_not_a_multistatus_is_a_failure_never_an_empty_calendar(self):
        for answer in ((207, "application/xml", b"<html><body>oops</body></html>"),
                       (207, "application/xml", b"not xml at all"),
                       (200, "application/xml", b'<d:multistatus xmlns:d="DAV:"/>'),
                       (207, "application/xml", b'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY a "aa">]>'
                                                b'<d:multistatus xmlns:d="DAV:"/>')):
            self.dav.raw_answer = answer
            self.fails(CalendarError, self.cal.query, S, E)

    def test_a_title_that_mentions_a_doctype_inside_cdata_is_read_like_any_other(self):
        """The guard is the parser's, not a byte scan: text that only looks like a DOCTYPE is data."""
        data = vcal(timed("odd-title", S, E, "SUMMARY:<!DOCTYPE x [<!ENTITY y \"z\">]> notes")).decode()
        body = ('<?xml version="1.0" encoding="utf-8"?>\n<d:multistatus xmlns:d="DAV:" '
                'xmlns:cal="urn:ietf:params:xml:ns:caldav"><d:response><d:href>%sa.ics</d:href><d:propstat>'
                '<d:prop><cal:calendar-data><![CDATA[%s]]></cal:calendar-data></d:prop>'
                '<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>' % (COLLECTION, data))
        self.dav.raw_answer = (207, "application/xml; charset=utf-8", body.encode("utf-8"))
        self.assertEqual(self.cal.query(S, E), [Event("odd-title", "", "", "timed", S, E)])

    def test_a_doctype_in_any_encoding_is_refused_never_expanded(self):
        """UTF-16 hides "<!DOCTYPE" from a byte scan; the parser still sees it, and nothing is expanded."""
        event = vcal(timed("from-an-entity", S, E)).decode().replace("\r\n", "&#10;")
        for encoding in ("utf-16", "utf-16-le", "utf-8"):
            body = ('<?xml version="1.0" encoding="%s"?>\n<!DOCTYPE d [<!ENTITY ev "%s">]>\n'
                    '<d:multistatus xmlns:d="DAV:" xmlns:cal="urn:ietf:params:xml:ns:caldav"><d:response>'
                    '<d:href>%sa.ics</d:href><d:propstat><d:prop><cal:calendar-data>&ev;</cal:calendar-data>'
                    '</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>'
                    % (encoding.split("-le")[0], event, COLLECTION))
            data = body.encode(encoding)
            if encoding == "utf-16-le":
                data = b"\xff\xfe" + data
            self.dav.raw_answer = (207, "application/xml", data)
            err = self.fails(CalendarError, self.cal.query, S, E)
            self.assertIn("DOCTYPE", str(err), encoding)

    def test_an_encoding_it_cannot_decode_is_a_calendar_error_not_a_raw_exception(self):
        for name in ("x-no-such-encoding", "utf-7"):
            body = ('<?xml version="1.0" encoding="%s"?><d:multistatus xmlns:d="DAV:"/>' % name).encode("ascii")
            self.dav.raw_answer = (207, "application/xml", body)
            with self.assertRaises(Exception) as cm:
                self.cal.query(S, E)
            self.assertIsInstance(cm.exception, CalendarError, name)
            self.assertIn("cannot be read", str(cm.exception))

    def test_an_href_that_is_not_a_url_is_a_calendar_error_not_a_raw_exception(self):
        self.dav.report = [item("a.ics", vcal(timed("a", S, E)), href="//[")]
        with self.assertRaises(Exception) as cm:
            self.cal.query(S, E)
        self.assertIsInstance(cm.exception, CalendarError)

    def test_a_server_that_hangs_is_a_calendar_timeout(self):
        self.dav.fault["REPORT"] = "hang"
        cal = self.make_calendar(timeout_s=0.3)
        started = time.monotonic()
        self.fails(CalendarTimeout, cal.query, S, E)
        self.assertLess(time.monotonic() - started, 1.4)

    def test_a_connection_lost_after_the_request_is_a_calendar_timeout(self):
        self.dav.fault["REPORT"] = "drop"
        self.fails(CalendarTimeout, self.cal.query, S, E)

    def test_a_connect_failure_is_a_calendar_error_not_a_timeout(self):
        err = self.fails(CalendarError, self.make_calendar(port=closed_port()).query, S, E)
        self.assertNotIsInstance(err, CalendarTimeout)
        self.assertIn("connect", str(err))

    def test_a_naive_or_backward_span_is_refused_before_any_call(self):
        with self.assertRaises(ValueError):
            self.cal.query(S.replace(tzinfo=None), E)
        with self.assertRaises(ValueError):
            self.cal.query(E, S)
        self.assertEqual(self.dav.requests, [])


# ===========================================================================================
# Calendar: create, get, delete
# ===========================================================================================
class CreateTest(CalFixture):

    def test_puts_her_event_create_only_under_its_booking_id(self):
        data = her_event()
        self.cal.create_only(BID, data)
        (method, path, headers, body), = self.dav.requests
        self.assertEqual((method, path, body), ("PUT", COLLECTION + BID + ".ics", data))
        self.assertEqual(headers["if-none-match"], "*")
        self.assertTrue(headers["content-type"].startswith("text/calendar"))
        self.assertEqual(headers["authorization"], self.dav.auth)
        self.assertEqual(self.dav.resources[BID + ".ics"][1], data)

    def test_an_id_already_on_the_calendar_is_already_exists(self):
        self.cal.create_only(BID, her_event())
        before = dict(self.dav.resources)
        self.fails(AlreadyExists, self.cal.create_only, BID, her_event())
        self.assertEqual(self.dav.resources, before)

    def test_anything_not_hers_or_not_her_fixed_form_is_refused_before_any_call(self):
        self.fails(NotHers, self.cal.create_only, "someone-else", her_event())
        self.fails(CalendarError, self.cal.create_only, BID, her_event(bid="doris-ffffffffffffffffffffffff"))
        self.fails(CalendarError, self.cal.create_only, BID,
                   her_event().replace(b"STATUS:CONFIRMED", b"STATUS:CONFIRMED\r\nATTENDEE:mailto:x@example.com"))
        self.fails(CalendarError, self.cal.create_only, BID, her_event(method="PUBLISH"))
        self.assertTrue(issubclass(NotHers, CalendarError))
        self.assertEqual(self.dav.requests, [])

    def test_a_connection_lost_after_the_write_went_is_a_calendar_timeout(self):
        self.dav.fault["PUT"] = "drop"
        self.fails(CalendarTimeout, self.cal.create_only, BID, her_event())

    def test_no_answer_in_time_is_a_calendar_timeout(self):
        self.dav.fault["PUT"] = "hang"
        self.fails(CalendarTimeout, self.make_calendar(timeout_s=0.3).create_only, BID, her_event())

    def test_a_refused_write_is_a_calendar_error_naming_the_status(self):
        for code in (403, 409, 500):
            self.dav.status["PUT"] = code
            err = self.fails(CalendarError, self.cal.create_only, BID, her_event())
            self.assertNotIsInstance(err, (CalendarTimeout, AlreadyExists))
            self.assertIn(str(code), str(err))

    def test_a_connect_failure_before_the_write_is_a_calendar_error_not_a_timeout(self):
        err = self.fails(CalendarError, self.make_calendar(port=closed_port()).create_only, BID, her_event())
        self.assertNotIsInstance(err, CalendarTimeout)

    def test_only_201_counts_as_created(self):
        """Under If-None-Match: * a 200 or 204 is an overwrite, or a server that ignores the condition."""
        for code in (200, 204):
            self.dav.status["PUT"] = code
            err = self.fails(CalendarError, self.cal.create_only, BID, her_event())
            self.assertNotIsInstance(err, (CalendarTimeout, AlreadyExists))
            self.assertIn(str(code), str(err))

    def test_a_gateway_error_or_a_timeout_answer_is_unknown_never_not_written(self):
        """The write may have landed behind the gateway: the caller looks it up by booking ID."""
        for code in (408, 502, 504):
            self.dav.status["PUT"] = code
            err = self.fails(CalendarTimeout, self.cal.create_only, BID, her_event())
            self.assertIn(str(code), str(err))


class GetTest(CalFixture):

    def test_reads_her_event_back_by_booking_id(self):
        self.cal.create_only(BID, her_event())
        self.assertEqual(self.cal.get(BID), ics.read_event(her_event()))
        self.assertEqual(self.dav.requests[-1][:2], ("GET", COLLECTION + BID + ".ics"))

    def test_none_only_when_the_server_says_404(self):
        self.assertIsNone(self.cal.get(BID))
        self.assertEqual(self.dav.methods(), ["GET", "REPORT"])
        for code in (403, 410, 500):
            self.dav.status["GET"] = code
            err = self.fails(CalendarError, self.cal.get, BID)
            self.assertIn(str(code), str(err))
        del self.dav.status["GET"]
        self.dav.fault["GET"] = "drop"
        self.fails(CalendarTimeout, self.cal.get, BID)

    def test_an_id_not_hers_is_refused_before_any_call(self):
        for bad in ("other", "doris-../x", "", None, BID + "\n"):
            self.fails(NotHers, self.cal.get, bad)
        self.assertEqual(self.dav.requests, [])

    def test_a_404_is_confirmed_by_asking_the_collection_for_her_uid(self):
        """None only when the calendar itself answers and holds no event with her UID."""
        self.dav.put("someone-else.ics", vcal(timed("someone-else", S, E)))
        self.assertIsNone(self.cal.get(BID))
        (_, path, headers, body) = self.dav.requests[-1]
        self.assertEqual((self.dav.methods(), path, headers["depth"]), (["GET", "REPORT"], COLLECTION, "1"))
        self.assertEqual(report_uid(body), BID)
        self.assertIsNone(ET.fromstring(body).find(".//" + C_NS + "time-range"))

    def test_a_calendar_that_answers_404_itself_is_a_failure_never_none(self):
        """A revoked share or a wrong collection answers 404 to everything: her event may still be there."""
        for report in (404, 403, 500):
            self.dav.status.update(GET=404, REPORT=report)
            err = self.fails(CalendarError, self.cal.get, BID)
            self.assertIn(str(report), str(err))

    def test_her_uid_filed_under_another_name_is_a_failure_never_none(self):
        self.dav.put("renamed-by-a-client.ics", vcal(timed(BID, S, E)))
        self.fails(CalendarError, self.cal.get, BID)

    def test_an_event_filed_under_her_id_with_another_uid_is_a_failure(self):
        self.dav.put(BID + ".ics", vcal(timed("not-hers", S, E)))
        self.fails(CalendarError, self.cal.get, BID)

    def test_anything_but_exactly_her_one_event_is_a_failure_never_her_event(self):
        """Not the first VEVENT that carries her UID: exactly one VEVENT, hers, that does not repeat."""
        for data in (vcal(timed(BID, S, E), timed("someone", S, E)),
                     vcal(timed(BID, S, E), timed(BID, S + DAY, E + DAY, "RECURRENCE-ID:" + stamp(S + DAY))),
                     vcal(timed(BID, S, E), timed(BID, S, E)),
                     vcal(timed(BID, S, E, "RRULE:FREQ=DAILY")),
                     vcal(timed(BID, S, E, "RDATE:" + stamp(S + DAY)))):
            self.dav.put(BID + ".ics", data)
            self.fails(CalendarError, self.cal.get, BID)

    def test_her_event_comes_back_with_the_times_and_status_the_server_holds(self):
        """What the server holds, never what she wrote: the loop compares the two."""
        self.dav.put(BID + ".ics", vcal(timed(BID, S + DAY, E + DAY, "STATUS:CANCELLED")))
        self.assertEqual(self.cal.get(BID), Event(BID, "CANCELLED", "", "timed", S + DAY, E + DAY))

    def test_an_event_it_cannot_read_is_a_failure_never_none(self):
        for data in (b"garbage", vcal(["UID:" + BID, "DTSTART;VALUE=DATE:99991231"])):
            self.dav.put(BID + ".ics", data)
            self.fails(CalendarError, self.cal.get, BID)


class DeleteTest(CalFixture):

    def test_reads_then_removes_exactly_her_resource_if_unchanged(self):
        self.dav.put("other.ics", vcal(timed("other", S, E)))
        self.cal.create_only(BID, her_event())
        etag = self.dav.resources[BID + ".ics"][0]
        self.cal.delete(BID)
        self.assertEqual(self.dav.methods(), ["PUT", "GET", "DELETE"])
        method, path, headers, body = self.dav.requests[-1]
        self.assertEqual((path, headers["if-match"]), (COLLECTION + BID + ".ics", etag))
        self.assertEqual(set(self.dav.resources), {"other.ics"})
        self.assertIsNone(self.cal.get(BID))

    def test_an_id_already_gone_is_a_no_op(self):
        self.cal.delete(BID)
        self.assertEqual(self.dav.methods(), ["GET", "REPORT"])
        self.assertEqual(report_uid(self.dav.requests[-1][3]), BID)

    def test_a_calendar_that_answers_404_itself_is_a_failure_not_a_no_op(self):
        self.dav.status.update(GET=404, REPORT=404)
        err = self.fails(CalendarError, self.cal.delete, BID)
        self.assertIn("404", str(err))
        self.assertNotIn("DELETE", self.dav.methods())

    def test_her_uid_filed_under_another_name_is_a_failure_not_a_no_op(self):
        self.dav.put("renamed-by-a-client.ics", vcal(timed(BID, S, E)))
        self.fails(CalendarError, self.cal.delete, BID)
        self.assertNotIn("DELETE", self.dav.methods())

    def test_an_id_not_hers_is_refused_before_any_call(self):
        for bad in ("other", "doris-../x", "", None, "doris-a/b", BID + "\n"):
            self.fails(NotHers, self.cal.delete, bad)
        self.assertEqual(self.dav.requests, [])

    def test_a_resource_under_her_id_that_is_not_only_her_event_is_not_deleted(self):
        for data in (vcal(timed("someone", S, E)), vcal(timed(BID, S, E), timed("someone", S, E))):
            self.dav.put(BID + ".ics", data)
            self.fails(NotHers, self.cal.delete, BID)
        self.assertNotIn("DELETE", self.dav.methods())
        self.assertIn(BID + ".ics", self.dav.resources)

    def test_a_resource_it_cannot_read_is_not_deleted(self):
        for data in (b"garbage", vcal(["UID:" + BID, "DTSTART;VALUE=DATE:99991231"])):
            self.dav.put(BID + ".ics", data)
            self.fails(CalendarError, self.cal.delete, BID)
        self.assertNotIn("DELETE", self.dav.methods())

    def test_a_refused_changed_or_lost_delete_fails_loudly(self):
        for code in (403, 412):
            self.cal.create_only(BID, her_event()) if BID + ".ics" not in self.dav.resources else None
            self.dav.status["DELETE"] = code
            err = self.fails(CalendarError, self.cal.delete, BID)
            self.assertNotIsInstance(err, CalendarTimeout)
            self.assertIn(str(code), str(err))
        del self.dav.status["DELETE"]
        self.dav.fault["DELETE"] = "drop"
        self.fails(CalendarTimeout, self.cal.delete, BID)
        self.assertIn(BID + ".ics", self.dav.resources)

    def test_without_a_strong_etag_nothing_is_deleted(self):
        """If-Match takes a strong ETag only; without one the DELETE would be unconditional."""
        for etag in ('W/"weak-1"', None, "", "unquoted"):
            self.dav.resources[BID + ".ics"] = (etag, her_event())
            err = self.fails(CalendarError, self.cal.delete, BID)
            self.assertIn("ETag", str(err), repr(etag))
        self.assertNotIn("DELETE", self.dav.methods())
        self.assertIn(BID + ".ics", self.dav.resources)

    def test_a_resource_gone_between_the_read_and_the_delete_is_a_no_op(self):
        self.cal.create_only(BID, her_event())

        def someone_deletes_it_first(method, name):
            if method == "DELETE":
                self.dav.resources.pop(name, None)
        self.dav.on_request = someone_deletes_it_first
        self.cal.delete(BID)
        self.assertEqual(self.dav.methods(), ["PUT", "GET", "DELETE", "REPORT"])

    def test_a_delete_answered_404_while_her_event_is_still_there_is_a_failure(self):
        self.cal.create_only(BID, her_event())
        self.dav.status["DELETE"] = 404
        self.fails(CalendarError, self.cal.delete, BID)
        self.assertIn(BID + ".ics", self.dav.resources)


class DeadlineTest(CalFixture):
    """One deadline per call. A socket timeout restarts with every byte, so a server answering a
    byte at a time would otherwise hold a call (and her loop) for as long as it liked."""

    def test_an_answer_dripped_a_byte_at_a_time_times_out_at_the_calls_deadline(self):
        cal = self.make_calendar(timeout_s=0.5)
        for mode in ("drip", "drip_body"):
            self.dav.fault["REPORT"] = mode
            started = time.monotonic()
            self.fails(CalendarTimeout, cal.query, S, E)
            self.assertLess(time.monotonic() - started, 1.5, mode)

    def test_every_request_of_one_call_shares_its_deadline(self):
        """get's GET (404) and the query that confirms it: 0.3 s each is over a 0.5 s call."""
        cal = self.make_calendar(timeout_s=0.5)
        self.dav.delay.update(GET=0.3, REPORT=0.3)
        started = time.monotonic()
        self.fails(CalendarTimeout, cal.get, BID)
        self.assertLess(time.monotonic() - started, 1.0)

    def test_a_call_within_its_deadline_is_unchanged(self):
        self.dav.put(BID + ".ics", her_event())
        self.dav.fault["GET"] = "drip_body"
        self.dav.drip_s = 0.001
        self.assertEqual(self.make_calendar(timeout_s=5.0).get(BID), ics.read_event(her_event()))


class CalDAVTransportTest(unittest.TestCase):

    def test_caldav_is_https_with_a_verified_certificate_and_the_timeout(self):
        seen = {}

        def fake(host, port=None, timeout=None, context=None, **kw):
            seen.update(host=host, port=port, timeout=timeout, context=context)
            raise ConnectionRefusedError("stop here")
        with mock.patch.object(http.client, "HTTPSConnection", fake):
            with self.assertRaises(CalendarError) as cm:
                live.LiveCalendar(env(), timeout_s=7).query(S, E)
        self.assertNotIsInstance(cm.exception, CalendarTimeout)
        self.assertEqual((seen["host"], seen["port"], seen["timeout"]), ("caldav.fastmail.com", 443, 7.0))
        self.assertEqual(seen["context"].verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(seen["context"].check_hostname)



if __name__ == "__main__":
    unittest.main()
