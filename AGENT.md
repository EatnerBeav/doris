# AGENT.md

Doris is an inbox and calendar agent you can walk away from. Agentware 2026, track T1 (Inbox & Calendar).

Team Whizz: Shane Bornstein and Wolf (Claude), his CTO. Shane makes the calls and narrates; Wolf designs with him and writes the code. Anthropic is not involved in the entry.

Everything below is cited by file and function so you can check it. Anything not built yet says **pending** in plain words. If this page and the code disagree, the code and its receipts are the truth, and this page is wrong.

## The job

She runs the owner's email (forwarded to her) and his calendar while he's gone. A meeting request lands; she reads the thread, checks the calendar, books the slot or offers times, sends the reply with an `.ics`, writes the follow-up into her ledger, and leaves a daily digest that leads with the ratio of handled to held.

She handles these alone, with no human in the loop:

- Meeting requests: book directly when one clear time is written and available, otherwise offer 1 to 3 times. A reply that picks one books it.
- Replies that pick, decline, move or cancel a slot she offered or booked.
- Newsletters, notifications, FYIs, auto-replies and out-of-office: filed, no reply.
- Opt-outs ("stop", "unsubscribe", "take me off your list"): that sender is silenced.

Everything else is held for the owner with the reason and a draft: money, legal, complaints, anything outside her classes, or the model unsure. A hold means "this needed a human, so she drafted it and did not send it." Holds are policy, not approvals: every run counts human approvals, and the count is 0. There is no approval path in the code, so that count cannot be anything else; the number that measures how much she does alone is how much she settles without a hold (the demo day: 13 handled, 2 held).

## Architecture: six steps, one loop

One message at a time, oldest first, in `src/doris/loop.py` (read it top to bottom):

```
read      fetch new mail by server ID against her own ledger, then screens 1 to 11 in code
classify  one model call: strict JSON, a fixed class list, an explicit "unsure"
decide    a static policy table in code maps the class to one plan
check     the gate: G1 to G17, every send and every booking, default deny
act       through an adapter (sandbox or live), then read back what was written or sent
record    one hash-chained receipt line for every step
```

| Step | Where | What it does | What it never does |
|---|---|---|---|
| read | `adapters/*.fetch_new`, `mail.parse_eml`, `screens.run_batch`, `screens.run_message` | Reads new mail by server ID (live: IMAP UID with UIDVALIDITY), never the seen flag. Decodes strictly in each part's declared charset. Runs the screens before the model: decode (quarantine on failure), seen or her own, the stop scan over the whole batch, sensitive mail, owner mail, no-reply kinds, addressing (ruling 18), oversize, hidden text, then counts links and attachments. | Opens a link, attachment, image, invite or attached email. A broken email never stops the run: it is quarantined with a receipt. |
| classify | `model.Model.classify`, `model.build_request`, `model.parse_answer` | Sends the subject, the sender's own text, any text below their closing, and the sent date as labelled data. Takes back one JSON object: a class from `shapes.CLASSES`; the date, 24-hour time, zone and length the sender wrote; a pick number; and two flags (an instruction aimed at an AI, a side question). Anything off the schema becomes `unsure`. | Writes a reply, picks a tool or takes an action. No answer at all (model down after one retry, no key) is a hold, never a guess. |
| decide | `policy.decide` | One pure function: a held thread first, then holds that code finds on its own (money words or a currency amount, a Reply-To unlike From), then the model's class, one row at a time. The parser (`parser.py`) must agree with the model before anything is booked directly. | Lets the model choose the action. Code can force a hold, never a send. |
| check | `gate.check` | Runs all 17 checks on every action, in order, and records pass, fail, n/a or error for each. Passes only if every check that applies passed. | Skips a check, or treats an error or a missing input as a pass. |
| act | `adapters/sandbox.py`, `adapters/live.py`, `compose.py`, `templates.render`, `ics.build_event` | Sends an approved template with the locked signature, writes her event create-only, deletes her own event by booking ID, files mail with a keyword. Reads every write back: the event is fetched after it is written, and her sent copy is looked for in Sent. | Sends model-written prose, deletes or moves mail, edits an event in place, or retries a write whose result it doesn't know. |
| record | `receipts.ReceiptWriter.write`, `ledger.Ledger` | Appends one JSONL line per step (observed, classified, decided, checked, called, result, held, halted, error, summary), each hashed with the line before it, flushed and fsynced. Every decided, checked, held and halted line carries a `why` from the closed list in `why.py`. | Stores a key, the meeting link or a sent body (a send is stored as its hash). |

