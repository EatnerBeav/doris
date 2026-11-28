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
  - wolf_sends:
    - control characters in the subject should exit 2;
    - add the owner-is-not-Doris check;
    - fix the 3.9 RFC 2047 encoding of `--in-reply-to`;
    - print a Ctrl-C "may have gone" line.

## Running on our server

- **The soak:** tmux session `doris-live`, running `python3 -m doris run --live --fresh` in /root/doris/src since 10:52 PM (checkout detached at a13cadd).
  - PIDs: 2610638 shell, 2610640 run, 2610641 tee, 2610642 worker.
  - Log: /root/doris-live-run.log.
  - Receipts: /root/doris/state/live/runs/model-live-live-20261128T065251Z/.
- **Its trace:** when it stops, `run --live` writes `traces/live-run-<stamp>/` itself, pass or fail. It holds the receipts, terminal.log, a timeline and a manifest (mode live, start and end, commit, model, measured cost, counts, chain check). It is committed from our server whether it passed or not (decisions 87 and 101).
- **Who stops it:** the first fresh thread on Saturday morning (live ops), around 7 AM, through the Remote Control session on Shane's PC.
  1. Run `cd /root/doris/src && python3 -m doris stop`.
  2. Commit the trace on our server, on branch `traces/live-run-<stamp>`. The hook checks /root/.doris-scan-terms.
  3. Push it and fold it into main.
  4. Log it in the near-miss log and decisions.md.
  5. Add its path to AGENT.md's `run --live` row and to SAFETY.md's "Committed so far".

  Never push from Shane's PC.

## Open questions (answered Sat 12:48 AM, decision 103)

1. Scratch on our server and Shane's PC: leave it all alone until after Mon 10 AM.
2. Resolved (decision 106): Steve set the provider spend limit on the key.
3. The form's team field: the tracker's recommendation; Steve submits Sunday.
4. Repo description typo: Shane said fix it now; Wolf's tools can't change repo settings, so Shane does it at https://github.com/EatnerBeav/doris (gear beside About).
5. Resolved (decision 106): the ruling 13 law fix landed Sat 1:33 AM and now matches decision 77 (law 15,967 chars).
6. Keep the S2 line "Failed inbox reads: [k]."
7. #17's trade-offs accepted as they are.

## Next three from the tracker

1. Stop the soak and commit its trace (Sat morning).
2. Record answers from a real keyed demo run, so `demo` replays a real model (Sat 12:00 PM). No sandbox/answers.jsonl exists yet.
3. A fresh live-model Trap Pack run on merged main, with RESULTS.md graded against the key beside each verdict (Sat 12:00 PM).

After those, the Saturday 8 PM overnight run.
