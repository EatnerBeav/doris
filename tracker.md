# Tracker

Every item the judges score, one line each: **status** · item · owner · due (Pacific).
Updated Fri Nov 27, 10:55 PM. Re-checked against the law's Never cut, commands and Dates and the Brief check tab after the smoke. Kept current on main as items move.

Status words: **done** (on main, tested) · **in review** (built, in a pull request) · **building** · **blocked** (says on what) · **not started**.

## Tonight and the live runs

- **done** · PR #2 merged to main: live mail and calendar, the live loop, the digest, kill-switch hardening, JMAP sending · Wolf · Fri 10:05 PM
  - CI green on all six legs; the three smoke traces are on main. Tonight's status: [status/2026-11-27.md](status/2026-11-27.md).
- **done** · Tonight's end-to-end live smoke: a test mail to the owner, the forward to Doris, her reply over JMAP, the read-back · Wolf · Fri 9:50 PM
  - Run 1 (8:29 PM) failed at the first JMAP call, nothing sent: our host check refused Fastmail's regional API host. Fixed. [Run 1 trace](https://github.com/EatnerBeav/doris/tree/traces/live-smoke-20261128T042934Z/traces/live-smoke-20261128T042934Z).
  - Run 2 (9:15 PM): the whole mail path passed live (forward to Doris in 31 s, her reply over JMAP for $0.004, read back in Sent and in wolf's inbox). Failed on the CalDAV test event (a reused test ID) and the secret scan wrongly blocked its trace on an address. Both fixed. [Run 2 trace](https://github.com/EatnerBeav/doris/tree/traces/live-smoke-20261128T051500Z/traces/live-smoke-20261128T051500Z).
  - Run 3 (9:50 PM) passed, every step: calendar write and read-back, the forward in 31 s, her reply over JMAP with the live model for $0.004, read back in her Sent and in wolf's inbox. 0 human approvals. [Run 3 trace](https://github.com/EatnerBeav/doris/tree/traces/live-smoke-20261128T055002Z/traces/live-smoke-20261128T055002Z).
- **building** · Live booking: wolf asks for a meeting at an exact time, Doris books it on the owner's calendar and replies · Wolf · pulled forward to Fri night
  - PRs #11 and #12 merged (10:48 PM). The test sender tool is on main; the live run is starting on our server now.
- **not started** · Short overnight soak tonight, the same live run left going, trace committed in the morning (extra evidence) · Wolf · Fri night to Sat morning
  - Only if the booking checks out.
- **blocked** · Overnight run, under 60 emails (never cut; a sandbox run is the fallback) · Wolf · Sat 8:00 PM start
  - On the live booking (tonight). Stays the main overnight run.
- **not started** · Recorded answers from a real keyed demo run, so `demo` replays a real model · Wolf · Sat 12:00 PM, again at the freeze
  - Unblocked. Today the demo falls back to the keyword stand-in and says so.
- **not started** · Fresh live-model Trap Pack run on merged main, RESULTS.md and RUNS.md to match · Wolf · Sat 12:00 PM
  - Unblocked. The committed report is run 1's; run 2 (20/20) is on its own branch.
- **not started** · Feature freeze and the video: the full unattended run, then a fresh run and the Stop landing mid-call; unlisted YouTube checked in a signed-out window, link in the README · Wolf with Shane · Sun evening
- **not started** · The submission form (team, T1, repo, video link) · Shane · Sun night

## The head start: Usefulness, Demo, a judge's first 15 minutes (Shane, Fri 10:49 PM)

No new features; the law and the scope stand. Bar: first place, and would Steve Jobs ship it.

- **building** · Judge experience: a stranger runs the README's one command and understands what they saw in 60 seconds. A zero-context agent does a fresh clone on Linux and Windows, follows only the README, times it and logs every stumble; every stumble fixed · judge-experience thread · Sat 10:00 AM
- **building** · Demo value: when `demo` ends, the owner's view in plain words, all from the receipts (meetings booked, replies sent, holds and why, the digest, the cost, approvals 0); the same story on the watch page · demo-value thread · Sat 12:00 PM
- **building** · The video: a 3:00-max script and shot list Shane narrates (the problem in 10 s, one full unattended live run, one hold as a feature, the Trap Pack, Stop landing mid-call with the halt receipt on screen); credit Shane Bornstein and Wolf (Claude), his CTO; Hire the Machines only · video thread · rehearsal Sat afternoon, filming Sun

## Safety bonus: audit trail, live monitoring, kill switch, guardrails

- **done** · `verify`: chain check plus the tamper demo · Wolf
- **done** · `replay` (audit): step-through of any finished run, chain checked first · replay thread
  - PR #8, merged in PR #11 with the command wired (Fri 10:48 PM).
- **done** · Watch page: the four labels and a code-written why on every decision, one Stop button · watch thread
  - PR #9, merged in PR #11 with the command wired. Every decision receipt carries its why.
- **done** · Halt receipt names the call in flight and the dropped queue · Wolf · on camera at Sat 4:00 PM rehearsal
- **done** · `stop` / `resume` · Wolf
  - PR #2 adds SIGTERM/SIGHUP, the lifeline and the `sending` state.
- **done** · Yes-man redteam proof: a model that obeys every injection, 0 bad actions · redteam thread
  - PR #10, merged in PR #11: 0 bad actions in 206 runs over 24 cases.
- **done** · Signature check (gate G8) · Wolf
- **done** · Booking re-check before the write, read back after (gate G11, ruling 4) · Wolf
- **done** · Sensitive-mail screen before the model: filed unread, no reply, a sender-only digest line · Wolf
- **done** · The digest: once a day, the ratio first, only what needs the owner · Wolf
- **not started** · Model bill capped at the provider too, by a spend limit Shane sets on the key (the code cap is done) · Shane · Sat 12:00 PM
- **done** · The never-mention list checked on trace commits from our server · Wolf
  - Found tonight: the server's scan had no private terms file. Added (Shane's yes); the whole repo and Trap Pack run 2's trace re-scanned clean.
