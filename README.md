# Doris

An inbox and calendar agent you can walk away from. Agentware 2026, track T1 (Inbox & Calendar).

Team Whizz: Shane Bornstein and Wolf (Claude), his CTO. Anthropic is not involved in the entry.

> Built during the Agentware 2026 window (Fri Nov 27 to Mon Nov 30, 2026). This README fills in as the build lands; the trace is the source of truth.

## Run it (no install, no key)

Python 3.9 or newer, standard library only.

```
git clone https://github.com/EatnerBeav/doris
cd doris/src
python3 -m doris demo
```

On Windows the last line is `py -m doris demo`.

## Working on the repo

Every commit runs the secret scan. Turn the hook on once per clone:

```
git config core.hooksPath .githooks
```

Run the unit tests from the repo root with `python3 -m unittest discover -s tests -t .`

## Credits

The Agentware 2026 Trap Pack used in `tests/` is the "Agentware 2026 Trap Pack, Hackathon Raptors", CC BY 4.0.

MIT licence. See `LICENSE`.
