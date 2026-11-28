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

Everything else is held for the owner with the reason and a draft: money, legal, complaints, anything outside her classes, or the model unsure. A hold means "this needed a human, so she drafted it and did not send it." Holds are policy, not approvals: every run counts human approvals, and the count is 0.

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
| classify | `model.Model.classify`, `model.build_request`, `model.parse_answer` | Sends the sender's own text, the subject and the sent date as labelled data. Takes back one JSON object: a class from `shapes.CLASSES`; the date, 24-hour time, zone and length the sender wrote; a pick number; and two flags (an instruction aimed at an AI, a side question). Anything off the schema becomes `unsure`. | Writes a reply, picks a tool or takes an action. No answer at all (model down after one retry, no key) is a hold, never a guess. |
| decide | `policy.decide` | One pure function: a held thread first, then holds that code finds on its own (money words or a currency amount, a Reply-To unlike From), then the model's class, one row at a time. The parser (`parser.py`) must agree with the model before anything is booked directly. | Lets the model choose the action. Code can force a hold, never a send. |
| check | `gate.check` | Runs all 17 checks on every action, in order, and records pass, fail, n/a or error for each. Passes only if every check that applies passed. | Skips a check, or treats an error or a missing input as a pass. |
| act | `adapters/sandbox.py`, `adapters/live.py`, `compose.py`, `templates.render`, `ics.build_event` | Sends an approved template with the locked signature, writes her event create-only, deletes her own event by booking ID, files mail with a keyword. Reads every write back: the event is fetched after it is written, and her sent copy is looked for in Sent. | Sends model-written prose, deletes or moves mail, edits an event in place, or retries a write whose result it doesn't know. |
| record | `receipts.ReceiptWriter.write`, `ledger.Ledger` | Appends one JSONL line per step (observed, classified, decided, checked, called, result, held, halted, error, summary), each hashed with the line before it, flushed and fsynced. Every decided, checked, held and halted line carries a `why` from the closed list in `why.py`. | Stores a key, the meeting link or a sent body (a send is stored as its hash). |

### Her tools

Every effect she can have is one of five fixed action kinds (`shapes.ACTION_KINDS`), and every one passes the gate:

| Action | What it is | Who it can reach |
|---|---|---|
| `send_reply` | An approved template (T1, T2, T3, T4, T9, T10, T12, T13, T18) | The sender of the message she is answering, on their thread, with one blind copy to the owner's address from config |
| `send_hold_notice` | S1: the reason, the sender's own words (defanged, codes blanked) and a draft | The owner only |
| `send_digest` | S2: the day's counts, from receipts | The owner only |
| `calendar_create` | Her event, create-only, under her own booking ID | The owner's calendar. No organizer, no attendees, so the calendar server mails nobody |
| `calendar_delete` | Removes her own event by booking ID | The owner's calendar, her own `doris-` events only |

Her reads are listed in `receipts.CALLS`: fetch mail, look in Sent, file a message (a keyword on her own copy), query the calendar, read one event back, and the model call. There is no tool for the web, a shell, a payment, deleting or moving mail, editing an event, or writing to anyone an email names.

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
| `src/doris/adapters/sandbox.py`, `adapters/live.py` | Sandbox files, or live IMAP, SMTP and CalDAV |
| `src/doris/receipts.py`, `ledger.py`, `why.py` | The receipt chain, her state in SQLite, the closed list of whys |
| `src/doris/supervisor.py` | The kill switch: one runner per ledger, halt, resume, the worker as a child process |
| `src/doris/netguard.py` | The network guard: every outbound connection checked against a list |
| `src/doris/traps.py`, `smoke.py` | The Trap Pack runner, and the live smoke |
| `src/secret_scan.py` | The secret scan run by the commit hook and CI |

## Commands

From `src/`, with Python 3.9 or newer (`py` in place of `python3` on Windows). No install and no key for any of them except `run --live` and `smoke`.

