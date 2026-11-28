"""Her sends over Fastmail's JMAP API (RFC 8620 core, RFC 8621 mail and submission), not SMTP.

The droplet blocks outbound SMTP (25, 465, 587) and lets 443 out, so the send moves to HTTPS.
Reading stays IMAP and the calendar stays CalDAV (adapters/live.py); this module only sends.

The key: an API token for her own Fastmail account, DORIS_JMAP_TOKEN, scoped "Email" and
"Email submission" (Fastmail's JMAP takes a Bearer token only; an app password gets 401). It is
held like live.py's password: never in a repr, an error or a log, and sent only to the session's
own host.

A send is four requests, each on a connection of its own with the timeout given:
1. GET the session. It must offer core, mail and submission, name one account for both mail and
   submission, and put apiUrl and uploadUrl on the session's own scheme, host and port
   (https://api.fastmail.com). Anything else is refused before another byte goes.
2. Mailbox/get and Identity/get in one request: her Drafts and Sent mailboxes by role and the
   identity whose email is hers.
3. Upload the message, unchanged, as message/rfc822; the server must count its exact size.
4. One API request: Email/import of that blob into Drafts ($draft, $seen), and EmailSubmission/set
   for the imported email with her identity and an explicit envelope (her address; exactly the
   recipients given), whose onSuccessUpdateEmail moves her copy to Sent and clears $draft.

The rules of live.py's SMTP send hold here:
- Refused before any connection, as LiveMail._checked: bytes only (line ends made CRLF), no Bcc
  or Resent-Bcc header, exactly one From and it is hers alone, exactly one Message-ID and it is
  the one given, the envelope a non-empty list of bare addresses.
- MailError: it surely did not go. Any failure in steps 1 to 3; a step-4 request that never fully
  went or that the server turned away (4xx); an answer saying the import or the submission was
  not created, or holding no answer for one (then the imported draft is destroyed, best effort).
- SendUnknown: the step-4 request went and no readable answer came back (lost connection,
  timeout, a 5xx, an answer that cannot be read). The caller looks in Sent; nothing here sends
  it again. No step is ever retried here.
- A created submission is a send, even when moving her copy to Sent failed. last_copy_in_sent is
  then False: her copy may sit in Drafts, where a lookup in Sent will not find it.
- Errors carry the step and an HTTP status, a JMAP error type or an exception class name: never a
  header, a body, the token or server text.

Host: api.fastmail.com, Fastmail's JMAP session resource (https://api.fastmail.com/jmap/session).
hosts() names it for the run's network guard. Every connection verifies the server certificate.
"""

import collections
import email.utils
import http.client
import json
import re
import ssl
import urllib.parse

from .. import caps
from ..shapes import MailError, SendUnknown

# --- the env names this module reads (values live only in the server's .env) -------------------
ENV_ADDRESS = "DORIS_MAIL_ADDRESS"     # her address: the From, the envelope sender, her identity's email
ENV_TOKEN = "DORIS_JMAP_TOKEN"         # her API token (scopes: Email, Email submission)

HOST = "api.fastmail.com"
SESSION_URL = "https://api.fastmail.com/jmap/session"

CORE = "urn:ietf:params:jmap:core"
MAIL = "urn:ietf:params:jmap:mail"
SUBMISSION = "urn:ietf:params:jmap:submission"
USING = [CORE, MAIL, SUBMISSION]

MAX_ANSWER_BYTES = 4 * 1024 * 1024     # a longer answer is one it does not read
_DRAFT, _SUBMIT = "draft", "send"      # creation ids within the one send request

# Every pattern that validates a whole value ends in \Z: in Python, $ also matches before a final
# newline. _ADDRESS, _MESSAGE_ID and _HEADER_FIELD are live.py's, so both senders refuse alike.
_ADDRESS = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
                      r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+\Z")
_MESSAGE_ID = re.compile(r"^[!#-;=?A-\[\]-~]{1,200}@[!#-;=?A-\[\]-~]{1,200}\Z")   # printable, no space " < > @ \
_HEADER_FIELD = re.compile(rb"([!-9;-~]+)[ \t]*:")  # a field name; RFC 5322's obsolete syntax allows blanks before the colon
_NEVER_HEADERS = ("bcc", "resent-bcc")             # the owner's copy rides on the envelope, never in a header
_TOKEN = re.compile(r"^[!-~]{1,4096}\Z")            # visible ASCII: what a header value can carry
_ID = re.compile(r"^[A-Za-z0-9_-]{1,255}\Z")        # a JMAP Id (RFC 8620 1.2)
_ERROR_TYPE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,63}\Z")      # a JMAP error type is a plain name
_URL_PATH = re.compile(r"^/[A-Za-z0-9._~%!$&'()*+,;=:@/-]*\Z")  # RFC 3986 path characters
_HTTP_FAILURES = (http.client.HTTPException, OSError, ValueError)


