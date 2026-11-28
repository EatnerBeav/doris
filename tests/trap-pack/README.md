# The organizers' Trap Pack

`pack/` holds the 20 input files of the **Agentware 2026 Trap Pack, Hackathon Raptors (CC BY 4.0)**,
https://creativecommons.org/licenses/by/4.0/, copied byte for byte from the `traps/` folder of the
organizers' zip (24,820 bytes, sha256 `4da3e72c814b42d6537c049775c1110d4caaff4c9090fe65ea2224ad86452c0b`,
fetched Nov 27, 2026), keeping its track subfolders. The pack's `EXPECTED.md` and `manifest.json` hold
the answers and are not in this repo's inputs: we read them only after our first honest run.

Every address, domain, company and person in the pack is fictional. Its keys and tokens are fake test
inputs: they are kept here unchanged and never copied into code or docs.

## Running it

From `src/`:

    python3 -m doris traps                 # the pack in tests/trap-pack/pack/, model stub
    python3 -m doris traps PATH            # another copy of the organizers' folder (never written to)
    python3 -m doris traps --model replayed --answers FILE
    python3 -m doris traps --record        # ours, for a committed run: RESULTS.md here and traces/

Each file runs through the real Doris loop on its own fresh throwaway state under
`state/throwaway/<source>-<UTC stamp>/<NN>/` (NN its place in the pack: short, so every path a run
writes fits Windows' default path limit from a clone folder of up to about 115 characters), with
the network guard allowing no host. A web page becomes one
text/html part, a document or transcript one text/plain part, the file's bytes unchanged; an `.eml`
goes in as is. The first line of the report prints the whole setup.

## What it writes

- `RESULTS.md`: one row per file (what she did, from her receipts; the outcome in the rubric's
  words; pass or failed against invariants I1 to I9), the verify results, the canary scan, the known
  limits and this credit.
- `model-<source>-throwaway-traps-<UTC stamp>/<item>/`: her receipts, the wrapped `.eml`, the model
  answers, the rendered timeline and the terminal log for each item, plus `pack-manifest.json` with
  every file's sha256.

By default both go under `state/throwaway/traces/model-<source>-throwaway-traps-<UTC stamp>/`
(RESULTS.md beside the item folders), which git ignores: your run leaves our committed report and
traces as they are and the tree clean, and its last line says where it wrote. Only `--record`, which
we use for a committed run, writes `RESULTS.md` here and the traces under `traces/` at the repo root.
