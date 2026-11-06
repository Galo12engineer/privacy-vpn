"""Node startup. #14: the data-plane (wg+nft) is brought up from last-known FIRST, then
(re)registration with the CP with backoff — an unreachable CP at startup does NOT crash the agent
and does not cut off existing clients (reboot-survivable regression). Launched from run.py."""

from __future__ import annotations

import logging
import time

from agent import cp_pinning, nft, registration, store, tls, xray
from agent.config import AgentSettings
from agent.wg import Runner, WireGuard, default_runner, tool_for_protocol

log = logging.getLogger("agent.bootstrap")


def wg_bringup_commands(
    settings: AgentSettings,
    *,
    server_address: str,
    listen_port: int,
    private_key_path: str,
    protocol: str = "wireguard",
    amnezia: dict | None = None,
) -> list[list[str]]:
    """Interface bring-up commands. For amneziawg: link-type `amneziawg` + an inline obfuscation
    profile via `awg set` (tokens jc/jmin/jmax/s1/s2/h1-h4 — verified against set.c)."""
    iface = settings.wg_interface
    tool = tool_for_protocol(protocol)
    link_type = "amneziawg" if protocol == "amneziawg" else "wireguard"
    set_cmd = [
        tool, "set", iface, "listen-port", str(listen_port), "private-key", private_key_path
    ]
    if protocol == "amneziawg" and amnezia:
        # S1,S2,H1-H4 change the packet format → must match the client config (the CP generates it)
        for key in ("jc", "jmin", "jmax", "s1", "s2", "h1", "h2", "h3", "h4"):
            set_cmd += [key, str(amnezia[key])]
    cmds: list[list[str]] = []
    if protocol == "amneziawg":
        cmds.append(["modprobe", "amneziawg"])  # the kernel module is needed for `ip link add type amneziawg`
    cmds += [
        ["ip", "link", "add", "dev", iface, "type", link_type],
        set_cmd,
        ["ip", "addr", "add", server_address, "dev", iface],
        ["ip", "link", "set", iface, "up"],
    ]
    return cmds


def bring_up_interface(
    settings: AgentSettings,
    *,
    server_address: str,
    listen_port: int,
    private_key_path: str,
    protocol: str = "wireguard",
    amnezia: dict | None = None,
    runner: Runner = default_runner,
) -> None:
    # the private key already lives in the persist directory (store.load_or_create_wg_key), 0600 — wg takes it by path.
    # Idempotency: we tear down the existing wg0 before creating it — on an agent restart the interface
    # survived (the kernel object outlives the process) and `ip link add` would fail. On a real reboot
    # the interface is absent → del is simply a no-op (we swallow the error).
    try:
        runner(["ip", "link", "del", "dev", settings.wg_interface])
    except Exception:  # noqa: BLE001 — the interface may not exist (reboot); that is normal
        pass
    for cmd in wg_bringup_commands(
        settings, server_address=server_address, listen_port=listen_port,
        private_key_path=private_key_path, protocol=protocol, amnezia=amnezia,
    ):
        runner(cmd)


def _apply_dataplane(settings: AgentSettings, cfg: dict, *, key_path: str, runner: Runner) -> None:
    """Idempotently bring up wg + nft from the network config (cache or a fresh reg). Without peers."""
    bring_up_interface(
        settings,
        server_address=cfg["server_address"],
        listen_port=settings.wg_listen_port,
        private_key_path=key_path,
        protocol=cfg.get("protocol", "wireguard"),
        amnezia=cfg.get("amnezia"),
        runner=runner,
    )
    ruleset = nft.render_ruleset(
        wan_interface=settings.wan_interface,
        subnet_v4=cfg["subnet_v4"],
        subnet_v6=cfg.get("subnet_v6"),
        wg_interface=settings.wg_interface,
        wg_listen_port=settings.wg_listen_port,
        wg_listen_port_alt=settings.wg_listen_port_alt,
        control_port=settings.listen_port,
        control_allow_cidrs=[c for c in settings.control_allow_cidrs.split(",") if c.strip()],
        protocol=cfg.get("protocol", "wireguard"),
    )
    nft.apply_ruleset(ruleset, runner=runner)


def _apply_xray(settings: AgentSettings, cfg: dict, state, *, runner: Runner) -> None:
    """vless_reality → bring up Xray with the reality profile and the users from cfg['peers']
    (empty in the data-plane-first case without the CP; users will arrive via reattach → Xray restart)."""
    reality = cfg.get("reality")
    if cfg.get("protocol") != "vless_reality" or not reality:
        return
    users = [
        {"uuid": p["uuid"], "email": p["email"]}
        for p in cfg.get("peers", [])
        if p.get("protocol") == "vless_reality"
    ]
    state.xray = xray.Xray(
        config_path=settings.xray_config_path, api_addr=settings.xray_api_addr, runner=runner
    )
    state.xray.write_config(
        xray.render_config(reality, listen_port=settings.wg_listen_port, users=users)
    )
    state.xray.restart()


