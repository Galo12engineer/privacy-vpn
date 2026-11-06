#!/usr/bin/env bash
# privacyvpn monitoring probe (systemd-timer on anonvm): checks healthz (proxy+TLS+CP+DB) and
# the freshness of node health (via CP). Alerts on a state CHANGE (OK<->FAIL) — no spam — PLUS
# a daily heartbeat ("alive, all OK") as a dead-man's switch: otherwise a silently dead monitor
# is indistinguishable from "everything is fine". Heartbeat hour: env HB_HOUR (UTC, default 12 = 08:00 EDT).
#
# It also carries the SALES notifier (section 3): paid invoices and redeemed gift codes, sent to the
# same channel. Not monitoring, but it rides here for one reason — the alert channel already exists
# on this host, whereas notifying from control-plane would mean two new CP_* variables, and a CP_*
# whose compose passthrough was forgotten is the failure mode CLAUDE.md warns about twice.
#
# Channel: /etc/privacyvpn-monitor.env -> TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID (create a bot
# via @BotFather, find out chat_id). Empty -> the probe works, but alerts go only to the log (dormant).
#
# WARNING Limitation: the probe lives on anonvm -> it will not notice a full DEATH of anonvm itself (it dies along with it).
# For that you need an EXTERNAL uptime check (UptimeRobot free / phone) — operator's responsibility. This probe
# catches: a node going down, DB unavailability, application hangs, TLS/proxy problems.
set -uo pipefail

COMPOSE_DIR="${COMPOSE_DIR:-/opt/privacyvpn}"
DOMAIN="${DOMAIN:-privacyvpn.example.com}"
STATE="${STATE:-/var/lib/privacyvpn-monitor/state}"
mkdir -p "$(dirname "$STATE")"

problems=""

# 1) healthz — the whole external path (proxy -> TLS -> CP -> SELECT 1). We retry: a blip during a
# deploy/restart of control-plane (Caddy returns 502) must not send a false FAIL — a real outage
# survives every attempt.
#
# What the retries have to outlast is a CONTAINER RECREATE, not a network hiccup: `up -d --build`
# stops the old container, waits for the DB healthcheck and runs `alembic upgrade head` before the
# new one serves. Measured on 2026-08-08: ~55s of 502, and the previous 3 attempts / 8s of sleep
# alerted the operator mid-deploy. The span is what matters, not the attempt count — Caddy answers
# 502 immediately, so during a blip the elapsed time is essentially the sleeps: 5x10 = 50s here.
# Still not the whole recreate window, deliberately: covering it fully would mean a minute and a
# half of silence over a genuine outage.
code="000"
for i in 1 2 3 4 5 6; do
  code=$(curl -fsS -o /dev/null -w "%{http_code}" --max-time 15 "https://$DOMAIN/healthz" 2>/dev/null)
  code=${code:-000}
  [ "$code" = "200" ] && break
  [ "$i" = "6" ] || sleep 10   # no point sleeping after the last attempt — the budget is not free
done
[ "$code" = "200" ] || problems="${problems}healthz=$code; "

