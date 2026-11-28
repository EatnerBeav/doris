# The video: script and shot list

Doris, Agentware 2026, track T1. Shane Bornstein narrates. Hard limit 3:00; this script runs about 2:56.
Credit, exactly: **Shane Bornstein and Wolf (Claude), his CTO.** Hire the Machines is the only business named. Anthropic is not involved in the entry. Doris never narrates.

How to film it: [filming-plan.md](filming-plan.md) (Saturday rehearsal, Sunday filming, the edit).

## Rules for every frame

1. **Only what really ran.** Every word and number on screen comes from Doris's own output, her receipts or her mail. The edit adds labels and credits, nothing else.
2. **The live run has no cuts.** One continuous recording from Rae pressing Send to the event on the calendar. The wait for Doris's next pass is sped up, labelled "sped up, nothing cut", with the clock visible. Punch-ins are fine.
3. **Labels.** Each beat carries a strip in the bottom-left corner: `LIVE`, `SANDBOX` or `STAGED`, worded as in the shot list.
4. **The narration follows the screen.** If a count or a word differs on the day, Shane says what the screen says.
5. **Never on screen:** the server's name, the env file, any key or token, the ssh or tunnel commands, the real meeting link (cover it with `[meeting link]`, as the law says), anyone else's mail, and the three names on the never-mention list.

## Shot list

| Time | On screen | Shane says | Where it comes from |
|---|---|---|---|
| **0:00 to 0:10** The problem | Title text over black, one line at a time, the sample day's real subject lines: "Quick call this week?" "Intro with Shane" "Friday?" Each gets a grey "no reply" under it. | "When I'm out with a client, my inbox waits. Someone asks for a meeting, hears nothing all day, and books someone else." | Subjects from `sandbox/inbox/` |
| **0:10 to 0:22** Doris | Title: **Doris**. Under it: "An inbox and calendar agent you can walk away from." Lower third: "Shane Bornstein and Wolf (Claude), his CTO · Hire the Machines" | "So Wolf, my CTO, who is Claude, and I built Doris. My email is forwarded to her, and she runs it and my calendar while I'm gone. Nobody approves anything." | |
| **0:22 to 0:32** Live: the trigger | Strip: `LIVE · real mail, real calendar · Sun Nov 29 · nothing cut`. Three windows tiled: the watch page (left), Rae's Gmail (top right), Shane's Fastmail calendar (bottom right). Rae sends **"Quick call Thursday?"**: "Hi, could Shane do Thursday, December 3 at 2:00 PM Pacific for 30 minutes? I'd like to talk about our scheduling. Rae". Twenty seconds later, **"Pilot question"**: "What would Shane charge for a three-month pilot, and what's included?" | "This is live: real email, my real calendar. Rae, a test prospect, asks for Thursday at two, Pacific. Then she asks what a pilot costs." | Rae's Gmail |
| **0:32 to 0:40** Live: the wait | Same windows, sped up. Label: "sped up, nothing cut". The clock in the corner runs to the next ten-minute mark. Nobody touches anything. | "Doris checks her inbox every ten minutes. Nobody touches anything." | Passes run at :00, :10, :20 (`loop.wait_for_cycle`) |
| **0:40 to 0:55** Live: she works | Real time. Punch in on the watch page as the panes tick: read, classified meeting request, "Plan: book" with its why, gate passed, calendar write, read back, reply sent, her copy found in Sent. | "She reads it, checks my calendar, books the slot, reads the booking back to make sure it landed, and replies." | Watch page, from her receipts |
| **0:55 to 1:05** Live: Rae's side | Punch in on Rae's Gmail: Doris's reply lands on the same thread: "You're booked with Shane Bornstein for Thu Dec 3, 2:00 PM Pacific, 30 min.", the .ics attached, the AI signature. | "Rae gets a confirmation with a calendar file, signed as an AI." | Rae's Gmail |
| **1:05 to 1:10** Live: my side | Punch in on the Fastmail calendar: the event appears on Thursday at 2:00 PM with no refresh. Then the watch page header: "1 handled, 1 held, 0 human approvals so far" (as worded on the day). | "And it's on my calendar." | Fastmail; watch page |
| **1:10 to 1:30** The hold | Shane's Fastmail inbox: **"Held for you: Rae Wolfe"**. Punch in on "money, legal or a complaint", on "Nothing has been sent to them", and on the draft that starts "Hi Rae, Shane here." Cut to Rae's "Pilot question" thread in Gmail: no reply. | "The price question needs me, so she held it. Rae got nothing. I got the reason, and a draft I can send myself. Everything Doris sends is a sentence I approved. The model only reads; code writes." | The S1 notice; Rae's Gmail |
| **1:30 to 1:52** A judge's day | Strip: `SANDBOX · sample mail and calendar` plus the run's own first line (for example "Model answers replayed from recorded run ..."). A Windows terminal on a fresh clone: `py -m doris demo`. Fifteen lines. Highlight Marcus: "Staged fault timeout-once: retried once." Highlight Sam: "Another event landed during the booking: her event removed, offer fresh times." Hold on the ending: what she did for the owner, as the demo prints it on the frozen commit. | "Judges run the same agent on a sample day: one command, no install, no key. Fifteen emails. The calendar times out, and she retries once. A meeting lands mid-booking, so she removes hers and offers new times. And at the end, what she did for me, from her receipts." | `demo` output |
| **1:52 to 2:12** The Trap Pack | The same Windows terminal, no key: `py -m doris traps`. Its first line names the graded run whose answers it replays. The twenty rows, then the totals line. Then the first lines of `tests/redteam/RESULTS.md`: the yes-man model, 0 bad actions. | "The organizers gave every team twenty traps: fake bosses, poisoned pages, broken files. To Doris, email is data, never orders, and she opens no links and no attachments. **[Trap line, below.]** Then we gave her a model that obeys every trap. The gate, in code, let zero bad actions through." | `traps` output; `tests/trap-pack/RUNS.md`; `tests/redteam/RESULTS.md` |
| **2:12 to 2:44** Stop, mid-call | Strip: `SANDBOX · STAGED: calendar slowed 8 seconds so the Stop lands mid-call`. The watch page fills the screen: the four panes and the red Stop. Aisha's request runs (it is already in the sandbox inbox). The current step reads "Tool call: calendar_query span, no answer yet." Shane clicks **Stop**. The page turns red: "Halted: Stop pressed on the watch page. When stopped, calendar_query span was in flight; dropped 1: book." Punch in on the halted receipt line. Cut to the terminal: "Halted ... The worker was dead [N] ms after the stop. In flight: ... Dropped 1 queued item(s)." Then `python -m doris demo` again: "Not started: Doris is halted ..." | "This is her watch page: every step, why, and what it's cost. I've slowed the sample calendar so you can see this. She's waiting on my calendar. Stop. The call is cut off mid-flight, the booking she planned is dropped, and the receipt names both. Nothing booked, nothing sent. And she stays stopped until I resume her from my own terminal." | Watch page; halted receipt; terminal |
| **2:44 to 2:56** Close | `python -m doris verify`: "ok: [N] lines, every hash matches the line before it." Then a full-screen card: **Human approvals: 0**. Last card: "Doris · github.com/EatnerBeav/doris · Shane Bornstein and Wolf (Claude), his CTO · Hire the Machines". Small line under it: "Anthropic is not involved in this entry." | "Every step leaves a receipt chained to the one before, and verify checks every link. Human approvals: zero. Doris. By Shane Bornstein and Wolf, my CTO. Hire the Machines." | `verify`; receipts |

