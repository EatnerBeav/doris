"""An .eml into a Message (screen 1's reader).

Everything an email can try reaches Doris through this file, so it is strict:
- Parsed with the standard library's email package (policy=default). Every header is decoded
  (RFC 2047, and raw UTF-8 per RFC 6532); every text part is decoded by this code, strictly,
  in its declared charset. Python's own text decoder replaces bad bytes silently, so it is
  never used (decision 60).
- No usable From, a broken structure or an undecodable body or header raises DecodeError.
  Screen 1 quarantines it and the run goes on. A read error never becomes "nothing found".
- Attachments are listed by name, declared type and size and never decoded. A text/calendar
  part is always an attachment. An attached email (message/*) is never descended into.
  A delivery report (multipart/report or message/delivery-status) is not read at all.
- HTML is turned into text with html.parser: script, style, head, title and template dropped,
  hidden text (ruling 13's token list) kept apart in hidden_text for the hold quote.
- Links, phone numbers and addresses are counted, never opened.
- own_text / below_closing / quoted_text follow row N4: the sender's own non-quoted words down
  to the first closing line. The text is kept as written (N1 folding is the parser's job).
"""

import datetime as _dt
import hashlib
import re
import unicodedata
from email import policy as _policy
from email import utils as _eutils
from email.parser import BytesHeaderParser, BytesParser
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple

from .shapes import UTC, Attachment, DecodeError, Message

_POLICY = _policy.default

# Headers kept on Message.headers (lowercased names). Only these; the rest is never needed.
KEPT_HEADERS = (
    "auto-submitted", "precedence", "list-id", "list-unsubscribe", "list-post", "list-help",
    "x-autoreply", "x-autorespond", "x-auto-response-suppress", "content-type", "return-path",
    "x-failed-recipients", "in-reply-to", "references", "message-id", "reply-to", "cc",
)

# Closed set of decode faults (recorded on the quarantine receipt as a word, never email text).
DECODE_FAULTS = ("no_from", "bad_address", "header", "structure", "charset", "body", "parser_error")

_BROKEN_STRUCTURE = (
    "NoBoundaryInMultipartDefect", "StartBoundaryNotFoundDefect", "CloseBoundaryNotFoundDefect",
    "MultipartInvariantViolationDefect", "MissingHeaderBodySeparatorDefect", "MisplacedEnvelopeHeaderDefect",
)
_BAD_TRANSFER = ("InvalidBase64CharactersDefect", "InvalidBase64LengthDefect", "InvalidBase64PaddingDefect")


def _fail(fault, detail=""):
    err = DecodeError("%s%s" % (fault, (": " + detail) if detail else ""))
    err.fault = fault
    return err


# ---------------------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------------------
def _raw_header_values(msg, name) -> List[str]:
    out = []
    for k, v in msg.raw_items():
        if k.lower() == name:
            out.append(v)
    return out


def _fix_raw(value: str, name: str) -> str:
    """Unfold, and turn raw 8-bit bytes (kept by the parser as surrogates) back into UTF-8, strictly."""
    value = re.sub(r"\r?\n(?=[ \t])", "", value).replace("\r", "").replace("\n", "")
    if re.search("[\udc80-\udcff]", value):
        try:
            value = value.encode("ascii", "surrogateescape").decode("utf-8", "strict")
        except UnicodeError:
            raise _fail("header", name)
    return value


def _decoded_header(msg, name) -> Optional[object]:
    """The header object for name (several occurrences joined), decoded strictly, or None."""
    raws = _raw_header_values(msg, name)
    if not raws:
        return None
    sep = ", " if name in ("to", "cc", "reply-to", "from") else " "
    fixed = sep.join(_fix_raw(v, name) for v in raws)
    try:
        h = _POLICY.header_factory(name, fixed)
        text = str(h)
    except Exception:
        raise _fail("header", name)
    if "\ufffd" in text and "\ufffd" not in fixed:
        raise _fail("header", name)       # an encoded word in an unknown charset, or bad bytes inside one
    return h


