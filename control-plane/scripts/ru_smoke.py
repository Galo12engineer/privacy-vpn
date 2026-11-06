#!/usr/bin/env python
"""Smoke test of the VLESS+Reality transport "as if from RU" — via an RU SOCKS5 proxy.

WHY. The operator is not in RU, and there is no live tester yet. The only way to run the
Reality handshake through a Russian network path (TSPU) is to wrap the CLIENT dial into a
residential/mobile RU SOCKS5. We build an Xray client with the chain:

    local socks (127.0.0.1) → outbound[vless-reality] → dialerProxy → outbound[socks5 RU]
                                                                          → TCP to node:443
                                                                          → Reality handshake goes ALONG the RU path

VLESS(+XHTTP) is TCP, carried by SOCKS5 CONNECT. AmneziaWG is UDP and is NOT testable via
SOCKS5: the script rejects such links.

⚠️ READ HONESTLY (docs/ru-validation.md §false-negatives):
  • Take ONLY a residential/mobile RU SOCKS5. A datacenter proxy lies (TSPU has a different
    policy toward DC subnets) — a "pass" on it means nothing.
  • PASS is encouraging but does NOT replace a live tester (§6.2.3): the proxy adds its own
    hop, its own drops and its own geography. FAIL is ambiguous (proxy or TSPU?) — check the logs.
  • This does NOT lift the roadmap gate "we don't sell until a live RU test has passed".

MODES:
    # 1) build the config and run it (an xray binary in PATH is required):
    python scripts/ru_smoke.py --link 'vless://...#nl-1' \
        --ru-socks socks5://user:pass@RU_HOST:1080 --expect-ip <node-WAN-IP> --minutes 8
    # 2) only build the config (no xray) — print it and hand it over manually:
    python scripts/ru_smoke.py --link 'vless://...' --ru-socks socks5://RU_HOST:1080 --config-only
    # 3) structural self-test (no network, no xray) — CI/local check of the builder:
    python scripts/ru_smoke.py --selftest

The Xray client logs (loglevel=warning) belong to the operator's LOCAL test box, not the node;
the node's no-log invariant is not affected here.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qs, unquote, urlparse

LOCAL_SOCKS_PORT = 10808  # local inbound of the test client
# "my external IP" echo services — simple, return a plain-text IP. Several of them in case one is blocked.
IP_ECHO_URLS = ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com")


class SmokeError(RuntimeError):
    pass


# --------------------------------------------------------------------------- parse

def parse_vless_link(link: str) -> dict:
    """vless://UUID@host:port?params#label → dict of fields. Only VLESS+Reality+XHTTP."""
    if not link.startswith("vless://"):
        raise SmokeError(
            "ожидается vless://-ссылка. AmneziaWG (UDP) через SOCKS5 не тестируется — пропусти её."
        )
    u = urlparse(link)
    if not u.username or not u.hostname or not u.port:
        raise SmokeError("в ссылке нет uuid@host:port")
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    if q.get("security") != "reality":
        raise SmokeError(f"security={q.get('security')!r}, ожидался reality")
    if q.get("type") not in (None, "xhttp"):
        # our transport is xhttp; a different type means a mismatch with the server (xray.py render_config)
        raise SmokeError(f"type={q.get('type')!r}, наш сервер — xhttp")
    for req in ("sni", "pbk"):
        if not q.get(req):
            raise SmokeError(f"в ссылке нет обязательного параметра {req}")
    return {
        "uuid": u.username,
        "host": u.hostname,
        "port": u.port,
        "sni": q["sni"],
        "pbk": q["pbk"],
        "sid": q.get("sid", ""),
        "fp": q.get("fp", "firefox"),
        "path": q.get("path", "/"),
        "flow": q.get("flow", ""),  # we have no flow (Vision is incompatible with XHTTP)
        "label": unquote(u.fragment) if u.fragment else "",
    }