### Her tools

Every send and every calendar write is one of five fixed action kinds (`shapes.ACTION_KINDS`), and every one passes the gate. Filing (a keyword on her own copy of a message) is the one write outside the gate.

| Action | What it is | Who it can reach |
|---|---|---|
| `send_reply` | An approved template (T1, T2, T3, T4, T9, T10, T12, T13, T18) | The sender of the message she is answering, on their thread, with one blind copy to the owner's address from config |
| `send_hold_notice` | S1: the reason, the sender's own words (defanged, codes blanked) and a draft | The owner only |
| `send_digest` | S2: the day's counts, from receipts | The owner only |
| `calendar_create` | Her event, create-only, under her own booking ID | The owner's calendar. No organizer, no attendees, so the calendar server mails nobody |
| `calendar_delete` | Removes her own event by booking ID | The owner's calendar, her own `doris-` events only |

Her other calls are listed in `receipts.CALLS`: fetch mail, look in Sent, query the calendar, read one event back, the model call, and filing a message. There is no tool for the web, a shell, a payment, deleting or moving mail, editing an event, or writing to anyone an email names.

### The modules

| File | Job |
|---|---|
| `src/doris/__main__.py` | The command line: the one way in |
| `src/doris/loop.py` | The six steps, one message at a time; a run's cycles |
| `src/doris/screens.py` | Screens 1 to 11, all code, before the model |
| `src/doris/mail.py` | `.eml` to a message, strictly; lists attachments without decoding them |
| `src/doris/sensitive.py` | The sensitive-mail screen (screen 5 and G17) |
| `src/doris/model.py` | The one model call, and the replayed, stub and yes-man stand-ins |
| `src/doris/parser.py`, `zones.py` | The plain date parser and time zones, with no zone database |
| `src/doris/policy.py` | The policy table |
| `src/doris/gate.py` | The gate, G1 to G17, in one file |
| `src/doris/caps.py` | Every hard limit, in one file |
| `src/doris/slots.py` | Where a slot can sit, what counts as busy, which slots she offers |
| `src/doris/templates.py`, `compose.py`, `holds.py`, `ics.py` | Every sentence she can send, the signature, the hold notice, her `.ics` |
| `src/doris/digest.py` | The daily digest, counted from receipts |
| `src/doris/adapters/base.py` | The adapter contract both sides keep |
| `src/doris/adapters/sandbox.py`, `adapters/live.py`, `adapters/jmap.py` | Sandbox files, or live: IMAP to read and file her mail, CalDAV for the calendar, and JMAP for every send |
| `src/doris/receipts.py`, `ledger.py`, `why.py` | The receipt chain, her state in SQLite, the closed list of whys |
| `src/doris/supervisor.py` | The kill switch: one runner per ledger, halt, resume, the worker as a child process |
| `src/doris/netguard.py` | The network guard: every outbound connection checked against a list |
| `src/doris/traps.py`, `smoke.py` | The Trap Pack runner, and the live smoke. The Trap Pack runner and the red-team proof ship in the package because you run them as commands; only those commands import them, never a run of hers |
| `src/doris/replay.py`, `watch.py`, `redteam.py` | A finished run told from its receipts alone; the live page and its Stop button; the yes-man proof |
| `src/doris/livetrace.py` | The trace every live run and smoke keeps in `traces/`, pass or fail |
| `src/doris/envfile.py`, `sentrecord.py` | Reads the env file without echoing a value; the smoke's record that a sent copy was found |
| `src/secret_scan.py` | The secret scan run by the commit hook and CI |

## Commands

