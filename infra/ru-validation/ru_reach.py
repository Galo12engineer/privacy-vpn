#!/usr/bin/env python
"""Infra reachability of the Reality node from RU — WITHOUT a live client (check-host.net).

Checks the NETWORK layer only: is the node's TCP :443 reachable from Russian check-host.net
nodes. Catches blocking by IP / subnet / ASN and "port closed". Does NOT catch behavioural
TSPU DPI (active Reality probing, ~16KB threshold, throttling after a session) — that is only
visible to a real client (scripts/ru_smoke.py via an RU proxy and, finally, a live RU tester).

    python infra/ru-validation/ru_reach.py <node-IP> [--port 443] [--nodes 25]

Data goes to the public check-host.net (the node IP is not a secret, it is in the vless:// link anyway).
No registration/key. RIPE Atlas (more precise, but needs an account + credits) — commands at the bottom of the output.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request

API = "https://check-host.net"


def _get(path: str, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(API + path, headers={"Accept": "application/json", "User-Agent": "curl/8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def main() -> int:
    ap = argparse.ArgumentParser(description="Reachability of a node's :443 from RU via check-host.net")
    ap.add_argument("ip", help="public IP of the Reality node")
    ap.add_argument("--port", type=int, default=443)
    ap.add_argument("--nodes", type=int, default=25, help="how many check-host probes to use")
    args = ap.parse_args()

    target = f"{args.ip}:{args.port}"
    print(f"[*] check-host.net TCP-connect -> {target} (RU probes)...")
    try:
        started = _get(f"/check-tcp?host={target}&max_nodes={args.nodes}")
    except Exception as e:  # noqa: BLE001
        print(f"[!] could not start the check: {type(e).__name__}: {e}")
        return 2
    rid = started.get("request_id")
    node_meta = started.get("nodes", {})
    if not rid:
        print(f"[!] check-host returned no request_id: {started}")
        return 2

    # RU nodes: metadata of the form ["Russia","Moscow", ...] in nodes[node]
    ru_nodes = {n for n, meta in node_meta.items() if isinstance(meta, list) and meta and meta[0] == "Russia"}
    if not ru_nodes:
        print("[!] check-host returned no RU probes this time — retry later.")
        return 2
    print(f"[*] RU probes in the sample: {len(ru_nodes)}. Waiting for results...")

    # the result is filled in asynchronously — poll for up to ~30 s
    results: dict = {}
    for _ in range(15):
        time.sleep(2)
        try:
            results = _get(f"/check-result/{rid}")
        except Exception:  # noqa: BLE001
            continue
        done = [n for n in ru_nodes if results.get(n) not in (None, [None])]
        if len(done) >= len(ru_nodes):
            break

    ok = bad = pending = 0
    print("\n    probe (city)               result")
    print("    " + "-" * 46)
    for n in sorted(ru_nodes):
        meta = node_meta.get(n, [])
        city = meta[1] if len(meta) > 1 else "?"
        res = results.get(n)
        # TCP format: [{"time":0.12,"address":"1.2.3.4"}] on success, [{"error":"..."}] on failure
        if res in (None, [None]):
            verdict, pending = "... (no answer)", pending + 1
        elif isinstance(res, list) and res and isinstance(res[0], dict) and "error" not in res[0]:
            t = res[0].get("time")
            verdict, ok = f"OK  {float(t) * 1000:.0f}ms" if t else "OK", ok + 1
        else:
            err = res[0].get("error") if isinstance(res, list) and res and isinstance(res[0], dict) else res
            verdict, bad = f"BLOCKED/timeout ({err})", bad + 1
        print(f"    {n:<26} {city:<12} {verdict}")

    total = len(ru_nodes)
    print(f"\n[=] RU probes: OK={ok}  BLOCKED/timeout={bad}  no answer={pending}  of {total}")
    print(f"    details: {API}/check-report/{rid}")
    if ok and not bad:
        print("[+] Port 443 is reachable from RU (IP/ASN not blocklisted). The infra layer is clean.")
        print("    This does NOT mean DPI lets Reality through — that needs ru_smoke.py + a live tester.")
        rc = 0
    elif ok and bad:
        print("[~] Mixed: some RU probes cannot reach it. Could be a targeted ASN block or probe flap.")
        rc = 1
    else:
        print("[!] The port is NOT reachable from RU although the IP is up. Looks like an IP/subnet/ASN block —")
        print("    change the hosting/ASN; the transport (Reality) is not the problem here.")
        rc = 1

    print("\n# RIPE Atlas (more precise; needs an account + credits):")
    print(f"#   ripe-atlas measure sslcert --target {args.ip} --port {args.port} --probes 20 --from-country ru")
    print(f"#   ripe-atlas measure tcp     --target {args.ip} --port {args.port} --probes 20 --from-country ru")
    print("#   ASN: Rostelecom AS12389 - MTS AS8359 - Beeline AS3216 - Megafon AS3113")
    return rc


if __name__ == "__main__":
    sys.exit(main())
