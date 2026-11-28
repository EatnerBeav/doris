"""adapters/jmap.py: her sends over Fastmail's JMAP API (RFC 8620, RFC 8621) instead of SMTP.

No network: the real http.client talks to a small fake JMAP server on 127.0.0.1 (plain HTTP in
place of TLS, through the sender's factory hook). The fake keeps a mailbox of its own and records
what it was sent, so a test can prove what Doris never does (a second submission, a Bcc, another
host, a token or server text in an error).
"""
import http.client
import http.server
import json
import ssl
import threading
import time
import traceback
import unittest
from unittest import mock

from doris import caps
from doris.adapters import jmap
from doris.shapes import MailError, SendUnknown

DORIS = "doris.htm@fastmail.com"
OWNER = "whizz@fastmail.com"
PROSPECT = "prospect@example.com"
TOKEN = "dummy-jmap-token-4242"          # a stand-in: never a real key
MID = "<5d1e0c2b9a@fastmail.com>"
ACCOUNT = "u1a2b3c4"
SERVER_TEXT = "server-words-never-shown"  # every description the fake writes; never in an error of ours

CORE = "urn:ietf:params:jmap:core"
MAIL = "urn:ietf:params:jmap:mail"
SUBMISSION = "urn:ietf:params:jmap:submission"


def env(**overrides):
    values = {"DORIS_MAIL_ADDRESS": DORIS, "DORIS_JMAP_TOKEN": TOKEN}
    values.update(overrides)
    return {k: v for k, v in values.items() if v is not None}


def reply_bytes(mid=MID, sender=DORIS, extra=b""):
    return (b"From: " + sender.encode() + b"\r\nTo: " + PROSPECT.encode() + b"\r\nSubject: Re: hello\r\n"
            b"Date: Fri, 27 Nov 2026 18:00:00 +0000\r\nMessage-ID: " + mid.encode() + b"\r\n" + extra
            + b"Auto-Submitted: auto-replied\r\nMIME-Version: 1.0\r\nContent-Type: text/plain; charset=\"utf-8\"\r\n"
            b"\r\nHello.\r\n.a line that starts with a dot\r\n")


def secret_free(test, exc):
    """Neither the token nor anything the server wrote appears in anything an error prints."""
    printed = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    for text in (str(exc), repr(exc), printed):
        for secret in (TOKEN, SERVER_TEXT):
            test.assertNotIn(secret, text)


