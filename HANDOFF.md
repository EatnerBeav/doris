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

## Our server after the soak (Sat Nov 28, 7:05 AM Pacific)

- **The soak is stopped and its trace is on main:** [traces/live-run-20261128T065251Z/](traces/live-run-20261128T065251Z/). Fri 10:52 PM to Sat 7:05 AM on a13cadd: 2 handled (the offer, then the booking Wolf picked), 0 held, 0 errors, 0 human approvals, 3 sends (digest, T4, T1), 2 model calls, $0.0076. `stop` landed in 1 s; the worker was dead 59 ms later, nothing in flight, 0 dropped. Outcome pass; 153 lines, chain verified on our server and on main's code. Committed from our server by /mnt/project-files/doris-v2/ops/doris_stop_soak_on_wide.sh (Remote Control on Shane's PC, which never pushed).
- **HALT is in force in /root/doris, sandbox and live.** The next live run there starts only after `cd /root/doris/src && python3 -m doris resume` on our server. The live ledger in /root/doris/state/live/ is kept (no `--fresh` next time: it already exists). tmux `doris-live` closed when the run ended.
- **Smoke age:** the newest passing smoke is Fri 9:50 PM, so `run --live` refuses to start after Sat 9:50 PM until a new smoke passes (overnight.sh in PR #31 runs one first).
- /root/doris is still detached at a13cadd: check out main before the next live run. Left on our server (scratch, per decision 103): /root/doris-soak-trace (the clone that committed), /root/doris_stop_soak_on_wide.sh, /root/doris-stop-soak.log.

## Open questions (answered Sat 12:48 AM, decision 103)

1. Scratch on our server and Shane's PC: leave it all alone until after Mon 10 AM.
2. Resolved (decision 106): Steve set the provider spend limit on the key.
3. The form's team field: the tracker's recommendation; Steve submits Sunday.
4. Resolved: Shane fixed the repo description typo; it now reads "walk away from".
5. Resolved (decision 106): the ruling 13 law fix landed Sat 1:33 AM and now matches decision 77 (law 15,967 chars).
6. Keep the S2 line "Failed inbox reads: [k]."
7. #17's trade-offs accepted as they are.

## Next three from the tracker

1. Done Sat 7:05 AM: the soak stopped, its trace on main (above).
2. Done Sat 4:33 AM (PR #29, decision 107): `sandbox/answers.jsonl` from a keyed demo on our server, $0.0337. Re-record with a keyed `demo` after any change to the model request (prompt, model, fields) and once at the freeze; `test_cli.RecordedAnswers` fails until you do.
3. Done Sat 4:33 AM (PR #29): Trap Pack run 3 on main, $0.0649, graded against the key in RESULTS.md and RUNS.md. A new `traps --record` regenerates RESULTS.md and drops the hand-added "Against the key" column: grade it again, and update the costs and trace paths in README, AGENT.md, SAFETY.md and tracker.md.

After those, the Saturday 8 PM overnight run.