| Command | What it shows | Status |
|---|---|---|
| `python3 -m doris demo` | The sandbox day under the kill switch: 15 emails, ending `13 handled, 2 held, 0 human approvals`, then the chain verified | Built. With no key it replays recorded model answers if `sandbox/answers.jsonl` is present, else a keyword stand-in (stub) reads the mail, and the first line and every receipt say which. Recorded answers: **pending** |
| `python3 -m doris traps` | The organizers' Trap Pack, one file at a time on fresh throwaway state; writes `tests/trap-pack/RESULTS.md` and a trace per item | Built |
| `python3 -m doris verify [receipts.jsonl]` | Recomputes a run's hash chain; names the first line that was changed, cut, moved or added | Built |
| `python3 -m doris stop` | Halts her now, in the sandbox and live; a run in progress is killed mid-call | Built |
| `python3 -m doris resume` | Clears a halt. Only the command line can | Built |
| `python3 -m doris run` | The long run: a pass every ten minutes until stopped, and the day's digest from 5:00 PM Pacific. Sandbox by default | Built |
| `python3 -m doris run --live` | The same against her own mailbox and the owner's shared calendar. Refuses to start without a smoke from the last 24 hours that found its sent copy | Built; its first live run is **pending** |
| `python3 -m doris smoke` | Every live adapter call once against the real servers | Built |
| `python3 -m doris redteam` | The hostile yes-man stand-in model under the same gate: zero bad actions | **Pending**: prints "not built yet" today. The stand-in itself exists (`model._yes_man_answer`) |
| `python3 -m doris replay <run>` | Steps through a finished run in plain words: saw, decided and why, called, got back, verified | **Pending**: prints "not built yet" today |
| `python3 -m doris watch` | A live page of every step, its why and the cost so far, with one Stop button | **Pending**: prints "not built yet" today |

## Models