# ===========================================================================================
# A fake JMAP server: the session, uploads and the methods Doris uses
# ===========================================================================================
class FakeJMAP(http.server.ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self):
        http.server.ThreadingHTTPServer.__init__(self, ("127.0.0.1", 0), JMAPHandler)
        self.token = TOKEN
        self.requests = []          # (kind, method, path, headers with lowercase names, body)
        self.capabilities = [CORE, MAIL, SUBMISSION]
        self.primary = {MAIL: ACCOUNT, SUBMISSION: ACCOUNT}
        self.api_url = None         # None: this server's own
        self.upload_url = None
        self.session_body = None    # bytes answering the session instead
        self.mailboxes = [{"id": "mb-inbox", "role": "inbox", "name": "Inbox"},
                          {"id": "mb-drafts", "role": "drafts", "name": "Drafts"},
                          {"id": "mb-sent", "role": "sent", "name": "Sent"}]
        self.identities = [{"id": "I-doris", "email": DORIS, "name": "Doris"}]
        self.blobs = {}             # blobId -> bytes
        self.uploads = []           # (content type, bytes)
        self.emails = {}            # id -> {"blobId", "mailboxIds", "keywords", "bytes"}
        self.submissions = []       # what went: {"identityId", "emailId", "envelope", "bytes", "fields"}
        self.status = {}            # request kind -> an HTTP status to answer with instead
        self.method_error = {}      # method name -> an error type, answered as an "error" response
        self.not_created = {}       # "Email/import" | "EmailSubmission/set" -> an error type in notCreated
        self.omit = set()           # method names whose responses are left out of the answer
        self.fail_update = None     # an error type for the implicit Email/set after a submission
        self.fault = {}             # request kind -> "drop" (no answer) | "hang" | "garbage" | an HTTP
                                    # status, each after the request was processed in full
        self.size_skew = 0          # added to the size an upload answer reports
        self.rewrite = None         # a function given the answer to a send request, returning the one sent
        self.hang_s = 1.5
        self.counter = 0
        self.thread = threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    @property
    def port(self):
        return self.server_address[1]

    @property
    def base(self):
        return "http://127.0.0.1:%d" % self.port

    def stop(self):
        self.shutdown()
        self.server_close()

    def next_id(self, prefix):
        self.counter += 1
        return "%s%d" % (prefix, self.counter)

    def kinds(self):
        return [r[0] for r in self.requests]

    def calls(self, kind):
        """The method calls of each API request of this kind."""
        return [json.loads(r[4])["methodCalls"] for r in self.requests if r[0] == kind]

    # -- the session object (RFC 8620 2) -------------------------------------------------------
    def session(self):
        caps_ = {name: {} for name in self.capabilities}
        if CORE in caps_:
            caps_[CORE] = {"maxSizeUpload": 50000000, "maxConcurrentUpload": 4, "maxSizeRequest": 10000000,
                           "maxConcurrentRequests": 4, "maxCallsInRequest": 16, "maxObjectsInGet": 1000,
                           "maxObjectsInSet": 1000, "collationAlgorithms": []}
        return {"capabilities": caps_,
                "accounts": {ACCOUNT: {"name": DORIS, "isPersonal": True, "isReadOnly": False,
                                       "accountCapabilities": {MAIL: {}, SUBMISSION: {}}}},
                "primaryAccounts": self.primary,
                "username": DORIS,
                "apiUrl": self.base + "/jmap/api/" if self.api_url is None else self.api_url,
                "downloadUrl": "https://www.fastmailusercontent.com/jmap/download/{accountId}/{blobId}/{name}?type={type}",
                "uploadUrl": self.base + "/jmap/upload/{accountId}/" if self.upload_url is None else self.upload_url,
                "eventSourceUrl": self.base + "/jmap/event/",
                "state": "s1"}

    # -- methods (RFC 8621) --------------------------------------------------------------------
    NEEDS = {"Mailbox/get": MAIL, "Email/import": MAIL, "Email/set": MAIL,
             "Identity/get": SUBMISSION, "EmailSubmission/set": SUBMISSION}

    def api(self, request):
        using = request.get("using") or []
        created_ids, responses = {}, []
        for name, args, call_id in request["methodCalls"]:
            if CORE not in using or self.NEEDS.get(name) not in using:
                out = [["error", {"type": "unknownMethod"}, call_id]]
            elif args.get("accountId") != ACCOUNT:
                out = [["error", {"type": "accountNotFound"}, call_id]]
            elif name in self.method_error:
                out = [["error", {"type": self.method_error[name], "description": SERVER_TEXT}, call_id]]
            else:
                out = getattr(self, "m_" + name.replace("/", "_"))(args, call_id, created_ids)
            for response in out:
                if not (response[0] in self.omit or (response[0] == "error" and name in self.omit)):
                    responses.append(response)
        return {"methodResponses": responses, "sessionState": "s1"}

    @staticmethod
    def pick(items, args):
        props = args.get("properties")
        return [{k: v for k, v in item.items() if props is None or k in props} for item in items]

    def m_Mailbox_get(self, args, call_id, created_ids):
        return [["Mailbox/get", {"accountId": ACCOUNT, "state": "m1", "notFound": [],
                                 "list": self.pick(self.mailboxes, args)}, call_id]]

    def m_Identity_get(self, args, call_id, created_ids):
        return [["Identity/get", {"accountId": ACCOUNT, "state": "i1", "notFound": [],
                                  "list": self.pick(self.identities, args)}, call_id]]

    def m_Email_import(self, args, call_id, created_ids):
        created, not_created = {}, {}
        for cid, item in args["emails"].items():
            blob = self.blobs.get(item.get("blobId"))
            if "Email/import" in self.not_created:
                not_created[cid] = {"type": self.not_created["Email/import"], "description": SERVER_TEXT}
            elif blob is None:
                not_created[cid] = {"type": "blobNotFound"}
            else:
                eid = self.next_id("M")
                self.emails[eid] = {"blobId": item["blobId"], "mailboxIds": dict(item.get("mailboxIds") or {}),
                                    "keywords": dict(item.get("keywords") or {}), "bytes": blob}
                created[cid] = {"id": eid, "blobId": item["blobId"], "threadId": "T1", "size": len(blob)}
                created_ids[cid] = eid
        return [["Email/import", {"accountId": ACCOUNT, "oldState": "e1", "newState": "e2",
                                  "created": created or None, "notCreated": not_created or None}, call_id]]

    def m_EmailSubmission_set(self, args, call_id, created_ids):
        created, not_created, email_of = {}, {}, {}
        for cid, sub in (args.get("create") or {}).items():
            email_id = sub.get("emailId") or ""
            if email_id.startswith("#"):
                email_id = created_ids.get(email_id[1:])
            if "EmailSubmission/set" in self.not_created:
                not_created[cid] = {"type": self.not_created["EmailSubmission/set"], "description": SERVER_TEXT}
            elif email_id not in self.emails:
                not_created[cid] = {"type": "invalidProperties", "properties": ["emailId"]}
            elif sub.get("identityId") not in [i["id"] for i in self.identities]:
                not_created[cid] = {"type": "invalidProperties", "properties": ["identityId"]}
            else:
                sid = self.next_id("S")
                self.submissions.append({"identityId": sub["identityId"], "emailId": email_id,
                                         "envelope": sub.get("envelope"), "bytes": self.emails[email_id]["bytes"],
                                         "fields": sorted(sub)})
                created[cid] = {"id": sid, "undoStatus": "final", "sendAt": "2026-11-27T18:00:00Z"}
                created_ids[cid] = sid
                email_of[cid] = email_id
        out = [["EmailSubmission/set", {"accountId": ACCOUNT, "oldState": "q1", "newState": "q2",
                                        "created": created or None, "notCreated": not_created or None}, call_id]]
        updated, not_updated = {}, {}
        for ref, patch in (args.get("onSuccessUpdateEmail") or {}).items():
            eid = email_of.get(ref[1:]) if ref.startswith("#") else None
            if eid is None:
                continue
            if self.fail_update:
                not_updated[eid] = {"type": self.fail_update, "description": SERVER_TEXT}
                continue
            for path, value in patch.items():
                prop, _, key = path.partition("/")
                if value is None:
                    self.emails[eid][prop].pop(key, None)
                else:
                    self.emails[eid][prop][key] = value
            updated[eid] = None
        if updated or not_updated:
            out.append(["Email/set", {"accountId": ACCOUNT, "oldState": "e2", "newState": "e3",
                                      "updated": updated or None, "notUpdated": not_updated or None}, call_id])
        return out

    def m_Email_set(self, args, call_id, created_ids):
        destroyed, not_destroyed = [], {}
        for eid in args.get("destroy") or []:
            if self.emails.pop(eid, None) is None:
                not_destroyed[eid] = {"type": "notFound"}
            else:
                destroyed.append(eid)
        return [["Email/set", {"accountId": ACCOUNT, "oldState": "e3", "newState": "e4",
                               "destroyed": destroyed or None, "notDestroyed": not_destroyed or None}, call_id]]


