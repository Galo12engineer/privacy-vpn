#!/usr/bin/env bash
# Idempotent provisioning of a VPN node (Debian/Ubuntu). RAM-only, no-swap, no-disk-logs.
# Launched from cloud-init or manually by root. Re-running is safe.
#
# Required env (passed by cloud-init):
#   CP_BASE_URL           — control plane URL (via Tor/clearnet)
#   AGENT_REGISTRATION_TOKEN — one-time registration token
#   AGENT_SUBNET_V4       — e.g. 10.7.0.0/24
#   AGENT_WAN_INTERFACE   — e.g. eth0
# Optional: AGENT_SUBNET_V6, AGENT_WG_LISTEN_PORT(=51820), AGENT_WG_LISTEN_PORT_ALT(=443),
#   AGENT_DNS_LISTEN_IP(=10.7.0.1),
#   AGENT_CONTROL_ALLOW_CIDRS — control-plane IP for the control channel (empty = port closed from WAN)
set -euo pipefail

: "${CP_BASE_URL:?CP_BASE_URL required}"
: "${AGENT_REGISTRATION_TOKEN:?AGENT_REGISTRATION_TOKEN required}"
: "${AGENT_SUBNET_V4:?AGENT_SUBNET_V4 required}"
: "${AGENT_WAN_INTERFACE:=eth0}"
: "${AGENT_WG_LISTEN_PORT:=51820}"
: "${AGENT_WG_LISTEN_PORT_ALT:=443}"
: "${AGENT_DNS_LISTEN_IP:=10.7.0.1}"
: "${AGENT_SUBNET_V6:=}"
: "${AGENT_CONTROL_ALLOW_CIDRS:=}"
# node transport: wireguard (default) or amneziawg (DPI obfuscation). Only used to gate
# package installation at provisioning time — the obfuscation profile itself comes from the CP at registration.
: "${AGENT_PROTOCOL:=wireguard}"

echo "[*] 1/8 packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq wireguard-tools nftables unbound python3 python3-venv python3-pip git curl

# AmneziaWG (obfuscated WG): kernel module via DKMS + awg-tools. Installed ONLY for
# amneziawg nodes (the DKMS build requires linux-headers for the running kernel; we don't pull it onto WG nodes).
if [ "${AGENT_PROTOCOL}" = "amneziawg" ]; then
  echo "[*] 1b/8 AmneziaWG (PPA amnezia → amneziawg + amneziawg-tools, DKMS)"
  apt-get install -y -qq gnupg2 "linux-headers-$(uname -r)"
  install -d -m 0755 /usr/share/keyrings
  # GPG key of the amnezia PPA (Launchpad, key 57290828) into a separate keyring (apt-key is deprecated)
  tmp_kr="$(mktemp)"
  gpg --no-default-keyring --keyring "$tmp_kr" \
      --keyserver keyserver.ubuntu.com --recv-keys 57290828
  gpg --no-default-keyring --keyring "$tmp_kr" --export > /usr/share/keyrings/amnezia.gpg
  rm -f "$tmp_kr"
  # DEB822 source (Debian 12/13 friendly); suite focal — the PPA packages are built for focal,
  # amneziawg-dkms builds the module from source against the local kernel
  cat >/etc/apt/sources.list.d/amnezia.sources <<EOF
Types: deb
URIs: https://ppa.launchpadcontent.net/amnezia/ppa/ubuntu
Suites: focal
Components: main
Signed-By: /usr/share/keyrings/amnezia.gpg
EOF
  apt-get update -qq
  apt-get install -y -qq amneziawg amneziawg-tools
  modprobe amneziawg
  # critical: without the module the agent cannot bring up `ip link add type amneziawg`
  if ! lsmod | grep -q '^amneziawg'; then
    echo "[!] amneziawg module did not load (DKMS build failed?) — aborting" >&2
    exit 1
  fi
fi

