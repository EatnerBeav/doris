"""Her sends over Fastmail's JMAP API (RFC 8620 core, RFC 8621 mail and submission), not SMTP.

The law: live mail is sent via Fastmail JMAP over HTTPS (DigitalOcean blocks SMTP). Reading stays
IMAP and the calendar stays CalDAV (adapters/live.py, whose LiveMail sends through this module).

The key: an API token for her own Fastmail account, DORIS_JMAP_TOKEN, scoped "Email" and
"Email submission" (Fastmail's JMAP takes a Bearer token only; an app password gets 401). It is
held like live.py's password: never in a repr, an error or a log, and sent only to the session's
host and to the host the session names for apiUrl and uploadUrl once checked (step 1). A token
with a space, a control or a non-ASCII character in it, or one too short to be an API token
(MIN_TOKEN_CHARS), is refused before anything connects, naming the setting only.
The session must be for the account the address setting names (its username), so a token for
another account never sends as her. The smoke builds a second sender for the wolf.whizz test
account from its own two settings (address_name, token_name); her loop never reads them.

A send is four requests, each on a connection of its own with the timeout given:
1. GET the session (https://api.fastmail.com/jmap/session). It must offer core, mail and
   submission, name one account for both mail and submission, and put apiUrl and uploadUrl on
   https, port 443 (or none), on api.fastmail.com or a host below it at a label boundary:
   Fastmail's real session names the account's own host, such as https://phl.api.fastmail.com,
   and can move the account, so no one regional name is pinned. Anything else is refused before
   another byte goes. Requests 2 to 4 go to the host and port apiUrl and uploadUrl name.
2. Mailbox/get and Identity/get in one request: her Drafts and Sent mailboxes by role and the
   identity whose email is hers.
3. Upload the message, unchanged, as message/rfc822; the server must count its exact size.
4. One API request: Email/import of that blob into Drafts ($draft, $seen), and EmailSubmission/set
   for the imported email with her identity and an explicit envelope (her address; exactly the
   recipients given), whose onSuccessUpdateEmail moves her copy to Sent and clears $draft.

The rules every send of hers keeps:
- Refused before any connection (_checked): bytes only (line ends made CRLF), no Bcc
  or Resent-Bcc header, exactly one From and it is hers alone, exactly one Message-ID and it is
  the one given, the envelope a non-empty list of bare addresses.
- MailError: it surely did not go. Any failure in steps 1 to 3; a step-4 request that never fully
  went or that the server turned away (4xx); an answer saying the submission was not created
  (notCreated), or that the import failed (notCreated, or an error for the whole method), so no
  draft was there to submit (then any imported draft is destroyed, best effort).
- SendUnknown: the step-4 request went and no readable answer came back (lost connection,
  timeout, a 5xx, an answer that cannot be read); or the answer holds no answer for the
  submission, or an error for its whole method, while the import did not fail (review: neither
  says no submission was made). The caller looks in Sent; nothing here sends it again. No step
  is ever retried here.
- A created submission is a send, even when moving her copy to Sent failed. last_copy_in_sent is
  then False: her copy may sit in Drafts, where a lookup in Sent will not find it.
- Every part of the message is inline text/plain or her own .ics (review 14): checked before any
  connection with the rest.
- Errors carry the step and an HTTP status, a JMAP error type or an exception class name: never a
  header, a body, the token or server text.

find_message reads mail back (Email/query on the Message-ID header, then Email/get): the smoke's
look for her reply in the wolf.whizz account. It sends and changes nothing.

Hosts: api.fastmail.com, Fastmail's JMAP session resource (https://api.fastmail.com/jmap/session),
which hosts() names for the run's network guard; then the host the session names for apiUrl and
uploadUrl, which the guard cannot know before the session is read: once the session passed the
check above, the sender tells the guard of it (netguard.admit, which takes only a name below
api.fastmail.com, on the guards that allow api.fastmail.com). Every connection verifies the server
certificate, TLS 1.2 or newer.
"""

import collections
import email.parser
import email.policy
import email.utils
import http.client
import json
import re
import ssl
import urllib.parse

from .. import caps, ics, netguard
from ..shapes import MailError, SendUnknown