From `src/`, with Python 3.9 or newer (`py` in place of `python3` on Windows). No install and no key for any of them except `run --live` and `smoke`.

| Command | What it shows | Status |
|---|---|---|
| `python3 -m doris demo` | The sandbox day under the kill switch: 15 emails, ending `13 handled, 2 held, 0 human approvals`, then the chain verified. Each `demo` is the same day again: a finished day's state, sends and bookings are moved to `state/old/` first, so a second `demo` in the same folder handles 13 again (a halted day is not moved: after `resume`, the next `demo` finishes it) | Built. With no key it replays recorded model answers if `sandbox/answers.jsonl` is present, else a keyword stand-in (stub) reads the mail, and the first line and every receipt say which. Recorded answers: in the repo, from a keyed run on main `18d160a` (9 calls, $0.034): [`traces/model-live-sandbox-20261128T123315Z/`](traces/model-live-sandbox-20261128T123315Z/) |
| `python3 -m doris traps` | The organizers' Trap Pack, one file at a time on fresh throwaway state; writes a report (`RESULTS.md`) and a trace per item | Built. With no key it replays the model's answers recorded in live-model run 3 (`tests/trap-pack/answers.jsonl`; `--model stub` for the keyword stub), with a key it calls Claude, and it writes its report under `state/throwaway/traces/`; only `--record` (our committed runs) rewrites `RESULTS.md`, which is live-model run 3's report. It runs outside the supervisor: `stop` does not reach it, a `STOP` file in the repo root halts it at its next step |
| `python3 -m doris verify [receipts.jsonl]` | Recomputes a run's hash chain; names the first line that was changed, cut, moved or added | Built |
| `python3 -m doris stop` | Halts her now, in the sandbox and live; a run in progress is killed mid-call | Built |
| `python3 -m doris resume` | Clears a halt. Only the command line can | Built |
| `python3 -m doris run` | The long run: a pass every ten minutes until stopped, and the day's digest from 5:00 PM Pacific. Sandbox by default | Built |
| `python3 -m doris run --live` | The same against her own mailbox and the owner's shared calendar. Refuses to start without a smoke from the last 24 hours that found its sent copy (`caps.SMOKE_RECORD_MAX_AGE`). Keeps its trace in `traces/live-run-<UTC stamp>/`, pass or fail | Built. First live run, Fri Nov 27 10:52 PM to Sat Nov 28 7:05 AM Pacific on `a13cadd`: an offer, then the booking the sender picked, 0 held, 0 human approvals, $0.0076; ended by `stop` (worker dead 59 ms later), outcome pass, chain verified: [`traces/live-run-20261128T065251Z/`](traces/live-run-20261128T065251Z/) |
| `python3 -m doris smoke` | Every live adapter call once against the real servers. With `--send-to`, one test send found in her Sent, then end to end: our test sender's meeting request through the forwarding rule, her loop on it, and her reply found in the sender's mailbox. Keeps its trace in `traces/live-smoke-<UTC stamp>/`, pass or fail | Built. Three live smokes committed (Nov 27, Pacific): the first two failed, at the JMAP session and at the calendar write, and run 3 passed every step: [`traces/live-smoke-20261128T055002Z/`](traces/live-smoke-20261128T055002Z/) |
| `python3 -m doris redteam` | The hostile yes-man stand-in model under the same gate: zero bad actions | Built: 0 bad actions in 206 runs over 24 cases. A plain run keeps its report and trace in its own throwaway state; only `--record` writes `tests/redteam/RESULTS.md` and `traces/model-yes-man-throwaway-redteam-<stamp>/` |
| `python3 -m doris replay <run>` | Steps through a finished run in plain words: saw, decided and why, called, got back, verified | Built. Chain checked first; takes a receipts file, run folder, trace folder or run ID |
| `python3 -m doris watch` | A live page of every step, its why and the cost so far, with one Stop button | Built. 127.0.0.1 only, port 8765 unless `--port N`, one Stop button; Ctrl+C closes the page and does not stop her |

## Models

