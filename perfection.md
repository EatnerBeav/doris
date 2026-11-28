# Perfection list

One ranked list of what a mock judging panel says still costs us points. Written Sat Nov 28, 8:45 AM Pacific, and checked against main `2c09d69`. Highest lost points first. With [tracker.md](tracker.md) and [brief-compliance.md](brief-compliance.md) this is the to-do list until the freeze.

## Where we stand

Three mock judges each had the organizers' brief and rubric, a fresh clone and no key. None saw our working pages or each other's forms. A fourth pass was a read-only audit of `src/`. Their forms are kept in the project's files, not in this repo.

| Judge | Autonomy 30 | Usefulness 25 | Reliability 20 | Engineering 15 | Demo 10 | Of 100 | Safety bonus |
|---|---|---|---|---|---|---|---|
| 1, leaning autonomy and usefulness | 4 | 4 | 4 | 4 | 1 | 74 | +4 |
| 2, leaning reliability and the Trap Pack | 4 | 4 | 4 | 4 | 3 | 78 | +4, the kill switch point paid once the video shows it |
| 3, leaning engineering and code | 4 | 4 | 4 | 4 | 1 | 74 | +4, the kill switch point at risk until the video |

Panel average: 75 of 100. Straight 5s are 25 points away.

**How the points are counted.** One step on a criterion (4 to 5) is worth its weight divided by 5: Autonomy 6, Usefulness 5, Reliability 4, Engineering 3, Demo 2. A judge gives one score per criterion, so the items under a criterion share that step. Each item's number is an estimate of its share, taken from what each judge wrote would make that score a 5, averaged over the three. The numbers rank the work; they are not measurements. They add up to the 25.

| State | Points | Items |
|---|---|---|
| Already being worked | about 12 | 1, 2, 6, and parts of 5, 15 and 20 |
| Waits on Shane's call | about 8 | 3, 4, 7, 8, 9, 12, 16 |
| Inside the law and ready to assign | about 5 | the rest of 5, and 10, 11, 13, 14, 15, 17 to 22 |

"Checked" below means the line was opened or the search was run on main `2c09d69` for this page. "Judge's run" means the judge ran it and this page did not repeat the run.

## The ranked list

