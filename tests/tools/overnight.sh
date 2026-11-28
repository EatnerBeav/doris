#!/usr/bin/env bash
# The overnight run on our server: her live run under the kill switch, the test sender's drip
# (tests/tools/overnight_drip.py), a forced restart 7 hours in (kill -9 of her run and its worker,
# then `run --live` again on the same ledger), `stop` 10 hours in, then every trace kept
# (tests/tools/keep_overnight.py) and committed on a branch of its own and pushed, pass or fail.
#
#   cd /root/doris/src && python3 -m doris resume      # a person clears the last halt, by hand
#   cd /root/doris && tmux new -d -s doris-night 'bash tests/tools/overnight.sh'
#
# It never resumes a halt: a stop during the night (the CLI, the watch page, a STOP file) ends it,
# and a run that will not start for a halt is said and kept as it is. One Doris per ledger: it
# refuses to start while any `doris run` or worker is alive. It changes nothing in the checkout
# but the trace files it adds, and the branch it commits them on.
#
# Knobs (environment): RESTART_AFTER_S (25200), STOP_AFTER_S (36000), NIGHT_DIR (/root/doris-night-
# <stamp>), BRANCH (traces/overnight-<stamp>), COMMIT (1; 0 keeps the traces uncommitted).
# OVERNIGHT_MODE=sandbox is the dry run of this script anywhere: `run` on the sandbox instead of
# --live, no drip, traces under NIGHT_DIR, nothing committed.
set -u
REPO=$(cd "$(dirname "$0")/../.." && pwd)
SRC=$REPO/src
MODE=${OVERNIGHT_MODE:-live}
RESTART_AFTER_S=${RESTART_AFTER_S:-25200}
STOP_AFTER_S=${STOP_AFTER_S:-36000}
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
NIGHT=${NIGHT_DIR:-/root/doris-night-$STAMP}
BRANCH=${BRANCH:-traces/overnight-$STAMP}
COMMIT=${COMMIT:-1}
PY=${PYTHON:-python3}
LOG=$NIGHT/night.log
utc() { date -u +%Y-%m-%dT%H:%M:%SZ; }
say() { printf '%s %s\n' "$(utc)" "$*" | tee -a "$LOG"; }
stop_here() { say "NOT STARTED: $*"; exit 1; }

[ -e "$NIGHT" ] && { echo "$NIGHT already exists; not reusing it"; exit 1; }
mkdir -p "$NIGHT" || exit 1
if [ "$MODE" = live ]; then
  RUNARGS="--live"; STATE=$REPO/state/live; TRACES=$REPO/traces; STATUS=$REPO/status; REH=""
else
  RUNARGS=""; STATE=$REPO/state/sandbox; TRACES=$NIGHT/traces; STATUS=$NIGHT/status; REH="--rehearsal"; COMMIT=0
fi
HEAD_SHA=$(git -C "$REPO" rev-parse HEAD)
say "overnight ($MODE) from $REPO at $HEAD_SHA; restart after ${RESTART_AFTER_S}s, stop after ${STOP_AFTER_S}s; night folder $NIGHT"

# --- before anything starts -------------------------------------------------------------------------
pgrep -f "doris (run|_worker)" >/dev/null && stop_here "a doris run or worker is alive: $(pgrep -af 'doris (run|_worker)' | tr '\n' ';')"
[ "$MODE" != live ] || [ -f "$STATE/ledger.sqlite" ] || stop_here "no live ledger at $STATE (never --fresh here)"
[ -z "$(git -C "$REPO" status --porcelain --untracked-files=no)" ] || stop_here "the checkout has changes to tracked files"
"$PY" "$REPO/tests/tools/overnight_drip.py" --rehearse >>"$LOG" 2>&1 || stop_here "the drip's sandbox rehearsal failed (see $LOG)"
say "drip rehearsal passed"

