#!/bin/bash
# Tests for uptime-mac.sh. Run: bash infra/monitoring/test-uptime-mac.sh
# Uses local HTTP servers + the UPTIME_NOTIFY_CMD/UPTIME_WITNESS_CMD seams, so nothing is ever
# sent to Telegram and no real host is contacted. Case 5 is the regression test for the false
# alert of 2026-07-24: a blip inside the probe window must NOT be reported as an outage.
set -u
set +m   # no job-control chatter when the helper servers are killed

SCRIPT="$(cd "$(dirname "$0")" && pwd)/uptime-mac.sh"
WORK=$(mktemp -d)
PORT_T=18081   # target
PORT_R=18082   # reference
pass=0; fail=0

cleanup() { pkill -f "http.server $PORT_T" 2>/dev/null; pkill -f "http.server $PORT_R" 2>/dev/null; rm -rf "$WORK"; }
trap cleanup EXIT

mkdir -p "$WORK/www"; echo ok > "$WORK/www/healthz"
serve() {  # $1=port, $2=delay before start
  ( sleep "$2"; cd "$WORK/www" && exec python3 -m http.server "$1" --bind 127.0.0.1 ) >/dev/null 2>&1 &
  disown 2>/dev/null || true
}
wait_up() { for _ in $(seq 1 40); do curl -s -o /dev/null -m 1 "http://127.0.0.1:$1/healthz" && return 0; sleep 0.25; done; return 1; }

run() {  # run the script in an isolated sandbox; echoes nothing, sets $OUT/$ALERTS/$STATE_VAL
  local name="$1"; shift
  rm -f "$WORK/alerts"; touch "$WORK/alerts"
  OUT=$(env -i PATH="$PATH" HOME="$WORK" \
      UPTIME_ENV=/dev/null \
      UPTIME_STATE="$WORK/state" \
      UPTIME_LOG="$WORK/uptime.log" \
      UPTIME_LOCK="$WORK/lock" \
      UPTIME_NOTIFY_CMD="tee -a $WORK/alerts" \
      UPTIME_ROUNDS="${R_ROUNDS:-2}" UPTIME_ROUND_GAP="${R_GAP:-1}" UPTIME_TIMEOUT="${R_TIMEOUT:-3}" \
      "$@" bash "$SCRIPT" 2>&1)
  ALERTS=$(cat "$WORK/alerts"); STATE_VAL=$(cat "$WORK/state" 2>/dev/null || echo MISSING)
}

check() {  # $1=label, $2=expected, $3=actual
  if [ "$2" = "$3" ]; then pass=$((pass+1)); printf '  ✅ %s\n' "$1"
  else fail=$((fail+1)); printf '  ❌ %s\n     expected: %s\n     got:      %s\n' "$1" "$2" "$3"; fi
}
alert_kind() { case "$ALERTS" in *🔴*) echo RED;; *🟡*) echo YELLOW;; *🟢*) echo GREEN;; "") echo NONE;; *) echo OTHER;; esac; }
# NB: an inline `case` inside $(...) breaks bash parsing on the `)` — wrap it in a function.
# The needles passed to `contains` below are Russian on purpose: they are substrings of the alert
# text uptime-mac.sh actually sends, and that text is operator-facing (see CLAUDE.md — Telegram
# operator texts stay Russian, code and logs do not). Translating them here would stop matching.
contains() { case "$1" in *"$2"*) echo "yes";; *) echo "no: $2";; esac; }

# Witness stubs: witness_probe passes the host as an argument, so the hook must be a script that
# ignores it — `echo 200` would print "200 deploy@node1" and never match.
printf '#!/bin/sh\necho 200\n'  > "$WORK/witness-up.sh";   chmod +x "$WORK/witness-up.sh"
printf '#!/bin/sh\nexit 0\n'    > "$WORK/witness-down.sh"; chmod +x "$WORK/witness-down.sh"

# Own port that nothing ever binds: PORT_T comes back up in case 5, so it cannot double as "dead".
DEAD="http://127.0.0.1:18099/healthz"            # nothing listening -> curl exit 7
LIVE_T="http://127.0.0.1:$PORT_T/healthz"
LIVE_R="http://127.0.0.1:$PORT_R/healthz"

echo "== 1. target answers 200 -> OK, silence =="
serve "$PORT_T" 0; wait_up "$PORT_T" || { echo "server did not come up"; exit 1; }
echo OK > "$WORK/state"
run t1 UPTIME_URL="$LIVE_T" UPTIME_REF_URL="$LIVE_T"
check "state=OK" OK "$STATE_VAL"; check "no alert" NONE "$(alert_kind)"