# --- the env names this module reads (values live only in the server's .env) -------------------
ENV_ADDRESS = "DORIS_MAIL_ADDRESS"     # her address: the From, the envelope sender, her identity's email
ENV_TOKEN = "DORIS_JMAP_TOKEN"         # her API token (scopes: Email, Email submission)
ENV_NAMES = (ENV_ADDRESS, ENV_TOKEN)
MIN_TOKEN_CHARS = 32                   # Fastmail's API tokens run to about 70 characters; fewer is a paste gone wrong
MAX_TOKEN_CHARS = 4096

HOST = "api.fastmail.com"
SESSION_URL = "https://api.fastmail.com/jmap/session"

CORE = "urn:ietf:params:jmap:core"
MAIL = "urn:ietf:params:jmap:mail"
SUBMISSION = "urn:ietf:params:jmap:submission"
USING = [CORE, MAIL, SUBMISSION]

MAX_ANSWER_BYTES = 4 * 1024 * 1024     # a longer answer is one it does not read
FIND_LIMIT = 50                        # messages one look for a Message-ID reads at most
_DRAFT, _SUBMIT = "draft", "send"      # creation ids within the one send request
# Mailbox roles (RFC 8621 2, the IANA registry) a found message's place is named by; any other is "other".
ROLES = ("all", "archive", "drafts", "flagged", "important", "inbox", "junk", "sent", "subscribed", "trash")

# Every pattern that validates a whole value ends in \Z: in Python, $ also matches before a final
# newline. _ADDRESS, _MESSAGE_ID and _HEADER_FIELD are live.py's, so both senders refuse alike.
_ADDRESS = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
                      r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+\Z")
_MESSAGE_ID = re.compile(r"^[!#-;=?A-\[\]-~]{1,200}@[!#-;=?A-\[\]-~]{1,200}\Z")   # printable, no space " < > @ \
_HEADER_FIELD = re.compile(rb"([!-9;-~]+)[ \t]*:")  # a field name; RFC 5322's obsolete syntax allows blanks before the colon
_NEVER_HEADERS = ("bcc", "resent-bcc")             # the owner's copy rides on the envelope, never in a header
_MIME_HEADERS = ("Content-Type", "Content-Transfer-Encoding", "Content-Disposition")
_TOKEN = re.compile(r"^[!-~]+\Z")                   # visible ASCII: no space, control or non-ASCII character
_ID = re.compile(r"^[A-Za-z0-9_-]{1,255}\Z")        # a JMAP Id (RFC 8620 1.2)
_ERROR_TYPE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,63}\Z")      # a JMAP error type is a plain name
_URL_PATH = re.compile(r"^/[A-Za-z0-9._~%!$&'()*+,;=:@/-]*\Z")  # RFC 3986 path characters
_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")  # one DNS label (RFC 1123), as urlsplit lowercases it
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
    """The host the sender connects to first, for its session. The run's network guard allows it;
    the host the session names is added to the guard once checked (netguard.admit)."""
    return [HOST]


# ------------------------------------------------------------------------------------------------
# Settings
# ------------------------------------------------------------------------------------------------
def _value(env, name):
    value = env.get(name)
    return value.strip() if isinstance(value, str) else ""


def _address(env, name=ENV_ADDRESS):
    value = _value(env, name)
    if not value:
        raise ConfigError("%s is not set" % name)
    if not _ADDRESS.match(value):
        raise ConfigError("%s is not one bare email address" % name)
    return value


def _bearer(env, name=ENV_TOKEN):
    """The Authorization header value for the token setting `name`, held as a secret. The value is
    taken as it is, never trimmed: a space at either end is a paste gone wrong, refused like one
    inside. ConfigError names the setting, never its value."""
    value = env.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("%s is not set" % name)
    if not _TOKEN.match(value):
        raise ConfigError("%s has a space, a control or a non-ASCII character in it, which no API token has" % name)
    if len(value) < MIN_TOKEN_CHARS:
        raise ConfigError("%s is too short to be an API token" % name)
    if len(value) > MAX_TOKEN_CHARS:
        raise ConfigError("%s is too long to be an API token" % name)
    return _Secret("Bearer " + value)