### 1. There is no video, and the kill switch has to be seen on it (6.7 points, plus one bonus point)
- **What:** Demo is scored on the video. All three judges call the `demo` command itself 5 work.
- **Evidence:** `README.md:126` and four lines in `SAFETY.md` say pending. Checked.
- **Judges:** 1, 2, 3.
- **Files:** `docs/video/` (PR #20), then the README's rubric table and SAFETY.md's four "On the video" lines.
- **In flight:** yes. Rehearsal today, filming Sunday.
- **Needs Shane:** he narrates. From the panel, for the shoot: the full untouched run first, then Stop landing during the slowed calendar call, as `SAFETY.md:82` describes.

### 2. The follow-up is claimed and is in no trace (3.0)
- **What:** the track's own example run ends on "files the follow-up". We say she does it. No receipt shows it.
- **Evidence:** `README.md:5` and `AGENT.md:11` claim it. `README.md:151` and `AGENT.md:136` say the nudge is not built. Checked. No receipt line in any committed trace contains "follow" (judges' runs).
- **Judges:** 1, 2, 3.
- **Files:** `loop.py`, `ledger.py`, `gate.py`, `policy.py`, `caps.py`, `why.py`, `digest.py`, `templates.py`.
- **In flight:** yes, the follow-up thread: the dated entry, a receipt line, the one nudge, the gone-booking check. **Done (PR #42, merged):** all four, with AGENT.md's "The follow-up" section; SAFETY.md's nudge and ruling 17 lines in package A's second docs PR; the README lines with package A's README pass. No committed trace shows a T9 sent yet.
- **Needs Shane:** no. After it merges: the four doc lines above change (package A), and `caps.NUDGE_AFTER_BUSINESS_DAYS`, `caps.RULING17_REREAD_WAIT_S` and `ledger.future_bookings` are checked for use.

### 3. A move is never finished (2.3)
- **What:** a sender asks to move to an exact time that is free. She cancels the meeting and sends a list to pick from again. The sender has to write a third email for the time they already named, and has no meeting until they do.
- **Evidence:** `sandbox/inbox/08-priya-move.eml` and her reply `005-T18.eml`, in the demo every judge runs. `policy._move` always plans cancel plus offer. The demo day ends with one meeting standing after five people asked (judge 1).
- **Judges:** 1, 2, 3. All three name it as what Usefulness needs for a 5.
- **Files:** `policy.py`, `loop.py`, `gate.py`, `templates.py`, their tests, the demo day's expected story, the README's numbers, SAFETY.md's overwrite row, the video script.
- **In flight:** no.
- **Needs Shane:** yes, question 1. It is ruling 7 and decision 46, set on purpose.

### 4. The "draft" on a hold is a form letter (1.6)
- **What:** every hold notice carries the same text: "Hi [name], Shane here. [Your answer on: reason.]" and three meeting times. It went out for an overdue invoice that threatens lawyers, and for a docs request (Trap Pack 18).
- **Evidence:** `templates.py:579`. Checked. Three faults inside it that need no ruling: mail from the owner's address reads "Hi Shane, Shane here."; a sender with no known zone gets times like "(Thu Dec 3, 00:00 UTC)"; the no-model notice's draft reads "[Your answer on: the model wasn't available, so I didn't read it.]" (judges' runs).
- **Judges:** 1, 2, 3.
- **Files:** `templates.py`, `holds.py`, `README.md:5`.
- **In flight:** no. `templates.py` is the follow-up thread's until it merges.
- **Needs Shane:** yes, question 4, for the wording. The three faults do not.

### 5. One run's totals disagree (1.6)
- **What:** on one screen the digest, the closing line and the story give different counts.
- **Evidence:** a repeated Message-ID (a judge copies a demo email to make a test): digest "Handled 13, held 2", closing line "14 handled, 2 held", story "15 emails came in" with 16 files. When the repeat follows a held original, the story drops the hold from "Needs Shane". Cause, checked: `loop.count_outcomes` (`loop.py:332`) counts a skipped repeat as handled, `digest.build` (`digest.py:67`) counts distinct messages, and `story.build` lets the last closing line for a Message-ID win. After stop and resume the digest says "Handled 7" on a 13 day. Also: answers that were not replayed are counted as replayed model calls; a no-model hold counts as a model call; "Staged fault sent-unconfirmed: no effect" when it caused a hold; "Needs Shane" calls a failed lookup "the calendar didn't answer twice"; the approvals count lives in four places and one differs (`loop.py:337`, `replay.py:416`, `story.py:182`, `digest.py:95`).
- **Judges:** 2, 3, and judge 1 in passing. Judge 3 names it as all of Reliability's missing step.
- **Files:** one counting function in a new module, then `story.py`, `digest.py`, `replay.py`, `watch.py`, `__main__.py` (the closing line, `:403`), `loop.py` (`count_outcomes` only).
- **In flight:** in part. The whole-day digest after a resume is with the follow-up thread; the whole-day story is PR #37. The repeat, the model-call counts and the labels are not.
- **Needs Shane:** no. The fix carries a test that redelivers every demo email and checks the three totals match.

### 6. The live evidence is thin (1.5)
- **What:** one live run is committed: 8 hours, 2 emails, both from our own test sender.
- **Evidence:** `traces/live-run-20261128T065251Z/`. Checked.
- **Judges:** 1, 2, 3.
- **Files:** `traces/`, `traces/README.md`, the README's rubric table.
- **In flight:** yes. The full live loop today and the overnight run tonight, under 60 emails.
- **Needs Shane:** no.

### 7. Two Trap Pack items on our own track are weak (1.2)
- **What:** item 03, an offsite invite a person asks the owner to accept by Friday, is filed and the owner never hears it arrived. Item 01, a plain meeting request with a hidden instruction and a clean plain-text part, is held, not answered. Overall 7 of 20 are safe but silent, and 7 of the 13 met rows are holds.
- **Evidence:** `tests/trap-pack/RESULTS.md:13` and `:48` (03: read as fyi, the `.ics` never opened, its impossible date unreported). Checked. Item 01 is held by ruling 13.
- **Judges:** 1, 2. Judges will run these two first.
- **Files:** `digest.py`, `templates.py`, `README.md`. A changed digest shows in the report only after a new graded Trap Pack run.
- **In flight:** no.
- **Needs Shane:** yes. Question 2 for a digest line on invites she did not open; question 6 for item 01.

### 8. A two-second outage becomes a permanent hold (1.1)
- **What:** the model call is retried once after 2 seconds, then the email is held, and every later message on that thread is held too. A refused key, which never works, and a rate limit, which clears in a minute, are treated alike. A blip at 2 AM hands the owner those threads for good.
- **Evidence:** `model.py:492-517`, `policy.py:330` (no model: hold), `policy.py:316` (a held thread stays held). Checked. Judge 2's run with a refused key: 6 holds. Our own near-miss log, Sat 7:30 AM: with a refused key, three replies on her threads were filed as forged, not held.
- **Judges:** 2.
- **Files:** `loop.py`, `model.py`, `caps.py`, `policy.py`, their tests.
- **In flight:** in part. PR #36 says a refused key once on the terminal. It does not change the holds.
- **Needs Shane:** yes, question 5. Locked call 12 says no model means hold everything.

### 9. A cancel on a held thread disappears (1.0)
- **What:** a prospect whose thread was held an hour earlier writes to cancel the meeting she booked. Nothing goes to them, no notice goes to the owner, the digest counts it in "held 3" and lists it on no line, and the booking stays. The owner turns up for a call the prospect cancelled. Judge 1: "the one I would not leave running".
- **Evidence:** `policy.py:316-319` returns the held-thread plan before anything else is looked at. `digest.py:117` leaves that code off every line. Checked. The terminal summary also prints the line twice, one copy cut off (judge's run).
- **Judges:** 1.
- **Files:** `policy.py`, `loop.py`, `digest.py`, `templates.py`, `story.py`.
- **In flight:** no.
- **Needs Shane:** yes, questions 2 and 3. The doubled line does not.

### 10. Comments cite documents a stranger cannot open (1.0)
- **What:** about 190 to 230 lines in `src/doris/` point at "ruling 8", "decision 76", "locked call 9", "Contract 7", "A3", spec files, and our own review rounds. None of those is in the repo. README, AGENT.md, SAFETY.md and the trap reports do the same. The numbering has gaps nobody explains (screen 3 and condition B10 are cut; templates run T1 to T18 with nine in use).
- **Evidence:** the audit's counts: 114 rulings, 44 decisions, 17 locked calls, 51 review rounds. `gate.py:5`, `screens.py:4`, `loop.py:5`.
- **Judges:** 1, 2, 3, and the audit (finding 3).
- **Files:** every file in `src/doris/`, comments only, and one appendix in AGENT.md: the 18 calendar rulings and 12 locked calls in their own words, where the G, B, T, S and screen numbers are defined, and why the numbering has gaps.
- **In flight:** no. It touches every file, so the comment pass goes last, after every code change has merged. **Appendix done (PR #40):** AGENT.md, "Appendix: the rules the comments cite".
- **Needs Shane:** no. The appendix is checked against the never-mention list before it is committed.

### 11. The spine does not read top to bottom (0.9)
- **What:** AGENT.md tells the judge to read `loop.py` top to bottom. It is 2,028 lines, one class of 90 methods. The first of the six steps starts at line 823, after live settings and smoke-only code. The entry point is the last function. `handle`, the one method that is the six steps, has no docstring.
- **Evidence:** `loop.py`, `AGENT.md:24`.
- **Judges:** 1, 2, 3, and the audit (finding 1).
- **Files:** `loop.py`; for the optional move of the settings block, a new module plus `__main__.py` and `smoke.py`.
- **In flight:** no. `loop.py` is the follow-up thread's until it merges.
- **Needs Shane:** no. No behaviour change: a map in the module docstring of where each step lives, a docstring on `handle` naming the six steps, one line on each step method, and matching step numbers. It goes in the same last pass as item 10.

### 12. The owner's digest never says what she booked (0.75)
- **What:** the one email the owner gets lists holds, open offers and opt-outs, and not one meeting she put on his calendar. The README says "a digest of what she did".
- **Evidence:** the digest template: its "Booked" line was cut on Nov 26. Judge 1's day of 6 bookings showed none.
- **Judges:** 1, 2.
- **Files:** `digest.py`, `templates.py`, `README.md:5`.
- **In flight:** no.
- **Needs Shane:** yes, question 2.

### 13. Offers do not listen to what the sender said (0.6)
- **What:** "Tuesday or Wednesday afternoon (UK time)" got Monday at noon Pacific, labelled UTC. A Saturday request and a request under 24 hours out got the plain offer with the why "No single clear time", when the time was clear and simply not available. Three options all on one morning. UK, London and CET are not zones she knows, and Known limits does not say so. Our live loop found a cousin this morning: "10:00 AM Pacific about planning" is not read as a zone, so a busy time got the plain offer.
- **Evidence:** judge 1's emails; judge 3's under-24-hours case; the near-miss log, Sat 8:00 AM.
- **Judges:** 1, and judge 3 in passing.
- **Files:** `parser.py` (the day and part-of-day steer, the zone-follow word list), `slots.py`, then `policy.py` and `why.py` for the "that time is not available" reply and a true why. `README.md` for the limit.
- **In flight:** no. **Doc line done in AGENT.md's limits (PR #40)**; the README line comes with package A's README pass.
- **Needs Shane:** no. Steering by what the sender wrote is ruling 2 ("the sender's apparent time first"). It changes offers only, never a direct booking. New zones would be a new feature: US zones and UTC go under Known limits instead.

### 14. Docs that say something untrue (0.45, and the honesty rule)
- **What and evidence,** each line opened on main:
  - `AGENT.md:92`: "a second `demo` in the same folder handles 0". It starts a fresh day and handles 13.
  - `SAFETY.md:32`: `traps` "by default runs the keyword stub". Since PR #34 it replays run 3.
  - `SAFETY.md:72`: "With no key the model is the stub and no cost is shown". With no key it is the recorded answers.
  - `SAFETY.md:121` cites `test_a_run_that_ends_on_failed_reads_keeps_its_trace_with_outcome_fail`, which no longer exists; it is now `test_a_run_whose_reads_fail_keeps_trying_and_once_stopped_keeps_its_trace_with_outcome_fail`.
  - `caps.py:38`: the holiday list is "Published in the README". The word holiday is in neither README nor AGENT.md. A judge who asks for Dec 24 gets an offer and no way to learn why.
  - `SAFETY.md:3`: "every send and every booking passes one gate". The smoke check calls the adapters directly (`smoke.py:354, 375, 392, 422`). It is bounded another way, and one line should say so.
  - `tests/trap-pack/README.md:10`: the pack's tokens are "never copied into code or docs"; `RESULTS.md:106` prints all 20.
  - `loop.py:1758`: "the 250" is an SMTP code; she sends over JMAP.
  - Not stated anywhere: the hash chain has no anchor outside the file (item 19); the send, notice and calendar caps are per run; "0 human approvals" can only be 0 because no approval path exists, so the hold rate is the number that measures autonomy.
- **Judges:** 1, 2, 3, and the audit (finding 9).
- **Files:** `AGENT.md`, `SAFETY.md`, `README.md`, `tests/trap-pack/README.md`. The two source comments go with item 10's pass.
- **In flight:** no. PR #37 changes one README line. **Done (PR #40)** for AGENT.md, SAFETY.md and the trap-pack README, all the "not stated" lines included. Left: the README lines (package A's README pass), and for package G `caps.py:38` (the holidays are now listed in AGENT.md's appendix, under ruling 3) and `loop.py:1758`.
- **Needs Shane:** no.

### 15. A judge's own email with no key (0.3)
- **What:** every email a no-key judge adds comes back "the model wasn't available". The other route, the keyword stand-in, sent meeting times in reply to "Thanks so much for the help last week. Talk soon!". A Date header in 2038 moved the sandbox clock to Dec 31. The README asks for four hand edits per email.
- **Evidence:** judges' runs. Decision 34 already asks for one clear line saying which key to add.
- **Judges:** 1, 2, 3.
- **Files:** `sandbox/README.md`, `README.md`, a ready-to-edit template email in `sandbox/`, `adapters/sandbox.py` (refuse a Date outside the run's window), `model.py` (the stand-in files anything that asks for nothing), `story.py` or `__main__.py` (the one line on how to have it read).
- **In flight:** in part. PR #36: a filled `.env` is read, and a refused key is said once. **Done (PR #40):** `sandbox/README.md` says the no-key hold first, `sandbox/your-email.eml` is the ready-to-edit template (no facts.json line needed), with a test that runs it through the stand-in day.
- **Needs Shane:** no.

### 16. Working files sit in the repo root (0.3)
- **What:** `HANDOFF.md`, `tracker.md`, `brief-compliance.md`, `status/` and this page are outside the organizers' layout and read as working files left in a submission.
- **Judges:** 1, 2, 3.
- **Files:** those five, and every link to them.
- **In flight:** no.
- **Needs Shane:** yes, question 7. The standing rule is that the status and the record stay in the repo.

### 17. `redteam` reads the working sandbox inbox (0.2)
- **What:** after a judge follows "Try it on your own email", one unit test fails (`test_redteam.TamperTest.test_a_booking_moved_away_later_is_still_checked`) and `redteam`'s demo-day cases run the judge's files too.
- **Evidence:** `redteam.py:258` copies `sandbox/inbox` as it stands. Checked.
- **Judges:** 1.
- **Files:** `redteam.py`, `tests/unit/test_redteam.py`.
- **In flight:** no. **Needs Shane:** no.

### 18. Code hygiene from the audit (0.15 from the judges; the audit rates it higher)
From `src/` alone the audit gives Engineering a 4 and names these, with items 10 and 11, as the way to a 5. None adds a feature.
- **Hidden control characters typed raw into source:** `parser.py:34`, `parser.py:58`, `traps.py:623`. Checked. GitHub puts its hidden-text warning on those files, in a repo whose pitch is hidden-text defence. Write them as `\u` escapes; the strings are the same at run time.
- **A hold that states an untrue reason:** `loop.py:1284` holds a re-read message whose booking is gone as "the model was unsure". **Fixed by PR #42:** a gone booking is now held as removed by the owner (`hold.owner_removed`). The last fallback, a re-read message whose booking is already cancelled, still holds as "unsure" (`loop.py:1326`).
- **"Every hard limit lives in one file" is not quite true:** `policy.py` spells the two-offer cap and the 30-minute default as numbers in ten places; `parser.py:27`, `model.py:316` and `shapes.py:158` repeat the 30.
- **The policy "table":** `policy.ACTIONS` is read nowhere and leaves out `replied`; `Plan.pick_kind` is set in seven places and read in none; seven classes are an if-chain.
- **Dead code:** `mail.fold_for_match`, `redteam._ids`, three ledger constants, `host_kind`, `__version__`, 11 unused imports, 9 unused parameters, two no-op blocks. The audit's finding 8 has the full list.
- **Docstrings about how we built it, or untrue:** "the calendar builder", "another builder owns it", "the skeleton", and one that names our server by its nickname; `adapters/__init__.py` says adapters hold the model call; `adapters/base.py` says sandbox and live implement exactly the contract while the loop calls three sandbox-only methods; `parser.py:6` cites a B12 that does not exist; `zones.py:8` reads like an open bug.
- **One thing defined in several places:** the offer template lists (`loop.py:62`, `gate.py:45-46`, `redteam.py:88, 96`), `_norm_mid` three times, `_display` four, `_iso` five, `UTC` in 13 modules.
- **Private names used across modules:** 31 uses.
- **Naming in the spine:** `live` means three things in `loop.py`; screen numbers are bare (`r.screen == 5`).
- **Test stand-ins inside the runtime model module:** `model.py:249-428`.
- **Judges:** 2 and 3 in passing; the audit (findings 4 to 15).
- **Files:** most of `src/doris/`. `parser.py`'s long functions are left alone: a wrong parse is a wrong booking.
- **In flight:** no, but half of these files are held by open threads. **Needs Shane:** no.

### 19. The receipt chain can be rewritten whole (protects the audit-trail bonus point)
- **What:** the chain has no key and no anchor outside the file. Judge 2's 15-line script changed one send, rehashed to the end, and `verify` said ok. SAFETY.md does not state the limit.
- **Judges:** 2.
- **Files:** `SAFETY.md` (state it); `story.py` and `__main__.py` (print the head hash at the end of `demo` and in `verify`, so a copy held elsewhere proves the file); the digest's last line is question 2.
- **In flight:** no. **The limit is stated in SAFETY.md (PR #40).** **Needs Shane:** only for the digest line.

### 20. Printed commands say `python` (small)
- **What:** a stock Mac has no `python`; the README says `python3`, and `py` on Windows.
- **Evidence:** `story.py` is fixed in PR #37. Left, checked: `__main__.py:474` and `redteam.py:1764-1765`.
- **Judges:** 1, 3, and the audit (finding 2). **In flight:** in part. **Needs Shane:** no.

### 21. Oversize mail costs a minute of CPU before it is held (0.1)
- **What:** a 30 MB email took about 75 seconds before the hold, because the stop scan reads the whole text first.
- **Judges:** 2.
- **Files:** `screens.py`, `mail.py`.
- **In flight:** no. **Needs Shane:** no, as a speed fix only. The order stays: ruling 9 puts stop before every rule, so size cannot be checked first.

### 22. Small wording (under 0.1)
- A confirmation to a sender who wrote Pacific reads "10:00 AM Pacific (18:00 UTC)".
- A move's reply writes the cancelled time zone-first one way and the offered times the other.
- The digest says "Model calls" in a stand-in run.
- `story.py` and SAFETY.md on the gate count: fixed in PR #37.

## Left as it is

Each of these was raised and is not a fix. Most are the law working as written.

- After a hold the sender hears nothing. The law: a held prospect gets nothing from her.
- Mail to the owner ends 'Reply "stop" to opt out'. One signature on everything she sends, checked at the gate; a second signature is a new branch in a never-cut check.
- "Handled 13" counts mail that needed no reply. The terminal says so on the next line.
- One open slot can be offered to three people. The re-check before the write settles it; the second picker is re-offered.
- "Either of two times works" gets both offered back. Ruling 2: a wrong booking is the worst outcome.
- `stop` does not reach `traps`. Stated under Known limits.
- Fastmail only, the owner's hours and name in the source, credentials wider than the job. All stated.
- The red-team and Trap Pack harnesses ship in the package. Judges run both as commands; AGENT.md gets one line saying so.
- The adapter's send takes no proof that the gate passed. SAFETY.md says how the gate is reached and names the smoke check as the one path around it (item 14).
- AGENT.md does not name the tools used to build her. The brief asks for the model and the stack, and both are there.

## Shane's calls

Seven questions, each with one recommendation.

1. **A move to a named time (item 3; ruling 7, decision 46).** When a move names one clear time that passes the same test as a direct booking and is free, book the new time first, then remove the old one, and confirm both in one reply. Everything else stays cancel plus offer. It needs one law line and one approved template. **Recommend yes.** All three judges ask for it, and it closes Priya's loop in the demo, so the day ends with two meetings standing. It is the calendar write path a day before the freeze: built after the follow-up merge with the most careful setting and a separate review, and dropped if it is not green by Sunday noon. Not recommended: keeping the old meeting while a new time is picked (new state), and booking the first of two "either works" times (ruling 2).
2. **Four digest lines (items 7, 9, 12, 19).** Each counted from receipts, zero-count lines left out as now: "Booked: [name, slot]" and "Cancelled: [name, slot]"; "More mail on a held thread: [name], '[their first 200 characters, defanged]'"; "Invites not opened: [k], from [names]"; and the chain's head hash after "Receipts verify: yes". **Recommend yes to all four.** The Booked line was cut on Nov 26; two judges missed it.
3. **A cancel on a held thread (item 9).** When the same sender cancels her own booking on a thread that was held, she removes her event, sends them nothing, and tells the owner on the digest line above. **Recommend yes.** Removing her own event is the safe direction, and the notice the owner already has says she will send nothing more on that thread, which stays true.
4. **The hold "draft" (item 4).** Keep it written by code. Call it "a starter reply" in the notice and the README, and leave the meeting times out when the hold is not about a meeting. **Recommend yes.** Not recommended: a draft written by the model. It would put words built from hostile mail in front of the owner, and it means recording the answers and grading the Trap Pack again.
5. **A short outage (item 8; locked call 12).** In a long run, when the model or the calendar fails in a way that can pass (a timeout, a rate limit, a server error), she leaves that email unread and tries again on each of the next three passes, a receipt each time, then holds as today. A refused key still holds at once. `demo` and `traps` are unchanged. **Recommend yes, landing Sunday morning, after the overnight run has started on a main we know.**
6. **Hidden text, Trap Pack item 01 (ruling 13).** **Recommend no change.** The hold is graded "met" against the organizers' key. The README gets one line: hidden text always costs a hold, and why.
7. **Working files in the root (item 16).** At the freeze, move `HANDOFF.md`, `tracker.md`, `brief-compliance.md`, `status/` and this page into `docs/team/`, in one commit with the links fixed, and link the folder once from the README as how we ran the build. **Recommend yes.** The record stays in the repo and the root matches the organizers' layout.

## Work packages

One owner per file. No package touches a file an open thread holds. Shared files go in the order shown.

**Open threads and their files today:** the follow-up thread (`loop.py`, `ledger.py`, `gate.py`, `policy.py`, `caps.py`, `why.py`, `digest.py`, `templates.py`, and the new lines in `replay.py`, `watch.py`, `livetrace.py`); PR #36 (`__main__.py`, `model.py`, `.env.example`); PR #37 (`story.py`, `watch.py`, one README line); PR #20 (`docs/video/`).

### Can start now

**A. Docs that match the code.** Items 14, 10 (the AGENT.md appendix), 6 and 1 (links as traces and the video land), 7 (the hidden-text line), 13 (zones under Known limits), 15 (the own-email section), 19 (the limit stated).
Files: `AGENT.md`, `SAFETY.md`, `tests/trap-pack/README.md`, `sandbox/README.md`, and `README.md` once PR #37 has merged. A second short pass after the follow-up thread and PR #36 merge: `README.md:5` and `:151`, `AGENT.md:11` and `:136`, the `.env` step.

**B. Free-file code, no behaviour change.** Items 17, 20 (`redteam.py`), 21, 15 (the Date window), and item 18 for these files: the raw control characters in `traps.py`, dead code, untrue docstrings, the duplicated helpers, private names.
Files: `traps.py`, `redteam.py`, `mail.py`, `screens.py`, `netguard.py`, `shapes.py`, `zones.py`, `compose.py`, `ics.py`, `receipts.py` (docstring only), `__init__.py`, `adapters/` (all), `tests/unit/test_redteam.py`.

**C. Offers that listen.** Item 13's parser and slot half, and item 18's `parser.py` lines (the `\u` escapes, the docstrings).
Files: `parser.py`, `slots.py`, their tests. Offers only: no change to what counts as a direct booking. The most careful setting and a separate review.

### After the follow-up thread, PR #36 and PR #37 merge

**D. One count everywhere.** Items 5, 9 (the doubled line), 15 (the how-to-have-it-read line, the model-call count), 19 (the head hash printed), 20 (`__main__.py:474`), 22 ("Model calls").
Files: a new counting module, `story.py`, `replay.py`, `watch.py`, `digest.py`, `__main__.py`, `model.py` (the stand-in), `loop.py` (`count_outcomes` and the digest call only). First on `loop.py` and `digest.py` after the follow-up merge. No ruling needed.

**E. Finish the move.** Question 1, with item 13's reply half ("that time is not available" and a true why) and item 18's policy lines (the caps read from `caps.py`, `ACTIONS`, `pick_kind`, the untrue hold reason).
Files: `policy.py`, `loop.py`, `gate.py`, `templates.py`, `why.py`, `caps.py`, their tests, the demo day's expected story. After D merges. The most careful setting and a separate review. Starts only on Shane's yes; without it the package is the item 13 and 18 lines alone.

**F. The owner's side.** Questions 2, 3, 4 and 5: the digest lines, the cancel on a held thread, the hold starter and its three faults, the short outage.
Files: `digest.py`, `holds.py`, `templates.py`, `policy.py`, `loop.py`, `model.py`, `caps.py`, their tests. After E merges. The retry count stays in the run, not in the ledger, so the ledger's layout does not change again. A new graded Trap Pack run after it, so the report shows the invite line.

### Last, after every code package has merged

**G. The comment pass and the spine.** Items 10 and 11, the two source comments from item 14, and item 18's naming.
Files: every file in `src/doris/`, comments and docstrings only, plus the map and docstrings in `loop.py`. Sunday afternoon, before the freeze recording.

**H. The root.** Question 7. One commit at the freeze.

Order on the shared files: follow-up thread, then D, then E, then F, then G.
