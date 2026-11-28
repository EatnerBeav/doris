"""Screen 5 and G17: sensitive mail never reaches her.

scan_message(msg) reads the whole decoded message in code only (sender domain, subject, the
plain part, the HTML part's text including hidden text, quoted text) and returns the name of
the first check that matched, or None. scan_text(text) runs the body checks on text about to
leave (template slots, hold quotes, digest lines). The matched value is never returned, logged
or stored: only the check name.

Only patterns and the sender list match, never a topic: "I just left the bank" is clean.
"""

import re
from typing import Iterable, List, Optional

from . import mail

CHECKS = ("sender-domain", "subject-phrase", "one-time-code", "card-number", "bank-routing",
          "bank-account", "ssn", "reset-or-login-alert")

SENDER_DOMAINS = (
    "chase.com", "bankofamerica.com", "wellsfargo.com", "citi.com", "capitalone.com", "americanexpress.com",
    "discover.com", "usbank.com", "paypal.com", "venmo.com", "stripe.com", "irs.gov", "ssa.gov", "adp.com",
    "gusto.com", "intuit.com", "kp.org",
)

# Phrases are matched on folded text: lowercase, apostrophes dropped, other punctuation a space.
_SUBJECT = [re.compile(p) for p in (
    r"\bverification code\b", r"\bsecurity code\b", r"\bpass ?code\b", r"\bone ?time (?:code|pass ?code|password|pin)\b",
    r"\bpassword reset\b", r"\bnew sign ?in\b", r"\bsign ?in alert\b", r"\bnew log ?in\b", r"\blog ?in alert\b",
    r"\btwo factor\b", r"\b2fa\b", r"\baccount statement\b", r"\byour statement\b", r"\bw ?2\b", r"\b1099\b",
)]
_CODE_WORDING = re.compile(
    r"\bverification code\b|\bsecurity code\b|\bpass ?code\b|\bone ?time (?:code|pass ?code|password|pin)\b"
    r"|\botp\b|\blog ?in code\b|\byour code\b")
_ALERT = [re.compile(p) for p in (
    r"\breset your password\b", r"\bpassword reset\b", r"\bnew sign ?in\b", r"\bnew log ?in\b",
    r"\bsign ?in attempt\b", r"\blog ?in attempt\b", r"\bunusual sign ?in\b", r"\bunusual log ?in\b",
    r"\bwas this you\b", r"\bverify its you\b",
)]
_APOS = "'\u2019\u2018\u02bc`\u00b4"
_PHONE_WORDS = re.compile(r"(?:\+|\b(?:tel|phone|cell|mobile|call|fax)\b[\s:.#]*)\s*\(?\s*$", re.I)


def _fold(text: str) -> str:
    return mail.fold_for_match(text)


def _words(text: str) -> str:
    t = _fold(text).lower()
    for a in _APOS:
        t = t.replace(a, "")
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


# ---------------------------------------------------------------------------------------
# Number checks
# ---------------------------------------------------------------------------------------
def luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _card_network(d: str) -> bool:
    n = len(d)
    p2, p3, p4, p6 = int(d[:2]), int(d[:3]), int(d[:4]), int(d[:6])
    if d[0] == "4" and n in (13, 16, 19):
        return True                                            # Visa
    if (51 <= p2 <= 55 or 2221 <= p4 <= 2720) and n == 16:
        return True                                            # Mastercard
    if p2 in (34, 37) and n == 15:
        return True                                            # American Express
    if (p4 == 6011 or 644 <= p3 <= 649 or p2 == 65 or 622126 <= p6 <= 622925) and 16 <= n <= 19:
        return True                                            # Discover
    if (300 <= p3 <= 305 or p2 in (36, 38, 39)) and 14 <= n <= 19:
        return True                                            # Diners Club
    if 3528 <= p4 <= 3589 and 16 <= n <= 19:
        return True                                            # JCB
    if p2 == 62 and 16 <= n <= 19:
        return True                                            # UnionPay
    if (p2 == 50 or 56 <= p2 <= 69) and 12 <= n <= 19:
        return True                                            # Maestro
    return False


def routing_ok(d: str) -> bool:
    if len(d) != 9 or d == "000000000":
        return False
    w = [int(c) for c in d]
    return (3 * (w[0] + w[3] + w[6]) + 7 * (w[1] + w[4] + w[7]) + (w[2] + w[5] + w[8])) % 10 == 0


def iban_ok(s: str) -> bool:
    s = s.replace(" ", "").upper()
    if not 15 <= len(s) <= 34 or not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]+", s):
        return False
    r = s[4:] + s[:4]
    num = "".join(str(int(c, 36)) for c in r)
    return int(num) % 97 == 1


def ssn_ok(area: str, group: str, serial: str) -> bool:
    a = int(area)
    return not (a == 0 or a == 666 or a >= 900 or int(group) == 0 or int(serial) == 0)


_DIGIT_RUN = re.compile(r"(?<![\w.,/])\d+(?:[ -]\d+)*(?![\w/]|[.,]\d)")


def _group_windows(text: str, lo: int, hi: int):
    """(start, digits) for every run of whole digit groups (joined by single spaces or dashes) of lo..hi digits."""
    for m in _DIGIT_RUN.finditer(text):
        groups = [(g.start() + m.start(), g.group()) for g in re.finditer(r"\d+", m.group())]
        for i in range(len(groups)):
            digits = ""
            for j in range(i, len(groups)):
                digits += groups[j][1]
                if len(digits) > hi:
                    break
                if len(digits) >= lo:
                    yield groups[i][0], digits