# 2) nodes — staleness via CP (last_health_at older than node_stale_seconds).
# `timeout` is load-bearing: a wedged docker/CP would block this forever, the systemd timeout would
# kill the whole probe, and the alert code below would never run — silence exactly during an outage.
#
# 🔴 The python MUST stay inside SINGLE quotes. Inside "..." bash strips the inner double quotes, so
# `SKIP = {"PROVISIONING", ...}` reached python as `SKIP = {PROVISIONING, ...}` -> NameError, the
# `2>/dev/null` swallowed it and an ignored exit code turned the crash into "no nodes down" = OK.
# The node-down alert was therefore dead from the moment the SKIP filter was added until 2026-07-16.
NODE_STALE_SECONDS="${NODE_STALE_SECONDS:-300}"
down=$(cd "$COMPOSE_DIR" && timeout 20 docker compose exec -T -e NODE_STALE_SECONDS="$NODE_STALE_SECONDS" control-plane python -c '
import os
from app.db import SessionLocal
from app.models import Node
from app.models.base import utcnow
from app.config import get_settings
lim = int(os.environ.get("NODE_STALE_SECONDS") or get_settings().node_stale_seconds)
# Nodes that are NOT supposed to be healthy must not wake the operator:
#   PROVISIONING — just created, the agent has not come up yet (otherwise EVERY node addition = a false 🔴);
#   DISABLED     — deliberately turned off.
# DOWN is deliberately NOT skipped: if the brain itself marked a dead node as DOWN, silence would mask the outage.
SKIP = {"PROVISIONING", "DISABLED"}
with SessionLocal() as s:
    for n in s.query(Node).all():
        if getattr(n.status, "name", str(n.status)).upper() in SKIP:
            continue
        age = (utcnow() - n.last_health_at).total_seconds() if n.last_health_at else 9e9
        if age > lim:
            print(n.name)
' 2>/dev/null)
node_rc=$?
# "could not check" is NOT "healthy" — say so out loud instead of silently reporting no problems.
if [ "$node_rc" -ne 0 ]; then
  problems="${problems}nodes_query=failed(rc=$node_rc); "
elif [ -n "$down" ]; then
  problems="${problems}nodes_down=$(echo "$down" | tr '\n' ',' ); "
fi

# 3) onion — is it actually reachable THROUGH Tor? Catches what healthz/Caddy do NOT see: the HS descriptor
# stopped being published (happened on 09.07, the onion was down ~6 days unnoticed) OR the tor container went down.
# The containerized tor with SocksPort 0 is unsuitable -> a host-tor SOCKS is needed (apt install tor -> 127.0.0.1:9050).
# The address is hardcoded (it is stable, stored in the volume) — so that the check works even with a dead tor container.
TOR_SOCKS="${TOR_SOCKS:-127.0.0.1:9050}"
ONION="${ONION:-privacyvpnexampleonionaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.onion}"
# 🔴 --max-time here must fit a COLD connection, not a warm one. When this tor client's cached HS
# descriptor expires (every few hours), the next request pays descriptor re-fetch + introduce +
# rendezvous. Measured on three fresh tor clients against this very onion: 3.5s / 25s / 79s — ALL
# HTTP 200 — while a warm fetch is ~1s. The old --max-time 30 clipped the cold path, all retries
# landed inside the same slow fetch, and the probe cried "🔴 onion=000" every few hours about an
# onion that was never down (12 such false alarms in 24h on 2026-07-16/17). A real user's Tor
# Browser simply waits those seconds and gets the page.
ONION_MAX_TIME="${ONION_MAX_TIME:-120}"
ocode="000"
for i in 1 2; do  # two generous attempts beat three stingy ones: the first pays the cold cost
  # curl -w prints %{http_code} (000 on a connection failure) -> no `|| echo`, otherwise the code gets duplicated
  o_start=$(date +%s)
  ocode=$(curl -fsS -o /dev/null -w "%{http_code}" --socks5-hostname "$TOR_SOCKS" \
    --max-time "$ONION_MAX_TIME" "http://$ONION/healthz" 2>/dev/null)
  ocode=${ocode:-000}
  echo "onion attempt $i: http=$ocode in $(( $(date +%s) - o_start ))s"  # journal breadcrumb: makes
                                                                          # "slow but fine" visible
  [ "$ocode" = "200" ] && break
  [ "$i" = "2" ] || sleep 5
done

# Flap damping across RUNS, not just attempts within one run. Retries inside a single run all land
# in the same bad window and fail together, so they damp nothing.
# Observed 2026-07-16/17: twelve 🔴 onion=000 alerts in 24h, every one self-healing in 4-15 min
# (1-3 timer cycles), while the onion was demonstrably fine — six fresh tor clients fetched it in
# 3-9s, and the HS itself logged 59 INTRODUCE2 cells / 59 rendezvous circuits in 12h. The probe's
# tor is a long-lived client (uptime 7d+): when our HS rotates introduction points, this client's
# CACHED descriptor goes stale and its attempts fail until it re-fetches. A visitor with a fresh
# Tor Browser never sees that window. So: only call the onion down once it stays down.
# The 6-day silent outage of 2026-07-09 would still be caught — it failed every single cycle.
OFAIL_STATE="${OFAIL_STATE:-/var/lib/privacyvpn-monitor/onion-fail-streak}"
ONION_FAIL_STREAK="${ONION_FAIL_STREAK:-3}"  # x 5min timer => ~10-15 min of real unreachability
if [ "$ocode" = "200" ]; then
  printf '0' > "$OFAIL_STATE" 2>/dev/null
else
  streak=$(cat "$OFAIL_STATE" 2>/dev/null || echo 0)
  case "$streak" in (*[!0-9]*|"") streak=0 ;; esac
  streak=$((streak + 1))
  # Persist the streak; capture whether the write actually landed. An unwritable OFAIL_STATE (disk
  # full / RO /var — a condition CORRELATED with real incidents) would otherwise pin streak at 1
  # every run, a double trap: (a) it never reaches the threshold -> a genuine outage goes SILENT, and
  # (b) the self-heal below would kick a host-tor restart EVERY run -> a storm that itself causes
  # cold-cache 000s. Both are closed by gating on a successful write (streak_ok).
  if printf '%s' "$streak" > "$OFAIL_STATE" 2>/dev/null; then streak_ok=1; else streak_ok=0; fi
  # Self-heal the dominant cause of onion=000: THIS probe's long-lived host tor client (9050) is
  # sitting on a STALE hidden-service descriptor — its cached intro points are gone. Always true for
  # ~30-50 min right after a `docker compose restart tor` (the HS republishes at new intro points),
  # and periodically on descriptor rotation. A FRESH tor client fetches the same onion with 200 the
  # whole time (that is why the rule-3 deploy check, which spins an ephemeral tor, sees green while
  # this probe screams). Dropping the stale cache = restart host tor, which is exactly what
  # tor-refresh.service does. Kick it on the FIRST fail of a streak so the NEXT run re-fetches fresh
  # and the streak resets before it ever reaches the alert threshold. --no-block: fire-and-forget,
  # the ~30s re-bootstrap must not stall this probe. Harmless if the onion is GENUINELY down — the
  # refresh changes nothing, the streak climbs to the threshold and alerts exactly as before. Only
  # when the streak is trustworthy (streak_ok) and exactly 1: no restart storm, one kick per streak.
  # ONION_SELF_HEAL=0 disables; ONION_REFRESH_CMD overrides the kick (tests stub it).
  if [ "$streak_ok" = 1 ] && [ "$streak" -eq 1 ] && [ "${ONION_SELF_HEAL:-1}" = "1" ]; then
    if ${ONION_REFRESH_CMD:-systemctl start --no-block privacyvpn-tor-refresh.service} 2>/dev/null; then
      echo "onion=000 (streak=1) -> kicked tor-refresh (drop host-tor stale descriptor) before next run"
    else
      echo "onion=000 (streak=1) -> tor-refresh kick failed/unavailable — streak damping still applies"
    fi
  fi
  if [ "$streak_ok" != 1 ]; then
    # Can't persist the counter -> can't damp reliably. Do NOT stay silent during a real 000: surface
    # it undamped (an unwritable state dir is itself worth waking someone). Distinct token so the
    # alert reads as an infra problem, not a plain onion outage.
    problems="${problems}onion=$ocode(state-unwritable); "
    echo "onion=$ocode but OFAIL_STATE unwritable -> alerting undamped (cannot trust streak)"
  elif [ "$streak" -ge "$ONION_FAIL_STREAK" ]; then
    # The streak stays OUT of the string: `problems` IS the state, and alerts fire when the state
    # CHANGES. Embedding a counter that grows every run would make the state differ every cycle and
    # re-alert every 5 min for the whole outage. It goes to the journal instead.
    problems="${problems}onion=$ocode; "
    echo "onion=$ocode for $streak consecutive runs -> alerting"
  else
    echo "onion=$ocode, streak=$streak/$ONION_FAIL_STREAK — transient, not alerting yet"
  fi
fi

if [ -n "$problems" ]; then new="FAIL: $problems"; else new="OK"; fi
old=$(cat "$STATE" 2>/dev/null || echo "")
echo "$(date -u +%FT%TZ) $new"

# -- sending to Telegram (shared helper; --data-urlencode for the sake of multiline+emoji) --
# Exit codes are load-bearing for the callers below:
#   0 = delivered   1 = send ATTEMPTED and failed   2 = channel not configured (dormant, owes nothing)
# 1 vs 2 must stay distinct: a dormant probe has no undelivered alert to retry, a failed send does.
tg_send() {
  local text="$1" label
  label=$(printf '%s' "$text" | head -1)
  if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
    echo "[channel not configured (TELEGRAM_* empty) — log only] $label" >&2
    return 2
  fi
  curl -fsS --max-time 15 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -d chat_id="${TELEGRAM_CHAT_ID}" --data-urlencode "text=$text" >/dev/null 2>&1 \
    && { echo "[sent] $label"; return 0; } \
    || { echo "[send FAILED] $label" >&2; return 1; }
}

send_failed=0   # "an alert we owed the operator did not get through" — drives the exit code at the end

# 1) ALERT on a state CHANGE (broke / got fixed)
#
# 🔴 STATE is persisted ONLY after the alert is actually delivered. It used to be written
# unconditionally BEFORE the send, which armed a silent trap: a 15s Telegram blip (seen live on
# 2026-08-04) would swallow the 🔴 forever — the file already read FAIL, so every following run saw
# new==old and stayed silent for the WHOLE outage. Keeping the old state on a failed send turns the
# 5-min timer into the retry: the next run sees the change again and re-sends. Same reasoning as the
# heartbeat's HB_STATE below. Dormant channel (rc=2) owes nothing -> persist, or a dormant probe
# would re-log its state change every 5 minutes forever.
if [ "$new" != "$old" ]; then
  # ru-ok:begin operator-facing Telegram text — the operator reads these alerts, CLAUDE.md keeps bot texts RU
  if [ "$new" = "OK" ]; then tg_send "✅ privacyvpn: восстановлено — OK"
  else tg_send "🔴 privacyvpn: СБОЙ — $problems"; fi
  # ru-ok:end
  case $? in
    1) send_failed=1
       echo "state change NOT persisted (send failed) -> next run re-alerts" >&2 ;;
    *) printf '%s' "$new" > "$STATE" ;;
  esac