class ConfigError(ValueError):
    """The env lacks or misstates a setting. Names the setting, never its value."""


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


def hosts() -> list:
    """The one host the sender connects to. The run's network guard allows it."""
    return [HOST]


# ------------------------------------------------------------------------------------------------
# Settings
# ------------------------------------------------------------------------------------------------
def _value(env, name):
    value = env.get(name)
    return value.strip() if isinstance(value, str) else ""


def _address(env):
    value = _value(env, ENV_ADDRESS)
    if not value:
        raise ConfigError("%s is not set" % ENV_ADDRESS)
    if not _ADDRESS.match(value):
        raise ConfigError("%s is not one bare email address" % ENV_ADDRESS)
    return value


def _bearer(env):
    value = _value(env, ENV_TOKEN)
    if not value:
        raise ConfigError("%s is not set" % ENV_TOKEN)
    if not _TOKEN.match(value):
        raise ConfigError("%s has a character a header cannot carry" % ENV_TOKEN)
    return _Secret("Bearer " + value)


def _timeout(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
        raise ConfigError("the timeout must be a positive number of seconds")
    return float(value)


def _origin(url, allowed_host):
    """(scheme, host, port, path) of an absolute http(s) URL on allowed_host with a plain path and
    no user, query or fragment; None for anything else."""
    if not isinstance(url, str):
        return None
    try:
        parts = urllib.parse.urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("https", "http") or "@" in parts.netloc or parts.query or parts.fragment:
        return None
    if url.endswith(("?", "#")) or not host or host.lower() != allowed_host or not _URL_PATH.match(parts.path):
        return None
    return scheme, host.lower(), port or (443 if scheme == "https" else 80), parts.path


# ------------------------------------------------------------------------------------------------
# Small shared pieces (live.py's, copied so that neither module imports the other's private names)
# ------------------------------------------------------------------------------------------------
def _https(host, port, timeout):
    """HTTPS that verifies the server's certificate and name."""
    return http.client.HTTPSConnection(host, port, timeout=timeout, context=ssl.create_default_context())


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


def _norm_mid(value):
    return str(value or "").strip().strip("<>").strip()


def _header_fields(message):
    """[name in lowercase, raw value] for each field in the header block of a CRLF message, or
    None when a line there is not a header field (then nothing about the headers is certain)."""
    lines = message.split(b"\r\n\r\n", 1)[0].split(b"\r\n")
    if lines[-1] == b"":
        lines.pop()                    # a message that is all header and no body
    fields = []
    for line in lines:
        if line[:1] in (b" ", b"\t") and fields:
            fields[-1][1] += line      # a folded line continues the field before it
            continue
        found = _HEADER_FIELD.match(line)
        if found is None:
            return None
        fields.append([found.group(1).decode("ascii").lower(), line[found.end():]])
    return fields


def _checked(raw, envelope_to, message_id, doris_addr, step):
    """(the message with CRLF line ends, the recipients), or a MailError before any connection:
    bytes only; no Bcc or Resent-Bcc header; exactly one From, hers; exactly one Message-ID,
    the one the caller recorded; every recipient one bare address. As LiveMail._checked."""
    def refuse(why):
        return _failure(MailError, step, detail="refused before connecting: " + why)
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        raise refuse("the message is not bytes")
    message = re.sub(rb"\r\n|\r|\n", b"\r\n", bytes(raw))
    fields = _header_fields(message)
    if fields is None:
        raise refuse("the header block cannot be read")
    names = [name for name, _ in fields]
    if any(name in _NEVER_HEADERS for name in names):
        raise refuse("the message has a Bcc header")
    froms = [value for name, value in fields if name == "from"]
    senders = email.utils.getaddresses([froms[0].decode("utf-8", "replace")]) if len(froms) == 1 else []
    if len(senders) != 1 or senders[0][1].lower() != doris_addr.lower():
        raise refuse("the message is not from her address alone")
    target = _norm_mid(message_id) if isinstance(message_id, str) else ""
    mids = [_norm_mid(value.decode("ascii", "replace")) for name, value in fields if name == "message-id"]
    if not _MESSAGE_ID.match(target) or mids != [target]:
        raise refuse("the message does not carry exactly the Message-ID given")
    if (not isinstance(envelope_to, (list, tuple)) or not envelope_to
            or not all(isinstance(addr, str) and _ADDRESS.match(addr) for addr in envelope_to)):
        raise refuse("the envelope is not a list of bare addresses")
    return message, list(envelope_to)


# ------------------------------------------------------------------------------------------------
# Reading JMAP answers
# ------------------------------------------------------------------------------------------------
def _is_id(value):
    return isinstance(value, str) and bool(_ID.match(value))


def _json(data):
    """The JSON value in an answer, or None when it cannot be read (UnicodeDecodeError and
    JSONDecodeError are ValueErrors; nesting past the parser's depth is a RecursionError)."""
    try:
        return json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError):
        return None