- **done** · SAFETY.md: the five verbs (send, delete, overwrite, pay, deploy) · docs thread · final pass Sun
  - PR #7, merged in PR #11.
- **done** · Only the supervisor starts a process (a test for SAFETY's deploy row) · docs thread
  - PR #7, merged in PR #11.

## Reliability, engineering, honesty

- **done** · One calendar timeout with retry in the demo · Wolf
- **done** · Trap Pack report: every input, what she did, the outcome in the rubric's words (recover, retry, ask, stop cleanly, refused, not inspected), a plain line on each failure · Wolf
- **done** · `.agentware.toml`, LICENSE (MIT) and the organizers' repo layout · Wolf
- **in review** · The one command from a fresh clone on three systems · Wolf, then the judge-experience thread · Sat 10:00 AM
  - CI runs `demo` and `traps` on Linux, macOS and Windows on every push, all green on main. By hand: Linux all pass; Windows `traps` failed on paths over 260 characters, fixed on main; a re-run on Shane's PC is next.
- **done** · No-key line in the demo · Wolf
  - Wording of what a key costs to check on Sunday.
- **building** · Live or replayed mark on every trace · Wolf · Sat 12:00 PM
  - Live runs write a manifest (live, date, commit, measured cost); demo and Trap Pack traces next.
- **building** · `terminal.log` in every trace folder · Wolf · Sat 10:00 AM
  - Live traces carry it; new Trap Pack runs get it in the follow-up PR (being built). The committed old traces stay as they ran.
- **building** · `traps` with no key leaves the committed RESULTS.md alone (a `--record` flag for our committed runs), and replays a recorded live run · Wolf · Sat 12:00 PM
  - The first half is in the follow-up PR being built.
- **done** · AGENT.md permissions: each credential, its scope, what it cannot do · docs thread · final pass Sun
  - PR #7, merged in PR #11.
- **not started** · Follow-up nudge on an unanswered offer (T9) · new thread after PR #2 · Sat 2:00 PM
- **not started** · Ruling 17's once-a-cycle check for a booking gone from the calendar · new thread after PR #2 · Sat 2:00 PM
- **not started** · Keys, tiers and cost table · README pass · Sun 6:00 PM
- **not started** · README rubric-to-evidence map · README pass · Sun 6:00 PM
- **not started** · README: the 60-second run, known limits (no Google or Microsoft calendar, overnight under 60 emails), optional live setup never asked of a judge · README pass · Sun 6:00 PM

## Known limit, written down

`stop` and a HALT file don't reach `traps`, which runs the loop in its own process; only a STOP file at the repo root halts it, at its next step.