else
  printf '%s' "$new" > "$STATE"
fi

# 2) DAILY HEARTBEAT (dead-man's switch): once a day after HB_HOUR (UTC)
if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
  HB_STATE="${HB_STATE:-/var/lib/privacyvpn-monitor/heartbeat-date}"
  HB_HOUR="${HB_HOUR:-12}"
  today=$(date -u +%F)
  hb_last=$(cat "$HB_STATE" 2>/dev/null || echo "")
  if [ "$today" != "$hb_last" ] && [ "$((10#$(date -u +%H)))" -ge "$HB_HOUR" ]; then
    # ru-ok:begin operator-facing Telegram text — the operator reads these alerts, CLAUDE.md keeps bot texts RU (node summary is pasted into the heartbeat message)
    nodes=$(cd "$COMPOSE_DIR" && timeout 20 docker compose exec -T control-plane python -c '
from app.db import SessionLocal
from app.models import Node
from app.models.base import utcnow
with SessionLocal() as s:
    rows = s.query(Node).all()
    if not rows:
        print("  (нод нет)")
    for n in rows:
        st = getattr(n.status, "name", str(n.status)).lower()
        if n.last_health_at:
            age = int((utcnow() - n.last_health_at).total_seconds())
            print(f"  {n.name}: {st}, health {age}s назад")
        else:
            print(f"  {n.name}: {st}, health нет")
' 2>/dev/null)
    [ -n "$nodes" ] || nodes="  (сводка нод недоступна)"
    if [ -z "$problems" ]; then hb_head="🟢 privacyvpn daily — всё ОК"
    else hb_head="🔴 privacyvpn daily — ЕСТЬ ПРОБЛЕМЫ: $problems"; fi
    hb_msg="$hb_head
healthz: $code
onion: $ocode
Ноды:
$nodes
$(date -u +'%d %b %Y %H:%M UTC')"
    # ru-ok:end
    tg_send "$hb_msg"
    # Same persist-after-delivery rule as the state alert: an undelivered heartbeat must stay owed,
    # so the next run re-sends it. That self-retry already works — on 2026-08-04 the 15:02 send timed
    # out and the 15:07 run delivered it. What did NOT work was the exit code; see below.
    case $? in
      1) send_failed=1 ;;
      *) printf '%s' "$today" > "$HB_STATE" ;;
    esac
  fi
