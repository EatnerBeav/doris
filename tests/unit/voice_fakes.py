"""Fakes for the voice modules' tests (model, templates, compose, holds, digest).

Other builders' modules (zones, sensitive) may be half-written while we work, so the voice
tests never import them: they use these fakes instead.
"""
import datetime as dt
import re

from doris.shapes import Message, UTC


class FakeZones:
    """December-only fixed offsets (no daylight time), the same API as zones.py."""

    OFFSETS = {"Pacific": -8, "Mountain": -7, "Central": -6, "Eastern": -5, "UTC": 0, "UK": 0}

    def _offset(self, zone_id):
        if zone_id in self.OFFSETS:
            return dt.timedelta(hours=self.OFFSETS[zone_id])
        m = re.match(r"^UTC([+-])(\d\d):(\d\d)$", zone_id)
        if not m:
            raise ValueError("unknown zone %r" % (zone_id,))
        sign = 1 if m.group(1) == "+" else -1
        return sign * dt.timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))

    def to_local(self, utc, zone_id):
        return (utc.astimezone(UTC) + self._offset(zone_id)).replace(tzinfo=None)

    def display(self, zone_id):
        return zone_id


def utc(y, mo, d, h=0, mi=0):
    return dt.datetime(y, mo, d, h, mi, tzinfo=UTC)


def make_msg(**kw):
    base = dict(
        key="msg-1@northwind.example", raw_sha256="0" * 64, raw_size=100,
        message_id="<msg-1@northwind.example>", from_addr="priya@northwind.example",
        from_name="Priya Raman", reply_to=[], to=["whizz@fastmail.com"], cc=[],
        subject="Quick call this week?", date_utc=utc(2026, 12, 1, 16, 2),
        in_reply_to=[], references=[], headers={}, text_plain="", html=None,
        visible_text="", own_text="", below_closing="", quoted_text="", attachments=[],
        is_report=False,
    )
    base.update(kw)
    if "visible_text" not in kw:
        base["visible_text"] = base["own_text"]
    return Message(**base)
