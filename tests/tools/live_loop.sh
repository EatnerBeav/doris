#!/usr/bin/env bash
# Copied out of this branch to /root/doris_live_loop_on_wide.sh and run from there (it checks out main in /root/doris).
# Doris on wide, Sat Nov 28 2026: the full live loop (Shane's 6:10 AM item 2). One script, three steps:
#   bash /root/doris_live_loop_on_wide.sh start    checkout main, resume, `run --live` in tmux doris-live,
#                                                  wolf's mix (tests/tools/live_mix.py) in tmux doris-mix
#   bash /root/doris_live_loop_on_wide.sh status   what she and the mix have done so far
#   bash /root/doris_live_loop_on_wide.sh finish [force]
#        stop her (`python3 -m doris stop`), let `run --live` write traces/live-run-<stamp>/ itself, verify
#        it, commit it with the mix's log (traces/live-loop-<stamp>/) from a NEW clone on the thread's
#        branch, push. `force` ends a mix still running first (SIGTERM: it writes its tally).
# Leaves HALT in force afterwards (the overnight go runs `resume`). Prints no setting's value.
set -u
DORIS=/root/doris
BRANCH=claude/project-thread-gj55xi
LOOP=/root/doris-live-loop
WORK=/root/doris-live-loop-trace
say() { printf '%s %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }
die() { say "STOPPED: $*"; exit 1; }

mixsum() {
  [ -f "$LOOP/mix-log.jsonl" ] || { say "no mix log yet"; return; }
  python3 -I - "$LOOP/mix-log.jsonl" <<'PY'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1], encoding="utf-8") if l.strip()]
sent = [r for r in rows if r["event"] == "sent"]
ans = [r for r in rows if r["event"] == "answer"]
ended = [r for r in rows if r["event"] == "thread_ended"]
print("mix: %d emails sent, %d answers of hers, %d threads ended" % (len(sent), len(ans), len(ended)))
for r in rows:
    if r["event"] in ("sent", "answer", "thread_ended", "halted", "terminated", "send_failed", "look_failed", "cap"):
        print("  %s %-12s %s %s %s" % (r.get("t", ""), r["event"], r.get("thread") or "-",
              "step %s expects %s" % (r.get("step"), r.get("expect")) if r["event"] == "sent" else
              "%s %s" % (r.get("kind", ""), r.get("template", "")) if r["event"] == "answer" else "",
              r.get("why", "") or r.get("cause", "")))
    if r["event"] == "tally":
        print("TALLY", {k: v for k, v in r.items() if k != "rows"})
PY
}

start() {
  [ -e "$LOOP" ] && die "$LOOP already exists (start ran before): use status or finish"
  pgrep -af "doris (run|_worker)" && die "a doris run or worker is alive"
  for s in doris-live doris-mix doris-night; do tmux has-session -t "$s" 2>/dev/null && die "tmux $s exists"; done
  cd "$DORIS" || die "no $DORIS"
  [ -z "$(git status --porcelain --untracked-files=no)" ] || die "tracked files changed in $DORIS"
  [ -f state/live/ledger.sqlite ] || die "no live ledger in $DORIS/state/live (never --fresh)"
  say "checkout before: $(git rev-parse --short HEAD)"
  git fetch -q origin main || die "fetch main"
  mkdir -p "$LOOP/aside"
  # Trace folders a live run wrote here and that main now holds (the soak's) would block the checkout:
  # moved aside, never deleted, each said identical to main's copy or not.
  for d in $(git ls-files --others --directory -- traces/); do
    d=${d%/}
    git cat-file -e "origin/main:$d" 2>/dev/null || continue
    same=yes
    for f in $(cd "$d" && find . -type f | sed 's:^\./::'); do
      git show "origin/main:$d/$f" 2>/dev/null | cmp -s - "$d/$f" || same=no
    done
    mv "$d" "$LOOP/aside/" && say "moved untracked $d aside to $LOOP/aside/ (identical to main's: $same)"
  done
  git checkout -q --detach origin/main || die "checkout main"
  say "checkout now: $(git log --oneline -1)"
  git fetch -q origin "$BRANCH" || die "fetch $BRANCH"
  git show FETCH_HEAD:tests/tools/live_mix.py > tests/tools/live_mix.py || die "live_mix.py from $BRANCH"
  say "tests/tools/live_mix.py from $BRANCH $(git rev-parse --short FETCH_HEAD) (untracked; finish removes it)"
  python3 -I -c 'import json,sys; r=json.load(open(sys.argv[1])); print("smoke record: ts %s, sent copy found %s" % (r.get("ts"), r.get("sent_copy_found")))' \
    state/live/smoke-sent.json || die "no smoke record (run --live would refuse)"
  python3 tests/tools/live_mix.py --rehearse >"$LOOP/rehearse.out" 2>&1; rc=$?
  cat "$LOOP/rehearse.out"; [ "$rc" = 0 ] || die "the mix's sandbox rehearsal failed"
  (cd src && python3 -m doris resume); say "resume exit $?"
  tmux new -d -s doris-live "cd $DORIS/src && exec python3 -m doris run --live >>$LOOP/run.out 2>&1"
  sleep 30
  pgrep -af "doris run --live" >/dev/null || { tail -n 20 "$LOOP/run.out"; die "run --live did not keep running"; }
  say "run --live is up: $(pgrep -af 'doris (run|_worker)' | wc -l) process(es)"
  tail -n 15 "$LOOP/run.out"
  tmux new -d -s doris-mix "cd $DORIS && exec python3 tests/tools/live_mix.py --live --log $LOOP/mix-log.jsonl >>$LOOP/mix.out 2>&1"
  sleep 20
  cat "$LOOP/mix.out"; mixsum
  say "STARTED"
}