def parse_socks(spec: str) -> dict:
    """socks5://[user:pass@]host:port or host:port → {address,port,user,pass}."""
    s = spec if "://" in spec else "socks5://" + spec
    u = urlparse(s)
    if u.scheme not in ("socks5", "socks", "socks5h"):
        raise SmokeError(f"РФ-прокси должен быть socks5, а не {u.scheme!r}")
    if not u.hostname or not u.port:
        raise SmokeError("РФ-SOCKS5 в формате [user:pass@]host:port")
    out = {"address": u.hostname, "port": u.port}
    if u.username:
        out["user"] = unquote(u.username)
        out["pass"] = unquote(u.password or "")
    return out


# --------------------------------------------------------------------------- build

def build_client_config(link_fields: dict, socks: dict, *, local_port: int = LOCAL_SOCKS_PORT) -> dict:
    """Xray client config: a vless-reality outbound with dialerProxy onto the socks5 RU outbound.

    Mirrors the server inbound (node-agent/agent/xray.py render_config): network=xhttp,
    security=reality, realitySettings{serverName,publicKey,shortId}, xhttpSettings{path,auto}.
    """
    lf = link_fields
    reality = {
        "show": False,
        "fingerprint": lf["fp"],
        "serverName": lf["sni"],
        "publicKey": lf["pbk"],
        "shortId": lf["sid"],
        "spiderX": "/",
    }
    user = {"id": lf["uuid"], "encryption": "none", "level": 0}
    if lf["flow"]:  # usually empty; if the server unexpectedly has flow — pass it through to match
        user["flow"] = lf["flow"]

    ru_out = {
        "tag": "ru-socks",
        "protocol": "socks",
        "settings": {"servers": [{"address": socks["address"], "port": socks["port"]}]},
    }
    if "user" in socks:
        ru_out["settings"]["servers"][0]["users"] = [{"user": socks["user"], "pass": socks["pass"]}]

    vless_out = {
        "tag": "proxy",
        "protocol": "vless",
        "settings": {"vnext": [{"address": lf["host"], "port": lf["port"], "users": [user]}]},
        "streamSettings": {
            "network": "xhttp",
            "security": "reality",
            "realitySettings": reality,
            "xhttpSettings": {"path": lf["path"], "mode": "auto"},
            # KEY POINT: the vless outbound dials THROUGH ru-socks → the handshake goes along the RU path.
            "sockopt": {"dialerProxy": "ru-socks"},
        },
    }

    return {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {
                "tag": "in",
                "listen": "127.0.0.1",
                "port": local_port,
                "protocol": "socks",
                "settings": {"udp": False, "auth": "noauth"},
            }
        ],
        # proxy is the default (first) outbound: all traffic from the local inbound goes into it,
        # and it dials out through ru-socks. ru-socks itself is NOT the default — it is only the dialer.
        "outbounds": [vless_out, ru_out, {"protocol": "freedom", "tag": "direct"}],
    }


# --------------------------------------------------------------------------- run

