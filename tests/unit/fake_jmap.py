"""A fake JMAP server (RFC 8620, RFC 8621) on 127.0.0.1 for the real http.client: the session,
uploads and the methods Doris uses, with faults to stage. Plain HTTP in place of TLS, reached
through the senders' factory hooks. It keeps a mailbox of its own and records what it was sent,
so a test can prove what Doris never does (a second submission, a Bcc, another host, a token or
server text in an error).

Shared by test_jmap (the sender), test_live (LiveMail sends through it), test_live_loop and
test_smoke, whose end-to-end test has two accounts, hers and wolf's (for_account, dialling). Every
value here is made up.

as_fastmail names URLs as Fastmail's real session does: the session on api.fastmail.com, apiUrl,
uploadUrl and eventSourceUrl on the account's own host below it (phl.api.fastmail.com). dialling
lets a test's senders reach the fakes under those very host names, through the network guard as
a real connection goes: only the name lookup below the guard is the test's (Dns).
"""
import email
import email.policy
import http.client
import http.server
import json
import socket
import threading
import time
from unittest import mock

from doris.adapters import jmap

DORIS = "doris.htm@fastmail.com"
TOKEN = "dummy-jmap-token-for-doris-tests-4242"   # a stand-in: never a real key
ACCOUNT = "u1a2b3c4"
SERVER_TEXT = "server-words-never-shown"  # every description the fake writes; never in an error of ours
SESSION_HOST = "https://api.fastmail.com"        # the session's own host (jmap.SESSION_URL)
FASTMAIL = "https://phl.api.fastmail.com"        # where the real session puts apiUrl, uploadUrl and eventSourceUrl:
                                                 # the account's own host below api.fastmail.com (it can move)
DOWNLOAD = "https://phl-www.fastmailusercontent.com/jmap/download/{accountId}/{blobId}/{name}?type={type}"
FASTMAIL_HOSTS = ("api.fastmail.com", "phl.api.fastmail.com")   # the names Dns resolves to a fake

CORE = "urn:ietf:params:jmap:core"
MAIL = "urn:ietf:params:jmap:mail"
SUBMISSION = "urn:ietf:params:jmap:submission"


def header_text(raw, name):
    """The decoded value of one header field of a message, or "" without it."""
    msg = email.message_from_bytes(raw, policy=email.policy.compat32)
    return str(msg.get(name) or "")


def message_ids(raw):
    """What a JMAP server reports as an email's messageId: the ids in its Message-ID header, no brackets."""
    value = header_text(raw, "message-id").strip()
    return [value.strip("<>")] if value else None




