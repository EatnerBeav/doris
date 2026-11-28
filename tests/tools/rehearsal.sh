#!/usr/bin/env bash
# Copied out of this branch to /root/doris_rehearsal_on_wide.sh and run from there (it checks out main in /root/doris).
# Doris on wide, Sat Nov 28 2026: the 2:30 PM rehearsal's live dry run (docs/video/filming-plan.md, PR 20;
# decision 115). Rae's two emails are sent by hand from her Gmail in a browser; nothing here sends mail.
#   bash /root/doris_rehearsal_on_wide.sh prep     look only, plus one copy: the live ledger kept aside before
#                                                  the schema 3 to 4 upgrade; her bookings and Rae's Thursday
#                                                  read from the calendar (times and status only)
#   bash /root/doris_rehearsal_on_wide.sh drymigrate   main's code opens a copy of that ledger in a scratch folder
#   bash /root/doris_rehearsal_on_wide.sh start    checkout main, resume, `run --live` in tmux doris-live,
#                                                  `watch` in tmux doris-watch (127.0.0.1:8765 on wide)
#   bash /root/doris_rehearsal_on_wide.sh status   what she has done so far
#   bash /root/doris_rehearsal_on_wide.sh finish staging|rehearsal   stop her (`python3 -m doris stop`), let `run --live` write
#        traces/live-run-<stamp>/ itself, verify it, commit it from a NEW clone on the thread's branch, push.
# Leaves HALT in force afterwards (the overnight go runs `resume`) and the watch page up. Prints no setting's value.
set -u
DORIS=/root/doris
BRANCH=claude/project-thread-tgp2qi
LOOP=/root/doris-rehearsal
WORK=/root/doris-rehearsal-trace
say() { printf '%s %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }
die() { say "STOPPED: $*"; exit 1; }

ledger_look() {
  python3 -I - "$1" <<'PY'
import sqlite3, sys
con = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True)
print("ledger schema version:", [r[0] for r in con.execute("SELECT version FROM schema_version")])
rows = con.execute("SELECT booking_id, start_utc, end_utc, state FROM bookings ORDER BY start_utc").fetchall()
print("bookings in her ledger: %d" % len(rows))
for r in rows:
    print("  %s  %s to %s  %s" % r)
PY
}

calendar_look() {
  # Read only, as her, with the checkout's own adapter: her ledger's future bookings by ID, then every
  # event touching Thu Dec 3 and Fri Dec 4, 9 to 5 Pacific. Times and status, never a title or a setting.
  python3 -I - "$DORIS" <<'PY'
import datetime as dt, sqlite3, sys
root = sys.argv[1]
sys.path.insert(0, root + "/src")
from doris import envfile, loop, paths
from doris.adapters import live
env = envfile.load(paths.LIVE_ENV, names=loop._her_names())
cal = live.LiveCalendar(env)
utc = dt.timezone.utc
pst = dt.timezone(dt.timedelta(hours=-8))
now = dt.datetime.now(utc)
con = sqlite3.connect("file:%s/state/live/ledger.sqlite?mode=ro" % root, uri=True)
for bid, start, end, state in con.execute("SELECT booking_id, start_utc, end_utc, state FROM bookings ORDER BY start_utc"):
    when = dt.datetime.fromisoformat(start.replace("Z", "+00:00"))
    if when < now:
        continue
    try:
        ev = cal.get(bid)
        there = "GONE from the calendar" if ev is None else "on the calendar, %s, %s to %s Pacific" % (
            ev.status or "no status", ev.start_utc.astimezone(pst).strftime("%a %b %d %H:%M"),
            ev.end_utc.astimezone(pst).strftime("%H:%M"))
    except Exception as exc:
        there = "read failed: %s" % type(exc).__name__
    print("  booking %s (ledger: %s, %s): %s" % (bid, state, when.astimezone(pst).strftime("%a %b %d %H:%M"), there))
for day in (dt.date(2026, 12, 3), dt.date(2026, 12, 4)):
    a = dt.datetime(day.year, day.month, day.day, 9, tzinfo=pst)
    b = dt.datetime(day.year, day.month, day.day, 17, tzinfo=pst)
    try:
        events = cal.query(a.astimezone(utc), b.astimezone(utc))
    except Exception as exc:
        print("  %s: read failed: %s" % (day, type(exc).__name__))
        continue
    print("  %s, 9 to 5 Pacific: %d event(s) touching it, %d readable only as busy all span" % (
        day.strftime("%a %b %d"), len(events), cal.last_query_whole_span))
    for ev in events:
        span = "%s to %s Pacific" % (ev.start_utc.astimezone(pst).strftime("%H:%M"),
                                     ev.end_utc.astimezone(pst).strftime("%H:%M")) if ev.start_utc and ev.end_utc else ev.kind
        print("    %s  %s %s %s%s" % (span, ev.kind, ev.status or "no status", ev.transp or "no transp",
                                      "  (hers)" if ev.uid.startswith("doris-") else ""))
PY
}