- **Runtime model:** one, `claude-sonnet-5-5` (`model.DEFAULT_MODEL`). `DORIS_MODEL` changes it. Called over HTTPS with `urllib` (no SDK) at `https://api.anthropic.com/v1/messages`, low effort, 2,048 output tokens at most, a 60-second timeout and one retry after 2 seconds (`caps.MODEL_TIMEOUT_S`, `caps.MODEL_RETRY_WAIT_S`).
- **What it does:** it classifies one email. It never writes a word that is sent: every sentence she sends is an approved template filled by code (`templates.render`), and the gate (G8) renders it again and compares it byte for byte.
- **Cost:** every model call's receipt carries its token counts and its cost from the prices in `caps.MODEL_PRICES_USD_PER_MTOK` (read from Anthropic's pricing page on Nov 27, 2026). Measured so far: Trap Pack run 1, 20 files, $0.06 for the model calls (`tests/trap-pack/RUNS.md`). A run stops itself at $5.00 of model spend (`caps.BUDGET_USD_PER_RUN`, gate check G3). The keys, tiers and cost table is in the README: **pending**, after the overnight run's measured cost.
- **Stand-ins:** `replayed` (recorded real answers, matched by the request's sha256; a miss is a hold, never a guess), `stub` (a keyword classifier, labelled stub everywhere) and `yes-man` (the hostile stand-in that always gives the most permissive valid answer). A run never mixes sources, and the run's folder name says which it used (`model-live-…`, `model-replayed-…`, `model-stub-…`).
- **Built with:** Python 3.9+, standard library only. No pip, no framework, no SDK. One SQLite file per mode plus the receipt chain.

## Permissions

Judges need no credential at all: every command above except `run --live` and `smoke` runs on the sandbox with no network. We never ask anyone for their credentials.

Live, Doris uses Fastmail test accounts we control: `whizz@fastmail.com` (the owner and his calendar), `doris.htm@fastmail.com` (her own mailbox) and `wolf.whizz@fastmail.com` (our test sender). Every secret lives in one file on our server, `/root/doris-secrets/.env`, never in the repo; `.env.example` lists the names with no values. The secret scan (`src/secret_scan.py`, run by the commit hook and by CI on every push) blocks known key shapes, and on any machine that holds that file, every value in it.

| Credential | What it is and its scope | Used by | What it cannot do |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | An Anthropic API key | `model.Model` only, to call `api.anthropic.com`. Never logged or recorded | The model's answer is data: a class and a time reading. It cannot send, book, delete or open anything, because the model never picks an action and every action passes the gate. Spend stops at $5.00 a run (G3) |
| `DORIS_APP_PASSWORD` | A Fastmail app password for **her own account only** (`DORIS_MAIL_ADDRESS`), with mail and calendar access | `adapters/live.py`: IMAP (read her inbox, file with a keyword, look in Sent), CalDAV (the owner's shared calendar), and SMTP until JMAP sending lands | It cannot log in to the owner's account, read his mailbox, search it, send as him or delete from it. The key itself could do more inside her own mailbox than her code does. Her code sends only these IMAP commands: `CAPABILITY`, `LOGIN`, `LOGOUT`, `NOOP`, `SELECT`, `EXAMINE`, `LIST`, `UID SEARCH`, `UID FETCH` (bodies as `BODY.PEEK` only) and `UID STORE` of a keyword. Never `\Deleted`, `EXPUNGE`, `MOVE`, `COPY` or `APPEND` (checked after every IMAP test in `tests/unit/test_live.py`, `ImapFixture.tearDown`) |
| `DORIS_JMAP_TOKEN` | A Fastmail API token for her own account, with the Email and Email submission scopes | Sending her mail over JMAP on HTTPS. **Pending**: the JMAP sender is being wired in tonight (our server's host blocks SMTP); until it merges, the code on this branch sends by SMTP | It has no calendar scope and no access to any other account. The token itself could read and change mail in her own account; her code only sends |
| `WOLF_JMAP_TOKEN` | A Fastmail API token for our test sender, `wolf.whizz@fastmail.com` | The live smoke only, to send one test message through the forwarding rule and look for her reply. Never read by her loop. **Pending**, with the JMAP sender | It never reaches her loop, her mailbox or the owner's |
| The owner's calendar, shared to her account | Not a key: the owner shared his calendar to her Fastmail account. `DORIS_CALDAV_URL` names it; she logs in with her own app password | `adapters/live.py` `LiveCalendar`: a time-range query, a create-only write, a read-back by booking ID, a delete by booking ID | The share lets her login see more than her code ever asks for: every event on that calendar, titles included, and it lets her account change them. Her code reads only start, end, status and transparency, never a title (`ics.read_event`); writes create-only (`If-None-Match: *`); and deletes only events whose booking ID starts `doris-` and that are in her ledger (G14) |

Settings that are not secrets: `DORIS_OWNER_ADDRESS` (where hold notices, the digest and the blind copy go: always from config, never from a message), `DORIS_MEETING_LINK` (the one fixed meeting room; replaced by `[meeting link]` in every receipt, recorded answer and kept copy), `DORIS_MODEL`, and the host and port overrides.

**What she can see.** Mail reaches her by a forwarding rule on the owner's account that skips her own address and sensitive mail (banks, payments, security codes, password resets, payroll, tax, medical). She sees every message that rule passes, which is all mail to the owner except the skips, from the moment it was switched on. The rule lives in the owner's Fastmail account, not in this repo, and its first end-to-end check is the live smoke: **pending**. Anything sensitive that slips through is filed unread by screen 5 before the model, and only the sender, a hash and the check name are kept.

**Where she can connect.** For the life of a run, `netguard.install` checks every outbound connection against a list: live, her IMAP, SMTP and CalDAV hosts (`live.hosts`), plus `api.anthropic.com` when the model is live; sandbox and traps with a replayed or stub model, no host at all. A name that is not on the list is never even looked up.

## Limits we state

- Google and Microsoft calendars need an OAuth adapter we did not build. She speaks IMAP, SMTP or JMAP, and CalDAV.
- The overnight run stays under 60 emails.
- Not built yet: the one nudge on an unanswered offer (T9), and the once-a-cycle check that her future bookings are still on the calendar (ruling 17; today it runs only when a booking is resumed after a restart).
- The Trap Pack's known limits, and what each run got wrong, are in `tests/trap-pack/RUNS.md`.

## Repo layout

`README.md` (how to run it) · `AGENT.md` (this page) · `SAFETY.md` (every irreversible action, its rule, its test, its trap; the four safety sections) · `.env.example` · `src/` · `sandbox/` (the demo's inbox, calendar and outbox) · `traces/` (real runs) · `tests/` (unit and process tests, and the Trap Pack with its results) · `LICENSE` (MIT) · `.agentware.toml`.
