"""state/live/smoke-sent.json: the smoke's sent record. No live run starts without it (decision 76).

`python -m doris smoke --send-to` writes it, and only when it found its own test message in her
Sent folder: proof that mail she hands to SMTP gets its copy in Sent, which is how a live run
confirms each send. `python -m doris run --live` reads it before anything connects, on the
command line and again in the worker under the live lock. Both sides use this module, so they
agree on one file and one format: one line of ASCII JSON, keys sorted,

    {"look": 1, "sent_copy_found": true, "ts": "2026-11-28T15:04:05Z", "waited_s": 0.4}

holding exactly these four keys and nothing else (no address, ID or setting):
  ts               when the smoke wrote it: UTC, to the second (TS_FORMAT)
  sent_copy_found  true
  look             1 or 2: the look in Sent that found the copy first
  waited_s         seconds from the end of the send to the end of that look, to 0.1 s

A live run takes the record only as the smoke writes it, byte for byte. No file, a file that
cannot be read, anything else in it, a key missing or extra, or a value of another kind: the run
is refused, with one phrase saying which (problem). The smoke never writes what a run refuses.
"""

import datetime as _dt
import json
import math
import os
import re
import tempfile

UTC = _dt.timezone.utc
NAME = "smoke-sent.json"                # in the live state folder, paths.state_dir("live")
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
KEYS = ("look", "sent_copy_found", "ts", "waited_s")
MAX_BYTES = 256                         # a record is under 100 bytes; a longer file is not one
MISSING = "no smoke check has found her sent copy in Sent yet"
_ITS = "the smoke check's record (%s)" % NAME
_TS = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")


def path(state_dir):
    """The record's file in a state folder: the live one, paths.state_dir("live")."""
    return os.path.join(state_dir, NAME)


def encode(look, waited_s, now):
    """The record's bytes: the copy found by look 1 or 2, waited_s seconds after the send, written
    at now (an aware time). ValueError, and nothing to write, for a record a run would refuse as
    malformed."""
    data = _layout({"ts": now.astimezone(UTC).strftime(TS_FORMAT), "sent_copy_found": True,
                    "look": look, "waited_s": round(float(waited_s), 1)})
    _parse(data)
    return data


def write(file_path, look, waited_s, now):
    """Write the record to file_path whole or not at all: a temp file beside it, flushed to disk,
    then os.replace. On any failure no temp file is left and the error is raised."""
    data = encode(look, waited_s, now)
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(file_path) + ".", suffix=".tmp",
                               dir=os.path.dirname(file_path))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, file_path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def problem(state_dir):
    """None when the state folder holds the record as the smoke writes it; else why not, as one
    phrase that shows no value from the file."""
    try:
        with open(path(state_dir), "rb") as fh:
            data = fh.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return MISSING
    except OSError as exc:
        return "%s cannot be read: %s" % (_ITS, type(exc).__name__)
    try:
        _parse(data)
    except ValueError as exc:
        return "%s is not as the smoke writes it: %s" % (_ITS, exc)
    return None


def _layout(record):
    return json.dumps(record, sort_keys=True).encode("ascii") + b"\n"


def _parse(data):
    """The record's time (aware UTC) when data is a record exactly as the smoke writes it; else
    ValueError naming what is wrong, never a value."""
    if len(data) > MAX_BYTES:
        raise ValueError("longer than %d bytes" % MAX_BYTES)
    try:
        record = json.loads(data.decode("ascii"))
    except ValueError:                                   # not ASCII, or not JSON
        raise ValueError("not one JSON object in ASCII") from None
    if not isinstance(record, dict):
        raise ValueError("not one JSON object in ASCII")
    if sorted(record) != list(KEYS):
        raise ValueError("its keys are not exactly %s" % ", ".join(KEYS))
    when = _when(record["ts"])
    if when is None:
        raise ValueError("ts is not a UTC time to the second")
    if record["sent_copy_found"] is not True:
        raise ValueError("sent_copy_found is not true")
    if type(record["look"]) is not int or record["look"] not in (1, 2):
        raise ValueError("look is not 1 or 2")
    waited = record["waited_s"]
    if type(waited) is not float or not math.isfinite(waited) or waited < 0 or round(waited, 1) != waited:
        raise ValueError("waited_s is not seconds to 0.1")
    if data != _layout(record):
        raise ValueError("not laid out as the smoke lays it out (one line, keys sorted)")
    return when


def _when(ts):
    if not isinstance(ts, str) or not _TS.fullmatch(ts):
        return None
    try:
        return _dt.datetime.strptime(ts, TS_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None