# VLESS+Reality (Xray-core): installed only for vless_reality nodes. Xray lives alongside WG;
# the agent generates the config in tmpfs (/run/xray), logs are disabled (no-logs). For a vless node
# AGENT_WG_LISTEN_PORT = the reality port (usually 443) and it must match endpoint_port in the CP.
if [ "${AGENT_PROTOCOL}" = "vless_reality" ]; then
  echo "[*] 1c/8 Xray-core (VLESS+Reality+XHTTP)"
  apt-get install -y -qq unzip
  # official XTLS installer (installs /usr/local/bin/xray). AGENT_XRAY_VERSION pins
  # the version (RECOMMENDED for reproducibility/supply-chain); empty → latest.
  xray_args="install"
  [ -n "${AGENT_XRAY_VERSION:-}" ] && xray_args="install --version ${AGENT_XRAY_VERSION}"
  # shellcheck disable=SC2086
  bash -c "$(curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ ${xray_args}
  # our own systemd unit: config from tmpfs (written by the agent), stdout/stderr to null (NOT to journald →
  # not to disk, honest no-logs). Runs as root (simple permissions on the tmpfs config and
  # the abstract socket @xhttp). The agent owns the lifecycle (systemctl restart xray in
  # _apply_xray AFTER generating the config), therefore we do NOT enable it on boot.
  cat >/etc/systemd/system/xray.service <<'XRAYEOF'
[Unit]
Description=Xray (VLESS+Reality) privacyvpn
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/xray run -c /run/xray/config.json
Restart=on-failure
RestartSec=3
RuntimeDirectory=xray
RuntimeDirectoryPreserve=yes
StandardOutput=null
StandardError=null
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
XRAYEOF
  systemctl daemon-reload
  # suppress the installer's auto-start: the agent starts xray after generating the config
  systemctl disable xray 2>/dev/null || true
  systemctl stop xray 2>/dev/null || true
  echo "[*]   Xray installed; the agent will generate /run/xray/config.json and start the service"
  echo "[*]   steal-oneself: if dest points at YOUR OWN domain — bring up the decoy web server + cert separately"
fi

echo "[*] 2/8 ip_forward"
cat >/etc/sysctl.d/99-vpn.conf <<EOF
net.ipv4.ip_forward=1
net.ipv6.conf.all.forwarding=1
EOF
sysctl -p /etc/sysctl.d/99-vpn.conf >/dev/null

echo "[*] 3/8 no-swap (no RAM leak to disk)"
swapoff -a || true
# remove swap from fstab (idempotent)
sed -i.bak '/\sswap\s/d' /etc/fstab || true
systemctl mask "dev-*.swap" 2>/dev/null || true
# critical for privacy: is swap really off? otherwise secrets/traffic leak to disk → stop
if [ -n "$(swapon --show --noheadings 2>/dev/null)" ]; then
  echo "[!] swap is still active — the RAM-only guarantee is broken, aborting" >&2
  exit 1
fi

echo "[*] 4/8 journald in RAM (logs are not written to disk)"
mkdir -p /etc/systemd/journald.conf.d
cat >/etc/systemd/journald.conf.d/volatile.conf <<EOF
[Journal]
Storage=volatile
RuntimeMaxUse=32M
ForwardToSyslog=no
EOF
systemctl restart systemd-journald

echo "[*] 5/8 per-node no-log DNS (unbound, RAM, no query logging)"
install -m 0644 "$(dirname "$0")/dns/unbound-nolog.conf" /etc/unbound/unbound.conf.d/vpn-nolog.conf
sed -i "s|@DNS_LISTEN_IP@|${AGENT_DNS_LISTEN_IP}|g" /etc/unbound/unbound.conf.d/vpn-nolog.conf
systemctl enable --now unbound
systemctl restart unbound