def _http_get_via_socks(url: str, port: int, timeout: float = 12.0) -> str:
    """GET through the local SOCKS5 (test client) using curl.

    IMPORTANT: stdlib urllib does NOT support socks5 (only http/https/ftp proxies), so we go
    through `curl --socks5-hostname` — it exists on any Linux/macOS and does remote DNS
    on the proxy (socks5h), exactly as our xray socks inbound expects.
    """
    proxy = f"127.0.0.1:{port}"
    out = subprocess.run(
        ["curl", "-fsS", "--max-time", str(int(timeout)), "--socks5-hostname", proxy,
         "-A", "curl/8", url],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise SmokeError(f"curl через цепочку не прошёл (код {out.returncode}): {out.stderr.strip()[:120]}")
    return out.stdout.strip()


def _observed_exit_ip(port: int) -> str | None:
    for url in IP_ECHO_URLS:
        try:
            ip = _http_get_via_socks(url, port).split()[0]
            if ip and all(c in "0123456789.:abcdefABCDEF" for c in ip):
                return ip
        except Exception:  # noqa: BLE001 — the echo/chain may be down; try the next echo
            continue
    return None


# POST echo endpoints (they reflect the body/size) — to check the UPLOAD direction
UPLOAD_ECHO_URLS = ("https://postman-echo.com/post", "https://httpbin.org/post")


def _upload_probe(port: int, kb: int, timeout: float = 40.0) -> tuple[bool, str]:
    """POST kb KB through the chain → catches "the upload hangs" (Telegram/calls/file uploads).

    It is exactly this traffic that breaks XHTTP mode=auto in sing-box clients: downloading
    works, while upload/bidir stalls. Here the client is xray-core (resolves auto→stream-one),
    so a failure here = a problem of the SERVER/transport/link, not of the client.
    Returns (ok, info)."""
    payload = b"0" * (kb * 1024)
    proxy = f"127.0.0.1:{port}"
    last = "ни один upload-echo не ответил"
    for url in UPLOAD_ECHO_URLS:
        r = subprocess.run(
            ["curl", "-fsS", "--max-time", str(int(timeout)), "--socks5-hostname", proxy,
             "-o", "/dev/null", "-w", "%{http_code} %{time_total} %{size_upload}",
             "-X", "POST", "--data-binary", "@-", url],
            input=payload, capture_output=True,
        )
        if r.returncode != 0:
            # a timeout/drop on upload is that very symptom; but the echo could simply be down → try the next one.
            last = f"curl rc={r.returncode}: {r.stderr.decode('utf-8', 'replace').strip()[:100]}"
            continue
        parts = (r.stdout.decode("ascii", "replace").strip() or "").split()
        code = parts[0] if parts else "?"
        tt = float(parts[1]) if len(parts) > 1 else 0.0
        up = int(parts[2]) if len(parts) > 2 else 0
        if code.startswith("2") and up >= len(payload):
            mbit = (up * 8 / 1e6) / tt if tt else 0
            return True, f"HTTP {code}, отдано {up // 1024}KB за {tt:.1f}s (~{mbit:.1f} Mbps)"
        last = f"HTTP {code}, отдано {up}/{len(payload)}B за {tt:.1f}s"
    return False, last


def run_live(config: dict, *, expect_ip: str | None, minutes: float, xray_bin: str,
             upload_kb: int = 512) -> int:
    """Start xray, check the exit IP, the UPLOAD direction and stability over N minutes. 0 = pass-like."""
    port = config["inbounds"][0]["port"]
    if shutil.which("curl") is None:
        print("[!] нужен curl (проверка exit-IP через локальный SOCKS5). Поставь curl.")
        return 2
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(config, f)
        cfg_path = f.name
    log_path = cfg_path + ".log"
    print(f"[*] xray config → {cfg_path}")
    print(f"[*] xray stderr → {log_path}")

    # -test: xray validates the config itself (catches a field mismatch before startup)
    test = subprocess.run([xray_bin, "run", "-test", "-c", cfg_path], capture_output=True, text=True)
    if test.returncode != 0:
        print("[!] xray -test не прошёл (конфиг невалиден):")
        print(test.stderr or test.stdout)
        return 2

    logf = open(log_path, "w")
    proc = subprocess.Popen([xray_bin, "run", "-c", cfg_path], stdout=logf, stderr=logf)
    try:
        time.sleep(3)  # give it time to come up
        if proc.poll() is not None:
            print(f"[!] xray упал сразу (код {proc.returncode}). Логи: {log_path}")
            return 2

        exit_ip = _observed_exit_ip(port)
        if exit_ip is None:
            print("[!] Трафик НЕ пошёл через цепочку (ни один echo не ответил).")
            print("    Причина неоднозначна: РФ-прокси лёг ИЛИ ТСПУ рубит хендшейк. Смотри логи.")
            return 1
        print(f"[+] Внешний IP через цепочку: {exit_ip}")
        if expect_ip:
            if exit_ip == expect_ip:
                print(f"[+] Совпал с ожидаемым WAN ноды ({expect_ip}) — выход через ноду. ✓")
            else:
                print(f"[!] НЕ совпал с ожидаемым WAN ноды ({expect_ip}).")
                print("    Возможно, выход через сам РФ-прокси (цепочка не достроилась) — проверь логи.")
                return 1

        # UPLOAD (upload/bidir) — what Telegram/calls/file uploads need
        upload_ok = True
        if upload_kb > 0:
            upload_ok, info = _upload_probe(port, upload_kb)
            if upload_ok:
                print(f"[+] Отдача ({upload_kb}KB) прошла: {info} ✓ — Telegram/звонки/выгрузка поедут.")
            else:
                print(f"[!] ОТДАЧА ВИСНЕТ: {info}")
                print("    Это ровно симптом «сайты грузятся, а выгрузка/звонки стоят». Клиент здесь")
                print("    xray-core → значит проблема в СЕРВЕРЕ/транспорте/канале, а не в клиенте. НЕ продавать.")

        # stability
        ticks = max(1, int(minutes * 60 // 15))
        ok = 0
        first_drop = None
        print(f"[*] Стабильность: {minutes:g} мин, тик 15 c ({ticks} проверок)…")
        for i in range(ticks):
            t0 = time.time()
            ip = _observed_exit_ip(port)
            dt = time.time() - t0
            if ip:
                ok += 1
                print(f"    [{i + 1:>3}/{ticks}] ok  {ip}  {dt * 1000:.0f}ms")
            else:
                if first_drop is None:
                    first_drop = i * 15
                print(f"    [{i + 1:>3}/{ticks}] DROP  (обрыв на ~{i * 15}s)")
            if proc.poll() is not None:
                print(f"[!] xray умер на тике {i + 1}. Логи: {log_path}")
                break
            time.sleep(max(0, 15 - dt))

        uptime = 100 * ok / ticks
        print(f"\n[=] Uptime цепочки: {ok}/{ticks} ({uptime:.0f}%)"
              + (f"; первый обрыв ~{first_drop}s" if first_drop is not None else "; обрывов нет")
              + f"; отдача: {'OK' if upload_ok else 'ВИСНЕТ'}")
        if uptime >= 90 and first_drop is None and upload_ok:
            print("[+] PASS-подобно: держит + отдача идёт. НО через прокси — нужен живой РФ-тестер (гейт).")
            return 0
        if not upload_ok:
            print("[!] Отдача не прошла — для Telegram/звонков транспорт непригоден, чини до продажи.")
        else:
            print("[!] Нестабильно/рвётся. Неоднозначно (прокси vs ТСПУ). Попробуй другой РФ-прокси/площадку/dest.")
        return 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        logf.close()


# --------------------------------------------------------------------------- selftest

def _selftest() -> int:
    sample = (
        "vless://11111111-2222-3333-4444-555555555555@node.example.net:443"
        "?encryption=none&security=reality&sni=dl.google.com&fp=firefox"
        "&pbk=AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHHIIIIJJJJKKK&sid=0123456789abcdef"
        "&type=xhttp&path=%2F&mode=auto#nl-1"
    )
    lf = parse_vless_link(sample)
    assert lf["uuid"] == "11111111-2222-3333-4444-555555555555", lf
    assert lf["host"] == "node.example.net" and lf["port"] == 443
    assert lf["sni"] == "dl.google.com" and lf["path"] == "/" and lf["label"] == "nl-1"

    cfg = build_client_config(lf, parse_socks("socks5://u:p@10.20.30.40:1080"))
    js = json.dumps(cfg)  # must be serializable
    proxy = cfg["outbounds"][0]
    ru = cfg["outbounds"][1]
    assert proxy["tag"] == "proxy" and proxy["protocol"] == "vless"
    ss = proxy["streamSettings"]
    assert ss["network"] == "xhttp", "network должен зеркалить сервер (xhttp)"
    assert ss["security"] == "reality"
    assert ss["realitySettings"]["serverName"] == "dl.google.com"
    assert ss["realitySettings"]["publicKey"].startswith("AAAA")
    assert ss["realitySettings"]["shortId"] == "0123456789abcdef"
    assert ss["xhttpSettings"] == {"path": "/", "mode": "auto"}
    assert ss["sockopt"]["dialerProxy"] == "ru-socks", "хендшейк ОБЯЗАН идти через РФ-прокси"
    assert ru["tag"] == "ru-socks" and ru["protocol"] == "socks"
    assert ru["settings"]["servers"][0]["users"][0] == {"user": "u", "pass": "p"}
    u0 = proxy["settings"]["vnext"][0]["users"][0]
    assert u0["encryption"] == "none" and "flow" not in u0, "flow быть не должно (XHTTP)"
    assert cfg["inbounds"][0]["listen"] == "127.0.0.1", "локальный вход только на loopback"

    # without auth — the users section is absent
    ru2 = build_client_config(lf, parse_socks("1.2.3.4:1080"))["outbounds"][1]
    assert "users" not in ru2["settings"]["servers"][0]

    # reject non-reality / non-vless
    for bad, why in [
        ("vless://x@h:443?security=tls&sni=a&pbk=b", "security!=reality"),
        ("wireguard://x@h:51820", "не vless"),
        ("vless://x@h:443?security=reality&type=tcp&sni=a&pbk=b", "type!=xhttp"),
    ]:
        try:
            parse_vless_link(bad)
        except SmokeError:
            pass
        else:
            raise AssertionError(f"должно было отклонить: {why}")

    print("[+] selftest OK — сборщик конфига зеркалит сервер, dialerProxy на месте, валидация ловит рассинхрон.")
    print(f"    (пример конфига: {len(js)} байт JSON)")
    return 0


# --------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="Смок VLESS+Reality через РФ-SOCKS5.")
    ap.add_argument("--link", help="vless://-ссылка из кабинета (/devices/add на Reality-ноде)")
    ap.add_argument("--ru-socks", help="РФ-SOCKS5: socks5://[user:pass@]host:port (ТОЛЬКО резидентский/мобильный)")
    ap.add_argument("--expect-ip", default=None, help="ожидаемый WAN-IP ноды (сверка выхода)")
    ap.add_argument("--minutes", type=float, default=8.0, help="длительность теста стабильности")
    ap.add_argument("--upload-kb", type=int, default=512,
                    help="сколько КБ отдать (проверка Telegram/звонков/выгрузки; 0 = выкл)")
    ap.add_argument("--config-only", action="store_true", help="только собрать конфиг и распечатать")
    ap.add_argument("--xray-bin", default="xray", help="путь к бинарю xray (по умолчанию из PATH)")
    ap.add_argument("--selftest", action="store_true", help="структурный self-test (без сети/xray)")
    args = ap.parse_args()

    if args.selftest:
        return _selftest()
    if not args.link or not args.ru_socks:
        ap.error("нужны --link и --ru-socks (или --selftest)")

    try:
        lf = parse_vless_link(args.link)
        socks = parse_socks(args.ru_socks)
    except SmokeError as e:
        print(f"[!] {e}")
        return 2
    cfg = build_client_config(lf, socks)

    if args.config_only:
        print(json.dumps(cfg, indent=2, ensure_ascii=False))
        print(f"\n# запусти вручную:  {args.xray_bin} run -c <файл>.json", file=sys.stderr)
        print(f"# нода: {lf['host']}:{lf['port']}  sni={lf['sni']}  label={lf['label']}", file=sys.stderr)
        return 0

    if shutil.which(args.xray_bin) is None:
        print(f"[!] бинарь xray ('{args.xray_bin}') не найден. Поставь Xray-core или используй --config-only.")
        print("    Ставится: github.com/XTLS/Xray-core/releases (тот же тег, что AGENT_XRAY_VERSION).")
        return 2
    return run_live(cfg, expect_ip=args.expect_ip, minutes=args.minutes, xray_bin=args.xray_bin,
                    upload_kb=args.upload_kb)


if __name__ == "__main__":
    raise SystemExit(main())
