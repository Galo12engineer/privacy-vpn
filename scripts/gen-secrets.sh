#!/usr/bin/env bash
# Prints strong secret values for .env. Does NOT overwrite .env — copy them across by hand.
#   ./scripts/gen-secrets.sh
set -euo pipefail

rand() { head -c "$1" /dev/urandom | base64 | tr -d '/+=\n' | head -c "$1"; }

echo "# --- generated secrets (paste into .env) ---"
echo "POSTGRES_PASSWORD=$(rand 32)"
echo "CP_ACCOUNT_NUMBER_PEPPER=$(rand 48)"
echo "CP_SESSION_SECRET=$(rand 48)"
echo "CP_BOOTSTRAP_ADMIN_PASSWORD=$(rand 32)"

# Fernet key for email/TOTP (needs Python with cryptography; otherwise leave it empty)
if python3 -c "import cryptography" >/dev/null 2>&1; then
  echo "CP_EMAIL_ENCRYPTION_KEY=$(python3 -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())')"
else
  echo "# CP_EMAIL_ENCRYPTION_KEY=  (no cryptography here — generate it inside the container:"
  echo "#   docker compose run --rm control-plane python -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())')"
fi

echo "# WARNING changing CP_ACCOUNT_NUMBER_PEPPER breaks every account-number already issued — only before launch."