status() {
  say "processes: $(pgrep -af 'doris (run|_worker)' | wc -l) doris, $(pgrep -af 'live_mix.py --live' | wc -l) mix; tmux: $(tmux ls 2>/dev/null | cut -d: -f1 | tr '\n' ' ')"
  say "her output (last 40 lines):"; tail -n 40 "$LOOP/run.out"
  say "mix output:"; tail -n 30 "$LOOP/mix.out"; mixsum
}

finish() {
  cd "$DORIS" || die "no $DORIS"
  if pgrep -f "live_mix.py --live" >/dev/null; then
    [ "${1:-}" = force ] || die "the mix is still running (status shows it); finish force ends it first"
    pkill -TERM -f "live_mix.py --live"; sleep 5
  fi
  say "mix output:"; cat "$LOOP/mix.out"; mixsum
  found() { ls -d "$DORIS"/traces/live-run-* 2>/dev/null; }
  [ -z "$(found)" ] || die "a traces/live-run-* folder already exists in $DORIS: $(found | tr '\n' ' ')"
  RUN_PIDS=$(pgrep -f "doris (run|_worker)" | tr '\n' ' ')
  say "python3 -m doris stop (her processes: $RUN_PIDS)"
  ( cd src && python3 -m doris stop ); say "stop exit $?"
  for i in $(seq 1 240); do
    T=$(found | tail -1)
    if [ -n "$T" ] && [ -f "$T/terminal.log" ] && ! pgrep -f "doris (run|_worker)" >/dev/null; then break; fi
    sleep 1
  done
  pgrep -af "doris (run|_worker)" && say "WARNING: a doris process is still alive after the stop"
  [ "$(found | wc -l)" -eq 1 ] || die "expected exactly one traces/live-run-* folder, found: $(found | tr '\n' ' ')"
  T=$(found); NAME=$(basename "$T"); STAMP=${NAME#live-run-}
  say "trace folder: traces/$NAME"; ls -la "$T"
  say "her output (last 30 lines):"; tail -n 30 "$LOOP/run.out"
  say "timeline.txt (last 60 lines):"; tail -n 60 "$T/timeline.txt"
  ( cd src && python3 -m doris verify "$T/receipts.jsonl" ); say "verify exit $?"
  rm -f tests/tools/live_mix.py
  [ -z "$(git status --porcelain)" ] || say "NOTE: $DORIS still shows changes: $(git status --porcelain | tr '\n' ' ')"
  [ -e "$WORK" ] && die "$WORK already exists; not reusing it (trace left in $T)"
  git clone -q "$(git remote get-url origin)" "$WORK" || die "clone failed"
  cd "$WORK" || die "cd $WORK"
  git checkout -q -b "$BRANCH" "origin/$BRANCH" || die "branch $BRANCH"
  say "clone at $BRANCH $(git rev-parse --short HEAD)"
  git config core.hooksPath .githooks
  git config user.name EatnerBeav
  git config user.email 45749433+EatnerBeav@users.noreply.github.com
  cp -rp "$T" "traces/$NAME" || die "copy failed"
  mkdir -p "traces/live-loop-$STAMP"
  for f in mix-log.jsonl mix.out rehearse.out; do [ -f "$LOOP/$f" ] && cp -p "$LOOP/$f" "traces/live-loop-$STAMP/$f"; done
  ( cd src && python3 -m doris verify "../traces/$NAME/receipts.jsonl" ); say "verify in the clone exit $?"
  SUMMARY=$(python3 -I -c '
import json, sys
m = json.load(open(sys.argv[1]))
c = m.get("counts", {})
print("outcome %s, first failing step %s, exit %s; %s to %s UTC; commit %s; model %s; cost $%s; "
      "messages %s, handled %s, held %s, sends %s, model calls %s, errors %s, human approvals %s; chain ok=%s (%s lines)" % (
      m.get("outcome"), m.get("first_failing_step"), m.get("exit_code"), m.get("started_utc"), m.get("ended_utc"),
      (m.get("commit") or "")[:7], m.get("model"), m.get("cost_usd"), c.get("messages"), c.get("handled"),
      c.get("held"), c.get("sends"), c.get("model_calls"), c.get("errors"), c.get("human_approvals"),
      m.get("receipt_chain", {}).get("ok"), m.get("receipt_chain", {}).get("lines")))
' "traces/$NAME/manifest.json") || die "manifest unreadable"
  say "SUMMARY $SUMMARY"
  TALLY=$(grep '"event": "tally"' "traces/live-loop-$STAMP/mix-log.jsonl" | tail -1 | python3 -I -c '
import json, sys
t = json.loads(sys.stdin.read() or "{}")
print("mix: %s emails from wolf; met %s, safe %s, wrong %s, unsent %s; answered twice %s; stopped %s" % (
      t.get("emails"), t.get("met"), t.get("safe"), t.get("wrong"), t.get("unsent"), t.get("answered_twice"), t.get("stopped")))')
  say "TALLY $TALLY"
  git add -- "traces/$NAME" "traces/live-loop-$STAMP"
  git status --short
  git diff --cached --quiet && die "nothing to commit"
  cat > /root/doris-live-loop-msg.txt <<MSG
Live loop trace from wide: traces/$NAME and traces/live-loop-$STAMP (pass or fail, committed as kept)

The full live loop, Sat Nov 28: python3 -m doris run --live on main in tmux doris-live, wolf's mix
(tests/tools/live_mix.py) and Rae's two emails from her Gmail, stopped with python3 -m doris stop.
run --live wrote traces/$NAME itself.
$SUMMARY
$TALLY

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QnTDpJ5LcxqN9YXsCJcdgS
MSG
  git commit -q -F /root/doris-live-loop-msg.txt || die "commit blocked (secret scan?); nothing pushed. Staged files left in $WORK"
  say "committed $(git rev-parse HEAD)"
  ok=""
  for i in 1 2 3 4; do
    if git push -q -u origin "$BRANCH"; then ok=1; break; fi
    sleep $((2 ** i))
  done
  [ -n "$ok" ] || die "push failed 4 times; commit kept in $WORK"
  say "pushed to origin/$BRANCH"
  # main will hold this trace once the PR merges: keep /root/doris's copy aside so the overnight's
  # checkout of main is not blocked by it.
  mkdir -p "$LOOP/aside" && mv "$T" "$LOOP/aside/" && say "moved $DORIS/traces/$NAME aside to $LOOP/aside/"
  say "untracked in $DORIS now: $(git -C "$DORIS" ls-files --others --exclude-standard | tr '\n' ' ')"
  say "halt files now: $(ls "$DORIS"/state/live/ "$DORIS"/state/sandbox/ 2>/dev/null | grep -i halt | tr '\n' ' ')"
  say "ALL DONE"
}

case "${1:-}" in
  start) start ;;
  status) status ;;
  finish) shift; finish "${1:-}" ;;
  *) echo "usage: $0 start | status | finish [force]"; exit 2 ;;
esac
