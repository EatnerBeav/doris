# The sandbox

## Try it on your own email

**With no key, she holds your email unread.** The demo's model answers are recorded, and your email has none, so with no key it is held: nothing is sent to you, and her notice to the owner says the model wasn't available. To have the model read it, set `ANTHROPIC_API_KEY` in your shell before the demo (about $0.004 an email; how to get a key is in the README, [With a key](../README.md#with-a-key-keys-tier-and-cost)). To have the built-in keyword stand-in read the whole day instead, yours included, move `answers.jsonl` out of this folder; the stand-in is crude and labelled stub everywhere.

1. Copy [`your-email.eml`](your-email.eml) into `inbox/` with a name that sorts after the demo's, such as `14-yourname.eml`.
2. Edit it in any text editor: your name and address in From, your Subject and your words. Give each email you add its own Message-ID (change `your-email-1` to `your-email-2`, and so on): a Message-ID she has seen is a repeat, and she skips it.
3. Leave To as it is, the owner `whizz@fastmail.com` alone with nobody in Cc: that is how a request forwarded from the owner's mailbox reaches her. Keep the Date on Tue, 01 Dec 2026, between the demo's last email (3:06 PM Pacific) and the 5:00 PM digest: her sandbox day follows the dates of the mail, so a Date on another day moves her clock there with it.
4. From `src/`, run `python3 -m doris demo` again. It starts the day fresh (the last run is kept in `state/old/`), and your email arrives with the demo's. Her answer, if she sends one, lands in `outbox/`.

The template as it ships asks for Thursday, December 10 at 2:00 PM Pacific, a time that is available: read by the stand-in, she books it and sends you a confirmation with an `.ics`. Any mail app can also save a real message as an `.eml` file; give it the same To and Date.

## What is here

`inbox/` is the demo day: 15 emails, and `facts.json`, which stages the day's faults (the calendar timing out once, another event landing mid-booking) and notes which emails came through the owner's forwarding rule; an email you add needs no line there. `calendar/` is the owner's calendar, his busy blocks as `.ics` files. `answers.jsonl` holds the model's answers recorded from a keyed run of this day, which the demo replays when there is no key. `clock.txt` is where her day starts.

`outbox/` is her Sent folder, empty in a fresh clone. After a run it holds one .eml for every message she sent, named `<n>-<template>.eml`: each reply goes from her address to the sender, with a blind copy to the owner that is recorded in `outbox/envelope.jsonl` and never shown in a header (a booking or a cancel carries its .ics file), and her hold notices and the digest go to the owner. Any mail app opens them. `outbox/keywords.json` records how she filed each email she read, because she never moves or deletes mail, and her bookings appear in `calendar/` as `doris-*.ics` beside the owner's busy blocks.
