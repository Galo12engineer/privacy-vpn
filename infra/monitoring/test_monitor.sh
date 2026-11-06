#!/usr/bin/env bash
# Integration test for monitor.sh AND monitor-failed.sh — runs the REAL scripts against stubbed
# curl/docker/timeout/systemctl, asserting the onion self-heal + streak damping, the persist-only-
# after-delivery rule, and the anti-spam quiet window. No network, no systemd, no side effects.
# Run: bash infra/monitoring/test_monitor.sh   (exit 0 = all pass). NOT wired into pytest CI.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
MON="$HERE/monitor.sh"
MF="$HERE/monitor-failed.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
BIN="$TMP/bin"; mkdir -p "$BIN"

# --- stubs on PATH ---------------------------------------------------------
# curl: onion probe (has --socks5-hostname) prints $FAKE_ONION_CODE; a telegram POST exits $FAKE_TG_RC
# (non-zero = the send failed, exactly how a real curl reports a timeout); healthz prints 200.
# A telegram POST also dumps its argv to $TG_MARK when set — that file is how the tests below tell
# "stayed silent" apart from "tried and failed"; without it a suppressed run and a failed send look
# identical from the outside, which is exactly the confusion these tests exist to rule out.
cat > "$BIN/curl" <<'EOF'
#!/usr/bin/env bash
for a in "$@"; do
  [ "$a" = "--socks5-hostname" ] && { echo "${FAKE_ONION_CODE:-000}"; exit 0; }
  case "$a" in *api.telegram.org*)
    [ -n "${TG_MARK:-}" ] && printf '%s\n' "$@" > "$TG_MARK"
    exit "${FAKE_TG_RC:-0}" ;;
  esac
done
# healthz: 200 unless FAKE_HEALTHZ_CODE says otherwise. Every call is tallied in $HZ_MARK — the
# retry COUNT is load-bearing (it is what keeps a container recreate from paging the operator),
# and nothing else in the suite would notice if a future edit shortened the loop.
[ -n "${HZ_MARK:-}" ] && echo x >> "$HZ_MARK"
echo "${FAKE_HEALTHZ_CODE:-200}"
EOF
# docker/timeout: node-staleness query -> no nodes down, clean exit. timeout just drops its duration arg.
# The SALES query is told apart by a token that only its python carries; it prints $FAKE_SALES (the
# "id|text" wire format monitor.sh parses) so the notifier can be driven without a database.
cat > "$BIN/docker" <<'EOF'
#!/usr/bin/env bash
case "$*" in *GiftToken*) printf '%s\n' "${FAKE_SALES:-}"; exit "${FAKE_SALES_RC:-0}" ;; esac
exit 0
EOF
printf '#!/usr/bin/env bash\nshift; exec "$@"\n' > "$BIN/timeout"
# sleep: the retry backoffs (4s healthz, 5s onion) are real seconds that nothing here asserts — with
# them the suite takes ~a minute, without them it is instant. Timing-free by construction: the only
# clock-sensitive assertion (group 11) compares epochs written within one run.
printf '#!/usr/bin/env bash\nexit 0\n' > "$BIN/sleep"
# systemctl: monitor-failed.sh asks it why the parent died (`show ... -p Result --value`).
cat > "$BIN/systemctl" <<'EOF'
#!/usr/bin/env bash
case "$*" in *"-p Result"*) echo "${FAKE_RESULT:-exit-code}" ;; esac
exit 0
EOF
chmod +x "$BIN/curl" "$BIN/docker" "$BIN/timeout" "$BIN/systemctl" "$BIN/sleep"