prep() {
  cd "$DORIS" || die "no $DORIS"
  say "doris processes: $(pgrep -af 'doris (run|_worker|watch)' | wc -l); tmux: $(tmux ls 2>/dev/null | cut -d: -f1 | tr '\n' ' ')"
  say "checkout: $(git log --oneline -1)"
  say "tracked changes: $(git status --porcelain --untracked-files=no | wc -l); untracked: $(git ls-files --others --exclude-standard | tr '\n' ' ')"
  say "halt files: $(ls state/live/ state/sandbox/ 2>/dev/null | grep -i halt | tr '\n' ' ')"
  python3 -I -c 'import json,sys; r=json.load(open(sys.argv[1])); print("smoke record: ts %s, sent copy found %s" % (r.get("ts"), r.get("sent_copy_found")))' \
    state/live/smoke-sent.json || say "NO smoke record (run --live would refuse)"
  [ -f state/live/ledger.sqlite ] || die "no live ledger in $DORIS/state/live"
  ls -la state/live/ | grep -i ledger
  ledger_look state/live/ledger.sqlite
  mkdir -p "$LOOP/aside"
  if ls "$LOOP"/aside/ledger.sqlite.before-rehearsal-* >/dev/null 2>&1; then
    say "ledger copy already aside: $(ls "$LOOP"/aside/ | grep ledger | tr '\n' ' ')"
  else
    pgrep -f "doris (run|_worker)" >/dev/null && die "a doris run is alive: not copying a ledger in use"
    STAMP=$(date -u +%Y%m%dT%H%M%SZ)
    for f in state/live/ledger.sqlite*; do
      cp -p "$f" "$LOOP/aside/$(basename "$f").before-rehearsal-$STAMP" || die "copy of $f failed"
    done
    say "ledger copied aside: $(sha256sum state/live/ledger.sqlite | cut -c1-16) here, $(sha256sum "$LOOP/aside/ledger.sqlite.before-rehearsal-$STAMP" | cut -c1-16) aside"
  fi
  say "calendar, read only:"; calendar_look
  say "PREP DONE"
}

drymigrate() {
  # main's own code opens a COPY of the ledger kept aside, in a scratch folder: the schema upgrade is seen
  # to work on her real rows before the live run makes it for good. Nothing live is opened or written.
  cd "$DORIS" || die "no $DORIS"
  git fetch -q origin main || die "fetch main"
  A=$(ls "$LOOP"/aside/ledger.sqlite.before-rehearsal-* 2>/dev/null | head -1)
  [ -n "$A" ] || die "no ledger copy aside: run prep first"
  D=$(mktemp -d "$LOOP/main-check.XXXXXX") || die "no scratch folder"
  git archive origin/main src | tar -x -C "$D" || die "archive of main failed"
  say "main is $(git log --oneline -1 origin/main)"
  cp -p "$A" "$D/ledger.sqlite" || die "copy failed"
  say "the copy before:"; ledger_look "$D/ledger.sqlite"
  python3 -I - "$D" <<'PY'
import sys
sys.path.insert(0, sys.argv[1] + "/src")
from doris import ledger
led = ledger.Ledger(sys.argv[1] + "/ledger.sqlite", create=False)
led.close()
print("main's code opened the copy: no error (it reads schema version %d)" % ledger.SCHEMA_VERSION)
PY
  say "python exit $?"
  say "the copy after:"; ledger_look "$D/ledger.sqlite"
  python3 -I -c 'import sqlite3,sys; c=sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True); print("followups rows:", c.execute("SELECT COUNT(*) FROM followups").fetchone()[0], "| quick_check:", c.execute("PRAGMA quick_check").fetchone()[0])' "$D/ledger.sqlite"
  say "live ledger untouched: $(sha256sum state/live/ledger.sqlite | cut -c1-16) (aside copy $(sha256sum "$A" | cut -c1-16))"
  say "DRY MIGRATION DONE in $D"
}