def _enroll_once(settings: AgentSettings, identity, *, public_key: str, tls_cert_pem: str, http):
    if identity:
        reg = registration.reattach(
            settings, node_id=identity["node_id"], agent_secret=identity["agent_secret"],
            public_key=public_key, tls_cert_pem=tls_cert_pem, client=http,
        )
        reg.setdefault("node_id", identity["node_id"])
        reg.setdefault("agent_secret", identity["agent_secret"])
        return reg
    return registration.register(settings, public_key=public_key, tls_cert_pem=tls_cert_pem, client=http)


def bootstrap(
    settings: AgentSettings,
    state,
    *,
    runner: Runner = default_runner,
    http=None,
    enroll_attempts: int = 3,
    enroll_backoff: float = 3.0,
    sleep=time.sleep,
) -> dict:
    # #13: fail-closed — we do not enroll over an insecure channel (agent_secret would leak to a MITM)
    if not settings.is_cp_secure():
        raise RuntimeError(
            f"cp_base_url is insecure (https:// or .onion required): {settings.cp_base_url}"
        )
    # #13-extra: TOFU pinning of the CP cert — an extra layer on top of the CA check, best-effort (does not break enroll)
    try:
        cp_pinning.check_and_pin(settings.state_dir, settings.cp_base_url)
    except Exception as e:  # noqa: BLE001 — extra protection, must not break bootstrap
        log.warning("cp cert pinning skipped: %s", type(e).__name__)
    # persisting the WG key: the server pubkey is STABLE across reboots → client configs do not break
    _priv, pub, key_path = store.load_or_create_wg_key(settings.state_dir)
    # H1: the self-signed TLS of the control channel is generated in RAM on every start; the CP re-pins it on
    # register/reattach. uvicorn will come up with this cert.
    cert = tls.generate_self_signed()
    state.tls_cert_path, state.tls_key_path = tls.write_materials(cert)
    state.wg = WireGuard(settings.wg_interface, runner=runner)

    identity = store.load_identity(settings.state_dir)
    cached = store.load_config(settings.state_dir)

    # #14: DATA-PLANE FIRST — if a last-known net-config exists, we bring up wg+nft BEFORE contacting
    # the CP. Then an unreachable CP at startup does not bring down the agent and (with a stable key) does not
    # break the tunnel infrastructure. Peers will be restored once reattach succeeds.
    if cached and cached.get("server_address"):
        _apply_dataplane(settings, cached, key_path=key_path, runner=runner)
        # peer management tool (awg/wg) per the protocol from the cache (the CP may have been unreachable)
        state.wg = WireGuard(
            settings.wg_interface, tool=tool_for_protocol(cached.get("protocol")), runner=runner
        )
        _apply_xray(settings, cached, state, runner=runner)  # without users until reattach

    # (re)registration with backoff. If there is a cache → a CP failure is NOT fatal; first boot without a cache →
    # there is nothing to bring up without the CP, so we propagate the error (systemd will restart).
    reg = None
    last_err: Exception | None = None
    for attempt in range(max(1, enroll_attempts)):
        try:
            reg = _enroll_once(settings, identity, public_key=pub, tls_cert_pem=cert.cert_pem, http=http)
            break
        except Exception as e:  # noqa: BLE001 — network/CP down; we decide based on cache presence below
            last_err = e
            log.warning("enroll %d/%d failed: %s", attempt + 1, enroll_attempts, type(e).__name__)
            if attempt + 1 < enroll_attempts:
                sleep(enroll_backoff * (attempt + 1))

    if reg is None:
        if cached and identity:
            log.warning("CP unreachable — node runs from last-known config; reattach will retry")
            state.node_id = identity["node_id"]
            state.agent_secret = identity["agent_secret"]
            return {**cached, "node_id": identity["node_id"],
                    "agent_secret": identity["agent_secret"], "peers": []}
        raise last_err if last_err else RuntimeError("enroll failed")  # first boot without a cache

    # enroll succeeded
    if not identity:
        store.save_identity(settings.state_dir, node_id=reg["node_id"], agent_secret=reg["agent_secret"])
    state.node_id = reg["node_id"]
    state.agent_secret = reg["agent_secret"]
    store.save_config(
        settings.state_dir, server_address=reg["server_address"],
        subnet_v4=reg["subnet_v4"], subnet_v6=reg.get("subnet_v6"),
        protocol=reg.get("protocol", "wireguard"), amnezia=reg.get("amnezia"),
        reality=reg.get("reality"),
    )
    # re-apply the data-plane with the fresh config (idempotent; first boot or a subnet change)
    _apply_dataplane(settings, reg, key_path=key_path, runner=runner)
    # peer management tool (awg/wg) per the node protocol from reg
    state.wg = WireGuard(
        settings.wg_interface, tool=tool_for_protocol(reg.get("protocol")), runner=runner
    )
    # peer reconciliation: active WG peers from reattach back onto wg (after a reboot the state is
    # empty). We skip VLESS users — they are applied via Xray (_apply_xray below).
    for p in reg.get("peers", []):
        if p.get("protocol") == "vless_reality":
            continue
        state.wg.add_peer(p["public_key"], p["allowed_ips"], p.get("preshared_key"))
    _apply_xray(settings, reg, state, runner=runner)  # render the config with VLESS users + restart
    return reg
