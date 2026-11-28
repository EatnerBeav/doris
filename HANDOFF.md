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

- **Branch `demo-fresh`** (c83e77f, pushed, no PR). It holds 8 commits on PR #17's 060242c, with 215 targeted tests passing on 3.x and 3.9; the full suite hasn't run. The commits:
  - a second `demo` after a finished day starts fresh and keeps the old day in `state/old/`; after a halt it continues the day instead;
  - an honest digest line;
  - a line when the staged timeout starts;
  - paths printed from the working folder;
  - help that says `python3`;
  - stand-in wording and template names;
  - #17's two review fixes: the meeting-link check on raw bodies, and `stop` trying every mode.

  **Next:**
  1. Merge main in.
  2. Wire the story at the end of `demo()`: after `code = supervise(...)`, add `from . import story` and `for line in story.lines(os.path.join(run_dir, "receipts.jsonl")): _say(line)`, then `return code`. Add a test.
  3. Open a PR with base main and get one Fable review.
  4. Merge on green CI.
  5. Tell the README thread.
- **PR #19** (README, draft) waits on demo-fresh. **PR #20** (video docs) and **PR #21** (brief-compliance.md) belong to their threads.
- **Queued lows:**
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

## Open questions

1. **Shane:** OK to delete the leftover scratch on our server? That's /root/doris-fresh-20261128T020557Z, /root/doris-jmap, /root/doris-merge-pr6-20261128T042704Z, /root/doris-traps-run2, /tmp/jmap_hosts_check.py and /tmp/jmap_session_check.py, plus a doris-fresh folder in his PC's Temp.
2. **Shane:** the provider spend limit on the key.
3. **Shane:** the form's team field (recommendation on the tracker).
4. **Shane:** OK to fix the repo description typo.
5. **Shane:** ruling 13 conflicts with decision 77; the one-line law fix has been sent to him.
6. **Steve or Wolf:** keep the S2 line "Failed inbox reads: [k]." (decision 89)?
7. **Wolf:** confirm #17's trade-offs:
   - files already in the inbox at the first look are read as they are;
   - one extra IMAP SEARCH per fetch;
   - the legacy smoke cleanup pins two 2028 dates.

## Next three from the tracker

1. Stop the soak and commit its trace (Sat morning).
2. Record answers from a real keyed demo run, so `demo` replays a real model (Sat 12:00 PM). No sandbox/answers.jsonl exists yet.
3. A fresh live-model Trap Pack run on merged main, with RESULTS.md graded against the key beside each verdict (Sat 12:00 PM).

After those, the Saturday 8 PM overnight run.