- **Runtime model:** one, `claude-sonnet-5-5` (`model.DEFAULT_MODEL`). `DORIS_MODEL` changes it. Called over HTTPS with `urllib` (no SDK) at `https://api.anthropic.com/v1/messages`, low effort, 2,048 output tokens at most, a 60-second timeout and one retry after 2 seconds (`caps.MODEL_TIMEOUT_S`, `caps.MODEL_RETRY_WAIT_S`).
- **What it does:** it classifies one email. It never writes a word that is sent: every sentence she sends is an approved template filled by code (`templates.render`), and the gate (G8) renders it again and compares it byte for byte.
- **Cost:** every model call's receipt carries its token counts and its cost from the prices in `caps.MODEL_PRICES_USD_PER_MTOK` (read from Anthropic's pricing page on Nov 27, 2026). Measured so far, from her receipts: one live email $0.0038 (smoke run 3), the first live run's offer and booking $0.0076 (2 calls), the demo day $0.0337 (9 calls), and Trap Pack run 3 $0.0649 (20 files, 15 calls; graded against the pack's key: met 13, safe and silent 7, missed 0, bug 0, `tests/trap-pack/RUNS.md`). A run stops itself at $5.00 of model spend (`caps.BUDGET_USD_PER_RUN`, gate check G3). The key, the tier and the cost table with each trace are in the README ([With a key](README.md#with-a-key-keys-tier-and-cost)).
- **Stand-ins:** `replayed` (recorded real answers, matched by the request's sha256; a miss is a hold, never a guess), `stub` (a keyword classifier, labelled stub everywhere) and `yes-man` (the hostile stand-in that always gives the most permissive valid answer). A run never mixes sources, and the run's folder name says which it used (`model-live-…`, `model-replayed-…`, `model-stub-…`, `model-yes-man-…`).
- **Built with:** Python 3.9+, standard library only. No pip, no framework, no SDK. One SQLite file per mode plus the receipt chain.

## Permissions

Judges need no credential at all: every command above except `run --live` and `smoke` runs on the sandbox with no network. We never ask anyone for their credentials. A judge who wants the model to read the mail puts their own key in `.env` at the repo root (copied from `.env.example`; git ignores it), or in the environment, which wins; `demo`, the sandbox `run` and `traps` take only `ANTHROPIC_API_KEY` and `DORIS_MODEL` from it (`__main__.load_dotenv`), and `run --live` never reads it.

Live, Doris uses Fastmail test accounts we control: `whizz@fastmail.com` (the owner and his calendar), `doris.htm@fastmail.com` (her own mailbox) and `wolf.whizz@fastmail.com` (our test sender). Every secret lives in one file on our server, `/root/doris-secrets/.env`, never in the repo; `.env.example` lists every name with no values, the smoke's own two (`WOLF_EMAIL`, `WOLF_JMAP_TOKEN`) included. The secret scan (`src/secret_scan.py`, run by the commit hook and by CI on every push) blocks known key shapes, and on any machine that holds that file, every value in it.

| Credential | What it is and its scope | Used by | What it cannot do |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | An Anthropic API key | `model.Model` only, to call `api.anthropic.com`. Never logged or recorded | The model's answer is data: a class and a time reading. It cannot send, book, delete or open anything, because the model never picks an action and every action passes the gate. Spend stops at $5.00 a run (G3) |
| `DORIS_APP_PASSWORD` | A Fastmail app password for **her own account only** (`DORIS_MAIL_ADDRESS`), with mail and calendar access | `adapters/live.py`: IMAP (read her inbox, file with a keyword, look in Sent) and CalDAV (the owner's shared calendar). Never used to send | It cannot log in to the owner's account, read his mailbox, search it, send as him or delete from it. The key itself could do more inside her own mailbox than her code does. Her code sends only these IMAP commands: `CAPABILITY`, `LOGIN`, `LOGOUT`, `NOOP`, `SELECT`, `EXAMINE`, `LIST`, `UID SEARCH`, `UID FETCH` (bodies as `BODY.PEEK` only) and `UID STORE` of a keyword. Never `\Deleted`, `EXPUNGE`, `MOVE`, `COPY` or `APPEND` (checked after every IMAP test in `tests/unit/test_live.py`, `ImapFixture.tearDown`) |
| `DORIS_JMAP_TOKEN` | A Fastmail API token for her own account, with the Email and Email submission scopes | `adapters/jmap.py` `JmapSender`: every send, over HTTPS (port 443) to `api.fastmail.com` and the host its session names for apiUrl and uploadUrl, which must be `api.fastmail.com` or a host below it (Fastmail answers with a regional one, such as `phl.api.fastmail.com`); our server's host blocks SMTP, and there is no SMTP sender. The session's username must be her own address (`DORIS_MAIL_ADDRESS`); an alias is refused. A missing or malformed token stops `run --live` and `smoke` before anything connects, naming the setting, never its value (`test_smoke.py`: `SetupTest.test_a_jmap_token_missing_or_malformed_exits_2_naming_it_never_its_value`); a session for any account but hers is refused before anything more is sent | It has no calendar scope and no access to any other account. The token itself could read, change and delete mail in her own account. Her code uploads one message, imports it into Drafts and submits it to exactly the envelope given; before connecting it refuses a Bcc header, a From that is not hers alone, or a Message-ID other than the recorded one, and it never retries. Its one other change: destroying its own just-imported draft when a submission fails (`test_jmap.py`: `RefusalTest`, `SessionTest.test_a_session_for_another_account_is_refused_before_anything_more_is_sent`, `SendTest.test_the_send_is_one_request_holding_the_import_and_the_submission_only`) |
| `WOLF_JMAP_TOKEN` | A Fastmail API token for our test sender, `wolf.whizz@fastmail.com` | The live smoke only, with `WOLF_EMAIL`: it sends one meeting request to the owner, to come through the forwarding rule, and looks for her reply in the test sender's mailbox (`jmap.JmapSender.find_message`) | It never reaches her loop: a live run asks the env file for her names alone (`test_live_loop.py`: `WolfNeverReadTest.test_a_live_run_asks_the_env_for_her_names_alone`, `SmokeOnlySettingsTest.test_her_live_settings_hold_only_the_names_she_reads`). It has no access to her mailbox or the owner's |
| The owner's calendar, shared to her account | Not a key: the owner shared his calendar to her Fastmail account. `DORIS_CALDAV_URL` names it; she logs in with her own app password | `adapters/live.py` `LiveCalendar`: a time-range query, a create-only write, a read-back by booking ID, a delete by booking ID | The share lets her login see more than her code ever asks for: every event on that calendar, titles included, and it lets her account change them. Her code reads only start, end, status and transparency, never a title (`ics.read_event`); writes create-only (`If-None-Match: *`); and deletes only events whose booking ID starts `doris-` and that are in her ledger (G14) |

Settings that are not secrets: `DORIS_OWNER_ADDRESS` (where hold notices, the digest and the blind copy go: always from config, never from a message), `DORIS_MEETING_LINK` (the one fixed meeting room; replaced by `[meeting link]` in every receipt, recorded answer and kept copy), `DORIS_MODEL`, and the host and port overrides.

**What she can see.** Mail reaches her by a forwarding rule on the owner's account that skips her own address and sensitive mail (banks, payments, security codes, password resets, payroll, tax, medical). She sees every message that rule passes, which is all mail to the owner except the skips, from the moment it was switched on. The rule lives in the owner's Fastmail account, not in this repo, and the live smoke proved it end to end on Nov 27 (the test sender's mail reached her in 31 s, [`traces/live-smoke-20261128T055002Z/`](traces/live-smoke-20261128T055002Z/)); its skips are not exercised live. Anything sensitive that slips through is filed unread by screen 5 before the model, and only the sender, a hash and the check name are kept.

**Where she can connect.** For the life of a run, `netguard.install` checks every outbound connection against a list: live, her IMAP host, the JMAP host `api.fastmail.com` and her CalDAV host (`live.hosts`), plus the host below `api.fastmail.com` that her JMAP session names, admitted only after the sender checked it (`netguard.admit`), plus `api.anthropic.com` and the HTTPS proxy's host, if one is set, when the model is live; sandbox and traps with a replayed or stub model, no host at all. A name that is not on the list is never even looked up.

## Limits we state

- Google and Microsoft calendars need an OAuth adapter we did not build. She reads and files mail over IMAP, sends over JMAP (Fastmail's API; there is no SMTP sender), and uses CalDAV for the calendar.
- The overnight run stays under 60 emails.
- The per-run caps (100 sends to prospects, 40 calendar writes, $5.00 of model spend; `caps.py`) count over the whole of one run, and a `run` is one run however many days it lasts. A send or a booking past a cap is held, not sent; reaching the spend cap halts her until `resume`. The hourly caps count the hour just past.
- Zones she reads: Pacific, Mountain, Central and Eastern (by name or abbreviation, daylight time included), UTC or GMT, and a numeric offset written after UTC or GMT, such as UTC+1 or GMT-8 (`zones.py`, `parser.py`); a bare offset such as -08:00 after a time is no zone. Any other zone, such as UK, London, CET or IST, is no zone to her: that email never books directly, and its offer shows Pacific times with UTC beside them.
- The days she never offers or books are on a fixed list in `caps.HOLIDAYS`, given below under ruling 3.
- Not built yet: the one nudge on an unanswered offer (T9), and the once-a-cycle check that her future bookings are still on the calendar (ruling 17; today it runs only when a booking is resumed after a restart).
- The Trap Pack's known limits, and what each run got wrong, are in `tests/trap-pack/RUNS.md`.

## Repo layout

`README.md` (how to run it) · `AGENT.md` (this page) · `SAFETY.md` (every irreversible action, its rule, its test, its trap; the four safety sections) · `.env.example` · `src/` · `sandbox/` (the demo's inbox, calendar and outbox) · `traces/` (real runs) · `tests/` (unit and process tests, and the Trap Pack with its results) · `LICENSE` (MIT) · `.agentware.toml`.

## Appendix: the rules the comments cite

Comments, tests and these pages cite rules by number: "ruling 8", "locked call 9", "G11", "B4", "T18", "screen 10". The rules come from our project rules page, which is not in this repo, so here they are in our own words. Each one is enforced by the code that cites it; where this appendix and the code disagree, the code is what runs.

### The 12 locked calls

1. Scheduling runs from the first email: no trial period, no earned autonomy, no approval step.
2. She books directly only when the plain date parser and the model agree on one written date, time and zone, and the slot is available. Otherwise she offers 1 to 3 written times, and a reply choosing one books it. The details are rulings 2 to 6.
3. Her events carry no organizer and no attendees, so the calendar server mails nobody. The prospect gets her own email with an `.ics`, through the gate, and their cancel or change is read from their reply.
4. The model reads, code writes. Every sentence she sends is an approved template; text the model wrote is never sent.
5. No second model. The safety claim: assume the model is fully compromised; the gate bounds the worst it can do. The hostile yes-man stand-in proves it (`redteam`).
6. Mail can never start, steer or resume her, and nothing is approved by email. The one thing mail can stop is her writing to that sender (ruling 9); not even the owner's mail halts her (ruling 1). Resume and every go command exist only on the command line.
7. The kill switch: one action halts everything mid-run (the worker is a child process, killed mid-call), drops pending work, survives a restart and writes a receipt.
8. Holds are few and by policy: money, legal, complaints, anything outside her classes, or unsure. Every run counts human approvals, and the count is 0.
9. Replies go only to the sender, on their thread, with a blind copy to the owner from config; no recipient or attendee ever comes from a message body. Her mail is stamped `Auto-Submitted: auto-replied` (RFC 3834), so other robots don't answer it. Every hard cap lives in one file, `caps.py`, including the model budget that stops the run.
10. Code appends one fixed signature to everything she sends: "Doris, AI assistant to Shane Bornstein, Hire the Machines. I'm an AI, not a person. Reply "stop" to opt out." (`compose.SIGNATURE`, appended in `compose.py`; G8 checks it byte for byte).
11. Standard library only: no pip, no framework, no SDK, Python 3.9 or newer. One SQLite file per mode plus the receipt chain. One Claude model at runtime, named above.
12. Fail closed and loud. With no model answer she holds and sends nothing.

### The 18 calendar rulings

1. **Owner mail is not a command channel.** Nothing from the owner's address halts or steers her; it is held for him (screen 6).
2. **A direct booking needs one clear time.** In the sender's own visible text, above their closing and outside any quote: an absolute date, one time and one zone, giving one instant that exists once. A written weekday must match the date. A zone abbreviation not in effect that day (PDT in December) is no zone. Text is folded to plain ASCII first. Subjects, quotes, forwards, signatures, attachments, hidden text and relative dates ("tomorrow", "next Tuesday") never count. A date or time with two readings counts only when exactly one is left and it is bookable; one instant written in two zones is one time. Anything else gets an offer, with the sender's apparent time first when it is available. Oversize mail is held. The conditions are B1 to B12, below.
3. **Busy and bookable.** Busy is any overlapping event that isn't cancelled, across the whole span, even one marked free, tentative or all-day. A time she offers or books sits Monday to Friday, 9 AM to 5 PM Pacific, at least 24 hours and at most 30 days out, and not on a holiday. The holidays (`caps.HOLIDAYS`): Thu Nov 26 and Fri Nov 27 2026 (Thanksgiving and the day after), Thu Dec 24 and Fri Dec 25 (Christmas Eve and Christmas), Thu Dec 31 and Fri Jan 1 2027 (New Year's Eve and Day), Mon Jan 18 2027 (Martin Luther King Jr. Day) and Mon Feb 15 2027 (Presidents' Day). A request for one of them gets an offer of other days.
4. **No double booking.** One email at a time. She reads the calendar again right before the write and reads her event back right after; if anything else overlaps, she removes hers and offers. A failed read is never "available".
5. **Length.** What the sender wrote, else 30 minutes. Over 60 minutes, open-ended, all-day, recurring, several meetings at once, or zero length: held.
6. **Offers.** One sender, one thread, absolute times, a 7-day expiry, each used once. A pick must name exactly one of the times, or she offers them again; a plain "yes" books only an offer of one time; a time close to one offered is offered back in both zones; a complete new time is a new request; the latest message wins; one available time is still offered. At most two offers per thread; a third would be held.
7. **Cancel and move.** Only her own events in her ledger, for the same sender on the same thread. A cancel removes her event, and the reply invites a new day. A move cancels hers, then offers fresh times, the time they named first if it is available. No booking on file, or anyone else's event: held.
8. **IDs and no repeats.** Her booking ID goes in the ledger with the Message-ID before any write. A write with an unknown result is looked up by ID, never retried blind, else held. A confirmation email is never sent blind twice: after a restart she looks in Sent first, and sends it only if it surely did not go, under the same Message-ID. A message she finished never runs twice. The sandbox and live keep separate ledgers. One Doris per ledger: a second copy refuses to start, and live refuses to start without its ledger unless told on the command line to start fresh.
9. **Stop.** A fixed list: "stop" or "unsubscribe" alone on a line; "stop emailing me", "don't email me", "opt out", "no more emails" or "take me off your list" anywhere in the sender's own text; "remove me" alone on a line or before "from your list" or "from this list". Code checks it before the model, across all new mail at once, and again at the gate (G5). It beats every other rule, silences that sender only, drops their queued work, keeps meetings already confirmed, and shows on the digest. The owner's address is never silenced; ruling 1 covers it.
10. **Her `.ics`.** Fixed fields only: start, end, a template title, her booking ID and the meeting link from config. Nothing the sender wrote. The confirmation gives the time in the sender's zone and in Pacific.
11. **What she never shows or answers.** Never a full calendar grid, the reason a time is busy, or an event's title. Calendar receipts, bounces, list mail and her own mail get no reply.
12. **Mail that is only an injection** (instructions aimed at an AI, or a lure, with no real request): filed, no reply, a receipt and one digest line. Not a hold.
13. **Hidden text.** She reads the plain-text part. HTML with hiding styles is held, and the notice quotes the visible text only.
14. **A pick plus a question** books the pick, and the question becomes a digest line. Money, legal or a complaint in the sender's own text (not a footer or a quote) holds the whole email.
15. **People.** An assistant booking for a boss gets the booking. A pick from a new address, or a Reply-To unlike From, is held.
16. **A readable email with an impossible time** (February 30, 25:00) gets an offer.
17. **A booking gone from the calendar.** If one of her future bookings is no longer on the calendar, she sends the owner a hold notice and nothing to the prospect. Edits to an event are not checked. (See Limits above for what is built.)
18. **Who she answers.** If the owner is in To with only the sender, she may reply; else if Doris is in To, on her own thread, with only the sender, she may reply. Anything else (Cc, Bcc, a group, a first email sent straight to her) is filed or held, never answered (screen 8, G6).

### The other numbers

| Label | What it is | Where |
|---|---|---|
| G1 to G17 | The gate's checks, run on every send and booking | `gate.py`; the table in [SAFETY.md](SAFETY.md#guardrails) |
| B1 to B12 | The conditions for a direct booking (ruling 2). B1 to B9 are the parser's: the sender's own text with no hidden direction marks or glued characters, one date and one time, side by side, one zone right after the time, an absolute date, no qualifier (a range, an either-or, "around" or "-ish", "before" or "by", a negation, or a word that says it is not a meeting, such as "deadline", "flight" or "webinar"), one reading left, bookable, weekdays that match. B11: the model, asked on its own, reads the same instant. B12: the gate at that moment (G9, G10, G11). B10, a check for the hour that daylight time skips or repeats, was cut: clocks change at 2 AM on a Sunday, outside bookable hours | `parser.py` (`CONDITIONS`, `model_agrees`), `gate.py` (G10) |
| Screens 1 to 11 | The checks in code before the model, in order: decode, seen or her own, (3 cut), stop scan, sensitive mail, owner mail, no-reply kinds, addressing, oversize, hidden text, count but never open. Screen 3 was a stop sent by the owner by email; it was cut because nothing in mail halts her (ruling 1) | `screens.py` |
| T1 to T18, S1, S2 | Her templates: T to a sender, S to the owner (S1 the hold notice, S2 the digest). Nine T templates are defined: T1, T2, T3, T4, T9, T10, T12, T13, T18. The other numbers were cut before the build and never reused: T5 and T11 (a move straight to a new time; a move is now T18's cancel plus fresh times), T6 (telling a prospect the owner removed their meeting; now only the owner is told, ruling 17), T7 (an opt-out confirmation; opting out is silent), T8 (a holding reply; a hold sends the prospect nothing), T14 (offers for a request over an hour; it is held), T15 and T16 (replies about booking links and invitations; both get the ordinary T2 offer, unopened), T17 (re-sending declined times; the second offer, T3, gives new ones) | `templates.py` |
| K1 to K3 | Cut replies to "are you a person?", "what do you do?" and "how does the call work?". Such questions are held for the owner | Cut |
| I1 to I9 | The Trap Pack runner's invariants, checked from her outputs after each item | `traps.py` |
| Rows N, D, H, Z, L, Q; P1, P2 | Rows of our tables of how people write dates and times: N folding text to plain ASCII, D dates, H times, Z zones, L relative dates, Q qualifiers that stop a direct booking; P1 and P2 the two rules that drop a second reading | `parser.py`, beside the code for each row; `tests/unit/test_parser.py` |
| A2, A3, A6 | Rules we added to our own build brief: A2 every decision carries a why from one closed list (`why.py`); A3 queued means written down (work is in the ledger before it starts, so a kill can name it); A6 a run's folder name starts with its model source | `why.py`, `ledger.py`, `model.py` |
| Contract 7 | One of the rules every module was built to: fail closed. A check that cannot run blocks; a failed read is never "empty", "available" or "success" | Every module |
| Decision N | An entry in our numbered decision log, kept with our working notes outside this repo. The comment beside it says what was decided, and the code is the rule | Outside the repo |
| Review and finding labels | "Fable review of PR #4", "finding 1", "M1", "second review": the code review that found a problem, and its finding number. They say where a line came from, not what it does | Outside the repo |