start() {
  [ -e "$LOOP/started" ] && die "$LOOP/started exists (start ran before): use status or finish"
  ls "$LOOP"/aside/ledger.sqlite.before-rehearsal-* >/dev/null 2>&1 || die "no ledger copy aside: run prep first"
  pgrep -af "doris (run|_worker)" && die "a doris run or worker is alive"
  for s in doris-live doris-mix doris-night; do tmux has-session -t "$s" 2>/dev/null && die "tmux $s exists"; done
  cd "$DORIS" || die "no $DORIS"
  [ -z "$(git status --porcelain --untracked-files=no)" ] || die "tracked files changed in $DORIS"
  say "checkout before: $(git rev-parse --short HEAD)"
  git fetch -q origin main || die "fetch main"
  # Trace folders a live run wrote here and that main now holds would block the checkout:
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
  python3 -I -c 'import json,sys; r=json.load(open(sys.argv[1])); print("smoke record: ts %s, sent copy found %s" % (r.get("ts"), r.get("sent_copy_found")))' \
    state/live/smoke-sent.json || die "no smoke record (run --live would refuse)"
  date -u +%Y%m%dT%H%M%SZ > "$LOOP/started"
  (cd src && python3 -m doris resume); say "resume exit $?"
  tmux new -d -s doris-live "cd $DORIS/src && exec python3 -m doris run --live >>$LOOP/run.out 2>&1"
  sleep 30
  pgrep -af "doris run --live" >/dev/null || { tail -n 20 "$LOOP/run.out"; die "run --live did not keep running"; }
  say "run --live is up: $(pgrep -af 'doris (run|_worker)' | wc -l) process(es)"
  tail -n 25 "$LOOP/run.out"
  ledger_look state/live/ledger.sqlite
  if tmux has-session -t doris-watch 2>/dev/null; then
    say "watch page already up (tmux doris-watch): left as it is"
  else
    tmux new -d -s doris-watch "cd $DORIS/src && exec python3 -m doris watch >>$LOOP/watch.out 2>&1"
    sleep 3
  fi
  tail -n 3 "$LOOP/watch.out"
  say "STARTED"
}

status() {
  say "processes: $(pgrep -af 'doris (run|_worker)' | wc -l) doris run, $(pgrep -af 'doris watch' | wc -l) watch; tmux: $(tmux ls 2>/dev/null | cut -d: -f1 | tr '\n' ' ')"
  say "her output (last ${1:-60} lines):"; tail -n "${1:-60}" "$LOOP/run.out"
}

finish() {
  case "${1:-}" in
    staging) WHAT="The staging run before the Sat Nov 28 rehearsal: python3 -m doris run --live on main in tmux doris-live,
the first live run on ledger schema 4 (the follow-up, the nudge, ruling 17's check). No mail was sent to her
by us in it. Stopped with python3 -m doris stop so the rehearsal could start on a newer main." ;;
    rehearsal) WHAT="The Sat Nov 28 rehearsal's live run: python3 -m doris run --live on main in tmux doris-live, Rae's
