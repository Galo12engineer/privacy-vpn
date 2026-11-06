#!/usr/bin/env bash
# OFFSITE backup: pulls encrypted (age) control-plane dumps from anonvm to THIS machine.
# Run by a launchd job on the operator's Mac (see infra/launchd/). Backups on prod are
# root-0600 → we pull them via `sudo rsync` on the remote side. Locally they stay
# age-encrypted (decryption only with the private BACKUP_AGE_IDENTITY — offline).
#
# Without an offsite copy DR is incomplete: losing anonvm = losing both the DB and the backups.
# This script closes that gap.
#
# ALERTING (added 2026-07-21 after this failed silently for two days): a backup that breaks
# quietly is worse than no backup, because you only find out when you need it. The launchd job
# reported exit 255 and the newest copy was 2 days old while nothing anywhere said a word — the
# same class of bug as the silent monitor and the no-op reconcile. Two independent alarms now:
#   1. the pull itself failed (what happened);
#   2. the pull succeeded but the newest copy is stale — this catches the case the first one
#      cannot see, namely prod's own dump job dying while rsync happily copies nothing new.
# Config (NOT in git), shared with the uptime check: ~/.config/privacyvpn/uptime.env (chmod 600):
#   TELEGRAM_BOT_TOKEN=... / TELEGRAM_CHAT_ID=...
set -euo pipefail

REMOTE="${REMOTE:-deploy@203.0.113.20}"
REMOTE_DIR="${REMOTE_DIR:-/opt/privacyvpn/backups-out}"
# NOT ~/Documents: macOS TCC blocks launchd agents there, and this job silently died on it once
# (exit 78, fixed in 7a5b709). The plist passes DEST explicitly; this default must match it, or
# a manual run would quietly write to a different directory than the scheduled one.
DEST="${DEST:-$HOME/.privacyvpn/backups/prod}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
STALE_DAYS="${STALE_DAYS:-2}"

ENV_FILE="${BACKUP_ENV:-$HOME/.config/privacyvpn/uptime.env}"
STATE="${BACKUP_STATE:-$HOME/.config/privacyvpn/backup-pull.state}"
# Re-alert while still broken. Alerting only on the OK→FAIL edge is right for a 5-minute probe,
# but this job runs once a day: one missed notification would mean permanent silence on a dead
# backup. After this many hours a still-failing state speaks up again.
RENOTIFY_HOURS="${RENOTIFY_HOURS:-72}"
REF_URL="${BACKUP_REF_URL:-https://api.telegram.org}"

# shellcheck disable=SC1090
[ -f "$ENV_FILE" ] && . "$ENV_FILE"
: "${TELEGRAM_BOT_TOKEN:=}"
: "${TELEGRAM_CHAT_ID:=}"

notify() {
  [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ] || { echo "[backup-pull] no telegram creds — alert skipped"; return 0; }
  curl -s -m 15 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=$1" -o /dev/null || echo "[backup-pull] could not deliver the alert" >&2
}

# Is this Mac simply offline? Then the failure says nothing about the backup, and crying wolf
# trains you to ignore the channel (that is exactly how the uptime check burned itself on
# 2026-07-16). The reference is the Telegram API on purpose: if it is unreachable we could not
# deliver the alert anyway, so staying quiet costs nothing.
mac_is_offline() {
  local code
  code=$(curl -s -o /dev/null -m 15 -w "%{http_code}" "$REF_URL" 2>/dev/null)
  [ "${code:-000}" = "000" ]
}

# state file: "<STATUS> <EPOCH_OF_LAST_ALERT>"
read_state() { cat "$STATE" 2>/dev/null || echo "INIT 0"; }
now_epoch() { date +%s; }

# Emit an alert on a state CHANGE, or when we have been failing quietly for RENOTIFY_HOURS.
report() {  # report <OK|FAIL> <text>
  local status="$1" text="$2" prev prev_ts now age
  read -r prev prev_ts <<<"$(read_state)"
  now=$(now_epoch); age=$(( (now - ${prev_ts:-0}) / 3600 ))
  mkdir -p "$(dirname "$STATE")"
  if [ "$status" != "$prev" ] && [ "$prev" != "INIT" ]; then
    notify "$text"; echo "$status $now" > "$STATE"; return 0
  fi
  if [ "$status" = "FAIL" ] && [ "$age" -ge "$RENOTIFY_HOURS" ]; then
    notify "$text"; echo "$status $now" > "$STATE"; return 0
  fi
  # no alert — keep the timestamp of the last one so re-notify timing stays honest
  echo "$status ${prev_ts:-0}" > "$STATE"
}

