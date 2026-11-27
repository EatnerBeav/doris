"""The network guard: for the life of a run, every outbound connect is checked against a list.

`install(allowed_hosts)` patches `socket.socket.connect`, `socket.socket.connect_ex`,
`socket.create_connection` and `socket.getaddrinfo` (so a refused name is never even looked
up), records every attempt in `Guard.attempts`, and raises `NetworkRefused` for any host not
on the list. `Guard.uninstall()` puts every original back. Sandbox, traps and redteam runs
with a stub or replayed model allow no host at all (invariant I4).

An address the guard cannot read (a Unix socket path, an unknown family) is refused: fail closed.
Guards nest; a connect goes out only when every installed guard allows it.
"""

import socket
import threading
from typing import Any, Dict, Iterable, List

_STACK = []             # installed guards, innermost last; every one must allow a host
_SAVED = None           # the originals, put back when the last guard is uninstalled
_LOCK = threading.RLock()


class NetworkRefused(ConnectionRefusedError):
    """A connect to a host outside the run's allowed list. Recorded, never let out."""


def _norm(host):
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    return str(host).strip().rstrip(".").lower()


def _split(address):
    """(host, port) from a socket address; (repr, None) when it is not an internet address."""
    if isinstance(address, tuple) and len(address) >= 2:
        return _norm(address[0]), address[1]
    return "unreadable:" + repr(address)[:80], None


class Guard:
    def __init__(self, allowed_hosts):
        self.allowed = frozenset(_norm(h) for h in allowed_hosts)
        self.attempts = []           # type: List[Dict[str, Any]]
        self._resolved = set()       # IP addresses looked up from an allowed name
        self._lock = threading.Lock()

    # -- queries ------------------------------------------------------------------------
    def allows(self, host):
        h = _norm(host)
        return h in self.allowed or h in self._resolved

    def refused(self):
        return [a for a in self.attempts if not a["allowed"]]

    def refused_count(self):
        return len(self.refused())

    def _record(self, call, host, port, ok):
        with self._lock:
            self.attempts.append({"call": call, "host": host, "port": port, "allowed": ok})

    # -- uninstall ----------------------------------------------------------------------
    def uninstall(self):
        with _LOCK:
            if self in _STACK:
                _STACK.remove(self)
                if not _STACK:
                    _restore()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.uninstall()
        return False


def _check(call, host, port):
    """Every installed guard records the attempt; it goes out only if every one allows it."""
    guards = list(_STACK)
    ok = bool(guards) and all(g.allows(host) for g in guards)
    for g in guards:
        g._record(call, host, port, ok)
    if not ok:
        raise NetworkRefused("network guard: %s to %s refused (not an allowed host)" % (call, host))


def _patch():
    global _SAVED
    orig_connect = socket.socket.connect
    orig_connect_ex = socket.socket.connect_ex
    orig_create = socket.create_connection
    orig_getaddrinfo = socket.getaddrinfo
    _SAVED = {
        "connect_in_dict": "connect" in socket.socket.__dict__,
        "connect_ex_in_dict": "connect_ex" in socket.socket.__dict__,
        "connect": socket.socket.__dict__.get("connect"),
        "connect_ex": socket.socket.__dict__.get("connect_ex"),
        "create_connection": orig_create,
        "getaddrinfo": orig_getaddrinfo,
    }

    def connect(sock, address):
        host, port = _split(address)
        _check("connect", host, port)
        return orig_connect(sock, address)

    def connect_ex(sock, address):
        host, port = _split(address)
        _check("connect_ex", host, port)
        return orig_connect_ex(sock, address)

    def create_connection(address, *args, **kwargs):
        host, port = _split(address)
        _check("create_connection", host, port)
        return orig_create(address, *args, **kwargs)

    def getaddrinfo(host, port, *args, **kwargs):
        name = _norm(host) if host is not None else ""
        guards = list(_STACK)
        if not guards or not all(g.allows(name) for g in guards):
            _check("getaddrinfo", name, port)
        result = orig_getaddrinfo(host, port, *args, **kwargs)
        for entry in result:
            sockaddr = entry[4]
            if isinstance(sockaddr, tuple) and sockaddr:
                for g in guards:
                    g._resolved.add(_norm(sockaddr[0]))
        return result

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.create_connection = create_connection
    socket.getaddrinfo = getaddrinfo


def _restore():
    global _SAVED
    saved, _SAVED = _SAVED, None
    if saved is None:
        return
    for name in ("connect", "connect_ex"):
        if saved[name + "_in_dict"]:
            setattr(socket.socket, name, saved[name])
        elif name in socket.socket.__dict__:
            delattr(socket.socket, name)
    socket.create_connection = saved["create_connection"]
    socket.getaddrinfo = saved["getaddrinfo"]


def install(allowed_hosts=()):
    # type: (Iterable[str]) -> Guard
    """Patch socket connects for this run and return its Guard.

    Guards nest (the traps runner holds one while loop.run may install its own): a connect goes
    out only when every installed guard allows the host, so an inner guard can only narrow, and
    every guard records every attempt. The originals come back when the last guard is uninstalled."""
    with _LOCK:
        guard = Guard(tuple(allowed_hosts))
        if not _STACK:
            _patch()
        _STACK.append(guard)
        return guard


def active():
    """The innermost installed guard, or None."""
    with _LOCK:
        return _STACK[-1] if _STACK else None