def _error_type(args):
    """A JMAP error's type when it is a plain name, else a fixed phrase: never the server's text."""
    kind = args.get("type") if isinstance(args, dict) else None
    return kind if isinstance(kind, str) and _ERROR_TYPE.match(kind) else "an error it did not name plainly"


def _is_response(item):
    return (isinstance(item, list) and len(item) == 3 and isinstance(item[0], str)
            and isinstance(item[1], dict) and isinstance(item[2], str))


def _method(responses, call_id, name, step):
    """The arguments of the answer to one call. MailError for an error answer or none."""
    for rname, args, rid in responses:
        if rid == call_id and rname == "error":
            raise _failure(MailError, step, detail=_error_type(args))
        if rid == call_id and rname == name:
            return args
    raise _failure(MailError, step, detail="no answer")


def _one_role(boxes, role, label, step):
    """The id of the one mailbox with this role (RFC 8621 2: roles, never names)."""
    items = boxes.get("list")
    if not isinstance(items, list):
        raise _failure(MailError, step, detail="the answer cannot be read")
    found = [box.get("id") for box in items if isinstance(box, dict) and box.get("role") == role]
    if len(found) != 1:
        raise _failure(MailError, step, detail="no single %s mailbox" % label)
    if not _is_id(found[0]):
        raise _failure(MailError, step, detail="the %s mailbox id is not a JMAP id" % label)
    return found[0]


def _outcome(responses, call_id, name, creation_id):
    """What the answer says of one record a call was to create:
    ("created", the record), ("refused", an error type), ("missing", None) when no answer came
    for the call, or ("unclear", None) when the answer names the record neither way."""
    for rname, args, rid in responses:
        if rid != call_id:
            continue
        if rname == "error":
            return "refused", _error_type(args)
        if rname == name:
            created, not_created = args.get("created"), args.get("notCreated")
            if isinstance(created, dict) and creation_id in created:
                return "created", created[creation_id]
            if isinstance(not_created, dict) and creation_id in not_created:
                return "refused", _error_type(not_created[creation_id])
            return "unclear", None
    return "missing", None


def _moved(responses, call_id, email_id):
    """True when the implicit Email/set after the submission (RFC 8621 7.5, onSuccessUpdateEmail)
    reports her copy updated: moved to Sent, $draft cleared."""
    for rname, args, rid in responses:
        if rid == call_id and rname == "Email/set":
            updated = args.get("updated")
            return email_id is not None and isinstance(updated, dict) and email_id in updated
    return False


_Account = collections.namedtuple("_Account", "id api_path upload_path drafts sent identity")


