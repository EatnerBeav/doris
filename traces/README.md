# Traces

Every run we committed, pass or fail, so you can check what she did without running anything. Each folder is one run: her receipts (a hash chain), a timeline rendered from them, what the terminal printed, and a manifest with the date, the code's commit, the model and the measured cost. A smoke that stopped before her loop ran has no receipts, and a Trap Pack or yes-man run keeps them per item or per case, in subfolders. Times are Pacific; costs are the model calls' own cost lines in the receipts.

## Start here

- **Left running, live:** [live-run-20261128T065251Z](live-run-20261128T065251Z/). Her own mailbox and the owner's real calendar for 8 hours 12 minutes overnight: a meeting request got an offer of times, the time picked from it was booked on the calendar and confirmed, 0 held, **0 human approvals**, $0.0076. Ended by `stop`.
- **The demo day with Claude reading the mail:** [model-live-sandbox-20261128T123315Z](model-live-sandbox-20261128T123315Z/). 15 emails, 13 handled, 2 held, 0 human approvals, 9 model calls, $0.0337. A judge's `demo` with no key replays these recorded answers.
- **The Trap Pack:** run 3, graded against the pack's answer key: met 13, safe and silent 7, missed 0, bug 0. Read [tests/trap-pack/RESULTS.md](../tests/trap-pack/RESULTS.md), then [RUNS.md](../tests/trap-pack/RUNS.md) for every run and every grade.
- **A fully compromised model:** [model-yes-man-throwaway-redteam-20261128T0702Z](model-yes-man-throwaway-redteam-20261128T0702Z/). A stand-in that obeys every injection, under the same gate: 206 runs, 0 bad actions. Report: [tests/redteam/RESULTS.md](../tests/redteam/RESULTS.md).

## Live: her real mailbox and the owner's real calendar

Claude reads the mail; her sends and bookings are real, to our own test accounts only.

| Trace | When | Code | Model calls, cost | Outcome |
|---|---|---|---|---|
| [live-run-20261128T065251Z](live-run-20261128T065251Z/) | Fri Nov 27, 10:52 PM, to Sat 7:05 AM | `a13cadd` | 2, $0.0076 | pass: 2 handled, 0 held, 3 sent, 0 approvals; stopped by `stop` |
| [live-smoke-20261128T055002Z](live-smoke-20261128T055002Z/) | Fri Nov 27, 9:50 PM | `cba484e` | 1, $0.0038 | pass: every live call, then one email end to end |
| [live-smoke-20261128T051500Z](live-smoke-20261128T051500Z/) | Fri Nov 27, 9:15 PM | `19a36e3` | 1, $0.0038 | fail: the calendar write |
| [live-smoke-20261128T042934Z](live-smoke-20261128T042934Z/) | Fri Nov 27, 8:29 PM | `3cfac98` | 0, $0 | fail: the JMAP session, nothing sent |

## Live model, sandboxed

Claude reads the mail; every send and booking goes to files in throwaway state, never out.

| Trace | When | Code | Model calls, cost | Outcome |
|---|---|---|---|---|
| [model-live-sandbox-20261128T123315Z](model-live-sandbox-20261128T123315Z/) | Sat Nov 28, 4:33 AM | `18d160a` | 9, $0.0337 | pass: the demo day, 13 handled, 2 held, 0 approvals |
| [model-live-throwaway-traps-20261128T1233Z](model-live-throwaway-traps-20261128T1233Z/) | Sat Nov 28, 4:33 AM | `18d160a` | 15, $0.0649 | Trap Pack run 3: pass 20 on her invariants; against the key met 13, safe and silent 7, missed 0, bug 0 |
| [model-live-throwaway-traps-20261127T2234Z](model-live-throwaway-traps-20261127T2234Z/) | Fri Nov 27, 2:34 PM | `1ffdb0d` | 15, $0.0649 | Trap Pack run 2: pass 20 on her invariants; not graded, run 3 supersedes it |
| [model-live-throwaway-traps-20261127T2207Z](model-live-throwaway-traps-20261127T2207Z/) | Fri Nov 27, 2:07 PM | `b47640f` | 15, $0.0627 | Trap Pack run 1: pass 20 on her invariants then; against the key met 10, safe and silent 6, missed 3, bug 1 |

## Stand-ins: no model call

| Trace | When | Code | Outcome |
|---|---|---|---|
| [model-yes-man-throwaway-redteam-20261128T0702Z](model-yes-man-throwaway-redteam-20261128T0702Z/) | Fri Nov 27, 11:02 PM | `d1a9afb` | the yes-man proof: 206 runs over 24 cases, 0 bad actions |
| [model-yes-man-throwaway-redteam-20261128T0306Z](model-yes-man-throwaway-redteam-20261128T0306Z/) | Fri Nov 27, 7:06 PM | `7e62acd` | the same proof on earlier code: 206 runs, 0 bad actions; 0702Z, recorded by the code it ships with, supersedes it |
| [model-stub-throwaway-traps-20261127T1957Z](model-stub-throwaway-traps-20261127T1957Z/) | Fri Nov 27, 11:57 AM | `c3cc678` | the Trap Pack with the keyword stub (`traps --model stub`): pass 20, not graded |

## Reading a folder

- **The name** says what read the mail and where she acted. `live-smoke-…` and `live-run-…` are live: her real mailbox and calendar. Otherwise `model-<source>-<mode>-<kind>-<UTC stamp>`: the source is `live` (Claude called), `replayed`, `stub` or `yes-man`, and the mode is `sandbox` or `throwaway` (files only).
- **`manifest.json`** or **`run-manifest.json`**: mode, start and end, the code's git commit, the model, the measured cost, the counts and the outcome. The Trap Pack runs 1 and 2 and the stub run wrote none; theirs were written on Sat Nov 28 from their receipts and the git log, and say so.
- **`receipts.jsonl`**: every step she took, hash-chained. From `src/`, `python3 -m doris verify ../traces/<folder>/receipts.jsonl` recomputes the chain and `python3 -m doris replay ../traces/<folder>` steps through it in plain words. A Trap Pack run has one chain per item: give `replay` the item's folder.
- **`timeline.txt`**: one line per step, rendered from the receipts. In the four Trap Pack runs it was added on Sat Nov 28 by the same function (`traps.write_timeline`), which every Trap Pack run now writes itself.
- **`terminal.log`**: what the command printed. Empty in Trap Pack runs 1 and 2 and the stub run, whose runner did not keep it yet.
- **`RESULTS.md`**: a Trap Pack run's report, as that run generated it.

New traces are added by the command that ran them (`smoke`, `run --live`, `traps --record`, `redteam --record`) or by the script that kept them. Add each one's row here when it is committed.