STATE="$TMP/state"; OFAIL="$TMP/ofail"; MARK="$TMP/refresh-called"; SFAIL="$TMP/sfail"
SALES="$TMP/sales-seen"
# Shared env for every run: temp state, existing COMPOSE_DIR (so `cd` succeeds), refresh kick = touch
# marker. HB_HOUR=99 is unreachable (`date -u +%H` maxes at 23) -> the daily heartbeat never fires,
# keeping these runs deterministic regardless of the wall clock; the heartbeat has its own cases below.
run() { # $1 = onion code
  FAKE_ONION_CODE="$1" \
  PATH="$BIN:$PATH" COMPOSE_DIR="$TMP" STATE="$STATE" OFAIL_STATE="$OFAIL" SFAIL_STATE="$SFAIL" \
  SALES_STATE="$SALES" ONION_REFRESH_CMD="touch $MARK" HB_HOUR=99 \
  bash "$MON" 2>&1
}
# Same, but with a configured (stubbed) Telegram channel; $2 = curl's exit code for telegram POSTs.
run_tg() { # $1 = onion code, $2 = FAKE_TG_RC
  FAKE_ONION_CODE="$1" FAKE_TG_RC="$2" \
  TELEGRAM_BOT_TOKEN=stub-token TELEGRAM_CHAT_ID=stub-chat \
  PATH="$BIN:$PATH" COMPOSE_DIR="$TMP" STATE="$STATE" OFAIL_STATE="$OFAIL" SFAIL_STATE="$SFAIL" \
  SALES_STATE="$SALES" ONION_REFRESH_CMD="touch $MARK" HB_HOUR=99 \
  ONION_FAIL_STREAK="${TEST_OSTREAK:-3}" \
  bash "$MON" 2>&1
}
# Everything healthy, channel configured, $1 = the "id|text" lines the sales query returns.
# $2 = curl's exit code for telegram POSTs (0 = delivered). TG_MARK captures the LAST message sent.
run_sales() {
  rm -f "$TGM_SALES"
  FAKE_ONION_CODE=200 FAKE_SALES="$1" FAKE_TG_RC="${2:-0}" TG_MARK="$TGM_SALES" \
  TELEGRAM_BOT_TOKEN=stub-token TELEGRAM_CHAT_ID=stub-chat \
  PATH="$BIN:$PATH" COMPOSE_DIR="$TMP" STATE="$STATE" OFAIL_STATE="$OFAIL" SFAIL_STATE="$SFAIL" \
  SALES_STATE="${3:-$SALES}" ONION_REFRESH_CMD="touch $MARK" HB_HOUR=99 \
  bash "$MON" 2>&1
}
TGM_SALES="$TMP/tg-sales"
reset() { rm -f "$STATE" "$OFAIL" "$MARK" "$SFAIL" "$SALES" "$TGM_SALES"; }

# --- monitor-failed.sh (the OnFailure= dead-man's switch) -------------------
MFSTATE="$TMP/failed-epoch"; TGM="$TMP/tg-sent"
# MONITOR_EXIT_CODE/STATUS are what systemd reports about the activation that triggered the unit;
# the defaults here simulate a genuine OnFailure (the parent exited 1). TEST_MONSTATUS=0 simulates a
# manual `systemctl start`, which is exactly how the false alarm of 2026-08-04 was produced.
# MONITOR_UNIT is deliberately NOT a knob: systemd sets it either way, so it discriminates nothing.
run_mf() { # $1 = curl exit code for the telegram POST (0 = delivered)
  rm -f "$TGM"
  FAKE_TG_RC="$1" TG_MARK="$TGM" FAKE_RESULT="${TEST_RESULT:-exit-code}" \
  MONITOR_UNIT=privacyvpn-monitor.service MONFAIL_FORCE="${TEST_FORCE:-0}" \
  MONITOR_EXIT_CODE="${TEST_MONCODE-exited}" MONITOR_EXIT_STATUS="${TEST_MONSTATUS-1}" \
  TELEGRAM_BOT_TOKEN="${TEST_TOKEN-stub-token}" TELEGRAM_CHAT_ID="${TEST_CHAT-stub-chat}" \
  PATH="$BIN:$PATH" MONFAIL_STATE="$MFSTATE" MONFAIL_QUIET_SEC="${TEST_QUIET:-1800}" \
  bash "$MF" 2>&1
}
reset_mf() { rm -f "$MFSTATE" "$TGM"; }

