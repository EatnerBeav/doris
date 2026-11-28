"""adapters/jmap.py: her sends over Fastmail's JMAP API (RFC 8620, RFC 8621), the one way she sends.

No network: the real http.client talks to the fake JMAP server of fake_jmap.py on 127.0.0.1
(plain HTTP in place of TLS, through the sender's factory hook). The fake keeps a mailbox of its
own and records what it was sent, so a test can prove what Doris never does (a second submission,
a Bcc, another host, a token or server text in an error).
"""
import base64
import http.client
import json
import ssl
import time
import traceback
import unittest
from unittest import mock

from doris import caps, compose, ics, netguard
from doris.adapters import jmap
from doris.shapes import MailError, SendUnknown
from tests.unit import fake_jmap
from tests.unit.fake_jmap import ACCOUNT, CORE, MAIL, SERVER_TEXT, SUBMISSION, TOKEN, FakeJMAP
from tests.unit.test_live import BID, LINK, SLOT, eml, her_event, u

DORIS = "doris.htm@fastmail.com"
OWNER = "whizz@fastmail.com"
PROSPECT = "prospect@example.com"
MID = "<5d1e0c2b9a@fastmail.com>"
WOLF = "wolf.whizz@fastmail.com"
WOLF_TOKEN = "dummy-wolf-jmap-token-for-smoke-tests-7373"   # a stand-in: never a real key


def env(**overrides):
    values = {"DORIS_MAIL_ADDRESS": DORIS, "DORIS_JMAP_TOKEN": TOKEN}
    values.update(overrides)
    return {k: v for k, v in values.items() if v is not None}


def reply_bytes(mid=MID, sender=DORIS, extra=b""):
    return (b"From: " + sender.encode() + b"\r\nTo: " + PROSPECT.encode() + b"\r\nSubject: Re: hello\r\n"
            b"Date: Fri, 27 Nov 2026 18:00:00 +0000\r\nMessage-ID: " + mid.encode() + b"\r\n" + extra
            + b"Auto-Submitted: auto-replied\r\nMIME-Version: 1.0\r\nContent-Type: text/plain; charset=\"utf-8\"\r\n"
            b"\r\nHello.\r\n.a line that starts with a dot\r\n")


def mime(*parts, kind="multipart/mixed"):
    """A message from her: the (part headers, part body) pairs inside one multipart of this kind,
    or (kind None) one part alone."""
    head = (b"From: " + DORIS.encode() + b"\r\nTo: " + PROSPECT.encode() + b"\r\nSubject: Re: hello\r\n"
            b"Date: Fri, 27 Nov 2026 18:00:00 +0000\r\nMessage-ID: " + MID.encode() + b"\r\nMIME-Version: 1.0\r\n")
    if kind is None:
        (headers, body), = parts
        return head + headers + b"\r\n" + body
    out = head + b'Content-Type: ' + kind.encode() + b'; boundary="b0"\r\n\r\n'
    for headers, body in parts:
        out += b"--b0\r\n" + headers + b"\r\n" + body + b"\r\n"
    return out + b"--b0--\r\n"


TEXT_PART = (b'Content-Type: text/plain; charset="utf-8"\r\n', b"Hello.\r\n")


def ics_part(data, content_type=b'text/calendar; charset="utf-8"; method=PUBLISH'):
    return (b"Content-Type: " + content_type + b'\r\nContent-Disposition: attachment; filename="call.ics"\r\n'
            b"Content-Transfer-Encoding: base64\r\n", base64.encodebytes(data).replace(b"\n", b"\r\n"))


def secret_free(test, exc):
    """Neither the token nor anything the server wrote appears in anything an error prints."""
    printed = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    for text in (str(exc), repr(exc), printed):
        for secret in (TOKEN, SERVER_TEXT):
            test.assertNotIn(secret, text)