fi

# 3) SALES — "a purchase was paid" / "a gift code was redeemed", into the same channel
#
# Not a health probe: nothing here can go red, and nothing here touches `problems`. It rides in this
# script because the Telegram channel already lives on this host (/etc/privacyvpn-monitor.env) —
# notifying from control-plane would need two new CP_* variables, and the compose passthrough for a
# new CP_* is exactly what silently does not arrive.
#
# 🔴 There is no "new since <timestamp>" to query, and that is by design, not an oversight:
#   - gift_token.redeemed_at is COARSENED TO THE DAY (an exact instant would point at the account
#     minted in that second), so it cannot serve as a watermark;
#   - invoice.paid_at is NULLED by scrub_invoice_linkage, and invoice_link_retention_minutes is 0 —
#     the scheduler wipes it within a minute of payment, well inside this timer's 5-minute period.
# So the state is a SET of already-reported ids, and the query returns only the last 2 days (both
# `created_at` and `redeemed_at` survive the scrub, coarsened to the day) — which keeps the file
# bounded: anything that leaves the window was reported days ago. Honest limit: if this monitor is
# dead for more than 2 days, the sales that happened meanwhile are never announced (the money is in
# the admin console regardless). That is the deliberate trade against a state file that grows forever.
#
# What is sent carries NO account data: the invoice↔account link is severed by the scrub, and the
# gift token never had one. Plan, days, amount, note — the same figures the admin console shows.
SALES_STATE="${SALES_STATE:-/var/lib/privacyvpn-monitor/sales-seen}"
if [ "${SALES_NOTIFY:-1}" = "1" ] && [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
  # ru-ok:begin operator-facing Telegram text — the operator reads these, CLAUDE.md keeps bot texts RU
  # 🔴 SINGLE quotes around the python, same trap as the node query above: inside "..." bash would
  # strip the inner double quotes and every string literal here would become a NameError.
  sales=$(cd "$COMPOSE_DIR" && timeout 20 docker compose exec -T control-plane python -c '
from datetime import timedelta

from sqlalchemy import select

from app.db import SessionLocal
from app.models import GiftToken, Invoice, PricePlan
from app.models.base import utcnow
from app.services.billing_fsm import ACTIVATING_STATUSES


def clean(s, n=120):
    # Collapses whitespace and kills the two characters that would break the wire format below:
    # "|" is the id/text separator, "\\" would be re-interpreted by printf %b on the bash side.
    s = " ".join((s or "").split()).replace("|", "/").replace("\\", "/")
    return s[:n]


now = utcnow()
since = now - timedelta(days=2)
out = []
with SessionLocal() as s:
    plans = {p.id: p.name for p in s.execute(select(PricePlan)).scalars()}
    by_code = {p.code: p.name for p in s.execute(select(PricePlan)).scalars()}
    # Status, never paid_at: the scrub nulls paid_at, so a fully paid invoice has paid_at IS NULL.
    rows = s.execute(
        select(Invoice).where(
            Invoice.status.in_(tuple(ACTIVATING_STATUSES)),
            Invoice.created_at >= since,
        )
    ).scalars()
    for inv in rows:
        plan = plans.get(inv.price_plan_id) or f"{inv.days_granted} дн."
        amount = f"{inv.amount_fiat_cents / 100:.2f} {inv.fiat_currency}"
        if inv.discount_percent:
            amount = f"{amount} (промокод -{inv.discount_percent}%)"
        out.append(
            f"sale:{inv.id}|💰 privacyvpn: продажа"
            f"\\nТариф: {clean(plan)}"
            f"\\nСрок: {inv.days_granted} дн."
            f"\\nСумма: {amount}"
        )
    gifts = s.execute(
        select(GiftToken).where(
            GiftToken.redeemed_at.is_not(None),
            GiftToken.redeemed_at >= since,
        )
    ).scalars()
    for g in gifts:
        plan = by_code.get(g.price_plan_code) or g.price_plan_code or "без тарифа"
        note = clean(g.note) or "нет"
        limit = g.device_limit if g.device_limit is not None else "по умолчанию"
        out.append(
            f"gift:{g.id}|🎁 privacyvpn: активирован подарочный код"
            f"\\nТариф: {clean(plan)}"
            f"\\nСрок: {g.days} дн., устройств: {limit}"
            f"\\nЗаметка: {note}"
        )
for line in sorted(out):
    print(line)
' 2>/dev/null)
  # ru-ok:end
  sales_rc=$?
  if [ "$sales_rc" -ne 0 ]; then
    # A wedged CP is already the healthz section's business; here it only means "no news this run".
    echo "sales query failed (rc=$sales_rc) — no notifications this run" >&2
  else
    seen=""
    while IFS='|' read -r ev_id ev_text; do
      [ -n "$ev_id" ] || continue
      if grep -qxF -- "$ev_id" "$SALES_STATE" 2>/dev/null; then
        seen="${seen}${ev_id}
"
        continue
      fi
      # Same persist-after-delivery rule as the alert and the heartbeat: an id is remembered only
      # once its message actually left, so a Telegram blip re-sends on the next run instead of
      # swallowing a sale for good.
      tg_send "$(printf '%b' "$ev_text")"
      case $? in
        1) send_failed=1
           echo "sale notification NOT persisted (send failed) -> next run retries" >&2 ;;
        *) seen="${seen}${ev_id}
