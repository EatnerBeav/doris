# Tracker: every scored item

Updated Fri Nov 27, 6:20 PM Pacific. One line per item the judges score, from the project law (Never cut, commands, Dates) and the Nov 26 brief check. Times are Pacific.

Status words: **done** = in the code on main and tested; **built** = in the code, the proof still to show; **in progress**; **not started**; **blocked** (says on what).

| # | Item | Scores | Status | Owner thread | Due |
|---|---|---|---|---|---|
| 1 | Live sending over JMAP and tonight's end-to-end live smoke (wolf.whizz to whizz@, the forward to Doris, her reply through JMAP, the read-back) | Autonomy, Reliability | in progress: both tokens answer the JMAP session; sending being wired test-first on PR #2 | Kickoff (Wolf) | Fri night |
| 2 | `replay` (audit): step-through of one finished run, chain checked first | Audit trail +1 | not started; thread started tonight | Build the replay audit command | Sat 8:00 AM |
| 3 | Watch page: the four labels (current step, tool calls, decisions, cost so far) and a code-written why on every decision | Live monitoring +1 | built: every decided, checked, held and halted receipt carries a code-written why. Page not started; thread started tonight | Build the live watch page | Sat 10:00 AM |
| 4 | Halt receipt naming the call in flight and the dropped queue | Kill switch +1 | built: the halted line carries `in_flight` and `dropped`. To show on camera, Stop landing mid-call | Kickoff (Wolf) | Sat 4:00 PM rehearsal |
| 5 | Yes-man redteam proof: a model that obeys every injection, 0 bad actions | Guardrails +1, Reliability | not started; thread started tonight | Prove the gate with a yes-man model | Sat 12:00 PM |
| 6 | One calendar timeout with retry in the demo | Reliability, Demo | done: the demo's `timeout-once` fault; she retries and books | Kickoff (Wolf) | done |
| 7 | `stop` / `resume` | Kill switch +1 | done on main; PR #2 adds SIGTERM/SIGHUP, the lifeline and the `sending` state | Kickoff (Wolf) | PR #2 merge, Fri night |
| 8 | Booking re-check: right before the write, read back after | Reliability, never cut | done: gate G11 plus the re-read after the write (ruling 4) | Kickoff (Wolf) | done |
| 9 | Signature check | Guardrails, never cut | done: gate G8 compares the signature byte for byte | Kickoff (Wolf) | done |
| 10 | Fresh-clone run of the one command on three systems | Engineering | in progress. Hand runs from a fresh clone of main, Fri 6:05 PM: Linux server, `demo` 14.1 s and `traps` 20/20; Shane's Windows 11 PC, `demo` 13.6 s, but `traps` 1/20: its run paths pass Windows' 260-character limit (fix and a path-budget test on PR #2). CI to run `demo` and `traps` on all six legs (PR #5). macOS from CI only | Kickoff (Wolf) | Sat 10:00 AM |
| 11 | No-key line | Engineering, honesty | built: the demo's first line says the answers are replayed and no key was found. Wording of what a key costs to check | Kickoff (Wolf) | Sun 12:00 PM |
| 12 | Live or replayed mark on every trace | Audit trail, honesty | in progress: receipts carry their mode; live runs get a manifest (live, date, commit, measured cost) with tonight's smoke; demo and Trap Pack traces next | Kickoff (Wolf) | Sat 12:00 PM |
| 13 | Keys, tiers and cost table | Engineering | not started: cost is measured in tonight's smoke and Saturday's runs | README pass (Sunday) | Sun 6:00 PM |
| 14 | SAFETY.md: the five verbs (send, delete, overwrite, pay, deploy) | Guardrails +1 | not started; draft thread started tonight | Draft SAFETY.md and AGENT.md | Sat 6:00 PM draft, Sun final |
| 15 | AGENT.md permissions: each credential, its scope, what it cannot do | Engineering | not started; draft thread started tonight | Draft SAFETY.md and AGENT.md | Sat 6:00 PM draft, Sun final |
| 16 | README rubric-to-evidence map | Every criterion | not started: written from the traces and this tracker | README pass (Sunday) | Sun 6:00 PM |
| 17 | Live booking | Autonomy, Usefulness | blocked on item 1 | Kickoff (Wolf) | Sat 2:00 PM |
| 18 | Overnight run | Autonomy, Reliability, never cut | blocked on item 17 | Kickoff (Wolf) | Sat 8:00 PM start |
| 19 | Feature freeze and the video | Demo, Kill switch +1 | not started | Kickoff (Wolf) with Shane | Sun evening |
| 20 | Recorded answers from a real keyed demo run, so `demo` replays a real model (today it falls back to the keyword stub) | Autonomy, honesty | not started: a keyed demo run on the server after PR #2 merges, its answers and live trace committed from there; recorded again at the freeze, since answers replay only on the code they ran on | Kickoff (Wolf) | Sat 12:00 PM, again Sun at freeze |
| 21 | Follow-up nudge on an unanswered offer (T9) | Usefulness | not started: marked pending in AGENT.md | New thread after PR #2 merges | Sat 2:00 PM |
| 22 | Ruling 17's once-a-cycle check for a booking gone from the calendar | Reliability | not started: today it runs only on a resumed booking; marked pending in AGENT.md | New thread after PR #2 merges | Sat 2:00 PM |
| 23 | Only the supervisor starts a process (a source-tree test for SAFETY's deploy row) | Guardrails +1 | in progress | Draft SAFETY.md and AGENT.md | Sat 10:00 AM |

Open pull requests: #2 `live` (live adapters, live loop, digest, kill-switch hardening; JMAP sending lands here), #5 `demo-then-tests` (tests pass after a judge's demo), #6 this tracker, #7 SAFETY.md and AGENT.md (base `live`). Trap Pack run 2 sits on branch `trap-pack-run-2`, unchanged, as it ran.
