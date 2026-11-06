#!/usr/bin/env bash
# Restore test: decrypt the latest backup → load it into a scratch DB → verify it is actually usable.
# A backup without a verified restore is not a backup.
#
# Env: BACKUP_FILE (.age / .gpg / plain .sql), BACKUP_AGE_IDENTITY or an accessible GPG key,
#      SCRATCH_DATABASE_URL (postgresql://... an EMPTY database — this script does not create it)
#
# Two defects lived here from the day it was written until 2026-07-21, and both had the same shape:
# the script would report success on a broken restore.
#   1. psql without ON_ERROR_STOP walks past a failing statement and still exits 0 — half a schema
#      restored, "[+] restore OK" printed. You would find out during the disaster.
#   2. the account count was printed but never asserted, so an EMPTY restore passed too — while
#      docs/disaster-recovery.md promised this script would "confirm the account table is not empty".
# Neither was visible by reading, because the script had never once been run. It now also accepts a
# plain .sql so the mechanics can be exercised WITHOUT the offline age identity — requiring that
# identity is exactly why it stayed unrun.
set -euo pipefail

: "${BACKUP_FILE:?BACKUP_FILE required}"
: "${SCRATCH_DATABASE_URL:?SCRATCH_DATABASE_URL required}"

tmp="$(mktemp -t restore-test.XXXXXX)"
cleanup() { shred -u "$tmp" 2>/dev/null || rm -f "$tmp"; }
trap cleanup EXIT

echo "[*] decrypt"
case "$BACKUP_FILE" in
  *.age) age -d -i "${BACKUP_AGE_IDENTITY:?BACKUP_AGE_IDENTITY required for .age}" -o "$tmp" "$BACKUP_FILE" ;;
  *.gpg) gpg --yes --decrypt --output "$tmp" "$BACKUP_FILE" ;;
  *.sql) cp "$BACKUP_FILE" "$tmp" ;;
  *) echo "[!] unknown backup format: $BACKUP_FILE" >&2; exit 1 ;;
esac
test -s "$tmp" || { echo "[!] decrypted dump is empty" >&2; exit 1; }

echo "[*] restore into scratch db"
# ON_ERROR_STOP: without it psql continues past a broken statement and exits 0.
psql -v ON_ERROR_STOP=1 --quiet "$SCRATCH_DATABASE_URL" < "$tmp" >/dev/null

echo "[*] verify"
q() { psql -tAc "$1" "$SCRATCH_DATABASE_URL"; }

# The schema version proves the dump was not truncated before the tail of the file.
schema="$(q 'select version_num from alembic_version')"
test -n "$schema" || { echo "[!] alembic_version is empty — dump truncated?" >&2; exit 1; }

accounts="$(q 'select count(*) from account')"
plans="$(q 'select count(*) from price_plan')"
admins="$(q 'select count(*) from admin_user')"

# An empty restore is the failure mode that looks most like success. `account` may legitimately be
# zero on a fresh deployment, but plans and an admin are what make the restored system a SERVICE
# rather than an empty schema — so those two are the ones asserted.
fail=0
[ "$plans"  -gt 0 ] || { echo "[!] price_plan is empty — that is a schema, not a service" >&2; fail=1; }
[ "$admins" -gt 0 ] || { echo "[!] admin_user is empty — nobody could log in to the restored panel" >&2; fail=1; }
[ "$fail" -eq 0 ] || exit 1

echo "[+] restore OK — alembic=$schema · accounts=$accounts · plans=$plans · admins=$admins"
