# The video: rehearsal, filming and the edit

The words and shots are in [script.md](script.md). Times are Pacific.

Shane narrates, plays Rae (presses her Send), clicks Stop, records and edits. Wolf prepares the server, checks every take against its receipts, fills the numbers from the receipts and logs every stumble in the near-miss log.

## The setup (once, Saturday)

**Recording.** OBS on Shane's PC records the whole desktop, at the highest resolution the screen gives, 30 fps. The Windows clock stays visible in the taskbar: it is the proof the live take is real time. Voice is recorded separately, over the finished picture.

**Windows on screen**

- A terminal connected to the server. Before recording: `export PS1='$ '` and `clear`, font 20 pt or larger. The connect and tunnel commands are never on screen.
- The watch page through the tunnel at `http://127.0.0.1:8765`, browser zoom 125%.
- Rae's Gmail: signed in, inbox clean, no signature, no other tabs.
- Shane's Fastmail (whizz@): inbox clean, calendar in week view on Mon Nov 30 to Fri Dec 4.

For the live take, tile them: the watch page on the left half, Rae's Gmail top right, the Fastmail calendar bottom right. Punch in during the edit.

**On the server, off camera**

- The checkout is on the commit being filmed (`git log -1`).
- A live smoke under 24 hours old, or `run --live` refuses to start (`caps.SMOKE_RECORD_MAX_AGE`).
- `python3 -m doris run --live` started at least one pass before the take. Passes run on the clock at :00, :10, :20 and so on, plus the forward from whizz@ takes about 31 seconds, so Rae sends at minute 7 or 8 of a ten.
- `python3 -m doris watch` running, with the tunnel open.
- For the sandbox beats only: load the model key into the shell from the env file with one line that reads only `ANTHROPIC_API_KEY`, then `clear`. The Stop beat needs it: with recorded answers in the repo, a new email has no recorded answer, is held, and never reaches the calendar.
- **`stop` halts the sandbox and live together.** Film the live take first. Stop the live run before the sandbox beats, unless the build thread wants it running, and run `resume` after every Stop take.

**The Stop beat's email.** Copy [`stop-beat/14-aisha-request.eml`](stop-beat/14-aisha-request.eml) into `sandbox/inbox/` and add this line to `sandbox/inbox/facts.json`:

```
"14-aisha-request.eml": {"forwarded": "yes", "fault": "slow-calendar", "fault_seconds": "8"}
```

With the key loaded, `demo` starts a fresh day: the fifteen sample emails run first, then her at 3:20 PM, and every calendar call for her waits 8 seconds. Click Stop while the current step reads "no answer yet". Afterwards remove the file, put `facts.json` back with `git checkout sandbox/inbox/facts.json`, and `resume`.

Checked in the cloud on main 2804e08: the halted receipt reads "Cause watch_button; calendar_query span (line 31) was in flight; dropped 1: book. Worker dead 80 ms after the stop." Nothing new was sent or booked, and a second `demo` refused: "Not started: Doris is halted".

## Saturday afternoon rehearsal (2:00 to 6:00 PM)

It ends before the overnight run starts at 8:00 PM. Nothing is run twice that is already proven.

| Time | What | Pass when |
|---|---|---|
| 2:00 | The setup above. | Every window is clean, and nothing on the never-on-screen list shows. |
| 2:30 | **Live, dry run.** Rae sends both emails at minute 8; Doris's next pass handles them. | The Gmail mail reaches Doris through the forward (time it; this path has never run live, because the smoke used the Fastmail test sender). She books, and her reply lands in Rae's **Inbox**, not Spam or Promotions (if Spam: mark "Not spam" and add Doris's address to Rae's contacts). The .ics opens. The event appears in Fastmail without a refresh. The hold notice reaches whizz@. Note every place the meeting link shows. |
| 3:15 | **Live, recorded take.** The same, with OBS on. | Kept as the Saturday fallback, labelled with its date. |
| 3:45 | **A judge's day on Windows.** A fresh clone, `cd doris\src`, `py -m doris demo`. | It runs end to end. Note its first line and how the ending reads on camera. |
| 4:00 | **Stop beat**, on the server with the key (the halted receipt on camera, as the tracker plans). Three tries. | Each Stop lands while a calendar call shows "no answer yet", and the halted line names it. |
| 4:30 | **Read-through.** Shane reads the script aloud with a timer over the clips. | Under 2:58, after the cut order in the script if needed. |
| 5:15 | **Stumbles.** Each one goes in the near-miss log with a fix and an owner. One status line to the project. | |
| 5:45 | Clean up: sandbox back to clean, `resume`, the live run left as the build thread wants it for the overnight run. | |