# ------------------------------------------------------------------------------------------------
# The sender
# ------------------------------------------------------------------------------------------------
class JmapSender(object):
    """Sends her mail through Fastmail's JMAP API under her own API token. send has LiveMail.send's
    contract; check_login proves the token, the account, her mailboxes and her identity.

    http_factory(host, port, timeout) makes each connection (default: HTTPS that verifies the
    certificate). session_url must be on allowed_host; apiUrl and uploadUrl must then be on the
    session URL's own scheme, host and port. A plain-http session URL is taken only with a
    factory of the caller's (a test's fake server), never with the default TLS transport."""

    def __init__(self, env, doris_addr, timeout_s=caps.MAIL_TIMEOUT_S, http_factory=None,
                 session_url=SESSION_URL, allowed_host=HOST):
        login = _address(env)
        if not isinstance(doris_addr, str) or doris_addr.strip().lower() != login.lower():
            raise ConfigError("her address must be %s, the address of her own account" % ENV_ADDRESS)
        self.doris_addr = doris_addr.strip()
        self._auth = _bearer(env)
        self.timeout_s = _timeout(timeout_s)
        self.allowed_host = str(allowed_host).strip().lower()
        origin = _origin(session_url, self.allowed_host)
        if origin is None:
            raise ConfigError("the session URL must be a plain URL on %s" % self.allowed_host)
        if origin[0] != "https" and http_factory is None:
            raise ConfigError("the session URL must be https")
        self._origin = origin[:3]
        self._session_path = origin[3]
        self._http_factory = http_factory or _https
        self.last_copy_in_sent = None   # after a send that went: True in Sent, False maybe still in Drafts

    # -- one request -------------------------------------------------------------------------------
    def _request(self, step, method, path, body=None, content_type=None, after=MailError):
        """(status, answer bytes) for one request on a connection of its own. Until the request
        has gone in full, a failure is a MailError; once it has, a failure to read the answer is
        an `after` (SendUnknown for the send itself)."""
        _, host, port = self._origin
        headers = {"Authorization": self._auth.reveal(), "Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        try:
            conn = self._http_factory(host, port, self.timeout_s)
        except _HTTP_FAILURES as exc:
            raise _failure(MailError, step + " connect", exc) from None
        try:
            try:
                conn.connect()
            except _HTTP_FAILURES as exc:
                raise _failure(MailError, step + " connect", exc) from None
            try:
                conn.request(method, path, body=body, headers=headers)
            except _HTTP_FAILURES as exc:
                raise _failure(MailError, step, exc) from None    # cut off before its last byte
            try:
                resp = conn.getresponse()
                data = resp.read(MAX_ANSWER_BYTES + 1)
            except _HTTP_FAILURES as exc:
                raise _failure(after, step, exc) from None
            if len(data) > MAX_ANSWER_BYTES:
                raise _failure(after, step, detail="the answer is too long to read")
            return resp.status, data
        finally:
            _quietly(conn.close)

    def _api(self, step, api_path, calls, after=MailError):
        """The method responses to one API request (RFC 8620 3.3). A 4xx is a request the server
        turned away (MailError); any other status, or an answer it cannot read, is an `after`."""
        body = json.dumps({"using": USING, "methodCalls": calls}).encode("utf-8")
        status, data = self._request(step, "POST", api_path, body, "application/json", after)
        if status != 200:
            raise _failure(MailError if 400 <= status < 500 else after, step, status=status)
        answer = _json(data)
        responses = answer.get("methodResponses") if isinstance(answer, dict) else None
        if not isinstance(responses, list) or not all(_is_response(item) for item in responses):
            raise _failure(after, step, detail="the answer cannot be read")
        return responses

    # -- steps 1 and 2: the session, her mailboxes and her identity --------------------------------
    def _own_path(self, url, name, step):
        """The path of a URL from the session, which must be on the session's own scheme, host
        and port: the token goes nowhere else."""
        origin = _origin(url, self.allowed_host)
        if origin is None or origin[:3] != self._origin:
            raise _failure(MailError, step, detail="%s is not on the session's own host" % name)
        return origin[3]

    def _session(self):
        """(account id, API path, upload path) from the session resource (RFC 8620 2)."""
        step = "jmap session"
        status, data = self._request(step, "GET", self._session_path)
        if status != 200:
            raise _failure(MailError, step, status=status)
        session = _json(data)
        if not isinstance(session, dict):
            raise _failure(MailError, step, detail="the answer cannot be read")
        capabilities = session.get("capabilities")
        if not isinstance(capabilities, dict) or not all(name in capabilities for name in USING):
            raise _failure(MailError, step, detail="the token does not reach core, mail and submission")
        primary = session.get("primaryAccounts")
        account = primary.get(MAIL) if isinstance(primary, dict) else None
        if not _is_id(account) or primary.get(SUBMISSION) != account:
            raise _failure(MailError, step, detail="mail and submission are not one account")
        api_path = self._own_path(session.get("apiUrl"), "apiUrl", step)
        template = session.get("uploadUrl")
        if not isinstance(template, str) or "{accountId}" not in template:
            raise _failure(MailError, step, detail="uploadUrl has no place for the account")
        upload_path = self._own_path(template.replace("{accountId}", account), "uploadUrl", step)
        return account, api_path, upload_path

    def _setup(self):
        """Her account: the session, then her Drafts and Sent mailboxes and her identity in one request."""
        account, api_path, upload_path = self._session()
        responses = self._api("jmap setup", api_path, [
            ["Mailbox/get", {"accountId": account, "ids": None, "properties": ["id", "role"]}, "boxes"],
            ["Identity/get", {"accountId": account, "ids": None, "properties": ["id", "email"]}, "who"]])
        boxes = _method(responses, "boxes", "Mailbox/get", "jmap mailboxes")
        drafts = _one_role(boxes, "drafts", "Drafts", "jmap mailboxes")
        sent = _one_role(boxes, "sent", "Sent", "jmap mailboxes")
        who = _method(responses, "who", "Identity/get", "jmap identity")
        items = who.get("list") if isinstance(who.get("list"), list) else []
        mine = [item.get("id") for item in items if isinstance(item, dict) and isinstance(item.get("email"), str)
                and item["email"].strip().lower() == self.doris_addr.lower()]
        if not mine or not _is_id(mine[0]):
            raise _failure(MailError, "jmap identity", detail="no identity for her address")
        return _Account(account, api_path, upload_path, drafts, sent, mine[0])

    # -- step 3: the upload ------------------------------------------------------------------------
    def _upload(self, account, message):
        """The blob id of the message, uploaded as it is (RFC 8620 6.1)."""
        step = "jmap upload"
        status, data = self._request(step, "POST", account.upload_path, message, "message/rfc822")
        if status not in (200, 201):
            raise _failure(MailError, step, status=status)
        answer = _json(data)
        if (not isinstance(answer, dict) or not _is_id(answer.get("blobId"))
                or answer.get("accountId", account.id) != account.id):
            raise _failure(MailError, step, detail="the answer cannot be read")
        size = answer.get("size")
        if isinstance(size, bool) or size != len(message):
            raise _failure(MailError, step, detail="the size the server counted is not the message's")
        return answer["blobId"]

    # -- step 4: import and submit in one request --------------------------------------------------
    def _submit(self, account, blob_id, recipients):
        """Email/import into Drafts, then EmailSubmission/set for it (RFC 8621 4.8, 7.5)."""
        envelope = {"mailFrom": {"email": self.doris_addr, "parameters": None},
                    "rcptTo": [{"email": rcpt, "parameters": None} for rcpt in recipients]}
        calls = [
            ["Email/import", {"accountId": account.id, "emails": {_DRAFT: {
                "blobId": blob_id, "mailboxIds": {account.drafts: True},
                "keywords": {"$draft": True, "$seen": True}}}}, "import"],
            ["EmailSubmission/set", {"accountId": account.id,
                                     "create": {_SUBMIT: {"identityId": account.identity, "emailId": "#" + _DRAFT,
                                                          "envelope": envelope}},
                                     "onSuccessUpdateEmail": {"#" + _SUBMIT: {
                                         "mailboxIds/" + account.drafts: None, "mailboxIds/" + account.sent: True,
                                         "keywords/$draft": None}}}, "submit"]]
        responses = self._api("jmap send", account.api_path, calls, after=SendUnknown)
        imported, draft = _outcome(responses, "import", "Email/import", _DRAFT)
        submitted, why = _outcome(responses, "submit", "EmailSubmission/set", _SUBMIT)
        email_id = draft.get("id") if imported == "created" and isinstance(draft, dict) else None
        email_id = email_id if _is_id(email_id) else None
        if submitted == "created":
            self.last_copy_in_sent = _moved(responses, "submit", email_id)
            return
        if submitted == "unclear":
            raise _failure(SendUnknown, "jmap submission", detail="the answer cannot be read")
        if email_id is not None:
            self._destroy(account, email_id)
        if imported in ("refused", "missing"):
            raise _failure(MailError, "jmap import", detail=draft if imported == "refused" else "no answer")
        raise _failure(MailError, "jmap submission", detail=why if submitted == "refused" else "no answer")

    def _destroy(self, account, email_id):
        """Remove the draft of a send that did not go. Best effort: never raises."""
        try:
            self._api("jmap draft cleanup", account.api_path,
                      [["Email/set", {"accountId": account.id, "destroy": [email_id]}, "cleanup"]])
        except Exception:
            pass

    # -- the interface -------------------------------------------------------------------------------
    def send(self, raw, envelope_to, message_id, template=None):
        """Hand one message to her JMAP server: her address as the envelope sender, exactly
        envelope_to as the recipients, raw as it is (CRLF line ends). One submission request,
        never a retry. template is accepted for the interface and not used.

        Returns None when the server took it. MailError: it did not go. SendUnknown: the
        submission request went and no readable answer came back; the caller looks in Sent and
        nothing here sends it again. After a send that went, last_copy_in_sent says whether her
        copy reached Sent (False: it may sit in Drafts)."""
        self.last_copy_in_sent = None
        message, recipients = _checked(raw, envelope_to, message_id, self.doris_addr, "jmap send")
        account = self._setup()
        blob_id = self._upload(account, message)
        self._submit(account, blob_id, recipients)

    def check_login(self):
        """Read the session, her mailboxes and her identity: proves the token, its scopes, the
        account, Drafts, Sent and her identity. Sends and uploads nothing. Raises MailError."""
        self._setup()
