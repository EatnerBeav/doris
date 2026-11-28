"""Every hard limit lives in this one file (locked call 9). Never in a prompt.

Cap values approved by Shane as proposed, Nov 26, 9 PM Pacific. The hourly and per-run
numbers were checked against the organizers' Trap Pack at kickoff (Nov 27).
"""

import datetime as _dt

# --- Sends and writes (G7) -------------------------------------------------------------
SENDS_PER_HOUR = 30            # sends to prospects in any hour
SENDS_PER_RUN = 100            # sends to prospects in one run
SENDS_PER_THREAD_PER_DAY = 5   # sends on one thread in a day
SENDS_PER_THREAD_EVER = 8      # sends on one thread, ever
OFFERS_PER_THREAD_EVER = 2     # offer records on one thread, ever (the law)
CALENDAR_WRITES_PER_RUN = 40   # calendar writes in one run
HOLD_NOTICES_PER_HOUR = 30     # past this, holds are still recorded and appear on the digest

# --- Model spend (G3) -------------------------------------------------------------------
BUDGET_USD_PER_RUN = 5.00      # reaching it halts the run with cause budget_cap; only the CLI resumes

# --- Oversize (screen 9, ruling 2) --------------------------------------------------------
MAX_TEXT_CHARS = 20000         # decoded text parts; attachments never count and are never opened
MAX_RAW_BYTES = 25 * 1024 * 1024

# --- Bookable interval (G9, rulings 3 and 5) ---------------------------------------------
BUSINESS_DAYS = (0, 1, 2, 3, 4)          # Monday to Friday
BUSINESS_START = _dt.time(9, 0)          # Pacific
BUSINESS_END = _dt.time(17, 0)           # Pacific; a 30-minute slot starts no later than 4:30 PM
MIN_NOTICE = _dt.timedelta(hours=24)
MAX_HORIZON = _dt.timedelta(days=30)
WRITTEN_START_MINUTES = (0, 15, 30, 45)  # a time the sender wrote may start on any quarter hour
OFFERED_START_MINUTES = (0, 30)          # a slot she offers starts on the hour or half hour
DEFAULT_LENGTH_MIN = 30
MIN_LENGTH_MIN = 5
MAX_LENGTH_MIN = 60
SLOTS_PER_OFFER = 3

# The holiday list, approved by Shane Nov 26, 9 PM Pacific. Published in the README.
HOLIDAYS = (
    _dt.date(2026, 11, 26),  # Thanksgiving
    _dt.date(2026, 11, 27),  # Day after Thanksgiving (ours)
    _dt.date(2026, 12, 24),  # Christmas Eve (ours)
    _dt.date(2026, 12, 25),  # Christmas
    _dt.date(2026, 12, 31),  # New Year's Eve (ours)
    _dt.date(2027, 1, 1),    # New Year's Day
    _dt.date(2027, 1, 18),   # Martin Luther King Jr. Day
    _dt.date(2027, 2, 15),   # Presidents' Day
)

# --- Offers (ruling 6) ---------------------------------------------------------------------
OFFER_EXPIRY = _dt.timedelta(days=7)
NUDGE_AFTER_BUSINESS_DAYS = 2            # the follow-up: one nudge (T9) this long after an offer nobody answered
NUDGES_PER_THREAD_EVER = 1               # once per offer and once per thread, never a second (G7)

# --- Calls and retries ("When a call fails") -----------------------------------------------
CALENDAR_TIMEOUT_S = 10.0
CALENDAR_RETRY_WAIT_S = 2.0
MAIL_TIMEOUT_S = 30.0
MODEL_TIMEOUT_S = 60.0
MODEL_RETRY_WAIT_S = 2.0
RULING17_REREAD_WAIT_S = 2.0             # "missing on two reads a short wait apart"
RULING17_UNREAD_IN_A_ROW = 2             # this many bookings in a row that cannot be read (never "missing") end
                                         # the cycle's check: the calendar is not answering; one alone does not
SENT_LOOK_WAIT_S = 5.0                   # live: between her two looks in Sent after a send (ruling 8)

# --- A run until stopped (`python -m doris run`) ---------------------------------------------
# Her inbox unreadable never ends the run (decision 89): each failed read is an error line, she
# sends nothing until a read goes (fail closed), and her next cycle reads again. From this many
# failures in a row (an hour of ten-minute cycles) she tries every MAIL_READ_RETRY_S instead, never
# more often than her cycle, until a read goes or a stop or a halt ends the run. One good read starts
# the count again. A finite run (demo, traps) ends on its one failed read.
MAIL_READ_FAILURES_IN_A_ROW = 6
MAIL_READ_RETRY_S = 600.0

# --- A live run's start (decision 76) -------------------------------------------------------
SMOKE_RECORD_MAX_AGE = _dt.timedelta(hours=24)   # how long after a smoke wrote its sent record a live run takes it

# --- Hold notices (S1) ---------------------------------------------------------------------
HOLD_QUOTE_CHARS = 400

# --- Model prices (USD per million tokens) -------------------------------------------------
# Read from Anthropic's pricing page at kickoff, never from memory. Each model call's receipt
# carries its token counts and the cost worked out from these constants.
# Read Nov 27 2026 from https://platform.claude.com/docs/en/about-claude/pricing (base input and
# output, global routing, no batch or cache discount). Haiku 5.5's price is for prompts up to
# 100,000 tokens; hers are a few thousand at most (MAX_TEXT_CHARS).
MODEL_PRICES_USD_PER_MTOK = {
    "claude-sonnet-5-5": (2.0, 10.0),     # the runtime model (decision 19)
    "claude-opus-5-5": (4.0, 20.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-fable-5-2": (8.0, 40.0),
}