def _near(text: str, pos: int, wording: re.Pattern, words: int = 6) -> bool:
    before = text[max(0, pos - 160):pos].split()[-words:]     # bounded slices: linear on hostile input
    after = text[pos:pos + 200].split()[:words + 1]
    window = _words(" ".join(before + after))
    return bool(wording.search(window))


def _has_card(text: str) -> bool:
    for start, d in _group_windows(text, 13, 19):
        if _PHONE_WORDS.search(text[max(0, start - 20):start]):
            continue
        if _card_network(d) and luhn_ok(d):
            return True
    return False


_ROUTING_WORDS = re.compile(r"\brouting\b|\baba\b|\brtn\b")
_ACCOUNT_WORDS = re.compile(r"\baccount number\b|\bacct\b|\baccount\b(?= ?(?:#|no\b|num))|\bchecking\b|\bsavings\b")
_SSN_WORDS = re.compile(r"\bssn\b|\bsocial security\b")


def _has_routing(text: str) -> bool:
    for start, d in _group_windows(text, 9, 9):
        if routing_ok(d) and _near(text, start, _ROUTING_WORDS):
            return True
    return False


def _account_text(text: str) -> str:
    return re.sub(r"account\s*#", "account no ", text, flags=re.I)


def _has_account(text: str) -> bool:
    t = _account_text(text)
    for start, d in _group_windows(t, 6, 17):
        if _near(t, start, _ACCOUNT_WORDS):
            return True
    for m in re.finditer(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,4})?\b", _fold(text).upper()):
        if iban_ok(m.group()):
            return True
    return False


def _has_ssn(text: str) -> bool:
    for m in re.finditer(r"(?<![\d-])(\d{3})[- ](\d{2})[- ](\d{4})(?![\d-])", text):
        if ssn_ok(*m.groups()):
            return True
    for start, d in _group_windows(text, 9, 9):
        if ssn_ok(d[:3], d[3:5], d[5:]) and _near(text, start, _SSN_WORDS):
            return True
    return False


_YEAR = re.compile(r"^(?:19|20)\d\d$")
_NUM_CODE = re.compile(r"(?<![\w/:.,-])(\d{3}[ -]\d{3}|\d{4,8})(?![\w/:-]|[.,]\d)")
_ALNUM_CODE = re.compile(r"(?<![\w/:.-])([A-Z0-9]{1,4}-?[A-Z0-9]{3,8})(?![\w/:-])")


def _has_one_time_code(text: str) -> bool:
    for sentence in re.split(r"(?<=[.!?])\s+", _fold(text)):
        if not _CODE_WORDING.search(_words(sentence)):
            continue
        for m in _NUM_CODE.finditer(sentence):
            v = m.group(1)
            if len(v) == 4 and _YEAR.match(v):
                continue
            return True
        for m in _ALNUM_CODE.finditer(sentence):
            v = m.group(1)
            core = v.replace("-", "")
            if len(core) < 5 or sum(c.isdigit() for c in core) < 2 or not re.search(r"[A-Z]", core):
                continue
            if re.fullmatch(r"\d{1,2}(?:AM|PM)", core) or re.fullmatch(r"W-?2|1099[A-Z]*", v):
                continue
            return True
    return False


def _has_alert(text: str) -> bool:
    w = _words(text)
    return any(p.search(w) for p in _ALERT)


# ---------------------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------------------
def sender_domain_matches(addr: str) -> bool:
    domain = (addr or "").rpartition("@")[2].strip().lower().rstrip(".")
    if not domain:
        return False
    for d in SENDER_DOMAINS:
        if domain == d or domain.endswith("." + d):
            return True
    for label in domain.split("."):
        if label == "mychart" or "mychart" in label.split("-"):
            return True
    return False


def subject_matches(subject: str) -> bool:
    w = _words(subject)
    return any(p.search(w) for p in _SUBJECT)


def _body_check(texts: Iterable[str]) -> Optional[str]:
    parts: List[str] = [t for t in texts if t]
    if not parts:
        return None
    text = "\n".join(parts)
    if _has_one_time_code(text):
        return "one-time-code"
    flat = _fold(text)
    if _has_card(flat):
        return "card-number"
    if _has_routing(flat):
        return "bank-routing"
    if _has_account(flat):
        return "bank-account"
    if _has_ssn(flat):
        return "ssn"
    if _has_alert(text):
        return "reset-or-login-alert"
    return None


def scan_text(text) -> Optional[str]:
    """G17: the body checks on outgoing text. Sender-domain and subject-phrase are for inbound
    mail only, so a digest line naming a skipped bank sender can still leave."""
    if not text:
        return None
    return _body_check([str(text)])


def scan_message(msg) -> Optional[str]:
    """Screen 5: the first check (in CHECKS order) that matches anywhere in the decoded message."""
    if sender_domain_matches(msg.from_addr):
        return "sender-domain"
    if subject_matches(msg.subject or ""):
        return "subject-phrase"
    texts = [msg.subject or "", msg.text_plain or "", msg.visible_text or "", msg.hidden_text or ""]
    if msg.html and msg.text_plain is not None:
        # the HTML part is read too when a plain part exists
        html_text, hidden, _, _ = mail.html_to_text(msg.html)
        texts.extend([html_text, hidden or ""])
    texts.append(msg.quoted_text or "")
    return _body_check(texts)