def _addresses(msg, name) -> List[Tuple[str, str]]:
    """[(display_name, bare address lowercased)] for an address header. Any unreadable address raises."""
    h = _decoded_header(msg, name)
    if h is None:
        return []
    for d in getattr(h, "defects", ()):
        if type(d).__name__ == "InvalidHeaderDefect":
            raise _fail("bad_address", name)
    out = []
    for a in getattr(h, "addresses", ()):
        spec = (a.addr_spec or "").strip()
        local, _, domain = spec.rpartition("@")
        if (not local or not domain or re.search(r"\s", spec) or "@" in local.strip('"')
                or re.search("[\udc80-\udcff\ufffd]", spec) or domain.startswith(".") or domain.endswith(".")):
            raise _fail("bad_address", name)
        out.append((a.display_name or "", spec.lower()))
    return out


def _msgids(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return re.findall(r"<[^<>\s]+>", value)


def message_key(message_id: Optional[str], raw: bytes) -> str:
    """Message.key (CONTRACTS, 10:35 PT): the Message-ID without surrounding whitespace and <>, case kept."""
    if message_id:
        k = message_id.strip()
        if k.startswith("<"):
            k = k[1:]
        if k.endswith(">"):
            k = k[:-1]
        k = k.strip()
        if k:
            return k
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def raw_key(raw: bytes) -> str:
    """Message.key read from the headers alone, for a message whose body can't be decoded (ruling 8):
    its one Message-ID when that is plain ASCII, else the hash of its bytes. Never raises."""
    raw = bytes(raw or b"")
    try:
        values = BytesHeaderParser(policy=_policy.compat32).parsebytes(raw).get_all("message-id") or []
    except Exception:
        values = []
    if len(values) == 1 and isinstance(values[0], str):
        value = values[0].strip()
        if value and value.isascii() and not any(c.isspace() for c in value):
            return message_key(value, raw)
    return message_key(None, raw)


def _date_utc(msg) -> Optional[_dt.datetime]:
    """The Date header in UTC, or None when missing or unreadable (never a quarantine: ruling 16).
    Parsed with email.utils directly, because Python 3.9's header registry raises on a bad date."""
    raws = _raw_header_values(msg, "date")
    if not raws:
        return None
    try:
        d = _eutils.parsedate_to_datetime(_fix_raw(raws[0], "date").strip())
    except Exception:
        return None
    if d is None:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)         # "-0000": the time is UTC, the zone unknown
    return d.astimezone(UTC)


# ---------------------------------------------------------------------------------------
# Body parts
# ---------------------------------------------------------------------------------------
def _decode_part_text(part) -> str:
    for d in getattr(part, "defects", ()):
        if type(d).__name__ in _BAD_TRANSFER:
            raise _fail("body", type(d).__name__)
    try:
        data = part.get_payload(decode=True)
    except Exception:
        raise _fail("body", "transfer encoding")
    for d in getattr(part, "defects", ()):
        if type(d).__name__ in _BAD_TRANSFER:
            raise _fail("body", type(d).__name__)
    if data is None:
        data = b""
    if not isinstance(data, bytes):
        raise _fail("body", "payload")
    charset = part.get_param("charset")
    if isinstance(charset, tuple):        # RFC 2231 form
        charset = charset[2]
    if charset:
        charset = str(charset).strip().strip('"').lower()
        try:
            return data.decode(charset, "strict")
        except LookupError:
            raise _fail("charset", "unknown")
        except UnicodeError:
            raise _fail("body", "bytes do not decode in the declared charset")
    for cs in ("ascii", "utf-8"):          # nothing declared: ASCII, or UTF-8 sent undeclared
        try:
            return data.decode(cs, "strict")
        except UnicodeError:
            continue
    raise _fail("body", "undeclared charset")


def _raw_size(part) -> int:
    try:
        if part.is_multipart() or part.get_content_maintype() == "message":
            return len(part.as_bytes())
        payload = part.get_payload(decode=False)
        if isinstance(payload, str):
            return len(payload.encode("ascii", "surrogateescape"))
        return len(payload or b"")
    except Exception:
        return -1                          # unknown size; the part is still listed, still not inspected


def _calendar_method(part) -> str:
    """The METHOD parameter of a calendar part's declared Content-Type (a MIME header, not its content)."""
    try:
        m = part.get_param("method")
    except Exception:
        return ""
    if isinstance(m, tuple):
        m = m[2]
    m = re.sub(r"[^A-Za-z]", "", str(m or "")).upper()
    return m[:20]


