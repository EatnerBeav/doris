# The organizers' Trap Pack

`pack/` holds the 20 input files of the **Agentware 2026 Trap Pack, Hackathon Raptors (CC BY 4.0)**,
https://creativecommons.org/licenses/by/4.0/, copied byte for byte from the `traps/` folder of the
organizers' zip (24,820 bytes, sha256 `4da3e72c814b42d6537c049775c1110d4caaff4c9090fe65ea2224ad86452c0b`,
fetched Nov 27, 2026), keeping its track subfolders. The pack's `EXPECTED.md` and `manifest.json` hold
the answers and are not in this repo's inputs: we read them only after our first honest run.

Every address, domain, company and person in the pack is fictional. Its keys and tokens are fake test
inputs, kept here unchanged. No code or unit test holds one of the pack's canary tokens: the runner
finds them by their pattern. The reports name them (`RESULTS.md`, `RUNS.md`), because a canary scan
has to say which token it looked for and where.

## Running it

From `src/`:

    python3 -m doris traps                 # the pack in tests/trap-pack/pack/: see below for the model
    python3 -m doris traps PATH            # another copy of the organizers' folder (never written to)
    python3 -m doris traps --model live    # Claude reads each file: ANTHROPIC_API_KEY in .env or set, about $0.065
    python3 -m doris traps --model stub    # the keyword stand-in
    python3 -m doris traps --model replayed --answers FILE
    python3 -m doris traps --record        # ours, for a committed run: RESULTS.md here and traces/

Each file runs through the real Doris loop on its own fresh throwaway state under
`state/throwaway/<source>-<UTC stamp>/<NN>/` (NN its place in the pack: short, so every path a run
writes fits Windows' default path limit from a clone folder of up to about 115 characters), with
the network guard allowing no host (only the model's API host when the model is live). A web page becomes one
text/html part, a document or transcript one text/plain part, the file's bytes unchanged; an `.eml`
goes in as is. The first line of the report prints the whole setup.

With no `--model`, it chooses as `demo` does. With `ANTHROPIC_API_KEY` in `.env` at the repo root or in the environment, Claude
reads each file live. Without one, it replays `answers.jsonl`: Claude's own answers from our latest
committed live run (run 3), one per model call, matched by the request's sha256, with no model
called and nothing sent anywhere. That run's report is what you get, row for row. With neither, it
runs the keyword stub. A replayed miss is a no-model hold, which fails its row; a
`traps --model live --record` that passes rewrites `answers.jsonl`.

## What it writes

- `RESULTS.md`: one row per file (what she did, from her receipts; the outcome in the rubric's
  words; the verdict, pass or failed against invariants I1 to I9 and the canary scan; "Against the
  key", which reads "not graded" because the run never opens the pack's answer key), the verify
  results, the canary scan, the known limits and this credit.
- `model-<source>-throwaway-traps-<UTC stamp>/<item>/`: for each item her receipts
  (`receipts.jsonl`, the hash chain), the wrapped `.eml`, `timeline.txt` (rendered from the
  receipts: one line per step), `terminal.log`, `verify.txt` (the chain check) and, when the model
  was called live, `recorded-answers.jsonl` (its raw answers); plus `pack-manifest.json` with every
  file's sha256.

By default both go under `state/throwaway/traces/model-<source>-throwaway-traps-<UTC stamp>/`
(RESULTS.md beside the item folders), which git ignores: your run leaves our committed report and
traces as they are and the tree clean, and its last line says where it wrote. Only `--record`, which
we use for a committed run, writes `RESULTS.md` here and the traces under `traces/` at the repo root,
where the run's folder keeps its own copy of the report.

## How we grade it

Pass means none of her invariants broke, not that she did what the pack's key asks. After each
committed run we read the pack's `EXPECTED.md` and grade every row by hand: met, safe and silent,
missed or bug. [RESULTS.md](RESULTS.md) is the latest run (run 3: met 13, safe and silent 7, missed
0, bug 0); [RUNS.md](RUNS.md) has every run, each grade's reason and what we fixed between runs. If
you have the organizers' zip, run `python3 -m doris traps PATH` on its `traps/` folder and grade
your own report against its `EXPECTED.md` the same way.
