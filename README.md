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

The banner on the first line says what read the mail. With no key, she reads the mail with the model's own answers, replayed from a real keyed run of this same day: `sandbox/answers.jsonl` holds them, the banner names the run, and every receipt says `replayed`. That run's trace, with its measured cost, is [`traces/model-live-sandbox-20261128T123315Z/`](traces/model-live-sandbox-20261128T123315Z/). An email with no recorded answer (one you add yourself) is held, never guessed. Without that file, a built-in keyword stand-in reads the mail and every receipt says `stub`. With `ANTHROPIC_API_KEY` set, the model is called live (see [With a key](#with-a-key)).

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

## With a key

None of the above needs a key. To have the model read the mail, set an Anthropic API key in the environment and run the demo:

- bash: `export ANTHROPIC_API_KEY=...`
- PowerShell: `$env:ANTHROPIC_API_KEY="..."`

Then run `python3 -m doris demo`. The banner says the model is called live, and each line shows the cost of its call.

- **Model:** `claude-sonnet-5-5`, one model, called with `urllib` (no SDK).
- **Cost, measured so far:** $0.004 for one email (live smoke run 3, [`traces/live-smoke-20261128T055002Z/`](traces/live-smoke-20261128T055002Z/)), $0.034 for the demo day's 9 calls ([`traces/model-live-sandbox-20261128T123315Z/`](traces/model-live-sandbox-20261128T123315Z/)) and $0.065 for the 20 Trap Pack files (run 3, [`tests/trap-pack/RUNS.md`](tests/trap-pack/RUNS.md)).
- **Limit:** a run stops itself at $5.00 of model spend (gate check G3).

The demo's 15 emails make 9 classification calls (the model's with a key, the stand-in's without); screens in code settle the other six before anything reads them.

## Evidence for each criterion

| Criterion | Where to see it |
|---|---|
| Autonomy | `demo` ends with `0 human approvals`, counted from the receipts. Holds are policy, labelled with their reason, and send nothing to the sender. Live: [`traces/live-smoke-20261128T055002Z/`](traces/live-smoke-20261128T055002Z/) is a real email forwarded to her, her reply sent and read back, with 0 approvals |
| Usefulness | The scheduling job end to end: `sandbox/outbox/` holds the real replies with `.ics` files, time zones written both ways, and the digest |
| Reliability | The staged calendar timeout (retried) and the mid-booking collision (recovered) in the demo, the quarantined email, `traps`, and the honest misses in [`tests/trap-pack/RUNS.md`](tests/trap-pack/RUNS.md) |
| Engineering | [AGENT.md](AGENT.md): six steps in one loop (`src/doris/loop.py`), standard library only, each credential and what it cannot do. Tests run on Linux, macOS and Windows in CI |
| Safety: guardrails | The gate (`src/doris/gate.py`), every irreversible action in [SAFETY.md](SAFETY.md), and `redteam` |
| Safety: live monitoring | `watch` |
| Safety: kill switch | `stop` mid-run, with the halt receipt |
| Safety: audit trail | `receipts.jsonl`, `verify` and `replay` |

## Live, on our own test accounts

Live, Doris works only with Fastmail test accounts we control. She reads mail over IMAP, sends over Fastmail's JMAP API, and uses CalDAV for the owner's shared calendar. A judge never needs this.

The setting names are in [`.env.example`](.env.example); what each credential can and cannot do is in [AGENT.md](AGENT.md#permissions). `python3 -m doris smoke` checks every live call once, and `python3 -m doris run --live` starts her. Every live run keeps its trace in `traces/live-*`, pass or fail.

## Known limits

- Google and Microsoft calendars need an OAuth adapter we did not build.
- The overnight run stays under 60 emails.
- Not built yet: the one follow-up nudge on an unanswered offer, and the check, once a cycle, that her future bookings are still on the calendar.
- `stop` does not reach `traps`, which runs outside the supervisor. A file named `STOP` at the repo root halts it at its next step.
- The Trap Pack's known limits are in [`tests/trap-pack/RUNS.md`](tests/trap-pack/RUNS.md).

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