The Trap Pack is not run in rehearsal; its output layout is already known from the noon run.

## Sunday evening filming (4:30 to 8:30 PM)

The feature freeze comes first. Everything is filmed on the frozen commit.

| Time | What |
|---|---|
| 4:30 | Server on the freeze commit; live smoke; `run --live` started **before 4:50 PM**, so the 5:00 PM digest goes out before the takes (her first pass after 5:00 sends the day's digest). Archive the whizz@ inbox after it lands. |
| 5:08 | **Take 1, live.** Rae sends at 5:08:00 and 5:08:20; the pass is at 5:10. Record until the event shows on the calendar. Then, hands on, the hold: the notice in Fastmail and Rae's empty "Pilot question" thread. |
| 5:18 | Take 2 if needed, for the 5:20 pass. A third at 5:28. |
| 5:45 | Stop the live run unless the build thread wants it running; check its receipts verify; `resume`. |
| 6:00 | A judge's day on Windows: fresh clone, `py -m doris demo`. |
| 6:15 | The Trap Pack on the same Windows clone, no key: `py -m doris traps` replays Claude's own answers from our graded Trap Pack run, and its first line names that run. |
| 6:30 | The Stop beat, two or three takes. |
| 6:45 | `verify` and the closing cards. |
| 7:00 | Voice: Shane reads each beat over the cut, two takes per beat, in a quiet room. |
| 7:30 | Edit (Clipchamp is built into Windows 11), with the rules below. Watch it once at full speed. |
| 8:15 | Upload, unlisted, to Shane's own or the Hire the Machines YouTube channel. Check it plays in a signed-out window. Send the link to Wolf for the README. Shane submits the form Sunday night. |

**Rae's times, one per take.** Each take books a real event, so never reuse a time. Never delete one of her events by hand either: a booking gone from the calendar sends Shane a hold notice (ruling 17).

| Take | Rae asks for | Free on whizz@ first? |
|---|---|---|
| Sat dry run | Thu Dec 3, 9:00 AM Pacific | free at 12:27 PM Sat; check again |
| Sat recorded | Thu Dec 3, 12:00 PM Pacific | free at 12:27 PM Sat; check again |
| Sun take 1 | Thu Dec 3, 2:00 PM Pacific | free at 12:27 PM Sat; check again |
| Sun take 2 | Thu Dec 3, 3:00 PM Pacific | free at 12:27 PM Sat; check again |
| Sun take 3 | Fri Dec 4, 10:00 AM Pacific | free at 12:27 PM Sat; check again |

Saturday's two times were Thu Dec 3 at 10:00 and 11:00 AM. Both are busy on whizz@ (Thursday is busy 10:00 AM to 12:00 PM and 1:00 to 2:00 PM Pacific; Friday 9:00 to 10:00 AM and 1:00 to 5:00 PM), so they moved. A time that is taken does not book: Rae gets an offer of the nearest free times (that day's slot nearest to it, then the same time on the nearest other days), and the take no longer tells the booking story.

The script is written for Sunday take 1 (Thursday at two). On any other take, Shane says the time Rae actually asked for.

Rae's emails, word for word (they stay clear of the money and add-a-person words, so only the second one holds):

- Subject "Quick call Thursday?": "Hi, could Shane do [the take's day and time] Pacific for 30 minutes? I'd like to talk about our scheduling. Rae"
- Subject "Pilot question": "What would Shane charge for a three-month pilot, and what's included?"

Never type "stop" or any opt-out phrase as Rae: it silences her for good.

## The edit

- **The live take is one recording**, from Rae's Send to the event on the calendar. Speed-ups only on the wait, each labelled "sped up, nothing cut", with the clock visible. No cuts inside it.
- **Labels** as the script words them: `LIVE`, `SANDBOX`, `STAGED`.
- **Cover the meeting link** with a `[meeting link]` box wherever it shows: Rae's reply, the .ics preview and the calendar event.
- Nothing typed in the edit except labels, the title, the "Human approvals: 0" card and the credits.
- Credit card, exactly: "Shane Bornstein and Wolf (Claude), his CTO · Hire the Machines", with "Anthropic is not involved in this entry." under it.
- Music: none, or a quiet bed from the YouTube Audio Library with no lyrics.
- Under 3:00 on the final export. Check the length on YouTube after upload.

## Stumble log

Every stumble from rehearsal and filming goes in the near-miss log the moment it happens: time, what, fix, trace link. Video-only stumbles also go here:

| Time | Beat | What happened | Fix | Owner |
|---|---|---|---|---|
| | | | | |
