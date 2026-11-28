# Tracker

Every item the judges score, one line each: **status** · item · owner · due (Pacific).
Updated Fri Nov 27, 8:40 PM. Kept current on main as items move.

Status words: **done** (on main, tested) · **in review** (built, in a pull request) · **building** · **blocked** (says on what) · **not started**.

## Tonight and the live runs

- **building** · PR #2 merged to main: live mail and calendar, the live loop, the digest, kill-switch hardening, JMAP sending · Wolf · Fri 11:30 PM
  - JMAP sending is on the `live` branch; two Windows CI fixes, a final review and CI remain.
- **building** · Tonight's end-to-end live smoke: a test mail to the owner, the forward to Doris, her reply over JMAP, the read-back · Wolf · Fri 10:00 PM
  - Run 1 (8:29 PM) failed at the first JMAP call, nothing sent: our host check refused Fastmail's regional API host. Fix in progress, then run 2. [Run 1 trace](https://github.com/EatnerBeav/doris/tree/traces/live-smoke-20261128T042934Z/traces/live-smoke-20261128T042934Z).
- **blocked** · Live booking · Wolf · Sat 2:00 PM
  - On PR #2 and the smoke.
- **blocked** · Overnight run · Wolf · Sat 8:00 PM start
  - On the live booking.
- **blocked** · Recorded answers from a real keyed demo run, so `demo` replays a real model · Wolf · Sat 12:00 PM, again at the freeze
  - On PR #2. Today the demo falls back to the keyword stand-in and says so.
- **not started** · Feature freeze and the video · Wolf with Shane · Sun evening

## Safety bonus: audit trail, live monitoring, kill switch, guardrails

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
- **in review** · SAFETY.md: the five verbs (send, delete, overwrite, pay, deploy) · docs thread · Sun final
  - PR #7, review-clean.
- **in review** · Only the supervisor starts a process (a test for SAFETY's deploy row) · docs thread
  - In PR #7.

## Reliability, engineering, honesty

- **done** · One calendar timeout with retry in the demo · Wolf
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

## Known limit, written down

`stop` and a HALT file don't reach `traps`, which runs the loop in its own process; only a STOP file at the repo root halts it, at its next step.
