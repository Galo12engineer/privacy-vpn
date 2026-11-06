#!/usr/bin/env bash
# Encrypted offsite backup of the control-plane (accounts+billing). Losing the CP = losing all
# account-number values (there is no recovery) → a backup is mandatory and MUST be encrypted.
#
# Env:
#   CP_DATABASE_URL        postgresql://...  (or a path to sqlite)
#   BACKUP_AGE_RECIPIENT   age recipient (age1...) OR
#   BACKUP_GPG_RECIPIENT   gpg key id/email
#   BACKUP_DIR             local folder (default ./backups-out)
#   BACKUP_REMOTE          rclone remote, e.g. "offsite:vpn-backups" (optional)
set -euo pipefail

: "${CP_DATABASE_URL:?CP_DATABASE_URL required}"
BACKUP_DIR="${BACKUP_DIR:-./backups-out}"
mkdir -p "$BACKUP_DIR"
ts="$(date -u +%Y%m%dT%H%M%SZ)"
dump="$BACKUP_DIR/cp-$ts.sql"

echo "[*] dump"
if [[ "$CP_DATABASE_URL" == postgresql* || "$CP_DATABASE_URL" == postgres* ]]; then
  pg_dump "$CP_DATABASE_URL" > "$dump"
elif [[ "$CP_DATABASE_URL" == sqlite* ]]; then
  db="${CP_DATABASE_URL##*:///}"; db="${db%%\?*}"
  sqlite3 "$db" .dump > "$dump"
else
  echo "unsupported CP_DATABASE_URL"; exit 1
fi

echo "[*] encrypt (a backup is NEVER kept in the clear)"
enc="$dump.age"
if [[ -n "${BACKUP_AGE_RECIPIENT:-}" ]]; then
  age -r "$BACKUP_AGE_RECIPIENT" -o "$enc" "$dump"
elif [[ -n "${BACKUP_GPG_RECIPIENT:-}" ]]; then
  enc="$dump.gpg"
  gpg --yes --encrypt --recipient "$BACKUP_GPG_RECIPIENT" --output "$enc" "$dump"
else
  echo "set BACKUP_AGE_RECIPIENT or BACKUP_GPG_RECIPIENT"; rm -f "$dump"; exit 1
fi
shred -u "$dump" 2>/dev/null || rm -f "$dump"   # remove the plaintext dump

echo "[*] offsite"
if [[ -n "${BACKUP_REMOTE:-}" ]]; then
  rclone copy "$enc" "$BACKUP_REMOTE/" && echo "[+] uploaded to $BACKUP_REMOTE"
fi
echo "[+] backup ready: $enc"