# ===========================================================================================
# A fake JMAP server: the session, uploads and the methods Doris uses
# ===========================================================================================
class FakeJMAP(http.server.ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self):
        http.server.ThreadingHTTPServer.__init__(self, ("127.0.0.1", 0), JMAPHandler)
        self.token = TOKEN
        self.username = DORIS       # the session's username: the account the token is for
        self.requests = []          # (kind, method, path, headers with lowercase names, body)
        self.capabilities = [CORE, MAIL, SUBMISSION]
        self.primary = {MAIL: ACCOUNT, SUBMISSION: ACCOUNT}
        self.api_url = None         # None: this server's own
        self.upload_url = None
        self.event_url = None
        self.download_url = None
        self.session_body = None    # bytes answering the session instead
        self.mailboxes = [{"id": "mb-inbox", "role": "inbox", "name": "Inbox"},
                          {"id": "mb-drafts", "role": "drafts", "name": "Drafts"},
                          {"id": "mb-sent", "role": "sent", "name": "Sent"}]
        self.identities = [{"id": "I-doris", "email": DORIS, "name": "Doris"}]
        self.blobs = {}             # blobId -> bytes
        self.uploads = []           # (content type, bytes)
        self.emails = {}            # id -> {"blobId", "mailboxIds", "keywords", "bytes"}
        self.submissions = []       # what went: {"identityId", "emailId", "envelope", "bytes", "fields"}
        self.delivered = []         # (envelope sender, [recipients], message bytes) for each submission created
        self.on_deliver = None      # called (sender, recipients, message) for each submission created: a test
                                    # files her copy in its fake IMAP Sent folder there, as Fastmail would
        self.on_request = None      # called (kind) as each request arrives, before it is answered: a test
                                    # stages a fault for one request there and clears it for the next
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

    def as_fastmail(self, origin=FASTMAIL):
        """Name URLs as Fastmail's real session does, for a sender whose session URL is the real one
        (on api.fastmail.com) and whose transport (jmap._https, patched) dials this fake: apiUrl,
        uploadUrl and eventSourceUrl on origin, by default the account's own host below
        api.fastmail.com (https://phl.api.fastmail.com), and downloadUrl on Fastmail's content host."""
        self.api_url = origin + "/jmap/api/"
        self.upload_url = origin + "/jmap/upload/{accountId}/"
        self.event_url = origin + "/jmap/event/"
        self.download_url = DOWNLOAD
        return self

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
                "username": self.username,
                "apiUrl": self.base + "/jmap/api/" if self.api_url is None else self.api_url,
                "downloadUrl": self.base + "/jmap/download/{accountId}/{blobId}/{name}?type={type}"
                               if self.download_url is None else self.download_url,
                "uploadUrl": self.base + "/jmap/upload/{accountId}/" if self.upload_url is None else self.upload_url,
                "eventSourceUrl": self.base + "/jmap/event/" if self.event_url is None else self.event_url,
                "state": "s1"}

    # -- methods (RFC 8621) --------------------------------------------------------------------
    NEEDS = {"Mailbox/get": MAIL, "Email/import": MAIL, "Email/set": MAIL, "Email/query": MAIL, "Email/get": MAIL,
             "Identity/get": SUBMISSION, "EmailSubmission/set": SUBMISSION}

    def deliver(self, raw, role="inbox"):
        """A message that reached this account (another account's mail to it), in the mailbox with this role."""
        box = [m["id"] for m in self.mailboxes if m["role"] == role][0]
        eid = self.next_id("M")
        self.emails[eid] = {"blobId": None, "mailboxIds": {box: True}, "keywords": {}, "bytes": raw}
        return eid

    @staticmethod
    def resolve(args, responses):
        """A back-reference (RFC 8620 3.7) to a top-level list of an earlier answer, as Doris uses them."""
        out = {}
        for key, value in args.items():
            if key.startswith("#"):
                (found,) = [a for n, a, c in responses if c == value["resultOf"] and n == value["name"]]
                out[key[1:]] = found[value["path"].lstrip("/")]
            else:
                out[key] = value
        return out

    def api(self, request):
        using = request.get("using") or []
        created_ids, responses = {}, []
        for name, args, call_id in request["methodCalls"]:
            try:
                args = self.resolve(args, responses)
            except ValueError:                     # the call it refers to answered with an error
                responses.append(["error", {"type": "invalidResultReference"}, call_id])
                continue
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
                envelope = sub.get("envelope") or {}
                went = ((envelope.get("mailFrom") or {}).get("email"),
                        [r.get("email") for r in envelope.get("rcptTo") or []], self.emails[email_id]["bytes"])
                self.delivered.append(went)
                if self.on_deliver is not None:
                    self.on_deliver(*went)
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

    def m_Email_query(self, args, call_id, created_ids):
        field, text = (list((args.get("filter") or {}).get("header") or []) + [None, None])[:2]
        ids = [eid for eid, em in self.emails.items()
               if field is not None and field.lower() == "message-id"
               and (text or "").lower() in header_text(em["bytes"], "message-id").lower()]
        return [["Email/query", {"accountId": ACCOUNT, "queryState": "q1", "canCalculateChanges": False,
                                 "position": 0, "ids": ids[:args.get("limit") or 256]}, call_id]]

    def m_Email_get(self, args, call_id, created_ids):
        props = args.get("properties")
        found, missing = [], []
        for eid in args.get("ids") or []:
            em = self.emails.get(eid)
            if em is None:
                missing.append(eid)
                continue
            item = {"id": eid, "messageId": message_ids(em["bytes"]), "mailboxIds": dict(em["mailboxIds"]),
                    "subject": SERVER_TEXT}
            found.append({k: v for k, v in item.items() if props is None or k in props or k == "id"})
        return [["Email/get", {"accountId": ACCOUNT, "state": "e1", "list": found, "notFound": missing}, call_id]]

    def m_Email_set(self, args, call_id, created_ids):
        destroyed, not_destroyed = [], {}
        for eid in args.get("destroy") or []:
            if self.emails.pop(eid, None) is None:
                not_destroyed[eid] = {"type": "notFound"}
            else:
                destroyed.append(eid)
        return [["Email/set", {"accountId": ACCOUNT, "oldState": "e3", "newState": "e4",
                               "destroyed": destroyed or None, "notDestroyed": not_destroyed or None}, call_id]]