" ;;
      esac
    done <<< "$sales"
    # Rewritten wholesale: ids that fell out of the 2-day window prune themselves here.
    printf '%s' "$seen" > "$SALES_STATE" 2>/dev/null \
      || echo "SALES_STATE unwritable -> already-sent sales may be announced again next run" >&2
  fi
fi

# -- exit code ---------------------------------------------------------------------------------
# A failed unit is the dead-man's switch: OnFailure= fires monitor-failed.sh, whose alert says
# "the monitor did not run — its silence is NOT 'all fine'". That must mean the probe could not do
# its job, NOT that one Telegram POST blipped. On 2026-08-04 every probe was green (healthz 200,
# onion 200 in 2s, nodes fine), the daily heartbeat's send timed out once at 15s, the unit exited 1,
# and the operator got the loudest alert in the system about nothing — while the heartbeat quietly
# delivered itself 5 minutes later. False alarms on THAT message are expensive: it is the one alert
# that must never be trained into background noise.
# So: swallow short blips (the unsent alert is already owed and retried by the next run), and fail
# the unit only once sending has been broken for SEND_FAIL_STREAK runs in a row (~15 min) — long
# enough to suspect the channel rather than the network. Honest limit: monitor-failed.sh shouts
# THROUGH the same Telegram, so if the channel is genuinely down its alert cannot arrive either;
# the failed unit then survives in `systemctl status` and the journal as the durable trace.
SFAIL_STATE="${SFAIL_STATE:-/var/lib/privacyvpn-monitor/send-fail-streak}"
SEND_FAIL_STREAK="${SEND_FAIL_STREAK:-3}"
if [ "$send_failed" = 0 ]; then
  printf '0' > "$SFAIL_STATE" 2>/dev/null
  exit 0
fi
sstreak=$(cat "$SFAIL_STATE" 2>/dev/null || echo 0)
case "$sstreak" in (*[!0-9]*|"") sstreak=0 ;; esac
sstreak=$((sstreak + 1))
# Unwritable state dir (disk full / RO /var — correlated with real incidents) would pin the streak at
# 1 and swallow the failure forever. Can't damp -> don't swallow, same rule as OFAIL_STATE above.
if ! printf '%s' "$sstreak" > "$SFAIL_STATE" 2>/dev/null; then
  echo "telegram send failed and SFAIL_STATE unwritable -> cannot damp, failing the unit" >&2
  exit 1
fi
if [ "$sstreak" -ge "$SEND_FAIL_STREAK" ]; then
  echo "telegram send failing for $sstreak consecutive runs -> failing the unit" >&2
  exit 1
fi
echo "telegram send failed, streak=$sstreak/$SEND_FAIL_STREAK — transient, unit stays green (alert still owed, retried next run)"
exit 0
