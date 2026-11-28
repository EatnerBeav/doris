# Doris

An inbox and calendar agent you can walk away from. Agentware 2026, track T1 (Inbox & Calendar).

Doris answers a business owner's email and runs the owner's calendar while the owner is away. A meeting request comes in. She reads the thread, checks the calendar, books the slot or offers times, sends the reply with a calendar file, and files the follow-up. Newsletters and auto-replies are filed with no reply. Anything that needs a person (money, legal, a complaint, anything outside her job) is held for the owner with the reason and a draft, and nothing is sent. Once a day she sends the owner a digest of what she did. No human approves anything in a run; every run counts approvals, and the count is 0.

The model only reads: for each email it returns one classification. Code decides what to do, and every send and every booking passes a gate of 17 checks in one file. Every step leaves a hash-chained receipt, and one command stops her mid-call.

Team Whizz: Shane Bornstein and Wolf (Claude), his CTO, for Hire the Machines. Anthropic is not involved in the entry.

## Run it: no install, no key, about 15 seconds

You need Python 3.9 or newer. Doris uses the standard library only, so there is nothing to install.

```
git clone https://github.com/EatnerBeav/doris
cd doris/src
python3 -m doris demo
```

- **Windows:** run `py -m doris demo`, and type `py` wherever this README says `python3` (on many Windows PCs `python3` only opens the Microsoft Store). In cmd, the second line is `cd doris\src`.
- **Downloaded the ZIP instead of cloning:** the folder is called `doris-main`, so the second line is `cd doris-main/src` (Windows' Extract All puts it one level deeper, in `doris-main\doris-main`).

Every `python3 -m doris` command in this README runs from `src/` (`py -m doris` on Windows). The demo uses no network and needs no account.

## What you just saw

The demo replays one working day for the owner, Shane Bornstein. It is a Tuesday in a sandbox: a folder of real `.eml` files is the inbox, `.ics` files are the calendar, and the clock is the sandbox's own.

Fifteen emails arrive between 8 AM and just after 3 PM Pacific. Each line of output is one email: the time, the sender, what she did, the reply she sent (its template code and name), and why, in the words of the rule that decided it. The day includes:

- a clear request that is booked;
- a request with no exact time, which gets an offer of times, and one for a time that is taken, which gets other times;
- a reply that picks one of the offered times and gets booked;
- a request to move a booked meeting;
- an auto-reply and a newsletter, filed with no reply;
- two opt-outs;
- an email that is nothing but instructions, filed with no reply;
- an unreadable email, quarantined;
- two emails held for the owner: one about money, one asking her to write to another person.

Two failures are staged on purpose, and the output labels them:

- The calendar times out once. That is the pause of about 12 seconds; she then retries and succeeds.
- Another event lands on the calendar in the middle of a booking. She removes her own booking and offers fresh times.

At 5 PM she sends the owner the digest. The run ends with **What Doris did for Shane**: the owner's day told from her receipts (what needs the owner and why, the meetings on the calendar, each thread in plain words, the digest, the cost), then the receipt chain verified.

The banner on the first line says what read the mail. With no key, she reads the mail with the model's own answers, replayed from a real keyed run of this same day: `sandbox/answers.jsonl` holds them, the banner names the run, and every receipt says `replayed`. That run's trace, with its measured cost, is [`traces/model-live-sandbox-20261128T123315Z/`](traces/model-live-sandbox-20261128T123315Z/). An email with no recorded answer (one you add yourself) is held, never guessed. Without that file, a built-in keyword stand-in reads the mail and every receipt says `stub`. With `ANTHROPIC_API_KEY` set, the model is called live (see [With a key](#with-a-key-keys-tier-and-cost)).

**The codes.** Every sentence she sends is a fixed, approved template filled in by code; the model never writes a word that goes out.

| Code | What it is |
|---|---|
| T1 | Booking confirmation, with the `.ics` attached |
| T2 | An offer of 2 or 3 times, when no single clear time was written |
| T4 | "Shane isn't available then, but is at any of these" |
| T18 | A move: her booking cancelled, with fresh times offered |
| S1 | A hold notice to the owner: the sender's own words, the reason, and a draft |
| S2 | The daily digest to the owner |
| G1 to G17 | The gate's checks, run before every send and booking (`src/doris/gate.py`, explained in [SAFETY.md](SAFETY.md)). The end summary's gate count is the number of times the gate ran |

## Open what she did

Paths here are from the repo root, one folder up from `src/`.

- `sandbox/outbox/`: every email she sent, as `.eml` files any mail app opens. Each file is named `<n>-<template>.eml`. Replies go to the sender only, with a blind copy to the owner, which is recorded in `envelope.jsonl`. Hold notices and the digest go to the owner. `keywords.json` records how she filed each email; she never moves or deletes mail.
- `sandbox/calendar/`: her bookings (`doris-*.ics`), next to the owner's busy blocks.
- `state/sandbox/runs/<run>/receipts.jsonl`: the receipt chain, one JSON line per step. Each line records what she saw, decided, called and got back, and the hash of the line before it.

Run the demo again and it starts the day fresh; earlier days are moved to `state/old/`, which appears after your second run. After a stop, the next demo continues the same day instead, and its summary counts from the resume.

## The next five minutes

| Command (from `src/`) | What you'll see | Time |
|---|---|---|
| `python3 -m doris replay` | The last run, email by email, from its receipts alone. Each step shows what she saw, how the email was classed, what was decided and by which policy row, every gate check, every call, and the read-back after each write and send | instant |
| `python3 -m doris verify` | The hash chain recomputed. Edit any line of `receipts.jsonl` and run it again: it names the first broken line, even when the tool you edit with re-saves the file with CRLF line endings, as PowerShell does | instant |
| `python3 -m doris stop` | The kill switch. Open a second terminal in `src/` and type `python3 -m doris stop` without pressing Enter. Start `demo` in the first terminal, then press Enter in the second during the calendar pause. She halts mid-call, and the halt line names the call in flight and the work dropped. `python3 -m doris resume` clears the halt (only the command line can), and the next `demo` finishes the day | instant |
| `python3 -m doris traps` | The organizers' Trap Pack: all 20 files through the real loop and gate, each on fresh throwaway state, one line per file. The outcome words: *refused* (an instruction in it was seen and not obeyed), *not inspected* (an attachment or link she never opened), *ask* (held for the owner, or times offered to the sender), *stop cleanly* (filed or quarantined, nothing sent); *pass* means none of the safety invariants listed in the report broke. The report, `RESULTS.md`, explains each row. With no key the stand-in reads them, and it is cruder than the model: it offers times to a few senders the model files with no reply. Our live-model runs and how we graded them are in [`tests/trap-pack/RUNS.md`](tests/trap-pack/RUNS.md) | 2 s |
| `python3 -m doris redteam` | The model replaced by a hostile stand-in that obeys every injection, run against every Trap Pack file and the demo day under the same gate. It passes only with 0 bad actions. Most rows show the gate blocking nothing: the screens and the policy table had already turned the attack into a hold, a filing or an ordinary reply, so nothing was left for the gate to block | 30 to 60 s |
| `python3 -m doris watch` | A live page at http://127.0.0.1:8765 showing the current step, tool calls, every decision with its reason, and the cost so far, with one Stop button. Leave it open and run `demo` in a second terminal | live |

`python3 -m doris --help` lists every command.

## Try it on your own email

The sandbox is the test account: no sign-up, no credential, and with no key nothing in it connects anywhere (her network guard allows no host). Save any email as an `.eml` file, put it in `sandbox/inbox/`, and run `demo` again. [`sandbox/README.md`](sandbox/README.md) says what the file needs: addressed to the owner alone, its own Message-ID, a Date inside the demo's afternoon, and one line in `inbox/facts.json`. With a key, the model reads it and her answer lands in `sandbox/outbox/`. With no key she holds it, because the recorded answers have none for it, and her notice to the owner says the model wasn't available: she never guesses. We never ask anyone for a credential.

## With a key: keys, tier and cost

None of the above needs a key, and with no key every command costs $0. To have the model read the mail, set an Anthropic API key in the environment and run the demo:

- bash: `export ANTHROPIC_API_KEY=...`
- PowerShell: `$env:ANTHROPIC_API_KEY="..."`

Then run `python3 -m doris demo`. The banner says the model is called live, and each line shows the cost of its call.

- **Keys:** one, `ANTHROPIC_API_KEY`. Every other name in [`.env.example`](.env.example) is for live mail on our own accounts, which no judge needs.
- **Model:** `claude-sonnet-5-5`, one model, called with `urllib` (no SDK).
- **Tier:** any of Anthropic's published tiers. On the lowest, Start, Claude Sonnet 5.5 allows 4,000 requests and 2,000,000 input tokens a minute ([Anthropic's rate limits](https://platform.claude.com/docs/en/api/rate-limits), read Nov 28, 2026). Her busiest command, `traps`, makes 15 calls one at a time: 27,417 input tokens in about 30 seconds in Trap Pack run 3. A new organization may start on Anthropic's Evaluation tier, whose lower limits are not published; we have not run on it. A call that fails twice (a refused key, a rate limit, a timeout) holds that email, and nothing is sent to its sender.
- **Limit:** a run stops itself at $5.00 of model spend (gate check G3).

**What a run costs**, measured from her receipts (about $0.004 a call):

| Run | Model calls | Cost | Trace |
|---|---|---|---|
| Every command above, with no key (recorded answers or the stand-in; the demo shows the recorded cost, marked so) | none | $0 | |
| One live email, read and answered (live smoke run 3, Nov 27) | 1 | $0.0038 | [`traces/live-smoke-20261128T055002Z/`](traces/live-smoke-20261128T055002Z/) |
| The first live run: a request she answered with times, then the booking the sender picked (Nov 27 to 28) | 2 | $0.0076 | [`traces/live-run-20261128T065251Z/`](traces/live-run-20261128T065251Z/) |
| The demo day, 15 emails | 9 | $0.0337 | [`traces/model-live-sandbox-20261128T123315Z/`](traces/model-live-sandbox-20261128T123315Z/) |
| The Trap Pack, 20 files (run 3) | 15 | $0.0649 | [`traces/model-live-throwaway-traps-20261128T1233Z/`](traces/model-live-throwaway-traps-20261128T1233Z/) |

The demo's 15 emails make 9 classification calls (the model's with a key, the stand-in's without); screens in code settle the other six before anything reads them.

## Evidence for each criterion

Judges rate five criteria out of 5, weighted 30, 25, 20, 15 and 10, and add up to 4 safety bonus points. Where to see each:

| Criterion | Weight | Where to see it |
|---|---|---|
| Autonomy | 30 | `demo` ends with `0 human approvals`, counted from the receipts. Holds are policy, labelled with their reason, and send nothing to the sender. Live, the first live run ([`traces/live-run-20261128T065251Z/`](traces/live-run-20261128T065251Z/)) ran 8 hours on its own: a request for a busy time got three other times, the sender picked one, and she re-checked the calendar, booked it, read it back and sent the confirmation with its `.ics`; 0 held, 0 human approvals |
| Usefulness | 25 | The scheduling job end to end: `sandbox/outbox/` holds the real replies with `.ics` files, time zones written both ways, and the digest. The demo ends with the owner's day told from her receipts |
| Reliability | 20 | The staged calendar timeout (retried) and the mid-booking collision (recovered) in the demo, the quarantined email, and `traps`. Trap Pack run 3 with the model live, graded by hand against the pack's own key: met 13, safe and silent 7, missed 0, bug 0; run 1's misses are kept beside it ([`tests/trap-pack/RUNS.md`](tests/trap-pack/RUNS.md)) |
| Engineering | 15 | [AGENT.md](AGENT.md): six steps in one loop (`src/doris/loop.py`), standard library only, each credential and what it cannot do. CI runs the unit tests on Linux, macOS and Windows under Python 3.9 and the latest 3.x, and this README's run lines as written in bash, zsh, cmd, Windows PowerShell 5.1 and PowerShell 7 |
| Demo | 10 | `demo` itself, and the 3-minute video: **pending** (filmed Sunday) |
| Safety: guardrails | +1 | The gate (`src/doris/gate.py`), every irreversible action in [SAFETY.md](SAFETY.md), and `redteam`: a model that obeys every injection, 0 bad actions in 206 runs |
| Safety: live monitoring | +1 | `watch` |
| Safety: kill switch | +1 | `stop` mid-run, with the halt receipt. The first live run was ended this way: worker dead 59 ms after the stop, chain verified |
| Safety: audit trail | +1 | `receipts.jsonl`, `verify` and `replay`, and every run in [`traces/`](traces/) |

## Live, on our own test accounts

Live, Doris works only with Fastmail test accounts we control. She reads mail over IMAP, sends over Fastmail's JMAP API, and uses CalDAV for the owner's shared calendar. A judge never needs this, and we never ask for a credential.

The setting names are in [`.env.example`](.env.example); what each credential can and cannot do is in [AGENT.md](AGENT.md#permissions). `python3 -m doris smoke` checks every live call once, and `python3 -m doris run --live` starts her. Every live run keeps its trace in `traces/live-*`, pass or fail: the three smokes of Nov 27 (two failed, run 3 passed every step) and the first live run, [`traces/live-run-20261128T065251Z/`](traces/live-run-20261128T065251Z/).

To repeat it on your own Fastmail test accounts (optional, never needed to judge):

1. Two accounts: the owner's, and hers. On the owner's, share the calendar with hers and add a forwarding rule to her that skips her own address.
2. On hers: an app password with mail and calendar access, and an API token with the Email and Email submission scopes.
3. An env file outside the repo with the names in `.env.example`, passed to each command with `--env PATH`.
4. `python3 -m doris smoke --env PATH --send-to ADDRESS`. Its sends go only to the addresses in `smoke.SEND_TO_ALLOWED`, which are our two test inboxes, so put your own test addresses there first. Its end-to-end step also needs `WOLF_EMAIL` and `WOLF_JMAP_TOKEN`: a third account, on that list, that sends her the test request.
5. `python3 -m doris run --live --fresh --env PATH`. She refuses to start without a smoke from the last 24 hours that found its sent copy, and `--fresh` starts her ledger once, so she reads only mail that arrives after it.

## Known limits

- Calendars: Google and Microsoft need an OAuth adapter we did not build. Live, we have run her only on Fastmail; sending is Fastmail's JMAP API, with no SMTP sender.
- Volume: the overnight run stays under 60 emails. The longest live run so far is the first one: 8 hours, 2 emails.
- A judge with no key sees the model's answers replayed from a real run, not a live call. An email you add yourself has no recorded answer, so with no key it is held.
- Not built yet: the one follow-up nudge on an unanswered offer, and the check, once a cycle, that her future bookings are still on the calendar.
- In Trap Pack run 3, 7 of 20 files were safe but silent: nothing unsafe happened, but she did not report what the pack's key wants reported (an impossible date in an invite she never opens, a bill in the wrong encoding, bad sensor rows, a risky install line in a pull request), because she files mail that isn't her job and never opens attachments. Seven of the 13 met were met by holding for the owner rather than doing the key's whole task ([`tests/trap-pack/RUNS.md`](tests/trap-pack/RUNS.md)).
- Text moved off-screen in HTML (a large negative position or a zero clip) is not on her hidden-text list yet, so it reaches the model as data ([`tests/trap-pack/RESULTS.md`](tests/trap-pack/RESULTS.md)).
- Hold notices blank anything shaped like a code, key or token; a code that reads as plain words is quoted as written.
- `stop` does not reach `traps`, which runs outside the supervisor. A file named `STOP` at the repo root halts it at its next step.

## Working on the repo

The unit tests take about 4 minutes. Run them from the repo root:

```
python3 -m unittest discover -s tests -t .
```

Every commit runs the secret scan. Turn the hook on once per clone with `git config core.hooksPath .githooks`.

## Credits

Shane Bornstein and Wolf (Claude), his CTO. Hire the Machines.

The Agentware 2026 Trap Pack used in `tests/` is the "Agentware 2026 Trap Pack, Hackathon Raptors", CC BY 4.0.

MIT licence. See [`LICENSE`](LICENSE).