fails=0
ok()   { echo "  ok: $1"; }
bad()  { echo "  FAIL: $1"; fails=$((fails+1)); }
assert_eq()      { [ "$1" = "$2" ] && ok "$3" || bad "$3 (got [$1] want [$2])"; }
assert_has()     { printf '%s' "$1" | grep -q -- "$2" && ok "$3" || bad "$3 (missing: $2)"; }
assert_absent()  { [ ! -e "$1" ] && ok "$2" || bad "$2 (exists: $1)"; }
assert_present() { [ -e "$1" ] && ok "$2" || bad "$2 (missing: $1)"; }

echo "0) healthz 502 -> retried 6 times before the alert (a container recreate must not page)"
reset
HZ="$TMP/hz-calls"; rm -f "$HZ"
out="$(FAKE_HEALTHZ_CODE=502 HZ_MARK="$HZ" run 200)"
assert_eq "$(wc -l < "$HZ" | tr -d " ")" "6" "healthz probed 6 times before giving up"
assert_has "$out" "healthz=502" "the failure is reported once the retries are spent"
assert_eq "$(cat "$STATE")" "FAIL: healthz=502; " "state == FAIL with healthz"
reset
HZ2="$TMP/hz-calls-ok"; rm -f "$HZ2"
out="$(HZ_MARK="$HZ2" run 200)"
assert_eq "$(wc -l < "$HZ2" | tr -d " ")" "1" "a healthy probe costs exactly one request"
assert_eq "$(cat "$STATE")" "OK" "state OK"

echo "1) single transient 000 -> kick refresh, streak=1, NO alert, state OK"
reset
out="$(run 000)"
assert_eq "$(cat "$OFAIL")" "1" "streak file == 1"
assert_present "$MARK" "tor-refresh was kicked on first 000"
assert_has "$out" "streak=1" "logs streak=1"
assert_has "$out" "kicked tor-refresh" "logs the self-heal kick"
assert_eq "$(cat "$STATE")" "OK" "state stays OK (onion NOT in problems)"

echo "2) then a 200 -> streak resets, no kick, state OK"
rm -f "$MARK"
out="$(run 200)"
assert_eq "$(cat "$OFAIL")" "0" "streak file reset to 0"
assert_absent "$MARK" "refresh NOT kicked on 200"
assert_eq "$(cat "$STATE")" "OK" "state OK"

echo "3) genuine outage: 000 x3 -> kick ONCE (streak1 only), alert at streak 3"
reset
o1="$(run 000)"; rm -f "$MARK"        # remove marker to prove it is NOT recreated later
o2="$(run 000)"
k2_absent=$([ ! -e "$MARK" ] && echo yes || echo no)
o3="$(run 000)"
k3_absent=$([ ! -e "$MARK" ] && echo yes || echo no)
assert_eq "$(cat "$OFAIL")" "3" "streak file == 3"
assert_eq "$k2_absent" "yes" "no kick on streak 2"
assert_eq "$k3_absent" "yes" "no kick on streak 3 (once per streak)"
assert_has "$o3" "for 3 consecutive runs -> alerting" "alerts at threshold"
assert_eq "$(cat "$STATE")" "FAIL: onion=000; " "state == FAIL with onion"

echo "4) ONION_SELF_HEAL=0 disables the kick"
reset
out="$(FAKE_ONION_CODE=000 PATH="$BIN:$PATH" COMPOSE_DIR="$TMP" STATE="$STATE" OFAIL_STATE="$OFAIL" \
      ONION_REFRESH_CMD="touch $MARK" ONION_SELF_HEAL=0 bash "$MON" 2>&1)"
assert_absent "$MARK" "no kick when ONION_SELF_HEAL=0"
assert_eq "$(cat "$OFAIL")" "1" "streak still counted (=1) with self-heal off"

echo "5) unwritable OFAIL_STATE -> NO kick storm, and alert undamped (never silent)"
reset
badof="$TMP/nodir/ofail"   # parent dir missing -> every write to it fails
out="$(FAKE_ONION_CODE=000 PATH="$BIN:$PATH" COMPOSE_DIR="$TMP" STATE="$STATE" OFAIL_STATE="$badof" \
      ONION_REFRESH_CMD="touch $MARK" bash "$MON" 2>&1)"
