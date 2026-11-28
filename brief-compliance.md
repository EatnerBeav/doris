# Brief compliance

Every line of the organizers' pages that asks for something, or says how judges score, mapped to what we can show today.

- **Read:** Fri Nov 27, 2026, 11:00 PM Pacific, from the live site: the brief with all 59 answered questions (https://agentwarehack.com/brief/ and its text copy https://agentwarehack.com/brief/agentware-brief.md), the Trap Pack page (https://agentwarehack.com/trap-pack/), the event page (https://agentwarehack.com/, for the Track 1 text and the named loopholes), the submission form (https://tally.so/r/WJzRbk) and the kickoff email.
- **Checked against:** `main` at `a13cadd`, on a fresh clone, Linux, Python 3.13, no key, no network. Every "met" below is something I ran or opened myself in this pass. Nothing is marked met on a plan, a memory note or another thread's report.
- **Wording:** each row states the ask in our own words and points to the place on the page that says it, so anyone can check a row against the source in seconds. The organizers' text is not reprinted here.
- **Status words:** **met** (on main, verified) · **partly** (some of it verified, the rest absent or only on a branch) · **missing** (nothing on main yet) · **wrong** (what main does contradicts the line).

## Count

75 rows: **met 50 · partly 14 · missing 9 · wrong 2.**

Six of the nine missing rows are deliverables that are not due yet (the video, four rows; the submission; the Near Miss write-up). The other three are work: the README's cost line, the README's rubric table, and one call from Shane on the form's team field.

## The three riskiest gaps

1. **A judge with no key never sees a real model, and the brief's own key step does not work.** `demo` and `traps` fall back to a keyword stand-in, and say so. The brief lists hardcoded outputs among the things that will not score, and it tells judges to fill in the example env file. I filled in a `.env` and `demo` ignored it: the key is read only from the process environment, and the README says nothing about a key, a tier or a cost. Rows 1.2, 1.3, 1.4, 5.9, 9.2. Touches Autonomy, Reliability and Engineering.
2. **The Trap Pack report on main overstates.** `tests/trap-pack/RESULTS.md` says pass 20 and "Failures: None" for run 1. Our own grading of that same run in `RUNS.md` is met 10, safe and silent 6, missed 3, bug 1, and the report's canary section shows two leak canaries reaching the owner. The run on the fixed code lives only on a branch. The Trap Pack page asks for honest over tidy and for both traces. Rows 9.3, 9.4, 9.5. Reliability, 20%.
3. **The last step of Track 1's own example run is invisible.** The example ends with the agent filing the follow-up. Her ledger keeps each offer with its expiry, but no receipt line, terminal line or digest line names a follow-up, and the one nudge on an unanswered offer is not built (the template exists; nothing sends it). Row 6.2. Usefulness, 25%.

## 1. A judge can run it

| # | What the brief asks | Where | Our evidence on main | Status |
|---|---|---|---|---|
| 1.1 | Clone, follow the README, and have the agent working inside 15 minutes | Brief, What to submit, judge item 1; Q41 | README "Run it". Fresh clone, `cd src && python3 -m doris demo`: 13 seconds, ends `13 handled, 2 held, 0 human approvals`, chain verified. `ci.yml` runs `demo` and `traps` on Linux, macOS and Windows | met |
| 1.2 | The same sentence has the judge fill in the example env file | Brief, What to submit, judge item 1 | Tried it: copied `.env.example` to `.env`, filled the model key, ran `demo`. It still ran the stand-in. `__main__.demo_source` reads the process environment only. `.env.example`'s header speaks to our server, not to a judge. The law says a key in `.env` makes the same commands call the model | wrong |
| 1.3 | Name the API keys and the tier | Brief, What to submit, judge item 1 | `.env.example` and AGENT.md Permissions name the model key. No file states a tier (`grep -i tier`: nothing). The README names no key | partly |
| 1.4 | The README says clearly what a test run costs | Q27 | No cost in the README. Measured costs exist elsewhere: `status/2026-11-27.md` ($0.06 for a 20-file Trap Pack run, $0.0038 for a live reply) and AGENT.md Models | missing |
| 1.5 | Keep it cheap: a free tier, a mock mode or a recorded trace is fine | Q27 | `demo`, `traps`, `redteam`, `verify`, `replay` and `watch` all ran with no key and no network, at $0 | met |
| 1.6 | A sandbox, test account or mock mode for anything personal; never ask a judge for credentials; a personal mailbox is not a setup guide | Brief, Rules 4; event page, named loopholes | `sandbox/` (15 emails, a calendar of files, an outbox). No doc asks a judge for a credential. AGENT.md Permissions says judges need none | met |
| 1.7 | Document how a judge can use a sandbox or test account themselves | Q26 | `sandbox/README.md` tells a judge how to drop in their own `.eml`. The README does not link it, and the optional live setup with a judge's own test accounts is not written | partly |
| 1.8 | Judges run it from the README over Nov 30 to Dec 10, with or without a live link | Q38; Brief, How it works 5 | Nothing in the judge path needs our accounts or our server: `traps` prints that its network guard allows no host, and every command above ran offline | met |
| 1.9 | README: what it does, how to run it, honest limits | Brief, layout | What it does: one line. How to run: yes. Honest limits: not in the README (they are in AGENT.md "Limits we state" and `tests/trap-pack/RUNS.md`) | partly |
| 1.10 | Docs a stranger follows without asking a question | Rubric, Engineering | The README is 37 lines and stops at `demo`. It does not say where the trace is or what to run next. A second `demo` in the same folder prints `0 handled` with no hint why | partly |

## 2. A judge can watch it

| # | What the brief asks | Where | Our evidence on main | Status |
|---|---|---|---|---|
| 2.1 | A video of three minutes or less: one full unattended run, trigger to result, nobody at the keyboard in the middle | Brief, What to submit; Q32 | No video yet | missing |
| 2.2 | The video opens without an account (unlisted YouTube is fine) | Q37 | No video yet | missing |
| 2.3 | The video shows one real run, clearly enough that a judge believes it before running it | Rubric, Demo | No video yet. "Real" means the filmed run calls the model (decision 42, to settle at Saturday's rehearsal) | missing |
| 2.4 | The kill switch is shown working in the demo | Safety bonus, Kill Switch | No video yet | missing |

## 3. A judge can read what it did

| # | What the brief asks | Where | Our evidence on main | Status |
|---|---|---|---|---|
| 3.1 | Every run leaves a trace: what it saw, decided, called, and what came back | Brief, What to submit, judge item 3 | `replay` on the fresh demo run prints, per email, Saw, Classed, Decided, Checked, Called with its result, Verified, Closed, each with its receipt line numbers (224 lines) | met |
| 3.2 | The trace is the receipt: approvals in the trace are scored as approvals | Brief, What to submit | The summary receipt (line 224) holds `human_approvals: 0`; the two holds are labelled holds. Live smoke 3's manifest: `human_approvals: 0` | met |
| 3.3 | `traces/` holds real run logs, start to finish | Brief, layout | Committed: three live smokes (two failed ones kept), Trap Pack run 1 with the live model, a stub run, the yes-man run. `verify` passes on smoke 3 and on run 1's item 01. Not there yet: any trace of her left running for a day or a night, or of a booking made live | partly |
| 3.4 | If the README claims more than the trace shows, the trace wins; an honest unfixed failure beats a confident README | Q41; Brief, What to submit | The docs mark unbuilt things "pending". A script checked all 282 tests, functions and paths cited in SAFETY.md, AGENT.md and the README: every one exists except `sandbox/answers.jsonl`, which AGENT.md itself calls pending. Each "Prove it" in SAFETY.md reproduced as written. The one overstatement found is row 9.3 | met |
| 3.5 | SAFETY.md says what is enforced in code, not what the model was told | Brief, What to submit | SAFETY.md cites a function and a test for every rule (checked as in 3.4); `gate.py` holds G1 to G17 | met |

## 4. On disk

| # | What the brief asks | Where | Our evidence on main | Status |
|---|---|---|---|---|
| 4.1 | AGENT.md: architecture, tools, permissions, track, models | Brief, layout | All five are there: "Architecture", "Her tools", "Permissions", track T1 on line 3, "Models" | met |
| 4.2 | SAFETY.md: guardrails, kill switch, monitoring, audit | Brief, layout | Four sections under the brief's own four names | met |
| 4.3 | `.env.example`: every key, no secrets | Brief, layout | 12 names, no values; every name the code reads from an env file is listed | met |
| 4.4 | `src/`, all written in the 72 hours | Brief, layout | First commit 2026-11-27 18:10:53 UTC, ten minutes after kickoff; 169 commits since | met |
| 4.5 | `tests/` including the Trap Pack results | Brief, layout | `tests/trap-pack/RESULTS.md`, `RUNS.md`, the pack itself, `tests/redteam/RESULTS.md` | met |
| 4.6 | LICENSE: MIT or Apache-2.0 | Brief, layout; Q36 | `LICENSE` is MIT; GitHub reports the licence as MIT | met |
| 4.7 | `.agentware.toml`: the track and a one-line pitch | Brief, layout; Q39 | `track = "T1"` and the pitch | met |

## 5. Rules

| # | What the brief asks | Where | Our evidence on main | Status |
|---|---|---|---|---|
| 5.1 | Open source under an OSI licence, public at submission | Rules 1 | GitHub API at 11:05 PM: public, MIT | met |
| 5.2 | Name the model and stack in AGENT.md, and roughly what a judge needs | Rules 2; Q20 | AGENT.md Models: `claude-sonnet-5-5`, Python 3.9+, standard library only; Commands: no install, no key | met |
| 5.3 | It has to act: real actions, real tools, real or sandboxed systems | Rules 3 | Sandbox: a fresh demo wrote 9 emails and a calendar event to disk (opened T1, S1, S2 and the `.ics`). Live: smoke 3's trace shows her send found in Sent and in the sender's inbox, and a calendar write read back | met |
| 5.4 | No secrets in the repo; a committed secret disqualifies | Rules 5 | `python3 src/secret_scan.py --all`: clean. My own search of every commit on every fetched ref for key shapes: none (the Trap Pack's own fake keys aside). `.env` is ignored; CI runs the scan | met |
| 5.5 | Do no harm: no spam, nobody who didn't write first, no terms broken, no money without a hard limit, destructive actions tested on sandboxes | Rules 6 | Demo `envelope.jsonl`: every send goes to the sender plus the owner's copy, or to the owner alone. Live traces use our three test accounts only. Model spend stops at $5.00 a run (G3) | met |
| 5.6 | New code only, and commit often | Rules 7; Q14, Q30 | Repo created empty at 17:55 UTC; first commit 18:10:53 UTC; 169 commits over the first 13 hours | met |
| 5.7 | Every builder registers; teams of 1 to 4 | Rules 8; How it works 1; Q1 | The form's confirmation mail is in the registration inbox. One registered person, with Wolf (Claude) credited as CTO | met |
| 5.8 | Someone on the team can explain it and answers the judges' written questions | Rules 9; Q22 | Decision 43 gives the job to the project chat, daily from Nov 30 to Dec 10. Nothing is scheduled yet that I can see | partly |
| 5.9 | No prerecorded demo over hardcoded outputs; judges will run it | Rules, will-not-score list; event page, named loopholes | The real loop, gate and adapters run (changing a staged fault changed the outcome), but with no key the reader is a keyword stand-in, labelled stub. `sandbox/answers.jsonl` (answers recorded from a real model) does not exist yet | partly |
| 5.10 | Not a chatbot, not one model call in a wrapper | Rules, will-not-score list; event page, named loopholes | 15 emails, 224 recorded steps, 9 sends, 2 bookings, 1 cancel, each through the gate | met |
| 5.11 | Never message real people who did not consent | Rules, will-not-score list | As 5.5: replies only to the person who wrote, on their thread (G6) | met |
| 5.12 | No real money without a hard limit and an approval | Rules, will-not-score list | No payment tool exists; the demo's pricing email is held for the owner | met |
| 5.13 | Never pass as a human | Rules, will-not-score list | All 9 demo sends carry the two signature lines saying she is an AI | met |
| 5.14 | Not an earlier project and not a renamed open-source agent | Rules, will-not-score list; Q16 | History starts at kickoff; no framework, no dependency | met |

## 6. Track 1, Inbox & Calendar

| # | What the brief asks | Where | Our evidence on main | Status |
|---|---|---|---|---|
| 6.1 | Inbox triage, replies, scheduling, a daily digest | Brief, track 1 | Demo: newsletter, auto-reply and injection filed; money held; unreadable mail quarantined; T1, T2, T4, T18 sent; 2 bookings, a pick, a move; the digest at 5:00 PM | met |
| 6.2 | Follow-ups; the example run ends by filing the follow-up | Brief, track 1 and its example run | The ledger's `offers` table keeps each offer with its expiry. Nothing a judge reads names a follow-up (the word is in no receipt, no terminal line, no digest line). The nudge (T9) is a template with no code that sends it | partly |
| 6.3 | The example run: a request lands, she reads the thread, checks the calendar, books the slot, sends the reply | Brief, track 1, example run | Demo email 1 (lines 4 to 27): read, calendar read twice, booked, read back, T1 sent with its `.ics`, found in Sent. Email 7 books a pick from her own earlier offer on the thread | met |
| 6.4 | She handles mail instead of leaving a longer to-do list | Event page, track 1 | The digest leads with the ratio and lists only the two holds | met |
| 6.5 | Enter one track | Brief, tracks | `.agentware.toml`: T1 | met |
| 6.6 | Narrow and finished beats broad and half-done | Brief, tracks; Rubric, Usefulness | She does scheduling end to end and holds everything else (AGENT.md "The job"; 13 of 15 demo emails finished alone) | met |

## 7. Scoring rubric

| # | What the brief asks | Where | Our evidence on main | Status |
|---|---|---|---|---|
| 7.1 | Five criteria on a 5-point scale (30, 25, 20, 15, 10), averaged across judges, plus the safety bonus | Rubric; Q40 | The law's rubric-to-evidence table in the README, so a judge finds each criterion's proof: not written | missing |
| 7.2 | Autonomy: how much runs with nobody in the loop, checked in the trace | Rubric, Autonomy | 13 of 15 demo emails end to end; approvals 0 in the summary receipt | met |
| 7.3 | Autonomy: how many steps run without a human | Rubric, Autonomy | 224 recorded steps in the demo run, none waiting on a person (the number is in the receipts, not yet printed anywhere) | met |
| 7.4 | Autonomy: plans, checks and finishes on its own | Rubric, Autonomy | Every email has a `decided` plan line, `checked` gate lines, read-backs after each write, and a closing line | met |
| 7.5 | Unattended from trigger to result; asking a human before an irreversible step is good design | Q25 | Money, another person on the thread and anything unsure are held with a notice and a draft; nothing waits for an approval | met |
| 7.6 | The agent you would actually leave running | Brief, opening; event page, the one condition; Prizes, 1st place | `run` passes over the inbox every ten minutes until stopped (ran it). No trace yet of her left running: the live booking is in progress tonight, the overnight run is Saturday | partly |
| 7.7 | Usefulness: a job someone would hand over; real problem, real workflow, real time saved | Rubric, Usefulness | The demo shows the job. Nothing we ship yet states what the owner was spared, and no run on real mail over real hours is committed | partly |
| 7.8 | Reliability: bad input, a failed API call, a timeout, an ambiguous request, a Trap Pack email; recover, retry, ask or stop cleanly | Rubric, Reliability | All seen: unreadable mail quarantined and the run goes on; a calendar timeout retried once; no clear time gets an offer; an event landing mid-booking is put right; a key the API refuses holds everything and sends nothing (tested); the calendar failing twice is a clean hold (tested by changing the staged fault; no loop test pins it) | met |
| 7.9 | Engineering: clean architecture, sensible tools, permissions scoped to the job | Rubric, Engineering | Six steps, one policy table, one gate file (851 lines), every cap in one file (86 lines), five fixed action kinds; AGENT.md gives each credential's scope and what it cannot do. Limits: `loop.py` is 1,979 lines; the scopes at Fastmail cannot be checked from the repo | met |

## 8. Safety bonus

| # | What the brief asks | Where | Our evidence on main | Status |
|---|---|---|---|---|
| 8.1 | Guardrails: delete, send, pay, deploy and overwrite need confirmation or are blocked in code, not in the prompt | Safety bonus 1 | SAFETY.md's table of the five verbs; `gate.py`; `redteam` re-run: 0 bad actions in 206 runs of a model that obeys every injection | met |
| 8.2 | Live monitoring: current step, tool calls, decisions and cost so far, in real time | Safety bonus 2 | `watch` fetched three times during a run: the four panes, the call in flight shown as unanswered, a why on every decision, one Stop button | met |
| 8.3 | Kill switch: one action, at once, mid-run, pending calls cancelled; closing the laptop is not one | Safety bonus 3; event page, named loopholes | `run`, then `stop` from a second terminal: worker dead 18 ms later, the calendar call in flight cut off, 1 queued item dropped, a halted receipt, `verify` ok, no restart until `resume` | met |
| 8.4 | Audit trail: enough detail to reconstruct and replay | Safety bonus 4 | `verify` ok on 224 lines; one changed character names line 20 and exits 1; `replay` tells the run step by step | met |
| 8.5 | Best Kill Switch: all four, done properly | Prizes | Each of the four works (8.1 to 8.4). SAFETY.md still says pending in six places: four "On the video" lines, the paused-password check against the live servers, and the Trap Pack run on the fixed code. `stop` does not reach `traps` (the tracker's known limit) | partly |

## 9. The Trap Pack

| # | What the brief asks | Where | Our evidence on main | Status |
|---|---|---|---|---|
| 9.1 | Run it and commit the results in `tests/`; other tracks' files welcome | Brief, Trap Pack; Trap Pack page | `tests/trap-pack/RESULTS.md`: all 20 files, live model, run 1 | met |
| 9.2 | Judges will run it too | Brief, Trap Pack | `python3 -m doris traps` on a fresh clone: 20 files in 1.2 seconds. With no key it is the keyword stand-in, and it overwrites the committed `RESULTS.md` and adds a trace folder to the clone | partly |
| 9.3 | Know what happens when you fail; honest beats tidy | Brief, Trap Pack; Trap Pack page, reporting | `RUNS.md` is honest: met 10, safe and silent 6, missed 3, bug 1, with why. `RESULTS.md`, the report itself, says pass 20 and "Failures: None" for that run, does not link `RUNS.md`, and its own canary section shows two leak canaries sent to the owner | wrong |
| 9.4 | If you fixed something after a failed run, say so and keep both traces | Trap Pack page, reporting | `RUNS.md` says what was fixed. Run 1's trace is on main; run 2's is only on the branch `trap-pack-run-2` | partly |
| 9.5 | Two files are harmless: refusing everything earns nothing | Trap Pack page, What it is | Control 04 is filed as a newsletter. Control 18 was refused in run 1; on run 2's branch it is held for the owner with a notice. Main shows only run 1 | partly |
| 9.6 | Feed each file the way real input arrives, with the agent's normal task, unattended | Trap Pack page, How to run it | Every file arrives as one email in a throwaway inbox, non-email files wrapped with their bytes unchanged (`tests/trap-pack/README.md`) | met |
| 9.7 | Keep the trace of each run | Trap Pack page, How to run it | One folder per item in `traces/`, each with its receipts and `verify.txt` | met |
| 9.8 | Sandboxes only; an attempted send or fetch is the result; block or log it, never let it out | Trap Pack page, How to run it | Fresh throwaway state per item; the report's first line names what the network guard allows (no host with the stub, the model's API host with a key) | met |
| 9.9 | Keep the answer files out of the agent's context; read them after the first run | Trap Pack page, What is in the zip | Neither answer file is in the repo (`pack/` holds the 20 inputs only); `RUNS.md` says they were read after run 1 | met |
| 9.10 | Search everything she sent or wrote for the canary prefix | Trap Pack page, Canaries | The report's "Canary scan" section, by token and by where each hit was found | met |
| 9.11 | One row per file: what the agent did, the outcome, the trace | Trap Pack page, reporting | `RESULTS.md` columns: Item, Kind, What she did, Outcome, Verdict, Trace | met |
| 9.12 | Credit the pack | Trap Pack page, Licence | README "Credits" and `tests/trap-pack/README.md` | met |

## 10. Submitting, and after

| # | What the brief asks | Where | Our evidence on main | Status |
|---|---|---|---|---|
| 10.1 | One person submits the form before the freeze (Mon Nov 30, 10:00 AM Pacific): project name, team, track, pitch, repo, video | How it works 4; Q31 to Q34; the form | Not submitted. Answers for every field are in the table below | missing |
| 10.2 | The form's team field takes 1 to 4 registered people, and a box confirms everyone listed registered | The form | Needs Shane's call. Wolf cannot register, and nothing we publish calls this solo | missing |
| 10.3 | The Near Miss: a write-up published and tagged, by Mon Dec 7, 10:00 AM Pacific | Brief, The Near Miss; Q57 | The near-miss log has been kept since hour one (project files). No write-up yet | missing |

## Gaps, with an owner and a due time (Pacific)

Rows the tracker already carries keep its owner and time. Rows marked **new** are not on the tracker yet.

| Rows | What closes it | Owner | Due | On tracker |
|---|---|---|---|---|
| 1.2 | `demo`, `traps` and the sandbox `run` read the model key from a `.env` in the repo root as well as the environment; the README gives the step | Wolf (owns `__main__.py`) | Sat 12:00 PM | **new** |
| 1.3, 1.4 | The README's keys, tiers and cost table | README pass | Sun 6:00 PM | yes |
| 1.7, 1.9, 1.10 | The README: the judge's path after `demo`, a link to `sandbox/README.md`, known limits, optional live setup | judge-experience thread, then README pass | Sat 10:00 AM, final Sun 6:00 PM | yes |
| 2.1 to 2.4 | The video, under 3:00, unlisted, checked signed out, Stop landing mid-call | Wolf with Shane | Sun evening | yes |
| 3.3, 7.6 | The live booking tonight; the overnight run, its trace committed | Wolf | Tonight; Sat 8:00 PM start | yes |
| 5.8 | The daily check of the registration inbox for judges' questions is scheduled, Nov 30 to Dec 10 | Wolf (project chat) | Mon 9:30 AM | **new** |
| 5.9, 9.2 | Answers recorded from a real keyed run, so `demo` and `traps` replay a real model with no key; `traps` leaves the committed report alone | Wolf | Sat 12:00 PM | yes |
| 6.2 | The nudge on an unanswered offer, and the follow-up named in the receipt and the digest when it is filed | nudge thread | Sat 2:00 PM | yes (the nudge); **new** (naming it) |
| 7.1 | The README's rubric-to-evidence table | README pass | Sun 6:00 PM | yes |
| 7.7 | The owner's view at the end of `demo`; the overnight run's real numbers | demo-value thread; Wolf | Sat 12:00 PM; Sun morning | yes |
| 8.5 | SAFETY.md's six pending lines closed or restated at the freeze | docs thread | Sun, final pass | yes |
| 9.3, 9.4, 9.5 | A fresh live run on main; `RESULTS.md` carries the grade against the key beside each generated verdict and links `RUNS.md`; run 2's trace lands on main beside run 1's | Wolf | Sat 12:00 PM | yes (the run); **new** (the grade in the report, run 2's trace) |
| 10.1 | The form | Shane | Sun night | yes |
| 10.2 | Shane's call on the team field. Recommendation: list the one registered person, and credit Wolf (Claude), his CTO, in the two free-text fields | Shane | Sun 6:00 PM | **new** |
| 10.3 | The Near Miss write-up drafted from the log, posted and tagged | Wolf drafts, Shane posts | Draft Fri Dec 4; post by Mon Dec 7, 10:00 AM | **new** |

Seen on the way, not brief lines:

- The GitHub repo description reads "walk awayfrom" (a missing space). Owner: Wolf. Now.
- The law says a trap test covers the calendar retry failing into a clean hold. The behaviour is right (7.8), but no loop test pins it. Owner: Wolf. Sat 2:00 PM.
- `redteam` with no flag also rewrites its committed report and adds a trace folder to a judge's clone (PR #13 adds `--record`).
- The law's ruling 13 still says a hidden-text hold quotes the hidden text. Decision 77 reversed that after Trap Pack run 1, and the code follows the decision. The law needs one line changed; the exact text is with Shane.

## The submission form, field by field

| Field | What we enter | Ready |
|---|---|---|
| Project name | Doris | yes |
| Contact email | The registration inbox, so judges' questions land where decision 43 looks | yes |
| Team members | Shane's call (row 10.2) | no |
| Track | Inbox & Calendar | yes |
| One-line pitch | The pitch in `.agentware.toml` | yes |
| Public repository | https://github.com/EatnerBeav/doris | yes |
| Demo video | The unlisted link | no (Sunday) |
| Live demo link | Leave empty: judges run her on the sandbox (decision 18) | yes |
| Models and main tools | From AGENT.md Models: one Claude model at runtime, Python standard library, IMAP, CalDAV and JMAP on Fastmail test accounts | yes |
| Partner challenges and partner tools | None used. One optional box concerns a partner prize for Best Kill Switch: Shane's call | Shane |
| Anything the judges should know | The credit line: Shane Bornstein and Wolf (Claude), his CTO; Anthropic is not involved | yes |
| The four confirmations | Each is true today: registered (5.7), code after kickoff (5.6), public, licensed and no secrets (5.1, 5.4), rules read (this page) | yes |

## The 59 answered questions

Each answer is covered by a row above, or asks nothing of the build. Numbered in the page's order.

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
- The scopes of the keys at Fastmail, and the forwarding rule's skips (AGENT.md says the same).
- The grades in `RUNS.md` against the pack's answer file. I read our grading, not the key.
- Windows and macOS by hand. CI covers both, and its run on `a13cadd` passed on all six legs (https://github.com/EatnerBeav/doris/actions/runs/46118506580).

## Step 2: improvements, held until every row above is met

At most five, each one a judge would see, each inside the law.

| # | Improvement | Lifts | Hours |
|---|---|---|---|
| 1 | Print the rubric's own number: steps recorded and how many waited on a human ("224 steps, 0 with a human"), on `demo`'s last line, in the digest and on the watch page, counted from receipts | Autonomy | 0.5 |
| 2 | A second `demo` in the same folder says in one line that she already handled this inbox and how to see it again, instead of `0 handled` | Engineering (docs a stranger follows) | 0.5 |
| 3 | Add text moved off-screen (a large negative position, a zero clip) to the hidden-text screen. Decision 65 parked it until after Trap Pack run 1, which is done. Ruling 13 already covers it | Reliability | 1.5 |
| 4 | When the overnight run is in, lead the README with its measured line (emails in, handled, held, approvals 0, cost) and a link to its trace | Usefulness, Autonomy | 0.5 |
| 5 | When a judge's key is refused or their tier lacks the model, say so once, with the status, before the per-email holds | Reliability | 0.5 |
