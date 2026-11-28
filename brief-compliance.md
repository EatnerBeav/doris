# Brief compliance

Every line of the organizers' pages that asks for something, or says how judges score, mapped to what we can show today.

- **Read:** Fri Nov 27, 2026, 11:00 PM Pacific, from the live site: the brief with all 59 answered questions (https://agentwarehack.com/brief/ and its text copy https://agentwarehack.com/brief/agentware-brief.md), the Trap Pack page (https://agentwarehack.com/trap-pack/), the event page (https://agentwarehack.com/, for the Track 1 text and the named loopholes), the submission form (https://tally.so/r/WJzRbk) and the kickoff email. Sat Nov 28, 7:35 AM: the brief's text copy fetched again (54,173 characters, sha256 `d757889c78c794eb5ee99610bcb42130f5a60243f708f6f434342530f80b7f00`), its "What to submit", Track 1, "Scoring rubric" and "Safety bonus" sections read again, and its answered questions counted: still 59, in the same eight groups. The Trap Pack page, the event page and the form were not opened again.
- **Checked against:** `main` at `5be0658`, Sat Nov 28, 2026, 7:25 to 8:10 AM Pacific, on a fresh clone, Linux, Python 3.13, no key. This is the second pass; the first was Fri 11:00 PM against `a13cadd`. Every command was run at `5086c7e`. Two merges landed during the pass and changed no code: PR #32 (the first live run's trace) and PR #33 (README, SAFETY.md, AGENT.md). Their files were read and checked after each merged, and the rows below are graded on the result. Every "met" below is something I ran or opened myself in this pass. Nothing is marked met on a plan, a memory note or another thread's report.
- **Wording:** each row states the ask in our own words and points to the place on the page that says it, so anyone can check a row against the source in seconds. The organizers' text is not reprinted here.
- **Status words:** **met** (on main, verified) · **partly** (some of it verified, the rest absent or only on a branch) · **missing** (nothing on main yet) · **wrong** (what main does contradicts the line).

## Count

75 rows: **met 61 · partly 7 · missing 6 · wrong 1.** (Fri 11:00 PM: met 50 · partly 14 · missing 9 · wrong 2.)

All six missing rows are deliverables that are not due yet: the video (four rows), the submission, the Near Miss write-up. The work left is the seven partly rows and the one wrong row.

What moved since Friday night:

| Row | Was | Now | Why |
|---|---|---|---|
| 1.3 | partly | met | The README names the one key and the tier |
| 1.4 | missing | met | The README states the measured cost of each run, with its trace |
| 1.7 | partly | met | The README links the sandbox guide and gives the steps for a judge's own test accounts |
| 1.9 | partly | met | The README has "Known limits" |
| 1.10 | partly | met | The README carries a judge past `demo`; a second `demo` starts the day fresh |
| 3.3 | partly | met | The first live run's trace is on main: 8 hours 12 minutes, with a live booking |
| 3.4 | met | partly | One claim in the README runs ahead of the trace, and one cited test was renamed |
| 5.9 | partly | met | With no key, `demo` replays answers recorded from a real keyed run |
| 7.1 | missing | met | The README has the rubric-to-evidence table, with the weights |
| 7.6 | partly | met | The same live trace: she was left running overnight |
| 9.3 | wrong | met | The Trap Pack report carries the grade against the key and links the run history |
| 9.4 | partly | met | Runs 1, 2 and 3 all have their traces on main |
| 9.5 | partly | met | Run 3 on main: both harmless files handled, neither refused |
| 10.2 | missing | partly | The call is made; it takes effect when the form is filed |

## The three riskiest gaps

1. **The last step of Track 1's own example run is still invisible.** The example ends with the agent filing the follow-up, and the README's opening says she does. Her ledger keeps each offer with its expiry, the digest lists "Offered times to" and the end of `demo` lists who is waiting to pick, but no receipt names a follow-up and the one nudge on an unanswered offer is not built (the README's limits say so). Rows 6.2, 3.4. Usefulness, 25%.
2. **A judge with no key meets the stand-in, not the model, when they run the Trap Pack.** `traps` with no key runs the keyword stand-in, which offers times to five trap senders the real model files in silence, so a judge's own run looks worse than our committed one. Replaying Trap Pack run 3's recorded answers with no key already works (tried it: all 20 rows match the committed report), but nothing ships it as the default. And the brief's own key step still does nothing: a filled `.env` is ignored and `demo` says "No key found". Rows 1.2, 9.2. Reliability and Engineering.
3. **What the owner was spared is not said, and the live proof is thin.** The end of `demo` tells the owner's day well, but neither it nor the README says in plain words what he did not have to do, and the only live trace of her left running holds two emails in eight hours. Tonight's overnight run is the fix. Row 7.7. Usefulness, 25%.

## 1. A judge can run it

| # | What the brief asks | Where | Our evidence on main | Status |
|---|---|---|---|---|
| 1.1 | Clone, follow the README, and have the agent working inside 15 minutes | Brief, What to submit, judge item 1; Q41 | README "Run it". Fresh clone, `cd src && python3 -m doris demo`: 12.8 seconds, ends `13 handled, 2 held, 0 human approvals`, chain verified, then the owner's summary. CI on `5086c7e`: all 16 checks green, among them the README walked on Linux bash, macOS zsh, Windows PowerShell 7, Windows PowerShell 5.1 and cmd, and the downloaded ZIP on Linux and Windows | met |
| 1.2 | The same sentence has the judge fill in the example env file | Brief, What to submit, judge item 1 | Tried it again: copied `.env.example` to `.env` at the repo root and in `src/`, filled the model key, ran `demo`. The first line still says "No key found" and the recorded answers are replayed. `src/doris/__main__.py:315` passes the process environment only. New since Friday: README "With a key: keys, tier and cost" gives a step that does work, setting `ANTHROPIC_API_KEY` in the shell (with a made-up key the first line read "Model called live" and the API's 401 was recorded). The README never says a `.env` file is not read, `.env.example`'s header still speaks to our server, and the law says a key in `.env` makes the same commands call the model | wrong |
| 1.3 | Name the API keys and the tier | Brief, What to submit, judge item 1 | README "With a key: keys, tier and cost": one key, `ANTHROPIC_API_KEY`; the model, `claude-sonnet-5-5`; the tier (any published tier, with the lowest one's limits set beside what her busiest command uses, and the plain statement that we have not run on the unpublished evaluation tier). I did not check the tier's name and limits against Anthropic's page | met |
| 1.4 | The README says clearly what a test run costs | Q27 | README "What a run costs": $0 with no key, $0.0038 for one live email, $0.0076 for the first live run, $0.0337 for the demo day's 9 calls, $0.0649 for the 20 Trap Pack files, each with its trace. Each figure matches its manifest: `traces/live-smoke-20261128T055002Z/manifest.json` (0.00379), `traces/live-run-20261128T065251Z/manifest.json` (0.007622), `traces/model-live-sandbox-20261128T123315Z/manifest.json` (0.033706), `traces/model-live-throwaway-traps-20261128T1233Z/run-manifest.json` (0.064894) | met |
| 1.5 | Keep it cheap: a free tier, a mock mode or a recorded trace is fine | Q27 | `demo`, `traps`, `redteam`, `verify`, `replay`, `stop`, `resume` and `watch` all ran with no key, at $0. `demo` replays answers recorded from a real keyed run and says so on its first line | met |
| 1.6 | A sandbox, test account or mock mode for anything personal; never ask a judge for credentials; a personal mailbox is not a setup guide | Brief, Rules 4; event page, named loopholes | `sandbox/` (15 emails, a calendar of files, an outbox). No doc asks a judge for a credential. AGENT.md "Permissions" opens by saying judges need none | met |
| 1.7 | Document how a judge can use a sandbox or test account themselves | Q26 | README "Try it on your own email" links `sandbox/README.md`, which tells a judge how to add their own `.eml`, and it works as written: I added one. With no key she held it (it has no recorded answer); with `answers.jsonl` moved aside the stand-in read it and booked it. README "Live, on our own test accounts" gives five steps for a judge's own Fastmail test accounts and says plainly that the smoke step needs one line of source changed. I read those steps; I did not run them | met |
| 1.8 | Judges run it from the README over Nov 30 to Dec 10, with or without a live link | Q38; Brief, How it works 5 | Nothing in the judge path needs our accounts or our server: every command in 1.5 ran from the clone alone, and `traps` and `redteam` print that the network guard allows no host | met |
| 1.9 | README: what it does, how to run it, honest limits | Brief, layout | What it does (the opening), how to run it ("Run it"), and "Known limits": no Google or Microsoft calendar, live only on Fastmail, the longest live run so far 8 hours and 2 emails, a judge with no key sees replayed answers, the nudge and the gone-booking check not built, 7 of 20 Trap Pack files safe but silent, off-screen text not screened, `stop` not reaching `traps` | met |
| 1.10 | Docs a stranger follows without asking a question | Rubric, Engineering | Followed the README top to bottom on the fresh clone. "What you just saw" matches the output, every path under "Open what she did" exists after a run, and each of the six commands under "The next five minutes" did what its row says. A second `demo` starts the day fresh and says where the last one went. CI walks the same README on five shells. Two small stumbles left: the last lines `demo` prints name `python -m doris replay` and `sandbox/outbox/`, which do not paste from `src/` on every system; and after stopping an idle `run`, the next `demo` prints `0 handled` once | met |

## 2. A judge can watch it

| # | What the brief asks | Where | Our evidence on main | Status |
|---|---|---|---|---|
| 2.1 | A video of three minutes or less: one full unattended run, trigger to result, nobody at the keyboard in the middle | Brief, What to submit; Q32 | No video yet. The script and shot list are in PR #20, not on main | missing |
| 2.2 | The video opens without an account (unlisted YouTube is fine) | Q37 | No video yet | missing |
| 2.3 | The video shows one real run, clearly enough that a judge believes it before running it | Rubric, Demo | No video yet | missing |
| 2.4 | The kill switch is shown working in the demo | Safety bonus, Kill Switch | No video yet. SAFETY.md's four "On the video" lines say pending | missing |

## 3. A judge can read what it did

| # | What the brief asks | Where | Our evidence on main | Status |
|---|---|---|---|---|
| 3.1 | Every run leaves a trace: what it saw, decided, called, and what came back | Brief, What to submit, judge item 3 | `replay` on the fresh demo run prints, per email, Saw, Asked, Classed, Decided, Called with its result, Checked, Verified, Closed, each with its receipt line numbers (170 lines of output over 224 receipts) | met |
| 3.2 | The trace is the receipt: approvals in the trace are scored as approvals | Brief, What to submit | The demo's summary receipt (line 224) holds `human_approvals: 0`; the two holds are labelled holds. The same count is 0 in the manifests of live smoke 3, the keyed demo day and the first live run | met |
| 3.3 | `traces/` holds real run logs, start to finish | Brief, layout | Committed: three live smokes (two failed ones kept); the first live run, `traces/live-run-20261128T065251Z/` (8 hours 12 minutes, 52 inbox reads, an offer, then the booking the sender picked, ended by `stop`); the keyed demo day; Trap Pack runs 1, 2 and 3 with the live model; a stub run; two yes-man runs. `verify` passes on the live run (153 lines), smoke 3, the keyed demo day and one item from each Trap Pack run. Still to come: the overnight run, Sat 8:00 PM | met |
| 3.4 | If the README claims more than the trace shows, the trace wins; an honest unfixed failure beats a confident README | Q41; Brief, What to submit | A script checked the 299 tests, functions and paths cited in SAFETY.md, AGENT.md and the README, and every relative link in the README: every one exists except one test that was renamed (SAFETY.md line 121 cites `LiveTraceTest.test_a_run_that_ends_on_failed_reads_keeps_its_trace_with_outcome_fail`; it is now `test_a_run_whose_reads_fail_keeps_trying_and_once_stopped_keeps_its_trace_with_outcome_fail`). Each "Prove it" in SAFETY.md reproduced. One claim runs ahead of the trace: the README's opening says she "files the follow-up", and no receipt names one (row 6.2). One line runs behind main: SAFETY.md line 72 says that with no key the model is the stub and no cost is shown (it is the recorded answers, with their recorded cost). PR #33 brought the rest up to date | partly |
| 3.5 | SAFETY.md says what is enforced in code, not what the model was told | Brief, What to submit | SAFETY.md cites a function and a test for every rule (checked as in 3.4; one renamed test); `gate.py` holds G1 to G17, and every action in the demo has a `checked` line listing them | met |

## 4. On disk

| # | What the brief asks | Where | Our evidence on main | Status |
|---|---|---|---|---|
| 4.1 | AGENT.md: architecture, tools, permissions, track, models | Brief, layout | All five are there: "Architecture", "Her tools", "Permissions", track T1 on line 3, "Models" | met |
| 4.2 | SAFETY.md: guardrails, kill switch, monitoring, audit | Brief, layout | Four sections under the brief's own four names, after a table of irreversible actions | met |
| 4.3 | `.env.example`: every key, no secrets | Brief, layout | 12 names, no values; every `DORIS_`, `WOLF_` and `ANTHROPIC_` name the code reads from an env file is listed (the four others in the code are set by her own processes or by the secret scan) | met |
| 4.4 | `src/`, all written in the 72 hours | Brief, layout | First commit 2026-11-27 18:10:53 UTC, ten minutes after kickoff; 246 commits on main since | met |
| 4.5 | `tests/` including the Trap Pack results | Brief, layout | `tests/trap-pack/RESULTS.md`, `RUNS.md`, the pack itself, `tests/redteam/RESULTS.md` | met |
| 4.6 | LICENSE: MIT or Apache-2.0 | Brief, layout; Q36 | `LICENSE` is MIT; GitHub reports the licence as MIT | met |
| 4.7 | `.agentware.toml`: the track and a one-line pitch | Brief, layout; Q39 | `track = "T1"` and the pitch | met |

## 5. Rules

| # | What the brief asks | Where | Our evidence on main | Status |
|---|---|---|---|---|
| 5.1 | Open source under an OSI licence, public at submission | Rules 1 | GitHub API at 7:29 AM: public, MIT | met |
| 5.2 | Name the model and stack in AGENT.md, and roughly what a judge needs | Rules 2; Q20 | AGENT.md "Models": `claude-sonnet-5-5`, Python 3.9+, standard library only; "Permissions": judges need no credential | met |
| 5.3 | It has to act: real actions, real tools, real or sandboxed systems | Rules 3 | Sandbox: a fresh demo wrote 9 emails and her booking to disk (parsed all nine, opened the `.ics`). Live: smoke 3's timeline shows her reply found in Sent and in the sender's inbox; the first live run's shows a calendar write and its T1 found in Sent | met |
| 5.4 | No secrets in the repo; a committed secret disqualifies | Rules 5 | `python3 src/secret_scan.py --all`: 2,777 files, clean. My own search of every commit on main for key shapes: none (the Trap Pack's own fake keys aside). `.env` is ignored and no `.env` is tracked; CI runs the scan | met |
| 5.5 | Do no harm: no spam, nobody who didn't write first, no terms broken, no money without a hard limit, destructive actions tested on sandboxes | Rules 6 | Demo `envelope.jsonl`: each of the nine sends goes to the sender plus the owner's copy, or to the owner alone. The live traces use our test accounts only. Model spend stops at $5.00 a run (`caps.py:19`, G3) | met |
| 5.6 | New code only, and commit often | Rules 7; Q14, Q30 | Repo created 17:55:09 UTC; first commit 18:10:53 UTC; 246 commits on main in the first 22 hours | met |
| 5.7 | Every builder registers; teams of 1 to 4 | Rules 8; How it works 1; Q1 | The form's "Registration confirmed" mail is in the registration inbox (seen again this pass). One registered person, with Wolf (Claude) credited as CTO | met |
| 5.8 | Someone on the team can explain it and answers the judges' written questions | Rules 9; Q22 | Decision 43 gives the job to the project chat, daily from Nov 30 to Dec 10. No daily check is scheduled yet (the project's scheduled tasks hold only two one-off reminders) | partly |
| 5.9 | No prerecorded demo over hardcoded outputs; judges will run it | Rules, will-not-score list; event page, named loopholes | Each `demo` runs the real loop, gate and adapters and writes a new receipt chain; only the model's nine answers are replayed, from a real keyed run (`sandbox/answers.jsonl`, trace `traces/model-live-sandbox-20261128T123315Z/`), and the first line and every receipt say so. Change the input and the outcome changes: a stop mid-call halted her, a calendar staged to fail twice became a hold, an email I added was held with no key and booked by the stand-in, a refused key held everything that needed reading | met |
| 5.10 | Not a chatbot, not one model call in a wrapper | Rules, will-not-score list; event page, named loopholes | 15 emails, 224 receipt lines, 9 emails sent, 2 bookings and a cancel, 14 passes through the gate | met |
| 5.11 | Never message real people who did not consent | Rules, will-not-score list | As 5.5: replies only to the person who wrote, on their thread (G6) | met |
| 5.12 | No real money without a hard limit and an approval | Rules, will-not-score list | No payment tool exists; the demo's pricing email is held for the owner | met |
| 5.13 | Never pass as a human | Rules, will-not-score list | All 9 demo sends carry the two signature lines saying she is an AI, and `Auto-Submitted: auto-replied` | met |
| 5.14 | Not an earlier project and not a renamed open-source agent | Rules, will-not-score list; Q16 | History starts at kickoff; no dependency file, and every import in `src/` is the standard library | met |

## 6. Track 1, Inbox & Calendar

| # | What the brief asks | Where | Our evidence on main | Status |
|---|---|---|---|---|
| 6.1 | Inbox triage, replies, scheduling, a daily digest | Brief, track 1 | Demo: newsletter, auto-reply and injection filed; money held; unreadable mail quarantined; T1, T2, T4, T18 sent; 2 bookings, a pick, a move; the digest at 5:00 PM | met |
| 6.2 | Follow-ups; the example run ends by filing the follow-up | Brief, track 1 and its example run | The ledger's `offers` table keeps each offer with its expiry. A judge now sees who is waiting: the digest's "Offered times to: Priya Raman, Sam Rivera" and the end of `demo`'s "Waiting to pick one of her times". But the word follow-up is in no receipt, no terminal line and no digest line, no receipt records the offer's expiry, and the nudge (T9) is a template with no code that sends it. The README's limits say the nudge is not built | partly |
| 6.3 | The example run: a request lands, she reads the thread, checks the calendar, books the slot, sends the reply | Brief, track 1, example run | Demo email 1 (lines 4 to 27): read, calendar read twice, booked, read back, T1 sent with its `.ics`, found in Sent. Email 7 books a pick from her own earlier offer on the thread. Live: the same pick-and-book in `traces/live-run-20261128T065251Z/` | met |
| 6.4 | She handles mail instead of leaving a longer to-do list | Event page, track 1 | The digest leads with the ratio and lists only the two holds as needing the owner | met |
| 6.5 | Enter one track | Brief, tracks | `.agentware.toml`: T1 | met |
| 6.6 | Narrow and finished beats broad and half-done | Brief, tracks; Rubric, Usefulness | She does scheduling end to end and holds everything else (AGENT.md "The job"; 13 of 15 demo emails finished alone) | met |

## 7. Scoring rubric

| # | What the brief asks | Where | Our evidence on main | Status |
|---|---|---|---|---|
| 7.1 | Five criteria on a 5-point scale (30, 25, 20, 15, 10), averaged across judges, plus the safety bonus | Rubric; Q40 | README "Evidence for each criterion": one row for each of the five criteria with its weight and each of the four safety items, each pointing at a command, a file or a trace that exists, the first live run among them. Its Demo row says the video is pending | met |
| 7.2 | Autonomy: how much runs with nobody in the loop, checked in the trace | Rubric, Autonomy | 13 of 15 demo emails end to end; approvals 0 in the summary receipt, and 0 in the first live run's manifest | met |
| 7.3 | Autonomy: how many steps run without a human | Rubric, Autonomy | 224 recorded steps in the demo run, none waiting on a person; the end of `demo` prints "Receipts: 224 lines". The count of steps with a human is not printed as such (Step 2, item 1) | met |
| 7.4 | Autonomy: plans, checks and finishes on its own | Rubric, Autonomy | Every email has a `decided` plan line, `checked` gate lines, read-backs after each write, and a closing line | met |
| 7.5 | Unattended from trigger to result; asking a human before an irreversible step is good design | Q25 | Money and a request to write to another person are held with a notice and a draft; nothing waits for an approval | met |
| 7.6 | The agent you would actually leave running | Brief, opening; event page, the one condition; Prizes, 1st place | She was left running: `traces/live-run-20261128T065251Z/`, live from Fri 10:52 PM to Sat 7:05 AM Pacific, a read of the inbox every ten minutes, an offer and a booking with nobody at the keyboard, 0 approvals, $0.0076, ended by `stop` with the worker dead 59 ms later. It is a thin night (two emails, both in the first half hour); the overnight run with volume and a forced restart is tonight | met |
| 7.7 | Usefulness: a job someone would hand over; real problem, real workflow, real time saved | Rubric, Usefulness | The end of `demo` now tells the owner's day from her receipts: 15 emails in, 13 handled by her, 2 needing him, the meeting on his calendar, each thread in a line. Neither it nor the README says in plain words what the owner was spared (the README's Usefulness row points at the outbox and at that summary), no time saved is stated, and the only live run on real hours holds two emails | partly |
| 7.8 | Reliability: bad input, a failed API call, a timeout, an ambiguous request, a Trap Pack email; recover, retry, ask or stop cleanly | Rubric, Reliability | All seen this pass: unreadable mail quarantined and the run goes on; a calendar timeout retried once; no clear time gets an offer; an event landing mid-booking is put right; a key the API refuses (401) holds everything that needs reading and sends nothing to a sender; the calendar failing twice is a clean hold and the run goes on (seen by changing the staged fault; still no loop test pins it) | met |
| 7.9 | Engineering: clean architecture, sensible tools, permissions scoped to the job | Rubric, Engineering | Six steps, one policy table, one gate file (851 lines), every cap in one file (89 lines); AGENT.md gives each credential's scope and what it cannot do. Limits: `loop.py` is 2,028 lines; the scopes at Fastmail cannot be checked from the repo | met |

## 8. Safety bonus

| # | What the brief asks | Where | Our evidence on main | Status |
|---|---|---|---|---|
| 8.1 | Guardrails: delete, send, pay, deploy and overwrite need confirmation or are blocked in code, not in the prompt | Safety bonus 1 | SAFETY.md's table of the five verbs; `gate.py`; `redteam` re-run: 0 bad actions in 206 runs of a model that obeys every injection, with G1, G7 and G9 seen blocking | met |
| 8.2 | Live monitoring: current step, tool calls, decisions and cost so far, in real time | Safety bonus 2 | `watch` fetched three times during a run: the four panes, the failed calendar call shown, a why on every decision, the cost so far, one Stop button | met |
| 8.3 | Kill switch: one action, at once, mid-run, pending calls cancelled; closing the laptop is not one | Safety bonus 3; event page, named loopholes | `demo`, then `stop` from a second terminal during the calendar pause: worker dead 32 ms later, the calendar call in flight cut off, 1 queued item dropped, a halted receipt, `verify` ok, `demo` refused until `resume` | met |
| 8.4 | Audit trail: enough detail to reconstruct and replay | Safety bonus 4 | `verify` ok on 224 lines; one changed character names line 20 and exits 1; `replay` tells the run step by step and refuses a changed file, naming the line | met |
| 8.5 | Best Kill Switch: all four, done properly | Prizes | Each of the four works (8.1 to 8.4). SAFETY.md says pending in six places, each still true: four "On the video" lines (32, 72, 82, 109), the paused-password check against the live servers (103) and the overnight run (121). The Trap Pack line was retired by PR #33. `stop` does not reach `traps` (stated in the README's limits) | partly |

## 9. The Trap Pack

| # | What the brief asks | Where | Our evidence on main | Status |
|---|---|---|---|---|
| 9.1 | Run it and commit the results in `tests/`; other tracks' files welcome | Brief, Trap Pack; Trap Pack page | `tests/trap-pack/RESULTS.md`: all 20 files, live model, run 3 on merged main | met |
| 9.2 | Judges will run it too | Brief, Trap Pack | `python3 -m doris traps` on a fresh clone: 20 files in 1.1 seconds, and the clone stays clean (the report and traces go under `state/throwaway/`). With no key it is the keyword stand-in, labelled, and it offers times to five senders the model files in silence, so a judge's own run looks worse than our committed one. `traps --model replayed` with run 3's recorded answers works with no key and gives the committed report's 20 outcomes (tried it), but the answers sit in 15 per-item files and no doc names the step | partly |
| 9.3 | Know what happens when you fail; honest beats tidy | Brief, Trap Pack; Trap Pack page, reporting | `RESULTS.md` is run 3's report with a hand-added "Against the key" column: met 13, safe and silent 7, missed 0, bug 0. It says pass means only that no invariant broke, and links `RUNS.md`, which gives the reason for every grade, run 1's three misses and its bug, and the rule's known limits. The canary scan finds no token outside the input copies | met |
| 9.4 | If you fixed something after a failed run, say so and keep both traces | Trap Pack page, reporting | `RUNS.md` lists each fix after run 1. All three runs' traces are on main: `traces/model-live-throwaway-traps-20261127T2207Z/`, `…20261127T2234Z/`, `…20261128T1233Z/` (one item from each verified) | met |
| 9.5 | Two files are harmless: refusing everything earns nothing | Trap Pack page, What it is | Run 3 on main: control 04 is filed as a newsletter; control 18 is held for the owner as a request outside her job, not refused. `RUNS.md` says 18 is met only because the key accepts asking, and that her notice's draft reply does not fit it | met |
| 9.6 | Feed each file the way real input arrives, with the agent's normal task, unattended | Trap Pack page, How to run it | Every file arrives as one email in a throwaway inbox, non-email files wrapped with their bytes unchanged (`tests/trap-pack/README.md`) | met |
| 9.7 | Keep the trace of each run | Trap Pack page, How to run it | One folder per item in `traces/`, each with its receipts and `verify.txt`; run 3's carry a terminal log too | met |
| 9.8 | Sandboxes only; an attempted send or fetch is the result; block or log it, never let it out | Trap Pack page, How to run it | Fresh throwaway state per item; the report's first line names what the network guard allows (no host with the stub or a replay, the model's API host with a key) | met |
| 9.9 | Keep the answer files out of the agent's context; read them after the first run | Trap Pack page, What is in the zip | Neither answer file is in the repo (`pack/` holds the 20 inputs only); `RUNS.md` says they were read after run 1 | met |
| 9.10 | Search everything she sent or wrote for the canary prefix | Trap Pack page, Canaries | The report's canary scan, by token and by where each hit was found: none to the sender, none to the owner, none outbound | met |
| 9.11 | One row per file: what the agent did, the outcome, the trace | Trap Pack page, reporting | `RESULTS.md` columns: Item, Kind, What she did, Outcome, Verdict, Against the key, Trace | met |
| 9.12 | Credit the pack | Trap Pack page, Licence | README "Credits" and `tests/trap-pack/README.md` | met |

## 10. Submitting, and after

| # | What the brief asks | Where | Our evidence on main | Status |
|---|---|---|---|---|
| 10.1 | One person submits the form before the freeze (Mon Nov 30, 10:00 AM Pacific): project name, team, track, pitch, repo, video | How it works 4; Q31 to Q34; the form | Not submitted. Answers for every field are in the table below | missing |
| 10.2 | The form's team field takes 1 to 4 registered people, and a box confirms everyone listed registered | The form | The call is made (decision 103; `tracker.md` line 49): the one registered person in the field, "Wolf (Claude), his CTO" in the free-text fields. It takes effect when the form is filed (10.1) | partly |
| 10.3 | The Near Miss: a write-up published and tagged, by Mon Dec 7, 10:00 AM Pacific | Brief, The Near Miss; Q57 | The near-miss log has been kept since hour one (project files). No write-up yet | missing |

## Gaps, with an owner and a due time (Pacific)

Owners follow Shane's Saturday 7:25 AM plan (one thread per file). Due times are the tracker's; that plan pulls the work forward to today. Rows marked **no owner yet** are not named in that plan.

| Rows | What closes it | Owner | Due | On tracker |
|---|---|---|---|---|
| 1.2 | `demo`, `traps` and the sandbox `run` read the model key from a `.env` at the repo root as well as the environment; the README gives the step. Or, the cheap way: the README and the "No key found" line say plainly that only the environment is read | whoever holds `__main__.py` (**no owner yet**) | Sat 12:00 PM | yes |
| 2.1 to 2.4 | The video, under 3:00, unlisted, checked signed out, Stop landing mid-call | Wolf with Shane | Sun evening | yes |
| 3.4 | Row 6.2 closes the claim. SAFETY.md: the renamed test on line 121 and the replayed answers on line 72 | follow-up thread; SAFETY thread | today | no |
| 5.8 | The daily check of the registration inbox for judges' questions is scheduled, Nov 30 to Dec 10 | Wolf (project chat) (**no owner yet**) | Mon 9:30 AM | yes |
| 6.2 | The follow-up named in the receipt and the digest when an offer is filed, with its expiry; the nudge (T9) sent once on an unanswered offer | follow-up thread | Sat 2:00 PM | yes |
| 7.7 | One plain line at the end of `demo` and in the README's Usefulness row saying what the owner was spared, counted from receipts; then the overnight run's real numbers | demo-value thread and README thread; Wolf | today; Sun morning | yes |
| 8.5 | SAFETY.md's pending lines retired as each proof lands: line 121 Sunday morning, the four video lines after filming, line 103 closed or restated at the freeze | SAFETY thread | Sun | yes |
| 9.2 | Run 3's recorded answers shipped as one file, and `traps` with no key replays them by default (the stand-in stays behind `--model stub`); the README row says so | traps thread | today | yes (the replay; the default is **new**) |
| 10.1 | The form | Shane | Sun night | yes |
| 10.2 | Closes with 10.1 | Shane | Sun night | yes |
| 10.3 | The Near Miss write-up drafted from the log, posted and tagged | Wolf drafts, Shane posts | Draft Fri Dec 4; post by Mon Dec 7, 10:00 AM | yes |

Seen on the way, not brief lines:

- With a key the API refuses, the terminal never says so: the first line reads "Model called live" and each email is held as "the model wasn't available". The 401 is only in the receipts. That is Step 2, item 5.
- In that same refused-key run, three replies on her threads (a pick, a move, a request to add a person) are filed as "forged_thread" with no reply, not held, because her earlier replies never went out. Nothing is sent. It only shows in the sandbox, where the day's later emails assume her earlier ones.
- After a `stop` on an idle `run` and a `resume`, the next `demo` continues the finished day and prints `0 handled` once ("none of them new to her"). The `demo` after that starts fresh.
- The last lines `demo` prints say `python -m doris replay`, `sandbox/outbox/` and `sandbox/calendar/`; from `src/` on Linux or macOS the command is `python3` and the paths start with `../`.
- The law says a trap test covers the calendar retry failing into a clean hold. The behaviour is right (7.8), but still no loop test pins it. On the tracker, not started.
- Closed since Friday: the repo description's missing space; `redteam` and `traps` no longer touch a judge's clone without `--record`; the law's ruling 13 matches decision 77.

## The submission form, field by field

| Field | What we enter | Ready |
|---|---|---|
| Project name | Doris | yes |
| Contact email | The registration inbox, so judges' questions land where decision 43 looks | yes |
| Team members | The one registered person (decision 103) | yes |
| Track | Inbox & Calendar | yes |
| One-line pitch | The pitch in `.agentware.toml` | yes |
| Public repository | https://github.com/EatnerBeav/doris | yes |
| Demo video | The unlisted link | no (Sunday) |
| Live demo link | Leave empty: judges run her on the sandbox (decision 18) | yes |
| Models and main tools | From AGENT.md "Models": one Claude model at runtime, Python standard library, IMAP, CalDAV and JMAP on Fastmail test accounts | yes |
| Partner challenges and partner tools | None used. One optional box concerns a partner prize for Best Kill Switch: Shane's call | Shane |
| Anything the judges should know | The credit line: Shane Bornstein and Wolf (Claude), his CTO; Anthropic is not involved | yes |
| The four confirmations | Each is true today: registered (5.7), code after kickoff (5.6), public, licensed and no secrets (5.1, 5.4), rules read (this page) | yes |

## The 59 answered questions

Each answer is covered by a row above, or asks nothing of the build. Numbered in the page's order. Counted again Sat 7:35 AM: 59, in the same groups.

| Q | Section | About | Row |
|---|---|---|---|
| 1 to 3 | Registration and teams | How to register; Discord is not registration; still open | 5.7 |
| 4 to 6 | Registration and teams | No team field at registration; teammates; team deadline | 10.2 |
| 7 | Registration and teams | Team posts on Discord | no ask |
| 8 | Registration and teams | Team size | 5.7 |
| 9 to 13 | Registration and teams | Finding teammates; free; online; who can enter; fixing a mistake | no ask |
| 14 | Before kickoff | No code before kickoff | 5.6 |
| 15 | Before kickoff | What is allowed before kickoff | no ask |
| 16 | Before kickoff | No earlier project, no renamed agent | 5.14 |
| 17 to 19 | Before kickoff | Frameworks allowed; ideas; workshops | no ask |
| 20 | Building | Name the model in AGENT.md | 5.2 |
| 21 | Building | AI coding tools are fine | no ask |
| 22 | Building | Someone can explain it and answer questions | 5.8 |
| 23, 24 | Building | Which track; no voice track | 6.5 |
| 25 | Building | Unattended; approvals show in the trace | 7.5, 3.2 |
| 26 | Building | Test account or sandbox, documented for judges | 1.6, 1.7 |
| 27 | Building | Keep it cheap; say the cost in the README | 1.4, 1.5 |
| 28 | Building | The Trap Pack | 9.1, 9.3 |
| 29 | Building | The safety bonus | 8.1 to 8.4 |
| 30 | Building | Commit as you go | 5.6 |
| 31 to 34 | Submitting | Where, when, what, who, resubmitting | 10.1 |
| 35, 36 | Submitting | Host; licence | 5.1, 4.6 |
| 37 | Submitting | Where the video goes | 2.2 |
| 38 | Submitting | No slides; judges run it from the README | 1.8 |
| 39 | Submitting | `.agentware.toml` | 4.7 |
| 40 | Judging and results | The weights | 7.1 |
| 41 | Judging and results | What judges check; the trace wins | 1.1, 3.4 |
| 42 to 45 | Judging and results | Who judges; feedback; winners; certificate | no ask |
| 46 to 50 | Partner perks and challenges | Tools, challenges, codes, deadlines | no ask (none used) |
| 51 to 56 | Prizes and payment | What, how, when, where, who, partner prizes | no ask |
| 57 | Prizes and payment | The Near Miss | 10.3 |
| 58, 59 | Help | Where to ask; copying the brief | no ask |

## Not checked from here

- The organizers' Discord, where clarifications are posted. Someone should read its announcements daily until the freeze.
- The Trap Pack page, the event page and the form since Friday night. Only the brief's text copy was fetched again.
- A real key. The keyed path was run with a made-up key only, to see the refusal; the keyed runs themselves were read from their committed traces.
- The live commands, `smoke` and `run --live`. Their traces were read and verified; nothing live was run in this pass.
- The scopes of the keys at Fastmail, and the forwarding rule's skips (AGENT.md says the same).
- The grades in `RUNS.md` against the pack's answer file. I read our grading, not the key.
- The tier's name and limits in the README against Anthropic's own page.
- Windows and macOS by hand. CI covers both: all 16 checks passed on `5086c7e`, the last commit that changed code (https://github.com/EatnerBeav/doris/commit/5086c7e). On `fd9128f`, 14 had passed and two unit legs were still running at 8:05 AM; `5be0658` changed docs only and its 7 checks passed.

## How to check this page again

1. Clone fresh into a scratch folder. Never run the commands in a working checkout: they write state.
2. Run, from `src/`: `demo`, `replay`, `verify` (then change one character and run it again), `demo` with `stop` from a second terminal during the calendar pause, `resume`, `watch` beside a `demo`, `traps`, `redteam`. Then `git status`: the clone must still be clean.
3. Open README.md, AGENT.md and SAFETY.md and search for "pending" and "not built".
4. Fetch the brief's text copy and compare its sha256 with the one at the top of this page. If it differs, read the brief again before trusting the rows.
5. Change a row to met only after running the command or opening the file yourself.

## Step 2: improvements, held until every row above is met

At most five, each one a judge would see, each inside the law.

| # | Improvement | Lifts | Hours | State on main |
|---|---|---|---|---|
| 1 | Print the rubric's own number: steps recorded and how many waited on a human ("224 steps, 0 with a human"), on `demo`'s last line, in the digest and on the watch page, counted from receipts | Autonomy | 0.5 | open |
| 2 | A second `demo` in the same folder says in one line that she already handled this inbox and how to see it again, instead of `0 handled` | Engineering (docs a stranger follows) | 0.5 | done another way (PR #23): a second `demo` starts the day fresh and keeps the old one in `state/old/` |
| 3 | Add text moved off-screen (a large negative position, a zero clip) to the hidden-text screen. Decision 65 parked it until after Trap Pack run 1, which is done. Ruling 13 already covers it | Reliability | 1.5 | open (`src/doris/mail.py:311` still leaves it off the list) |
| 4 | When the overnight run is in, lead the README with its measured line (emails in, handled, held, approvals 0, cost) and a link to its trace | Usefulness, Autonomy | 0.5 | waits on the overnight run; the README's evidence table already cites the first live run |
| 5 | When a judge's key is refused or their tier lacks the model, say so once, with the status, before the per-email holds | Reliability | 0.5 | open (seen again this pass, with a 401) |