### The trap line

Filled from the newest graded run in `tests/trap-pack/RUNS.md` on the frozen commit, never before. The generated report's "pass 20" checks her own invariants; the grade against the pack's answer key is the honest number.

- Nothing graded "missed" or "bug": "Nothing unsafe got through."
- Some missed: "[N] of twenty went her way, and the report says exactly what happened to the rest."

### Optional, only if the overnight trace is committed

After "Human approvals: zero", add: "Last night she ran on her own for [N] hours: [N] emails, [N] booked, [N] held." About 4 seconds. Every number from the overnight trace's manifest. Cut it first if the video runs long.

## If it runs long

Rehearse with a timer. Cut in this order, and stop as soon as it fits under 2:58:

1. The overnight line.
2. "fake bosses, poisoned pages, broken files"
3. "A meeting lands mid-booking, so she removes hers and offers new times." (Keep the timeout: the law wants it on video.)
4. "The model only reads; code writes."

Never cut: the live run, the hold, the timeout and retry, the Stop with its halted receipt, the credit.

## If the live run fails on Sunday

Use the first that exists:

1. **Saturday's rehearsal take**, if it was recorded clean. The strip reads `LIVE · Sat Nov 28`. Same script.
2. **The sample day with the model called live**, on the watch page. The strip reads `SANDBOX · model called live`. 0:22 to 1:10 becomes: "This is the sample day, with the model called live. Fifteen emails, nobody touching anything." and runs over the watch page and the demo's lines. The hold is Elena's notice from `sandbox/outbox/` opened in a mail app: "Elena asks about price. That needs me, so she held it, told me why, and left me a draft." Beat 1:30 then keeps only the timeout and the ending.

## Know this (Shane can explain every part)

| Beat | One line to know |
|---|---|
| Live | Mail to whizz@ is forwarded to Doris's own mailbox; she reads it over IMAP, books over CalDAV and replies through Fastmail JMAP. Her event has no attendees, so the calendar server emails nobody. |
| Book | She books straight away only when a plain date parser and the model agree on one date, time and zone, and the slot is open. Anything vaguer gets 1 to 3 offered times, on the days and in the part of day the sender named. A clear time that is taken gets the nearest free times: that day's slot nearest to it, then the same time on the nearest other days. |
| Read back | After every write she reads the calendar again; if anything else overlaps, she removes her own event and offers new times. |
| Hold | Money, legal, complaints, anything outside her classes, or unsure: held for me with the reason and a draft. Nothing goes to the sender. |
| Templates | Every sentence she sends is a fixed template I approved. The model classifies; code picks the action and fills the template. |
| Trap Pack | The organizers' 20 hostile files. She never opens a link, attachment, invite or image, so most traps have nothing to grab. |
| Yes-man | We replaced the model with one that obeys every injection. The gate still blocked everything harmful: 0 bad actions in 206 runs. |
| Stop | The worker runs as a child process. Stop kills it mid-call, writes a halted receipt naming the call in flight and what was dropped, and the halt survives a restart. Only `resume` on the command line clears it. Email can never start her. |
| Receipts | Every step is one JSON line whose hash includes the line before it. Change one character and `verify` fails at that line. |

## Approaches considered

- **Live take.** Chosen: Doris is already running, Rae's mail lands, and her next ten-minute pass picks it up; the wait is sped up and labelled. It shows the walk-away story with no keypress. Not chosen: pressing Enter on camera to start her with the mail waiting (real time, but a hand starts it).
- **The Stop.** Chosen: the sandbox with a staged slow calendar (decision 52), so the Stop lands mid-call every take and nothing live is half-written. Not chosen: a Stop during a live model call, which is a two-second window and can't be retaken cheaply.
- **The hold.** Chosen: Rae's live price question, so the hold notice lands in Shane's real inbox. Fallback: Elena's notice from the sample day.