assert_absent "$MARK" "no kick when streak file unwritable (F1: no restart storm)"
assert_has "$out" "OFAIL_STATE unwritable -> alerting undamped" "logs undamped alert"
assert_eq "$(cat "$STATE")" "FAIL: onion=000(state-unwritable); " "F2: state FAIL, not silent, when unwritable"

echo "6) 🔴 alert whose telegram send FAILS -> state NOT persisted (alert stays owed), unit still green"
reset
out="$(TEST_OSTREAK=1 run_tg 000 7)"; rc=$?          # rc 7 = curl couldn't connect
assert_has "$out" "send FAILED" "the send is reported as failed"
assert_eq "$(cat "$STATE" 2>/dev/null)" "" "B: state left UNwritten -> next run re-alerts, outage can't go silent"
assert_eq "$rc" "0" "A: one blipped send does NOT fail the unit"
assert_eq "$(cat "$SFAIL")" "1" "send-fail streak == 1"

echo "7) ...and the next run re-sends it: alert delivered, state finally persisted"
out="$(TEST_OSTREAK=1 run_tg 000 0)"; rc=$?
assert_has "$out" "СБОЙ" "the owed 🔴 is re-sent, not lost"  # ru-ok: needle matching the Russian alert text above; translating it would stop the match
assert_eq "$(cat "$STATE")" "FAIL: onion=000; " "state persisted once delivered"
assert_eq "$rc" "0" "unit green"
assert_eq "$(cat "$SFAIL")" "0" "streak reset after a successful send"

echo "8) telegram broken for 3 runs in a row -> unit DOES fail (dead-man's switch still armed)"
reset
TEST_OSTREAK=1 run_tg 000 7 >/dev/null; r1=$?
TEST_OSTREAK=1 run_tg 000 7 >/dev/null; r2=$?
out="$(TEST_OSTREAK=1 run_tg 000 7)"; r3=$?
assert_eq "$r1$r2" "00" "first two failures stay green"
assert_eq "$r3" "1" "third consecutive failure fails the unit"
assert_has "$out" "failing for 3 consecutive runs" "logs why it gave up"

echo "9) unwritable SFAIL_STATE -> cannot damp, so do not swallow: fail the unit at once"
reset
out="$(FAKE_ONION_CODE=000 FAKE_TG_RC=7 TELEGRAM_BOT_TOKEN=t TELEGRAM_CHAT_ID=c \
      PATH="$BIN:$PATH" COMPOSE_DIR="$TMP" STATE="$STATE" OFAIL_STATE="$OFAIL" \
      SFAIL_STATE="$TMP/nodir/sfail" ONION_REFRESH_CMD="touch $MARK" HB_HOUR=99 \
      ONION_FAIL_STREAK=1 bash "$MON" 2>&1)"; rc=$?
assert_eq "$rc" "1" "unit fails when the streak can't be trusted"
assert_has "$out" "SFAIL_STATE unwritable" "says why"

echo "10) heartbeat send fails -> HB_STATE stays unwritten (owed, retried) and the unit stays green"
reset
HB="$TMP/hb"
out="$(FAKE_ONION_CODE=200 FAKE_TG_RC=7 TELEGRAM_BOT_TOKEN=t TELEGRAM_CHAT_ID=c \
      PATH="$BIN:$PATH" COMPOSE_DIR="$TMP" STATE="$STATE" OFAIL_STATE="$OFAIL" SFAIL_STATE="$SFAIL" \
      HB_STATE="$HB" HB_HOUR=0 bash "$MON" 2>&1)"; rc=$?
assert_has "$out" "daily" "the heartbeat was attempted"
assert_absent "$HB" "HB_STATE unwritten -> the heartbeat is retried next run"
assert_eq "$rc" "0" "A (the 2026-08-04 regression): a blipped heartbeat must NOT fire ⚠️ 'монитор не отработал'"  # ru-ok: needle matching the Russian alert text above; translating it would stop the match

### monitor-failed.sh — fires from OnFailure= when the probe above dies. It is the last thing that
### can still speak, so it must (a) always speak the first time and (b) never become its own spam.

