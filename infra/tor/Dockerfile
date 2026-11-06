# Our own Tor image for the .onion mirror — replacing the single-maintainer osminogin/tor-simple (#11).
# Tor comes from the OFFICIAL Tor Project repository (signed key); the base is pinned by digest.
#
# CUTOVER PROCEDURE — follow it or the .onion address changes!
#   The HS private key lives in the tordata volume (/var/lib/tor/vpn_cabinet, 0700, owner uid=100
#   from osminogin). This image runs as uid=100/gid=101 → reads the same key → the .onion does NOT change.
#   Deploy: (1) back the key up  ssh: docker compose exec -T tor tar czf - -C /var/lib/tor vpn_cabinet > onion-key.tgz
#           (2) switch compose tor.image → build: {context: ., dockerfile: infra/tor/Dockerfile}
#           (3) docker compose up -d --build tor
#           (4) COMPARE the .onion: docker compose exec tor cat /var/lib/tor/vpn_cabinet/hostname
#               — if the address CHANGED, roll the image back and restore the key from the backup.
FROM debian:bookworm-slim@sha256:60eac759739651111db372c07be67863818726f754804b8707c90979bda511df

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends apt-transport-https ca-certificates gnupg wget; \
    wget -qO- https://deb.torproject.org/torproject.org/A3C4F0F979CAA22CDBA8F512EE8CBC9E886DDD89.asc \
      | gpg --dearmor > /usr/share/keyrings/tor-archive-keyring.gpg; \
    printf 'deb [signed-by=/usr/share/keyrings/tor-archive-keyring.gpg] https://deb.torproject.org/torproject.org bookworm main\n' \
      > /etc/apt/sources.list.d/tor.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends tor deb.torproject.org-keyring; \
    apt-get purge -y --auto-remove wget gnupg apt-transport-https; \
    rm -rf /var/lib/apt/lists/*; \
    # the tordata volume is owned by uid=100/gid=101 (osminogin) → remap debian-tor onto the same ids
    # so that we read the existing HS key and keep the .onion address.
    groupmod -g 101 debian-tor 2>/dev/null || groupadd -g 101 debian-tor; \
    usermod -u 100 -g 101 debian-tor 2>/dev/null || useradd -u 100 -g 101 -M -s /usr/sbin/nologin debian-tor; \
    mkdir -p /var/lib/tor; chown 100:101 /var/lib/tor

USER 100:101
# onion-only: SocksPort 0 in torrc; the healthcheck is disabled in compose (it reports a false unhealthy)
CMD ["tor", "-f", "/etc/tor/torrc"]
