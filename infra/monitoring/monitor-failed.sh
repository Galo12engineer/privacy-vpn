#!/usr/bin/env bash
# Fires via OnFailure= when privacyvpn-monitor.service ITSELF fails (timeout, crash, non-zero exit).
#
# Why this exists: monitor.sh sends its alert at the END of the run. Its probes get SLOW exactly when
# something is broken (a dead healthz burns 3x15s of retries, a dead onion 3x30s), so a real outage
# used to push the run past TimeoutStartSec, systemd killed it, and the alert code never ran. The
# dead-man's switch went quiet precisely when it mattered — and quiet reads as "all fine".
# Observed live on 2026-07-16: three consecutive runs killed with result 'timeout', zero alerts.
#
# Kept deliberately dumb and fast: no docker, no Tor, one HTTP call, short timeout. It must not be
# able to hang for the same reason its parent could.
set -uo pipefail

STATE="${MONFAIL_STATE:-/var/lib/privacyvpn-monitor/failed-alert-epoch}"
QUIET_FOR="${MONFAIL_QUIET_SEC:-1800}"  # at most one such alert per 30 min — a broken monitor
                                        # retries every 5 min and must not become its own spam

result=$(systemctl show privacyvpn-monitor.service -p Result --value 2>/dev/null)

# 🔴 Only shout when the parent ACTUALLY failed. Started any other way, this script sends the
# loudest alert in the system about nothing: on 2026-08-04 a post-deploy verification run of
# `systemctl start privacyvpn-monitor-failed.service` delivered a genuine "the monitor itself did
# not run" alert to the operator, stamped result=success — a message contradicting itself, since
# success means the parent had NOT failed. Second false alarm in one day on the ONE message that
# must never be trained into background noise.
#
# MONITOR_UNIT is NOT a usable discriminator, however tempting: systemd derives it from the
# OnFailure= relationship, so a plain manual start of THIS unit carries it too. Measured on anonvm
# with an ExecStartPre probe: manual `systemctl start` -> MONITOR_UNIT=privacyvpn-monitor.service,
# MONITOR_EXIT_STATUS=0; a genuine OnFailure -> MONITOR_EXIT_STATUS=1. So the exit REPORT is what
# separates them, and it belongs to the triggering activation, which also makes it race-free with
# respect to the parent's next scheduled run.
#   MONITOR_EXIT_CODE   'exited' | 'killed' | 'dumped'   MONITOR_EXIT_STATUS  exit code or signal
# A timeout (the very incident this script was written for, 2026-07-16) arrives as 'killed'/TERM,
# never as exited/0, so it still shouts.
# The parent's Result is kept as a SECOND opinion for the case where the variables are absent
# entirely (older systemd, running the script straight from a shell) while the parent really is
# failed. A dead-man's switch may go quiet only when EVERY signal agrees that nothing broke.
# MONFAIL_FORCE=1 bypasses the guard, so the send path stays testable without faking a failure —
# the absence of such a path is what made a bare `systemctl start` look reasonable in the first place.
mon_code="${MONITOR_EXIT_CODE:-}"
mon_status="${MONITOR_EXIT_STATUS:-}"
if [ "${MONFAIL_FORCE:-0}" != "1" ] \
   && { [ -z "$mon_code" ] || [ "$mon_code" = "exited" ]; } \
   && { [ -z "$mon_status" ] || [ "$mon_status" = "0" ]; } \
   && { [ -z "$result" ] || [ "$result" = "success" ]; }; then
  echo "[monitor-failed] nothing failed (exit=${mon_code:-none}/${mon_status:-none}, monitor is" \
       "'${result:-unknown}') — manual or spurious start, not alerting (MONFAIL_FORCE=1 to test)"
  exit 0
fi

if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
  echo "[monitor-failed] no telegram creds — log only" >&2
  exit 0
fi

now=$(date -u +%s)
last=$(cat "$STATE" 2>/dev/null || echo 0)
case "$last" in (*[!0-9]*|"") last=0 ;; esac
if [ "$((now - last))" -lt "$QUIET_FOR" ]; then
  echo "[monitor-failed] suppressed (last alert $((now - last))s ago)"
  exit 0
fi

# ru-ok:begin operator-facing Telegram text — the operator reads these alerts, CLAUDE.md keeps bot texts RU
text="⚠️ privacyvpn: САМ МОНИТОР не отработал (result=${result:-unknown}).
Его молчание — это НЕ «всё ок»: проверки могли не дойти до отправки алерта.
Проверить руками: healthz, .onion через Tor, ноды, docker ps."
# ru-ok:end

if curl -fsS --max-time 15 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
     -d chat_id="${TELEGRAM_CHAT_ID}" --data-urlencode "text=$text" >/dev/null 2>&1; then
  mkdir -p "$(dirname "$STATE")"
  printf '%s' "$now" > "$STATE"
  echo "[monitor-failed] alert sent (result=${result:-unknown})"
else
  echo "[monitor-failed] send FAILED (result=${result:-unknown})" >&2
fi
