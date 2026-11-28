# Tracker

Every item the judges score, one line each: **status** · item · owner · due (Pacific).
Updated Fri Nov 27, 10:10 PM. Re-checked against the law's Never cut, commands and Dates and the Brief check tab after the smoke. Kept current on main as items move.

Status words: **done** (on main, tested) · **in review** (built, in a pull request) · **building** · **blocked** (says on what) · **not started**.

## Tonight and the live runs

- **building** · PR #2 merged to main: live mail and calendar, the live loop, the digest, kill-switch hardening, JMAP sending · Wolf · Fri 11:30 PM
  - Everything is on `live`, the live smoke passed on it, and the last review says merge. Left: CI, merge.
- **done** · Tonight's end-to-end live smoke: a test mail to the owner, the forward to Doris, her reply over JMAP, the read-back · Wolf · Fri 9:50 PM
  - Run 1 (8:29 PM) failed at the first JMAP call, nothing sent: our host check refused Fastmail's regional API host. Fixed. [Run 1 trace](https://github.com/EatnerBeav/doris/tree/traces/live-smoke-20261128T042934Z/traces/live-smoke-20261128T042934Z).
  - Run 2 (9:15 PM): the whole mail path passed live (forward to Doris in 31 s, her reply over JMAP for $0.004, read back in Sent and in wolf's inbox). Failed on the CalDAV test event (a reused test ID) and the secret scan wrongly blocked its trace on an address. Both fixed. [Run 2 trace](https://github.com/EatnerBeav/doris/tree/traces/live-smoke-20261128T051500Z/traces/live-smoke-20261128T051500Z).
  - Run 3 (9:50 PM) passed, every step: calendar write and read-back, the forward in 31 s, her reply over JMAP with the live model for $0.004, read back in her Sent and in wolf's inbox. 0 human approvals. [Run 3 trace](https://github.com/EatnerBeav/doris/tree/traces/live-smoke-20261128T055002Z/traces/live-smoke-20261128T055002Z).
- **blocked** · Live booking · Wolf · Sat 2:00 PM
  - On PR #2's merge; the smoke passed.
- **blocked** · Overnight run, under 60 emails (never cut; a sandbox run is the fallback) · Wolf · Sat 8:00 PM start
  - On the live booking.
- **blocked** · Recorded answers from a real keyed demo run, so `demo` replays a real model · Wolf · Sat 12:00 PM, again at the freeze
  - On PR #2. Today the demo falls back to the keyword stand-in and says so.
- **not started** · Fresh live-model Trap Pack run on merged main, RESULTS.md and RUNS.md to match · Wolf · Sat 12:00 PM
  - On PR #2. The committed report is run 1's; run 2 (20/20) is on its own branch.
- **not started** · Feature freeze and the video: the full unattended run, then a fresh run and the Stop landing mid-call; unlisted YouTube checked in a signed-out window, link in the README · Wolf with Shane · Sun evening
- **not started** · The submission form (team, T1, repo, video link) · Shane · Sun night

## Safety bonus: audit trail, live monitoring, kill switch, guardrails

- **done** · `verify`: chain check plus the tamper demo · Wolf
- **in review** · `replay` (audit): step-through of any finished run, chain checked first · replay thread · merges with PR #2
  - PR #8, review-clean, CI green.
- **in review** · Watch page: the four labels and a code-written why on every decision, one Stop button · watch thread · merges with PR #2
  - PR #9, review-clean, CI green. Every decision receipt already carries its why.
- **done** · Halt receipt names the call in flight and the dropped queue · Wolf · on camera at Sat 4:00 PM rehearsal
- **done** · `stop` / `resume` · Wolf
  - PR #2 adds SIGTERM/SIGHUP, the lifeline and the `sending` state.
- **in review** · Yes-man redteam proof: a model that obeys every injection, 0 bad actions · redteam thread · Sat 12:00 PM
  - PR #10 (draft): 0 bad actions in 152 runs over 24 cases.
- **done** · Signature check (gate G8) · Wolf
- **done** · Booking re-check before the write, read back after (gate G11, ruling 4) · Wolf
- **done** · Sensitive-mail screen before the model: filed unread, no reply, a sender-only digest line · Wolf
- **in review** · The digest: once a day, the ratio first, only what needs the owner · Wolf · merges with PR #2
- **not started** · Model bill capped at the provider too, by a spend limit Shane sets on the key (the code cap is done) · Shane · Sat 12:00 PM
- **blocked** · The never-mention list checked on trace commits from our server · Wolf · on Shane's yes
  - Found tonight: the server's secret scan had no private terms file, so runs 1 to 3 were checked for keys and .env values only.
- **in review** · SAFETY.md: the five verbs (send, delete, overwrite, pay, deploy) · docs thread · Sun final
  - PR #7, review-clean.
- **in review** · Only the supervisor starts a process (a test for SAFETY's deploy row) · docs thread
  - In PR #7.

## Reliability, engineering, honesty

- **done** · One calendar timeout with retry in the demo · Wolf
- **done** · Trap Pack report: every input, what she did, the outcome in the rubric's words (recover, retry, ask, stop cleanly, refused, not inspected), a plain line on each failure · Wolf
- **done** · `.agentware.toml`, LICENSE (MIT) and the organizers' repo layout · Wolf
- **in review** · The one command from a fresh clone on three systems · Wolf · Sat 10:00 AM
  - CI runs `demo` and `traps` on Linux, macOS and Windows on every push. By hand: Linux all pass; Windows `demo` passes, `traps` failed on paths over 260 characters, fixed in PR #2.
- **done** · No-key line in the demo · Wolf
  - Wording of what a key costs to check on Sunday.
- **building** · Live or replayed mark on every trace · Wolf · Sat 12:00 PM
  - Live runs write a manifest (live, date, commit, measured cost); demo and Trap Pack traces next.
- **building** · `terminal.log` in every trace folder · Wolf · Sat 10:00 AM
  - Live traces carry it; the 40 committed Trap Pack traces have empty ones, fix queued.
- **not started** · `traps` with no key replays a recorded live run, and leaves the committed RESULTS.md alone · Wolf · Sat 12:00 PM
- **in review** · AGENT.md permissions: each credential, its scope, what it cannot do · docs thread · Sun final
  - PR #7.
- **not started** · Follow-up nudge on an unanswered offer (T9) · new thread after PR #2 · Sat 2:00 PM
- **not started** · Ruling 17's once-a-cycle check for a booking gone from the calendar · new thread after PR #2 · Sat 2:00 PM
- **not started** · Keys, tiers and cost table · README pass · Sun 6:00 PM
- **not started** · README rubric-to-evidence map · README pass · Sun 6:00 PM
- **not started** · README: the 60-second run, known limits (no Google or Microsoft calendar, overnight under 60 emails), optional live setup never asked of a judge · README pass · Sun 6:00 PM

## Known limit, written down

`stop` and a HALT file don't reach `traps`, which runs the loop in its own process; only a STOP file at the repo root halts it, at its next step.
