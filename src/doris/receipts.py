"""Receipts: one hash-chained JSONL line for every step (owner: state).

Each line is canonical JSON bytes plus b"\\n", appended in binary mode, flushed and fsynced
before write() returns, so the file is byte-identical on Linux, macOS and Windows.

    line = {"ts","run_id","mode","seq","kind","subject","payload","why"?,"prev","hash"}
    hash = sha256(prev.encode() + canonical(line without "hash")).hexdigest()

The first prev is "genesis" and seq starts at 1.
"""

import datetime as _dt
import hashlib
import json
import os
import threading
from collections import namedtuple
from typing import List, Optional

from .paths import MODES

GENESIS = "genesis"
KINDS = ("observed", "classified", "decided", "checked", "called", "result", "held", "halted", "error", "summary")
WHY_KINDS = ("decided", "checked", "held", "halted")
CALLS = ("send", "calendar_query", "calendar_create", "calendar_get", "calendar_delete", "model",
         "mail_fetch", "sent_lookup", "mail_file")

_UTC = _dt.timezone.utc


class ChainError(ValueError):
    """The receipt chain on disk is broken: nothing new is written past it."""


def canonical(obj) -> bytes:
    """Sorted keys, no spaces, ASCII only. NaN and infinities are refused (not JSON)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("ascii")


def iso(dt) -> str:
    """An aware datetime as "YYYY-MM-DDTHH:MM:SSZ" in UTC. A naive datetime is refused."""
    if not isinstance(dt, _dt.datetime) or dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("a receipt time must be an aware datetime, not %r" % (dt,))
    return dt.astimezone(_UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def jsonable(obj):
    """A JSON-safe copy: aware datetimes become iso(), dates "YYYY-MM-DD", tuples lists.
    Anything else that JSON cannot carry exactly (sets, bytes, objects, non-string keys,
    naive datetimes, NaN) raises ValueError. Objects are never dumped field by field, so a
    whole Message can never slip into a receipt by accident."""
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if not isinstance(key, str):
                raise ValueError("receipt keys must be strings, not %r" % (key,))
            out[key] = jsonable(value)
        return out
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, _dt.datetime):
        return iso(obj)
    if isinstance(obj, _dt.date):
        return obj.isoformat()
    if obj is None or isinstance(obj, (str, bool, int)):
        return obj
    if isinstance(obj, float):
        if obj != obj or obj in (float("inf"), float("-inf")):
            raise ValueError("a receipt number must be finite")
        return obj
    raise ValueError("not JSON-safe for a receipt: %s" % type(obj).__name__)


def _wall_clock():
    return _dt.datetime.now(_UTC)


VerifyResult = namedtuple("VerifyResult", "ok lines first_bad_seq reason")
VerifyResult.__doc__ = """ok; lines = whole lines that verified ahead of the first bad one (all of them when ok);
first_bad_seq = the seq the first bad line should have held (None when ok, or when there is no file); reason
in plain words. After a re-save with CRLF line endings, ok is never True: when a line is wrong beyond its
ending, it is the one named and lines counts those ahead of it whose content verified with the CR set aside;
when only endings changed, the first CRLF line is named and lines counts the byte-exact lines ahead of it."""

_FIELDS = {"ts": str, "run_id": str, "mode": str, "seq": int, "kind": str, "subject": str,
           "payload": dict, "prev": str, "hash": str}


def _parses(raw):
    try:
        return isinstance(json.loads(raw.decode("ascii")), dict)
    except (ValueError, RecursionError):
        return False


def _split(data):
    """File bytes -> (whole lines without their b"\\n", torn tail bytes or b"").
    The torn tail is a last line with no newline, or a last line that is not a JSON object."""
    parts = data.split(b"\n")
    torn = parts.pop()
    if not torn and parts and not _parses(parts[-1]):
        torn = parts.pop() + b"\n"
    return parts, torn


def _recompute(whole):
    """Recompute the chain over whole lines, byte for byte -> (VerifyResult, parsed lines that verified)."""
    prev = GENESIS
    good = []
    for raw in whole:
        expected = len(good) + 1

        def bad(reason):
            return VerifyResult(False, len(good), expected, "broken at seq %d: %s" % (expected, reason)), good
        try:
            obj = json.loads(raw.decode("ascii"))
        except (ValueError, RecursionError):
            return bad("the line is not valid JSON")
        if not isinstance(obj, dict):
            return bad("the line is not a JSON object")
        if set(obj) - {"why"} != set(_FIELDS) or any(type(obj[k]) is not t for k, t in _FIELDS.items()):
            return bad("the line's fields are not the receipt fields")
        try:
            form = canonical(obj)
        except ValueError:                 # NaN, Infinity, 1e999: json.loads takes them, JSON does not
            return bad("the line holds a number JSON cannot carry (NaN or an infinity)")
        if form != raw:
            return bad("the bytes are not the canonical form of the line")
        if obj["seq"] != expected:
            return bad("seq %d found where seq %d belongs" % (obj["seq"], expected))
        if obj["prev"] != prev:
            return bad("prev does not match the hash of the line before")
        unhashed = dict(obj)
        stated = unhashed.pop("hash")
        if hashlib.sha256(prev.encode("ascii") + canonical(unhashed)).hexdigest() != stated:
            return bad("the hash does not match the line")
        if obj["kind"] not in KINDS or (obj["kind"] in WHY_KINDS and "why" not in obj):
            return bad("not a line her writer writes (kind or why)")
        good.append(obj)
        prev = stated
    return VerifyResult(True, len(good), None, "ok"), good


def _crlf(whole):
    return "%d of %d lines now end CRLF, not LF" % (sum(raw.endswith(b"\r") for raw in whole), len(whole))


def _also_crlf(result, whole):
    """The same verdict, adding that the line endings were changed as well (kept short: replay shows 200
    characters of a reason)."""
    return result._replace(reason="%s; also, %s" % (result.reason, _crlf(whole)))


def _examine(whole):
    """_check, plus what verify needs to go on to the file's end -> (VerifyResult, lines that verified, content).

    Her lines end LF and hold no CR, so a file re-saved with CRLF line endings (PowerShell's Set-Content,
    some editors) is not the file she wrote and never verifies. But such a re-save touches every line at
    once, and "broken at seq 1" would hide the line that was really edited. So when the first bad line ends
    with a CR, the chain is recomputed with one CR set aside at the end of each line, and the verdict names
    the first line that is wrong beyond its ending. When no line is, the verdict names the first CRLF line
    and says the endings were changed; only then is content every line, parsed (else None)."""
    result, good = _recompute(whole)
    if result.ok or not whole[len(good)].endswith(b"\r"):
        return result, good, None
    under, content = _recompute([raw[:-1] if raw.endswith(b"\r") else raw for raw in whole])
    if not under.ok:
        return _also_crlf(under, whole), content, None
    seq = len(good) + 1
    return VerifyResult(False, len(good), seq,
                        "broken at seq %d: the line endings were changed: %s (PowerShell and some editors "
                        "re-save this way); every line still matches its hash without them"
                        % (seq, _crlf(whole))), good, content


def _check(whole):
    """Recompute the chain over whole lines -> (VerifyResult, parsed lines that verified). Never ok for a
    line that is not byte for byte hers; a CRLF re-save is seen through only to name the right line."""
    return _examine(whole)[:2]


CLOSING_KINDS = ("summary", "halted")


def verify(path, finished=False) -> VerifyResult:
    """Recompute the whole chain. Names the first bad seq for a changed byte, a deleted, reordered or
    duplicated line, a seq gap and a torn tail. finished=True (a run that has ended: the verify
    command) also needs the last line to be its summary or halted line (not a halted line that
    promises a summary after it), so whole lines cut from the end are named too; without it, a chain
    still being written passes. A file re-saved with CRLF line endings never passes, and the line named is
    the one that is wrong beyond its ending (_examine). Reads only; never raises on bad content."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        return VerifyResult(False, 0, None, "cannot read the receipt file: %s" % (exc.__class__.__name__,))
    whole, torn = _split(data)
    result, good, content = _examine(whole)
    if content is not None:                # only the line endings are wrong so far: is the file's end whole?
        end = _end(content, torn, finished)
        return _also_crlf(end, whole) if end else result
    if not result.ok:
        return result
    end = _end(good, torn, finished)
    if end:
        return end
    notes = ["torn tail moved aside at seq %d" % line["seq"] for line in good
             if line["kind"] == "error" and line["subject"] == "torn-tail"]
    return VerifyResult(True, len(good), None, "; ".join(["ok"] + notes))