def _attachment(part) -> Attachment:
    try:
        name = part.get_filename() or part.get_param("name") or ""
        if isinstance(name, tuple):
            name = name[2]
    except Exception:
        name = ""
    name = str(name)
    if re.search("[\udc80-\udcff]", name):
        name = name.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    return Attachment(name=name, declared_type=part.get_content_type().lower(), size=_raw_size(part), inspected=False)


def _is_attachment(part) -> bool:
    ctype = part.get_content_type().lower()
    if ctype not in ("text/plain", "text/html"):
        return True                        # text/calendar, images, PDFs, office files, archives, other text/*
    disp = (part.get_content_disposition() or "").lower()
    if disp == "attachment":
        return True
    try:
        if part.get_filename():
            return True
    except Exception:
        return True
    return False


def _walk(part, plains: List[str], htmls: List[str], atts: List[Attachment], report: List[bool], depth=0,
          cal_methods: Optional[List[str]] = None):
    if cal_methods is None:
        cal_methods = []
    if depth > 40:
        raise _fail("structure", "nesting")
    ctype = part.get_content_type().lower()
    if ctype in ("multipart/report", "message/delivery-status", "message/delivery-status-global"):
        report[0] = True
    if part.is_multipart():
        if part.get_content_maintype() == "message":
            atts.append(_attachment(part))     # an attached email: listed, never descended into
            return
        for d in getattr(part, "defects", ()):
            if type(d).__name__ in _BROKEN_STRUCTURE:
                raise _fail("structure", type(d).__name__)
        children = part.get_payload()
        if not isinstance(children, list):
            raise _fail("structure", "multipart payload")
        for child in children:
            _walk(child, plains, htmls, atts, report, depth + 1, cal_methods)
        return
    if part.get_content_maintype() == "multipart":
        raise _fail("structure", "multipart without parts")
    if part.get_content_maintype() == "message":
        atts.append(_attachment(part))
        return
    if _is_attachment(part):
        atts.append(_attachment(part))
        if ctype in ("text/calendar", "application/ics"):
            method = _calendar_method(part)
            if method:
                cal_methods.append(method)
        return
    text = _decode_part_text(part)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    (plains if ctype == "text/plain" else htmls).append(text)


# ---------------------------------------------------------------------------------------
# HTML to text, and ruling 13's hidden text
# ---------------------------------------------------------------------------------------
_SKIP = {"script", "style", "title", "template"}
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source",
         "track", "wbr", "keygen"}
_BLOCK = {"p", "div", "br", "tr", "li", "ul", "ol", "table", "h1", "h2", "h3", "h4", "h5", "h6", "hr",
          "blockquote", "pre", "section", "article", "header", "footer", "dl", "dt", "dd", "center"}
_INVISIBLE = re.compile("[\\s\u00a0\u00ad\u034f\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")
_URL_ATTR = re.compile(r"^\s*(?:https?:|ftp:|//)", re.I)

# Ruling 13's list, exactly: display none, visibility hidden, font-size of 0 or 1px, opacity 0,
# white text, mso-hide. (Off-screen positioning stays off the list: decision 65.)
HIDDEN_TOKENS = ("display_none", "visibility_hidden", "font_size", "opacity_0", "white_text", "mso_hide")
_WHITE = re.compile(
    r"^(?:white|#fff|#ffff|#ffffff|#ffffffff|rgba?\(\s*255\s*,\s*255\s*,\s*255\s*(?:,\s*[\d.]+%?\s*)?\)"
    r"|rgba?\(\s*100%\s*,\s*100%\s*,\s*100%\s*(?:,\s*[\d.]+%?\s*)?\)|hsla?\(\s*[\d.]+(?:deg)?\s*,\s*[\d.]+%\s*,\s*100%\s*(?:,\s*[\d.]+%?\s*)?\))$")
_ZERO = re.compile(r"^[+]?0*(?:\.0*)?(?:px|pt|em|rem|ex|ch|vw|vh|%|)$")
_SMALL_PX = re.compile(r"^[+]?(?:0*\.\d+|0*1(?:\.0*)?|0+)px$")


def _size_hides(v: str) -> bool:
    v = v.strip()
    return bool(_ZERO.match(v) or _SMALL_PX.match(v))


def _decls(style: str) -> Dict[str, str]:
    out = {}
    style = re.sub(r"/\*.*?\*/", " ", style or "", flags=re.S)
    for decl in style.split(";"):
        if ":" not in decl:
            continue
        prop, _, val = decl.partition(":")
        val = re.sub(r"!\s*important", "", val, flags=re.I).strip().lower()
        out[prop.strip().lower()] = val
    return out