def for_account(address, token, name="Wolf"):
    """A fake of another account's own (the smoke's wolf.whizz): its token, its session's username
    and its identity; its URLs named as Fastmail's session names them, as for hers."""
    server = FakeJMAP().as_fastmail()
    server.token, server.username = token, address
    server.identities = [{"id": "I-" + address.split("@")[0].replace(".", "-"), "email": address, "name": name}]
    return server


class Dns(object):
    """Name lookups for a test, below the network guard: each of Fastmail's hosts (FASTMAIL_HOSTS)
    resolves to a fake on 127.0.0.1, the one `ports` names for that host or else the one `port`
    holds (Routed sets it for each connection), 127.0.0.1 is itself, and any other name is unknown,
    so no test looks a name up for real. Patched in as socket.getaddrinfo before a guard is
    installed, it is asked only for a name every installed guard allows: a sender's connection
    meets the guard as a real one does (its host name and port at create_connection and
    getaddrinfo, then the address it resolved to at connect)."""

    def __init__(self, port, real, ports=None):
        self.port, self.real, self.ports = port, real, dict(ports or {})

    def __call__(self, host, port, *args, **kw):
        name = host.decode("ascii", "replace") if isinstance(host, bytes) else host
        if name in FASTMAIL_HOSTS:
            to = self.ports.get(name, self.port)
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", to))]
        if name == "127.0.0.1":
            return self.real(host, port, *args, **kw)
        raise socket.gaierror(socket.EAI_NONAME, "not a name this test resolves")


class Routed(http.client.HTTPConnection):
    """One connection to the host and port a sender asks for (api.fastmail.com:443 for the session,
    then the host it names), made once its request is: to the fake whose token the request carries
    (the first fake for any other token). Each account is a fake of its own, as each is its own
    account at Fastmail. Plain HTTP in place of TLS; the name goes through the guard, then Dns."""

    def __init__(self, host, port, servers, dns, timeout):
        http.client.HTTPConnection.__init__(self, host, port, timeout=timeout)
        self.servers, self.dns = servers, dns

    def connect(self):
        if self.sock is None and getattr(self, "routed", False):
            http.client.HTTPConnection.connect(self)

    def request(self, method, url, body=None, headers=None, **kw):
        headers = dict(headers or {})
        token = (headers.get("Authorization") or "")[len("Bearer "):]
        self.dns.port = {s.token: s for s in reversed(self.servers)}.get(token, self.servers[0]).port
        self.routed = True
        self.connect()
        return http.client.HTTPConnection.request(self, method, url, body=body, headers=headers, **kw)


def dialling(*servers):
    """The patches (start them before any guard is installed) for a test whose senders reach these
    fakes as they reach Fastmail: jmap._https makes a Routed connection to the host and port asked
    for, and socket.getaddrinfo is a Dns that takes Fastmail's hosts to the fakes. With more than one
    account, each connection goes to the account its token names."""
    dns = Dns(servers[0].port, socket.getaddrinfo)

    def dial(host, port, timeout):
        return Routed(host, port, list(servers), dns, timeout)
    return [mock.patch.object(socket, "getaddrinfo", dns), mock.patch.object(jmap, "_https", dial)]


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
            if "Email/query" in names:
                return "find"
            return "setup"
        return "other"

    def any_method(self):
        srv = self.server
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        kind = self.kind_of(body)
        srv.requests.append((kind, self.command, self.path, {k.lower(): v for k, v in self.headers.items()}, body))
        if srv.on_request is not None:
            srv.on_request(kind)
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
        elif kind in ("setup", "send", "destroy", "find"):
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


