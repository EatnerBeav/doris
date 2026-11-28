# Tracker

Every item the judges score, one line each: **status** · item · owner · due (Pacific).
Updated Sat Nov 28, 1:00 AM (demo-fresh thread: PR #23 merged, Shane's answers to HANDOFF.md, decision 103). Re-checked against the law's Never cut, commands and Dates and the Brief check tab after the smoke. Kept current on main as items move.

Status words: **done** (on main, tested) · **in review** (built, in a pull request) · **building** · **blocked** (says on what) · **not started**.

## Tonight and the live runs

- **done** · PR #2 merged to main: live mail and calendar, the live loop, the digest, kill-switch hardening, JMAP sending · Wolf · Fri 10:05 PM
  - CI green on all six legs; the three smoke traces are on main. Tonight's status: [status/2026-11-27.md](status/2026-11-27.md).
- **done** · Tonight's end-to-end live smoke: a test mail to the owner, the forward to Doris, her reply over JMAP, the read-back · Wolf · Fri 9:50 PM
  - Run 1 (8:29 PM) failed at the first JMAP call, nothing sent: our host check refused Fastmail's regional API host. Fixed. [Run 1 trace](https://github.com/EatnerBeav/doris/tree/traces/live-smoke-20261128T042934Z/traces/live-smoke-20261128T042934Z).
  - Run 2 (9:15 PM): the whole mail path passed live (forward to Doris in 31 s, her reply over JMAP for $0.004, read back in Sent and in wolf's inbox). Failed on the CalDAV test event (a reused test ID) and the secret scan wrongly blocked its trace on an address. Both fixed. [Run 2 trace](https://github.com/EatnerBeav/doris/tree/traces/live-smoke-20261128T051500Z/traces/live-smoke-20261128T051500Z).
  - Run 3 (9:50 PM) passed, every step: calendar write and read-back, the forward in 31 s, her reply over JMAP with the live model for $0.004, read back in her Sent and in wolf's inbox. 0 human approvals. [Run 3 trace](https://github.com/EatnerBeav/doris/tree/traces/live-smoke-20261128T055002Z/traces/live-smoke-20261128T055002Z).
- **done** · Live booking: wolf asks for a meeting, Doris books it on the owner's calendar and replies · Wolf · Fri 11:20 PM
  - Wolf asked for Wed Dec 2, 10 AM; the owner was busy 9-11, so she offered 3 times (T4). Wolf picked 11:00; she re-checked, booked it, read it back CONFIRMED (11:00-11:30 AM Pacific), sent T1 with the .ics, found in Sent. 0 held, 0 errors, 0 approvals, $0.0076. Main a13cadd. Trace lands with the soak's.
- **building** · Short overnight soak: the same live run left going since 10:52 PM, a pass every 10 minutes · Wolf · stop and commit Sat morning
  - Its trace (traces/live-run-<stamp>/, marked live with date, commit and measured cost) is written when it stops and committed from our server, pass or fail. Steps in HANDOFF.md.
- **not started** · Overnight run, under 60 emails (never cut; a sandbox run is the fallback) · Wolf · Sat 8:00 PM start
  - Unblocked: the live booking passed tonight. Stays the main overnight run.
- **not started** · Recorded answers from a real keyed demo run, so `demo` replays a real model · Wolf · Sat 12:00 PM, again at the freeze
  - Unblocked. Today the demo falls back to the keyword stand-in and says so.
- **not started** · Fresh live-model Trap Pack run on merged main, RESULTS.md and RUNS.md to match · Wolf · Sat 12:00 PM
  - Unblocked. The committed report is run 1's; run 2 (20/20) is on its own branch.
- **not started** · Feature freeze and the video: the full unattended run, then a fresh run and the Stop landing mid-call; unlisted YouTube checked in a signed-out window, link in the README · Wolf with Shane · Sun evening
- **not started** · The submission form (team, T1, repo, video link) · Shane · Sun night

## The head start: Usefulness, Demo, a judge's first 15 minutes (Shane, Fri 10:49 PM)

No new features; the law and the scope stand. Bar: first place, and would Steve Jobs ship it.

- **in review** · Judge experience: a stranger runs the README's one command and understands what they saw in 60 seconds; fresh-clone runs on Linux and Windows, every stumble fixed · judge-experience thread · Sat 10:00 AM
  - README PR #19 (draft) and a judge-path CI walk. Unblocked: PR #23 merged (a rerun starts the day fresh, honest digest line, paths from where you run, template names, the story at the end). sandbox/README.md's "mail she has already read is skipped" is false after a finished day; its fix goes in #19, which owns that file.
- **done** · Demo value: when `demo` ends, the owner's view in plain words, all from the receipts · demo-value thread · Sat 12:00 PM
  - story.py in PR #18, printed at the end of `demo` by PR #23 (decision 102).
- **not started** · The same story as a section on the watch page · demo-value thread · Sat 2:00 PM
  - Built and tested but not on GitHub: /mnt/project-files/doris-v2/patches/watch-story-section.patch, to rebase on main and open as its own PR with one Fable review.
- **in review** · The video: a 3:00-max script and shot list Shane narrates; credit Shane Bornstein and Wolf (Claude), his CTO; Hire the Machines only · video thread · rehearsal Sat afternoon, filming Sun
  - PR #20 (docs/video/), decision 99; Shane to confirm.

## From the brief check (brief-compliance.md, PR #21: 75 rows, 50 met, 14 partly, 9 missing, 2 wrong)

- **not started** · `demo`, `traps` and the sandbox `run` read the model key from a `.env` at the repo root as well as the environment; the README gives the step (row 1.2, wrong today) · Wolf · Sat 12:00 PM
- **not started** · tests/trap-pack/RESULTS.md carries the grade against the key beside each generated verdict and links RUNS.md; run 2's trace lands on main beside run 1's (rows 9.3 wrong, 9.4, 9.5) · Wolf · Sat 12:00 PM
- **not started** · The follow-up named in the receipt and the digest when an offer is filed (row 6.2; with the nudge) · nudge thread · Sat 2:00 PM
- **not started** · A loop test pinning the calendar failing twice into a clean hold (behaviour right, no test) · Wolf · Sat 2:00 PM
- **done** · The form's team field: the one registered person in the field, "Wolf (Claude), his CTO" in the free-text fields (row 10.2; Shane's call, decision 103) · Steve submits · Sun night
- **not started** · Daily check of the registration inbox for judges' questions, Nov 30 to Dec 10 (row 5.8) · Wolf (project chat) · Mon 9:30 AM
- **not started** · Near Miss write-up drafted from the log, posted and tagged (row 10.3) · Wolf drafts, Shane posts · draft Fri Dec 4, post by Mon Dec 7 10:00 AM
- **blocked** · GitHub repo description reads "walk awayfrom" (missing space); Shane said fix it now (decision 103), but repo settings can't be changed with Wolf's tools: Shane fixes it at https://github.com/EatnerBeav/doris (gear beside About) · Shane · Sat

- **blocked** · The law's ruling 13 conflicts with decision 77; the one-line law fix is queued for Steve's morning (decision 103), built around · Steve · Sat morning

## Safety bonus: audit trail, live monitoring, kill switch, guardrails

- **done** · `verify`: chain check plus the tamper demo · Wolf
  - PR #25 (decision 105): after a re-save with CRLF line endings (PowerShell's `Set-Content`), `verify` and `replay` still name the line that was really edited, and so do `watch` and the story. A CRLF file never verifies. Each message says its seq once.
  - Queued low: a re-save in another encoding (Windows PowerShell 5.1's `>` and `Out-File` write UTF-16; `-Encoding UTF8` adds a byte-order mark) still reads "BROKEN at seq 1: the line is not valid JSON". Name the encoding in the reason.
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
- **blocked** · Model bill capped at the provider too, by a spend limit set on the key (the code cap is done); queued for Steve's morning (decision 103), built around · Steve · Sat morning
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
- **done** · `terminal.log` in every trace folder · Wolf
  - Live traces and new Trap Pack runs carry it (PR #17). The committed old traces stay as they ran.
- **done** · `traps` with no key leaves the committed RESULTS.md alone (`--record` for our committed runs) · Wolf
  - PR #17. Replaying a recorded live run waits on recorded answers. `redteam --record` is on main too (PR #13).
- **done** · AGENT.md permissions: each credential, its scope, what it cannot do · docs thread · final pass Sun
  - PR #7, merged in PR #11.
- **not started** · Follow-up nudge on an unanswered offer (T9) · new thread after PR #2 · Sat 2:00 PM
- **not started** · Ruling 17's once-a-cycle check for a booking gone from the calendar · new thread after PR #2 · Sat 2:00 PM
- **not started** · Keys, tiers and cost table · README pass · Sun 6:00 PM
- **not started** · README rubric-to-evidence map · README pass · Sun 6:00 PM
- **not started** · README: the 60-second run, known limits (no Google or Microsoft calendar, overnight under 60 emails), optional live setup never asked of a judge · README pass · Sun 6:00 PM

## Known limit, written down

`stop` and a HALT file don't reach `traps`, which runs the loop in its own process; only a STOP file at the repo root halts it, at its next step.
