"""AmneziaWG obfuscation profile: Jc, Jmin, Jmax, S1, S2, H1-H4.

Constraints (README amneziawg-linux-kernel-module + docs.amnezia.org):
- Jc 1..128 (we take 4..12) — the number of junk packets before the handshake;
- Jmin < Jmax <= 1280 — junk packet size (not close to MTU, otherwise fragmentation);
- S1 <= 1132, S2 <= 1188 and MANDATORY S1 + 56 != S2 (otherwise init+S1 will match in length
  with resp+S2 and the obfuscation breaks) — padding of the handshake packets;
- H1..H4 — substituted headers (init/response/underload/transport): distinct, > 4,
  range 5..2147483647 (the upper bound is limited for AmneziaWG client compatibility).

KEY POINT: S1,S2,H1-H4 change the packet format → they MUST MATCH server↔client.
That is why the profile is generated once per node (CP — source of truth) and is used both in
the node config and in the client config. H1-H4 are random → different nodes have a different signature.
"""

from __future__ import annotations

import secrets

FIELDS = ("jc", "jmin", "jmax", "s1", "s2", "h1", "h2", "h3", "h4")
H_MIN = 5
H_MAX = 2147483647  # INT32_MAX — upper bound of AmneziaWG client compatibility


def _rand(lo: int, hi: int) -> int:
    """Random integer in [lo, hi] inclusive (CSPRNG)."""
    return lo + secrets.randbelow(hi - lo + 1)


def generate_profile() -> dict[str, int]:
    """Generate a valid obfuscation profile (all constraints satisfied)."""
    jc = _rand(4, 12)
    jmin, jmax = 40, 70  # jmin < jmax <= 1280
    s1 = _rand(15, 150)
    s2 = _rand(15, 150)
    while s1 + 56 == s2:  # forbidden equality of the init+S1 and resp+S2 lengths
        s2 = _rand(15, 150)
    headers: set[int] = set()
    while len(headers) < 4:
        headers.add(_rand(H_MIN, H_MAX))
    h1, h2, h3, h4 = sorted(headers)  # distinct and > 4 by construction
    return {
        "jc": jc, "jmin": jmin, "jmax": jmax,
        "s1": s1, "s2": s2, "h1": h1, "h2": h2, "h3": h3, "h4": h4,
    }


def profile_from_node(node) -> dict[str, int] | None:
    """Profile from the node's awg_* fields, or None if it is not fully set."""
    vals = {f: getattr(node, f"awg_{f}", None) for f in FIELDS}
    if any(v is None for v in vals.values()):
        return None
    return vals


def validate_profile(p: dict[str, int]) -> None:
    """Check the profile constraints; ValueError on violation (admin panel/tests)."""
    if not 1 <= p["jc"] <= 128:
        raise ValueError("Jc должен быть в диапазоне 1..128")
    if not p["jmin"] < p["jmax"] <= 1280:
        raise ValueError("нужно Jmin < Jmax <= 1280")
    if p["s1"] > 1132 or p["s2"] > 1188:
        raise ValueError("нужно S1 <= 1132 и S2 <= 1188")
    if p["s1"] + 56 == p["s2"]:
        raise ValueError("S1 + 56 не должно равняться S2")
    headers = [p["h1"], p["h2"], p["h3"], p["h4"]]
    if len(set(headers)) != 4 or any(h <= 4 for h in headers):
        raise ValueError("H1-H4 должны быть различны и больше 4")
    if any(h > H_MAX for h in headers):
        raise ValueError(f"H1-H4 не должны превышать {H_MAX}")