def _timeout(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
        raise ConfigError("the timeout must be a positive number of seconds")
    return float(value)


def _on_host(host, allowed_host, below):
    """host is allowed_host itself, or (below) a DNS name below it at a label boundary: whole labels,
    then "." and allowed_host ("phl.api.fastmail.com"; never "xapi.fastmail.com",
    "api.fastmail.com.example", ".api.fastmail.com" or a name with a final dot)."""
    if host == allowed_host:
        return True
    if not below or not host.endswith("." + allowed_host):
        return False
    return all(_LABEL.match(label) for label in host[:-len(allowed_host) - 1].split("."))


def _origin(url, allowed_host, below=False):
    """(scheme, host, port, path) of an absolute http(s) URL on allowed_host (or, with below, a host
    below it) with a plain path and no user, query or fragment; None for anything else."""
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
    if url.endswith(("?", "#")) or not host or not _URL_PATH.match(parts.path):
        return None
    host = host.lower()
    if not _on_host(host, allowed_host, below):
        return None
    return scheme, host, (443 if scheme == "https" else 80) if port is None else port, parts.path


# ------------------------------------------------------------------------------------------------
# Small shared pieces (live.py's, copied so that neither module imports the other's private names)
# ------------------------------------------------------------------------------------------------
def _tls():
    """A client context that verifies the server's certificate and name, TLS 1.2 or newer: Python
    3.9's default context sets no floor of its own (as live._tls, review 15)."""
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def _https(host, port, timeout):
    """HTTPS that verifies the server's certificate and name, TLS 1.2 or newer."""
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


def _parts_ok(message):
    """True when every part of the message is inline text/plain (no file name) or her own .ics
    (text/calendar whose content is her fixed form, ics.fields_ok), alone or inside one
    multipart/mixed, and the MIME structure reads without a defect or a doubled Content-* header
    (two readers could see two different messages). Never raises: what it cannot read is False."""
    try:
        root = email.parser.BytesParser(policy=email.policy.compat32).parsebytes(message)
        if root.is_multipart():
            if root.get_content_type() != "multipart/mixed":
                return False
            leaves = root.get_payload()
        else:
            leaves = [root]
        for part in [root] + list(leaves):
            if part.defects or any(len(part.get_all(name) or ()) > 1 for name in _MIME_HEADERS):
                return False
        for part in leaves:
            kind = part.get_content_type()
            if part.is_multipart() or kind not in ("text/plain", "text/calendar"):
                return False
            if kind == "text/plain" and (part.get_filename() is not None
                                         or part.get_content_disposition() == "attachment"):
                return False
            if kind == "text/calendar" and not ics.fields_ok(part.get_payload(decode=True)):
                return False
        return True
    except Exception:          # a check that cannot run fails closed
        return False


def _checked(raw, envelope_to, message_id, from_addr, step):
    """(the message with CRLF line ends, the recipients), or a MailError before any connection:
    bytes only; no Bcc or Resent-Bcc header; exactly one From, the account's own address alone;
    exactly one Message-ID, the one the caller recorded; every part text/plain or her own .ics
    (_parts_ok); every recipient one bare address."""
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
    if len(senders) != 1 or senders[0][1].lower() != from_addr.lower():
        raise refuse("the message is not from the account's own address alone")
    target = _norm_mid(message_id) if isinstance(message_id, str) else ""
    mids = [_norm_mid(value.decode("ascii", "replace")) for name, value in fields if name == "message-id"]
    if not _MESSAGE_ID.match(target) or mids != [target]:
        raise refuse("the message does not carry exactly the Message-ID given")
    if not _parts_ok(message):
        raise refuse("a part is not text/plain or her own .ics")
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
    ("created", the record), ("refused", an error type) when notCreated names it, ("error", an
    error type) when the whole method answered with an error, ("missing", None) when no answer
    came for the call, or ("unclear", None) when the answer names the record neither way."""
    for rname, args, rid in responses:
        if rid != call_id:
            continue
        if rname == "error":
            return "error", _error_type(args)
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


_Account = collections.namedtuple("_Account", "id api upload drafts sent identity")   # api, upload: (host, port, path)


# ------------------------------------------------------------------------------------------------
# The sender
# ------------------------------------------------------------------------------------------------
class JmapSender(object):
    """Sends mail through Fastmail's JMAP API under one account's API token: hers (DORIS_MAIL_ADDRESS,
    DORIS_JMAP_TOKEN) by default, or the account address_name and token_name name (the smoke's
    wolf.whizz sender). send has LiveMail.send's contract; check_login proves the token, the
    account, its mailboxes and its identity; find_message reads mail back.

    http_factory(host, port, timeout) makes each connection (default: HTTPS that verifies the
    certificate). session_url must be on allowed_host; apiUrl and uploadUrl must then be on the
    session URL's own scheme and port, on allowed_host or a host below it, and each request after
    the session goes to the host and port they name. A plain-http session URL, or one on a port
    other than 443, is taken only with a factory of the caller's (a test's fake server), never with
    the default TLS transport."""

    def __init__(self, env, from_addr, timeout_s=caps.MAIL_TIMEOUT_S, http_factory=None,
                 session_url=SESSION_URL, allowed_host=HOST, address_name=ENV_ADDRESS, token_name=ENV_TOKEN):
        self.address_name, self.token_name = str(address_name), str(token_name)
        login = _address(env, self.address_name)
        if not isinstance(from_addr, str) or from_addr.strip().lower() != login.lower():
            raise ConfigError("the address must be %s, the address of the account's own" % self.address_name)
        self.from_addr = from_addr.strip()
        self._auth = _bearer(env, self.token_name)
        self.timeout_s = _timeout(timeout_s)
        self.allowed_host = str(allowed_host).strip().lower()
        origin = _origin(session_url, self.allowed_host)
        if origin is None:
            raise ConfigError("the session URL must be a plain URL on %s" % self.allowed_host)
        if (origin[0] != "https" or origin[2] != 443) and http_factory is None:
            raise ConfigError("the session URL must be https, on port 443")
        self._origin = origin[:3]
        self._session_path = origin[3]
        self._http_factory = http_factory or _https
        self.last_copy_in_sent = None   # after a send that went: True in Sent, False maybe still in Drafts

    # -- one request -------------------------------------------------------------------------------
    def _request(self, step, method, where, body=None, content_type=None, after=MailError):
        """(status, answer bytes) for one request on a connection of its own to where, a (host,
        port, path): the session's, or one the session named and _named checked. Until the request
        has gone in full, a failure is a MailError; once it has, a failure to read the answer is
        an `after` (SendUnknown for the send itself)."""
        host, port, path = where
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

    def _api(self, step, api, calls, after=MailError):
        """The method responses to one API request (RFC 8620 3.3) to api, the (host, port, path) of
        apiUrl. A 4xx is a request the server turned away (MailError); any other status, or an
        answer it cannot read, is an `after`."""
        body = json.dumps({"using": USING, "methodCalls": calls}).encode("utf-8")
        status, data = self._request(step, "POST", api, body, "application/json", after)
        if status != 200:
            raise _failure(MailError if 400 <= status < 500 else after, step, status=status)
        answer = _json(data)
        responses = answer.get("methodResponses") if isinstance(answer, dict) else None
        if not isinstance(responses, list) or not all(_is_response(item) for item in responses):
            raise _failure(after, step, detail="the answer cannot be read")
        return responses

    # -- steps 1 and 2: the session, her mailboxes and her identity --------------------------------
    def _named(self, url, name, step):
        """(host, port, path) of a URL the session names, which must be on the session's own scheme
        and port (https and 443 for Fastmail's), on its host or a host below it at a label boundary:
        Fastmail names the account's own, such as phl.api.fastmail.com. The token goes nowhere else."""
        origin = _origin(url, self.allowed_host, below=True)
        scheme, _, port = self._origin
        if origin is None or origin[0] != scheme or origin[2] != port:
            raise _failure(MailError, step, detail="%s is not on %s or a host below it, over the session's own "
                                                   "scheme and port" % (name, self.allowed_host))
        return origin[1:]

    def _session(self):
        """(account id, apiUrl, uploadUrl) from the session resource (RFC 8620 2), each URL as the
        (host, port, path) its requests go to. Its username must be the account's own address: a
        token for another account sends nothing as this one. Once both URLs passed, the run's
        network guard is told of any host below the session's that they name (netguard.admit)."""
        step = "jmap session"
        status, data = self._request(step, "GET", self._origin[1:] + (self._session_path,))
        if status != 200:
            raise _failure(MailError, step, status=status)
        session = _json(data)
        if not isinstance(session, dict):
            raise _failure(MailError, step, detail="the answer cannot be read")
        username = session.get("username")
        if not isinstance(username, str) or username.strip().lower() != self.from_addr.lower():
            raise _failure(MailError, step, detail="the session is not for the account %s names" % self.address_name)
        capabilities = session.get("capabilities")
        if not isinstance(capabilities, dict) or not all(name in capabilities for name in USING):
            raise _failure(MailError, step, detail="the token does not reach core, mail and submission")
        primary = session.get("primaryAccounts")
        account = primary.get(MAIL) if isinstance(primary, dict) else None
        if not _is_id(account) or primary.get(SUBMISSION) != account:
            raise _failure(MailError, step, detail="mail and submission are not one account")
        api = self._named(session.get("apiUrl"), "apiUrl", step)
        template = session.get("uploadUrl")
        if not isinstance(template, str) or "{accountId}" not in template:
            raise _failure(MailError, step, detail="uploadUrl has no place for the account")
        upload = self._named(template.replace("{accountId}", account), "uploadUrl", step)
        for host in sorted({api[0], upload[0]} - {self.allowed_host}):
            netguard.admit(host, self.allowed_host)
        return account, api, upload

    def _setup(self):
        """Her account: the session, then her Drafts and Sent mailboxes and her identity in one request."""
        account, api, upload = self._session()
        responses = self._api("jmap setup", api, [
            ["Mailbox/get", {"accountId": account, "ids": None, "properties": ["id", "role"]}, "boxes"],
            ["Identity/get", {"accountId": account, "ids": None, "properties": ["id", "email"]}, "who"]])
        boxes = _method(responses, "boxes", "Mailbox/get", "jmap mailboxes")
        drafts = _one_role(boxes, "drafts", "Drafts", "jmap mailboxes")
        sent = _one_role(boxes, "sent", "Sent", "jmap mailboxes")
        who = _method(responses, "who", "Identity/get", "jmap identity")
        items = who.get("list") if isinstance(who.get("list"), list) else []
        mine = [item.get("id") for item in items if isinstance(item, dict) and isinstance(item.get("email"), str)
                and item["email"].strip().lower() == self.from_addr.lower()]
        if not mine or not _is_id(mine[0]):
            raise _failure(MailError, "jmap identity", detail="no identity for the account's address")
        return _Account(account, api, upload, drafts, sent, mine[0])

    # -- step 3: the upload ------------------------------------------------------------------------
    def _upload(self, account, message):
        """The blob id of the message, uploaded as it is (RFC 8620 6.1)."""
        step = "jmap upload"
        status, data = self._request(step, "POST", account.upload, message, "message/rfc822")
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
        envelope = {"mailFrom": {"email": self.from_addr, "parameters": None},
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
        responses = self._api("jmap send", account.api, calls, after=SendUnknown)
        imported, draft = _outcome(responses, "import", "Email/import", _DRAFT)
        submitted, why = _outcome(responses, "submit", "EmailSubmission/set", _SUBMIT)
        email_id = draft.get("id") if imported == "created" and isinstance(draft, dict) else None
        email_id = email_id if _is_id(email_id) else None
        if submitted == "created":
            self.last_copy_in_sent = _moved(responses, "submit", email_id)
            return
        no_draft = imported in ("refused", "error")       # the import surely failed: nothing to submit
        if submitted == "unclear" or (submitted in ("missing", "error") and not no_draft):
            raise _failure(SendUnknown, "jmap submission", detail={
                "unclear": "the answer cannot be read", "missing": "no answer"}.get(submitted, why))
        if email_id is not None:
            self._destroy(account, email_id)
        if no_draft:                                       # the cause, whatever the submission's answer
            raise _failure(MailError, "jmap import", detail=draft)
        raise _failure(MailError, "jmap submission", detail=why)     # notCreated names the submission

    def _destroy(self, account, email_id):
        """Remove the draft of a send that did not go. Best effort: never raises."""
        try:
            self._api("jmap draft cleanup", account.api,
                      [["Email/set", {"accountId": account.id, "destroy": [email_id]}, "cleanup"]])
        except Exception:
            pass

    # -- the interface -------------------------------------------------------------------------------
    def send(self, raw, envelope_to, message_id, template=None):
        """Hand one message to the JMAP server: the account's address as the envelope sender,
        exactly envelope_to as the recipients, raw as it is (CRLF line ends). One submission
        request, never a retry. template is accepted for the interface and not used.

        Returns None when the server took it. MailError: it did not go. SendUnknown: the
        submission request went and no readable answer came back; the caller looks in Sent and
        nothing here sends it again. After a send that went, last_copy_in_sent says whether her
        copy reached Sent (False: it may sit in Drafts)."""
        self.last_copy_in_sent = None
        message, recipients = _checked(raw, envelope_to, message_id, self.from_addr, "jmap send")
        account = self._setup()
        blob_id = self._upload(account, message)
        self._submit(account, blob_id, recipients)

    def check_login(self):
        """Read the session, the account's mailboxes and its identity: proves the token, its scopes,
        the account, Drafts, Sent and the identity. Sends and uploads nothing. Raises MailError."""
        self._setup()

    def find_message(self, message_id):
        """Where the message whose Message-ID is exactly this one sits in the account: the sorted
        roles (ROLES, else "other") of the mailboxes holding it, or None when no message has it.
        One request after the session: Email/query on the Message-ID header (a contains search, so
        each message it names is read back and kept only when its messageId is this one), Email/get
        of those, and the mailboxes' roles. Reads only. MailError when it cannot tell: an error or
        an answer it cannot read, or a message the query named that the get left out."""
        step = "jmap find"
        target = _norm_mid(message_id) if isinstance(message_id, str) else ""
        if not _MESSAGE_ID.match(target):
            raise _failure(MailError, step, detail="not a Message-ID it can search for")
        account, api, _ = self._session()
        responses = self._api(step, api, [
            ["Email/query", {"accountId": account, "filter": {"header": ["Message-ID", target]},
                             "limit": FIND_LIMIT}, "query"],
            ["Email/get", {"accountId": account, "#ids": {"resultOf": "query", "name": "Email/query", "path": "/ids"},
                           "properties": ["messageId", "mailboxIds"]}, "read"],
            ["Mailbox/get", {"accountId": account, "ids": None, "properties": ["id", "role"]}, "boxes"]])
        ids = _method(responses, "query", "Email/query", step).get("ids")
        if not isinstance(ids, list) or not all(_is_id(i) for i in ids):
            raise _failure(MailError, step, detail="the answer cannot be read")
        items = _method(responses, "read", "Email/get", step).get("list")
        boxes = _method(responses, "boxes", "Mailbox/get", step).get("list")
        if not isinstance(items, list) or not isinstance(boxes, list):
            raise _failure(MailError, step, detail="the answer cannot be read")
        read = {item.get("id"): item for item in items if isinstance(item, dict)}
        if any(i not in read for i in ids):
            raise _failure(MailError, step, detail="a message the query named could not be read; cannot tell")
        role_of = {box.get("id"): box.get("role") for box in boxes if isinstance(box, dict)}
        places = set()
        for i in ids:
            mids, where = read[i].get("messageId"), read[i].get("mailboxIds")
            if not (isinstance(mids, list) and any(isinstance(m, str) and _norm_mid(m) == target for m in mids)):
                continue
            if not isinstance(where, dict) or not where:
                raise _failure(MailError, step, detail="a message it found names no mailbox; cannot tell")
            for box_id, on in where.items():
                if on is True:
                    role = role_of.get(box_id)
                    places.add(role if role in ROLES else "other")
        if places:
            return sorted(places)
        if len(ids) >= FIND_LIMIT:
            raise _failure(MailError, step, detail="more messages matched than one look reads; cannot tell")
        return None
