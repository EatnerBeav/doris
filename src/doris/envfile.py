"""Read a plain KEY=VALUE .env file into a dict, without ever printing or echoing a value.

The format, and nothing more: one KEY=VALUE per line; blank lines and lines starting with #
are skipped; a value may sit in one pair of matching single or double quotes, which are
removed (the inside is kept verbatim). There is no `export`, no interpolation ($X stays $X),
no inline comment and no escape processing. The file is never written to os.environ.

Anything outside the format raises EnvFileError naming the line number (and, for a name set
twice, the name), never a value: the real file holds Doris's app password.
"""

import re

_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_EXPORT = re.compile(r"^export\s")
_LINES = re.compile(r"\r\n|\n|\r")
_QUOTES = ("'", '"')


class EnvFileError(ValueError):
    """The env file cannot be read or is not plain KEY=VALUE. Names lines, never values."""


def load(path) -> dict:
    """Every KEY=VALUE in the file at path, as a dict of strings."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        raise EnvFileError("cannot read the env file (%s)" % type(exc).__name__) from None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise EnvFileError("the env file is not UTF-8 text") from None
    if text.startswith("﻿"):
        text = text[1:]
    out = {}
    for number, line in enumerate(_LINES.split(text), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if _EXPORT.match(stripped):
            raise EnvFileError("line %d: export is not part of the format; write KEY=VALUE" % number)
        name, sep, value = stripped.partition("=")
        name = name.strip()
        if not sep:
            raise EnvFileError("line %d: not KEY=VALUE" % number)
        if not _NAME.match(name):
            raise EnvFileError("line %d: the name is not letters, digits and _" % number)
        if name in out:
            raise EnvFileError("line %d: %s is set twice" % (number, name))
        out[name] = _unquote(value.strip(), number)
    return out


def _unquote(value, number):
    first, last = value[:1], value[-1:]
    if first in _QUOTES or last in _QUOTES:
        if len(value) < 2 or first != last:
            raise EnvFileError("line %d: unbalanced quote" % number)
        return value[1:-1]
    return value