class JMAPHandler(http.server.BaseHTTPRequestHandler):
    disable_nagle_algorithm = True

    def log_message(self, *args):
        pass

    def answer(self, code, obj=None, raw=None, kind="application/json"):
        body = raw if raw is not None else json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def kind_of(self, body):
        if self.command == "GET" and self.path == "/jmap/session":
            return "session"
        if self.command == "POST" and self.path.startswith("/jmap/upload/"):
            return "upload"
        if self.command == "POST" and self.path == "/jmap/api/":
            try:
                names = [call[0] for call in json.loads(body)["methodCalls"]]
            except Exception:
                return "api?"
            if "EmailSubmission/set" in names:
                return "send"
            if "Email/set" in names:
                return "destroy"
            return "setup"
        return "other"

    def any_method(self):
        srv = self.server
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        kind = self.kind_of(body)
        srv.requests.append((kind, self.command, self.path, {k.lower(): v for k, v in self.headers.items()}, body))
        if self.headers.get("Authorization") != "Bearer " + srv.token:
            return self.answer(401, raw=b"", kind="text/plain")
        if kind in srv.status:
            return self.answer(srv.status[kind], {"type": "urn:ietf:params:jmap:error:limit", "status": srv.status[kind],
                                                  "detail": SERVER_TEXT}, kind="application/problem+json")
        if kind == "session":
            result = (200, None, srv.session_body) if srv.session_body is not None else (200, srv.session())
        elif kind == "upload":
            account = self.path[len("/jmap/upload/"):].rstrip("/")
            if account != ACCOUNT:
                return self.answer(404, {"type": "about:blank", "detail": SERVER_TEXT})
            blob_id = srv.next_id("B")
            srv.blobs[blob_id] = body
            srv.uploads.append((self.headers.get("Content-Type"), body))
            result = (201, {"accountId": ACCOUNT, "blobId": blob_id, "type": self.headers.get("Content-Type"),
                            "size": len(body) + srv.size_skew})
        elif kind in ("setup", "send", "destroy"):
            result = (200, srv.api(json.loads(body)))
            if kind == "send" and srv.rewrite is not None:
                result = (200, srv.rewrite(result[1]))
        else:
            return self.answer(404, {"type": "about:blank"})
        fault = srv.fault.get(kind)
        if fault in ("drop", "hang"):
            if fault == "hang":
                time.sleep(srv.hang_s)
            self.close_connection = True
            return
        if fault == "garbage":
            return self.answer(200, raw=b'{"methodResponses": [["EmailSub')
        if isinstance(fault, int):
            return self.answer(fault, {"type": "urn:ietf:params:jmap:error:limit", "detail": SERVER_TEXT})
        self.answer(*result)

    do_GET = do_POST = any_method


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
        self.assertEqual(jmap.JmapSender(env(), DORIS.upper()).doris_addr, DORIS.upper())

    def test_a_token_that_cannot_ride_in_a_header_is_refused_without_showing_it(self):
        for bad in ("dummy token with spaces", "dummy-token\r\nX-Evil: 1", "dummy-töken", "dummy\ttoken"):
            with self.assertRaises(jmap.ConfigError, msg=repr(bad)) as cm:
                jmap.JmapSender(env(DORIS_JMAP_TOKEN=bad), DORIS)
            self.assertIn("DORIS_JMAP_TOKEN", str(cm.exception))
            self.assertNotIn(bad, str(cm.exception))

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
        secret_free(self, cm.exception)


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
            self.assertIn(kind, ("session", "setup", "upload", "send", "destroy"))
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


if __name__ == "__main__":
    unittest.main()
