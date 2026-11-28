"""The adapter interface. Sandbox and live both implement it, so one code path serves both.

Beyond it the loop uses three things only one side has, each looked for before it is used:
next_arrival() on the sandbox mail (the demo's advancing clock), arm() and disarm() on the
sandbox adapters (staged faults), and smoke_left_out on the live mail (the smoke check's own
requests a fetch left out).

Rules every implementation keeps:
- A failure raises (MailError, CalendarError, CalendarTimeout, SendUnknown). It never returns
  an empty list, "free" or "sent" when it does not know.
- The mail adapter has no delete, expunge, move or forward. Filing is a keyword on her own copy.
- The calendar adapter writes create-only, reads times and status only (never a title), and
  deletes only her own events by booking ID.
- The signature is appended by the adapter (compose.py), never by the model or a template.
"""

from typing import List, Optional

from ..shapes import Event, InboundItem


class MailAdapter:
    def fetch_new(self) -> List[InboundItem]:
        """Every message not yet fetched, oldest first. Raises MailError on any failure."""
        raise NotImplementedError

    def send(self, raw: bytes, envelope_to: List[str], message_id: str, template: Optional[str] = None) -> None:
        """Hand one composed message to the server. envelope_to holds the To and the BCC; template
        names the file in the sandbox outbox and is ignored live.
        Raises MailError when refused, SendUnknown when the outcome is unknown."""
        raise NotImplementedError

    def find_in_sent(self, message_id: str) -> bool:
        """True when her Sent folder holds a message with this Message-ID. Raises MailError on failure."""
        raise NotImplementedError

    def file(self, server_id: str, keyword: str) -> None:
        """Mark her own copy with a keyword (e.g. doris-filed, doris-held). Never deletes or moves."""
        raise NotImplementedError


class CalendarAdapter:
    def query(self, start_utc, end_utc) -> List[Event]:
        """Every event overlapping [start, end), repeats expanded. Raises CalendarError/CalendarTimeout."""
        raise NotImplementedError

    def create_only(self, booking_id: str, ics: bytes) -> None:
        """Create her event under its booking ID (the UID and the resource name).
        Raises AlreadyExists if that ID is already there, CalendarTimeout if the result is unknown."""
        raise NotImplementedError

    def get(self, booking_id: str) -> Optional[Event]:
        """Her event by booking ID. None means confirmed absent. Raises on failure (never None)."""
        raise NotImplementedError

    def delete(self, booking_id: str) -> None:
        """Delete her own event by booking ID. Refuses any ID that is not one of hers."""
        raise NotImplementedError
