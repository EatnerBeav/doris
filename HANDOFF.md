# Handoff

From the kickoff thread (Wolf), Fri Nov 27, 11:55 PM Pacific. That thread is closed. New work starts in fresh threads from this file, [tracker.md](tracker.md), the law and MEMORY.md.

## Merged on main tonight

- PR #2: live mail, calendar, loop, digest and JMAP sending.
- PR #11: replay, watch and redteam, wired into the CLI, plus the docs.
- PR #12: the test sender, `tests/tools/wolf_sends.py`.
- PRs #13, #14 and #16: follow-ups to redteam, replay and watch. The redteam proof was re-recorded with `--record`: [traces/model-yes-man-throwaway-redteam-20261128T0702Z/](traces/model-yes-man-throwaway-redteam-20261128T0702Z/).
- PR #15: the docs.
- PR #17: the 13 follow-ups after PR #2.
- PR #18: `story.py`.
- PR #22: CI trims.
- Live smoke run 3 passed: [traces/live-smoke-20261128T055002Z/](traces/live-smoke-20261128T055002Z/).
- First live booking passed, on main a13cadd (decision 101). Wolf asked for 10 AM, which was busy, so Doris offered three times. Wolf picked 11:00. She re-checked the slot, booked it, read it back CONFIRMED, and sent T1 with the .ics. 0 approvals, $0.0076.

## Open, and its exact state

- **PR #23 (branch `demo-fresh`) merged** Sat Nov 28 by the demo-fresh thread (decisions 100 and 102): a second `demo` after a finished day starts fresh and keeps the old day in `state/old/`; the owner's story prints at the end of `demo`; a half-moved day is refused, never sent twice; the honest digest line and the terminal wording. One Fable review, its three real findings fixed; full suite 2,234 passed; CI green on all legs.
- **PR #19** (README, draft) is unblocked; it also owns the fix to sandbox/README.md's "mail she has already read is skipped", false after a finished day. **PR #20** (video docs) and **PR #21** (brief-compliance.md) belong to their threads.
- **Queued lows:**
  - From PR #23's review: the fresh-day move doesn't hold ledger.lock; `stop` calls an unreadable HALT "NOT halted" (the supervisor treats it as halted); the story's "sandbox/outbox/" and "python -m doris" don't paste from src/; the halted-run CLI test races a stopper thread.
  - `stop` says "killed mid-call" even for the smoke's unsupervised loop.
  - The supervisor should re-read HALT after its last refused replace.
  - `smoke_left_out` should search positively.
  - A fetch `MailError` always records the step as "fetch".
  - `cp -p` copies keep their old mtime.
  - The fetch receipt needs a "new" count (near-miss 23:10).
  - From PR #29's review: a hold notice's draft reply is always her offer of meeting times, even for a non-meeting hold (Trap Pack 18, a docs request); the no-model hold a judge's own email gets with no key could name the key to add.
  - wolf_sends:
    - control characters in the subject should exit 2;
    - add the owner-is-not-Doris check;
    - fix the 3.9 RFC 2047 encoding of `--in-reply-to`;
    - print a Ctrl-C "may have gone" line.

## Our server after the full live loop (Sat Nov 28, 9:25 AM Pacific)

- **The live loop passed and its trace is on this PR's branch:** [traces/live-run-20261128T153848Z/](traces/live-run-20261128T153848Z/) and the mix's log [traces/live-loop-20261128T153848Z/](traces/live-loop-20261128T153848Z/). Sat 7:38 to 9:25 AM on main 5be0658: wolf's 15-email demo-day mix ([tests/tools/live_mix.py](tests/tools/live_mix.py)), 15 of 15 answered as planned; 14 handled, 1 held, 0 errors, 0 human approvals, 11 sends, 13 model calls, $0.0485. `stop` left the worker dead 71 ms later, nothing in flight. Run with [tests/tools/live_loop.sh](tests/tools/live_loop.sh) (start, status, finish) through Remote Control on Shane's PC, which never pushed. The soak's trace stays on main: [traces/live-run-20261128T065251Z/](traces/live-run-20261128T065251Z/).
- **HALT is in force in /root/doris, sandbox and live.** The next live run (the 2:30 PM rehearsal's Rae dry run, or the 8 PM overnight) starts only after `cd /root/doris/src && python3 -m doris resume` on our server. The live ledger is kept: never `--fresh`.
- **/root/doris is detached at 5be0658 with nothing untracked:** the run's own trace copy and the soak's were moved aside to /root/doris-live-loop/aside/ (a trace folder main also holds blocks `git checkout` of main). Do the same after any live run whose trace gets merged.
- **Her live bookings now on the owner's calendar (never delete one by hand: ruling 17 sends the owner a hold notice):** Mon Nov 30 9:00 and 9:30 AM, Tue Dec 1 10:00 to 10:45 AM, Wed Dec 2 11:00 AM (the soak's, doris-763d37f7fc1a3fb145c2fa09) and 12:00 PM Pacific. None on the video's Rae times (Thu Dec 3, Fri Dec 4).
- **Ledger schema 3 to 4** (the follow-up PR, due ~2 PM) migrates the live ledger in place on first open, and older commits then refuse it: copy /root/doris/state/live/ledger.sqlite aside before the first run on that main (likely the 8 PM overnight).
- **Smoke age:** the newest passing smoke is Fri 9:50 PM, so `run --live` refuses after Sat 9:50 PM until a new smoke passes (overnight.sh runs one first). The 2:30 PM rehearsal is inside the window.
- Scratch on our server (decision 103, leave until after Mon 10 AM): /root/doris-live-loop (logs, aside/), /root/doris-live-loop-trace (the clone that committed), /root/doris_live_loop_on_wide.sh (+ .bak), /root/doris-live-loop-start.log, -finish.log, -finish-2.log; from the soak: /root/doris-soak-trace, /root/doris_stop_soak_on_wide.sh, /root/doris-stop-soak.log.

## Open questions (answered Sat 12:48 AM, decision 103)

1. Scratch on our server and Shane's PC: leave it all alone until after Mon 10 AM.
2. Resolved (decision 106): Steve set the provider spend limit on the key.
3. The form's team field: the tracker's recommendation; Steve submits Sunday.
4. Resolved: Shane fixed the repo description typo; it now reads "walk away from".
5. Resolved (decision 106): the ruling 13 law fix landed Sat 1:33 AM and now matches decision 77 (law 15,967 chars).
6. Keep the S2 line "Failed inbox reads: [k]."
7. #17's trade-offs accepted as they are.

## Next three from the tracker

1. Done Sat 7:05 AM: the soak stopped, its trace on main. Done Sat 9:25 AM: the full live loop (above).
2. Done Sat 4:33 AM (PR #29, decision 107): `sandbox/answers.jsonl` from a keyed demo on our server, $0.0337. Re-record with a keyed `demo` after any change to the model request (prompt, model, fields) and once at the freeze; `test_cli.RecordedAnswers` fails until you do.
3. Done Sat 4:33 AM (PR #29): Trap Pack run 3 on main, $0.0649, graded against the key in RESULTS.md and RUNS.md. A new `traps --record` regenerates RESULTS.md and drops the hand-added "Against the key" column: grade it again, and update the costs and trace paths in README, AGENT.md, SAFETY.md and tracker.md.

After those, the Saturday 8 PM overnight run.