echo "11) first fire -> alert sent, says WHY the parent died, epoch stamped"
reset_mf
out="$(run_mf 0)"; rc=$?
assert_present "$TGM" "a telegram POST was actually made"
assert_has "$(cat "$TGM")" "САМ МОНИТОР не отработал" "sends the dead-man's-switch text"  # ru-ok: needle matching the Russian alert text above; translating it would stop the match
assert_has "$(cat "$TGM")" "result=exit-code" "carries the parent's failure reason"
assert_has "$out" "alert sent" "logs the send"
assert_eq "$rc" "0" "exit 0 (nothing keys off its code, it must not add noise)"
now=$(date -u +%s); stamped=$(cat "$MFSTATE")
assert_eq "$(( now - stamped < 5 ))" "1" "epoch stamped ~now (got [$stamped] vs [$now])"

echo "12) fired again right away -> SUPPRESSED, no second message"
out="$(run_mf 0)"; rc=$?
assert_absent "$TGM" "no telegram POST inside the quiet window (this is the anti-spam guarantee)"
assert_has "$out" "suppressed" "logs the suppression"
assert_eq "$rc" "0" "exit 0"

echo "13) send FAILS -> window NOT consumed, so the next fire retries instead of going quiet"
reset_mf
out="$(run_mf 7)"; rc=$?                       # 7 = curl couldn't connect
assert_present "$TGM" "the send was attempted"
assert_has "$out" "send FAILED" "logs the failure"
assert_absent "$MFSTATE" "epoch UNstamped: a failed alert is not a delivered one"
assert_eq "$rc" "0" "exit 0"
out="$(run_mf 0)"                              # next OnFailure fire, telegram back up
assert_present "$TGM" "next fire re-sends rather than suppressing an alert nobody got"
assert_has "$out" "alert sent" "delivered on the retry"

echo "14) epoch older than the quiet window -> speaks again (suppression expires, not sticks)"
reset_mf
printf '%s' "$(( $(date -u +%s) - 1801 ))" > "$MFSTATE"
out="$(run_mf 0)"
assert_present "$TGM" "sends once the window has passed"
assert_has "$out" "alert sent" "logs the send"

echo "15) garbage epoch file -> treated as 'never', speaks (a corrupt state must not silence it)"
reset_mf
printf 'not-a-number' > "$MFSTATE"
out="$(run_mf 0)"
assert_present "$TGM" "sends despite the unparsable state"
assert_has "$out" "alert sent" "logs the send"

echo "16) no telegram creds (dormant) -> log only, no POST, still exit 0"
reset_mf
out="$(TEST_TOKEN= TEST_CHAT= run_mf 0)"; rc=$?
assert_absent "$TGM" "no POST attempted without creds"
assert_has "$out" "no telegram creds" "says it is log-only"
assert_eq "$rc" "0" "exit 0"
assert_absent "$MFSTATE" "no epoch stamped when nothing was sent"

echo "17) started BY HAND while the monitor is fine -> stays SILENT (regression 2026-08-04)"
reset_mf
out="$(TEST_MONSTATUS=0 TEST_RESULT=success run_mf 0)"; rc=$?
assert_absent "$TGM" "no POST: nothing failed, so the loudest alert must not fire"
assert_has "$out" "manual or spurious start" "explains why it kept quiet"
assert_eq "$rc" "0" "exit 0"
assert_absent "$MFSTATE" "no epoch stamped — the quiet window is not burned by a non-event"

echo "18) TIMEOUT (killed by signal, exit status is not a code) -> shouts; that is why it exists"
reset_mf
out="$(TEST_MONCODE=killed TEST_MONSTATUS=TERM TEST_RESULT=success run_mf 0)"
assert_present "$TGM" "a killed parent is a failure even when Result has not caught up"
assert_has "$out" "alert sent" "logs the send"

echo "19) no MONITOR_* at all (plain shell) but the monitor really IS failed -> still shouts"
reset_mf
out="$(TEST_MONCODE= TEST_MONSTATUS= TEST_RESULT=exit-code run_mf 0)"
assert_present "$TGM" "the second opinion (parent Result) keeps the dead-man's switch armed"
assert_has "$out" "alert sent" "logs the send"