def _end(good, torn, finished):
    """What is wrong at the end of a file whose whole lines all verified: a VerifyResult, or None."""
    if torn:
        return VerifyResult(False, len(good), len(good) + 1, "torn tail: %d bytes with no whole line" % len(torn))
    if finished and (not good or good[-1]["kind"] not in CLOSING_KINDS or _summary_follows(good[-1])):
        last = "%s line" % good[-1]["kind"] if good else "no line"
        return VerifyResult(False, len(good), len(good) + 1,
                            "no closing line: it ends at seq %d with %s, not a summary or halted line, so lines "
                            "may be cut from the end (or the run never finished)" % (len(good), last))
    return None


def _summary_follows(line):
    """A halted line a run wrote itself promises its summary next (the kill switch's parent writes
    the last line instead): ending on it means the summary was cut off."""
    return line["kind"] == "halted" and isinstance(line.get("payload"), dict) and \
        line["payload"].get("summary") == "follows"


def _check_why(why):
    if not isinstance(why, dict) or set(why) != {"code", "text"}:
        raise ValueError("a why is exactly {code, text}, got %r" % (why,))
    for key in ("code", "text"):
        if not isinstance(why[key], str) or not why[key]:
            raise ValueError("why %s must be a non-empty string" % key)


def _fsync_dir(folder):
    """Make a new directory entry durable (POSIX only; Windows cannot open a folder)."""
    if os.name != "posix":
        return
    try:
        fd = os.open(folder or ".", os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


class ReceiptWriter:
    """Appends receipt lines to one file, continuing the chain already there."""

    def __init__(self, path, run_id, mode, clock=None):
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id must be a non-empty string")
        if mode not in MODES:
            raise ValueError("unknown mode %r" % (mode,))
        self._path = os.fspath(path)
        self._run_id = run_id
        self._mode = mode
        self._clock = clock or _wall_clock
        self._lock = threading.Lock()
        self._seq = 0
        self._last = GENESIS
        self._size = 0
        self._called = set()
        self.torn = None
        with self._lock:
            self._open()

    @property
    def seq(self) -> int:
        """The seq of the last line in the chain (0 when the file has none)."""
        return self._seq

    @property
    def last_hash(self) -> str:
        """The hash of the last line ("genesis" when the file has none)."""
        return self._last

    @property
    def path(self) -> str:
        return self._path

    def _open(self):
        """Create the file, or continue its chain from the last whole line. A broken chain raises
        ChainError and nothing is touched; a torn tail is moved aside, then recorded in an error line."""
        folder = os.path.dirname(self._path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        if not os.path.exists(self._path):
            with open(self._path, "ab") as fh:
                fh.flush()
                os.fsync(fh.fileno())
            _fsync_dir(folder)
        with open(self._path, "rb") as fh:
            data = fh.read()
        whole, torn = _split(data)
        result, good = _check(whole)
        if not result.ok:
            raise ChainError("receipt chain %s: %s; nothing written past it" % (self._path, result.reason))
        self._seq = len(good)
        self._last = good[-1]["hash"] if good else GENESIS
        self._called = {line["seq"] for line in good if line["kind"] == "called"}
        self._size = len(data) - len(torn)
        if torn:
            self._move_aside(torn)

    def _move_aside(self, torn):
        """Copy the torn bytes to <path>.torn-<n> (a new file, fsynced), cut them off the end of the
        receipt file (the whole lines are never rewritten), then chain an error line recording them."""
        n = 1
        while True:
            dest = "%s.torn-%d" % (self._path, n)
            try:
                fh = open(dest, "xb")
            except FileExistsError:
                n += 1
                continue
            with fh:
                fh.write(torn)
                fh.flush()
                os.fsync(fh.fileno())
            break
        _fsync_dir(os.path.dirname(self._path))
        with open(self._path, "r+b") as fh:
            fh.truncate(self._size)
            fh.flush()
            os.fsync(fh.fileno())
        record = {"torn_bytes": len(torn), "torn_sha256": hashlib.sha256(torn).hexdigest(),
                  "moved_to": os.path.basename(dest), "after_seq": self._seq}
        self.torn = record
        self._append("error", dict(record), "torn-tail", None)

    def _resync(self):
        """Another writer (a killed child, say) changed the file since this one last wrote: continue from it."""
        try:
            size = os.path.getsize(self._path)
        except OSError:
            raise ChainError("receipt file %s is gone; nothing written" % (self._path,))
        if size != self._size:
            self._open()

    def write(self, kind, payload, subject="", why=None) -> dict:
        """Append one line; returns it as written. Refuses (ValueError) a bad line and writes nothing."""
        if kind not in KINDS:
            raise ValueError("unknown receipt kind %r" % (kind,))
        if not isinstance(subject, str):
            raise ValueError("subject must be a string")
        if not isinstance(payload, dict):
            raise ValueError("payload must be a dict")
        if why is not None or kind in WHY_KINDS:
            _check_why(why)
        body = jsonable(payload)
        if kind == "called":
            if body.get("call") not in CALLS:
                raise ValueError("a called line names its call, one of %s" % (", ".join(CALLS),))
            if "target" not in body or not (body["target"] is None or isinstance(body["target"], str)):
                raise ValueError("a called line carries a target (template id, booking ID or model id)")
            if body["call"] == "send" and set(body) & {"body", "body_text", "raw"}:
                raise ValueError("a sent body goes in a receipt only as body_sha256")
        if kind == "result":
            if "call_seq" not in body:
                raise ValueError("a result line carries call_seq (the seq of the called line it answers, or None)")
            call_seq = body["call_seq"]
            if call_seq is not None and type(call_seq) is not int:
                raise ValueError("call_seq must be an int or None")
        with self._lock:
            self._resync()
            if kind == "result" and body["call_seq"] is not None and body["call_seq"] not in self._called:
                raise ValueError("call_seq %r names no called line in this chain" % (body["call_seq"],))
            return self._append(kind, body, subject, why)

    def _append(self, kind, body, subject, why):
        line = {"ts": iso(self._clock()), "run_id": self._run_id, "mode": self._mode, "seq": self._seq + 1,
                "kind": kind, "subject": subject, "payload": body, "prev": self._last}
        if why is not None:
            line["why"] = dict(why)
        unhashed = canonical(line)
        if canonical(json.loads(unhashed.decode("ascii"))) != unhashed:
            raise ValueError("payload does not survive a JSON round trip byte for byte")
        line["hash"] = hashlib.sha256(self._last.encode("ascii") + unhashed).hexdigest()
        raw = canonical(line) + b"\n"
        with open(self._path, "ab") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        self._size += len(raw)
        self._seq = line["seq"]
        self._last = line["hash"]
        if kind == "called":
            self._called.add(self._seq)
        return json.loads(raw.decode("ascii"))


def read_lines(path) -> List[dict]:
    """Every whole line, parsed. A torn tail (no newline, or a bad-JSON last line) is left out;
    a bad line anywhere else raises ValueError. A missing file raises (a failed read is never empty)."""
    with open(path, "rb") as fh:
        data = fh.read()
    whole, _torn = _split(data)
    out = []
    for index, raw in enumerate(whole):
        try:
            obj = json.loads(raw.decode("ascii"))
        except (ValueError, RecursionError):
            obj = None
        if not isinstance(obj, dict):
            raise ValueError("receipt line %d is not a JSON object" % (index + 1,))
        out.append(obj)
    return out


def unanswered_calls(lines) -> List[dict]:
    """Every called line no result names by call_seq, oldest first, as {seq, call, target}."""
    answered = set()
    for line in lines:
        if line.get("kind") == "result":
            answered.add((line.get("payload") or {}).get("call_seq"))
    out = []
    for line in lines:
        if line.get("kind") == "called" and line.get("seq") not in answered:
            payload = line.get("payload") or {}
            out.append({"seq": line.get("seq"), "call": payload.get("call"), "target": payload.get("target")})
    return out


def in_flight(lines) -> Optional[dict]:
    """The last called line with no result: {seq, call, target}, or None."""
    calls = unanswered_calls(lines)
    return calls[-1] if calls else None