mkdir -p "$DEST"
echo "[*] $(date -u +%FT%TZ) pull $REMOTE:$REMOTE_DIR → $DEST"

# --rsync-path="sudo rsync": the files are root-0600 on prod; deploy has NOPASSWD sudo.
# `|| rc=$?` rather than letting `set -e` kill us: the whole point of this change is that
# something must RUN after a failure. An alert placed below a bare `rsync` would be dead code.
# NB: do NOT write this as `if ! rsync …; then rc=$?` — inside an `if !` condition `$?` holds the
# NEGATED status, i.e. 0, so the script would exit 0 on a failed pull (launchd would call it a
# success) and the alert would read "rc=0". Caught by the test harness, not by review.
rc=0
rsync -az --itemize-changes \
  -e "ssh -i $SSH_KEY -o ConnectTimeout=20 -o BatchMode=yes" \
  --rsync-path="sudo rsync" \
  "$REMOTE:$REMOTE_DIR/" "$DEST/" || rc=$?
if [ "$rc" -ne 0 ]; then
  echo "[!] rsync failed (rc=$rc)" >&2
  if mac_is_offline; then
    # Do not touch the state: flipping to FAIL here would also fake a "restored" message later.
    echo "[backup-pull] ${REF_URL} is unreachable too — this Mac is offline, not the backup; staying quiet"
    exit 0
  fi
  report FAIL "🔴 privacyvpn: ОФФСАЙТ-БЭКАП НЕ СКАЧАЛСЯ — rsync с ${REMOTE} упал (rc=${rc}). Интернет у Мака есть. DR-копия не обновляется; проверь SSH/доступность anonvm."  # ru-ok: operator-facing Telegram text — the operator reads these alerts, CLAUDE.md keeps bot texts RU
  exit "$rc"
fi

count=$(find "$DEST" -name 'cp-*.sql.age' | wc -l | tr -d ' ')
latest=$(ls -1t "$DEST"/cp-*.sql.age 2>/dev/null | head -1 || true)
echo "[+] offline copies: $count; latest: ${latest:-none}"

# Health-guard: the pull can succeed while prod's own dump job is dead — then we faithfully
# mirror a directory that stops growing. Freshness is the only thing that proves the chain works
# end to end, so it gets its own alarm rather than a stderr line nobody reads.
if [ -z "$latest" ]; then
  report FAIL "🔴 privacyvpn: ОФФСАЙТ-БЭКАП ПУСТ — в ${DEST} нет ни одного cp-*.sql.age. Проверь автобэкап на anonvm."  # ru-ok: operator-facing Telegram text — the operator reads these alerts, CLAUDE.md keeps bot texts RU
  exit 1
fi
if [ "$(find "$latest" -mtime -"$STALE_DAYS" 2>/dev/null)" = "" ]; then
  age_days=$(( ( $(now_epoch) - $(stat -f %m "$latest" 2>/dev/null || stat -c %Y "$latest") ) / 86400 ))
  echo "[!] the newest backup is older than ${STALE_DAYS}d — check the auto-backup on anonvm" >&2
  report FAIL "🔴 privacyvpn: ОФФСАЙТ-БЭКАП ПРОТУХ — скачивание идёт, но свежей копии нет ${age_days} сут (последняя: $(basename "$latest")). Значит сломался сам дамп на anonvm."  # ru-ok: operator-facing Telegram text — the operator reads these alerts, CLAUDE.md keeps bot texts RU
  exit 1
fi

report OK "🟢 privacyvpn: оффсайт-бэкап снова в порядке — свежая копия $(basename "$latest"), всего ${count}."  # ru-ok: operator-facing Telegram text — the operator reads these alerts, CLAUDE.md keeps bot texts RU
echo "[backup-pull] OK"
