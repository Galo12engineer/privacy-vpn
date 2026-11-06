#!/bin/bash
# External uptime check for privacyvpn — runs from the operator's Mac (INDEPENDENTLY of anonvm).
# Rationale: a monitor on anonvm itself will not notice the death of anonvm itself. This probes
# from the outside and alerts to Telegram on state transitions (no spam, via a state file).
#
# Config (NOT in git): ~/.config/privacyvpn/uptime.env (chmod 600):
#   TELEGRAM_BOT_TOKEN=...
#   TELEGRAM_CHAT_ID=...
#   UPTIME_WITNESS_HOSTS="deploy@<node1-ip> deploy@<node2-ip>"   # optional, see "witness" below
# Node IPs belong in the env file, not here: infra/ is git-tracked and must not carry them.
# Caveat: the Mac must be powered on / not asleep — this is "external best-effort", not a 24/7
# cloud. For 24/7 add UptimeRobot (requires your account) on top of this.
set -u

ENV_FILE="${UPTIME_ENV:-$HOME/.config/privacyvpn/uptime.env}"
STATE="${UPTIME_STATE:-$HOME/.config/privacyvpn/uptime.state}"
URL="${UPTIME_URL:-https://privacyvpn.example.com/healthz}"

# shellcheck disable=SC1090
[ -f "$ENV_FILE" ] && . "$ENV_FILE"
: "${TELEGRAM_BOT_TOKEN:=}"
: "${TELEGRAM_CHAT_ID:=}"

# The reference host is the Telegram API on purpose: if it is unreachable, no alert could be
# delivered anyway, so suppressing the alert costs nothing.
REF_URL="${UPTIME_REF_URL:-https://api.telegram.org}"
# A blip lasting tens of seconds must not be reported as an outage -> spread probes over ~1 min.
ROUNDS="${UPTIME_ROUNDS:-4}"
ROUND_GAP="${UPTIME_ROUND_GAP:-20}"
TIMEOUT="${UPTIME_TIMEOUT:-10}"
# Independent second opinion: hosts that are neither this Mac nor anonvm (the exit nodes). Empty
# by default — without them the check degrades to one point of view instead of failing.
WITNESS_HOSTS="${UPTIME_WITNESS_HOSTS:-}"
LOG="${UPTIME_LOG:-$HOME/.privacyvpn/logs/uptime.log}"   # NOT /tmp: after a flap this history is
LOG_MAX_BYTES="${UPTIME_LOG_MAX_BYTES:-524288}"          # the only forensic trail there is
LOCK="${UPTIME_LOCK:-$HOME/.privacyvpn/uptime.lock}"
# Test seams, set only by test-uptime-mac.sh — never in production.
NOTIFY_CMD="${UPTIME_NOTIFY_CMD:-}"
WITNESS_CMD="${UPTIME_WITNESS_CMD:-}"

log() {  # stdout goes to the launchd log; the file keeps the durable history
  echo "[uptime-mac] $1"
  mkdir -p "$(dirname "$LOG")" 2>/dev/null || return 0
  if [ -f "$LOG" ]; then
    local sz
    sz=$(wc -c < "$LOG" 2>/dev/null | tr -d ' ')
    [ "${sz:-0}" -gt "$LOG_MAX_BYTES" ] && mv -f "$LOG" "${LOG}.1"
  fi
  printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$1" >> "$LOG"
}

notify() {
  if [ -n "$NOTIFY_CMD" ]; then printf '%s\n' "$1" | $NOTIFY_CMD; return; fi
  [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ] || { log "no telegram creds — skip alert"; return; }
  curl -s -m 15 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=$1" -o /dev/null
}

probe() {  # -> "<http_code> <curl_exit>"; "000 <exit>" on a connection-level failure
  local out
  # curl -w already prints 000 when it cannot connect. `|| echo 000` would APPEND a second 000
  # ("HTTP 000000" — a real false alert on 2026-07-16) -> use the default-substitution instead.
  out=$(curl -s -o /dev/null -m "$TIMEOUT" -w '%{http_code} %{exitcode}' "$1" 2>/dev/null)
  printf '%s' "${out:-000 99}"
}

why() {  # curl exit code -> what the operator should actually go and look at
  # ru-ok:begin operator-facing Telegram text — the operator reads these alerts, CLAUDE.md keeps bot texts RU (this reason is pasted into the alert)
  case "$1" in
    0)     echo "" ;;
    6)     echo "DNS не резолвится" ;;
    7)     echo "TCP-коннект отбит / нет маршрута" ;;
    28)    echo "таймаут ${TIMEOUT}s" ;;
    35|60) echo "TLS/сертификат" ;;
    *)     echo "curl exit=$1" ;;
  esac
  # ru-ok:end
}

