#!/usr/bin/env bash
# Check an IP (or several) against Spamhaus before renting a VPS -- and periodically afterwards.
#
# WHY THIS EXISTS: on 2026-08-05 the control-plane IP 203.0.113.21 turned out to sit in Spamhaus
# DROP ("Don't Route Or Peer"), and not because of anything we did -- the whole /24 was listed under
# SBL695849, and the entire announcing AS209274 was in ASN-DROP. Networks that import those feeds
# null-route the range, so an unknown share of visitors simply cannot reach the site: no error, no
# log, nothing on our side to see. Delisting was not an option, because the listing belongs to the
# provider's network, not to us. Three DNS lookups before paying would have caught it.
#
# The exit node IPs matter too, for a different reason: a listed exit IP gets our customers CAPTCHA
# walls and silent blocks on ordinary sites, which reads to them as "this VPN is broken".
#
# Checks performed, cheapest first:
#   1. zen.spamhaus.org  -- the combined DNSBL (SBL / CSS / XBL / PBL), per-IP
#   2. DROP (drop_v4.json)   -- is the covering CIDR flagged "do not route"
#   3. ASN-DROP (asndrop.json) -- is the whole announcing AS flagged
#   4. RIPEstat + RIPE DB   -- who actually owns the range, i.e. who would have to fix it
#
# Steps 2-4 are what make this more than a mail-reputation check: an IP can pass the DNSBL and still
# live in an AS nobody routes.
#
# ⚠️ OPSEC: this makes outbound queries to spamhaus.org, ripe.net and ripestat, and those queries
# name the IP you care about. Run it from an operator machine, not from the anonymous VPS itself,
# if linking "who asks about this IP" to the IP matters to you.
#
# Usage:
#   bash scripts/check_ip_reputation.sh 203.0.113.10 [more IPs...]
# Exit status: 0 = everything clean, 1 = at least one IP is listed, 2 = usage/dependency error.

set -uo pipefail

if [ $# -eq 0 ]; then
	echo "usage: $0 <ip> [ip...]" >&2
	exit 2
fi
for dep in dig curl python3; do
	command -v "$dep" >/dev/null 2>&1 || { echo "missing dependency: $dep" >&2; exit 2; }
done

DROP_URL="https://www.spamhaus.org/drop/drop_v4.json"
ASNDROP_URL="https://www.spamhaus.org/drop/asndrop.json"
CACHE="${TMPDIR:-/tmp}/tt-spamhaus-$(date -u +%Y%m%d)"
mkdir -p "$CACHE"

fetch() { # url, dest -- cached for the day so repeated runs do not hammer Spamhaus
	[ -s "$2" ] || curl -fsS --max-time 30 "$1" -o "$2" 2>/dev/null
	[ -s "$2" ]
}
fetch "$DROP_URL" "$CACHE/drop_v4.json" || echo "  (warning: DROP list unavailable — step 2 skipped)"
fetch "$ASNDROP_URL" "$CACHE/asndrop.json" || echo "  (warning: ASN-DROP unavailable — step 3 skipped)"

# zen return codes -> human meaning. 127.0.0.9 is the one that null-routes traffic.
zen_meaning() {
	case "$1" in
		127.0.0.2)  echo "SBL — Spamhaus Block List (range believed spammer/criminal-controlled)" ;;
		127.0.0.3)  echo "SBL CSS — snowshoe/low-reputation listing" ;;
		127.0.0.4|127.0.0.5|127.0.0.6|127.0.0.7) echo "XBL — compromised host (bot/proxy/worm)" ;;
		127.0.0.9)  echo "SBL DROP/EDROP — 'Don't Route Or Peer': networks may null-route the range" ;;
		127.0.0.10|127.0.0.11) echo "PBL — end-user space, not expected to send mail directly" ;;
		127.255.255.*) echo "query error (public resolver blocked by Spamhaus — try another resolver)" ;;
		*) echo "unknown code" ;;
	esac
}