echo "== 2. target dead AND ref dead -> this Mac is offline: leave state alone, stay silent =="
pkill -f "http.server $PORT_T"; sleep 0.5
echo OK > "$WORK/state"
run t2 UPTIME_URL="$DEAD" UPTIME_REF_URL="http://127.0.0.1:$PORT_R/healthz"
check "state untouched" OK "$STATE_VAL"
check "no alert" NONE "$(alert_kind)"
check "log explains the offline case" "yes" "$(contains "$OUT" "THIS MAC is offline")"

echo "== 3. target dead, ref alive, witness answers 200 -> BLIND (🟡), NOT 🔴 =="
serve "$PORT_R" 0; wait_up "$PORT_R" || { echo "ref did not come up"; exit 1; }
echo OK > "$WORK/state"
run t3 UPTIME_URL="$DEAD" UPTIME_REF_URL="$LIVE_R" \
      UPTIME_WITNESS_HOSTS="deploy@node1" UPTIME_WITNESS_CMD="$WORK/witness-up.sh"
check "state=BLIND" BLIND "$STATE_VAL"; check "alert is yellow" YELLOW "$(alert_kind)"
check "text says the server is alive" "yes" "$(contains "$ALERTS" "сервер жив")"  # ru-ok: needle matching the Russian alert text above; translating it would stop the match

echo "== 4. target dead, ref alive, witness silent -> FAIL (🔴) =="
echo OK > "$WORK/state"
run t4 UPTIME_URL="$DEAD" UPTIME_REF_URL="$LIVE_R" \
      UPTIME_WITNESS_HOSTS="deploy@node1" UPTIME_WITNESS_CMD="$WORK/witness-down.sh"
check "state=FAIL" FAIL "$STATE_VAL"; check "alert is red" RED "$(alert_kind)"
check "text carries the reason (no route)" "yes" "$(contains "$ALERTS" "TCP-коннект")"  # ru-ok: needle matching the Russian alert text above; translating it would stop the match
check "text names the witness" "yes" "$(contains "$ALERTS" "свидетель node1")"  # ru-ok: needle matching the Russian alert text above; translating it would stop the match

echo "== 5. REGRESSION 2026-07-24: target dead in round 1, alive in round 2 -> OK, silence =="
echo OK > "$WORK/state"
serve "$PORT_T" 3     # comes up in the middle of the probe window
R_ROUNDS=4 R_GAP=2 run t5 UPTIME_URL="$LIVE_T" UPTIME_REF_URL="$LIVE_R" \
      UPTIME_WITNESS_HOSTS="deploy@node1" UPTIME_WITNESS_CMD="$WORK/witness-down.sh"
check "state=OK (blip survived)" OK "$STATE_VAL"
check "no false 🔴" NONE "$(alert_kind)"
wait_up "$PORT_T" >/dev/null

echo "== 6. recovery: prev=FAIL, target 200 -> 🟢 =="
echo FAIL > "$WORK/state"
run t6 UPTIME_URL="$LIVE_T" UPTIME_REF_URL="$LIVE_R"
check "state=OK" OK "$STATE_VAL"; check "alert is green" GREEN "$(alert_kind)"

echo "== 7. no state change (OK->OK) -> silence =="
echo OK > "$WORK/state"
run t7 UPTIME_URL="$LIVE_T" UPTIME_REF_URL="$LIVE_R"
check "no alert" NONE "$(alert_kind)"

echo "== 8. first ever run (no state file) -> write the state, do not alert =="
rm -f "$WORK/state"
run t8 UPTIME_URL="$LIVE_T" UPTIME_REF_URL="$LIVE_R"
check "state=OK" OK "$STATE_VAL"; check "no alert" NONE "$(alert_kind)"

echo "== 9. lock: a concurrent run is skipped =="
mkdir -p "$WORK/lock"
echo OK > "$WORK/state"
run t9 UPTIME_URL="$LIVE_T" UPTIME_REF_URL="$LIVE_R"
check "second run skipped" "yes" "$(contains "$OUT" "another run still in progress")"
rmdir "$WORK/lock" 2>/dev/null

echo "== 10. ref answers NON-200 (the real api.telegram.org gives 302) -> the Mac counts as online =="
# Regression from the live run on 2026-07-24: the condition 'ref != 200 => the Mac is offline' would
# have muted the check forever.
echo OK > "$WORK/state"
run t10 UPTIME_URL="$DEAD" UPTIME_REF_URL="http://127.0.0.1:$PORT_R/redirect-me" \
      UPTIME_WITNESS_HOSTS="deploy@node1" UPTIME_WITNESS_CMD="$WORK/witness-down.sh"
check "not swallowed as 'this Mac is offline'" "no: THIS MAC is offline" "$(contains "$OUT" "THIS MAC is offline")"
check "state=FAIL" FAIL "$STATE_VAL"; check "alert is red" RED "$(alert_kind)"

echo "== 11. the log is written to a file (survives a /tmp wipe) =="
check "log file is not empty" 0 "$([ -s "$WORK/uptime.log" ] && echo 0 || echo 1)"

echo
echo "total: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
