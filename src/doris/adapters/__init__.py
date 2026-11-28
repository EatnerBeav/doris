"""Adapters: the only code that touches her mail and her calendar. Sandbox or live, one interface.

Live, they talk only to her own mail account (IMAP to read, JMAP to send) and the calendar shared
to her (CalDAV). The model call is model.py's, not an adapter's. The run's network guard
(netguard.py) refuses every host besides these and, with a live model, the ones its call needs.
"""