# ===========================================================================================
# Settings and hosts
# ===========================================================================================
class SettingsTest(unittest.TestCase):

    def test_the_names_and_the_one_host(self):
        self.assertEqual(jmap.ENV_TOKEN, "DORIS_JMAP_TOKEN")
        self.assertEqual(jmap.SESSION_URL, "https://api.fastmail.com/jmap/session")
        self.assertEqual(jmap.HOST, "api.fastmail.com")
        self.assertEqual(jmap.hosts(), ["api.fastmail.com"])

    def test_it_needs_her_address_and_her_token(self):
        for missing in ("DORIS_MAIL_ADDRESS", "DORIS_JMAP_TOKEN"):
            with self.assertRaises(jmap.ConfigError) as cm:
                jmap.JmapSender(env(**{missing: None}), DORIS)
            self.assertIn(missing, str(cm.exception))
        with self.assertRaises(jmap.ConfigError):
            jmap.JmapSender(env(DORIS_JMAP_TOKEN="   "), DORIS)

    def test_it_refuses_an_address_that_is_not_hers(self):
        """She sends as herself with her one key: never as the owner, never as an alias."""
        with self.assertRaises(jmap.ConfigError):
            jmap.JmapSender(env(), OWNER)
        with self.assertRaises(jmap.ConfigError):
            jmap.JmapSender(env(DORIS_MAIL_ADDRESS="Doris <%s>" % DORIS), DORIS)
        self.assertEqual(jmap.JmapSender(env(), DORIS.upper()).from_addr, DORIS.upper())

    def test_a_token_that_cannot_ride_in_a_header_is_refused_without_showing_it(self):
        for bad in ("dummy token with spaces in it, long enough", "dummy-token-long-enough-to-pass\r\nX-Evil: 1",
                    "dummy-töken-long-enough-to-pass-the-length", "dummy\ttoken-long-enough-to-pass-the-length",
                    " " + TOKEN, TOKEN + " ", TOKEN + "\n", TOKEN + "\x00", TOKEN + "\x7f", TOKEN + "\u00a0",
                    TOKEN + "\u200b"):
            with self.assertRaises(jmap.ConfigError, msg=repr(bad)) as cm:
                jmap.JmapSender(env(DORIS_JMAP_TOKEN=bad), DORIS)
            self.assertIn("DORIS_JMAP_TOKEN", str(cm.exception))
            self.assertNotIn(bad.strip(), str(cm.exception))
            self.assertEqual(len(str(cm.exception).splitlines()), 1)

    def test_an_implausibly_short_token_is_refused_and_no_vendor_prefix_is_asked_for(self):
        for bad in ("x", "dummy", "dummy-token-1234", "d" * (jmap.MIN_TOKEN_CHARS - 1)):
            with self.assertRaises(jmap.ConfigError, msg=bad) as cm:
                jmap.JmapSender(env(DORIS_JMAP_TOKEN=bad), DORIS)
            self.assertEqual(str(cm.exception), "DORIS_JMAP_TOKEN is too short to be an API token")
        self.assertGreaterEqual(jmap.MIN_TOKEN_CHARS, 20)
        for good in ("d" * jmap.MIN_TOKEN_CHARS, "dummy-no-vendor-prefix-0123456789abcdef", "fmu1-" + "d" * 60):
            jmap.JmapSender(env(DORIS_JMAP_TOKEN=good), DORIS)
        with self.assertRaises(jmap.ConfigError):
            jmap.JmapSender(env(DORIS_JMAP_TOKEN="d" * 5000), DORIS)

    def test_another_account_is_named_by_its_own_settings_and_reads_no_other(self):
        """The smoke's wolf.whizz sender: WOLF_EMAIL and WOLF_JMAP_TOKEN, never her names."""
        wolf_env = {"WOLF_EMAIL": WOLF, "WOLF_JMAP_TOKEN": WOLF_TOKEN}
        sender = jmap.JmapSender(wolf_env, WOLF, address_name="WOLF_EMAIL", token_name="WOLF_JMAP_TOKEN")
        self.assertEqual(sender.from_addr, WOLF)
        for missing in ("WOLF_EMAIL", "WOLF_JMAP_TOKEN"):
            given = dict(wolf_env, DORIS_MAIL_ADDRESS=DORIS, DORIS_JMAP_TOKEN=TOKEN)
            del given[missing]
            with self.assertRaises(jmap.ConfigError) as cm:
                jmap.JmapSender(given, WOLF, address_name="WOLF_EMAIL", token_name="WOLF_JMAP_TOKEN")
            self.assertEqual(str(cm.exception), "%s is not set" % missing)
        with self.assertRaises(jmap.ConfigError) as cm:
            jmap.JmapSender(wolf_env, DORIS, address_name="WOLF_EMAIL", token_name="WOLF_JMAP_TOKEN")
        self.assertIn("WOLF_EMAIL", str(cm.exception))
        for text in (repr(sender), str(sender), repr(vars(sender))):
            self.assertNotIn(WOLF_TOKEN, text)

    def test_the_timeout_must_be_a_positive_number(self):
        for bad in (0, -1, True, "5"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                jmap.JmapSender(env(), DORIS, timeout_s=bad)
        self.assertEqual(jmap.JmapSender(env(), DORIS).timeout_s, caps.MAIL_TIMEOUT_S)

    def test_the_session_url_must_be_on_the_allowed_host(self):
        for bad in ("https://evil.example/jmap/session", "https://api.fastmail.com.evil.example/jmap/session",
                    "https://u:p@api.fastmail.com/jmap/session", "https://api.fastmail.com/jmap/session?x=1",
                    "ftp://api.fastmail.com/jmap/session", "api.fastmail.com/jmap/session"):
            with self.assertRaises(jmap.ConfigError, msg=bad):
                jmap.JmapSender(env(), DORIS, session_url=bad)

    def test_a_plain_http_session_url_needs_a_transport_of_the_callers(self):
        """The default transport is TLS; a plain-http URL with it is a mistake, refused up front."""
        with self.assertRaises(jmap.ConfigError):
            jmap.JmapSender(env(), DORIS, session_url="http://api.fastmail.com/jmap/session")

    def test_the_token_never_shows_in_the_sender(self):
        sender = jmap.JmapSender(env(), DORIS)
        for text in (repr(sender), str(sender), repr(vars(sender))):
            self.assertNotIn(TOKEN, text)

    def test_config_error_is_a_value_error(self):
        self.assertTrue(issubclass(jmap.ConfigError, ValueError))


class DefaultTransportTest(unittest.TestCase):
    """Without a factory every request is HTTPS to api.fastmail.com:443, verified, with the timeout."""

    def test_https_with_a_verified_certificate_and_the_timeout(self):
        seen = {}

        def fake(host, port=None, timeout=None, context=None, **kw):
            seen.update(host=host, port=port, timeout=timeout, context=context)
            raise ConnectionRefusedError("stop here")
        with mock.patch.object(http.client, "HTTPSConnection", fake):
            with self.assertRaises(MailError) as cm:
                jmap.JmapSender(env(), DORIS, timeout_s=7).check_login()
        self.assertNotIsInstance(cm.exception, SendUnknown)
        self.assertEqual((seen["host"], seen["port"], seen["timeout"]), ("api.fastmail.com", 443, 7.0))
        self.assertEqual(seen["context"].verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(seen["context"].check_hostname)
        self.assertEqual(seen["context"].minimum_version, ssl.TLSVersion.TLSv1_2)
        secret_free(self, cm.exception)

    def test_tls_is_1_2_or_newer_even_where_the_default_context_allows_older(self):
        """As live.py's connections (review 15): Python 3.9's default context sets no floor of its own."""
        real = ssl.create_default_context

        def as_on_3_9(*args, **kw):
            ctx = real(*args, **kw)
            ctx.minimum_version = ssl.TLSVersion.MINIMUM_SUPPORTED
            return ctx
        seen = {}

        def fake(host, port=None, timeout=None, context=None, **kw):
            seen["context"] = context
            raise ConnectionRefusedError("stop here")
        with mock.patch.object(ssl, "create_default_context", as_on_3_9), \
                mock.patch.object(http.client, "HTTPSConnection", fake):
            with self.assertRaises(MailError):
                jmap.JmapSender(env(), DORIS).check_login()
        self.assertEqual(seen["context"].minimum_version, ssl.TLSVersion.TLSv1_2)
        self.assertEqual(seen["context"].verify_mode, ssl.CERT_REQUIRED)


# ===========================================================================================
# The fixture: a sender wired to the fake
# ===========================================================================================
class JmapFixture(unittest.TestCase):

    def setUp(self):
        self.jmap = FakeJMAP()
        self.addCleanup(self.jmap.stop)
        self.dialed = []
        self.sender = self.make_sender()

    def make_sender(self, timeout_s=2.0, factory=None):
        return jmap.JmapSender(env(), DORIS, timeout_s=timeout_s, http_factory=factory or self.factory,
                               session_url=self.jmap.base + "/jmap/session", allowed_host="127.0.0.1")

    def factory(self, host, port, timeout):
        self.dialed.append((host, port, timeout))
        return http.client.HTTPConnection(host, port, timeout=timeout)

    def tearDown(self):
        """Every request went to the fake, on her account's paths, with her token; only these."""
        for host, port, _ in self.dialed:
            self.assertEqual((host, port), ("127.0.0.1", self.jmap.port))
        for kind, method, path, headers, body in self.jmap.requests:
            self.assertIn(kind, ("session", "setup", "upload", "send", "destroy", "find"))
            self.assertEqual(headers.get("authorization"), "Bearer " + TOKEN)
            self.assertNotIn(TOKEN.encode(), path.encode() + body)    # the header only, never a URL or body

    def assert_one_try(self):
        """Never a retry inside the sender: at most one submission request, at most one submission."""
        self.assertLessEqual(self.jmap.kinds().count("send"), 1)
        self.assertLessEqual(len(self.jmap.submissions), 1)

    def refused(self, *args, **kw):
        with self.assertRaises(MailError) as cm:
            self.sender.send(*args, **kw)
        self.assertNotIsInstance(cm.exception, SendUnknown)
        secret_free(self, cm.exception)
        return cm.exception

    def unknown(self, *args, **kw):
        with self.assertRaises(SendUnknown) as cm:
            self.sender.send(*args, **kw)
        secret_free(self, cm.exception)
        return cm.exception


# ===========================================================================================
# Refusals before any connection
# ===========================================================================================
class RefusalTest(JmapFixture):

    def tearDown(self):
        self.assertEqual(self.dialed, [])
        self.assertEqual(self.jmap.requests, [])
        JmapFixture.tearDown(self)

    def test_a_bcc_header_is_refused_before_connecting(self):
        for header in (b"Bcc: hidden@example.com\r\n", b"BCC: hidden@example.com\r\n", b"bcc :hidden@example.com\r\n",
                       b"Resent-Bcc: hidden@example.com\r\n", b"Bcc:\r\n"):
            err = self.refused(reply_bytes(extra=header), [PROSPECT, OWNER], MID)
            self.assertIn("Bcc", str(err))

    def test_a_message_not_from_her_alone_is_refused_before_connecting(self):
        self.refused(reply_bytes(sender=OWNER), [PROSPECT], MID)
        self.refused(reply_bytes(sender="%s, %s" % (DORIS, OWNER)), [PROSPECT], MID)
        self.refused(reply_bytes(extra=b"From: " + DORIS.encode() + b"\r\n"), [PROSPECT], MID)

    def test_a_message_without_exactly_the_recorded_message_id_is_refused_before_connecting(self):
        self.refused(reply_bytes(), [PROSPECT], "<another@fastmail.com>")
        self.refused(reply_bytes(), [PROSPECT], "")
        self.refused(reply_bytes(), [PROSPECT], None)
        self.refused(reply_bytes(extra=b"Message-ID: " + MID.encode() + b"\r\n"), [PROSPECT], MID)

    def test_anything_but_message_bytes_is_refused_before_connecting(self):
        self.refused(b"", [PROSPECT], MID)
        self.refused("text, not bytes", [PROSPECT], MID)
        self.refused(None, [PROSPECT], MID)
        self.refused(b"not a header line\r\n\r\nbody\r\n", [PROSPECT], MID)

    def test_a_bad_envelope_is_refused_before_connecting(self):
        for bad in ([], PROSPECT, ["not an address"], [PROSPECT + "\r\nX: y"], [None], (),
                    ["<%s>" % PROSPECT], ["Name <%s>" % PROSPECT], [PROSPECT + "\n"], None):
            self.refused(reply_bytes(), bad, MID)

    def test_a_part_other_than_text_or_her_own_ics_is_refused_before_connecting(self):
        """The only-her-.ics rule (review 14) holds in the sender too, not only upstream."""
        attendee = her_event(method="PUBLISH").replace(b"STATUS:CONFIRMED",
                                                       b"STATUS:CONFIRMED\r\nATTENDEE:mailto:x@example.com")
        for raw in (
                mime(TEXT_PART, (b'Content-Type: application/pdf\r\nContent-Disposition: attachment; filename="a.pdf"'
                                 b"\r\nContent-Transfer-Encoding: base64\r\n", base64.encodebytes(b"%PDF-1.4"))),
                mime(TEXT_PART, (b'Content-Type: text/html; charset="utf-8"\r\n', b"<p>Hello.</p>"),
                     kind="multipart/alternative"),
                mime(TEXT_PART, ics_part(attendee)),
                mime(TEXT_PART, ics_part(b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n")),
                mime(TEXT_PART, (b"Content-Type: message/rfc822\r\n", eml(1))),
                mime(TEXT_PART, (b'Content-Type: text/plain\r\nContent-Disposition: attachment; filename="notes.txt"\r\n',
                                 b"a file, not her words")),
                mime(TEXT_PART, (b'Content-Type: multipart/mixed; boundary="b1"\r\n',
                                 b"--b1\r\nContent-Type: text/plain\r\n\r\nnested\r\n--b1--")),
                mime((b"Content-Type: text/html\r\n", b"<p>Hello.</p>\r\n"), kind=None),
                mime((b"Content-Type: application/octet-stream\r\n", b"\x00\x01"), kind=None)):
            err = self.refused(raw, [PROSPECT], MID)
            self.assertIn("refused before connecting", str(err))


# ===========================================================================================
# The session (RFC 8620 2) and her mailboxes and identity: check_login
# ===========================================================================================
class SessionTest(JmapFixture):

    def fails(self, call=None):
        with self.assertRaises(MailError) as cm:
            (call or self.sender.check_login)()
        self.assertNotIsInstance(cm.exception, SendUnknown)
        secret_free(self, cm.exception)
        return cm.exception

    def test_check_login_reads_the_session_and_her_mailboxes_and_sends_nothing(self):
        self.assertIsNone(self.sender.check_login())
        self.assertEqual(self.jmap.kinds(), ["session", "setup"])
        self.assertEqual((self.jmap.uploads, self.jmap.emails, self.jmap.submissions), ([], {}, []))
        self.assertEqual(self.dialed, [("127.0.0.1", self.jmap.port, 2.0)] * 2)

    def test_the_setup_asks_for_ids_and_roles_and_identities_in_one_request(self):
        self.sender.check_login()
        (calls,) = self.jmap.calls("setup")
        self.assertEqual([c[0] for c in calls], ["Mailbox/get", "Identity/get"])
        self.assertEqual(calls[0][1], {"accountId": ACCOUNT, "ids": None, "properties": ["id", "role"]})
        self.assertEqual(calls[1][1], {"accountId": ACCOUNT, "ids": None, "properties": ["id", "email"]})
        request = json.loads([r[4] for r in self.jmap.requests if r[0] == "setup"][0])
        self.assertEqual(sorted(request["using"]), sorted([CORE, MAIL, SUBMISSION]))
        headers = [r[3] for r in self.jmap.requests if r[0] == "setup"][0]
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual([r[2] for r in self.jmap.requests], ["/jmap/session", "/jmap/api/"])

    def test_a_refused_token_at_the_session_is_a_mail_error_naming_the_status(self):
        self.jmap.token = "dummy-another-token"
        err = self.fails()
        self.assertEqual(str(err), "jmap session: 401")
        self.assertEqual(err.status, 401)
        self.jmap.token, self.jmap.status["session"] = TOKEN, 403
        self.assertEqual(str(self.fails()), "jmap session: 403")
        self.assertEqual(self.jmap.kinds(), ["session", "session"])

    def test_a_session_for_another_account_is_refused_before_anything_more_is_sent(self):
        """The token answers for one account; it must be the one DORIS_MAIL_ADDRESS names."""
        for other in (OWNER, "doris.htm@fastmail.com.example", "", None, 7, ["doris.htm@fastmail.com"]):
            self.jmap.username = other
            err = self.fails()
            self.assertEqual(str(err), "jmap session: the session is not for the account DORIS_MAIL_ADDRESS names")
            self.assertIn("not for the account", str(self.fails(lambda: self.sender.send(reply_bytes(), [PROSPECT], MID))))
        self.assertEqual(set(self.jmap.kinds()), {"session"})
        self.assertEqual(self.jmap.uploads, [])

    def test_the_sessions_username_is_matched_whatever_its_case(self):
        self.jmap.username = "  Doris.HTM@FastMail.com "
        self.assertIsNone(self.sender.check_login())

    def test_a_session_answer_that_cannot_be_read_is_a_mail_error(self):
        for body in (b"not json", b"[1, 2]", b'{"apiUrl": 7}', b"{}", b'{"capabilities": []}', b"\xff\xfe",
                     b"[" * 100000):
            self.jmap.session_body = body
            self.fails()
        self.assertEqual(set(self.jmap.kinds()), {"session"})

    def test_the_session_must_offer_core_mail_and_submission(self):
        for missing in (CORE, MAIL, SUBMISSION):
            self.jmap.capabilities = [c for c in (CORE, MAIL, SUBMISSION) if c != missing]
            self.fails()
        self.assertEqual(set(self.jmap.kinds()), {"session"})

    def test_mail_and_submission_must_be_one_account(self):
        for primary in ({MAIL: ACCOUNT}, {SUBMISSION: ACCOUNT}, {MAIL: ACCOUNT, SUBMISSION: "u9z9"},
                        {MAIL: "bad id/..", SUBMISSION: "bad id/.."}, {MAIL: 7, SUBMISSION: 7}, []):
            self.jmap.primary = primary
            self.fails()
        self.assertEqual(set(self.jmap.kinds()), {"session"})

    def bad_urls(self, path):
        port = self.jmap.port
        return ["http://api.example.test:%d%s" % (port, path),        # another host
                "https://127.0.0.1:%d%s" % (port, path),              # another scheme
                "http://127.0.0.1:%d%s" % (port + 1, path),           # another port
                "http://127.0.0.1%s" % path,                          # the scheme's default port, not this one
                "http://user@127.0.0.1:%d%s" % (port, path),          # a user name in it
                "http://127.0.0.1:%d%s?x=1" % (port, path),           # a query
                "http://127.0.0.1:%d%s#f" % (port, path),             # a fragment
                path,                                                 # relative
                "http://127.0.0.1:%d/jmap/a b/" % port,                # a character a path cannot carry
                "//127.0.0.1:%d%s" % (port, path),
                7, ""]

    def test_an_api_url_anywhere_else_is_refused_before_anything_more_is_sent(self):
        for bad in self.bad_urls("/jmap/api/"):
            self.jmap.api_url = bad
            self.assertIn("apiUrl", str(self.fails()), bad)
            self.assertIn("apiUrl", str(self.fails(lambda: self.sender.send(reply_bytes(), [PROSPECT], MID))))
        self.assertEqual(set(self.jmap.kinds()), {"session"})
        self.assertEqual(self.jmap.uploads, [])

    def test_an_upload_url_anywhere_else_is_refused_before_anything_is_uploaded(self):
        for bad in self.bad_urls("/jmap/upload/{accountId}/") + [self.jmap.base + "/jmap/upload/"]:
            self.jmap.upload_url = bad
            self.assertIn("uploadUrl", str(self.fails()), bad)
            self.assertIn("uploadUrl", str(self.fails(lambda: self.sender.send(reply_bytes(), [PROSPECT], MID))))
        self.assertEqual(set(self.jmap.kinds()), {"session"})
        self.assertEqual(self.jmap.uploads, [])

    def test_an_answer_too_long_to_read_is_a_mail_error(self):
        with mock.patch.object(jmap, "MAX_ANSWER_BYTES", 64):
            self.assertIn("too long", str(self.fails()))

    def test_a_connect_failure_is_a_mail_error(self):
        def refuse(host, port, timeout):
            raise ConnectionRefusedError("nothing listening")
        self.sender = self.make_sender(factory=refuse)
        self.assertIn("connect", str(self.fails()))

    def test_a_session_that_does_not_answer_in_time_is_a_mail_error_in_time(self):
        self.jmap.fault["session"] = "hang"
        self.sender = self.make_sender(timeout_s=0.3)
        started = time.monotonic()
        self.fails()
        self.assertLess(time.monotonic() - started, 1.4)


# ===========================================================================================
# The host the session names (Fastmail's real session: apiUrl and uploadUrl on the account's own
# host below api.fastmail.com, such as phl.api.fastmail.com, which Fastmail can move)
# ===========================================================================================
# Origins no apiUrl or uploadUrl may name: not https, not port 443, or not api.fastmail.com or a
# whole-label name below it. The token never goes to any of them.
NOT_FASTMAIL = ("https://evil-api.fastmail.com.attacker", "https://xapi.fastmail.com", "https://api.fastmail.com.attacker",
                "https://phl.api.fastmail.com.attacker", "https://fastmail.com", "https://attacker.example",
                "https://.api.fastmail.com", "https://phl..api.fastmail.com", "https://phl.api.fastmail.com.",
                "https://-phl.api.fastmail.com", "https://phl-.api.fastmail.com", "https://ph_l.api.fastmail.com",
                "https://phl%2Eapi.fastmail.com", "https://127.0.0.1", "https://[::1]",
                "http://phl.api.fastmail.com", "http://api.fastmail.com", "https://phl.api.fastmail.com:8443",
                "https://phl.api.fastmail.com:80", "https://api.fastmail.com:444", "https://phl.api.fastmail.com:0",
                "https://user@phl.api.fastmail.com", "https://user:pw@phl.api.fastmail.com")


class SessionHostTest(unittest.TestCase):
    """The real session URL (on api.fastmail.com) and a transport that dials the fake whatever the
    host, recording the host and port asked for: the session names apiUrl and uploadUrl as
    Fastmail's does (fake_jmap.as_fastmail). Taken only over https, on port 443 (or none), on
    api.fastmail.com or a host below it at a label boundary; anything else is refused before the
    token goes anywhere but the session."""

    def setUp(self):
        self.jmap = FakeJMAP().as_fastmail()
        self.addCleanup(self.jmap.stop)
        self.dialed = []
        self.sender = jmap.JmapSender(env(), DORIS, timeout_s=2.0, http_factory=self.factory)

    def factory(self, host, port, timeout):
        self.dialed.append((host, port))
        return http.client.HTTPConnection("127.0.0.1", self.jmap.port, timeout=timeout)

    def test_the_session_on_api_fastmail_com_names_the_accounts_own_host_and_the_rest_goes_there(self):
        self.assertIsNone(self.sender.send(reply_bytes(), [PROSPECT], MID))
        self.assertEqual(self.jmap.kinds(), ["session", "setup", "upload", "send"])
        self.assertEqual(self.dialed, [("api.fastmail.com", 443)] + [("phl.api.fastmail.com", 443)] * 3)
        self.assertEqual(self.jmap.delivered, [(DORIS, [PROSPECT], reply_bytes())])
        self.assertEqual(self.sender.find_message(MID), ["sent"])
        self.assertEqual(self.dialed[4:], [("api.fastmail.com", 443), ("phl.api.fastmail.com", 443)])

    def test_everything_after_the_session_lands_on_the_host_it_names(self):
        """Two servers, as at Fastmail: the session's own on api.fastmail.com, and the account's own
        on phl.api.fastmail.com, which holds her mailboxes. Nothing after the session goes to the first."""
        regional = FakeJMAP()
        self.addCleanup(regional.stop)
        ports = {"api.fastmail.com": self.jmap.port, "phl.api.fastmail.com": regional.port}

        def by_host(host, port, timeout):
            self.dialed.append((host, port))
            return http.client.HTTPConnection("127.0.0.1", ports[host], timeout=timeout)
        sender = jmap.JmapSender(env(), DORIS, timeout_s=2.0, http_factory=by_host)
        self.assertIsNone(sender.send(reply_bytes(), [PROSPECT], MID))
        self.assertEqual(self.jmap.kinds(), ["session"])
        self.assertEqual(regional.kinds(), ["setup", "upload", "send"])
        self.assertEqual((self.jmap.delivered, regional.delivered), ([], [(DORIS, [PROSPECT], reply_bytes())]))
        self.assertEqual(sender.find_message(MID), ["sent"])
        self.assertEqual((self.jmap.kinds(), regional.kinds()[-1]), (["session", "session"], "find"))

    def test_api_url_on_api_fastmail_com_itself_is_taken_too(self):
        self.jmap.as_fastmail(fake_jmap.SESSION_HOST)
        self.assertIsNone(self.sender.send(reply_bytes(), [PROSPECT], MID))
        self.assertEqual(self.dialed, [("api.fastmail.com", 443)] * 4)

    def test_any_host_below_api_fastmail_com_on_port_443_or_none_is_taken(self):
        for origin, host in (("https://phl.api.fastmail.com:443", "phl.api.fastmail.com"),
                             ("https://nyc1.api.fastmail.com", "nyc1.api.fastmail.com"),
                             ("https://a.b-2.api.fastmail.com", "a.b-2.api.fastmail.com"),
                             ("https://PHL.Api.FastMail.com", "phl.api.fastmail.com")):
            del self.dialed[:]
            self.jmap.as_fastmail(origin)
            self.assertIsNone(self.sender.check_login(), origin)
            self.assertEqual(self.dialed, [("api.fastmail.com", 443), (host, 443)], origin)

    def test_an_api_url_not_on_https_443_below_api_fastmail_com_is_refused_and_never_dialled(self):
        for origin in NOT_FASTMAIL:
            self.jmap.as_fastmail(origin)
            self.jmap.upload_url = fake_jmap.FASTMAIL + "/jmap/upload/{accountId}/"
            with self.assertRaises(MailError, msg=origin) as cm:
                self.sender.send(reply_bytes(), [PROSPECT], MID)
            self.assertNotIsInstance(cm.exception, SendUnknown)
            self.assertIn("apiUrl", str(cm.exception), origin)
            secret_free(self, cm.exception)
        self.assertEqual(set(self.dialed), {("api.fastmail.com", 443)})    # the session alone: the token went nowhere else
        self.assertEqual(set(self.jmap.kinds()), {"session"})
        self.assertEqual(self.jmap.uploads, [])

    def test_an_upload_url_not_on_https_443_below_api_fastmail_com_is_refused_and_never_dialled(self):
        for origin in NOT_FASTMAIL:
            self.jmap.upload_url = origin + "/jmap/upload/{accountId}/"
            with self.assertRaises(MailError, msg=origin) as cm:
                self.sender.send(reply_bytes(), [PROSPECT], MID)
            self.assertIn("uploadUrl", str(cm.exception), origin)
            secret_free(self, cm.exception)
        self.assertEqual(set(self.dialed), {("api.fastmail.com", 443)})
        self.assertEqual(set(self.jmap.kinds()), {"session"})
        self.assertEqual(self.jmap.uploads, [])

    def test_the_session_url_itself_must_be_on_port_443_with_the_default_transport(self):
        with self.assertRaises(jmap.ConfigError):
            jmap.JmapSender(env(), DORIS, session_url="https://api.fastmail.com:8443/jmap/session")


class SessionHostGuardTest(unittest.TestCase):
    """Under a run's network guard, which allows api.fastmail.com (as live.hosts names it) and not
    the host the session names: that host is allowed only once the sender checked the session, and
    every connection meets the guard by the host's own name and port (fake_jmap.dialling: only the
    name lookup below the guard is the test's)."""

    def setUp(self):
        self.jmap = FakeJMAP().as_fastmail()
        self.addCleanup(self.jmap.stop)
        patches = fake_jmap.dialling(self.jmap)
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.dns = patches[0].new
        self.guard = netguard.install(["api.fastmail.com"])
        self.addCleanup(self.guard.uninstall)
        self.sender = jmap.JmapSender(env(), DORIS, timeout_s=2.0)        # the default transport (patched)

    def dialled(self):
        return [(a["host"], a["port"], a["allowed"]) for a in self.guard.attempts if a["call"] == "create_connection"]

    def test_the_named_host_is_allowed_only_once_the_session_passed_then_reached_by_its_name(self):
        self.assertFalse(self.guard.allows("phl.api.fastmail.com"))
        self.assertIsNone(self.sender.send(reply_bytes(), [PROSPECT], MID))
        self.assertTrue(self.guard.allows("phl.api.fastmail.com"))
        self.assertEqual(self.dialled(), [("api.fastmail.com", 443, True)] + [("phl.api.fastmail.com", 443, True)] * 3)
        self.assertEqual(self.guard.refused_count(), 0)
        self.assertEqual(len(self.jmap.delivered), 1)

    def test_a_host_the_check_refuses_is_never_allowed_or_dialled(self):
        for origin in ("https://xapi.fastmail.com", "https://evil-api.fastmail.com.attacker",
                       "https://phl.api.fastmail.com:8443", "http://phl.api.fastmail.com"):
            self.jmap.as_fastmail(origin)
            with self.assertRaises(MailError, msg=origin):
                self.sender.check_login()
        self.assertEqual(self.guard.admitted, set())
        self.assertEqual(set(self.dialled()), {("api.fastmail.com", 443, True)})
        self.assertEqual(set(self.jmap.kinds()), {"session"})

    def test_the_requests_land_on_the_named_host_by_its_own_name_through_the_guard(self):
        """The session's server and the account's own, each behind its own name (Dns, below the guard)."""
        regional = FakeJMAP()
        self.addCleanup(regional.stop)
        self.dns.ports["phl.api.fastmail.com"] = regional.port
        self.assertIsNone(self.sender.send(reply_bytes(), [PROSPECT], MID))
        self.assertEqual(self.jmap.kinds(), ["session"])
        self.assertEqual(regional.kinds(), ["setup", "upload", "send"])
        self.assertEqual(self.dialled(), [("api.fastmail.com", 443, True)] + [("phl.api.fastmail.com", 443, True)] * 3)

    def test_a_guard_never_told_of_the_named_host_refuses_it(self):
        """The check alone is not enough to connect: a guard that was not told of the host refuses it."""
        with mock.patch.object(netguard, "admit", lambda host, under: False):
            with self.assertRaises(MailError) as cm:
                self.sender.check_login()
        self.assertIn("NetworkRefused", str(cm.exception))
        self.assertEqual(self.dialled()[-1], ("phl.api.fastmail.com", 443, False))
        self.assertEqual(self.jmap.kinds(), ["session"])


class SetupTest(JmapFixture):
    """Her Drafts and Sent mailboxes by role, and the identity whose email is hers."""

    def fails(self):
        with self.assertRaises(MailError) as cm:
            self.sender.check_login()
        self.assertNotIsInstance(cm.exception, SendUnknown)
        secret_free(self, cm.exception)
        self.assertEqual(self.jmap.uploads, [])
        return cm.exception

    def test_a_missing_drafts_or_sent_mailbox_is_a_mail_error(self):
        every = list(self.jmap.mailboxes)
        for role in ("drafts", "sent"):
            self.jmap.mailboxes = [m for m in every if m["role"] != role]
            self.assertIn(role.title(), str(self.fails()))

    def test_two_mailboxes_with_one_role_is_a_mail_error(self):
        self.jmap.mailboxes.append({"id": "mb-sent-2", "role": "sent", "name": "Sent 2"})
        self.fails()

    def test_a_mailbox_id_that_is_not_a_jmap_id_is_a_mail_error(self):
        self.jmap.mailboxes[1]["id"] = "mb/drafts"
        self.fails()

    def test_no_identity_for_her_address_is_a_mail_error(self):
        self.jmap.identities = [{"id": "I-owner", "email": OWNER, "name": "Owner"},
                                {"id": "I-wild", "email": "*@fastmail.com", "name": "Any"}]
        self.assertIn("identity", str(self.fails()))

    def test_her_identity_is_found_whatever_the_case(self):
        self.jmap.identities = [{"id": "I-owner", "email": OWNER}, {"id": "I-doris2", "email": "Doris.HTM@Fastmail.COM"}]
        self.sender.check_login()

    def test_an_error_answer_names_the_step_and_the_error_type_only(self):
        self.jmap.method_error["Mailbox/get"] = "serverFail"
        self.assertEqual(str(self.fails()), "jmap mailboxes: serverFail")
        self.jmap.method_error = {"Identity/get": "forbidden"}
        self.assertEqual(str(self.fails()), "jmap identity: forbidden")

    def test_an_error_type_that_is_not_a_plain_name_is_not_repeated(self):
        self.jmap.method_error["Mailbox/get"] = "evil type <b>"
        self.assertNotIn("evil", str(self.fails()))

    def test_a_missing_answer_is_a_mail_error(self):
        for name in ("Mailbox/get", "Identity/get"):
            self.jmap.omit = {name}
            self.fails()

    def test_an_http_error_or_an_unreadable_answer_is_a_mail_error(self):
        self.jmap.status["setup"] = 500
        self.assertIn("500", str(self.fails()))
        del self.jmap.status["setup"]
        self.jmap.fault["setup"] = "garbage"
        self.fails()
        self.jmap.fault["setup"] = "drop"
        self.fails()


# ===========================================================================================
# The send: upload, then Email/import and EmailSubmission/set in one request
# ===========================================================================================
ENVELOPE = {"mailFrom": {"email": DORIS, "parameters": None},
            "rcptTo": [{"email": PROSPECT, "parameters": None}, {"email": OWNER, "parameters": None}]}


class SendTest(JmapFixture):

    def test_her_message_goes_once_to_exactly_the_envelope_given_and_ends_in_sent(self):
        self.assertIsNone(self.sender.send(reply_bytes(), [PROSPECT, OWNER], MID))
        self.assertEqual(self.jmap.kinds(), ["session", "setup", "upload", "send"])
        self.assertEqual(self.jmap.uploads, [("message/rfc822", reply_bytes())])
        (sub,) = self.jmap.submissions
        self.assertEqual(sub["bytes"], reply_bytes())
        self.assertEqual(sub["envelope"], ENVELOPE)
        self.assertEqual(sub["identityId"], "I-doris")
        self.assertEqual(sub["fields"], ["emailId", "envelope", "identityId"])
        (copy,) = self.jmap.emails.values()
        self.assertEqual(copy["mailboxIds"], {"mb-sent": True})
        self.assertEqual(copy["keywords"], {"$seen": True})
        self.assertIs(self.sender.last_copy_in_sent, True)
        self.assertEqual(self.dialed, [("127.0.0.1", self.jmap.port, 2.0)] * 4)
        self.assert_one_try()

    def test_the_send_is_one_request_holding_the_import_and_the_submission_only(self):
        self.sender.send(reply_bytes(), [PROSPECT, OWNER], MID)
        (calls,) = self.jmap.calls("send")
        self.assertEqual([c[0] for c in calls], ["Email/import", "EmailSubmission/set"])
        (imp_cid, imp), = calls[0][1]["emails"].items()
        self.assertEqual(sorted(calls[0][1]), ["accountId", "emails"])
        self.assertEqual(imp, {"blobId": list(self.jmap.blobs)[0], "mailboxIds": {"mb-drafts": True},
                               "keywords": {"$draft": True, "$seen": True}})
        sub_args = calls[1][1]
        self.assertEqual(sorted(sub_args), ["accountId", "create", "onSuccessUpdateEmail"])
        (sub_cid, sub), = sub_args["create"].items()
        self.assertEqual(sub, {"identityId": "I-doris", "emailId": "#" + imp_cid, "envelope": ENVELOPE})
        self.assertEqual(sub_args["onSuccessUpdateEmail"],
                         {"#" + sub_cid: {"mailboxIds/mb-drafts": None, "mailboxIds/mb-sent": True,
                                          "keywords/$draft": None}})
        self.assertEqual([r[2] for r in self.jmap.requests if r[0] == "upload"], ["/jmap/upload/%s/" % ACCOUNT])

    def test_the_message_is_uploaded_byte_for_byte_with_crlf_line_ends(self):
        self.sender.send(reply_bytes().replace(b"\r\n", b"\n"), [PROSPECT], MID)
        self.assertEqual(self.jmap.uploads, [("message/rfc822", reply_bytes())])
        self.assertEqual(self.jmap.submissions[0]["envelope"]["rcptTo"], [{"email": PROSPECT, "parameters": None}])

    def test_her_reply_with_her_own_ics_goes_as_the_composer_builds_it(self):
        headers = [("From", DORIS), ("To", PROSPECT), ("Subject", "Re: hello"),
                   ("Date", "Fri, 27 Nov 2026 18:00:00 +0000"), ("Message-ID", MID), ("Auto-Submitted", "auto-replied")]
        for method, status in (("PUBLISH", "CONFIRMED"), ("CANCEL", "CANCELLED")):
            data = ics.build_event(BID, SLOT, LINK, status, 1, u(2026, 11, 27, 12), method)
            raw = compose._build(headers, compose.with_signature("Booked."), data, method, MID)
            self.sender.send(raw, [PROSPECT, OWNER], MID)
            self.assertEqual(self.jmap.submissions[-1]["bytes"], raw)
        self.sender.send(mime(TEXT_PART, ics_part(her_event(method="PUBLISH"))), [PROSPECT], MID)
        self.assertEqual(len(self.jmap.submissions), 3)

    def test_the_template_name_is_accepted_and_ignored(self):
        self.sender.send(reply_bytes(), [PROSPECT], MID, template="T1")
        self.sender.send(reply_bytes(), [PROSPECT], MID, "T2")
        self.assertEqual(len(self.jmap.submissions), 2)

    def test_where_her_copy_is_reads_none_until_a_send_goes(self):
        self.assertIsNone(self.sender.last_copy_in_sent)
        self.sender.send(reply_bytes(), [PROSPECT], MID)
        self.jmap.not_created["EmailSubmission/set"] = "forbiddenToSend"
        self.refused(reply_bytes(), [PROSPECT], MID)
        self.assertIsNone(self.sender.last_copy_in_sent)

    def test_no_identity_or_no_mailbox_uploads_nothing(self):
        self.jmap.identities = [{"id": "I-owner", "email": OWNER}]
        self.assertIn("identity", str(self.refused(reply_bytes(), [PROSPECT], MID)))
        self.jmap.identities = [{"id": "I-doris", "email": DORIS}]
        self.jmap.mailboxes = self.jmap.mailboxes[:2]
        self.assertIn("Sent", str(self.refused(reply_bytes(), [PROSPECT], MID)))
        self.assertEqual(self.jmap.uploads, [])
        self.assertNotIn("send", self.jmap.kinds())


class UploadTest(JmapFixture):

    def tearDown(self):
        self.assertNotIn("send", self.jmap.kinds())
        self.assertEqual(self.jmap.submissions, [])
        JmapFixture.tearDown(self)

    def test_a_refused_upload_is_a_mail_error_naming_the_status(self):
        self.jmap.status["upload"] = 413
        self.assertEqual(str(self.refused(reply_bytes(), [PROSPECT], MID)), "jmap upload: 413")

    def test_an_upload_the_server_counts_differently_is_a_mail_error(self):
        self.jmap.size_skew = -1
        self.assertIn("size", str(self.refused(reply_bytes(), [PROSPECT], MID)))

    def test_a_lost_or_unreadable_upload_answer_is_a_mail_error_not_unknown(self):
        for fault in ("drop", "garbage"):
            self.jmap.fault["upload"] = fault
            self.refused(reply_bytes(), [PROSPECT], MID)


class SubmitTest(JmapFixture):

    def destroyed(self):
        """The ids each follow-up request asked to destroy."""
        return [calls[0][1].get("destroy") for calls in self.jmap.calls("destroy")]

    def test_an_import_not_created_is_a_mail_error_naming_the_type(self):
        self.jmap.not_created["Email/import"] = "overQuota"
        self.assertEqual(str(self.refused(reply_bytes(), [PROSPECT], MID)), "jmap import: overQuota")
        self.assertEqual((self.jmap.submissions, self.jmap.emails), ([], {}))
        self.assertEqual(self.jmap.kinds(), ["session", "setup", "upload", "send"])

    def test_an_import_error_answer_is_a_mail_error_naming_the_type(self):
        self.jmap.method_error["Email/import"] = "serverFail"
        self.assertEqual(str(self.refused(reply_bytes(), [PROSPECT], MID)), "jmap import: serverFail")
        self.assertEqual(self.jmap.submissions, [])

    def test_a_submission_not_created_is_a_mail_error_and_the_draft_is_destroyed(self):
        self.jmap.emails["M-older"] = {"blobId": "B-old", "mailboxIds": {"mb-drafts": True},
                                       "keywords": {"$draft": True}, "bytes": b"an older draft of hers"}
        self.jmap.not_created["EmailSubmission/set"] = "forbiddenToSend"
        err = self.refused(reply_bytes(), [PROSPECT], MID)
        self.assertEqual(str(err), "jmap submission: forbiddenToSend")
        self.assertEqual(self.jmap.submissions, [])
        self.assertEqual(list(self.jmap.emails), ["M-older"])       # only the draft this send made is gone
        self.assertEqual(len(self.destroyed()), 1)
        self.assertNotIn("M-older", self.destroyed()[0])
        self.assertEqual(self.jmap.kinds(), ["session", "setup", "upload", "send", "destroy"])
        self.assert_one_try()

    def test_a_submission_error_answer_is_a_mail_error_and_the_draft_is_destroyed(self):
        self.jmap.method_error["EmailSubmission/set"] = "forbidden"
        self.assertEqual(str(self.refused(reply_bytes(), [PROSPECT], MID)), "jmap submission: forbidden")
        self.assertEqual((self.jmap.submissions, self.jmap.emails), ([], {}))

    def test_a_missing_submission_answer_is_a_mail_error_and_the_draft_is_destroyed(self):
        self.jmap.omit = {"EmailSubmission/set", "Email/set"}
        self.assertIn("jmap submission", str(self.refused(reply_bytes(), [PROSPECT], MID)))
        self.assertEqual(len(self.destroyed()), 1)

    def test_a_destroy_that_fails_never_hides_the_send_error(self):
        self.jmap.not_created["EmailSubmission/set"] = "tooManyRecipients"
        for how in ({"status": 500}, {"fault": "drop"}, {"fault": "garbage"}):
            self.jmap.status.pop("destroy", None)
            self.jmap.fault.pop("destroy", None)
            getattr(self.jmap, list(how)[0])["destroy"] = list(how.values())[0]
            err = self.refused(reply_bytes(), [PROSPECT], MID)
            self.assertEqual(str(err), "jmap submission: tooManyRecipients")

    def test_a_connection_lost_after_the_submission_request_is_send_unknown(self):
        self.jmap.fault["send"] = "drop"
        self.unknown(reply_bytes(), [PROSPECT], MID)
        self.assertEqual(len(self.jmap.submissions), 1)       # it went; the caller looks in Sent
        self.assertEqual(self.jmap.kinds(), ["session", "setup", "upload", "send"])
        self.assertIsNone(self.sender.last_copy_in_sent)
        self.assert_one_try()

    def test_no_answer_in_time_after_the_submission_request_is_send_unknown_in_time(self):
        self.jmap.fault["send"] = "hang"
        self.sender = self.make_sender(timeout_s=0.3)
        started = time.monotonic()
        self.unknown(reply_bytes(), [PROSPECT], MID)
        self.assertLess(time.monotonic() - started, 1.4)
        self.assert_one_try()

    def test_an_unreadable_answer_is_send_unknown(self):
        self.jmap.fault["send"] = "garbage"
        self.unknown(reply_bytes(), [PROSPECT], MID)
        self.assert_one_try()

    def test_a_submission_answer_that_says_neither_created_nor_not_is_send_unknown(self):
        def blank(answer):
            for name, args, _ in answer["methodResponses"]:
                if name == "EmailSubmission/set":
                    args["created"] = args["notCreated"] = None
            return answer
        self.jmap.rewrite = blank
        self.unknown(reply_bytes(), [PROSPECT], MID)
        self.assertNotIn("destroy", self.jmap.kinds())

    def test_a_server_error_after_the_request_is_send_unknown(self):
        self.jmap.fault["send"] = 502
        err = self.unknown(reply_bytes(), [PROSPECT], MID)
        self.assertEqual(err.status, 502)
        self.assert_one_try()

    def test_a_request_the_server_turned_away_is_a_mail_error(self):
        for status in (400, 401, 413, 429):
            self.jmap.status["send"] = status
            self.assertIn(str(status), str(self.refused(reply_bytes(), [PROSPECT], MID)))
        self.assertEqual(self.jmap.submissions, [])
        self.assertNotIn("destroy", self.jmap.kinds())

    def test_a_connect_failure_on_the_send_request_is_a_mail_error(self):
        def fourth_refused(host, port, timeout):
            if len(self.dialed) == 3:
                raise ConnectionRefusedError("nothing listening")
            return self.factory(host, port, timeout)
        self.sender = self.make_sender(factory=fourth_refused)
        self.assertIn("connect", str(self.refused(reply_bytes(), [PROSPECT], MID)))
        self.assertEqual(self.jmap.submissions, [])

    def test_a_failure_while_the_request_is_still_going_is_a_mail_error(self):
        """A request cut off before its last byte is one the server cannot have acted on."""
        class Cut(http.client.HTTPConnection):
            def request(self, method, url, body=None, headers=None, **kw):
                if body and b"EmailSubmission/set" in body:
                    raise BrokenPipeError(32, "Broken pipe")
                return http.client.HTTPConnection.request(self, method, url, body, headers or {}, **kw)

        def factory(host, port, timeout):
            self.dialed.append((host, port, timeout))
            return Cut(host, port, timeout=timeout)
        self.sender = self.make_sender(factory=factory)
        self.refused(reply_bytes(), [PROSPECT], MID)
        self.assertEqual(self.jmap.submissions, [])

    def test_the_copy_failing_to_move_after_a_created_submission_still_counts_as_sent(self):
        self.jmap.fail_update = "invalidPatch"
        self.assertIsNone(self.sender.send(reply_bytes(), [PROSPECT], MID))
        self.assertIs(self.sender.last_copy_in_sent, False)
        self.assertEqual(len(self.jmap.submissions), 1)
        (copy,) = self.jmap.emails.values()
        self.assertEqual(copy["mailboxIds"], {"mb-drafts": True})       # her copy sits in Drafts
        self.assertNotIn("destroy", self.jmap.kinds())
        self.assert_one_try()

    def test_a_missing_answer_for_the_move_still_counts_as_sent(self):
        self.jmap.omit = {"Email/set"}
        self.assertIsNone(self.sender.send(reply_bytes(), [PROSPECT], MID))
        self.assertIs(self.sender.last_copy_in_sent, False)
        self.assert_one_try()


# ===========================================================================================
# Reading a message back: the smoke's look for her reply in wolf.whizz's mailbox
# ===========================================================================================
def arrived(mid, sender=DORIS, to=WOLF):
    return (b"From: " + sender.encode() + b"\r\nTo: " + to.encode() + b"\r\nSubject: Re: a call\r\n"
            b"Message-ID: " + mid.encode() + b"\r\n\r\nHello.\r\n")


class FindTest(JmapFixture):

    def fails(self, *args):
        with self.assertRaises(MailError) as cm:
            self.sender.find_message(*args)
        self.assertNotIsInstance(cm.exception, SendUnknown)
        secret_free(self, cm.exception)
        return cm.exception

    def tearDown(self):
        self.assertEqual((self.jmap.uploads, self.jmap.submissions), ([], []))     # reading writes nothing
        JmapFixture.tearDown(self)

    def test_a_message_that_arrived_is_found_by_its_message_id_with_the_role_of_its_mailbox(self):
        self.jmap.deliver(arrived(MID))
        self.assertEqual(self.sender.find_message(MID), ["inbox"])
        self.assertEqual(self.sender.find_message(MID.strip("<>")), ["inbox"])

    def test_a_copy_filed_as_junk_is_found_and_says_so(self):
        self.jmap.mailboxes.append({"id": "mb-junk", "role": "junk", "name": "Spam"})
        self.jmap.deliver(arrived(MID), role="junk")
        self.assertEqual(self.sender.find_message(MID), ["junk"])
        self.jmap.deliver(arrived(MID))
        self.assertEqual(self.sender.find_message(MID), ["inbox", "junk"])

    def test_one_request_asks_for_the_header_the_ids_it_names_and_the_mailbox_roles(self):
        self.jmap.deliver(arrived(MID))
        self.sender.find_message(MID)
        self.assertEqual(self.jmap.kinds(), ["session", "find"])
        (calls,) = self.jmap.calls("find")
        self.assertEqual([c[0] for c in calls], ["Email/query", "Email/get", "Mailbox/get"])
        self.assertEqual(calls[0][1]["filter"], {"header": ["Message-ID", MID.strip("<>")]})
        self.assertEqual(calls[1][1]["#ids"], {"resultOf": calls[0][2], "name": "Email/query", "path": "/ids"})
        self.assertEqual(calls[1][1]["properties"], ["messageId", "mailboxIds"])

    def test_no_message_with_it_is_none(self):
        self.jmap.deliver(arrived("<another@fastmail.com>"))
        self.assertIsNone(self.sender.find_message(MID))

    def test_a_message_that_only_contains_the_id_is_not_a_match(self):
        """The header search is a contains; only a messageId equal to it counts."""
        self.jmap.deliver(arrived("<x" + MID.strip("<>") + ">"))
        self.assertIsNone(self.sender.find_message(MID))

    def test_a_message_in_a_mailbox_with_no_role_is_found_in_other(self):
        self.jmap.mailboxes.append({"id": "mb-folder", "role": None, "name": SERVER_TEXT})
        eid = self.jmap.deliver(arrived(MID))
        self.jmap.emails[eid]["mailboxIds"] = {"mb-folder": True}
        self.assertEqual(self.sender.find_message(MID), ["other"])

    def test_an_id_it_cannot_search_for_is_refused_before_any_request(self):
        for bad in ("", None, "no-at-sign", "<a b@c>", '<"q"@c>'):
            self.fails(bad)
        self.assertEqual(self.jmap.requests, [])

    def test_a_message_the_query_named_that_the_get_left_out_is_a_mail_error_never_none(self):
        self.jmap.deliver(arrived(MID))
        real = self.jmap.m_Email_get
        self.jmap.m_Email_get = lambda args, call_id, created: [[
            "Email/get", {"accountId": ACCOUNT, "list": [], "notFound": list(args["ids"])}, call_id]]
        self.assertIn("cannot tell", str(self.fails(MID)))
        self.jmap.m_Email_get = real

    def test_an_error_answer_or_one_it_cannot_read_is_a_mail_error_never_none(self):
        self.jmap.deliver(arrived(MID))
        for name in ("Email/query", "Email/get", "Mailbox/get"):
            self.jmap.method_error = {name: "serverFail"}
            self.assertEqual(str(self.fails(MID)), "jmap find: serverFail")
        self.jmap.method_error = {}
        for fault in ("drop", "garbage", 500):
            self.jmap.fault["find"] = fault
            self.fails(MID)
        self.jmap.fault = {}
        self.jmap.omit = {"Email/get"}
        self.fails(MID)

    def test_a_session_for_another_account_is_refused_before_the_query(self):
        self.jmap.username = OWNER
        self.fails(MID)
        self.assertEqual(self.jmap.kinds(), ["session"])

    def test_it_reads_with_another_accounts_token_when_built_with_its_names(self):
        self.jmap.token, self.jmap.username = WOLF_TOKEN, WOLF
        self.jmap.identities = [{"id": "I-wolf", "email": WOLF}]
        self.jmap.deliver(arrived(MID))
        sender = jmap.JmapSender({"WOLF_EMAIL": WOLF, "WOLF_JMAP_TOKEN": WOLF_TOKEN}, WOLF, timeout_s=2.0,
                                 http_factory=self.factory, session_url=self.jmap.base + "/jmap/session",
                                 allowed_host="127.0.0.1", address_name="WOLF_EMAIL", token_name="WOLF_JMAP_TOKEN")
        self.assertEqual(sender.find_message(MID), ["inbox"])
        self.assertEqual({r[3].get("authorization") for r in self.jmap.requests}, {"Bearer " + WOLF_TOKEN})
        del self.jmap.requests[:]        # the fixture's teardown checks her token on what is left: nothing


if __name__ == "__main__":
    unittest.main()