witness_probe() {  # ask an independent host whether the site answers FROM THERE
  local host="$1"
  if [ -n "$WITNESS_CMD" ]; then $WITNESS_CMD "$host"; return; fi
  ssh -o BatchMode=yes -o ConnectTimeout=8 "$host" \
    "curl -s -o /dev/null -m 10 -w '%{http_code}' '$URL'" 2>/dev/null
}

# One run at a time: a slow run must not overlap the next launchd tick.
if ! mkdir "$LOCK" 2>/dev/null; then
  if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +10 2>/dev/null)" ]; then
    rmdir "$LOCK" 2>/dev/null && mkdir "$LOCK" 2>/dev/null || exit 0
  else
    log "another run still in progress — skip"; exit 0
  fi
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

# ---------------------------------------------------------------------------------------------
# Probe loop. THE FIX (false alert of 2026-07-24): the reference host is probed IN THE SAME ROUND
# as the target, not once after every retry has already failed. The old order (3 target tries,
# then one reference try 15-60 s later) meant a short blip of the Mac's own network was already
# over by the time the reference was checked — the reference answered, the script concluded "this
# Mac is online, so the server is down", and fired 🔴 while Caddy had been serving 200s the whole
# time (node heartbeats every 30 s, zero gaps, no UFW drops, no TLS errors). The alert being
# delivered at all was itself the proof that connectivity had returned before the verdict.
# A monitor that cries wolf trains you to ignore it, and then you miss the real outage.
# ---------------------------------------------------------------------------------------------
code=000; cerr=99; status=FAIL
round=0
while [ "$round" -lt "$ROUNDS" ]; do
  round=$((round + 1))
  read -r code cerr <<< "$(probe "$URL")"
  if [ "$code" = "200" ]; then status=OK; break; fi

  # Same moment, not a minute later: is it the SERVER that is down, or this Mac that is offline?
  # ANY http code means DNS+TCP+TLS+HTTP all worked, i.e. this Mac has connectivity. Only 000 (no
  # response at all) means offline — api.telegram.org answers 302, so a `!= 200` test here would
  # declare the Mac offline forever and the check would never report a real outage again.
  read -r ref_code _ <<< "$(probe "$REF_URL")"
  if [ "$ref_code" = "000" ]; then
    # Leave the state untouched: flipping it to FAIL here would also fake a "🟢 restored" later.
    log "$URL -> $code, but $REF_URL -> $ref_code in the same round: THIS MAC is offline, not the server — no alert"
    exit 0
  fi
  log "round $round/$ROUNDS: $URL -> $code ($(why "$cerr")), $REF_URL -> $ref_code"
  [ "$round" -lt "$ROUNDS" ] && sleep "$ROUND_GAP"
done

# Every round failed with the Mac demonstrably online -> get a second opinion from a host that is
# neither this Mac nor anonvm. If the site answers from there, the server is fine and it is the
# path from here that is broken: that is a BLIND monitor, not an outage. Reported as 🟡, so a
# broken home network never again reads as "the service is down".
witness_note=""
if [ "$status" = "FAIL" ] && [ -n "$WITNESS_HOSTS" ]; then
  # ru-ok:begin operator-facing Telegram text — the operator reads these alerts, CLAUDE.md keeps bot texts RU (witness_note is pasted into the alert)
  for host in $WITNESS_HOSTS; do
    w=$(witness_probe "$host")
    if [ "$w" = "200" ]; then
      status=BLIND
      witness_note="со свидетеля ${host##*@} сайт отвечает 200"
      break
    fi
    witness_note="свидетель ${host##*@} → ${w:-нет ответа}"
  done
  # ru-ok:end
fi

prev=$(cat "$STATE" 2>/dev/null || echo INIT)
mkdir -p "$(dirname "$STATE")"
echo "$status" > "$STATE"

if [ "$status" != "$prev" ] && [ "$prev" != "INIT" ]; then
  # ru-ok:begin operator-facing Telegram text — the operator reads these alerts, CLAUDE.md keeps bot texts RU
  case "$status" in
    FAIL)
      notify "🔴 privacyvpn DOWN (внешний чек с Мака): ${URL} → HTTP ${code} ($(why "$cerr")), ${ROUNDS} раунда(ов) за $(( (ROUNDS - 1) * ROUND_GAP ))с. Интернет у Мака есть (${REF_URL} отвечал в тех же раундах)${witness_note:+, $witness_note}." ;;
    BLIND)
      notify "🟡 privacyvpn недоступен С МАКА, но сервер жив: ${URL} → HTTP ${code} ($(why "$cerr")), ${witness_note}. Это проблема сети/маршрута с этого Мака, а не падение сервиса — внешний чек временно слеп." ;;
    OK)
      notify "🟢 privacyvpn восстановлен (внешний чек с Мака): ${URL} → 200." ;;
  esac
  # ru-ok:end
fi

log "$URL -> $code ($status), prev=${prev}${witness_note:+, $witness_note}"
