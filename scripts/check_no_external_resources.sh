#!/usr/bin/env bash
# CI check for the frontend's .onion cleanliness.
# The site must work over .onion (Tor): no external fonts, no CDN, no external
# styles/scripts/images — otherwise resources leak and fail to load.
# Ordinary <a href="https://…"> links are fine (the user clicks them, they are not
# fetched automatically), so we only look at resource loads.
set -euo pipefail
cd "$(dirname "$0")/.."

targets="web/templates web/static"

# Patterns that auto-fetch an external resource (NOT ordinary <a href>):
pattern='fonts\.(googleapis|gstatic)|@font-face|@import|url\(\s*["'"'"']?https?:|<link[^>]*href=["'"'"']https?:|<script[^>]*src=["'"'"']https?:|<img[^>]*src=["'"'"']https?:'

hits=$(grep -rInE "$pattern" $targets --include='*.html' --include='*.css' 2>/dev/null || true)

if [ -n "$hits" ]; then
  echo "FAIL: the frontend pulls external resources — this breaks it over .onion:"
  echo "$hits"
  exit 1
fi
echo "OK: no external resources in the frontend (.onion-safe; system fonts)"