echo "20) MONFAIL_FORCE=1 -> sends even by hand, so the send path stays testable"
reset_mf
out="$(TEST_MONSTATUS=0 TEST_RESULT=success TEST_FORCE=1 run_mf 0)"
assert_present "$TGM" "force bypasses the guard"
assert_has "$out" "alert sent" "logs the send"

### sales notifier (section 3 of monitor.sh) — a redeemed gift / a paid invoice, once each.

GIFT='gift:11111111-1111-1111-1111-111111111111|🎁 privacyvpn: активирован подарочный код\nТариф: 30 дней\nЗаметка: партия для магазина X'  # ru-ok: the payload IS the operator-facing Russian message; translating it would test a string the script never sends
SALE='sale:22222222-2222-2222-2222-222222222222|💰 privacyvpn: продажа\nТариф: 90 дней\nСумма: 13.50 USD'  # ru-ok: same — this is the wire payload of the Russian notification

echo "21) a redeemed gift -> ONE message carrying plan and note, id remembered"
reset
out="$(run_sales "$GIFT")"
assert_present "$TGM_SALES" "the gift redemption was announced"
assert_has "$(cat "$TGM_SALES")" "подарочный код" "the message says a gift code was redeemed"  # ru-ok: needle matching the Russian message above
assert_has "$(cat "$TGM_SALES")" "магазина X" "the operator note is included — it is what tells the batches apart"  # ru-ok: needle matching the operator note above
assert_eq "$(cat "$SALES")" "gift:11111111-1111-1111-1111-111111111111" "the id is remembered"

echo "22) the same gift on the next run -> SILENT (5-minute timer must not repeat a sale)"
out="$(run_sales "$GIFT")"
assert_absent "$TGM_SALES" "no second message for an already-announced redemption"
assert_eq "$(cat "$SALES")" "gift:11111111-1111-1111-1111-111111111111" "state unchanged"

echo "23) a NEW sale alongside the old gift -> only the sale is announced, both ids kept"
out="$(run_sales "$GIFT
$SALE")"
assert_has "$(cat "$TGM_SALES")" "продажа" "the paid invoice is announced"  # ru-ok: needle matching the Russian message above
assert_eq "$(grep -c . "$SALES")" "2" "both ids remembered"

echo "24) send FAILS -> the id is NOT remembered, so the next run re-announces it (money is not silent)"
reset
out="$(run_sales "$GIFT" 7)"
assert_eq "$(cat "$SALES" 2>/dev/null)" "" "nothing persisted while the message is still owed"
assert_has "$out" "NOT persisted (send failed)" "says the notification stays owed"
out="$(run_sales "$GIFT" 0)"
assert_present "$TGM_SALES" "the owed notification is re-sent"
assert_eq "$(cat "$SALES")" "gift:11111111-1111-1111-1111-111111111111" "and only then remembered"

echo "25) an id that left the 2-day window prunes itself (the state file cannot grow forever)"
reset
printf 'gift:99999999-9999-9999-9999-999999999999\n' > "$SALES"
out="$(run_sales "$GIFT")"
assert_eq "$(cat "$SALES")" "gift:11111111-1111-1111-1111-111111111111" "stale id dropped, current id kept"

echo "26) SALES_NOTIFY=0 and a dead query stay silent instead of guessing"
# STATE pre-seeded to OK on purpose: a fresh run would also send the "recovered" state alert, and
# TG_MARK cannot tell two messages apart — the assertion here is about the SALES message only.
reset; printf 'OK' > "$STATE"
out="$(SALES_NOTIFY=0 run_sales "$GIFT")"
assert_absent "$TGM_SALES" "SALES_NOTIFY=0 disables the notifier"
rm -f "$SALES" "$TGM_SALES"
out="$(FAKE_SALES_RC=1 run_sales "$GIFT")"
assert_absent "$TGM_SALES" "a failed sales query announces nothing"
assert_has "$out" "sales query failed" "and says so in the journal"

echo
if [ "$fails" -eq 0 ]; then echo "ALL PASS"; else echo "$fails ASSERTION(S) FAILED"; fi
exit "$fails"