echo "[*] SSH hardening (node has a public IP, 22/tcp is open in nft — keys only, no root password)"
# lockout guard: we disable password login ONLY if a key is already authorized
if grep -rqs "ssh-" /root/.ssh/authorized_keys /home/*/.ssh/authorized_keys 2>/dev/null; then
  install -d -m 0755 /etc/ssh/sshd_config.d
  cat >/etc/ssh/sshd_config.d/99-hardening.conf <<'SSHEOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
SSHEOF
  systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
  # fail2ban against brute force.
  # GOTCHA (bit us on ro-1 2026-07-16, and nl-1 had been silently broken since day one):
  # step 4/8 sets journald Storage=volatile (no disk traces) → /var/log/auth.log does NOT exist →
  # the stock sshd jail ("auto" backend looks for the FILE) dies with
  #   "Failed during configuration: Have not found any log file for sshd jail"  (exit 255).
  # `|| true` then swallowed it, so provisioning reported success while fail2ban was dead.
  # Fix: backend = systemd → it reads the in-RAM journal, so no-logs-to-disk still holds.
  if apt-get install -y -qq fail2ban 2>/dev/null; then
    cat >/etc/fail2ban/jail.d/sshd.conf <<'F2BEOF'
[sshd]
enabled = true
backend = systemd
maxretry = 5
bantime = 1h
F2BEOF
    systemctl enable --now fail2ban 2>/dev/null || true
    systemctl restart fail2ban 2>/dev/null || true
    # do NOT lie about success: report loudly instead of hiding behind `|| true`
    if systemctl is-active --quiet fail2ban; then
      echo "[+] fail2ban active (jail sshd, backend=systemd)"
    else
      echo "[!] fail2ban did NOT come up — check: journalctl -u fail2ban" >&2
    fi
  else
    echo "[!] fail2ban failed to install" >&2
  fi
else
  echo "[!] no authorized SSH key — skipping the disabling of password login (otherwise lockout)" >&2
fi

echo "[*] 6/8 node-agent (code in /opt, secrets only in env/RAM)"
if [ ! -d /opt/vpn-node-agent ]; then
  if [ -n "${AGENT_REPO_URL:-}" ]; then
    # operator-provided PINNED reference (tag/commit), code in /opt/vpn-src
    git clone --depth 1 "${AGENT_REPO_URL}" /opt/vpn-src
  else
    mkdir -p /opt/vpn-node-agent  # the usual path: code is delivered by rsync (cloud-init/CI)
  fi
fi
python3 -m venv /opt/vpn-node-agent/.venv
/opt/vpn-node-agent/.venv/bin/pip install -q --upgrade pip
# the repo is not on PyPI → the agent is installed from a local source (delivered by rsync into /opt/vpn-src)
/opt/vpn-node-agent/.venv/bin/pip install -q /opt/vpn-src/node-agent

echo "[*] 7/8 agent env (persist: the config must survive a reboot; node secrets — in the state-dir)"
# PERSISTENT (not /run tmpfs — otherwise after a reboot the EnvironmentFile is empty and the agent won't start).
# The registration token is one-time: after the first boot the node stores its identity in the state-dir and
# comes back via reattach, the token is no longer needed.
install -d -m 0700 /etc/vpn-node-agent
install -d -m 0700 /var/lib/vpn-node-agent   # server WG private key + identity (persist)
cat >/etc/vpn-node-agent/agent.env <<EOF
AGENT_CP_BASE_URL=${CP_BASE_URL}
AGENT_REGISTRATION_TOKEN=${AGENT_REGISTRATION_TOKEN}
AGENT_SUBNET_V4=${AGENT_SUBNET_V4}
AGENT_SUBNET_V6=${AGENT_SUBNET_V6}
AGENT_WAN_INTERFACE=${AGENT_WAN_INTERFACE}
AGENT_WG_LISTEN_PORT=${AGENT_WG_LISTEN_PORT}
AGENT_WG_LISTEN_PORT_ALT=${AGENT_WG_LISTEN_PORT_ALT}
AGENT_DNS_LISTEN_IP=${AGENT_DNS_LISTEN_IP}
AGENT_CONTROL_ALLOW_CIDRS=${AGENT_CONTROL_ALLOW_CIDRS}
AGENT_STATE_DIR=/var/lib/vpn-node-agent
AGENT_BOOTSTRAP=true
EOF
chmod 0600 /etc/vpn-node-agent/agent.env

echo "[*] 8/8 agent systemd unit"
install -m 0644 "$(dirname "$0")/systemd/vpn-node-agent.service" /etc/systemd/system/vpn-node-agent.service
systemctl daemon-reload
systemctl enable --now vpn-node-agent.service

echo "[+] node is up (${AGENT_PROTOCOL}): ports ${AGENT_WG_LISTEN_PORT}+${AGENT_WG_LISTEN_PORT_ALT}, the agent will register with the CP"