def hiding_token(style: str) -> Optional[str]:
    """The first token of ruling 13's list that a CSS declaration block carries, or None."""
    d = _decls(style)
    if d.get("display", "").split(" ")[0:1] == ["none"]:
        return "display_none"
    if d.get("visibility", "") == "hidden":
        return "visibility_hidden"
    if "font-size" in d and _size_hides(d["font-size"]):
        return "font_size"
    if "font" in d:
        for tok in re.split(r"[\s/]+", d["font"]):
            if tok and tok[0].isdigit() and _size_hides(tok):
                return "font_size"
    if "opacity" in d and re.match(r"^[+]?0*(?:\.0*)?%?$", d["opacity"]) and d["opacity"] != "":
        return "opacity_0"
    if "color" in d and _WHITE.match(d["color"].replace(" ", " ")):
        return "white_text"
    if "mso-hide" in d and d["mso-hide"] not in ("", "none"):
        return "mso_hide"
    return None


def _compound(sel: str):
    """The last compound of a selector as (tag, id, classes); over-matching is fail-closed."""
    sel = re.sub(r"\[[^\]]*\]", "", sel)
    sel = re.sub(r"::?[\w-]+(\([^)]*\))?", "", sel)
    parts = re.split(r"[\s>+~]+", sel.strip())
    last = parts[-1] if parts else ""
    m = re.match(r"^([a-zA-Z][\w-]*|\*)?", last)
    tag = (m.group(1) or "").lower() if m else ""
    ids = re.findall(r"#([\w-]+)", last)
    classes = re.findall(r"\.([\w-]+)", last)
    if not (tag or ids or classes) and last not in ("", "*"):
        return None
    return (tag if tag != "*" else "", ids[0] if ids else "", set(classes))


def _style_rules(css: str) -> List[Tuple[Tuple[str, str, set], str]]:
    rules = []
    css = re.sub(r"/\*.*?\*/", " ", css, flags=re.S)
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        token = hiding_token(m.group(2))
        if not token:
            continue
        for sel in m.group(1).split(","):
            sel = sel.strip()
            if not sel or sel.startswith("@"):
                continue
            comp = _compound(sel)
            if comp is None:
                comp = ("", "", set())     # a selector we cannot read: assume it matches everything
            rules.append((comp, token))
    return rules


class _StyleCollector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.css = []
        self._in = False

    def handle_starttag(self, tag, attrs):
        if tag == "style":
            self._in = True

    def handle_endtag(self, tag):
        if tag == "style":
            self._in = False

    def handle_data(self, data):
        if self._in:
            self.css.append(data)


