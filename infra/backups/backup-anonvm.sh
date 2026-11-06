#!/usr/bin/env bash
# Auto-backup of the control-plane ON THE anonvm HOST (docker-compose deployment). Unlike backup.sh
# (which expects pg_dump + a direct CP_DATABASE_URL) — here there is NO pg_dump on the host, and `db`
# resolves only inside the docker network, so the dump goes THROUGH the db container, while age
# encryption happens on the host.
#
# The plaintext dump lives only in /dev/shm (RAM, tmpfs) and is shredded on exit — it NEVER lands on
# disk in the clear. Only the age ciphertext is written to disk.
#
# Env:
#   BACKUP_AGE_RECIPIENT  age recipient (age1...). The PRIVATE identity — OFFLINE at the operator ONLY.
#   COMPOSE_DIR           compose directory (default /opt/privacyvpn)
#   BACKUP_DIR            where to put the ciphertext (default $COMPOSE_DIR/backups-out)
#   BACKUP_KEEP           how many recent ones to keep (default 14)
#   POSTGRES_USER/DB      default vpn/vpn
set -euo pipefail

: "${BACKUP_AGE_RECIPIENT:?set BACKUP_AGE_RECIPIENT (the PUBLIC age1... recipient)}"
COMPOSE_DIR="${COMPOSE_DIR:-/opt/privacyvpn}"
BACKUP_DIR="${BACKUP_DIR:-$COMPOSE_DIR/backups-out}"
KEEP="${BACKUP_KEEP:-14}"
PGUSER="${POSTGRES_USER:-vpn}"
PGDB="${POSTGRES_DB:-vpn}"

umask 077
mkdir -p "$BACKUP_DIR"
ts="$(date -u +%Y%m%dT%H%M%SZ)"
out="$BACKUP_DIR/cp-$ts.sql.age"

# plaintext in RAM only; we shred it on any exit
tmp="$(mktemp -p /dev/shm cp-dump.XXXXXX.sql)"
trap 'shred -u "$tmp" 2>/dev/null || rm -f "$tmp"' EXIT

cd "$COMPOSE_DIR"
echo "[*] dump via db container"
docker compose exec -T db pg_dump -U "$PGUSER" -d "$PGDB" > "$tmp"

# integrity: pg_dump writes a completion marker at the very end
if ! grep -q "PostgreSQL database dump complete" "$tmp"; then
  echo "[!] dump is incomplete (no completion marker) — not encrypting, bailing out"; exit 1
fi

echo "[*] encrypt (age) — the plaintext dump never touches the disk"
age -r "$BACKUP_AGE_RECIPIENT" -o "$out" "$tmp"
[ -s "$out" ] || { echo "[!] empty ciphertext"; rm -f "$out"; exit 1; }
echo "[+] backup: $out ($(stat -c%s "$out") bytes)"

echo "[*] rotate (keeping the last $KEEP)"
ls -1t "$BACKUP_DIR"/cp-*.sql.age 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f

# ⚠️ Offsite (rclone) — a separate operator step: a local copy on the same VPS does not save you
# if the VPS dies. Configure BACKUP_REMOTE + rclone OR periodically pull backups-out/ to yourself.
if [ -n "${BACKUP_REMOTE:-}" ] && command -v rclone >/dev/null 2>&1; then
  rclone copy "$out" "$BACKUP_REMOTE/" && echo "[+] offsite → $BACKUP_REMOTE"
fi
echo "[+] done"