rc=0
for ip in "$@"; do
	echo "══════════ $ip ══════════"

	rev=$(echo "$ip" | awk -F. '{print $4"."$3"."$2"."$1}')
	codes=$(dig +short "$rev.zen.spamhaus.org" A 2>/dev/null | sort -u)
	if [ -z "$codes" ]; then
		echo "  [1] zen.spamhaus.org : not listed"
	else
		rc=1
		echo "  [1] zen.spamhaus.org : LISTED"
		for c in $codes; do printf '        %-14s %s\n' "$c" "$(zen_meaning "$c")"; done
		txt=$(dig +short "$rev.zen.spamhaus.org" TXT 2>/dev/null | head -2)
		[ -n "$txt" ] && echo "$txt" | sed 's/^/        /'
	fi

	# announcing AS + covering prefix, straight from RIPEstat
	ov=$(curl -fsS --max-time 25 \
		"https://stat.ripe.net/data/prefix-overview/data.json?resource=$ip" 2>/dev/null)
	asn=$(printf '%s' "$ov" | python3 -c \
		"import json,sys;d=json.load(sys.stdin)['data'];a=d.get('asns') or [{}];print(a[0].get('asn',''))" 2>/dev/null)
	holder=$(printf '%s' "$ov" | python3 -c \
		"import json,sys;d=json.load(sys.stdin)['data'];a=d.get('asns') or [{}];print(a[0].get('holder',''))" 2>/dev/null)
	prefix=$(printf '%s' "$ov" | python3 -c \
		"import json,sys;print(json.load(sys.stdin)['data'].get('resource',''))" 2>/dev/null)

	if [ -s "$CACHE/drop_v4.json" ]; then
		hit=$(python3 - "$ip" "$CACHE/drop_v4.json" <<-'PY'
			import ipaddress, json, sys
			ip = ipaddress.ip_address(sys.argv[1])
			for line in open(sys.argv[2], encoding="utf-8"):
			    line = line.strip().rstrip(",")
			    if not line.startswith("{"):
			        continue
			    try:
			        e = json.loads(line)
			    except ValueError:
			        continue
			    cidr = e.get("cidr")
			    if cidr and ip in ipaddress.ip_network(cidr):
			        print(f"{cidr} ({e.get('sblid', '?')})")
		PY
		)
		if [ -n "$hit" ]; then
			rc=1
			echo "  [2] DROP list        : LISTED — $hit"
			echo "        this is the severe one: DROP is meant to be null-routed by operators"
		else
			echo "  [2] DROP list        : not listed"
		fi
	fi

	if [ -n "$asn" ] && [ -s "$CACHE/asndrop.json" ]; then
		if grep -q "\"asn\":$asn\b" "$CACHE/asndrop.json" 2>/dev/null; then
			rc=1
			echo "  [3] ASN-DROP         : AS$asn IS LISTED — the whole autonomous system"
			echo "        delisting a single range is hopeless while the AS itself is flagged"
		else
			echo "  [3] ASN-DROP         : AS$asn not listed"
		fi
	fi

	echo "  [4] ownership        : ${prefix:-?} — AS${asn:-?} ${holder:-}"
	own=$(curl -fsS --max-time 25 \
		"https://rest.db.ripe.net/search.json?query-string=$ip&flags=no-referenced" 2>/dev/null |
		python3 -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: sys.exit()
want=('netname','country','abuse-c','org')
for o in d.get('objects',{}).get('object',[]):
    if o.get('type')!='inetnum': continue
    for a in o.get('attributes',{}).get('attribute',[]):
        if a['name'] in want: print(f\"        {a['name']}: {a['value']}\")
" 2>/dev/null)
	[ -n "$own" ] && echo "$own"
	echo
done

if [ $rc -eq 0 ]; then
	echo "RESULT: all clean"
else
	echo "RESULT: at least one IP is listed — see above."
	echo "A DROP or ASN-DROP hit belongs to the network owner, not to you: you cannot get it lifted."
	echo "For a prospective VPS that means: do not rent it. For one already in use: plan a move."
fi
exit $rc