class _HtmlText(HTMLParser):
    def __init__(self, rules):
        super().__init__(convert_charrefs=True)
        self.rules = rules
        self.stack = []                    # (tag, effective hidden token or None, blockquote depth)
        self.open_count: Dict[str, int] = {}
        self.skip = 0
        self.in_head = False
        self.pre = 0
        self.lines: List[str] = []
        self.cur: List[str] = []
        self.cur_depth = 0
        self.hidden: List[str] = []
        self.token: Optional[str] = None
        self.urls: List[str] = []

    # -- structure
    def _hidden_now(self):
        return self.stack[-1][1] if self.stack else None

    def _bq_depth(self):
        return self.stack[-1][2] if self.stack else 0

    def _newline(self):
        line = "".join(self.cur)
        line = re.sub(r"[ \t]+", " ", line).strip(" \t") if not self.pre else line.rstrip()
        if line and self.cur_depth:
            line = "> " * self.cur_depth + line
        self.lines.append(line)
        self.cur = []
        self.cur_depth = 0

    def _element_token(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        tok = hiding_token(a.get("style", ""))
        if tok:
            return tok
        if tag == "font" and _WHITE.match(a.get("color", "").strip().lower()):
            return "white_text"
        ids = a.get("id", "")
        classes = set(a.get("class", "").split())
        for (rtag, rid, rclasses), rtok in self.rules:
            if rtag and rtag != tag:
                continue
            if rid and rid != ids:
                continue
            if rclasses and not rclasses <= classes:
                continue
            return rtok
        return None

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        for k, v in attrs:
            if k.lower() in ("href", "src") and v and _URL_ATTR.match(v):
                self.urls.append(v.strip())
        if tag == "head":
            self.in_head = True
            return
        if tag == "body":
            self.in_head = False
        if tag in _SKIP:
            self.skip += 1
            return
        if tag in _BLOCK:
            self._newline()
        if tag in _VOID:
            return
        if tag == "pre":
            self.pre += 1
        inherited = self._hidden_now()
        token = inherited or self._element_token(tag, attrs)
        self.stack.append((tag, token, self._bq_depth() + (1 if tag == "blockquote" else 0)))
        self.open_count[tag] = self.open_count.get(tag, 0) + 1

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        for k, v in attrs:
            if k.lower() in ("href", "src") and v and _URL_ATTR.match(v):
                self.urls.append(v.strip())
        if tag in _BLOCK:
            self._newline()

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "head":
            self.in_head = False
            return
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if tag in _VOID:
            return
        if self.open_count.get(tag, 0) > 0:
            while self.stack:
                t = self.stack.pop()[0]
                self.open_count[t] -= 1
                if t == "pre":
                    self.pre = max(0, self.pre - 1)
                if t == tag:
                    break
        if tag in _BLOCK:
            self._newline()

    def handle_data(self, data):
        if self.skip or self.in_head:
            return
        tok = self._hidden_now()
        if tok:
            if _INVISIBLE.sub("", data):
                self.hidden.append(data)
                if self.token is None:
                    self.token = tok
            return
        if not self.pre:
            data = re.sub(r"[ \t\r\n\f]+", " ", data)
        elif "\n" in data:
            chunks = data.split("\n")
            for c in chunks[:-1]:
                self.cur.append(c)
                self.cur_depth = max(self.cur_depth, self._bq_depth())
                self._newline()
            data = chunks[-1]
        if data:
            self.cur.append(data)
            if data.strip():
                self.cur_depth = max(self.cur_depth, self._bq_depth())

    def result(self):
        self._newline()
        text = "\n".join(self.lines)
        text = re.sub(r"\n{3,}", "\n\n", text).strip("\n")
        hidden = re.sub(r"\s+", " ", " ".join(h.strip() for h in self.hidden if h.strip())).strip()
        return text, (hidden or None), (self.token if hidden else None), self.urls


def html_to_text(html: str) -> Tuple[str, Optional[str], Optional[str], List[str]]:
    """(visible text, hidden text or None, hidden token or None, link URLs in href/src attributes)."""
    sc = _StyleCollector()
    try:
        sc.feed(html)
        sc.close()
    except Exception:
        raise _fail("body", "html")
    rules = []
    for css in sc.css:
        rules.extend(_style_rules(css))
    p = _HtmlText(rules)
    try:
        p.feed(html)
        p.close()
    except Exception:
        raise _fail("body", "html")
    return p.result()


# ---------------------------------------------------------------------------------------
# Where she reads (row N4): own text, below the first closing line, quoted and forwarded text
# ---------------------------------------------------------------------------------------
_CLOSINGS = {
    "thanks", "thank you", "thanks so much", "thank you so much", "many thanks", "thanks again",
    "thanks in advance", "thanks a lot", "thx", "ty", "best", "best regards", "kind regards", "warm regards",
    "warmest regards", "regards", "cheers", "sincerely", "sincerely yours", "yours sincerely", "yours truly",
    "best wishes", "all the best", "warmly", "respectfully", "take care", "talk soon", "speak soon", "rgds",
    "with thanks", "thanks and regards", "thanks & regards", "best,", "yours",
}
_FWD = re.compile(
    r"^\s*(?:-{2,}\s*(?:forwarded message|original message|forwarded message)\s*-{2,}"
    r"|-{2,}\s*forwarded message\s*-*|begin forwarded message:|_{10,})\s*$", re.I)
_SENT_FROM = re.compile(r"^\s*(?:sent from (?:my |outlook|mail for|yahoo|aol)|get outlook for )", re.I)
_ATTR_ONE = re.compile(r"^\s*on\s.+\bwrote:\s*$", re.I)


def _is_closing(line: str) -> bool:
    s = line.strip()
    if s in ("--", "-- "):
        return True
    if _SENT_FROM.match(s):
        return True
    s = re.sub(r"[\s,.!;:\u2014\u2013-]+$", "", s).strip().lower()
    return s in _CLOSINGS


def _is_greeting_ok_closing(line: str) -> bool:
    """Closings that may not be a greeting: the signature mark and 'Sent from my ...'."""
    s = line.strip()
    return s in ("--",) or line.rstrip("\n") == "-- " or bool(_SENT_FROM.match(s))


def split_text(text: str) -> Tuple[str, str, str]:
    """(own_text, below_closing, quoted_text) from a message's visible text, lines kept as written."""
    lines = text.split("\n")
    n = len(lines)
    quoted = [False] * n
    # 1. Hand-forwarded blocks and reply headers: from the marker down, everything is quoted.
    for i, line in enumerate(lines):
        start = None
        if _FWD.match(line):
            start = i
            if line.strip().startswith("_") and not any(
                    re.match(r"^\s*\*?from:", l, re.I) for l in lines[i + 1:i + 4]):
                start = None
        elif re.match(r"^\s*\*?from:\*?\s", line, re.I) and any(
                re.match(r"^\s*\*?(sent|date):\*?\s", l, re.I) for l in lines[i + 1:i + 5]):
            start = i
        if start is not None:
            for j in range(start, n):
                quoted[j] = True
            break
    # 2. Quoted lines and "On ... wrote:" attributions (one or two lines).
    for i, line in enumerate(lines):
        if line.lstrip().startswith(">"):
            quoted[i] = True
        elif _ATTR_ONE.match(line):
            quoted[i] = True
        elif re.search(r"\bwrote:\s*$", line, re.I) and i > 0 and re.match(r"^\s*on\s", lines[i - 1], re.I):
            quoted[i] = quoted[i - 1] = True
    own, below, quote = [], [], []
    seen_own = False
    closed = False
    for i, line in enumerate(lines):
        if quoted[i]:
            quote.append(line)
            continue
        if not closed and line.strip():
            if _is_closing(line) and (seen_own or _is_greeting_ok_closing(line)):
                closed = True
            seen_own = True
        (below if closed else own).append(line)
    return "\n".join(own).strip("\n"), "\n".join(below).strip("\n"), "\n".join(quote).strip("\n")


# ---------------------------------------------------------------------------------------
# Counting what is never opened (screen 11)
# ---------------------------------------------------------------------------------------
_URL = re.compile(r"(?i)\b(?:https?://|ftp://|www\.)[^\s<>\"')\]]+")
_EMAIL = re.compile(r"(?i)(?<![\w.+-])[\w.+-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+")
_PHONE = re.compile(r"(?<![\w/.:-])(\+?\(?\d[\d\s().-]{5,}\d)(?![\w/:-]|\.\d)")
_DATEISH = re.compile(r"^\d{4}[-.]\d{1,2}[-.]\d{1,2}$|^\d{1,2}[-.]\d{1,2}[-.]\d{2,4}$")


def _count_phones(text: str) -> int:
    seen = set()
    for m in _PHONE.finditer(text):
        s = m.group(1).strip()
        digits = re.sub(r"\D", "", s)
        if not 7 <= len(digits) <= 15:
            continue
        if _DATEISH.match(s):
            continue
        if re.fullmatch(r"\d+", s) and not s.startswith("+") and len(digits) not in (10, 11):
            continue                       # a bare run of digits is a phone number only at 10 or 11 digits
        seen.add(digits)
    return len(seen)


def _count(text: str, hrefs: List[str]):
    links = set(u.rstrip(".,;:!?").lower() for u in _URL.findall(text))
    for u in hrefs:
        if u.lower().startswith("mailto:"):
            continue
        links.add(u.rstrip(".,;:!?").lower())
    addrs = set(a.lower().rstrip(".") for a in _EMAIL.findall(text))
    stripped = _URL.sub(" ", _EMAIL.sub(" ", text))
    return len(links), _count_phones(stripped), len(addrs)


# ---------------------------------------------------------------------------------------
# The one entry point
# ---------------------------------------------------------------------------------------
def parse_eml(raw: bytes) -> Message:
    """Parse one .eml. Raises DecodeError (with .fault from DECODE_FAULTS) when it cannot be read safely."""
    if not isinstance(raw, (bytes, bytearray)):
        raise _fail("structure", "not bytes")
    raw = bytes(raw)
    try:
        msg = BytesParser(policy=_POLICY).parsebytes(raw)
    except Exception:
        raise _fail("structure", "parse")
    for d in msg.defects:
        if type(d).__name__ in _BROKEN_STRUCTURE:
            raise _fail("structure", type(d).__name__)

    froms = _raw_header_values(msg, "from")
    if len(froms) != 1:
        raise _fail("no_from", "%d From headers" % len(froms))
    try:
        from_list = _addresses(msg, "from")
    except DecodeError as e:
        raise _fail("no_from", getattr(e, "fault", ""))
    if len(from_list) != 1:
        raise _fail("no_from", "%d addresses" % len(from_list))
    from_name, from_addr = from_list[0]

    to = [a for _, a in _addresses(msg, "to")]
    cc = [a for _, a in _addresses(msg, "cc")]
    reply_to = [a for _, a in _addresses(msg, "reply-to")]
    subj = _decoded_header(msg, "subject")
    subject = str(subj) if subj is not None else ""
    mid_h = _decoded_header(msg, "message-id")
    message_id = str(mid_h).strip() if mid_h is not None and str(mid_h).strip() else None
    irt = _decoded_header(msg, "in-reply-to")
    refs = _decoded_header(msg, "references")

    headers: Dict[str, str] = {}
    for name in KEPT_HEADERS:
        try:
            h = _decoded_header(msg, name)
        except DecodeError:
            raise
        if h is not None:
            headers[name] = str(h).strip()

    plains: List[str] = []
    htmls: List[str] = []
    atts: List[Attachment] = []
    report = [False]
    cal_methods: List[str] = []
    try:
        _walk(msg, plains, htmls, atts, report, 0, cal_methods)
    except DecodeError:
        raise
    except RecursionError:
        raise _fail("structure", "nesting")

    if report[0]:
        # A delivery report is filed by screen 7 and nothing in it is read: no part is decoded.
        plains, htmls, atts = [], [], []
        _list_report_parts(msg, atts)

    if cal_methods and not report[0]:
        headers["calendar-method"] = ",".join(cal_methods)    # read from the part's declared type only

    text_plain = "\n".join(plains) if plains else None
    html = "\n".join(htmls) if htmls else None
    hidden_text = hidden_token = None
    hrefs: List[str] = []
    html_text = ""
    if html is not None:
        html_text, hidden_text, hidden_token, hrefs = html_to_text(html)
    # She reads the plain part; HTML only when there is no plain part, or it holds nothing visible.
    visible = text_plain if (text_plain is not None and text_plain.strip()) else (html_text or text_plain or "")
    visible = visible.replace("\r\n", "\n").replace("\r", "\n")
    own, below, quoted = split_text(visible)

    count_text = "\n".join(t for t in (visible, html_text if text_plain is not None else "", hidden_text or "") if t)
    links, phones, addrs = _count(count_text, hrefs)

    return Message(
        key=message_key(message_id, raw),
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        raw_size=len(raw),
        message_id=message_id,
        from_addr=from_addr,
        from_name=from_name,
        reply_to=reply_to,
        to=to,
        cc=cc,
        subject=subject,
        date_utc=_date_utc(msg),
        in_reply_to=_msgids(str(irt) if irt is not None else None),
        references=_msgids(str(refs) if refs is not None else None),
        headers=headers,
        text_plain=text_plain,
        html=html,
        visible_text=visible,
        own_text=own,
        below_closing=below,
        quoted_text=quoted,
        attachments=atts,
        is_report=report[0],
        hidden_text=hidden_text,
        hidden_token=hidden_token,
        links_seen=links,
        phone_numbers_seen=phones,
        addresses_in_body=addrs,
    )


def _list_report_parts(part, atts: List[Attachment], depth=0):
    """A report's parts, listed by type only: the human-readable text part is not read either."""
    if depth > 40:
        return
    if part.is_multipart() and part.get_content_maintype() != "message":
        for child in part.get_payload():
            _list_report_parts(child, atts, depth + 1)
        return
    if depth == 0:
        return
    ctype = part.get_content_type().lower()
    if ctype in ("text/plain", "text/html") and not _is_attachment(part):
        return                             # the report's text: not read, not listed as an attachment
    atts.append(_attachment(part))


def fold_for_match(text: str) -> str:
    """NFKC, drop format characters: used by the stop and sensitive patterns, never stored."""
    text = unicodedata.normalize("NFKC", text or "")
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