emails sent by hand from her Gmail in a browser (her path's first live run, decision 115), stopped
with python3 -m doris stop." ;;
    *) die "finish takes which run this was: staging or rehearsal" ;;
  esac
  [ -e "$LOOP/started" ] || die "no $LOOP/started: no run of this script's is going"
  cd "$DORIS" || die "no $DORIS"
  found() { ls -d "$DORIS"/traces/live-run-* 2>/dev/null; }
  # main holds earlier runs' traces: this run's folder is the one not there before the stop
  BEFORE=$(found)
  newone() { for d in $(found); do printf '%s\n' "$BEFORE" | grep -qxF "$d" || echo "$d"; done; }
  say "trace folders before the stop: $(printf '%s' "$BEFORE" | xargs -r -n1 basename | tr '\n' ' ')"
  RUN_PIDS=$(pgrep -f "doris (run|_worker)" | tr '\n' ' ')
  say "python3 -m doris stop (her processes: $RUN_PIDS)"
  ( cd src && python3 -m doris stop ); say "stop exit $?"
  for i in $(seq 1 240); do
    T=$(newone | tail -1)
    if [ -n "$T" ] && [ -f "$T/terminal.log" ] && ! pgrep -f "doris (run|_worker)" >/dev/null; then break; fi
    sleep 1
  done
  pgrep -af "doris (run|_worker)" && say "WARNING: a doris process is still alive after the stop"
  [ "$(newone | wc -l)" -eq 1 ] || die "expected exactly one new traces/live-run-* folder, found: $(newone | tr '\n' ' ')"
  T=$(newone); NAME=$(basename "$T"); STAMP=${NAME#live-run-}
  say "trace folder: traces/$NAME"; ls -la "$T"
  say "her output (last 40 lines):"; tail -n 40 "$LOOP/run.out"
  say "timeline.txt (last 80 lines):"; tail -n 80 "$T/timeline.txt"
  ( cd src && python3 -m doris verify "$T/receipts.jsonl" ); say "verify exit $?"
  [ -z "$(git status --porcelain --untracked-files=no)" ] || say "NOTE: $DORIS shows tracked changes: $(git status --porcelain --untracked-files=no | tr '\n' ' ')"
  if [ -e "$WORK" ]; then        # an earlier finish's clone: brought up to the branch, nothing else in it
    cd "$WORK" || die "cd $WORK"
    [ -z "$(git status --porcelain)" ] || die "$WORK has changes; not reusing it (trace left in $T)"
    git fetch -q origin "$BRANCH" && git checkout -q "$BRANCH" && git merge -q --ff-only "origin/$BRANCH" || die "could not bring $WORK up to $BRANCH"
  else
    git clone -q "$(git remote get-url origin)" "$WORK" || die "clone failed"
    cd "$WORK" || die "cd $WORK"
    git checkout -q -b "$BRANCH" "origin/$BRANCH" || die "branch $BRANCH"
  fi
  say "clone at $BRANCH $(git rev-parse --short HEAD)"
  git config core.hooksPath .githooks
  git config user.name EatnerBeav
  git config user.email 45749433+EatnerBeav@users.noreply.github.com
  cp -rp "$T" "traces/$NAME" || die "copy failed"
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
  git add -- "traces/$NAME"
  git status --short
  git diff --cached --quiet && die "nothing to commit"
  cat > /root/doris-rehearsal-msg.txt <<MSG
Live trace from wide ($1 run): traces/$NAME (pass or fail, committed as kept)

$WHAT
run --live wrote traces/$NAME itself.
$SUMMARY

Co-Authored-By: Claude Fable 5.2 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QnTDpEFxyQyWjBqVXbZrTP
MSG
  git commit -q -F /root/doris-rehearsal-msg.txt || die "commit blocked (secret scan?); nothing pushed. Staged files left in $WORK"
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
  mv "$T" "$LOOP/aside/" && say "moved $DORIS/traces/$NAME aside to $LOOP/aside/"
  say "untracked in $DORIS now: $(git -C "$DORIS" ls-files --others --exclude-standard | tr '\n' ' ')"
  say "halt files now: $(ls "$DORIS"/state/live/ "$DORIS"/state/sandbox/ 2>/dev/null | grep -i halt | tr '\n' ' ')"
  say "watch page: $(pgrep -af 'doris watch' | wc -l) process(es), left up"
  mv "$LOOP/started" "$LOOP/finished-$STAMP" && mv "$LOOP/run.out" "$LOOP/run-$STAMP.out" && say "markers moved on: start can run again"
  say "ALL DONE"
}

case "${1:-}" in
  prep) prep ;;
  drymigrate) drymigrate ;;
  start) start ;;
  status) shift; status "${1:-}" ;;
  finish) shift; finish "${1:-}" ;;
  *) echo "usage: $0 prep | drymigrate | start | status [lines] | finish staging|rehearsal"; exit 2 ;;
esac