newest_run() { ls -1td "$STATE"/runs/*/ 2>/dev/null | head -1 | sed 's:/*$::'; }
start_run() {   # $1: label. Its own session, so one kill reaches the run and its worker together.
  local before after
  before=$(newest_run)
  (cd "$SRC" && exec setsid "$PY" -m doris run $RUNARGS >>"$NIGHT/$1.out" 2>&1 </dev/null) &
  RUN_PID=$!
  sleep 20
  if kill -0 "$RUN_PID" 2>/dev/null && [ "$(ps -o pgid= -p "$RUN_PID" | tr -d ' ')" != "$RUN_PID" ]; then
    say "$1 is not the leader of its own process group, so the forced restart could miss its worker"
    kill -INT "$RUN_PID"; wait "$RUN_PID"; stop_here "$1 stopped with Ctrl-C; start the script from a non-interactive shell"
  fi
  after=$(newest_run)
  if ! kill -0 "$RUN_PID" 2>/dev/null; then
    wait "$RUN_PID"; RUN_EXIT=$?
    say "$1 did not keep running: exit $RUN_EXIT (5 = a halt is in force; this script never resumes one). $(tail -2 "$NIGHT/$1.out" | tr '\n' ' ')"
    RUN_PID=""
  fi
  if [ -n "$after" ] && [ "$after" != "$before" ]; then RUN_DIR=$after; else RUN_DIR=""; fi
  say "$1: pid ${RUN_PID:-none}, run folder ${RUN_DIR:-none}"
}
until_t() { while [ "$(date +%s)" -lt "$1" ]; do sleep 20; done; }
alive() { [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null; }

# --- the night -------------------------------------------------------------------------------------
T0=$(date +%s); STARTED=$(utc)
RUN_EXIT=""; start_run run-1; RUN1_PID=$RUN_PID; RUN1_DIR=$RUN_DIR
[ -n "$RUN1_PID" ] || stop_here "run 1 did not start"
DRIP_PID=""
if [ "$MODE" = live ]; then
  (cd "$REPO" && exec "$PY" tests/tools/overnight_drip.py --live --log "$NIGHT/drip-log.jsonl" >>"$NIGHT/drip.out" 2>&1 </dev/null) &
  DRIP_PID=$!
  say "drip: pid $DRIP_PID"
fi

until_t $((T0 + RESTART_AFTER_S))
KILLED_AT=""; RUN2_PID=""; RUN2_DIR=""
if alive "$RUN1_PID"; then
  KILLED_AT=$(utc)
  kill -KILL -- "-$RUN1_PID"
  say "forced restart: kill -9 of run 1's process group $RUN1_PID at $KILLED_AT"
  wait "$RUN1_PID" 2>/dev/null
  sleep 5
  pgrep -g "$RUN1_PID" >/dev/null && say "WARNING: a process of run 1's group is still alive: $(pgrep -ag "$RUN1_PID" | tr '\n' ';')"
  RUN2_STARTED=$(utc); RUN_EXIT=""; start_run run-2; RUN2_PID=$RUN_PID; RUN2_DIR=$RUN_DIR; RUN2_EXIT=$RUN_EXIT
else
  wait "$RUN1_PID" 2>/dev/null; RUN1_EXIT=$?
  say "no restart: run 1 had already ended (exit $RUN1_EXIT); a stop during the night ends it"
fi

until_t $((T0 + STOP_AFTER_S))
if alive "$RUN2_PID"; then
  say "morning: $(cd "$SRC" && "$PY" -m doris stop 2>&1 | tr '\n' ' ')"
  for _ in $(seq 1 60); do alive "$RUN2_PID" || break; sleep 5; done
  alive "$RUN2_PID" && say "WARNING: run 2 is still alive 5 minutes after the stop"
  wait "$RUN2_PID" 2>/dev/null; RUN2_EXIT=$?
  say "run 2 ended: exit $RUN2_EXIT"
fi
if alive "$DRIP_PID"; then
  for _ in $(seq 1 36); do alive "$DRIP_PID" || break; sleep 5; done
  alive "$DRIP_PID" && { kill -TERM "$DRIP_PID"; say "drip still running 3 minutes after the stop: sent SIGTERM"; }
fi
[ -n "$DRIP_PID" ] && say "drip: $(tail -1 "$NIGHT/drip.out" 2>/dev/null)"

# --- keep it all, pass or fail ---------------------------------------------------------------------
if [ -n "$KILLED_AT" ]; then
  KEEP_ARGS=(--run1 "$RUN1_DIR" --killed-at "$KILLED_AT" --run2 "$RUN2_DIR" --run2-started "$RUN2_STARTED")
  [ -n "${RUN2_EXIT:-}" ] && KEEP_ARGS+=(--run2-exit "$RUN2_EXIT")
else
  KEEP_ARGS=(--run1 "$RUN1_DIR")
  [ -n "${RUN1_EXIT:-}" ] && KEEP_ARGS+=(--run2-exit "$RUN1_EXIT")
fi
"$PY" "$REPO/tests/tools/keep_overnight.py" --night "$NIGHT" --started "$STARTED" --traces-root "$TRACES" \
  --status-dir "$STATUS" $REH "${KEEP_ARGS[@]}" >"$NIGHT/keep.out" 2>&1
KEEP_EXIT=$?
cat "$NIGHT/keep.out" >>"$LOG"
say "kept (exit $KEEP_EXIT): $(grep '^OVERNIGHT' "$NIGHT/keep.out")"
[ "$COMMIT" = 1 ] || { say "not committed (COMMIT=$COMMIT)"; exit 0; }

cd "$REPO" || exit 1
mapfile -t PATHS < <(sed -n 's/^KEEP //p' "$NIGHT/keep.out")
[ "${#PATHS[@]}" -gt 0 ] || { say "nothing to commit"; exit 1; }
git checkout -q -b "$BRANCH" || { say "branch $BRANCH could not be made; traces stay uncommitted"; exit 1; }
git add -- "${PATHS[@]}"
git commit -q -m "Overnight run $STAMP: $(grep '^OVERNIGHT' "$NIGHT/keep.out" | cut -d' ' -f3- | cut -c1-150)" \
  -m "Traces kept by tests/tools/keep_overnight.py from the night on our server, pass or fail." \
  || { say "commit refused (the secret scan?); traces stay uncommitted on $BRANCH"; exit 1; }
for wait_s in 0 2 4 8 16; do
  sleep "$wait_s"
  git push -q -u origin "$BRANCH" && { say "pushed $BRANCH at $(git rev-parse --short HEAD)"; break; }
done
git checkout -q --detach "$HEAD_SHA"
say "the checkout is back at $HEAD_SHA; the night is over"
