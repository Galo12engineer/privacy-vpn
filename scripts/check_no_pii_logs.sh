#!/usr/bin/env bash
# CI check for the no-logs policy: the reverse proxy must not log the client IP.
set -euo pipefail
cd "$(dirname "$0")/.."
fail=0

# nginx: the config must not put the client IP in the log (comments ignored)
if grep -vE '^[[:space:]]*#' infra/reverse-proxy/nginx.conf \
   | grep -nE '\$remote_addr|\$http_x_forwarded_for|\$proxy_add_x_forwarded_for'; then
  echo "FAIL: nginx logs the client IP"; fail=1
fi
# Caddy: must explicitly delete remote_ip from the log
if ! grep -q 'remote_ip delete' infra/reverse-proxy/Caddyfile; then
  echo "FAIL: Caddyfile does not delete remote_ip from the log"; fail=1
fi

# CP code must not log request.client directly
if grep -rnE 'log(ger)?\.(info|warning|error|debug)\(.*request\.client' control-plane/app 2>/dev/null; then
  echo "FAIL: the code logs request.client"; fail=1
fi

if [ "$fail" -eq 0 ]; then echo "OK: no client-IP in proxy/app logging policy"; fi
exit $fail
